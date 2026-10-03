"""Same-origin site chat proxy to the authenticated local Codex app-server."""

import asyncio
import json
import logging
from contextlib import suppress
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from websockets.asyncio.client import connect

from app.services.codex_chat import EFFORT, MODEL, CodexRPC
from app.services.codex_health import read_health

router = APIRouter(prefix="/api/codex", tags=["codex"])
logger = logging.getLogger(__name__)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=8000)
    timezone: str = Field(default="UTC", max_length=64)


def same_origin(origin: str | None, host: str | None) -> bool:
    if not origin or not host:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.netloc == host


async def relay(websocket: WebSocket, rpc: CodexRPC, thread_id: str):
    active = False
    cancel_requested = False
    turn_id = None
    pending = {}
    readers = {
        asyncio.create_task(websocket.receive_json()): "browser",
        asyncio.create_task(rpc.receive()): "codex",
    }
    try:
        while True:
            done, _ = await asyncio.wait(
                readers,
                timeout=600 if active else 1800,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await websocket.send_json(
                    {
                        "type": "error",
                        "text": "Codex yanıtı zaman aşımına uğradı. "
                        "Sohbeti yeniden bağlayıp deneyebilirsin.",
                    }
                )
                return
            for task in done:
                source = readers.pop(task)
                message = task.result()
                readers[
                    asyncio.create_task(
                        websocket.receive_json() if source == "browser" else rpc.receive()
                    )
                ] = source
                if source == "browser":
                    if not isinstance(message, dict):
                        await websocket.send_json({"type": "error", "text": "Bir soru yaz."})
                        continue
                    if message.get("type") == "cancel":
                        cancel_requested = active
                        if active and turn_id:
                            pending[
                                await rpc.request(
                                    "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
                                )
                            ] = "interrupt"
                            cancel_requested = False
                        if active:
                            await websocket.send_json(
                                {
                                    "type": "working",
                                    "text": "Yanıt durduruluyor…",
                                }
                            )
                        continue
                    if active:
                        await websocket.send_json(
                            {
                                "type": "notice",
                                "text": "Codex mevcut soruyu yanıtlıyor. "
                                "Bitmesini bekleyebilirsin.",
                            }
                        )
                        continue
                    try:
                        body = ChatMessage.model_validate(message)
                        if not body.text.strip():
                            raise ValueError("Bir soru yaz.")
                        ZoneInfo(body.timezone)
                    except (ValidationError, ValueError, KeyError):
                        await websocket.send_json(
                            {
                                "type": "error",
                                "text": "Geçerli bir soru yaz (en fazla 8000 karakter) "
                                "ve saat dilimini kontrol et.",
                            }
                        )
                        continue
                    request_id = await rpc.request(
                        "turn/start",
                        {
                            "threadId": thread_id,
                            "input": [
                                {
                                    "type": "text",
                                    "text": f"Kullanıcının saat dilimi: {body.timezone}\n\n"
                                    f"{body.text}",
                                }
                            ],
                            "model": MODEL,
                            "effort": EFFORT,
                        },
                    )
                    pending[request_id] = "start"
                    active = True
                    cancel_requested = False
                    await websocket.send_json({"type": "working", "text": "Codex düşünüyor…"})
                    continue
                method = message.get("method")
                params = message.get("params", {})
                if method is None:
                    action = pending.pop(message.get("id"), None)
                    if "error" in message:
                        if action == "start":
                            active = False
                        await websocket.send_json(
                            {
                                "type": "error",
                                "text": message["error"].get(
                                    "message", "Codex isteği tamamlanamadı."
                                ),
                            }
                        )
                    elif action == "start":
                        turn_id = message["result"]["turn"]["id"]
                        if cancel_requested:
                            pending[
                                await rpc.request(
                                    "turn/interrupt",
                                    {
                                        "threadId": thread_id,
                                        "turnId": turn_id,
                                    },
                                )
                            ] = "interrupt"
                            cancel_requested = False
                    continue
                if "id" in message:
                    if method != "item/tool/call" or params.get("threadId") != thread_id:
                        await rpc.reject(message)
                        continue
                    await websocket.send_json(
                        {"type": "working", "text": "Sağlık arşivi sorgulanıyor…"}
                    )
                    try:
                        if params.get("tool") != "health_archive":
                            raise ValueError("Bu sağlık aracı desteklenmiyor.")
                        arguments = params["arguments"]
                        if isinstance(arguments, str):
                            arguments = json.loads(arguments)
                        data = await read_health(arguments)
                        success = True
                    except (ValueError, KeyError):
                        data = {
                            "error": "Sorgu geçersiz. Aracın tarih, saat dilimi "
                            "ve sayfa sınırlarını kontrol et."
                        }
                        success = False
                    except Exception:
                        logger.exception("Codex health archive query failed")
                        data = {
                            "error": "Sağlık arşivi okunamadı. Veritabanı bağlantısını kontrol et."
                        }
                        success = False
                    await rpc.send(
                        {
                            "id": message["id"],
                            "result": {
                                "success": success,
                                "contentItems": [
                                    {
                                        "type": "inputText",
                                        "text": json.dumps(data, ensure_ascii=False),
                                    }
                                ],
                            },
                        }
                    )
                    continue
                if params.get("threadId") != thread_id:
                    continue
                if method == "item/agentMessage/delta":
                    await websocket.send_json(
                        {"type": "delta", "id": params["itemId"], "text": params["delta"]}
                    )
                elif (
                    method == "item/completed"
                    and params.get("item", {}).get("type") == "agentMessage"
                ):
                    item = params["item"]
                    await websocket.send_json(
                        {"type": "message", "id": item["id"], "text": item["text"]}
                    )
                elif method == "turn/completed":
                    active = False
                    cancel_requested = False
                    turn_id = None
                    turn = params["turn"]
                    if turn["status"] == "failed":
                        await websocket.send_json(
                            {
                                "type": "error",
                                "text": (turn.get("error") or {}).get(
                                    "message", "Codex yanıt üretemedi."
                                ),
                            }
                        )
                    await websocket.send_json({"type": "done", "status": turn["status"]})
    finally:
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        if active and turn_id:
            with suppress(Exception):
                await rpc.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})


@router.websocket("/chat")
async def chat(websocket: WebSocket):
    if not same_origin(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    settings = websocket.app.state.settings
    if not settings.codex_bridge_token:
        await websocket.send_json(
            {
                "type": "error",
                "text": "Codex bağlantısı kurulmamış. "
                "Bilgisayarında scripts/start_codex_bridge.py çalıştır.",
            }
        )
        await websocket.close()
        return
    try:
        async with connect(
            settings.codex_bridge_url,
            additional_headers={"Authorization": f"Bearer {settings.codex_bridge_token}"},
            open_timeout=10,
            max_size=4 * 1024 * 1024,
        ) as connection:
            rpc = CodexRPC(connection)
            async with asyncio.timeout(60):
                thread_id, model = await rpc.start()
            await websocket.send_json({"type": "ready", "model": model, "effort": EFFORT})
            await relay(websocket, rpc, thread_id)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.warning("Local Codex chat connection unavailable", exc_info=False)
        with suppress(Exception):
            await websocket.send_json(
                {
                    "type": "error",
                    "text": "Yerel Codex'e bağlanılamadı. "
                    "Bilgisayarında Codex köprüsünü çalıştırıp "
                    "ChatGPT oturumunu kontrol et; ardından Yeniden bağlan'a bas.",
                }
            )
    finally:
        with suppress(Exception):
            await websocket.close()
