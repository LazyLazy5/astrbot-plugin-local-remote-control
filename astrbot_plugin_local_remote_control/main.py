from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .codex_app_bridge import CodexAppBridge
from .commands import CommandDispatcher, TerminalState
from .delivery_queue import DeliveryQueue
from .hapi_client import HapiClient, HapiConfig, extract_hapi_message_text, message_seq
from .platform_strategy import format_platform_status
from .safe_shell import SafeShell
from .terminal_session import PersistentTerminalSession, default_shell_command

DELIVERY_MAX_ITEMS_PER_TICK = 1


class ModeStore:
    def __init__(self, kv):
        self.kv = kv
        self.terminal_windows: set[str] = set()
        self.bridge_windows: set[str] = set()
        self.term_state_windows: set[str] = set()

    async def load(self):
        terminals = await self.kv.get_kv_data("terminal_windows", [])
        bridges = await self.kv.get_kv_data("codexbridge_windows", [])
        term_states = await self.kv.get_kv_data("term_state_windows", [])
        self.terminal_windows = {str(x) for x in terminals or []}
        self.bridge_windows = {str(x) for x in bridges or []}
        self.term_state_windows = {str(x) for x in term_states or []}

    def is_terminal(self, umo: str) -> bool:
        return umo in self.terminal_windows

    def is_bridge(self, umo: str) -> bool:
        return umo in self.bridge_windows

    async def enable_terminal(self, umo: str):
        self.terminal_windows.add(umo)
        await self.kv.put_kv_data("terminal_windows", sorted(self.terminal_windows))

    async def disable_terminal(self, umo: str):
        self.terminal_windows.discard(umo)
        await self.kv.put_kv_data("terminal_windows", sorted(self.terminal_windows))

    async def enable_bridge(self, umo: str):
        self.bridge_windows.add(umo)
        await self.kv.put_kv_data("codexbridge_windows", sorted(self.bridge_windows))

    async def disable_bridge(self, umo: str):
        self.bridge_windows.discard(umo)
        await self.kv.put_kv_data("codexbridge_windows", sorted(self.bridge_windows))

    async def remember_term_state_window(self, umo: str):
        self.term_state_windows.add(umo)
        await self.kv.put_kv_data("term_state_windows", sorted(self.term_state_windows))


def _split_control_command(text: str) -> tuple[str, str]:
    parts = (text or "").strip().split(maxsplit=1)
    if not parts:
        return "", ""
    command = parts[0].lstrip("/").lower()
    action = parts[1].strip().lower() if len(parts) > 1 else ""
    if command == "codex" and action.startswith("bridge "):
        return "codexbridge", action.removeprefix("bridge ").strip()
    if command == "codex" and action.startswith("brideg "):
        return "codexbridge", action.removeprefix("brideg ").strip()
    if command == "codexbrideg":
        command = "codexbridge"
    return command, action


def _stopped_plain_result(event, text: str):
    return event.plain_result(text).stop_event()


def _split_message_chunks(text: str, *, limit: int = 1200) -> list[str]:
    if len(text) <= limit:
        return [text]
    return [text[i:i + limit].rstrip() for i in range(0, len(text), limit)]


def _should_dispatch_terminal_text(text: str, state: TerminalState) -> bool:
    raw = (text or "").strip()
    if not raw:
        return True
    return True


def _normalize_terminal_dispatch_text(text: str, state: TerminalState) -> str:
    raw = (text or "").strip()
    if not raw or raw.startswith("/"):
        return text
    command, action = _split_control_command(raw)
    if command in ("pwd", "dir", "cd"):
        return _command_text(command, action)
    if command == "git" and action == "status":
        return "/git status"
    return text


def _bridge_should_dispatch_terminal_command(text: str) -> bool:
    command, action = _split_control_command(text)
    if command != "term":
        return False
    return action.startswith(("agent ", "ls", "use ", "stop", "send ", "shell", "hapi "))


def _command_text(command: str, action: str = "") -> str:
    action = (action or "").strip()
    return f"/{command} {action}".strip()


def _action_from_full_command_text(text: str, command: str, fallback: str = "") -> str:
    parsed_command, parsed_action = _split_control_command(text)
    if parsed_command == command:
        return parsed_action
    return (fallback or "").strip().lower()


def _is_bridge_ack_text(text: str) -> bool:
    normalized = (text or "").strip()
    return normalized.startswith("sent to Codex App thread ") or normalized.startswith("sent to app-thread-")


def _should_ignore_bridge_input_text(text: str) -> bool:
    normalized = (text or "").strip()
    return not normalized or _is_bridge_ack_text(normalized)


def _is_self_event(event) -> bool:
    try:
        sender_id = str(event.get_sender_id() or "").strip()
    except Exception:
        sender_id = ""
    try:
        self_id = str(event.get_self_id() or "").strip()
    except Exception:
        self_id = ""
    raw_message = getattr(getattr(event, "message_obj", None), "raw_message", {}) or {}
    if isinstance(raw_message, dict):
        if not self_id:
            self_id = str(raw_message.get("self_id", "") or "").strip()
        if not sender_id:
            sender = raw_message.get("sender") or {}
            if isinstance(sender, dict):
                sender_id = str(sender.get("user_id", "") or "").strip()
            if not sender_id:
                sender_id = str(raw_message.get("user_id", "") or "").strip()
        if str(raw_message.get("message_type", "")).lower() == "self":
            return True
    return bool(sender_id and self_id and sender_id == self_id)


def _onebot_direct_payload(umo: str, messages: list[dict], self_id: str = "") -> dict | None:
    parts = str(umo or "").split(":", 2)
    if len(parts) < 3:
        return None
    platform, message_type, session_id = parts
    if not (platform.startswith("onebot") or "napcat" in platform.lower()):
        return None
    if not session_id.isdigit():
        return None
    params: dict[str, Any] = {"message": messages}
    if self_id:
        params["self_id"] = str(self_id)
    if message_type == "FriendMessage":
        params["user_id"] = int(session_id)
        return {"action": "send_private_msg", "params": params}
    if message_type == "GroupMessage":
        params["group_id"] = int(session_id)
        return {"action": "send_group_msg", "params": params}
    return None


async def _deliver_due_items_once(delivery_queue, send_text, *, max_items: int = DELIVERY_MAX_ITEMS_PER_TICK) -> tuple[int, int]:
    due = await delivery_queue.due_items()
    failed_umos: set[str] = set()
    delivered = 0
    failed = 0
    processed = 0
    for item in due:
        if processed >= max_items:
            break
        if item.umo in failed_umos:
            continue
        try:
            await send_text(item.umo, item.text)
            await delivery_queue.mark_sent(item.id)
            delivered += 1
        except Exception as exc:
            failed += 1
            failed_umos.add(item.umo)
            await delivery_queue.mark_failed_umo(item.umo, str(exc))
        processed += 1
    return delivered, failed


async def _send_delivery_text(context, umo: str, text: str, self_id: str = "") -> None:
    payload = _onebot_direct_payload(umo, [{"type": "text", "data": {"text": text}}], self_id)
    if payload is not None:
        platform_name = str(umo).split(":", 1)[0]
        for platform in context.platform_manager.platform_insts:
            if platform.meta().id == platform_name:
                await platform.bot.call_action(payload["action"], **payload["params"])
                return
    chain = MessageChain().message(text)
    await context.send_message(umo, chain)


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
            raw_work_dir = str(config.get("work_dir", "") or "").strip()
            if raw_work_dir:
                work_dir = Path(raw_work_dir).expanduser().resolve()
            else:
                work_dir = Path(__file__).resolve().parent / "workspace"
            self.shell = SafeShell(work_dir)
            self.mode_store = ModeStore(self)
            self.terminal_states: dict[str, TerminalState] = {}
            self.terminals: dict[str, PersistentTerminalSession] = {}
            self.delivery_queue = DeliveryQueue(self)
            self.hapi = HapiClient(
                HapiConfig(
                    endpoint=str(config.get("hapi_endpoint", "") or "").strip(),
                    access_token=str(config.get("access_token", "") or "").strip(),
                    proxy_url=str(config.get("proxy_url", "") or "").strip(),
                    jwt_lifetime=int(config.get("jwt_lifetime", 900) or 900),
                    refresh_before=int(config.get("refresh_before_expiry", 180) or 180),
                )
            )
            self.hapi.reload_config = self._reload_hapi_config  # type: ignore[attr-defined]
            self.bridge = CodexAppBridge(
                self,
                enabled=bool(config.get("enable_codex_app_bridge", True)),
                delivery_queue=self.delivery_queue,
            )
            self.dispatcher = CommandDispatcher(
                self.shell,
                self.hapi,
                self.bridge,
                allow_git=bool(config.get("allow_git", True)),
            )
            self._bridge_task: asyncio.Task | None = None
            self._terminal_task: asyncio.Task | None = None
            self._delivery_task: asyncio.Task | None = None
            self._hapi_task: asyncio.Task | None = None
            self._umo_self_ids: dict[str, str] = {}

        async def initialize(self):
            await self.mode_store.load()
            await self.delivery_queue.load()
            await self.bridge.load()
            for umo in sorted(self.mode_store.term_state_windows | self.mode_store.terminal_windows):
                await self._load_state_for(umo)
            self._bridge_task = asyncio.create_task(self._bridge_poll_loop())
            self._terminal_task = asyncio.create_task(self._terminal_poll_loop())
            self._delivery_task = asyncio.create_task(self._delivery_loop())
            self._hapi_task = asyncio.create_task(self._hapi_poll_loop())
            logger.info("Local Remote Control initialized")

        async def terminate(self):
            if self._bridge_task and not self._bridge_task.done():
                self._bridge_task.cancel()
                try:
                    await self._bridge_task
                except asyncio.CancelledError:
                    pass
            if self._terminal_task and not self._terminal_task.done():
                self._terminal_task.cancel()
                try:
                    await self._terminal_task
                except asyncio.CancelledError:
                    pass
            if self._delivery_task and not self._delivery_task.done():
                self._delivery_task.cancel()
                try:
                    await self._delivery_task
                except asyncio.CancelledError:
                    pass
            if self._hapi_task and not self._hapi_task.done():
                self._hapi_task.cancel()
                try:
                    await self._hapi_task
                except asyncio.CancelledError:
                    pass
            for session in list(self.terminals.values()):
                await session.close()
            self.terminals.clear()
            await self.bridge.close()
            await self.hapi.close()

        async def _bridge_poll_loop(self):
            while True:
                try:
                    pending = await self.bridge.poll_once()
                    for umo, text in pending:
                        await self.delivery_queue.enqueue(umo, "codexbridge", text)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Codex Bridge poll failed: %s", exc)
                await asyncio.sleep(2)

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

        async def _hapi_poll_loop(self):
            while True:
                try:
                    for umo, state in list(self.terminal_states.items()):
                        if not state.current_session_id:
                            continue
                        sid = state.current_session_id
                        messages = await self.hapi.fetch_messages(sid, limit=50)
                        if not messages:
                            continue
                        last_seq_key = f"term_hapi_seq_{umo}_{sid}"
                        old_seq = int(await self.get_kv_data(last_seq_key, 0) or 0)
                        new_seq = old_seq
                        for message in sorted(messages, key=message_seq):
                            seq = message_seq(message)
                            if seq <= old_seq:
                                continue
                            new_seq = max(new_seq, seq)
                            role, text = extract_hapi_message_text(message)
                            if not text or role == "user":
                                continue
                            label = state.current_flavor or "hapi"
                            await self.delivery_queue.enqueue(
                                umo,
                                "term",
                                f"[HAPI/{label}]\n{text}",
                                dedupe_key=f"hapi:{sid}:{seq}",
                            )
                        if new_seq > old_seq:
                            await self.put_kv_data(last_seq_key, new_seq)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("HAPI poll failed: %s", exc)
                await asyncio.sleep(3)

        async def _terminal_poll_loop(self):
            while True:
                try:
                    for umo, session in list(self.terminals.items()):
                        if not self.mode_store.is_terminal(umo):
                            continue
                        output = await session.drain_output()
                        if output:
                            await self.delivery_queue.enqueue(umo, "term", output)
                        if not session.is_running:
                            self.terminals.pop(umo, None)
                            await self.mode_store.disable_terminal(umo)
                            await self.delivery_queue.enqueue(umo, "term", "Terminal process exited")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Terminal poll failed: %s", exc)
                await asyncio.sleep(0.5)

        def _is_admin(self, event: AstrMessageEvent) -> bool:
            configured = [str(x) for x in self.config.get("admin_uids", []) or []]
            if configured:
                return str(event.get_sender_id()) in configured
            astrbot_config = self.context.get_config(event.unified_msg_origin)
            admin_ids = [str(x) for x in astrbot_config.get("admins_id", [])]
            return str(event.get_sender_id()) in admin_ids

        def _state_for(self, umo: str) -> TerminalState:
            state = self.terminal_states.get(umo)
            if state is None:
                state = TerminalState(cwd=self.shell.root)
                self.terminal_states[umo] = state
            return state

        async def _load_state_for(self, umo: str) -> TerminalState:
            state = self._state_for(umo)
            raw = await self.get_kv_data(f"term_state_{umo}", {})
            if isinstance(raw, dict):
                state.backend = str(raw.get("backend") or state.backend)
                state.current_session_id = str(raw.get("current_session_id") or "") or None
                state.current_flavor = str(raw.get("current_flavor") or "") or None
                cwd = raw.get("cwd")
                if cwd:
                    try:
                        state.cwd = self.shell.resolve_inside(self.shell.root, str(cwd))
                    except ValueError:
                        state.cwd = self.shell.root
            return state

        async def _persist_state(self, umo: str) -> None:
            state = self._state_for(umo)
            await self.mode_store.remember_term_state_window(umo)
            await self.put_kv_data(
                f"term_state_{umo}",
                {
                    "backend": state.backend,
                    "cwd": str(state.cwd),
                    "current_session_id": state.current_session_id,
                    "current_flavor": state.current_flavor,
                },
            )

        async def _reload_hapi_config(self) -> tuple[bool, str]:
            try:
                path = Path.home() / ".astrbot" / "data" / "config" / "astrbot_plugin_local_remote_control_config.json"
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                return False, f"HAPI config reload failed: {exc}"
            config = HapiConfig(
                endpoint=str(data.get("hapi_endpoint", "") or "").strip(),
                access_token=str(data.get("access_token", "") or "").strip(),
                proxy_url=str(data.get("proxy_url", "") or "").strip(),
                jwt_lifetime=int(data.get("jwt_lifetime", 900) or 900),
                refresh_before=int(data.get("refresh_before_expiry", 180) or 180),
            )
            self.hapi.update_config(config)
            if not self.hapi.configured:
                return False, "HAPI config reloaded but hapi_endpoint/access_token is still empty"
            return True, "HAPI config reloaded"

        async def _terminal_for(self, umo: str) -> PersistentTerminalSession:
            session = self.terminals.get(umo)
            if session and session.is_running:
                return session
            state = self._state_for(umo)
            session = PersistentTerminalSession(default_shell_command(), cwd=state.cwd)
            await session.start()
            self.terminals[umo] = session
            return session

        async def _close_terminal(self, umo: str):
            session = self.terminals.pop(umo, None)
            if session:
                await session.close()

        @filter.command("term")
        async def cmd_term(self, event: AstrMessageEvent, action: str = ""):
            if _is_self_event(event):
                return
            self._remember_event_route(event)
            event.stop_event()
            if not self._is_admin(event):
                yield _stopped_plain_result(event, "此命令仅限管理员使用")
                return
            action = _action_from_full_command_text(event.message_str or "", "term", action)
            umo = event.unified_msg_origin
            await self._load_state_for(umo)
            if action == "on":
                await self.mode_store.enable_terminal(umo)
                state = self._state_for(umo)
                state.backend = "shell"
                state.current_session_id = None
                state.current_flavor = None
                await self._persist_state(umo)
                session = await self._terminal_for(umo)
                banner = await session.collect_output(timeout=0.5)
                text = "Terminal mode on\n当前窗口已连接到系统终端。输入 /term off 退出。"
                if banner:
                    text += f"\n\n{banner}"
                yield _stopped_plain_result(event, text)
            elif action == "off":
                await self.mode_store.disable_terminal(umo)
                await self._close_terminal(umo)
                await self._persist_state(umo)
                yield _stopped_plain_result(event, "Terminal mode off")
            elif action == "status":
                bridge = "on" if self.mode_store.is_bridge(umo) else "off"
                term = "on" if self.mode_store.is_terminal(umo) else "off"
                state = self._state_for(umo)
                q = self.delivery_queue.status(umo, "term")
                text = (
                    f"Terminal: {term}\n"
                    f"backend: {state.backend}\n"
                    f"Codex Bridge: {bridge}\n"
                    f"{format_platform_status(umo)}\n"
                    f"cwd: {state.cwd}\n"
                    f"session: {state.current_session_id or '-'}\n"
                    f"queue: {q['queue_length']}"
                )
                if q["last_error"]:
                    text += f"\nlast_error: {q['last_error']}"
                if q["next_retry_at"]:
                    text += f"\nnext_retry_at: {q['next_retry_at']:.0f}"
                if q.get("needs_user_refresh"):
                    text += "\nneeds_user_refresh: yes"
                yield _stopped_plain_result(event, text)
            elif action == "retry":
                count = await self.delivery_queue.retry(umo, "term")
                yield _stopped_plain_result(event, f"Terminal retry queued: {count}")
            elif action == "queue clear":
                count = await self.delivery_queue.clear(umo, "term")
                yield _stopped_plain_result(event, f"Terminal queue cleared: {count}")
            elif action.startswith(("agent ", "ls", "use ", "stop", "send ", "shell", "hapi ")):
                state = self._state_for(umo)
                result = await self.dispatcher.dispatch(umo, _command_text("term", action), state)
                if state.backend == "hapi":
                    await self.mode_store.enable_terminal(umo)
                await self._persist_state(umo)
                yield _stopped_plain_result(event, result.text)
            else:
                yield _stopped_plain_result(
                    event,
                    "用法: /term on | /term off | /term status | /term retry | /term queue clear | /term agent codex|cc [路径] | /term send <内容>",
                )

        @filter.command("codexbridge")
        async def cmd_codexbridge(self, event: AstrMessageEvent, action: str = ""):
            if _is_self_event(event):
                return
            self._remember_event_route(event)
            event.stop_event()
            if not self._is_admin(event):
                yield _stopped_plain_result(event, "此命令仅限管理员使用")
                return
            async for result in self._handle_codexbridge(event, action):
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
            async for result in self._handle_codexbridge(event, action):
                yield result

        async def _handle_codexbridge(self, event: AstrMessageEvent, action: str = ""):
            action = _action_from_full_command_text(event.message_str or "", "codexbridge", action)
            action = (action or "").strip().lower()
            umo = event.unified_msg_origin
            if action == "on":
                await self.mode_store.enable_bridge(umo)
                yield _stopped_plain_result(event, await self.bridge.enable(umo))
            elif action == "off":
                await self.mode_store.disable_bridge(umo)
                yield _stopped_plain_result(event, await self.bridge.disable(umo))
            elif action == "status":
                yield _stopped_plain_result(event, await self.bridge.status(umo))
            elif action == "retry":
                yield _stopped_plain_result(event, await self.bridge.retry(umo))
            elif action == "queue clear":
                count = await self.delivery_queue.clear(umo, "codexbridge")
                yield _stopped_plain_result(event, f"Codex Bridge queue cleared: {count}")
            elif action == "probe":
                yield _stopped_plain_result(event, await self.bridge.probe())
            elif action == "ls":
                yield _stopped_plain_result(event, await self.bridge.list_threads(umo))
            elif action.startswith("use "):
                target = action.split(maxsplit=1)[1].strip()
                yield _stopped_plain_result(event, await self.bridge.use_thread(umo, target))
            else:
                yield _stopped_plain_result(event, "用法: /codexbridge on | /codexbridge off | /codexbridge status | /codexbridge retry | /codexbridge queue clear | /codexbridge probe | /codexbridge ls | /codexbridge use <序号|id前缀>")

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
            await self._load_state_for(umo)

            text = (event.message_str or "").strip()
            command, action = _split_control_command(text)
            if command == "term" and action == "on":
                state = self._state_for(umo)
                state.backend = "shell"
                state.current_session_id = None
                state.current_flavor = None
                await self._persist_state(umo)
                session = await self._terminal_for(umo)
                banner = await session.collect_output(timeout=0.5)
                result = "Terminal mode on\n当前窗口已连接到系统终端。输入 /term off 退出。"
                if banner:
                    result += f"\n\n{banner}"
                yield _stopped_plain_result(event, result)
                return
            if command == "term" and action == "off":
                await self.mode_store.disable_terminal(umo)
                await self._close_terminal(umo)
                yield _stopped_plain_result(event, "Terminal mode off")
                return
            if command == "term" and action == "status":
                bridge = "on" if self.mode_store.is_bridge(umo) else "off"
                term = "on" if self.mode_store.is_terminal(umo) else "off"
                running = "running" if (umo in self.terminals and self.terminals[umo].is_running) else "stopped"
                state = self._state_for(umo)
                yield _stopped_plain_result(
                    event,
                    f"Terminal: {term} ({running})\n"
                    f"backend: {state.backend}\n"
                    f"Codex Bridge: {bridge}\n"
                    f"{format_platform_status(umo)}\n"
                    f"session: {state.current_session_id or '-'}",
                )
                return
            if command == "term" and action == "retry":
                count = await self.delivery_queue.retry(umo, "term")
                yield _stopped_plain_result(event, f"Terminal retry queued: {count}")
                return
            if command == "term" and action == "queue clear":
                count = await self.delivery_queue.clear(umo, "term")
                yield _stopped_plain_result(event, f"Terminal queue cleared: {count}")
                return
            if command == "codexbridge":
                async for result in self._handle_codexbridge(event, action):
                    yield result
                return
            if command in ("codex", "claude") and not action:
                yield _stopped_plain_result(
                    event,
                    f"当前微信终端是管道模式，不是 TTY，裸 {command} 交互界面无法正常启动。\n"
                    f"可用: {command} --version\n"
                    "建议: 使用 /term agent codex|cc 通过 HAPI 托管 CLI，或使用 /codexbridge on 接收 Codex App 推送。"
                )
                return

            state = self._state_for(umo)
            if _should_dispatch_terminal_text(text, state):
                dispatch_text = _command_text(command, action) if command == "term" else text
                dispatch_text = _normalize_terminal_dispatch_text(dispatch_text, state)
                result = await self.dispatcher.dispatch(umo, dispatch_text, state)
                await self._persist_state(umo)
                yield _stopped_plain_result(event, result.text)
                return
            session = await self._terminal_for(umo)
            await session.send_line(text)
            output = await session.collect_output(timeout=2.0)
            if output:
                yield _stopped_plain_result(event, output)

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
                async for result in self._handle_codexbridge(event, action):
                    yield result
                return
            if command == "term":
                if action == "on":
                    await self.mode_store.enable_terminal(umo)
                    state = self._state_for(umo)
                    state.backend = "shell"
                    state.current_session_id = None
                    state.current_flavor = None
                    await self._persist_state(umo)
                    session = await self._terminal_for(umo)
                    banner = await session.collect_output(timeout=0.5)
                    result = "Terminal mode on\n当前窗口已连接到系统终端。输入 /term off 退出。"
                    if banner:
                        result += f"\n\n{banner}"
                    yield _stopped_plain_result(event, result)
                    return
                if action == "off":
                    await self.mode_store.disable_terminal(umo)
                    await self._close_terminal(umo)
                    await self._persist_state(umo)
                    yield _stopped_plain_result(event, "Terminal mode off")
                    return
                if action == "status":
                    state = self._state_for(umo)
                    q = self.delivery_queue.status(umo, "term")
                    yield _stopped_plain_result(
                        event,
                        "Terminal: off\n"
                        f"backend: {state.backend}\n"
                        "Codex Bridge: on\n"
                        f"{format_platform_status(umo)}\n"
                        f"cwd: {state.cwd}\n"
                        f"session: {state.current_session_id or '-'}\n"
                        f"queue: {q['queue_length']}",
                    )
                    return
                if action == "retry":
                    count = await self.delivery_queue.retry(umo, "term")
                    yield _stopped_plain_result(event, f"Terminal retry queued: {count}")
                    return
                if action == "queue clear":
                    count = await self.delivery_queue.clear(umo, "term")
                    yield _stopped_plain_result(event, f"Terminal queue cleared: {count}")
                    return
                state = self._state_for(umo)
                result = await self.dispatcher.dispatch(umo, _command_text(command, action), state)
                if state.backend == "hapi":
                    await self.mode_store.enable_terminal(umo)
                await self._persist_state(umo)
                yield _stopped_plain_result(event, result.text)
                return
            ok, message = await self.bridge.send_to_bound_thread(umo, text)
            if ok:
                event.clear_result()
                return
            if not ok and not self.bridge.should_report_send_failure(umo, message):
                return
            yield _stopped_plain_result(event, message)

        async def _maybe_mark_user_refreshed(self, umo: str) -> None:
            if str(umo).startswith("weixin_"):
                await self.delivery_queue.mark_user_refreshed(umo)
