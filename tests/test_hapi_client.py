import asyncio
from pathlib import Path

from astrbot_plugin_local_remote_control.hapi_client import HapiClient, HapiConfig, HapiTermBackend


class FakeRawClient:
    def __init__(self):
        self.posts = []
        self.sessions = []
        self.machines = [{"id": "machine-1", "active": True, "runnerState": {"status": "running"}}]
        self.spawn_response = {"type": "success", "sessionId": "session-1"}

    async def get_json(self, path, params=None):
        if path == "/api/machines":
            return {"machines": self.machines}
        if path == "/api/sessions":
            return {"sessions": self.sessions}
        raise AssertionError(path)

    async def post_json(self, path, json=None):
        self.posts.append((path, json))
        if path == "/api/machines/machine-1/spawn":
            return self.spawn_response
        if path == "/api/sessions/session-1/messages":
            return {"ok": True}
        if path == "/api/sessions/session-1/abort":
            return {"ok": True}
        raise AssertionError(path)


def run(coro):
    return asyncio.run(coro)


def test_hapi_client_spawn_session_posts_agent_payload():
    raw = FakeRawClient()
    client = HapiClient(HapiConfig(endpoint="http://hapi", access_token="token"), raw_client=raw)

    ok, message, sid = run(client.spawn_session("codex", Path("C:/work")))

    assert ok is True
    assert sid == "session-1"
    assert "session-1" in message
    assert raw.posts == [
        (
            "/api/machines/machine-1/spawn",
            {
                "directory": str(Path("C:/work")),
                "agent": "codex",
                "sessionType": "simple",
                "yolo": False,
            },
        )
    ]


def test_hapi_client_workspace_root_error_is_actionable():
    raw = FakeRawClient()
    raw.machines = [
        {
            "id": "machine-1",
            "active": True,
            "runnerState": {"status": "running"},
            "workspaceRoots": ["C:/Projects", "D:/Work"],
        }
    ]
    raw.spawn_response = {"type": "error", "message": "Directory is outside this machine's workspace roots"}
    client = HapiClient(HapiConfig(endpoint="http://hapi", access_token="token"), raw_client=raw)

    ok, message, sid = run(client.spawn_session("codex", Path("C:/bad")))

    assert ok is False
    assert sid is None
    assert "outside this machine's workspace roots" in message
    assert "/term agent codex <路径>" in message
    assert "work_dir" in message
    assert "workspace_roots:" in message
    assert "C:/Projects" in message
    assert "D:/Work" in message


def test_hapi_term_backend_creates_and_sends_to_bound_session(tmp_path):
    raw = FakeRawClient()
    client = HapiClient(HapiConfig(endpoint="http://hapi", access_token="token"), raw_client=raw)
    backend = HapiTermBackend(client)

    ok, message = run(backend.start_agent("umo", "claude", tmp_path))
    send_ok, send_message = run(backend.send("umo", "hello"))

    assert ok is True
    assert "session-1" in message
    assert send_ok is True
    assert "session-1" in send_message
    assert raw.posts[-1] == ("/api/sessions/session-1/messages", {"text": "hello"})


def test_hapi_term_backend_lists_and_uses_sessions():
    raw = FakeRawClient()
    raw.sessions = [
        {"id": "abc123", "active": True, "metadata": {"flavor": "codex"}},
        {"id": "def456", "active": False, "metadata": {"flavor": "claude"}},
    ]
    client = HapiClient(HapiConfig(endpoint="http://hapi", access_token="token"), raw_client=raw)
    backend = HapiTermBackend(client)

    ok, message = run(backend.use_session("umo", "def"))

    assert ok is True
    assert "def456" in message
    assert backend.session_for("umo") == "def456"


def test_hapi_client_update_config_replaces_empty_runtime_config():
    client = HapiClient(HapiConfig())

    client.update_config(HapiConfig(endpoint="http://hapi", access_token="token"))

    assert client.configured is True
    assert client.config.endpoint == "http://hapi"
    assert client.config.access_token == "token"


def test_hapi_client_diagnostic_status_hides_token_and_reports_machine_count():
    raw = FakeRawClient()
    client = HapiClient(HapiConfig(endpoint="http://hapi", access_token="secret-token"), raw_client=raw)

    message = run(client.diagnostic_status())

    assert "endpoint: http://hapi" in message
    assert "token: set len=12" in message
    assert "secret-token" not in message
    assert "machines: 1" in message
    assert "runner: running" in message


def test_hapi_client_diagnostic_status_reports_workspace_roots():
    raw = FakeRawClient()
    raw.machines = [
        {
            "id": "machine-1",
            "active": True,
            "namespace": "default",
            "runnerState": {"status": "running"},
            "workspaceRoots": ["C:/Projects", "D:/Work"],
        }
    ]
    client = HapiClient(HapiConfig(endpoint="http://hapi", access_token="secret-token"), raw_client=raw)

    message = run(client.diagnostic_status())

    assert "workspace_roots:" in message
    assert "C:/Projects" in message
    assert "D:/Work" in message


def test_hapi_client_not_configured_messages_are_actionable():
    client = HapiClient(HapiConfig())

    ok, status = run(client.status())
    list_ok, list_message = run(client.list_sessions())
    spawn_ok, spawn_message, sid = run(client.spawn_session("codex", Path("C:/work")))
    send_ok, send_message = run(client.send_message("session-1", "hello"))

    assert ok is False
    assert list_ok is False
    assert spawn_ok is False
    assert send_ok is False
    assert sid is None
    for message in (status, list_message, spawn_message, send_message):
        assert "HAPI not configured" not in message
        assert "/term hapi reload" in message
        assert "hapi_endpoint/access_token" in message
