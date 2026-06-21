import asyncio
import json
import sys

import astrbot_plugin_local_remote_control.codex_app_bridge as codex_app_bridge
from astrbot_plugin_local_remote_control.codex_app_bridge import CodexAppBridge
from astrbot_plugin_local_remote_control.delivery_queue import DeliveryQueue
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
        self.remembered = {}
        self.threads = [{"id": "app-thread-1", "title": "App Thread", "path": "C:/rollout.jsonl"}]

    async def list_threads(self):
        return self.threads

    async def send_text(self, thread_id, text):
        self.sent.append((thread_id, text))
        return True, f"sent to {thread_id}"

    def remember_thread_path(self, thread_id, path):
        self.remembered[thread_id] = path


class StatusAwareFakeAppServer(FakeAppServer):
    def __init__(self, status="idle"):
        super().__init__()
        self.status = status
        self.status_calls = []

    async def thread_status(self, thread_id):
        self.status_calls.append(thread_id)
        return self.status


class FakePersistentAppServer(FakeAppServer):
    def __init__(self):
        super().__init__()
        self.closed = False
        self.events = []
        self.next_turn = 0

    async def send_text(self, thread_id, text):
        self.next_turn += 1
        turn_id = f"turn-{self.next_turn}"
        self.sent.append((thread_id, text))
        return True, f"sent to {thread_id} turn {turn_id}"

    async def poll_events(self):
        events = self.events
        self.events = []
        return events

    async def close(self):
        self.closed = True


class FailingAppServer:
    async def list_threads(self):
        raise RuntimeError("proxy socket unavailable")

    async def send_text(self, thread_id, text):
        raise RuntimeError("proxy send unavailable")


class FailingSendAppServer(FakeAppServer):
    async def send_text(self, thread_id, text):
        raise RuntimeError("failed to connect to socket at C:/app-server-control.sock")


class FakeStdin:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(json.loads(data.decode("utf-8")))

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class FakeStdout:
    def __init__(self, rows):
        self.rows = [json.dumps(row).encode("utf-8") + b"\n" for row in rows]

    async def readline(self):
        if self.rows:
            return self.rows.pop(0)
        return b""


class FakeStderr:
    async def read(self):
        return b""


class FakeProcess:
    def __init__(self, rows):
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(rows)
        self.stderr = FakeStderr()
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class ResumeAwareFakeAppServer:
    def __init__(self):
        self.sent = []

    async def list_threads(self):
        return [{"id": "app-thread-1", "title": "App Thread", "path": "C:/rollout.jsonl"}]

    async def send_text(self, thread_id, text):
        self.sent.append((thread_id, text))
        return True, f"sent to {thread_id}"


class AppendingFakeAppServer:
    def __init__(self, rollout):
        self.rollout = rollout

    async def list_threads(self):
        return [{"id": "app-thread-1", "title": "App Thread", "path": str(self.rollout)}]

    async def send_text(self, thread_id, text):
        with self.rollout.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "BRIDGE_E2E_OK", "phase": "final_answer"}}) + "\n")
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
    bridge = CodexAppBridge(
        FakeKv(),
        codex_home=codex_home,
        app_server=FailingAppServer(),
        fallback_app_server=FailingAppServer(),
    )

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


def test_enable_app_server_thread_also_binds_jsonl_path(tmp_path):
    rollout = tmp_path / ".codex" / "sessions" / "rollout-app-thread-1.jsonl"
    write_jsonl(rollout, [])
    app_server = FakeAppServer()
    app_server.threads = [{"id": "app-thread-1", "title": "App Thread", "path": str(rollout)}]
    bridge = CodexAppBridge(FakeKv(), codex_home=tmp_path / ".codex", app_server=app_server)

    run(bridge.enable("umo"))

    assert bridge.bindings["umo"].thread_id == "app-thread-1"
    assert bridge.bindings["umo"].rollout_path == rollout
    assert bridge.bindings["umo"].offset == rollout.stat().st_size


def test_enable_falls_back_to_stdio_app_server_when_proxy_fails(tmp_path):
    fallback = FakeAppServer()
    bridge = CodexAppBridge(
        FakeKv(),
        codex_home=tmp_path / ".codex",
        app_server=FailingAppServer(),
        fallback_app_server=fallback,
    )

    message = run(bridge.enable("umo"))
    ok, send_message = run(bridge.send_to_bound_thread("umo", "hello"))

    assert "Codex Bridge on" in message
    assert "App Thread" in message
    assert bridge.app_bindings["umo"] == "app-thread-1"
    assert ok is True
    assert send_message == "sent to app-thread-1"
    assert fallback.sent == [("app-thread-1", "hello")]


def test_load_restores_stdio_transport_and_thread_path(tmp_path):
    kv = FakeKv()
    kv.data["codexbridge_windows"] = ["umo"]
    kv.data["codexbridge_app_thread_umo"] = "app-thread-1"
    kv.data["codexbridge_app_transport_umo"] = "stdio"
    kv.data["codexbridge_app_thread_path_umo"] = "C:/rollout.jsonl"
    proxy = FakeAppServer()
    stdio = FakeAppServer()
    bridge = CodexAppBridge(
        kv,
        codex_home=tmp_path / ".codex",
        app_server=proxy,
        fallback_app_server=stdio,
    )

    run(bridge.load())
    ok, message = run(bridge.send_to_bound_thread("umo", "hello"))

    assert ok is True
    assert message == "sent to app-thread-1"
    assert proxy.sent == []
    assert stdio.sent == [("app-thread-1", "hello")]
    assert stdio.remembered == {"app-thread-1": "C:/rollout.jsonl"}
    assert "transport: stdio" in run(bridge.status("umo"))


def test_send_falls_back_from_proxy_socket_error_to_stdio(tmp_path):
    proxy = FailingSendAppServer()
    stdio = FakeAppServer()
    bridge = CodexAppBridge(
        FakeKv(),
        codex_home=tmp_path / ".codex",
        app_server=proxy,
        fallback_app_server=stdio,
    )

    run(bridge.enable("umo"))
    ok, message = run(bridge.send_to_bound_thread("umo", "hello"))

    assert ok is True
    assert message == "sent to app-thread-1"
    assert stdio.sent == [("app-thread-1", "hello")]
    assert "transport: stdio" in run(bridge.status("umo"))


def test_send_reports_both_transport_errors_when_proxy_and_stdio_fail(tmp_path):
    bridge = CodexAppBridge(
        FakeKv(),
        codex_home=tmp_path / ".codex",
        app_server=FailingSendAppServer(),
        fallback_app_server=FailingAppServer(),
    )
    bridge.windows.add("umo")
    bridge.app_bindings["umo"] = "app-thread-1"
    bridge._active_app_servers["umo"] = bridge.app_server
    bridge._app_server_modes["umo"] = "proxy"

    ok, message = run(bridge.send_to_bound_thread("umo", "hello"))

    assert ok is False
    assert "read-only" in message
    assert "proxy:" in message
    assert "stdio:" in message


def test_stdio_app_server_initializes_before_request_and_ignores_notifications(monkeypatch):
    process = FakeProcess(
        [
            {"jsonrpc": "2.0", "id": 1, "result": {"codexHome": "x", "platformFamily": "windows", "platformOs": "windows", "userAgent": "test"}},
            {"method": "thread/status/changed", "params": {"threadId": "ignored"}},
            {"jsonrpc": "2.0", "id": 999, "result": {"ignored": True}},
            {"jsonrpc": "2.0", "id": 2, "result": {"data": [{"id": "thread-1", "title": "Thread"}]}},
        ]
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    client = codex_app_bridge.CodexStdioAppServerClient(codex_command="codex.cmd")

    threads = run(client.list_threads())

    assert threads == [{"id": "thread-1", "title": "Thread"}]
    assert [write["method"] for write in process.stdin.writes] == ["initialize", "initialized", "thread/list"]
    assert process.stdin.writes[0]["params"]["capabilities"]["experimentalApi"] is True
    assert process.stdin.closed is True
    assert process.terminated is True


def test_stdio_app_server_resumes_thread_before_turn_start(monkeypatch):
    process = FakeProcess(
        [
            {"jsonrpc": "2.0", "id": 1, "result": {"codexHome": "x", "platformFamily": "windows", "platformOs": "windows", "userAgent": "test"}},
            {"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "thread-1"}}},
            {"method": "thread/status/changed", "params": {"threadId": "thread-1"}},
            {"jsonrpc": "2.0", "id": 3, "result": {"turn": {"id": "turn-1"}}},
        ]
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    client = codex_app_bridge.CodexStdioAppServerClient(codex_command="codex.cmd")
    client.remember_thread_path("thread-1", "C:/rollout.jsonl")

    ok, message = run(client.send_text("thread-1", "hello"))

    assert ok is True
    assert "turn-1" in message
    assert [write["method"] for write in process.stdin.writes] == [
        "initialize",
        "initialized",
        "thread/resume",
        "turn/start",
    ]
    assert process.stdin.writes[2]["params"] == {
        "threadId": "thread-1",
        "path": "C:/rollout.jsonl",
        "excludeTurns": True,
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
        "cwd": codex_app_bridge.CodexStdioAppServerClient._default_cwd(),
    }


def test_app_server_turn_requests_full_permissions(monkeypatch):
    calls = []

    async def fake_request(self, method, params):
        calls.append((method, params))
        return {"turn": {"id": "turn-1"}} if method == "turn/start" else {}

    monkeypatch.setattr(codex_app_bridge.CodexAppServerClient, "_request", fake_request)
    client = codex_app_bridge.CodexAppServerClient()

    ok, message = run(client.send_text("thread-1", "hello"))

    assert ok is True
    assert "turn-1" in message
    resume = calls[0][1]
    turn_start = calls[1][1]
    assert resume["approvalPolicy"] == "never"
    assert resume["sandbox"] == "danger-full-access"
    assert resume["cwd"]
    assert turn_start["approvalPolicy"] == "never"
    assert turn_start["sandboxPolicy"] == {"type": "dangerFullAccess"}
    assert turn_start["cwd"]


def test_persistent_stdio_client_does_not_close_process_after_turn_start(monkeypatch):
    process = FakeProcess(
        [
            {"jsonrpc": "2.0", "id": 1, "result": {"codexHome": "x", "platformFamily": "windows", "platformOs": "windows", "userAgent": "test"}},
            {"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "thread-1"}}},
            {"jsonrpc": "2.0", "id": 3, "result": {"turn": {"id": "turn-1"}}},
        ]
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    client = codex_app_bridge.PersistentCodexStdioAppServerClient(codex_command="codex.cmd")
    client.remember_thread_path("thread-1", "C:/rollout.jsonl")

    ok, message = run(client.send_text("thread-1", "hello"))

    assert ok is True
    assert "turn-1" in message
    assert process.stdin.closed is False
    assert process.terminated is False

    run(client.close())


def test_bridge_remembers_stdio_thread_path_for_send(tmp_path):
    fallback = ResumeAwareFakeAppServer()
    bridge = CodexAppBridge(
        FakeKv(),
        codex_home=tmp_path / ".codex",
        app_server=FailingAppServer(),
        fallback_app_server=fallback,
    )

    run(bridge.enable("umo"))
    run(bridge.send_to_bound_thread("umo", "hello"))

    assert fallback.sent == [("app-thread-1", "hello")]


def test_bridge_queues_user_input_while_turn_running_and_flushes_after_completion(tmp_path):
    server = FakePersistentAppServer()
    bridge = CodexAppBridge(
        FakeKv(),
        codex_home=tmp_path / ".codex",
        app_server=FailingAppServer(),
        fallback_app_server=server,
    )
    run(bridge.enable("umo"))

    ok, _ = run(bridge.send_to_bound_thread("umo", "first"))
    assert ok is True
    ok, message = run(bridge.send_to_bound_thread("umo", "second"))

    assert ok is True
    assert "queued" in message
    assert server.sent == [("app-thread-1", "first")]

    server.events.append({"method": "turn/completed", "params": {"threadId": "app-thread-1", "turnId": "turn-1"}})
    run(bridge.poll_once())

    assert server.sent == [("app-thread-1", "first"), ("app-thread-1", "second")]


def test_status_recovers_stale_running_state_and_flushes_pending_input(tmp_path):
    server = StatusAwareFakeAppServer(status="idle")
    bridge = CodexAppBridge(
        FakeKv(),
        codex_home=tmp_path / ".codex",
        app_server=server,
    )
    run(bridge.enable("umo"))
    state = bridge._turn_state("umo")
    state.status = "running"
    state.active_turn_id = "stale-turn"
    state.pending_user_inputs.append("queued probe")

    status = run(bridge.status("umo"))

    assert "turn_status: running" not in status
    assert "turn_status: running(stale)" not in status
    assert "turn_status: idle" in status
    assert "pending_user_inputs: 0" in status
    assert server.status_calls == ["app-thread-1"]
    assert server.sent == [("app-thread-1", "queued probe")]


def test_bridge_flushes_stdio_agent_deltas_as_one_message_on_completion(tmp_path):
    queue = DeliveryQueue(FakeKv())
    server = FakePersistentAppServer()
    bridge = CodexAppBridge(
        queue.kv,
        codex_home=tmp_path / ".codex",
        app_server=FailingAppServer(),
        fallback_app_server=server,
        delivery_queue=queue,
    )
    run(bridge.enable("umo"))
    server.events.extend(
        [
            {"method": "item/agentMessage/delta", "params": {"threadId": "app-thread-1", "turnId": "turn-1", "delta": "BR"}},
            {"method": "item/agentMessage/delta", "params": {"threadId": "app-thread-1", "turnId": "turn-1", "delta": "IDGE_E2E_OK"}},
            {"method": "turn/completed", "params": {"threadId": "app-thread-1", "turnId": "turn-1"}},
        ]
    )

    run(bridge.poll_once())

    due = run(queue.due_items())
    assert [item.text for item in due] == ["[Codex App]\nBRIDGE_E2E_OK"]


def test_jsonl_task_complete_marks_turn_idle(tmp_path):
    codex_home = tmp_path / ".codex"
    thread_id = "app-thread-1"
    rollout = codex_home / "sessions" / "rollout-app-thread-1.jsonl"
    write_jsonl(rollout, [])
    bridge = CodexAppBridge(FakeKv(), codex_home=codex_home)
    bridge.windows.add("umo")
    bridge.app_bindings["umo"] = thread_id
    bridge.turn_states["umo"] = codex_app_bridge.BridgeTurnState(status="running", active_turn_id="turn-1")
    bridge.bindings["umo"] = bridge.create_binding(thread_id, rollout, rollout.stat().st_size)
    with rollout.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}}) + "\n")

    run(bridge.poll_once())

    status = run(bridge.status("umo"))
    assert "turn_status: idle" in status


def test_jsonl_task_complete_flushes_pending_user_input(tmp_path):
    codex_home = tmp_path / ".codex"
    thread_id = "app-thread-1"
    rollout = codex_home / "sessions" / "rollout-app-thread-1.jsonl"
    write_jsonl(rollout, [])
    server = FakePersistentAppServer()
    bridge = CodexAppBridge(
        FakeKv(),
        codex_home=codex_home,
        app_server=server,
    )
    bridge.windows.add("umo")
    bridge.app_bindings["umo"] = thread_id
    bridge._active_app_servers["umo"] = server
    bridge._app_server_modes["umo"] = "proxy"
    bridge.turn_states["umo"] = codex_app_bridge.BridgeTurnState(
        status="running",
        active_turn_id="turn-1",
        pending_user_inputs=["queued after complete"],
    )
    bridge.bindings["umo"] = bridge.create_binding(thread_id, rollout, rollout.stat().st_size)
    with rollout.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}}) + "\n")

    run(bridge.poll_once())

    status = run(bridge.status("umo"))
    assert "turn_status: running" in status
    assert "pending_user_inputs: 0" in status
    assert server.sent == [(thread_id, "queued after complete")]


def test_disable_closes_persistent_app_server_when_last_window_is_off(tmp_path):
    server = FakePersistentAppServer()
    bridge = CodexAppBridge(
        FakeKv(),
        codex_home=tmp_path / ".codex",
        app_server=FailingAppServer(),
        fallback_app_server=server,
    )
    run(bridge.enable("umo"))

    message = run(bridge.disable("umo"))

    assert message == "Codex Bridge off"
    assert server.closed is True


def test_codexbridge_status_includes_turn_state_and_pending_count(tmp_path):
    server = FakePersistentAppServer()
    bridge = CodexAppBridge(
        FakeKv(),
        codex_home=tmp_path / ".codex",
        app_server=FailingAppServer(),
        fallback_app_server=server,
    )
    run(bridge.enable("umo"))
    run(bridge.send_to_bound_thread("umo", "first"))
    run(bridge.send_to_bound_thread("umo", "second"))

    status = run(bridge.status("umo"))

    assert "turn_status: running" in status
    assert "active_turn_id: turn-1" in status
    assert "pending_user_inputs: 1" in status


def test_default_codex_command_prefers_windows_cmd_or_exe(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(codex_app_bridge.shutil, "which", lambda name: f"C:/tools/{name}" if name == "codex.cmd" else None)

    assert codex_app_bridge.default_codex_command() == "codex.cmd"


def test_probe_reports_read_only_when_proxy_and_stdio_fail(tmp_path):
    bridge = CodexAppBridge(
        FakeKv(),
        codex_home=tmp_path / ".codex",
        app_server=FailingAppServer(),
        fallback_app_server=FailingAppServer(),
    )

    message = run(bridge.enable("umo"))
    probe = run(bridge.probe())

    assert "Codex Bridge on" in message
    assert "read-only" in probe
    assert "proxy socket unavailable" in probe


def test_read_only_send_failure_notice_is_suppressed_for_30_seconds(tmp_path):
    now = 100.0
    bridge = CodexAppBridge(FakeKv(), codex_home=tmp_path / ".codex", now=lambda: now)
    message = "Codex Bridge is read-only in this build; app-server write support unavailable."

    assert bridge.should_report_send_failure("umo", message) is True
    assert bridge.should_report_send_failure("umo", message) is False

    now = 131.0

    assert bridge.should_report_send_failure("umo", message) is True


def test_send_uses_app_server_when_bound(tmp_path):
    app_server = FakeAppServer()
    bridge = CodexAppBridge(FakeKv(), codex_home=tmp_path / ".codex", app_server=app_server)
    run(bridge.enable("umo"))

    ok, message = run(bridge.send_to_bound_thread("umo", "hello"))

    assert ok is True
    assert message == "sent to app-thread-1"
    assert app_server.sent == [("app-thread-1", "hello")]


def test_send_to_bound_thread_polls_new_app_output_after_success(tmp_path):
    rollout = tmp_path / ".codex" / "sessions" / "rollout-app-thread-1.jsonl"
    write_jsonl(rollout, [])
    kv = FakeKv()
    queue = DeliveryQueue(kv)
    app_server = AppendingFakeAppServer(rollout)
    bridge = CodexAppBridge(kv, codex_home=tmp_path / ".codex", app_server=app_server, delivery_queue=queue)
    run(bridge.enable("umo"))

    ok, message = run(bridge.send_to_bound_thread("umo", "bridge diagnostic"))

    assert ok is True
    assert message == "sent to app-thread-1"
    due = run(queue.due_items())
    assert [item.text for item in due] == ["[Codex App]\nBRIDGE_E2E_OK"]


def test_codexbridge_list_threads_remembers_candidates(tmp_path):
    app_server = FakeAppServer()
    app_server.threads = [
        {"id": "thread-1", "title": "First", "path": "C:/first.jsonl"},
        {"id": "thread-2", "title": "Second", "path": "C:/second.jsonl"},
    ]
    bridge = CodexAppBridge(FakeKv(), codex_home=tmp_path / ".codex", app_server=app_server)

    message = run(bridge.list_threads("umo"))

    assert "1. thread-1 First" in message
    assert "2. thread-2 Second" in message


def test_codexbridge_use_selects_thread_by_index_and_persists_transport(tmp_path):
    kv = FakeKv()
    app_server = FakeAppServer()
    app_server.threads = [
        {"id": "thread-1", "title": "First", "path": "C:/first.jsonl"},
        {"id": "thread-2", "title": "Second", "path": "C:/second.jsonl"},
    ]
    bridge = CodexAppBridge(kv, codex_home=tmp_path / ".codex", app_server=app_server)

    run(bridge.list_threads("umo"))
    message = run(bridge.use_thread("umo", "2"))
    ok, send_message = run(bridge.send_to_bound_thread("umo", "hello"))

    assert "thread-2" in message
    assert ok is True
    assert send_message == "sent to thread-2"
    assert kv.data["codexbridge_app_thread_umo"] == "thread-2"
    assert kv.data["codexbridge_app_transport_umo"] == "proxy"
    assert kv.data["codexbridge_app_thread_path_umo"] == "C:/second.jsonl"


def test_codexbridge_use_selects_thread_by_id_prefix(tmp_path):
    app_server = FakeAppServer()
    app_server.threads = [
        {"id": "abc123", "title": "First", "path": "C:/first.jsonl"},
        {"id": "def456", "title": "Second", "path": "C:/second.jsonl"},
    ]
    bridge = CodexAppBridge(FakeKv(), codex_home=tmp_path / ".codex", app_server=app_server)

    run(bridge.list_threads("umo"))
    message = run(bridge.use_thread("umo", "def"))

    assert "def456" in message


def test_codexbridge_status_includes_needs_user_refresh(tmp_path):
    kv = FakeKv()
    queue = DeliveryQueue(kv)
    umo = "weixin_personal_tnco:FriendMessage:user"
    bridge = CodexAppBridge(kv, codex_home=tmp_path / ".codex", delivery_queue=queue)

    run(queue.enqueue(umo, "codexbridge", "pending", dedupe_key="pending"))
    run(queue.mark_failed_umo(umo, "ret=-2"))

    status = run(bridge.status(umo))

    assert "needs_user_refresh: yes" in status


def test_poll_reads_new_assistant_and_app_user_messages(tmp_path):
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

    assert messages == [("umo", "[Codex App/User]\nignore me"), ("umo", "[Codex App]\nnew reply")]


def test_poll_enqueues_and_advances_offset_without_send_ack(tmp_path):
    codex_home = tmp_path / ".codex"
    thread_id = "019ee497-9203-7cf0-b071-a37dfe0f4733"
    rollout = codex_home / "sessions" / "2026" / "06" / "20" / f"rollout-demo-{thread_id}.jsonl"
    write_jsonl(rollout, [])
    initial_offset = rollout.stat().st_size
    kv = FakeKv()
    queue = DeliveryQueue(kv)
    bridge = CodexAppBridge(kv, codex_home=codex_home, delivery_queue=queue)
    bridge.windows.add("umo")
    bridge.bindings["umo"] = bridge.create_binding(thread_id, rollout, initial_offset)
    with rollout.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "new reply"}]}}) + "\n")

    messages = run(bridge.poll_once())

    assert messages == []
    assert bridge.bindings["umo"].offset == rollout.stat().st_size
    assert kv.data["codexbridge_offset_umo"] == rollout.stat().st_size
    due = run(queue.due_items())
    assert [(item.umo, item.channel, item.text) for item in due] == [("umo", "codexbridge", "[Codex App]\nnew reply")]


def test_poll_enqueues_codex_app_user_messages(tmp_path):
    codex_home = tmp_path / ".codex"
    thread_id = "019ee497-9203-7cf0-b071-a37dfe0f4733"
    rollout = codex_home / "sessions" / "2026" / "06" / "20" / f"rollout-demo-{thread_id}.jsonl"
    write_jsonl(rollout, [])
    queue = DeliveryQueue(FakeKv())
    bridge = CodexAppBridge(queue.kv, codex_home=codex_home, delivery_queue=queue)
    bridge.windows.add("umo")
    bridge.bindings["umo"] = bridge.create_binding(thread_id, rollout, rollout.stat().st_size)
    with rollout.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "typed in app"}]}}) + "\n")

    run(bridge.poll_once())

    due = run(queue.due_items())
    assert [item.text for item in due] == ["[Codex App/User]\ntyped in app"]


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


def test_extract_visible_text_returns_user_role():
    line = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "from app"}],
            },
        }
    )

    assert CodexAppBridge.extract_visible_text(line) == ("user", "from app")


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


def test_poll_buffers_jsonl_agent_message_fragments_until_task_complete(tmp_path):
    codex_home = tmp_path / ".codex"
    thread_id = "019ee497-9203-7cf0-b071-a37dfe0f4733"
    rollout = codex_home / "sessions" / "2026" / "06" / "20" / f"rollout-demo-{thread_id}.jsonl"
    write_jsonl(rollout, [])
    bridge = CodexAppBridge(FakeKv(), codex_home=codex_home)
    bridge.windows.add("umo")
    bridge.bindings["umo"] = bridge.create_binding(thread_id, rollout, rollout.stat().st_size)
    with rollout.open("a", encoding="utf-8") as f:
        for text in ("execut", "ing", "-pl", "ans"):
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": text, "phase": "commentary"}}) + "\n")

    messages = run(bridge.poll_once())

    assert messages == []

    with rollout.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}}) + "\n")

    messages = run(bridge.poll_once())

    assert messages == [("umo", "[Codex App]\nexecuting-plans")]


def test_poll_deduplicates_event_and_jsonl_final_output_with_delivery_queue(tmp_path):
    codex_home = tmp_path / ".codex"
    thread_id = "app-thread-1"
    rollout = codex_home / "sessions" / "rollout-app-thread-1.jsonl"
    write_jsonl(rollout, [])
    kv = FakeKv()
    queue = DeliveryQueue(kv)
    server = FakePersistentAppServer()
    bridge = CodexAppBridge(
        kv,
        codex_home=codex_home,
        app_server=FailingAppServer(),
        fallback_app_server=server,
        delivery_queue=queue,
    )
    run(bridge.enable("umo"))
    bridge.bindings["umo"] = bridge.create_binding(thread_id, rollout, rollout.stat().st_size)
    server.events.extend(
        [
            {"method": "item/agentMessage/delta", "params": {"threadId": thread_id, "turnId": "turn-1", "delta": "BRIDGE_E2E_OK"}},
            {"method": "turn/completed", "params": {"threadId": thread_id, "turnId": "turn-1"}},
        ]
    )
    with rollout.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "BRIDGE_E2E_OK", "phase": "final_answer"}}) + "\n")
        f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "BRIDGE_E2E_OK"}]}}) + "\n")

    run(bridge.poll_once())

    due = run(queue.due_items())
    assert [item.text for item in due] == ["[Codex App]\nBRIDGE_E2E_OK"]


def test_poll_suppresses_environment_context_app_user_messages(tmp_path):
    codex_home = tmp_path / ".codex"
    thread_id = "019ee497-9203-7cf0-b071-a37dfe0f4733"
    rollout = codex_home / "sessions" / "2026" / "06" / "20" / f"rollout-demo-{thread_id}.jsonl"
    write_jsonl(rollout, [])
    queue = DeliveryQueue(FakeKv())
    bridge = CodexAppBridge(queue.kv, codex_home=codex_home, delivery_queue=queue)
    bridge.windows.add("umo")
    bridge.bindings["umo"] = bridge.create_binding(thread_id, rollout, rollout.stat().st_size)
    with rollout.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>\n  <current_date>2026-06-21</current_date>\n</environment_context>"}]}}) + "\n")

    run(bridge.poll_once())

    assert run(queue.due_items()) == []


def test_poll_surfaces_function_call_execution_errors_as_system_messages(tmp_path):
    codex_home = tmp_path / ".codex"
    thread_id = "app-thread-1"
    rollout = codex_home / "sessions" / "rollout-app-thread-1.jsonl"
    write_jsonl(rollout, [])
    bridge = CodexAppBridge(FakeKv(), codex_home=codex_home)
    bridge.windows.add("umo")
    bridge.bindings["umo"] = bridge.create_binding(thread_id, rollout, rollout.stat().st_size)
    with rollout.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-1",
                        "output": "execution error: windows sandbox: runner error: CreateProcessAsUserW failed: 5",
                    },
                }
            )
            + "\n"
        )

    messages = run(bridge.poll_once())

    assert messages == [
        (
            "umo",
            "[Codex App/System]\nexecution error: windows sandbox: runner error: CreateProcessAsUserW failed: 5",
        )
    ]


def test_proxy_app_server_resumes_thread_before_turn_start(monkeypatch):
    calls = []

    async def fake_request(self, method, params):
        calls.append(method)
        return {"turn": {"id": "turn-1"}} if method == "turn/start" else {}

    monkeypatch.setattr(codex_app_bridge.CodexAppServerClient, "_request", fake_request)
    client = codex_app_bridge.CodexAppServerClient()

    ok, message = run(client.send_text("thread-1", "hello"))

    assert ok is True
    assert calls == ["thread/resume", "turn/start"]


def test_split_message_chunks_preserves_prefix_and_limits_size():
    text = "[Codex App]\n" + ("x" * 2500)

    chunks = _split_message_chunks(text, limit=1000)

    assert len(chunks) == 3
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert chunks[0].startswith("[Codex App]\n")
