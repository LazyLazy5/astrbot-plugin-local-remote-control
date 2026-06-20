from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BridgeBinding:
    thread_id: str
    rollout_path: Path
    offset: int


class CodexAppBridge:
    """Experimental per-window bridge state.

    The first implementation provides safe, explicit enable/disable/status and
    read-only discovery from Codex's local session index. App-server write support
    can be layered behind this interface without changing terminal dispatch.
    """

    def __init__(self, kv, *, codex_home: Path | None = None, enabled: bool = True):
        self.kv = kv
        self.codex_home = codex_home or (Path.home() / ".codex")
        self.feature_enabled = enabled
        self.windows: set[str] = set()
        self.bindings: dict[str, BridgeBinding] = {}

    async def load(self):
        stored = await self.kv.get_kv_data("codexbridge_windows", [])
        self.windows = {str(x) for x in stored or []}
        for umo in list(self.windows):
            thread_id = await self.kv.get_kv_data(f"codexbridge_thread_{umo}", "")
            if not thread_id:
                continue
            path = self.find_rollout_path(str(thread_id))
            if path:
                offset = int(await self.kv.get_kv_data(f"codexbridge_offset_{umo}", path.stat().st_size) or 0)
                self.bindings[umo] = self.create_binding(str(thread_id), path, offset)

    async def enable(self, umo: str) -> str:
        if not self.feature_enabled:
            return "Codex Bridge disabled by config"
        self.windows.add(umo)
        await self.kv.put_kv_data("codexbridge_windows", sorted(self.windows))
        thread = self._latest_thread()
        if thread:
            thread_id = str(thread.get("id") or "")
            path = self.find_rollout_path(thread_id)
            if path:
                offset = path.stat().st_size
                self.bindings[umo] = self.create_binding(thread_id, path, offset)
                await self.kv.put_kv_data(f"codexbridge_thread_{umo}", thread_id)
                await self.kv.put_kv_data(f"codexbridge_offset_{umo}", offset)
            return f"Codex Bridge on\n绑定最近 thread: {thread.get('thread_name', thread.get('id'))}"
        return "Codex Bridge on\n未找到 Codex thread；当前为等待绑定状态。"

    async def disable(self, umo: str) -> str:
        self.windows.discard(umo)
        self.bindings.pop(umo, None)
        await self.kv.put_kv_data("codexbridge_windows", sorted(self.windows))
        return "Codex Bridge off"

    async def status(self, umo: str) -> str:
        on = umo in self.windows
        thread_id = await self.kv.get_kv_data(f"codexbridge_thread_{umo}", "")
        return f"Codex Bridge: {'on' if on else 'off'}" + (f"\nthread: {thread_id}" if thread_id else "")

    async def send_to_bound_thread(self, umo: str, text: str) -> tuple[bool, str]:
        if umo not in self.windows:
            return False, "No selected HAPI session. Use /codex new, /cc new, /use, or /codexbridge on."
        return False, "Codex Bridge is read-only in this build; app-server write support unavailable."

    @staticmethod
    def create_binding(thread_id: str, rollout_path: Path, offset: int) -> BridgeBinding:
        return BridgeBinding(thread_id=thread_id, rollout_path=rollout_path, offset=offset)

    def find_rollout_path(self, thread_id: str) -> Path | None:
        if not thread_id:
            return None
        sessions = self.codex_home / "sessions"
        if not sessions.exists():
            return None
        matches = sorted(
            sessions.rglob(f"*{thread_id}.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return matches[0] if matches else None

    async def poll_once(self) -> list[tuple[str, str]]:
        notifications: list[tuple[str, str]] = []
        for umo, binding in list(self.bindings.items()):
            if umo not in self.windows or not binding.rollout_path.exists():
                continue
            size = binding.rollout_path.stat().st_size
            if size < binding.offset:
                binding.offset = 0
            if size == binding.offset:
                continue
            with binding.rollout_path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(binding.offset)
                chunk = f.read()
                binding.offset = f.tell()
            await self.kv.put_kv_data(f"codexbridge_offset_{umo}", binding.offset)
            for line in chunk.splitlines():
                text = self.extract_assistant_text(line)
                if text:
                    notifications.append((umo, f"[Codex App]\n{text}"))
        return notifications

    @staticmethod
    def extract_assistant_text(line: str) -> str:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            return ""
        payload = item.get("payload") or {}
        if item.get("type") != "response_item":
            return ""
        if payload.get("type") != "message":
            return ""
        if payload.get("role") not in ("assistant", "agent"):
            return ""
        parts = []
        for content in payload.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts).strip()

    def _latest_thread(self) -> dict | None:
        index = self.codex_home / "session_index.jsonl"
        if not index.exists():
            return None
        latest = None
        try:
            for line in index.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                latest = item
        except Exception:
            return None
        return latest if isinstance(latest, dict) else None
