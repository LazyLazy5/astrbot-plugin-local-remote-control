from __future__ import annotations

import asyncio

from ..common.routing import action_from_full_command_text, stopped_plain_result
from .bridge import CodexAppBridge


class CodexBridgeController:
    def __init__(self, owner, config, mode_store, delivery_queue, logger):
        self.owner = owner
        self.config = config
        self.mode_store = mode_store
        self.delivery_queue = delivery_queue
        self.logger = logger
        self.bridge = CodexAppBridge(
            owner,
            enabled=bool(config.get("enable_codex_app_bridge", True)),
            delivery_queue=delivery_queue,
        )
        self._bridge_task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.bridge.load()
        self._bridge_task = asyncio.create_task(self._bridge_poll_loop())

    async def close(self) -> None:
        if self._bridge_task and not self._bridge_task.done():
            self._bridge_task.cancel()
            try:
                await self._bridge_task
            except asyncio.CancelledError:
                pass
        await self.bridge.close()

    async def handle_command(self, event, action: str = ""):
        action = action_from_full_command_text(event.message_str or "", "codexbridge", action)
        action = (action or "").strip().lower()
        umo = event.unified_msg_origin
        if action == "on":
            await self.mode_store.enable_bridge(umo)
            yield stopped_plain_result(event, await self.bridge.enable(umo))
        elif action == "off":
            await self.mode_store.disable_bridge(umo)
            yield stopped_plain_result(event, await self.bridge.disable(umo))
        elif action == "status":
            yield stopped_plain_result(event, await self.bridge.status(umo))
        elif action == "retry":
            yield stopped_plain_result(event, await self.bridge.retry(umo))
        elif action == "queue clear":
            count = await self.delivery_queue.clear(umo, "codexbridge")
            yield stopped_plain_result(event, f"Codex Bridge queue cleared: {count}")
        elif action == "probe":
            yield stopped_plain_result(event, await self.bridge.probe())
        elif action == "ls":
            yield stopped_plain_result(event, await self.bridge.list_threads(umo))
        elif action.startswith("use "):
            target = action.split(maxsplit=1)[1].strip()
            yield stopped_plain_result(event, await self.bridge.use_thread(umo, target))
        else:
            yield stopped_plain_result(
                event,
                "用法: /codexbridge on | /codexbridge off | /codexbridge status | /codexbridge retry | /codexbridge queue clear | /codexbridge probe | /codexbridge ls | /codexbridge use <序号|id前缀>",
            )

    async def intercept_message(self, event):
        text = (event.message_str or "").strip()
        ok, message = await self.bridge.send_to_bound_thread(event.unified_msg_origin, text)
        if ok:
            event.clear_result()
            return
        if not ok and not self.bridge.should_report_send_failure(event.unified_msg_origin, message):
            return
        yield stopped_plain_result(event, message)

    async def send_to_bound_thread(self, umo: str, text: str):
        return await self.bridge.send_to_bound_thread(umo, text)

    def should_report_send_failure(self, umo: str, message: str) -> bool:
        return self.bridge.should_report_send_failure(umo, message)

    async def _bridge_poll_loop(self):
        while True:
            try:
                pending = await self.bridge.poll_once()
                for umo, text in pending:
                    await self.delivery_queue.enqueue(umo, "codexbridge", text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning("Codex Bridge poll failed: %s", exc)
            await asyncio.sleep(2)
