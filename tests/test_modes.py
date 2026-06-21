import asyncio

from astrbot_plugin_local_remote_control.commands import TerminalState
from astrbot_plugin_local_remote_control.main import (
    ModeStore,
    _action_from_full_command_text,
    _bridge_should_dispatch_terminal_command,
    _command_text,
    _is_bridge_ack_text,
    _is_self_event,
    _normalize_terminal_dispatch_text,
    _should_ignore_bridge_input_text,
    _should_dispatch_terminal_text,
    _split_control_command,
)


class FakeKv:
    def __init__(self):
        self.data = {}

    async def get_kv_data(self, key, default=None):
        return self.data.get(key, default)

    async def put_kv_data(self, key, value):
        self.data[key] = value


def run(coro):
    return asyncio.run(coro)


def test_terminal_and_bridge_modes_are_independent():
    kv = FakeKv()
    store = ModeStore(kv)

    run(store.load())
    run(store.enable_terminal("window"))
    run(store.enable_bridge("window"))
    run(store.disable_terminal("window"))

    assert not store.is_terminal("window")
    assert store.is_bridge("window")


def test_modes_persist_separately():
    kv = FakeKv()
    first = ModeStore(kv)

    run(first.load())
    run(first.enable_terminal("term-window"))
    run(first.enable_bridge("bridge-window"))

    second = ModeStore(kv)
    run(second.load())

    assert second.is_terminal("term-window")
    assert not second.is_terminal("bridge-window")
    assert second.is_bridge("bridge-window")
    assert not second.is_bridge("term-window")


def test_mode_store_persists_term_state_windows():
    kv = FakeKv()
    store = ModeStore(kv)

    run(store.load())
    run(store.remember_term_state_window("hapi-window"))

    second = ModeStore(kv)
    run(second.load())

    assert "hapi-window" in second.term_state_windows


def test_control_commands_accept_slashless_text_inside_terminal_mode():
    assert _split_control_command("/term off") == ("term", "off")
    assert _split_control_command("term off") == ("term", "off")
    assert _split_control_command("/term agent codex .") == ("term", "agent codex .")
    assert _split_control_command("/codexbridge on") == ("codexbridge", "on")
    assert _split_control_command("codexbridge off") == ("codexbridge", "off")


def test_control_commands_accept_codexbridge_typo_alias():
    assert _split_control_command("/codexbrideg on") == ("codexbridge", "on")
    assert _split_control_command("codex brideg off") == ("codexbridge", "off")


def test_bare_codex_is_recognized_as_control_hint_inside_terminal_mode():
    assert _split_control_command("codex") == ("codex", "")
    assert _split_control_command("/codex") == ("codex", "")
    assert _split_control_command("codex --version") == ("codex", "--version")
    assert _split_control_command("claude") == ("claude", "")
    assert _split_control_command("claude --version") == ("claude", "--version")


def test_bridge_mode_routes_term_commands_to_terminal_dispatcher():
    assert _bridge_should_dispatch_terminal_command("/term agent codex .")
    assert _bridge_should_dispatch_terminal_command("/term hapi status")
    assert not _bridge_should_dispatch_terminal_command("/term on")
    assert not _bridge_should_dispatch_terminal_command("/term off")
    assert not _bridge_should_dispatch_terminal_command("/term status")
    assert not _bridge_should_dispatch_terminal_command("/term retry")
    assert not _bridge_should_dispatch_terminal_command("/term queue clear")
    assert not _bridge_should_dispatch_terminal_command("/codexbridge off")
    assert not _bridge_should_dispatch_terminal_command("hello codex app")


def test_command_text_restores_slash_for_dispatcher():
    assert _command_text("term", "agent codex") == "/term agent codex"
    assert _command_text("term", "") == "/term"


def test_action_from_full_command_text_keeps_remaining_arguments():
    assert _action_from_full_command_text("/term agent codex", "term", "agent") == "agent codex"
    assert _action_from_full_command_text("term agent cc .", "term", "agent") == "agent cc ."
    assert _action_from_full_command_text("/codexbridge queue clear", "codexbridge", "queue") == "queue clear"
    assert _action_from_full_command_text("hello", "term", "fallback") == "fallback"


def test_shell_backend_plain_text_goes_to_dispatcher_for_restricted_handling(tmp_path):
    state = TerminalState(cwd=tmp_path, backend="shell")

    assert _should_dispatch_terminal_text("echo hi", state)
    assert _should_dispatch_terminal_text("你好", state)
    assert _should_dispatch_terminal_text("/pwd", state)


def test_slashless_safe_shell_commands_are_normalized_in_shell_backend(tmp_path):
    state = TerminalState(cwd=tmp_path, backend="shell")

    assert _normalize_terminal_dispatch_text("pwd", state) == "/pwd"
    assert _normalize_terminal_dispatch_text("dir child", state) == "/dir child"
    assert _normalize_terminal_dispatch_text("cd child", state) == "/cd child"
    assert _normalize_terminal_dispatch_text("git status", state) == "/git status"
    assert _normalize_terminal_dispatch_text("hello", state) == "hello"


def test_hapi_backend_does_not_slash_normalize_plain_text(tmp_path):
    state = TerminalState(cwd=tmp_path, backend="hapi", current_session_id="sid")

    assert _normalize_terminal_dispatch_text("hello", state) == "hello"


def test_safe_shell_commands_are_normalized_even_in_hapi_backend(tmp_path):
    state = TerminalState(cwd=tmp_path, backend="hapi", current_session_id="sid")

    assert _normalize_terminal_dispatch_text("pwd", state) == "/pwd"
    assert _normalize_terminal_dispatch_text("dir", state) == "/dir"
    assert _normalize_terminal_dispatch_text("cd .", state) == "/cd ."
    assert _normalize_terminal_dispatch_text("git status", state) == "/git status"


def test_hapi_backend_plain_text_goes_to_dispatcher(tmp_path):
    state = TerminalState(cwd=tmp_path, backend="hapi", current_session_id="sid")

    assert _should_dispatch_terminal_text("hello", state)


class FakeEvent:
    def __init__(self, sender_id="123", self_id="999", raw_message=None):
        self.message_obj = type("MessageObj", (), {"raw_message": raw_message or {}})()
        self._sender_id = sender_id
        self._self_id = self_id

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id


def test_self_event_detection_uses_sender_and_self_id():
    assert _is_self_event(FakeEvent(sender_id="999", self_id="999"))
    assert not _is_self_event(FakeEvent(sender_id="123", self_id="999"))


def test_self_event_detection_uses_onebot_raw_message_fallback():
    event = FakeEvent(sender_id="", self_id="", raw_message={"sender": {"user_id": 10001}, "self_id": 10001})

    assert _is_self_event(event)


def test_bridge_ack_text_is_not_forwarded_back_to_codex_app():
    assert _is_bridge_ack_text("sent to Codex App thread 019ee497 turn 019ee66c")
    assert _is_bridge_ack_text("sent to app-thread-1")
    assert not _is_bridge_ack_text("please inspect sent to Codex App thread logs")


def test_blank_bridge_input_is_ignored_before_stop_event():
    assert _should_ignore_bridge_input_text("")
    assert _should_ignore_bridge_input_text("   ")
    assert _should_ignore_bridge_input_text("sent to Codex App thread 019ee497 turn 019ee66c")
    assert not _should_ignore_bridge_input_text("nihao")


def test_bridge_ack_prefix_is_ignored_even_with_whitespace():
    assert _should_ignore_bridge_input_text("  sent to Codex App thread 019ee497 turn 019ee66c  ")
