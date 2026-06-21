import asyncio
import json

from scripts import simulate_onebot_e2e
from scripts.simulate_onebot_e2e import OneBotHarness
from scripts.simulate_onebot_e2e import _classify_bridge_marker_failure


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


def run(coro):
    return asyncio.run(coro)


def test_send_and_expect_any_can_wait_longer_than_default_timeout():
    harness = OneBotHarness(
        url="ws://test",
        self_id=1,
        user_id=2,
        nickname="tester",
        timeout=1,
        command_timeout=0.01,
    )
    ws = FakeWs()

    async def exercise():
        async def delayed_response():
            await asyncio.sleep(0.05)
            harness.sent_actions.append({"params": {"message": "late ok"}})

        task = asyncio.create_task(delayed_response())
        try:
            return await harness._send_and_expect_any(ws, "/slow", ["late ok"], timeout=0.2)
        finally:
            await task

    name, ok, detail = run(exercise())

    assert name == "/slow"
    assert ok is True
    assert "late ok" in detail
    assert ws.sent[0]["raw_message"] == "/slow"


def test_send_and_expect_any_tolerates_astrbot_rate_limit_stall_by_default():
    harness = OneBotHarness(
        url="ws://test",
        self_id=1,
        user_id=2,
        nickname="tester",
        timeout=1,
        command_timeout=0.01,
    )
    ws = FakeWs()

    async def exercise():
        async def delayed_response():
            await asyncio.sleep(0.05)
            harness.sent_actions.append({"params": {"message": "rate limited ok"}})

        task = asyncio.create_task(delayed_response())
        try:
            return await harness._send_and_expect_any(ws, "/rate-limited", ["rate limited ok"])
        finally:
            await task

    _, ok, detail = run(exercise())

    assert ok is True
    assert "rate limited ok" in detail


def test_bridge_marker_failure_classifies_pending_running_status():
    status_text = """Codex Bridge: on
turn_status: running
pending_user_inputs: 1
queue: 0"""

    classification = _classify_bridge_marker_failure([], status_text)

    assert classification == "classified as queued: turn_status=running pending_user_inputs=1"


def test_run_sequence_clears_bridge_queue_after_target_binding(monkeypatch):
    harness = OneBotHarness(
        url="ws://test",
        self_id=1,
        user_id=2,
        nickname="tester",
        timeout=0.01,
        command_timeout=0.01,
        thread_target="target-thread",
        skip_bridge_send=True,
    )
    ws = FakeWs()
    calls = []

    async def fake_send_and_expect(self, ws, text, expected):
        calls.append(text)
        return text, True, expected

    async def fake_send_and_expect_any(self, ws, text, expected, *, timeout=None):
        calls.append(text)
        return text, True, expected[0]

    async def fake_send_private_message(self, ws, text):
        calls.append(text)

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(OneBotHarness, "_send_and_expect", fake_send_and_expect)
    monkeypatch.setattr(OneBotHarness, "_send_and_expect_any", fake_send_and_expect_any)
    monkeypatch.setattr(OneBotHarness, "_send_private_message", fake_send_private_message)
    monkeypatch.setattr(simulate_onebot_e2e.asyncio, "sleep", fake_sleep)

    run(harness._run_sequence(ws))

    use_index = calls.index("/codexbridge use target-thread")
    queue_clear_indices = [index for index, text in enumerate(calls) if text == "/codexbridge queue clear"]
    assert any(index > use_index for index in queue_clear_indices)
