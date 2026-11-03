"""Archive lifetime races, using events rather than a model, database or timed sleeps."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect
from test_codex_chat import RPC, Browser, take
from websockets.exceptions import ConnectionClosed

from app.api import codex
from app.services import chat_tools


class HeldQuery:
    def __init__(self, *, late=False, error=False):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.late = late
        self.error = error
        self.task = None

    async def __call__(self, _arguments):
        self.task = asyncio.current_task()
        self.entered.set()
        try:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                if not self.late:
                    raise
                await self.release.wait()
            if self.error:
                raise RuntimeError("synthetic query failure")
            return {"streams": [{"data_type": "heart-rate"}]}
        finally:
            self.cleaned.set()


def tool_request(identifier="tool", turn="turn-1", thread="site-thread"):
    return {
        "id": identifier,
        "method": "item/tool/call",
        "params": {
            "threadId": thread,
            "turnId": turn,
            "tool": "health_archive",
            "arguments": {"action": "inventory"},
        },
    }


@pytest.fixture
async def running(monkeypatch, request):
    browser, rpc = Browser(), RPC()
    chat = codex.ChatRelay(browser, rpc, "site-thread")
    monkeypatch.setattr(codex, "ChatRelay", lambda *_args: chat)

    # Both synthetic transports can inject disconnection at receive, without sockets.
    async def receive():
        item = await rpc.incoming.get()
        if isinstance(item, Exception):
            raise item
        return item

    rpc.receive = receive
    query = HeldQuery()
    monkeypatch.setattr(codex, "read_health", query)
    task = asyncio.create_task(codex.relay(browser, rpc, chat.thread_id))
    try:
        await browser.incoming.put({"text": "Uykumu incele. Kısa anlat."})
        start = await take(rpc.outgoing)
        known_turn = getattr(request, "param", True)
        if known_turn:
            await rpc.incoming.put({"id": start["id"], "result": {"turn": {"id": "turn-1"}}})
        await rpc.incoming.put(tool_request(turn="turn-1" if known_turn else None))
        await asyncio.wait_for(query.entered.wait(), 2)
        await take(browser.outgoing)  # thinking
        await take(browser.outgoing)  # querying
        yield SimpleNamespace(
            chat=chat, browser=browser, rpc=rpc, query=query, task=task, start=start
        )
    finally:
        query.release.set()
        if not task.done():
            await browser.incoming.put(WebSocketDisconnect())
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("running", [True, False], indirect=True)
async def test_cancel_reaches_bridge_before_query_release(running):
    r = running
    await r.browser.incoming.put({"type": "cancel"})
    assert (await take(r.browser.outgoing))["text"] == "Yanıt durduruluyor…"
    if r.chat.turn_id is None:
        assert r.chat.cancel_requested and r.rpc.outgoing.empty()
        await r.rpc.incoming.put({"id": r.start["id"], "result": {"turn": {"id": "turn-1"}}})
    interrupt = await take(r.rpc.outgoing)
    assert interrupt["method"] == "turn/interrupt"
    assert interrupt["params"]["turnId"] == "turn-1"
    assert not r.query.release.is_set()
    await asyncio.wait_for(r.query.cleaned.wait(), 2)
    assert r.query.task.cancelled() and r.chat.transfer.calls == []
    assert r.chat.context.cancelled
    # No fresh health query may sneak in after interrupt acknowledgement.
    await r.rpc.incoming.put({"id": interrupt["id"], "result": {}})
    await r.rpc.incoming.put(tool_request("after-cancel"))
    assert (await take(r.rpc.outgoing))["error"] == "unsupported"


async def test_cancel_wins_when_socket_and_query_result_are_ready_together(running, monkeypatch):
    r = running
    original = asyncio.wait

    async def simultaneous(tasks, **_kwargs):
        monkeypatch.setattr(asyncio, "wait", original)
        await r.query.task
        browser_reader = next(t for t in tasks if t.get_coro().__name__ == "receive_json")
        await browser_reader
        return {t for t in tasks if t.done()}, {t for t in tasks if not t.done()}

    monkeypatch.setattr(asyncio, "wait", simultaneous)
    # Wake the existing wait; the next wait batches both ready outcomes deterministically.
    await r.rpc.incoming.put(tool_request("barrier", thread="foreign"))
    assert (await take(r.rpc.outgoing))["id"] == "barrier"
    r.query.release.set()
    await r.browser.incoming.put({"type": "cancel"})
    assert (await take(r.rpc.outgoing))["method"] == "turn/interrupt"
    assert r.chat.transfer.calls == [] and r.rpc.outgoing.empty()


@pytest.mark.parametrize("exit_kind", ["browser", "bridge", "timeout", "error", "cancelled"])
async def test_exit_cleans_pending_query_and_all_owned_tasks(running, monkeypatch, exit_kind):
    r = running
    if exit_kind == "timeout":
        # Expire deterministically on the next socket message; no wall-clock sleep.
        monkeypatch.setattr(codex, "ACTIVE_TURN_TIMEOUT_SECONDS", 0)
        await r.browser.incoming.put({"text": "busy"})
        await asyncio.wait_for(r.task, 2)
    else:
        if exit_kind == "cancelled":
            r.task.cancel()
            error = asyncio.CancelledError
        elif exit_kind == "browser":
            await r.browser.incoming.put(WebSocketDisconnect())
            error = WebSocketDisconnect
        else:
            error = ConnectionClosed if exit_kind == "bridge" else RuntimeError
            await r.rpc.incoming.put(
                ConnectionClosed(None, None) if exit_kind == "bridge" else error()
            )
        with pytest.raises(error):
            await asyncio.wait_for(r.task, 2)
    assert r.query.cleaned.is_set() and r.query.task.done()
    assert not r.chat.tools.tasks and not r.chat.tools.seen
    assert r.chat.transfer.calls == [] and r.chat.context.current is None
    assert r.chat.transfer.status == (
        "timed_out"
        if exit_kind == "timeout"
        else "failed"
        if exit_kind == "error"
        else "disconnected"
    )


@pytest.mark.parametrize("status", ["completed", "interrupted", "failed"])
async def test_turn_notification_cancels_query_without_waiting_for_result(running, status):
    r = running
    await r.rpc.incoming.put(
        {
            "method": "turn/completed",
            "params": {"threadId": r.chat.thread_id, "turn": {"id": "turn-1", "status": status}},
        }
    )
    if status == "failed":
        assert (await take(r.browser.outgoing))["type"] == "error"
    assert (await take(r.browser.outgoing)) == {"type": "done", "status": status}
    await asyncio.wait_for(r.query.cleaned.wait(), 2)
    assert r.query.task.cancelled() and not r.query.release.is_set()
    assert r.chat.transfer.calls == [] and r.chat.transfer.status == status


async def test_success_is_sent_once_to_its_rpc_and_duplicates_do_not_run(running, monkeypatch):
    r = running
    await r.rpc.incoming.put(tool_request())  # duplicate while pending

    async def immediate(_arguments):
        return {"streams": []}

    monkeypatch.setattr(codex, "read_health", immediate)
    await r.rpc.incoming.put(tool_request("second"))
    second = await take(r.rpc.outgoing)
    assert second["id"] == "second" and second["result"]["success"]
    r.query.release.set()
    reply = await take(r.rpc.outgoing)
    assert reply["id"] == "tool" and reply["result"]["success"]
    await r.rpc.incoming.put(tool_request("stale", turn="old-turn"))
    assert (await take(r.rpc.outgoing)) == {"id": "stale", "error": "unsupported"}
    await r.rpc.incoming.put(tool_request())  # duplicate after successful send
    await r.rpc.incoming.put(tool_request("barrier", thread="foreign"))
    assert (await take(r.rpc.outgoing)) == {"id": "barrier", "error": "unsupported"}
    assert r.rpc.outgoing.empty()
    assert len(r.chat.transfer.calls) == 2
    assert r.chat.transfer.snapshot()["counts"]["metadata_items"] == 1


async def test_task_limit_includes_cancelled_queries_until_cleanup_finishes(monkeypatch):
    browser, rpc = Browser(), RPC()
    chat = codex.ChatRelay(browser, rpc, "site-thread")
    await chat.on_browser({"text": "Arşivi incele"})
    held = [HeldQuery(late=True) for _ in range(chat_tools.MAX_TOOL_TASKS)]
    tasks = []
    try:
        for index, query in enumerate(held):
            monkeypatch.setattr(codex, "read_health", query)
            await chat.on_codex(tool_request(str(index)))
            await asyncio.wait_for(query.entered.wait(), 2)
        tasks = list(chat.tools.tasks)
        await chat.on_codex(tool_request("over-limit"))
        assert (await take(rpc.outgoing))["method"] == "turn/start"
        assert (await take(rpc.outgoing)) == {"id": "over-limit", "error": "unsupported"}
        await chat.cancel()
        await asyncio.gather(*(query.cancelled.wait() for query in held))
        await chat.on_notification("turn/completed", {"turn": {"status": "interrupted"}})
        await chat.on_browser({"text": "Yeni soru"})
        await chat.on_codex(tool_request("still-full"))
        assert len(chat.tools.tasks) == chat_tools.MAX_TOOL_TASKS
        assert (await take(rpc.outgoing))["method"] == "turn/start"
        assert (await take(rpc.outgoing)) == {"id": "still-full", "error": "unsupported"}
        for query in held:
            query.release.set()
        await asyncio.gather(*tasks)
        for task in tasks:
            await chat.on_tool_result(task)
        assert chat.transfer.calls == [] and not chat.tools.full
        await chat.on_codex(tool_request("new-query"))
        task = next(iter(chat.tools.tasks))
        await task
        await chat.on_tool_result(task)
        assert (await take(rpc.outgoing))["id"] == "new-query"
        assert len(chat.transfer.calls) == 1
    finally:
        for query in held:
            query.release.set()
        await chat.tools.close()


async def test_rpc_deduplication_storage_has_a_connection_limit(monkeypatch):
    monkeypatch.setattr(chat_tools, "MAX_TOOL_REQUESTS", 2)
    chat = codex.ChatRelay(Browser(), RPC(), "site-thread")
    for identifier in ("one", "two", "one"):
        await chat.on_codex(tool_request(identifier))  # inactive requests are rejected once
    with pytest.raises(RuntimeError, match="request limit"):
        await chat.on_codex(tool_request("three"))
    assert len(chat.tools.seen) == 2 and not chat.tools.tasks


@pytest.mark.parametrize("cleanup", ["reap", "close"])
async def test_invalidated_task_exception_is_consumed(monkeypatch, cleanup):
    chat = codex.ChatRelay(Browser(), RPC(), "site-thread")
    query = HeldQuery(late=True, error=True)

    async def failing_tool(_params):
        # Exercise a task failure outside run_health_tool's normal error conversion.
        return await query({}), True

    monkeypatch.setattr(codex, "run_health_tool", failing_tool)
    await chat.on_browser({"text": "Arşivi incele"})
    await chat.on_codex(tool_request())
    await asyncio.wait_for(query.entered.wait(), 2)
    task = next(iter(chat.tools.tasks))
    try:
        chat.tools.cancel()
        await asyncio.wait_for(query.cancelled.wait(), 2)
        query.release.set()
        if cleanup == "reap":
            await asyncio.wait_for(query.cleaned.wait(), 2)
            assert task.done()
            await chat.on_tool_result(task)
        else:
            await chat.tools.close()
        assert not chat.tools.tasks and chat.transfer.calls == []
    finally:
        query.release.set()
        await chat.tools.close()


async def test_close_does_not_wait_forever_for_a_query_that_ignores_cancellation(monkeypatch):
    monkeypatch.setattr(chat_tools, "CLOSE_GRACE_SECONDS", 0.01)
    chat = codex.ChatRelay(Browser(), RPC(), "site-thread")
    query = HeldQuery(late=True)
    monkeypatch.setattr(codex, "read_health", query)
    await chat.on_browser({"text": "Arşivi incele"})
    await chat.on_codex(tool_request())
    await asyncio.wait_for(query.entered.wait(), 2)
    try:
        await asyncio.wait_for(chat.tools.close(), 2)
        assert query.cancelled.is_set() and not query.task.done()
        assert not chat.tools.tasks and not chat.tools.seen
        assert chat.transfer.calls == []
    finally:
        query.release.set()
        await asyncio.wait_for(query.cleaned.wait(), 2)
    await asyncio.sleep(0)
    assert query.task.done() and query.task not in chat_tools._ABANDONED


async def test_delivered_tool_result_renews_the_turn_deadline(running, monkeypatch):
    r = running
    # The deadline was set by the last socket message with the default (long) timeout.
    # Only a renewal at delivery can pick up this zero timeout and expire the turn.
    monkeypatch.setattr(codex, "ACTIVE_TURN_TIMEOUT_SECONDS", 0)
    r.query.release.set()
    assert (await take(r.rpc.outgoing))["id"] == "tool"
    await asyncio.wait_for(r.task, 2)
    assert r.chat.transfer.status == "timed_out" and len(r.chat.transfer.calls) == 1
