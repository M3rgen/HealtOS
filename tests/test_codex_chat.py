import asyncio
import json
import logging
from datetime import UTC, datetime

import pytest
from fastapi import WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy import func, select

from app.api import codex
from app.models.state import RawHealthData
from app.services import codex_chat, codex_health
from app.services.codex_chat import CodexRPC
from app.services.codex_health import ArchiveQuery, read_health


def raw(identifier, timestamp, **overrides):
    values = {
        "data_type": "heart-rate",
        "google_data_point_id": identifier,
        "source_start_time": datetime.fromisoformat(timestamp).replace(tzinfo=UTC),
        "raw_json": {"heartRate": {"bpm": 70}},
        "payload_hash": "test",
        "first_seen_at": datetime.now(UTC),
        "last_seen_at": datetime.now(UTC),
    }
    values.update(overrides)
    return RawHealthData(**values)


async def test_archive_inventory_pagination_and_read_only(sessions, monkeypatch):
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    async with sessions() as session:
        session.add_all(
            [
                raw("before", "2026-09-28T21:59:00"),
                raw("first", "2026-09-28T22:00:00"),
                raw("second", "2026-09-29T21:59:00"),
                raw("after", "2026-09-29T22:00:00"),
                raw("quarantine", "2026-09-29T12:00:00", is_quarantined=True),
                raw("deleted", "2026-09-29T12:00:00", source_deleted_at=datetime.now(UTC)),
            ]
        )
        await session.commit()
    inventory = await read_health({"action": "inventory"})
    assert inventory["streams"][0]["count"] == 4
    query = {
        "action": "records",
        "data_type": "heart-rate",
        "start_date": "2026-09-29",
        "end_date": "2026-09-29",
        "timezone": "Europe/Paris",
        "limit": 1,
    }
    first = await read_health(query)
    assert first["records"][0]["start"].startswith("2026-09-28T22:00")
    assert first["next_offset"] == 1
    second = await read_health(dict(query, offset=first["next_offset"]))
    assert second["records"][0]["start"].startswith("2026-09-29T21:59")
    assert second["next_offset"] is None
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 6


async def test_daily_tool_reuses_real_read_model(sessions, monkeypatch):
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    result = await read_health(
        {
            "action": "daily",
            "start_date": "2026-09-28",
            "end_date": "2026-09-29",
            "timezone": "UTC",
        }
    )
    assert len(result["days"]) == 2
    assert result["days"][0]["steps"] is None
    assert "sleep_method" in result


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "delete"},
        {"action": "inventory", "sql": "DROP TABLE raw_health_data"},
        {"action": "daily", "start_date": "2020-01-01", "end_date": "2026-09-29"},
        {"action": "daily", "start_date": "2099-01-01", "end_date": "2099-01-01"},
        {"action": "records", "start_date": "2026-09-01", "end_date": "2026-09-29"},
        {"action": "inventory", "limit": 201},
        {
            "action": "records",
            "data_type": "heart-rate",
            "start_date": "2026-09-29",
            "end_date": "2026-09-28",
        },
    ],
)
def test_archive_query_rejects_unbounded_or_write_requests(arguments):
    with pytest.raises(ValidationError):
        ArchiveQuery.model_validate(arguments)


def test_same_origin_protects_local_subscription():
    assert codex.same_origin("http://localhost:8000", "localhost:8000")
    assert not codex.same_origin("https://attacker.example", "localhost:8000")
    assert not codex.same_origin("null", "localhost:8000")
    assert not codex.same_origin(None, "localhost:8000")
    assert not codex.same_origin("http://[invalid", "localhost:8000")


class Browser:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def receive_json(self):
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def send_json(self, item):
        await self.outgoing.put(item)


class RPC:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.sequence = 0

    async def receive(self):
        return await self.incoming.get()

    async def request(self, method, params):
        self.sequence += 1
        await self.outgoing.put({"id": self.sequence, "method": method, "params": params})
        return self.sequence

    async def send(self, item):
        await self.outgoing.put(item)

    async def reject(self, item):
        await self.outgoing.put({"id": item["id"], "error": "unsupported"})


async def take(queue):
    return await asyncio.wait_for(queue.get(), 2)


async def test_new_thread_pins_requested_model_and_is_ephemeral(tmp_path, monkeypatch):
    class Connection:
        def __init__(self):
            self.sent = []
            self.responses = asyncio.Queue()

        async def send(self, text):
            request = json.loads(text)
            self.sent.append(request)
            if "id" not in request:
                return
            result = {
                "initialize": {},
                "account/read": {"account": {"type": "chatgpt"}},
                "thread/start": {"thread": {"id": "new-site-thread"}, "model": "gpt-6.1-sol"},
            }[request["method"]]
            await self.responses.put(json.dumps({"id": request["id"], "result": result}))

        async def recv(self):
            return await self.responses.get()

    connection = Connection()
    thread, model = await CodexRPC(connection).start()
    assert (thread, model) == ("new-site-thread", "gpt-6.1-sol")
    params = connection.sent[-1]["params"]
    assert params["ephemeral"] is True
    assert params["model"] == "gpt-6.1-sol"
    assert params["allowProviderModelFallback"] is False
    assert params["config"]["model_reasoning_effort"] == "medium"
    assert params["sandbox"] == "read-only"
    assert params["dynamicTools"][0]["name"] == "health_archive"
    assert "# Umay" in params["developerInstructions"]
    assert "Örnek diyaloglar üslup örneğidir" in params["developerInstructions"]
    assert params["baseInstructions"] == codex_chat.INSTRUCTIONS
    assert "health_archive" in params["baseInstructions"]

    # Edits apply to the next site thread; the running thread's snapshot stays fixed.
    soul = tmp_path / "soul.md"
    soul.write_text("# Umay\nYeni kişilik tercihi: daha şiirsel.", encoding="utf-8")
    monkeypatch.setattr(codex_chat, "SOUL_PATH", soul)
    await CodexRPC(connection).start()
    updated = connection.sent[-1]["params"]
    assert "Yeni kişilik tercihi" in updated["developerInstructions"]
    assert "Yeni kişilik tercihi" not in params["developerInstructions"]
    assert updated["baseInstructions"] == params["baseInstructions"]
    assert updated["dynamicTools"] == params["dynamicTools"]


def test_empty_umay_persona_is_rejected(tmp_path, monkeypatch):
    soul = tmp_path / "soul.md"
    soul.write_text(" \n", encoding="utf-8")
    monkeypatch.setattr(codex_chat, "SOUL_PATH", soul)
    with pytest.raises(ValueError, match="kişilik dosyası boş"):
        codex_chat.load_umay_persona()


async def test_relay_tools_streaming_followup_and_cancel(monkeypatch):
    browser, rpc = Browser(), RPC()

    async def health(arguments):
        assert arguments == {"action": "inventory"}
        return {"streams": []}

    monkeypatch.setattr(codex, "read_health", health)
    task = asyncio.create_task(codex.relay(browser, rpc, "site-thread"))
    try:
        await browser.incoming.put({"text": "Uykumu incele"})
        start = await take(rpc.outgoing)
        assert start["params"]["threadId"] == "site-thread"
        assert (await take(browser.outgoing))["type"] == "working"
        await rpc.incoming.put({"id": start["id"], "result": {"turn": {"id": "turn-1"}}})
        await rpc.incoming.put(
            {
                "id": "tool-1",
                "method": "item/tool/call",
                "params": {
                    "threadId": "site-thread",
                    "tool": "health_archive",
                    "arguments": {"action": "inventory"},
                },
            }
        )
        assert (await take(browser.outgoing))["type"] == "working"
        reply = await take(rpc.outgoing)
        assert reply["result"]["success"] is True
        assert json.loads(reply["result"]["contentItems"][0]["text"]) == {"streams": []}
        await rpc.incoming.put(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "other-thread",
                    "itemId": "other",
                    "delta": "PRIVATE",
                },
            }
        )
        await rpc.incoming.put(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "site-thread",
                    "itemId": "answer",
                    "delta": "Veri yok.",
                },
            }
        )
        assert (await take(browser.outgoing))["text"] == "Veri yok."
        await rpc.incoming.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "site-thread",
                    "turn": {"status": "completed"},
                },
            }
        )
        assert (await take(browser.outgoing))["status"] == "completed"
        await browser.incoming.put({"text": "Peki geçen ay?"})
        followup = await take(rpc.outgoing)
        assert followup["params"]["threadId"] == "site-thread"
        await take(browser.outgoing)
        await browser.incoming.put({"type": "cancel"})
        assert (await take(browser.outgoing))["text"] == "Yanıt durduruluyor…"
        # Cancellation arriving before turn/start's reply must be retained.
        await rpc.incoming.put({"id": followup["id"], "result": {"turn": {"id": "turn-2"}}})
        cancel = await take(rpc.outgoing)
        assert cancel["method"] == "turn/interrupt"
        assert cancel["params"]["turnId"] == "turn-2"
    finally:
        await browser.incoming.put(WebSocketDisconnect())
        with pytest.raises(WebSocketDisconnect):
            await asyncio.wait_for(task, 2)


async def test_relay_guards_input_tools_errors_and_closing_interrupt(monkeypatch):
    browser, rpc = Browser(), RPC()

    async def health(arguments):
        raise RuntimeError("database down")

    monkeypatch.setattr(codex, "read_health", health)
    task = asyncio.create_task(codex.relay(browser, rpc, "site-thread"))
    try:
        # Idle cancel is a no-op; the next reply belongs to the non-object message.
        await browser.incoming.put({"type": "cancel"})
        await browser.incoming.put(["not", "an", "object"])
        assert await take(browser.outgoing) == {"type": "error", "text": "Bir soru yaz."}
        for invalid in ({"text": "   "}, {"text": "Uyku", "timezone": "Mars/Olympus"}):
            await browser.incoming.put(invalid)
            assert (await take(browser.outgoing))["text"].startswith("Geçerli bir soru yaz")
        assert rpc.outgoing.empty()

        # A rejected turn/start ends the turn so the next question is accepted.
        await browser.incoming.put({"text": "Uykumu incele"})
        first = await take(rpc.outgoing)
        await take(browser.outgoing)
        await browser.incoming.put({"text": "İkinci soru"})
        assert (await take(browser.outgoing))["type"] == "notice"
        await rpc.incoming.put({"id": first["id"], "error": {"message": "model busy"}})
        assert await take(browser.outgoing) == {"type": "error", "text": "model busy"}

        await browser.incoming.put({"text": "Tekrar dene"})
        second = await take(rpc.outgoing)
        await take(browser.outgoing)
        await rpc.incoming.put({"id": second["id"], "result": {"turn": {"id": "turn-2"}}})
        # Only this thread's tool calls are served; everything else is rejected.
        for foreign in (
            {"id": "x1", "method": "item/commandExecution/requestApproval", "params": {}},
            {
                "id": "x2",
                "method": "item/tool/call",
                "params": {"threadId": "other-thread", "tool": "health_archive"},
            },
        ):
            await rpc.incoming.put(foreign)
            assert await take(rpc.outgoing) == {"id": foreign["id"], "error": "unsupported"}
        for tool_id, params, error in (
            ("t1", {"tool": "shell", "arguments": {}}, "Sorgu geçersiz"),
            ("t2", {"tool": "health_archive", "arguments": "{not json"}, "Sorgu geçersiz"),
            ("t3", {"tool": "health_archive", "arguments": {}}, "Sağlık arşivi okunamadı"),
        ):
            await rpc.incoming.put(
                {
                    "id": tool_id,
                    "method": "item/tool/call",
                    "params": {"threadId": "site-thread", **params},
                }
            )
            assert (await take(browser.outgoing))["type"] == "working"
            reply = await take(rpc.outgoing)
            assert reply["id"] == tool_id
            assert reply["result"]["success"] is False
            assert json.loads(reply["result"]["contentItems"][0]["text"])["error"].startswith(error)
        await rpc.incoming.put(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "site-thread",
                    "item": {"type": "agentMessage", "id": "answer", "text": "Tamam."},
                },
            }
        )
        assert await take(browser.outgoing) == {"type": "message", "id": "answer", "text": "Tamam."}

        # With the turn id known, cancel interrupts at once.
        await browser.incoming.put({"type": "cancel"})
        interrupt = await take(rpc.outgoing)
        assert interrupt["method"] == "turn/interrupt"
        assert interrupt["params"] == {"threadId": "site-thread", "turnId": "turn-2"}
        assert (await take(browser.outgoing))["text"] == "Yanıt durduruluyor…"
        await rpc.incoming.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "site-thread",
                    "turn": {"status": "failed", "error": {"message": "quota"}},
                },
            }
        )
        assert await take(browser.outgoing) == {"type": "error", "text": "quota"}
        assert await take(browser.outgoing) == {"type": "done", "status": "failed"}

        await browser.incoming.put({"text": "Son soru"})
        third = await take(rpc.outgoing)
        await take(browser.outgoing)
        await rpc.incoming.put({"id": third["id"], "result": {"turn": {"id": "turn-3"}}})
        await asyncio.sleep(0.05)
    finally:
        await browser.incoming.put(WebSocketDisconnect())
        with pytest.raises(WebSocketDisconnect):
            await asyncio.wait_for(task, 2)
    # Leaving mid-turn interrupts the running turn.
    closing = await take(rpc.outgoing)
    assert closing["method"] == "turn/interrupt"
    assert closing["params"]["turnId"] == "turn-3"


async def test_relay_times_out_an_idle_chat(monkeypatch):
    browser, rpc = Browser(), RPC()
    monkeypatch.setattr(codex, "IDLE_TIMEOUT_SECONDS", 0.05)
    await asyncio.wait_for(codex.relay(browser, rpc, "site-thread"), 2)
    message = await take(browser.outgoing)
    assert message["type"] == "error"
    assert "zaman aşımına" in message["text"]
    assert rpc.outgoing.empty()


def test_unavailable_bridge_warns_once_per_outage(monkeypatch, caplog):
    monkeypatch.setattr(codex, "_bridge_unavailable_logged", False)
    caplog.set_level(logging.DEBUG, logger=codex.logger.name)

    for _ in range(5):
        codex._log_bridge_unavailable()
    codex._log_bridge_available()
    codex._log_bridge_unavailable()

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    debug = [record for record in caplog.records if record.levelno == logging.DEBUG]
    assert len(warnings) == 2
    assert len(debug) == 4
