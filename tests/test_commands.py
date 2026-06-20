import asyncio
from pathlib import Path

from astrbot_plugin_local_remote_control.commands import CommandDispatcher, TerminalState
from astrbot_plugin_local_remote_control.safe_shell import SafeShell


class DummyHapi:
    async def status(self):
        return False, "HAPI unavailable"

    async def list_sessions(self, flavor=None):
        return False, "HAPI unavailable"

    async def send_to_current(self, umo, text):
        return False, "No selected session"


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
    assert "No selected session" in result.text
