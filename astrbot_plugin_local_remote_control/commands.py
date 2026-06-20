from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .safe_shell import SafeShell


@dataclass
class TerminalState:
    cwd: Path
    current_session_id: str | None = None
    current_flavor: str | None = None
    last_sessions: list[dict] | None = None


@dataclass
class DispatchResult:
    text: str
    handled: bool = True


class HapiLike(Protocol):
    async def status(self) -> tuple[bool, str]:
        ...

    async def list_sessions(self, flavor: str | None = None) -> tuple[bool, str | list[dict]]:
        ...

    async def spawn_session(self, flavor: str, directory: Path) -> tuple[bool, str, str | None]:
        ...

    async def send_message(self, session_id: str, text: str) -> tuple[bool, str]:
        ...


class BridgeLike(Protocol):
    async def status(self, umo: str) -> str:
        ...

    async def send_to_bound_thread(self, umo: str, text: str) -> tuple[bool, str]:
        ...


class CommandDispatcher:
    def __init__(self, shell: SafeShell, hapi: HapiLike, bridge: BridgeLike, *, allow_git: bool = True):
        self.shell = shell
        self.hapi = hapi
        self.bridge = bridge
        self.allow_git = allow_git

    async def dispatch(self, umo: str, text: str, state: TerminalState) -> DispatchResult:
        raw = (text or "").strip()
        if not raw:
            return DispatchResult("当前处于终端模式。输入 /term off 退出。")

        try:
            if raw == "/pwd":
                state.cwd = self.shell.resolve_inside(state.cwd, ".")
                return DispatchResult(str(state.cwd))

            if raw == "/dir" or raw.startswith("/dir "):
                target = raw[5:].strip() if raw.startswith("/dir ") else ""
                return DispatchResult(self.shell.dir_list(state.cwd, target))

            if raw.startswith("/cd "):
                target = raw[4:].strip()
                if not target:
                    return DispatchResult("用法: /cd <相对路径>")
                state.cwd = self.shell.cd(state.cwd, target)
                return DispatchResult(f"cwd: {state.cwd}")

            if raw == "/git status":
                if not self.allow_git:
                    return DispatchResult("/git status disabled by config")
                return DispatchResult(self.shell.git_status(state.cwd))

            if raw == "/hapi status":
                _, message = await self.hapi.status()
                return DispatchResult(str(message))

            if raw == "/hapi list":
                ok, data = await self.hapi.list_sessions()
                if not ok:
                    return DispatchResult(str(data))
                sessions = data if isinstance(data, list) else []
                state.last_sessions = sessions
                return DispatchResult(self._format_sessions(sessions))

            if raw in ("/codex ls", "/cc ls"):
                flavor = "codex" if raw.startswith("/codex") else "claude"
                ok, data = await self.hapi.list_sessions(flavor)
                if not ok:
                    return DispatchResult(str(data))
                sessions = data if isinstance(data, list) else []
                state.last_sessions = sessions
                return DispatchResult(self._format_sessions(sessions))

            if raw.startswith("/codex new") or raw.startswith("/cc new"):
                parts = raw.split(maxsplit=2)
                flavor = "codex" if parts[0] == "/codex" else "claude"
                target = parts[2].strip() if len(parts) >= 3 else "."
                directory = self.shell.resolve_inside(state.cwd, target)
                if not directory.is_dir():
                    return DispatchResult(f"not a directory: {target}")
                ok, message, sid = await self.hapi.spawn_session(flavor, directory)
                if ok and sid:
                    state.current_session_id = sid
                    state.current_flavor = flavor
                return DispatchResult(message)

            if raw.startswith("/codex use ") or raw.startswith("/cc use ") or raw.startswith("/use "):
                target = raw.split()[-1]
                return DispatchResult(self._select_session(state, target))

            if raw.startswith("/send "):
                return await self._send_to_current_or_bridge(umo, raw[6:].strip(), state)

            if raw.startswith("/"):
                return DispatchResult("当前处于终端模式，未识别命令。输入 /term off 退出。")

            return await self._send_to_current_or_bridge(umo, raw, state)
        except ValueError as exc:
            return DispatchResult(f"拒绝: {exc}")
        except subprocess.TimeoutExpired:
            return DispatchResult("命令超时")

    async def _send_to_current_or_bridge(self, umo: str, text: str, state: TerminalState) -> DispatchResult:
        if state.current_session_id:
            _, message = await self.hapi.send_message(state.current_session_id, text)
            return DispatchResult(str(message))
        ok, message = await self.bridge.send_to_bound_thread(umo, text)
        if ok:
            return DispatchResult(str(message))
        if "read-only" in str(message).lower():
            return DispatchResult(str(message))
        return DispatchResult("No selected session. Use /codex new, /cc new, /use, or /codexbridge on.")

    def _select_session(self, state: TerminalState, target: str) -> str:
        sessions = state.last_sessions or []
        chosen = None
        if target.isdigit():
            index = int(target)
            if 1 <= index <= len(sessions):
                chosen = sessions[index - 1]
        else:
            matches = [s for s in sessions if str(s.get("id", "")).startswith(target)]
            if len(matches) == 1:
                chosen = matches[0]
            elif len(matches) > 1:
                return "匹配到多个 session，请输入更长 ID 前缀。"
        if not chosen:
            return "未找到 session，请先 /codex ls、/cc ls 或 /hapi list。"

        state.current_session_id = str(chosen.get("id", ""))
        metadata = chosen.get("metadata") or {}
        state.current_flavor = str(metadata.get("flavor") or "")
        return f"已选择 [{state.current_flavor or '?'}] {state.current_session_id[:8]}"

    @staticmethod
    def _format_sessions(sessions: list[dict]) -> str:
        if not sessions:
            return "(no sessions)"
        lines = []
        for i, session in enumerate(sessions, 1):
            sid = str(session.get("id", ""))
            metadata = session.get("metadata") or {}
            flavor = metadata.get("flavor", "?")
            active = "active" if session.get("active") else "inactive"
            summary = metadata.get("summary") or {}
            title = summary.get("text", "") if isinstance(summary, dict) else str(summary or "")
            lines.append(f"{i}. [{flavor}][{active}] {sid[:8]} {title}".rstrip())
        return "\n".join(lines)
