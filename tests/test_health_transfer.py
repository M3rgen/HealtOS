"""Synthetic SQLite reads through the relay's actual JSON-RPC send boundary."""

import asyncio
import json
import logging
from copy import deepcopy
from datetime import timedelta

import pytest
from derived_records import DAY, T0, manual, point, sleep, snapshot, stage, steps, store, summary
from fastapi import WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from app.api import codex
from app.services import codex_health, coverage
from app.services.health_transfer import COUNT_KEYS, describe_result, field_paths
from app.services.manual_flags import record_flag

QUESTION = "synthetic-private-question"
SECRET = "synthetic-private-value-and-token"
QUERY = {"start_date": str(DAY), "end_date": str(DAY), "timezone": "UTC"}


@pytest.fixture(autouse=True)
def capture_logs(caplog, monkeypatch):
    # Alembic disables existing loggers and create_app replaces root handlers in other
    # tests. Capture these producers explicitly; privacy assertions must see real logs.
    for name in ("app.services.health_transfer", "app.api.codex"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "disabled", False)
        monkeypatch.setattr(logger, "handlers", [caplog.handler])
        monkeypatch.setattr(logger, "propagate", False)


class Socket:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.closed = False

    async def send_json(self, message):
        if self.closed:
            raise WebSocketDisconnect()
        self.sent.append(deepcopy(message))
        await self.outgoing.put(message)

    async def receive_json(self):
        message = await self.incoming.get()
        if isinstance(message, Exception):
            raise message
        return message


class Bridge:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.fail = False
        self.sequence = 0

    async def send(self, message):
        if self.fail:
            raise self.fail if isinstance(self.fail, Exception) else RuntimeError(SECRET)
        self.sent.append(deepcopy(message))
        await self.outgoing.put(message)

    async def request(self, method, params):
        self.sequence += 1
        await self.send({"id": self.sequence, "method": method, "params": params})
        return self.sequence

    async def reject(self, message):
        await self.send({"id": message["id"], "error": "unsupported"})

    async def receive(self):
        message = await self.incoming.get()
        if isinstance(message, Exception):
            raise message
        return message


async def started():
    socket, bridge = Socket(), Bridge()
    chat = codex.ChatRelay(socket, bridge, SECRET)
    await chat.on_browser({"text": QUESTION})
    return chat, socket, bridge


def receipts(socket):
    return [item for item in socket.sent if item["type"] == "health_transfer"]


async def tool(chat, bridge, arguments):
    await chat.on_server_request(
        {"id": f"private-rpc-id-{len(chat.tools.seen)}"},
        "item/tool/call",
        {"threadId": chat.thread_id, "tool": "health_archive", "arguments": arguments},
    )
    tasks = tuple(chat.tools.tasks)
    await asyncio.gather(*tasks, return_exceptions=True)
    for task in tasks:
        await chat.on_tool_result(task)
    reply = bridge.sent[-1]["result"]
    return json.loads(reply["contentItems"][0]["text"]), reply["success"]


async def complete(chat, status="completed"):
    await chat.on_notification("turn/completed", {"turn": {"status": status}})


@pytest.fixture
def actual_reads(sessions, monkeypatch):
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    reads = []

    async def read(arguments):
        result = await codex_health.read_health(arguments)
        reads.append(deepcopy(result))
        return result

    monkeypatch.setattr(codex, "read_health", read)
    return reads


@pytest.mark.parametrize("action", ["inventory", "daily", "records", "coverage"])
async def test_empty_actions_match_exact_wire_result_and_do_not_count_placeholders(
    action, actual_reads
):
    chat, socket, bridge = await started()
    args = {"action": action, **QUERY}
    if action == "records":
        args["data_type"] = "heart-rate"
    result, success = await tool(chat, bridge, args)
    assert success and result == actual_reads[-1]
    receipt = receipts(socket)[-1]
    call = receipt["calls"][0]
    assert call == describe_result(args, result, True)
    assert call["unique_source_records"] is None
    assert call["data_dates"] is call["profile_dates"] is call["metadata_dates"] is None
    assert call["counts"] == {
        key: 9 if action == "coverage" and key == "metadata_items" else 0 for key in COUNT_KEYS
    }
    assert call["requested"] == (
        None if action == "inventory" else {"first": str(DAY), "last": str(DAY)}
    )
    assert call["timezone"] == "UTC"
    await complete(chat)
    assert receipts(socket)[-1]["status"] == "completed"


async def test_inventory_and_coverage_counts_describe_metadata_not_the_archive(
    sessions, actual_reads, caplog
):
    await store(sessions, "steps", steps("one"), steps("two"), steps("three"))
    chat, socket, bridge = await started()
    caplog.set_level(logging.INFO)
    inventory, _ = await tool(chat, bridge, {"action": "inventory"})
    assert inventory["streams"][0]["count"] == 3
    call = receipts(socket)[-1]["calls"][-1]
    assert call["counts"]["raw_records"] == 0
    assert call["counts"]["metadata_items"] == 1
    assert call["data_dates"] is None
    assert call["metadata_dates"] == {"first": str(DAY), "last": str(DAY)}
    card, _ = await tool(chat, bridge, {"action": "coverage", **QUERY, "data_type": "steps"})
    assert card == actual_reads[-1]
    assert card["schema_version"] == "2" and card["metrics"][0]["sampling"]["sample_count"] == 3
    call = receipts(socket)[-1]["calls"][-1]
    assert call["counts"]["metadata_items"] == 1 and call["counts"]["raw_records"] == 0
    assert call["data_dates"] is None
    assert call["metadata_dates"] == {"first": str(DAY), "last": str(DAY)}
    assert SECRET not in caplog.text and QUESTION not in caplog.text
    assert "Synthetic band" not in caplog.text and "kaynak-" not in caplog.text
    assert "Umay health transfer" in caplog.text


async def test_only_transmitted_pages_count_and_repeats_are_not_unique_records(
    sessions, actual_reads
):
    await store(
        sessions,
        "steps",
        steps("a", start=T0 - timedelta(hours=1), end=T0 + timedelta(hours=1)),
        steps("b"),
    )
    chat, socket, bridge = await started()
    args = {"action": "records", **QUERY, "data_type": "steps", "limit": 1}
    first, _ = await tool(chat, bridge, json.dumps(args))
    assert first == actual_reads[-1] and first["next_offset"] == 1
    call = receipts(socket)[-1]["calls"][0]
    assert call["counts"]["raw_records"] == 1
    assert call["data_dates"] == {"first": str(DAY - timedelta(days=1)), "last": str(DAY)}
    assert "records[].payload.steps.count" in call["fields"]
    second, _ = await tool(chat, bridge, dict(args, offset=1))
    assert second["next_offset"] is None
    await tool(chat, bridge, args)  # same page again: representations, not unique sources
    await tool(chat, bridge, dict(args, offset=2))  # empty page
    receipt = receipts(socket)[-1]
    assert receipt["counts"]["raw_records"] == 3
    assert [c["counts"]["raw_records"] for c in receipt["calls"]] == [1, 1, 1, 0]
    assert all(c["unique_source_records"] is None for c in receipt["calls"])


@pytest.mark.parametrize(
    "timezone, first, count",
    [
        ("UTC", str(DAY - timedelta(days=1)), 1),
        ("Europe/Paris", str(DAY), 1),
        ("Pacific/Honolulu", None, 0),
    ],
)
async def test_actual_record_days_use_query_timezone_not_requested_bounds(
    timezone, first, count, sessions, actual_reads
):
    await store(
        sessions, "steps", steps(start=T0 - timedelta(hours=2), end=T0 + timedelta(hours=1))
    )
    chat, socket, bridge = await started()
    result, _ = await tool(
        chat,
        bridge,
        {
            "action": "records",
            **QUERY,
            "data_type": "steps",
            "timezone": timezone,
        },
    )
    assert result == actual_reads[-1]
    call = receipts(socket)[-1]["calls"][0]
    assert call["counts"]["raw_records"] == count
    assert call["timezone"] == timezone
    assert call["requested"] == {"first": str(DAY), "last": str(DAY)}
    assert call["data_dates"] == ({"first": first, "last": str(DAY)} if count else None)


async def test_daily_includes_legacy_charts_definitions_and_profile_before_window(
    sessions, actual_reads, caplog
):
    await store(sessions, "steps", steps(count=0))
    await store(
        sessions,
        "daily-heart-rate-variability",
        summary(
            "daily-heart-rate-variability",
            averageHeartRateVariabilityMilliseconds=11,
            rootMeanSquareOfSuccessiveDifferencesMilliseconds=22,
            deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds=33,
        ),
    )
    await store(
        sessions,
        "heart-rate",
        point(
            "heart-rate",
            "sample",
            {
                "sampleTime": {"physicalTime": T0.isoformat()},
                "beatsPerMinute": 77,
            },
        ),
    )
    await manual(sessions, 998877.25, T0 - timedelta(days=20))
    async with sessions() as session:
        before = await snapshot(session)
    chat, socket, bridge = await started()
    caplog.set_level(logging.INFO)
    result, success = await tool(chat, bridge, {"action": "daily", **QUERY})
    assert success and result == actual_reads[-1]
    call = receipts(socket)[-1]["calls"][0]
    assert call["counts"] == dict(zip(COUNT_KEYS, (0, 1, 1, 2, 5, 5, 0), strict=True))
    assert call["requested"] == {"first": str(DAY), "last": str(DAY)}
    assert call["data_dates"] == {"first": str(DAY - timedelta(days=20)), "last": str(DAY)}
    assert call["profile_dates"] == {
        "first": str(DAY - timedelta(days=20)),
        "last": str(DAY - timedelta(days=20)),
    }
    for path in (
        "days[].hrv_ms",
        "profile.weight.value",
        "charts.heart_rate[]",
        "derived.days[].result.streams[].observations[].source_value",
        "derived.days[].result.manual_profile[].observations[].measured_at",
    ):
        assert path in call["fields"]
    assert "998877.25" not in caplog.text
    assert "Umay health transfer" in caplog.text
    assert result["days"][0]["steps"] == 0
    assert len(result["derived"]["days"][0]["result"]["streams"]) == 4
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    "args, code",
    [
        ({"action": "coverage", **QUERY, "data_type": SECRET}, "coverage_unknown_data_type"),
        ({"action": "coverage", **QUERY}, "coverage_budget_exceeded"),
        ({"action": SECRET}, None),
    ],
)
async def test_refusals_and_invalid_queries_are_counted_without_copying_values(
    args, code, actual_reads, monkeypatch, caplog
):
    monkeypatch.setattr(coverage, "COVERAGE_BUDGET_BYTES", 1)
    caplog.set_level(logging.INFO)
    chat, socket, bridge = await started()
    result, success = await tool(chat, bridge, args)
    assert success is False and "error" in result
    if code:
        assert result == actual_reads[-1] and result["code"] == code
    call = receipts(socket)[-1]["calls"][0]
    assert call["outcome"] == "refused" and not any(call["counts"].values())
    assert call["data_dates"] is None
    assert SECRET not in caplog.text and QUESTION not in caplog.text
    assert "Umay health transfer" in caplog.text


async def test_exception_messages_payload_values_and_dynamic_field_keys_never_log(
    monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    chat, socket, bridge = await started()

    async def broken(_arguments):
        raise RuntimeError(SECRET + " 998877.25 Bearer synthetic-credential")

    monkeypatch.setattr(codex, "read_health", broken)
    result, success = await tool(chat, bridge, {"action": "inventory"})
    assert not success and result["error"].startswith("Sağlık arşivi okunamadı")
    assert not any(record.exc_info for record in caplog.records)
    assert "Umay health archive query failed" in caplog.text

    async def sensitive(_arguments):
        return {
            "records": [
                {
                    "start": T0.isoformat(),
                    "source_timezone": "Europe/Paris",
                    "payload": {
                        SECRET: {"998877.25": {"value": SECRET}},
                        "name": SECRET,
                        "dataSource": {"device": {SECRET: SECRET}},
                        "heartRate": {"bpm": 998877.25},
                    },
                }
            ]
        }

    monkeypatch.setattr(codex, "read_health", sensitive)
    result, success = await tool(
        chat, bridge, {"action": "records", **QUERY, "data_type": "heart-rate"}
    )
    assert success and result["records"][0]["payload"]["name"] == SECRET
    call = receipts(socket)[-1]["calls"][-1]
    assert call["source_timezones"] == ["Europe/Paris"]
    assert "records[].payload.*.*.value" in call["fields"]
    assert "records[].payload.dataSource.device.*" in call["fields"]
    for value in (SECRET, QUESTION, "998877.25", "synthetic-credential", "private-rpc-id"):
        assert value not in caplog.text
    assert "Umay health transfer" in caplog.text


async def test_sleep_evidence_and_repeated_old_manual_profile_are_representations(
    sessions, actual_reads
):
    await store(sessions, "sleep", sleep(stages=[stage("LIGHT", 1, 3)]))
    await manual(sessions, 70, T0 - timedelta(days=20))
    flagged = await manual(sessions, 90)
    async with sessions() as session, session.begin():
        await record_flag(session, flagged, "test", True)
    async with sessions() as session:
        before = await snapshot(session)
    chat, socket, bridge = await started()
    result, _ = await tool(
        chat,
        bridge,
        {
            "action": "daily",
            **QUERY,
            "end_date": str(DAY + timedelta(days=1)),
        },
    )
    assert result == actual_reads[-1]
    call = receipts(socket)[-1]["calls"][0]
    assert call["counts"] == dict(zip(COUNT_KEYS, (0, 1, 1, 0, 3, 3, 0), strict=True))
    assert call["unique_source_records"] is None
    assert call["profile_dates"] == {
        "first": str(DAY - timedelta(days=20)),
        "last": str(DAY - timedelta(days=20)),
    }
    assert result["profile"]["weight"]["value"] == 70
    assert all(
        day["result"]["manual_profile"][0]["value"] == 70 for day in result["derived"]["days"]
    )
    assert "derived.days[].result.streams[].sleep.sessions[].timeline[].start" in call["fields"]
    assert (
        "derived.days[].result.streams[].sleep.stage_total_differences_seconds.*" in call["fields"]
    )
    async with sessions() as session:
        assert await snapshot(session) == before


def test_deep_and_dynamic_json_field_paths_are_bounded_without_mutation():
    payload = {SECRET: {"value": 998877.25}}
    for _ in range(100):
        payload = {SECRET: payload}
    before = deepcopy(payload)
    fields = field_paths(payload)
    assert len(fields) == 1 and fields[0].endswith(".**")
    assert SECRET not in str(fields) and payload == before


async def test_receipt_failure_still_answers_the_model_with_a_neutral_receipt(
    sessions, actual_reads, monkeypatch, caplog
):
    await store(sessions, "steps", steps())
    chat, socket, bridge = await started()

    def broken(*args):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(codex, "describe_result", broken)
    result, success = await tool(chat, bridge, {"action": "daily", **QUERY})
    assert success and result == actual_reads[-1] and result["days"][0]["steps"] == 120
    call = receipts(socket)[-1]["calls"][0]
    assert call["action"] == "invalid" and call["outcome"] == "sent"
    assert call["counts"] == dict.fromkeys(COUNT_KEYS, 0) and call["fields"] == []
    assert call["data_dates"] is None and call["requested"] is None
    assert "RuntimeError" in caplog.text and SECRET not in caplog.text


@pytest.mark.parametrize("status", ["completed", "interrupted", "failed"])
async def test_turn_lifecycle_followup_and_separate_chats_reset_receipts(status, actual_reads):
    chat, socket, bridge = await started()
    first = receipts(socket)[-1]
    assert first["status"] == "active" and first["calls"] == []
    await tool(chat, bridge, {"action": "inventory"})
    await chat.on_browser({"text": "busy-question"})
    assert receipts(socket)[-1]["turn"] == first["turn"]
    await chat.cancel()  # cancel before start acknowledgement retains turn and counts
    await chat.on_response({"id": 1, "result": {"turn": {"id": "private-model-turn"}}})
    assert bridge.sent[-1]["method"] == "turn/interrupt"
    await complete(chat, status)
    assert receipts(socket)[-1]["status"] == status
    assert receipts(socket)[-1]["turn"] == first["turn"]
    assert len(receipts(socket)[-1]["calls"]) == 1
    await chat.on_browser({"text": "followup"})
    followup = receipts(socket)[-1]
    assert followup["turn"] != first["turn"] and followup["calls"] == []
    assert not any(followup["counts"].values())
    await complete(chat)  # no health tool used
    assert receipts(socket)[-1]["calls"] == []
    other, other_socket, _ = await started()
    assert receipts(other_socket)[-1]["turn"] not in {first["turn"], followup["turn"]}
    assert other.transfer.calls == []


async def test_start_rejection_and_inactive_tool_calls_do_not_reuse_previous_receipt(actual_reads):
    chat, socket, bridge = await started()
    await chat.on_response({"id": 1, "error": {"message": SECRET}})
    assert receipts(socket)[-1]["status"] == "failed"
    await chat.on_server_request({"id": 3}, "item/tool/call", {"threadId": SECRET})
    assert "error" in bridge.sent[-1] and receipts(socket)[-1]["calls"] == []
    await chat.on_browser({"text": " "})
    assert len(receipts(socket)) == 2


@pytest.mark.parametrize("with_prior", [False, True])
async def test_failed_bridge_send_does_not_count_result_or_discard_earlier_sends(
    with_prior, sessions, actual_reads, caplog
):
    caplog.set_level(logging.INFO)
    await store(sessions, "steps", steps())
    chat, socket, bridge = await started()
    if with_prior:
        await tool(chat, bridge, {"action": "coverage", **QUERY})
    before = deepcopy(receipts(socket)[-1])
    bridge.fail = True
    with pytest.raises(RuntimeError, match=SECRET):
        await tool(chat, bridge, {"action": "daily", **QUERY})
    receipt = receipts(socket)[-1]
    assert receipt["calls"] == before["calls"] and receipt["counts"] == before["counts"]
    assert receipt["failed_sends"] == 1 and receipt["status"] == "failed"
    assert actual_reads[-1]["days"][0]["steps"] == 120
    assert SECRET not in caplog.text


@pytest.mark.parametrize("side", ["browser", "bridge", "error", "timeout", "start_send"])
async def test_relay_finalizes_on_socket_loss_timeout_and_unexpected_errors(
    side, monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    socket, bridge = Socket(), Bridge()
    if side == "timeout":
        monkeypatch.setattr(codex, "ACTIVE_TURN_TIMEOUT_SECONDS", 0.02)
    bridge.fail = side == "start_send"
    task = asyncio.create_task(codex.relay(socket, bridge, SECRET))
    await socket.incoming.put({"text": QUESTION})
    if side != "start_send":
        await asyncio.wait_for(bridge.outgoing.get(), 2)
    if side == "browser":
        socket.closed = True
        await socket.incoming.put(WebSocketDisconnect())
    elif side in {"bridge", "error"}:
        await bridge.incoming.put(
            ConnectionClosed(None, None) if side == "bridge" else RuntimeError(SECRET)
        )
    if side == "timeout":
        await asyncio.wait_for(task, 2)
    else:
        with pytest.raises((RuntimeError, WebSocketDisconnect, ConnectionClosed)):
            await asyncio.wait_for(task, 2)
    logs = [
        json.loads(record.message.split("Umay health transfer ")[1])
        for record in caplog.records
        if record.message.startswith("Umay health transfer ")
    ]
    expected = (
        "timed_out"
        if side == "timeout"
        else "disconnected"
        if side in {"browser", "bridge"}
        else "failed"
    )
    assert logs[-1]["status"] == expected
    assert logs[-1]["calls"] == [] and not any(logs[-1]["counts"].values())
    assert SECRET not in caplog.text and QUESTION not in caplog.text


async def test_bridge_disconnect_during_send_is_finalized_without_counting_result(actual_reads):
    chat, socket, bridge = await started()
    bridge.fail = ConnectionClosed(None, None)
    with pytest.raises(ConnectionClosed):
        await tool(chat, bridge, {"action": "inventory"})
    receipt = receipts(socket)[-1]
    assert receipt["status"] == "disconnected" and receipt["failed_sends"] == 1
    assert receipt["calls"] == []


async def test_browser_delivery_failure_still_logs_successful_bridge_send(actual_reads, caplog):
    caplog.set_level(logging.INFO)
    chat, socket, bridge = await started()
    send_json = socket.send_json

    async def disconnect_after_read(message):
        if message["type"] == "health_transfer":
            socket.closed = True
        await send_json(message)

    socket.send_json = disconnect_after_read
    with pytest.raises(WebSocketDisconnect):
        await tool(chat, bridge, {"action": "inventory"})
    await chat.finish_transfer("disconnected")
    assert bridge.sent[-1]["result"]["success"]
    assert chat.transfer.snapshot()["calls"][0]["action"] == "inventory"
    assert '"status": "disconnected"' in caplog.text
    assert SECRET not in caplog.text
