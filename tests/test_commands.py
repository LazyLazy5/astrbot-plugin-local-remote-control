import asyncio
from pathlib import Path

from astrbot_plugin_local_remote_control.term.commands import CommandDispatcher, TerminalState
from astrbot_plugin_local_remote_control.term.safe_shell import SafeShell


class DummyHapi:
    def __init__(self):
        self.spawned = []
        self.sent = []
        self.stopped = []
        self.sessions = []
        self.reloaded = False

    async def status(self):
        return True, "HAPI available"

    async def diagnostic_status(self):
        return "endpoint: http://hapi\ntoken: set len=5\nmachines: 1\nrunner: running"

    async def reload_config(self):
        self.reloaded = True
        return True, "HAPI config reloaded"

    async def list_sessions(self, flavor=None):
        if flavor:
            return True, [s for s in self.sessions if (s.get("metadata") or {}).get("flavor") == flavor]
        return True, self.sessions

    async def spawn_session(self, flavor, directory):
        self.spawned.append((flavor, directory))
        return True, f"created {flavor} session-1", "session-1"

    async def send_message(self, session_id, text):
        self.sent.append((session_id, text))
        return True, f"sent {session_id}"

    async def abort_session(self, session_id):
        self.stopped.append(session_id)
        return True, f"stopped {session_id}"


class DummyBridge:
    async def enable(self, umo):
        return "Codex Bridge enabled"

    async def disable(self, umo):
        return "Codex Bridge disabled"

    async def status(self, umo):
        return "Codex Bridge off"

    async def send_to_bound_thread(self, umo, text):
        return False, "No bridge thread"


def run(coro):
    return asyncio.run(coro)


def test_dispatch_pwd(tmp_path):
    state = TerminalState(cwd=tmp_path)
    dispatcher = CommandDispatcher(SafeShell(tmp_path), DummyHapi(), DummyBridge())

    result = run(dispatcher.dispatch("umo", "/pwd", state))

    assert str(tmp_path.resolve()) in result.text
    assert state.cwd == tmp_path.resolve()


def test_dispatch_cd_and_dir(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    state = TerminalState(cwd=tmp_path)
    dispatcher = CommandDispatcher(SafeShell(tmp_path), DummyHapi(), DummyBridge())

    cd_result = run(dispatcher.dispatch("umo", "/cd child", state))
    dir_result = run(dispatcher.dispatch("umo", "/dir", state))

    assert "cwd:" in cd_result.text
    assert state.cwd == child.resolve()
    assert "(empty)" in dir_result.text


def test_unrecognized_input_is_not_fallback(tmp_path):
    state = TerminalState(cwd=tmp_path)
    dispatcher = CommandDispatcher(SafeShell(tmp_path), DummyHapi(), DummyBridge())

    result = run(dispatcher.dispatch("umo", "/unknown", state))

    assert result.handled is True
    assert "终端模式" in result.text


def test_plain_text_tries_current_ai_session(tmp_path):
    state = TerminalState(cwd=tmp_path)
    dispatcher = CommandDispatcher(SafeShell(tmp_path), DummyHapi(), DummyBridge())

    result = run(dispatcher.dispatch("umo", "hello", state))

    assert result.handled is True
    assert "/term agent codex|cc" in result.text


def test_term_agent_codex_creates_hapi_session(tmp_path):
    hapi = DummyHapi()
    state = TerminalState(cwd=tmp_path)
    dispatcher = CommandDispatcher(SafeShell(tmp_path), hapi, DummyBridge())

    result = run(dispatcher.dispatch("umo", "/term agent codex .", state))

    assert result.handled is True
    assert "created codex session-1" in result.text
    assert state.backend == "hapi"
    assert state.current_session_id == "session-1"
    assert hapi.spawned == [("codex", tmp_path.resolve())]


def test_term_agent_cc_creates_claude_session(tmp_path):
    hapi = DummyHapi()
    state = TerminalState(cwd=tmp_path)
    dispatcher = CommandDispatcher(SafeShell(tmp_path), hapi, DummyBridge())

    result = run(dispatcher.dispatch("umo", "/term agent cc .", state))

    assert "created claude session-1" in result.text
    assert state.current_flavor == "claude"


def test_term_stop_aborts_current_hapi_session(tmp_path):
    hapi = DummyHapi()
    state = TerminalState(cwd=tmp_path, backend="hapi", current_session_id="session-1")
    dispatcher = CommandDispatcher(SafeShell(tmp_path), hapi, DummyBridge())

    result = run(dispatcher.dispatch("umo", "/term stop", state))

    assert "stopped session-1" in result.text
    assert hapi.stopped == ["session-1"]


def test_term_send_sends_to_current_hapi_session(tmp_path):
    hapi = DummyHapi()
    state = TerminalState(cwd=tmp_path, backend="hapi", current_session_id="session-1")
    dispatcher = CommandDispatcher(SafeShell(tmp_path), hapi, DummyBridge())

    result = run(dispatcher.dispatch("umo", "/term send hello from qq", state))

    assert "sent session-1" in result.text
    assert hapi.sent == [("session-1", "hello from qq")]


def test_term_hapi_status_uses_diagnostic_status(tmp_path):
    state = TerminalState(cwd=tmp_path)
    dispatcher = CommandDispatcher(SafeShell(tmp_path), DummyHapi(), DummyBridge())

    result = run(dispatcher.dispatch("umo", "/term hapi status", state))

    assert "endpoint: http://hapi" in result.text
    assert "machines: 1" in result.text


def test_codex_new_sets_hapi_backend(tmp_path):
    hapi = DummyHapi()
    state = TerminalState(cwd=tmp_path)
    dispatcher = CommandDispatcher(SafeShell(tmp_path), hapi, DummyBridge())

    result = run(dispatcher.dispatch("umo", "/codex new .", state))

    assert "created codex session-1" in result.text
    assert state.backend == "hapi"
    assert state.current_session_id == "session-1"


def test_term_agent_absolute_path_outside_workdir(tmp_path):
    other_dir = tmp_path.parent
    hapi = DummyHapi()
    state = TerminalState(cwd=tmp_path)
    dispatcher = CommandDispatcher(SafeShell(tmp_path), hapi, DummyBridge())

    result = run(dispatcher.dispatch("umo", f"/term agent codex {other_dir}", state))

    assert "created codex" in result.text
    assert hapi.spawned[0][1] == other_dir.resolve()


def test_term_hapi_reload_updates_runtime_client(tmp_path):
    hapi = DummyHapi()
    state = TerminalState(cwd=tmp_path)
    dispatcher = CommandDispatcher(SafeShell(tmp_path), hapi, DummyBridge())

    result = run(dispatcher.dispatch("umo", "/term hapi reload", state))

    assert result.text == "HAPI config reloaded"
    assert hapi.reloaded is True


def test_term_use_rejects_inactive_session(tmp_path):
    state = TerminalState(
        cwd=tmp_path,
        last_sessions=[{"id": "inactive-1", "active": False, "metadata": {"flavor": "codex"}}],
    )
    dispatcher = CommandDispatcher(SafeShell(tmp_path), DummyHapi(), DummyBridge())

    result = run(dispatcher.dispatch("umo", "/term use 1", state))

    assert "session inactive" in result.text
    assert state.current_session_id is None
    assert state.backend == "shell"
