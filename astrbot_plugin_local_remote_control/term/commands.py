from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .safe_shell import SafeShell


@dataclass
class TerminalState:
    cwd: Path
    backend: str = "shell"
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

    async def diagnostic_status(self) -> str:
        ...

    async def reload_config(self) -> tuple[bool, str]:
        ...

    async def list_sessions(self, flavor: str | None = None) -> tuple[bool, str | list[dict]]:
        ...

    async def spawn_session(self, flavor: str, directory: Path) -> tuple[bool, str, str | None]:
        ...

    async def send_message(self, session_id: str, text: str) -> tuple[bool, str]:
        ...

    async def abort_session(self, session_id: str) -> tuple[bool, str]:
        ...


class BridgeLike(Protocol):
    async def status(self, umo: str) -> str:
        ...

    async def send_to_bound_thread(self, umo: str, text: str) -> tuple[bool, str]:
        ...


def _resolve_spawn_dir(cwd: Path, target: str) -> Path:
    """Resolve a directory for HAPI spawn — absolute paths are allowed outside work_dir."""
    raw = Path(target).expanduser()
    return raw.resolve() if raw.is_absolute() else (cwd / raw).resolve()


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

            if raw == "/term hapi status":
                return DispatchResult(await self.hapi.diagnostic_status())

            if raw == "/term hapi reload":
                _, message = await self.hapi.reload_config()
                return DispatchResult(str(message))

            if raw == "/term shell":
                state.backend = "shell"
                return DispatchResult("Terminal backend: shell")

            if raw.startswith("/term agent "):
                parts = raw.split(maxsplit=3)
                if len(parts) < 3:
                    return DispatchResult("用法: /term agent codex|cc [相对路径]")
                flavor = self._normalize_agent(parts[2])
                if not flavor:
                    return DispatchResult("agent 只能是 codex 或 cc/claude")
                target = parts[3].strip() if len(parts) >= 4 else "."
                directory = _resolve_spawn_dir(state.cwd, target)
                if not directory.is_dir():
                    return DispatchResult(f"not a directory: {target}")
                ok, message, sid = await self.hapi.spawn_session(flavor, directory)
                if ok and sid:
                    state.backend = "hapi"
                    state.current_session_id = sid
                    state.current_flavor = flavor
                return DispatchResult(message)

            if raw == "/term ls" or raw.startswith("/term ls "):
                parts = raw.split(maxsplit=2)
                flavor = None
                if len(parts) == 3 and parts[2] != "all":
                    flavor = self._normalize_agent(parts[2])
                    if not flavor:
                        return DispatchResult("agent 只能是 codex、cc/claude 或 all")
                ok, data = await self.hapi.list_sessions(flavor)
                if not ok:
                    return DispatchResult(str(data))
                sessions = data if isinstance(data, list) else []
                state.last_sessions = sessions
                return DispatchResult(self._format_sessions(sessions))

            if raw.startswith("/term use "):
                target = raw.split()[-1]
                return DispatchResult(self._select_session(state, target))

            if raw == "/term stop":
                if not state.current_session_id:
                    return DispatchResult("No selected HAPI session.")
                _, message = await self.hapi.abort_session(state.current_session_id)
                return DispatchResult(str(message))

            if raw.startswith("/term send "):
                return await self._send_to_current_or_bridge(umo, raw[len("/term send "):].strip(), state)

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
                directory = _resolve_spawn_dir(state.cwd, target)
                if not directory.is_dir():
                    return DispatchResult(f"not a directory: {target}")
                ok, message, sid = await self.hapi.spawn_session(flavor, directory)
                if ok and sid:
                    state.current_session_id = sid
                    state.current_flavor = flavor
                    state.backend = "hapi"
                return DispatchResult(message)

            if raw.startswith("/codex use ") or raw.startswith("/cc use ") or raw.startswith("/use "):
                target = raw.split()[-1]
                return DispatchResult(self._select_session(state, target))

            if raw.startswith("/send "):
                return await self._send_to_current_or_bridge(umo, raw[6:].strip(), state)

            if raw.startswith("/"):
                return DispatchResult("当前处于终端模式，未识别命令。输入 /term off 退出。")

            if state.backend == "hapi":
                return await self._send_to_current_or_bridge(umo, raw, state)

            return DispatchResult(
                "shell backend 只支持 /pwd、/dir、/cd、/git status 等受限命令。"
                "普通文本不会发送到 cmd；请用 /term agent codex|cc 进入 HAPI backend。"
            )
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
        if chosen.get("active") is False:
            return "session inactive，请使用 /term agent codex|cc 创建新 session，或选择 active session。"

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

    @staticmethod
    def _normalize_agent(value: str) -> str | None:
        value = (value or "").strip().lower()
        if value == "codex":
            return "codex"
        if value in ("cc", "claude", "claude-code"):
            return "claude"
        return None
