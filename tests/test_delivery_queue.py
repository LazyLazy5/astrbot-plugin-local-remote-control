import asyncio
from types import SimpleNamespace

from astrbot_plugin_local_remote_control.delivery_queue import DeliveryQueue, split_delivery_text
from astrbot_plugin_local_remote_control.main import _deliver_due_items_once, _onebot_direct_payload, _send_delivery_text


class FakeKv:
    def __init__(self):
        self.data = {}

    async def get_kv_data(self, key, default=None):
        return self.data.get(key, default)

    async def put_kv_data(self, key, value):
        self.data[key] = value


def run(coro):
    return asyncio.run(coro)


def test_delivery_queue_advances_failed_items_with_backoff():
    queue = DeliveryQueue(FakeKv(), now=lambda: 1000.0)

    run(queue.enqueue("umo", "codexbridge", "hello", dedupe_key="rollout:1"))
    due = run(queue.due_items())

    assert len(due) == 1
    assert due[0].text == "hello"

    run(queue.mark_failed(due[0].id, "ret=-2"))

    assert run(queue.due_items()) == []
    status = queue.status("umo", "codexbridge")
    assert status["queue_length"] == 1
    assert status["last_error"] == "ret=-2"
    assert status["next_retry_at"] == 1030.0
    assert status["needs_user_refresh"] is False


def test_delivery_queue_persists_and_removes_sent_items():
    kv = FakeKv()
    first = DeliveryQueue(kv, now=lambda: 1.0)
    run(first.enqueue("umo", "term", "one", dedupe_key="term:1"))

    second = DeliveryQueue(kv, now=lambda: 1.0)
    run(second.load())
    due = run(second.due_items())

    assert [item.text for item in due] == ["one"]

    run(second.mark_sent(due[0].id))

    third = DeliveryQueue(kv, now=lambda: 1.0)
    run(third.load())
    assert run(third.due_items()) == []


def test_delivery_queue_deduplicates_by_key():
    queue = DeliveryQueue(FakeKv(), now=lambda: 1.0)

    run(queue.enqueue("umo", "codexbridge", "one", dedupe_key="same"))
    run(queue.enqueue("umo", "codexbridge", "one again", dedupe_key="same"))

    assert [item.text for item in run(queue.due_items())] == ["one"]


def test_split_delivery_text_adds_part_headers_and_limits_size():
    text = "[Codex App]\n" + ("x" * 1900)

    chunks = split_delivery_text(text, limit=850)

    assert len(chunks) == 3
    assert all(len(chunk) <= 850 for chunk in chunks)
    assert chunks[0].startswith("[Codex App 1/3]\n")
    assert chunks[1].startswith("[Codex App 2/3]\n")


def test_delivery_loop_stops_same_window_after_send_failure():
    queue = DeliveryQueue(FakeKv(), now=lambda: 1.0)
    sent = []

    async def send(umo, text):
        sent.append((umo, text))
        if text == "first":
            raise RuntimeError("ret=-2")

    run(queue.enqueue("umo", "codexbridge", "first", dedupe_key="one"))
    run(queue.enqueue("umo", "codexbridge", "second", dedupe_key="two"))

    delivered, failed = run(_deliver_due_items_once(queue, send))

    assert delivered == 0
    assert failed == 1
    assert sent == [("umo", "first")]
    due = run(queue.due_items())
    assert due == []
    assert queue.status("umo")["next_retry_at"] > 1.0


def test_delivery_loop_limits_items_per_tick():
    queue = DeliveryQueue(FakeKv(), now=lambda: 1.0)
    sent = []

    async def send(umo, text):
        sent.append((umo, text))

    for index in range(5):
        run(queue.enqueue("umo", "term", f"message-{index}", dedupe_key=str(index)))

    delivered, failed = run(_deliver_due_items_once(queue, send, max_items=3))

    assert delivered == 3
    assert failed == 0
    assert len(sent) == 3
    assert len(run(queue.due_items())) == 2


def test_delivery_loop_defaults_to_one_item_per_tick():
    queue = DeliveryQueue(FakeKv(), now=lambda: 1.0)
    sent = []

    async def send(umo, text):
        sent.append((umo, text))

    run(queue.enqueue("umo", "term", "one", dedupe_key="one"))
    run(queue.enqueue("umo", "term", "two", dedupe_key="two"))

    delivered, failed = run(_deliver_due_items_once(queue, send))

    assert delivered == 1
    assert failed == 0
    assert sent == [("umo", "one")]
    assert [item.text for item in run(queue.due_items())] == ["two"]


def test_delivery_queue_enqueues_long_text_as_separate_chunks():
    queue = DeliveryQueue(FakeKv(), now=lambda: 1.0)
    text = "[Codex App]\n" + ("x" * 1900)

    run(queue.enqueue("umo", "codexbridge", text, dedupe_key="long"))

    due = run(queue.due_items())
    assert len(due) == 3
    assert all(len(item.text) <= 850 for item in due)
    assert due[0].dedupe_key == "long:1/3"


def test_delivery_queue_preserves_fifo_when_first_item_is_cooling_down():
    now = 1000.0
    queue = DeliveryQueue(FakeKv(), now=lambda: now)

    run(queue.enqueue("umo", "term", "first", dedupe_key="one"))
    first = run(queue.due_items())[0]
    run(queue.mark_failed(first.id, "ret=-2"))
    run(queue.enqueue("umo", "term", "second", dedupe_key="two"))

    assert run(queue.due_items()) == []


def test_delivery_queue_clear_removes_only_matching_window_and_channel():
    queue = DeliveryQueue(FakeKv(), now=lambda: 1.0)

    run(queue.enqueue("umo", "term", "term", dedupe_key="term"))
    run(queue.enqueue("umo", "codexbridge", "bridge", dedupe_key="bridge"))
    run(queue.enqueue("other", "term", "other", dedupe_key="other"))

    removed = run(queue.clear("umo", "term"))

    assert removed == 1
    assert [(item.umo, item.channel, item.text) for item in run(queue.due_items())] == [
        ("umo", "codexbridge", "bridge"),
        ("other", "term", "other"),
    ]


def test_weixin_ret_minus_two_pauses_window_until_user_refresh():
    now = 1000.0
    queue = DeliveryQueue(FakeKv(), now=lambda: now)
    umo = "weixin_personal_tnco:FriendMessage:user"

    run(queue.enqueue(umo, "codexbridge", "first", dedupe_key="one"))
    first = run(queue.due_items())[0]
    run(queue.mark_failed_umo(umo, "ret=-2"))
    run(queue.enqueue(umo, "codexbridge", "second", dedupe_key="two"))

    assert run(queue.due_items()) == []
    status = queue.status(umo, "codexbridge")
    assert status["needs_user_refresh"] is True
    assert "ret=-2" in status["last_error"]

    run(queue.mark_user_refreshed(umo))
    assert queue.status(umo, "codexbridge")["needs_user_refresh"] is False
    assert [item.text for item in run(queue.due_items())] == ["first"]

    now = 1001.0

    assert [item.text for item in run(queue.due_items())] == ["first"]

    now = 1002.0

    assert [item.text for item in run(queue.due_items())] == ["first", "second"]


def test_qq_send_failure_uses_backoff_without_user_refresh():
    queue = DeliveryQueue(FakeKv(), now=lambda: 1000.0)
    umo = "onebot_napcat:FriendMessage:1503663035"

    run(queue.enqueue(umo, "term", "first", dedupe_key="one"))
    run(queue.mark_failed_umo(umo, "send failed"))

    status = queue.status(umo, "term")
    assert status["needs_user_refresh"] is False
    assert status["next_retry_at"] == 1030.0


def test_onebot_direct_payload_includes_self_id_for_private_delivery():
    payload = _onebot_direct_payload(
        "onebot_napcat:FriendMessage:1503663035",
        [{"type": "text", "data": {"text": "hello"}}],
        "1904439708",
    )

    assert payload == {
        "action": "send_private_msg",
        "params": {
            "user_id": 1503663035,
            "message": [{"type": "text", "data": {"text": "hello"}}],
            "self_id": "1904439708",
        },
    }


def test_onebot_direct_payload_includes_self_id_for_group_delivery():
    payload = _onebot_direct_payload(
        "onebot_napcat:GroupMessage:123456",
        [{"type": "text", "data": {"text": "hello"}}],
        "1904439708",
    )

    assert payload == {
        "action": "send_group_msg",
        "params": {
            "group_id": 123456,
            "message": [{"type": "text", "data": {"text": "hello"}}],
            "self_id": "1904439708",
        },
    }


def test_send_delivery_text_routes_onebot_with_remembered_self_id():
    calls = []
    context_calls = []

    class FakeBot:
        async def call_action(self, action, **params):
            calls.append((action, params))

    class FakePlatform:
        bot = FakeBot()

        @staticmethod
        def meta():
            return SimpleNamespace(id="onebot_napcat")

    class FakeContext:
        platform_manager = SimpleNamespace(platform_insts=[FakePlatform()])

        async def send_message(self, umo, chain):
            context_calls.append((umo, chain))

    run(
        _send_delivery_text(
            FakeContext(),
            "onebot_napcat:FriendMessage:1503663035",
            "hello",
            "1904439708",
        )
    )

    assert calls == [
        (
            "send_private_msg",
            {
                "message": [{"type": "text", "data": {"text": "hello"}}],
                "self_id": "1904439708",
                "user_id": 1503663035,
            },
        )
    ]
    assert context_calls == []
