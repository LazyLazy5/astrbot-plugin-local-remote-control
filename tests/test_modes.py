import asyncio

from astrbot_plugin_local_remote_control.main import ModeStore, _split_control_command


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


def test_control_commands_accept_slashless_text_inside_terminal_mode():
    assert _split_control_command("/term off") == ("term", "off")
    assert _split_control_command("term off") == ("term", "off")
    assert _split_control_command("/codexbridge on") == ("codexbridge", "on")
    assert _split_control_command("codexbridge off") == ("codexbridge", "off")


def test_control_commands_accept_codexbridge_typo_alias():
    assert _split_control_command("/codexbrideg on") == ("codexbridge", "on")
    assert _split_control_command("codex brideg off") == ("codexbridge", "off")


def test_bare_codex_is_recognized_as_control_hint_inside_terminal_mode():
    assert _split_control_command("codex") == ("codex", "")
    assert _split_control_command("/codex") == ("codex", "")
    assert _split_control_command("codex --version") == ("codex", "--version")
