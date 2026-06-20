import asyncio
import json

from astrbot_plugin_local_remote_control.codex_app_bridge import CodexAppBridge


class FakeKv:
    def __init__(self):
        self.data = {}

    async def get_kv_data(self, key, default=None):
        return self.data.get(key, default)

    async def put_kv_data(self, key, value):
        self.data[key] = value


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
