from __future__ import annotations

import asyncio
from typing import Any

from .codexbridge.controller import CodexBridgeController
from .common.delivery_queue import DeliveryQueue
from .common.routing import (
    ModeStore,
    action_from_full_command_text as _action_from_full_command_text,
    bridge_should_dispatch_terminal_command as _bridge_should_dispatch_terminal_command,
    command_text as _command_text,
    deliver_due_items_once as _deliver_due_items_once,
    fixed_auto_reply_text as _fixed_auto_reply_text,
    is_bridge_ack_text as _is_bridge_ack_text,
    is_self_event as _is_self_event,
    normalize_terminal_dispatch_text as _normalize_terminal_dispatch_text,
    onebot_direct_payload as _onebot_direct_payload,
    send_delivery_text as _common_send_delivery_text,
    should_dispatch_terminal_text as _should_dispatch_terminal_text,
    should_ignore_bridge_input_text as _should_ignore_bridge_input_text,
    split_control_command as _split_control_command,
    split_message_chunks as _split_message_chunks,
    stopped_plain_result as _stopped_plain_result,
)
from .term.controller import TermController


async def _send_delivery_text(context, umo: str, text: str, self_id: str = "") -> None:
    await _common_send_delivery_text(context, globals().get("MessageChain"), umo, text, self_id)


try:
    from astrbot.api import AstrBotConfig, logger
    from astrbot.api.event import AstrMessageEvent, MessageChain, filter
    from astrbot.api.star import Context, Star, register

    ASTROBOT_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only outside AstrBot
    AstrBotConfig = Any  # type: ignore
    AstrMessageEvent = Any  # type: ignore
    Context = Any  # type: ignore
    Star = object  # type: ignore
    ASTROBOT_AVAILABLE = False


if ASTROBOT_AVAILABLE:

    @register(
        "astrbot_plugin_local_remote_control",
        "local",
        "窗口级终端模式、HAPI 托管 Codex/Claude Code 与 Codex App Bridge",
        "0.3.0",
    )
    class LocalRemoteControlPlugin(Star):
        def __init__(self, context: Context, config: AstrBotConfig):
            super().__init__(context)
            self.context = context
            self.config = config
            self.mode_store = ModeStore(self)
            self.delivery_queue = DeliveryQueue(self)
            self.codexbridge = CodexBridgeController(self, config, self.mode_store, self.delivery_queue, logger)
            self.term = TermController(
                self,
                config,
                self.mode_store,
                self.delivery_queue,
                self.codexbridge,
                logger,
            )
            self._delivery_task: asyncio.Task | None = None
            self._umo_self_ids: dict[str, str] = {}

        async def initialize(self):
            await self.mode_store.load()
            await self.delivery_queue.load()
            await self.codexbridge.start()
            await self.term.start()
            self._delivery_task = asyncio.create_task(self._delivery_loop())
            logger.info("Local Remote Control initialized")

        async def terminate(self):
            if self._delivery_task and not self._delivery_task.done():
                self._delivery_task.cancel()
                try:
                    await self._delivery_task
                except asyncio.CancelledError:
                    pass
            await self.term.close()
            await self.codexbridge.close()

        async def _delivery_loop(self):
            while True:
                try:
                    async def send_text(umo: str, text: str):
                        await self._send_delivery_text(umo, text)

                    await _deliver_due_items_once(self.delivery_queue, send_text)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Delivery queue failed: %s", exc)
                await asyncio.sleep(2)

        async def _send_delivery_text(self, umo: str, text: str) -> None:
            await _send_delivery_text(self.context, umo, text, self._umo_self_ids.get(umo, ""))

        def _remember_event_route(self, event: AstrMessageEvent) -> None:
            self_id = str(event.get_self_id() or "").strip()
            if self_id:
                self._umo_self_ids[event.unified_msg_origin] = self_id

        def _is_admin(self, event: AstrMessageEvent) -> bool:
            configured = [str(x) for x in self.config.get("admin_uids", []) or []]
            if configured:
                return str(event.get_sender_id()) in configured
            astrbot_config = self.context.get_config(event.unified_msg_origin)
            admin_ids = [str(x) for x in astrbot_config.get("admins_id", [])]
            return str(event.get_sender_id()) in admin_ids

        @filter.command("term")
        async def cmd_term(self, event: AstrMessageEvent, action: str = ""):
            if _is_self_event(event):
                return
            self._remember_event_route(event)
            event.stop_event()
            if not self._is_admin(event):
                yield _stopped_plain_result(event, "此命令仅限管理员使用")
                return
            async for result in self.term.handle_term_command(event, action):
                yield result

        @filter.command("codexbridge")
        async def cmd_codexbridge(self, event: AstrMessageEvent, action: str = ""):
            if _is_self_event(event):
                return
            self._remember_event_route(event)
            event.stop_event()
            if not self._is_admin(event):
                yield _stopped_plain_result(event, "此命令仅限管理员使用")
                return
            async for result in self.codexbridge.handle_command(event, action):
                yield result

        @filter.command("cb")
        async def cmd_codexbridge_alias(self, event: AstrMessageEvent, action: str = ""):
            if _is_self_event(event):
                return
            self._remember_event_route(event)
            event.stop_event()
            if not self._is_admin(event):
                yield _stopped_plain_result(event, "此命令仅限管理员使用")
                return
            async for result in self.codexbridge.handle_command(event, action):
                yield result

        @filter.command("codexbrideg")
        async def cmd_codexbridge_typo(self, event: AstrMessageEvent, action: str = ""):
            if _is_self_event(event):
                return
            self._remember_event_route(event)
            event.stop_event()
            if not self._is_admin(event):
                yield _stopped_plain_result(event, "此命令仅限管理员使用")
                return
            async for result in self.codexbridge.handle_command(event, action):
                yield result

        @filter.event_message_type(filter.EventMessageType.ALL, priority=60)
        async def intercept_fixed_auto_reply(self, event: AstrMessageEvent):
            if _is_self_event(event):
                return
            reply = _fixed_auto_reply_text(event.message_str or "")
            if reply is None:
                return
            self._remember_event_route(event)
            event.stop_event()
            yield _stopped_plain_result(event, reply)

        @filter.event_message_type(filter.EventMessageType.ALL, priority=50)
        async def intercept_terminal(self, event: AstrMessageEvent):
            if _is_self_event(event):
                return
            self._remember_event_route(event)
            umo = event.unified_msg_origin
            await self._maybe_mark_user_refreshed(umo)
            if not self.mode_store.is_terminal(umo):
                return
            event.stop_event()
            if not self._is_admin(event):
                return
            text = (event.message_str or "").strip()
            command, action = _split_control_command(text)
            if command == "codexbridge":
                async for result in self.codexbridge.handle_command(event, action):
                    yield result
                return
            async for result in self.term.intercept_message(event):
                yield result

        @filter.event_message_type(filter.EventMessageType.ALL, priority=40)
        async def intercept_bridge(self, event: AstrMessageEvent):
            if _is_self_event(event):
                return
            self._remember_event_route(event)
            umo = event.unified_msg_origin
            await self._maybe_mark_user_refreshed(umo)
            if self.mode_store.is_terminal(umo) or not self.mode_store.is_bridge(umo):
                return
            text = (event.message_str or "").strip()
            if _should_ignore_bridge_input_text(text):
                return
            event.stop_event()
            if not self._is_admin(event):
                event.clear_result()
                return

            command, action = _split_control_command(text)
            if command == "codexbridge":
                async for result in self.codexbridge.handle_command(event, action):
                    yield result
                return
            if command == "term":
                async for result in self.term.handle_term_command(event, action):
                    yield result
                return
            async for result in self.codexbridge.intercept_message(event):
                yield result

        async def _maybe_mark_user_refreshed(self, umo: str) -> None:
            if str(umo).startswith("weixin_"):
                await self.delivery_queue.mark_user_refreshed(umo)
