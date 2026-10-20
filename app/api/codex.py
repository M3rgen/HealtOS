"""Same-origin site chat proxy to the authenticated local Codex app-server."""

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from websockets.asyncio.client import connect

from app.core.auth import request_authenticated
from app.services.codex_chat import EFFORT, MODEL, CodexRPC, JsonObject
from app.services.codex_health import read_health

router = APIRouter(prefix="/api/codex", tags=["codex"])
logger = logging.getLogger(__name__)

# The browser reconnects with backoff while the chat panel is open, so an absent bridge
# (e.g. a server deployment without the host Codex CLI) would log on every attempt.
# Warn once per outage; repeats go to debug until a connection succeeds again.
_bridge_unavailable_logged = False


def _log_bridge_unavailable() -> None:
    global _bridge_unavailable_logged
    if _bridge_unavailable_logged:
        logger.debug("Local Codex chat connection still unavailable")
        return
    _bridge_unavailable_logged = True
    logger.warning(
        "Local Codex chat connection unavailable; further failures are logged at debug "
        "level until the bridge connects",
        exc_info=False,
    )


def _log_bridge_available() -> None:
    global _bridge_unavailable_logged
    if _bridge_unavailable_logged:
        logger.info("Local Codex chat connection restored")
    _bridge_unavailable_logged = False


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


PendingAction = Literal["start", "interrupt"]
Source = Literal["browser", "codex"]

# Seconds without any message before the chat gives up: during a turn / while idle.
ACTIVE_TURN_TIMEOUT_SECONDS = 600
IDLE_TIMEOUT_SECONDS = 1800


async def run_health_tool(params: JsonObject) -> tuple[object, bool]:
    """Answer one ``health_archive`` tool call; returns (result payload, success)."""
    try:
        if params.get("tool") != "health_archive":
            raise ValueError("Bu sağlık aracı desteklenmiyor.")
        arguments = params["arguments"]
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        return await read_health(arguments), True
    except (ValueError, KeyError):
        return {
            "error": "Sorgu geçersiz. Aracın tarih, saat dilimi ve sayfa sınırlarını kontrol et."
        }, False
    except Exception:
        logger.exception("Codex health archive query failed")
        return {"error": "Sağlık arşivi okunamadı. Veritabanı bağlantısını kontrol et."}, False


class ChatRelay:
    """Turn state for one browser chat socket bound to one ephemeral Codex thread.

    ``active`` is true from the browser's question until Codex reports ``turn/completed``
    (or rejects ``turn/start``). ``turn_id`` is only known once ``turn/start`` answers, so
    a cancel that arrives earlier is remembered in ``cancel_requested`` and sent as soon as
    the id arrives. ``pending`` maps our outstanding JSON-RPC request ids to their purpose.
    """

    def __init__(self, websocket: WebSocket, rpc: CodexRPC, thread_id: str) -> None:
        self.websocket = websocket
        self.rpc = rpc
        self.thread_id = thread_id
        self.active = False
        self.cancel_requested = False
        self.turn_id: str | None = None
        self.pending: dict[int, PendingAction] = {}

    @property
    def timeout(self) -> float:
        return ACTIVE_TURN_TIMEOUT_SECONDS if self.active else IDLE_TIMEOUT_SECONDS

    async def send(self, kind: str, text: str) -> None:
        await self.websocket.send_json({"type": kind, "text": text})

    async def interrupt(self, turn_id: str) -> None:
        request_id = await self.rpc.request(
            "turn/interrupt", {"threadId": self.thread_id, "turnId": turn_id}
        )
        self.pending[request_id] = "interrupt"
        self.cancel_requested = False

    async def interrupt_on_close(self) -> None:
        """Stop a running turn when the browser leaves; the socket may already be gone."""
        if self.active and self.turn_id:
            with suppress(Exception):
                await self.rpc.request(
                    "turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}
                )

    # Browser -> Codex

    async def on_browser(self, message: Any) -> None:
        if not isinstance(message, dict):
            await self.send("error", "Bir soru yaz.")
            return
        if message.get("type") == "cancel":
            await self.cancel()
            return
        if self.active:
            await self.send("notice", "Codex mevcut soruyu yanıtlıyor. Bitmesini bekleyebilirsin.")
            return
        await self.start_turn(cast(JsonObject, message))

    async def cancel(self) -> None:
        self.cancel_requested = self.active
        if self.active and self.turn_id:
            await self.interrupt(self.turn_id)
        if self.active:
            await self.send("working", "Yanıt durduruluyor…")

    async def start_turn(self, message: JsonObject) -> None:
        try:
            body = ChatMessage.model_validate(message)
            if not body.text.strip():
                raise ValueError("Bir soru yaz.")
            ZoneInfo(body.timezone)
        except (ValidationError, ValueError, KeyError):
            await self.send(
                "error",
                "Geçerli bir soru yaz (en fazla 8000 karakter) ve saat dilimini kontrol et.",
            )
            return
        request_id = await self.rpc.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": f"Kullanıcının saat dilimi: {body.timezone}\n\n{body.text}",
                    }
                ],
                "model": MODEL,
                "effort": EFFORT,
            },
        )
        self.pending[request_id] = "start"
        self.active = True
        self.cancel_requested = False
        await self.send("working", "Codex düşünüyor…")

    # Codex -> browser

    async def on_codex(self, message: JsonObject) -> None:
        method = message.get("method")
        params = message.get("params", {})
        if method is None:
            await self.on_response(message)
        elif "id" in message:
            await self.on_server_request(message, method, params)
        elif params.get("threadId") == self.thread_id:
            await self.on_notification(method, params)

    async def on_response(self, message: JsonObject) -> None:
        """Reply to one of our own requests (turn/start or turn/interrupt)."""
        request_id: Any = message.get("id")  # an id we never issued simply matches nothing
        action = self.pending.pop(request_id, None)
        if "error" in message:
            if action == "start":
                self.active = False
            await self.send("error", message["error"].get("message", "Codex isteği tamamlanamadı."))
        elif action == "start":
            turn_id: str = message["result"]["turn"]["id"]
            self.turn_id = turn_id
            if self.cancel_requested:
                await self.interrupt(turn_id)

    async def on_server_request(self, message: JsonObject, method: str, params: JsonObject) -> None:
        """Codex asks us to do something; only this thread's health tool is allowed."""
        if method != "item/tool/call" or params.get("threadId") != self.thread_id:
            await self.rpc.reject(message)
            return
        await self.send("working", "Sağlık arşivi sorgulanıyor…")
        data, success = await run_health_tool(params)
        await self.rpc.send(
            {
                "id": message["id"],
                "result": {
                    "success": success,
                    "contentItems": [
                        {"type": "inputText", "text": json.dumps(data, ensure_ascii=False)}
                    ],
                },
            }
        )

    async def on_notification(self, method: str, params: JsonObject) -> None:
        if method == "item/agentMessage/delta":
            await self.websocket.send_json(
                {"type": "delta", "id": params["itemId"], "text": params["delta"]}
            )
        elif method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
            item = params["item"]
            await self.websocket.send_json(
                {"type": "message", "id": item["id"], "text": item["text"]}
            )
        elif method == "turn/completed":
            self.active = False
            self.cancel_requested = False
            self.turn_id = None
            turn = params["turn"]
            if turn["status"] == "failed":
                await self.send(
                    "error", (turn.get("error") or {}).get("message", "Codex yanıt üretemedi.")
                )
            await self.websocket.send_json({"type": "done", "status": turn["status"]})


def _read(websocket: WebSocket, rpc: CodexRPC, source: Source) -> asyncio.Task[Any]:
    return asyncio.create_task(websocket.receive_json() if source == "browser" else rpc.receive())


async def relay(websocket: WebSocket, rpc: CodexRPC, thread_id: str) -> None:
    """Pump both sockets until either side disconnects or the chat times out."""
    chat = ChatRelay(websocket, rpc, thread_id)
    readers: dict[asyncio.Task[Any], Source] = {
        _read(websocket, rpc, "browser"): "browser",
        _read(websocket, rpc, "codex"): "codex",
    }
    try:
        while True:
            done, _ = await asyncio.wait(
                readers, timeout=chat.timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                await chat.send(
                    "error",
                    "Codex yanıtı zaman aşımına uğradı. Sohbeti yeniden bağlayıp deneyebilirsin.",
                )
                return
            for task in done:
                source = readers.pop(task)
                message = task.result()
                readers[_read(websocket, rpc, source)] = source
                if source == "browser":
                    await chat.on_browser(message)
                else:
                    await chat.on_codex(message)
    finally:
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await chat.interrupt_on_close()


@router.websocket("/chat")
async def chat(websocket: WebSocket) -> None:
    if not same_origin(
        websocket.headers.get("origin"), websocket.headers.get("host")
    ) or not request_authenticated(websocket.app.state.settings.app_ui_token, websocket.headers):
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
            _log_bridge_available()
            await websocket.send_json({"type": "ready", "model": model, "effort": EFFORT})
            await relay(websocket, rpc, thread_id)
    except WebSocketDisconnect:
        pass
    except Exception:
        _log_bridge_unavailable()
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
