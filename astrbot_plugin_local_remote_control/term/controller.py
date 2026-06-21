from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ..common.platform_strategy import format_platform_status
from ..common.routing import (
    action_from_full_command_text,
    command_text,
    normalize_terminal_dispatch_text,
    should_dispatch_terminal_text,
    split_control_command,
    stopped_plain_result,
)
from .commands import CommandDispatcher, TerminalState
from .hapi_client import HapiClient, HapiConfig, extract_hapi_message_text, message_seq
from .safe_shell import SafeShell
from .terminal_session import PersistentTerminalSession, default_shell_command


class TermController:
    def __init__(self, owner, config, mode_store, delivery_queue, bridge, logger):
        self.owner = owner
        self.config = config
        self.mode_store = mode_store
        self.delivery_queue = delivery_queue
        self.logger = logger
        raw_work_dir = str(config.get("work_dir", "") or "").strip()
        if raw_work_dir:
            work_dir = Path(raw_work_dir).expanduser().resolve()
        else:
            work_dir = Path(__file__).resolve().parent.parent / "workspace"
        self.shell = SafeShell(work_dir)
        self.terminal_states: dict[str, TerminalState] = {}
        self.terminals: dict[str, PersistentTerminalSession] = {}
        self.hapi = HapiClient(
            HapiConfig(
                endpoint=str(config.get("hapi_endpoint", "") or "").strip(),
                access_token=str(config.get("access_token", "") or "").strip(),
                proxy_url=str(config.get("proxy_url", "") or "").strip(),
                jwt_lifetime=int(config.get("jwt_lifetime", 900) or 900),
                refresh_before=int(config.get("refresh_before_expiry", 180) or 180),
            )
        )
        self.hapi.reload_config = self.reload_hapi_config  # type: ignore[attr-defined]
        self.dispatcher = CommandDispatcher(
            self.shell,
            self.hapi,
            bridge,
            allow_git=bool(config.get("allow_git", True)),
        )
        self._terminal_task: asyncio.Task | None = None
        self._hapi_task: asyncio.Task | None = None

    async def start(self) -> None:
        for umo in sorted(self.mode_store.term_state_windows | self.mode_store.terminal_windows):
            await self.load_state_for(umo)
        self._terminal_task = asyncio.create_task(self._terminal_poll_loop())
        self._hapi_task = asyncio.create_task(self._hapi_poll_loop())

    async def close(self) -> None:
        for task in (self._terminal_task, self._hapi_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        for session in list(self.terminals.values()):
            await session.close()
        self.terminals.clear()
        await self.hapi.close()

    async def handle_term_command(self, event, action: str = ""):
        action = action_from_full_command_text(event.message_str or "", "term", action)
        umo = event.unified_msg_origin
        await self.load_state_for(umo)
        async for result in self._handle_action(event, umo, action, include_running=False):
            yield result

    async def intercept_message(self, event):
        umo = event.unified_msg_origin
        await self.load_state_for(umo)
        text = (event.message_str or "").strip()
        command, action = split_control_command(text)
        if command == "term":
            async for result in self._handle_action(event, umo, action, include_running=True):
                yield result
            return
        if command in ("codex", "claude") and not action:
            yield stopped_plain_result(
                event,
                f"当前微信终端是管道模式，不是 TTY，裸 {command} 交互界面无法正常启动。\n"
                f"可用: {command} --version\n"
                "建议: 使用 /term agent codex|cc 通过 HAPI 托管 CLI，或使用 /codexbridge on 接收 Codex App 推送。",
            )
            return

        state = self.state_for(umo)
        if should_dispatch_terminal_text(text, state):
            dispatch_text = command_text(command, action) if command == "term" else text
            dispatch_text = normalize_terminal_dispatch_text(dispatch_text, state)
            result = await self.dispatcher.dispatch(umo, dispatch_text, state)
            await self.persist_state(umo)
            yield stopped_plain_result(event, result.text)
            return
        session = await self.terminal_for(umo)
        await session.send_line(text)
        output = await session.collect_output(timeout=2.0)
        if output:
            yield stopped_plain_result(event, output)

    async def _handle_action(self, event, umo: str, action: str, *, include_running: bool):
        action = (action or "").strip().lower()
        if action == "on":
            text = await self.enable_terminal(umo)
            yield stopped_plain_result(event, text)
        elif action == "off":
            await self.mode_store.disable_terminal(umo)
            await self.close_terminal(umo)
            await self.persist_state(umo)
            yield stopped_plain_result(event, "Terminal mode off")
        elif action == "status":
            yield stopped_plain_result(event, self.status_text(umo, include_running=include_running))
        elif action == "retry":
            count = await self.delivery_queue.retry(umo, "term")
            yield stopped_plain_result(event, f"Terminal retry queued: {count}")
        elif action == "queue clear":
            count = await self.delivery_queue.clear(umo, "term")
            yield stopped_plain_result(event, f"Terminal queue cleared: {count}")
        elif action.startswith(("agent ", "ls", "use ", "stop", "send ", "shell", "hapi ")):
            state = self.state_for(umo)
            result = await self.dispatcher.dispatch(umo, command_text("term", action), state)
            if state.backend == "hapi":
                await self.mode_store.enable_terminal(umo)
            await self.persist_state(umo)
            yield stopped_plain_result(event, result.text)
        else:
            yield stopped_plain_result(
                event,
                "用法: /term on | /term off | /term status | /term retry | /term queue clear | /term agent codex|cc [路径] | /term send <内容>",
            )

    async def enable_terminal(self, umo: str) -> str:
        await self.mode_store.enable_terminal(umo)
        state = self.state_for(umo)
        state.backend = "shell"
        state.current_session_id = None
        state.current_flavor = None
        await self.persist_state(umo)
        session = await self.terminal_for(umo)
        banner = await session.collect_output(timeout=0.5)
        text = "Terminal mode on\n当前窗口已连接到系统终端。输入 /term off 退出。"
        if banner:
            text += f"\n\n{banner}"
        return text

    def status_text(self, umo: str, *, include_running: bool = False) -> str:
        bridge = "on" if self.mode_store.is_bridge(umo) else "off"
        term = "on" if self.mode_store.is_terminal(umo) else "off"
        state = self.state_for(umo)
        if include_running:
            running = "running" if (umo in self.terminals and self.terminals[umo].is_running) else "stopped"
            return (
                f"Terminal: {term} ({running})\n"
                f"backend: {state.backend}\n"
                f"Codex Bridge: {bridge}\n"
                f"{format_platform_status(umo)}\n"
                f"session: {state.current_session_id or '-'}"
            )
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
        return text

    def state_for(self, umo: str) -> TerminalState:
        state = self.terminal_states.get(umo)
        if state is None:
            state = TerminalState(cwd=self.shell.root)
            self.terminal_states[umo] = state
        return state

    async def load_state_for(self, umo: str) -> TerminalState:
        state = self.state_for(umo)
        raw = await self.owner.get_kv_data(f"term_state_{umo}", {})
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

    async def persist_state(self, umo: str) -> None:
        state = self.state_for(umo)
        await self.mode_store.remember_term_state_window(umo)
        await self.owner.put_kv_data(
            f"term_state_{umo}",
            {
                "backend": state.backend,
                "cwd": str(state.cwd),
                "current_session_id": state.current_session_id,
                "current_flavor": state.current_flavor,
            },
        )

    async def reload_hapi_config(self) -> tuple[bool, str]:
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

    async def terminal_for(self, umo: str) -> PersistentTerminalSession:
        session = self.terminals.get(umo)
        if session and session.is_running:
            return session
        state = self.state_for(umo)
        session = PersistentTerminalSession(default_shell_command(), cwd=state.cwd)
        await session.start()
        self.terminals[umo] = session
        return session

    async def close_terminal(self, umo: str):
        session = self.terminals.pop(umo, None)
        if session:
            await session.close()

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
                    old_seq = int(await self.owner.get_kv_data(last_seq_key, 0) or 0)
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
                        await self.owner.put_kv_data(last_seq_key, new_seq)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning("HAPI poll failed: %s", exc)
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
                self.logger.warning("Terminal poll failed: %s", exc)
            await asyncio.sleep(0.5)
