import asyncio

from astrbot_plugin_local_remote_control.main import ModeStore


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
