from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .codex_app_bridge import CodexAppBridge
from .commands import CommandDispatcher, TerminalState
from .hapi_bridge import HapiBridge
from .safe_shell import SafeShell
from .terminal_session import PersistentTerminalSession, default_shell_command


class ModeStore:
    def __init__(self, kv):
        self.kv = kv
        self.terminal_windows: set[str] = set()
        self.bridge_windows: set[str] = set()

    async def load(self):
        terminals = await self.kv.get_kv_data("terminal_windows", [])
        bridges = await self.kv.get_kv_data("codexbridge_windows", [])
        self.terminal_windows = {str(x) for x in terminals or []}
        self.bridge_windows = {str(x) for x in bridges or []}

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
        "窗口级终端模式与 Codex App Bridge",
        "0.1.0",
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
            self.hapi = HapiBridge(connector_plugin_name=str(config.get("hapi_connector_plugin_name", "astrbot_plugin_hapi_connector")))
            self.bridge = CodexAppBridge(
                self,
                enabled=bool(config.get("enable_codex_app_bridge", True)),
            )
            self.dispatcher = CommandDispatcher(
                self.shell,
                self.hapi,
                self.bridge,
                allow_git=bool(config.get("allow_git", True)),
            )
            self._bridge_task: asyncio.Task | None = None
            self._terminal_task: asyncio.Task | None = None

        async def initialize(self):
            await self.mode_store.load()
            await self.bridge.load()
            await self.hapi.init()
            self._bridge_task = asyncio.create_task(self._bridge_poll_loop())
            self._terminal_task = asyncio.create_task(self._terminal_poll_loop())
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
            for session in list(self.terminals.values()):
                await session.close()
            self.terminals.clear()
            await self.hapi.close()

        async def _bridge_poll_loop(self):
            while True:
                try:
                    for umo, text in await self.bridge.poll_once():
                        chain = MessageChain().message(text)
                        await self.context.send_message(umo, chain)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Codex Bridge poll failed: %s", exc)
                await asyncio.sleep(2)

        async def _terminal_poll_loop(self):
            while True:
                try:
                    for umo, session in list(self.terminals.items()):
                        if not self.mode_store.is_terminal(umo):
                            continue
                        output = await session.drain_output()
                        if output:
                            chain = MessageChain().message(output)
                            await self.context.send_message(umo, chain)
                        if not session.is_running:
                            self.terminals.pop(umo, None)
                            await self.mode_store.disable_terminal(umo)
                            chain = MessageChain().message("Terminal process exited")
                            await self.context.send_message(umo, chain)
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
            if not self._is_admin(event):
                yield event.plain_result("此命令仅限管理员使用")
                return
            action = (action or "").strip().lower()
            umo = event.unified_msg_origin
            if action == "on":
                await self.mode_store.enable_terminal(umo)
                session = await self._terminal_for(umo)
                banner = await session.collect_output(timeout=0.5)
                text = "Terminal mode on\n当前窗口已连接到系统终端。输入 /term off 退出。"
                if banner:
                    text += f"\n\n{banner}"
                yield event.plain_result(text)
            elif action == "off":
                await self.mode_store.disable_terminal(umo)
                await self._close_terminal(umo)
                yield event.plain_result("Terminal mode off")
            elif action == "status":
                bridge = "on" if self.mode_store.is_bridge(umo) else "off"
                term = "on" if self.mode_store.is_terminal(umo) else "off"
                cwd = self._state_for(umo).cwd
                yield event.plain_result(f"Terminal: {term}\nCodex Bridge: {bridge}\ncwd: {cwd}")
            else:
                yield event.plain_result("用法: /term on | /term off | /term status")

        @filter.command("codexbridge")
        async def cmd_codexbridge(self, event: AstrMessageEvent, action: str = ""):
            if not self._is_admin(event):
                yield event.plain_result("此命令仅限管理员使用")
                return
            async for result in self._handle_codexbridge(event, action):
                yield result

        @filter.command("codexbrideg")
        async def cmd_codexbridge_typo(self, event: AstrMessageEvent, action: str = ""):
            if not self._is_admin(event):
                yield event.plain_result("此命令仅限管理员使用")
                return
            async for result in self._handle_codexbridge(event, action):
                yield result

        async def _handle_codexbridge(self, event: AstrMessageEvent, action: str = ""):
            action = (action or "").strip().lower()
            umo = event.unified_msg_origin
            if action == "on":
                await self.mode_store.enable_bridge(umo)
                yield event.plain_result(await self.bridge.enable(umo))
            elif action == "off":
                await self.mode_store.disable_bridge(umo)
                yield event.plain_result(await self.bridge.disable(umo))
            elif action == "status":
                yield event.plain_result(await self.bridge.status(umo))
            else:
                yield event.plain_result("用法: /codexbridge on | /codexbridge off | /codexbridge status")

        @filter.event_message_type(filter.EventMessageType.ALL, priority=50)
        async def intercept_terminal(self, event: AstrMessageEvent):
            umo = event.unified_msg_origin
            if not self.mode_store.is_terminal(umo):
                return
            event.stop_event()
            if not self._is_admin(event):
                return

            text = (event.message_str or "").strip()
            command, action = _split_control_command(text)
            if command == "term" and action == "off":
                await self.mode_store.disable_terminal(umo)
                await self._close_terminal(umo)
                yield event.plain_result("Terminal mode off")
                return
            if command == "term" and action == "status":
                bridge = "on" if self.mode_store.is_bridge(umo) else "off"
                term = "on" if self.mode_store.is_terminal(umo) else "off"
                running = "running" if (umo in self.terminals and self.terminals[umo].is_running) else "stopped"
                yield event.plain_result(f"Terminal: {term} ({running})\nCodex Bridge: {bridge}")
                return
            if command == "codexbridge":
                async for result in self._handle_codexbridge(event, action):
                    yield result
                return

            session = await self._terminal_for(umo)
            await session.send_line(text)
            output = await session.collect_output(timeout=2.0)
            if output:
                yield event.plain_result(output)
