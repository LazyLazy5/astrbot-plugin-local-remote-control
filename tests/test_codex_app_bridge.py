import asyncio
import json

from astrbot_plugin_local_remote_control.codex_app_bridge import CodexAppBridge
from astrbot_plugin_local_remote_control.main import _split_message_chunks


class FakeKv:
    def __init__(self):
        self.data = {}

    async def get_kv_data(self, key, default=None):
        return self.data.get(key, default)

    async def put_kv_data(self, key, value):
        self.data[key] = value


class FakeAppServer:
    def __init__(self):
        self.sent = []

    async def list_threads(self):
        return [{"id": "app-thread-1", "title": "App Thread"}]

    async def send_text(self, thread_id, text):
        self.sent.append((thread_id, text))
        return True, f"sent to {thread_id}"


def run(coro):
    return asyncio.run(coro)


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_enable_binds_latest_thread_and_rollout_offset(tmp_path):
    codex_home = tmp_path / ".codex"
    thread_id = "019ee497-9203-7cf0-b071-a37dfe0f4733"
    write_jsonl(
        codex_home / "session_index.jsonl",
        [{"id": thread_id, "thread_name": "demo", "updated_at": "2026-06-20T00:00:00Z"}],
    )
    rollout = codex_home / "sessions" / "2026" / "06" / "20" / f"rollout-demo-{thread_id}.jsonl"
    write_jsonl(
        rollout,
        [{"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "old"}]}}],
    )
    bridge = CodexAppBridge(FakeKv(), codex_home=codex_home)

    message = run(bridge.enable("umo"))

    assert "Codex Bridge on" in message
    assert bridge.bindings["umo"].thread_id == thread_id
    assert bridge.bindings["umo"].rollout_path == rollout
    assert bridge.bindings["umo"].offset == rollout.stat().st_size


def test_enable_prefers_app_server_thread(tmp_path):
    bridge = CodexAppBridge(FakeKv(), codex_home=tmp_path / ".codex", app_server=FakeAppServer())

    message = run(bridge.enable("umo"))

    assert "Codex Bridge on" in message
    assert "App Thread" in message
    assert bridge.app_bindings["umo"] == "app-thread-1"


def test_send_uses_app_server_when_bound(tmp_path):
    app_server = FakeAppServer()
    bridge = CodexAppBridge(FakeKv(), codex_home=tmp_path / ".codex", app_server=app_server)
    run(bridge.enable("umo"))

    ok, message = run(bridge.send_to_bound_thread("umo", "hello"))

    assert ok is True
    assert message == "sent to app-thread-1"
    assert app_server.sent == [("app-thread-1", "hello")]


def test_poll_reads_only_new_assistant_messages(tmp_path):
    codex_home = tmp_path / ".codex"
    thread_id = "019ee497-9203-7cf0-b071-a37dfe0f4733"
    rollout = codex_home / "sessions" / "2026" / "06" / "20" / f"rollout-demo-{thread_id}.jsonl"
    write_jsonl(
        rollout,
        [{"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "old"}]}}],
    )
    bridge = CodexAppBridge(FakeKv(), codex_home=codex_home)
    bridge.windows.add("umo")
    bridge.bindings["umo"] = bridge.create_binding(thread_id, rollout, rollout.stat().st_size)
    with rollout.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "ignore me"}]}}) + "\n")
        f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "new reply"}]}}) + "\n")

    messages = run(bridge.poll_once())

    assert messages == [("umo", "[Codex App]\nnew reply")]


def test_extracts_agent_message_events():
    line = json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "progress update",
                "phase": "commentary",
            },
        }
    )

    assert CodexAppBridge.extract_assistant_text(line) == "progress update"


def test_poll_does_not_commit_offset_until_ack(tmp_path):
    codex_home = tmp_path / ".codex"
    thread_id = "019ee497-9203-7cf0-b071-a37dfe0f4733"
    rollout = codex_home / "sessions" / "2026" / "06" / "20" / f"rollout-demo-{thread_id}.jsonl"
    write_jsonl(rollout, [])
    initial_offset = rollout.stat().st_size
    bridge = CodexAppBridge(FakeKv(), codex_home=codex_home)
    bridge.windows.add("umo")
    bridge.bindings["umo"] = bridge.create_binding(thread_id, rollout, initial_offset)
    with rollout.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "new reply"}]}}) + "\n")

    messages = run(bridge.poll_once())

    assert messages == [("umo", "[Codex App]\nnew reply")]
    assert bridge.bindings["umo"].offset == initial_offset
    assert bridge.kv.data.get("codexbridge_offset_umo") is None

    run(bridge.ack("umo"))

    assert bridge.bindings["umo"].offset == rollout.stat().st_size
    assert bridge.kv.data["codexbridge_offset_umo"] == rollout.stat().st_size


def test_poll_deduplicates_agent_message_and_response_message(tmp_path):
    codex_home = tmp_path / ".codex"
    thread_id = "019ee497-9203-7cf0-b071-a37dfe0f4733"
    rollout = codex_home / "sessions" / "2026" / "06" / "20" / f"rollout-demo-{thread_id}.jsonl"
    write_jsonl(rollout, [])
    bridge = CodexAppBridge(FakeKv(), codex_home=codex_home)
    bridge.windows.add("umo")
    bridge.bindings["umo"] = bridge.create_binding(thread_id, rollout, rollout.stat().st_size)
    with rollout.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "same text", "phase": "commentary"}}) + "\n")
        f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "same text"}]}}) + "\n")

    messages = run(bridge.poll_once())

    assert messages == [("umo", "[Codex App]\nsame text")]


def test_split_message_chunks_preserves_prefix_and_limits_size():
    text = "[Codex App]\n" + ("x" * 2500)

    chunks = _split_message_chunks(text, limit=1000)

    assert len(chunks) == 3
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert chunks[0].startswith("[Codex App]\n")
