from __future__ import annotations

import json
from pathlib import Path


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

    async def load(self):
        stored = await self.kv.get_kv_data("codexbridge_windows", [])
        self.windows = {str(x) for x in stored or []}

    async def enable(self, umo: str) -> str:
        if not self.feature_enabled:
            return "Codex Bridge disabled by config"
        self.windows.add(umo)
        await self.kv.put_kv_data("codexbridge_windows", sorted(self.windows))
        thread = self._latest_thread()
        if thread:
            await self.kv.put_kv_data(f"codexbridge_thread_{umo}", thread.get("id"))
            return f"Codex Bridge on\n绑定最近 thread: {thread.get('thread_name', thread.get('id'))}"
        return "Codex Bridge on\n未找到 Codex thread；当前为等待绑定状态。"

    async def disable(self, umo: str) -> str:
        self.windows.discard(umo)
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

