from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class BridgeBinding:
    thread_id: str
    rollout_path: Path
    offset: int


class AppServerLike(Protocol):
    async def list_threads(self) -> list[dict]:
        ...

    async def send_text(self, thread_id: str, text: str) -> tuple[bool, str]:
        ...


class CodexAppServerClient:
    """Best-effort JSON-RPC client for `codex app-server proxy`.

    It opens one proxy process per request. That is slower than a persistent
    transport, but keeps failure isolated and makes the bridge safe to disable.
    """

    def __init__(self, codex_command: str = "codex", timeout: int = 10):
        self.codex_command = codex_command
        self.timeout = timeout
        self._next_id = 1

    async def list_threads(self) -> list[dict]:
        response = await self._request(
            "thread/list",
            {
                "archived": False,
                "limit": 10,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "useStateDbOnly": True,
            },
        )
        data = response.get("data") or []
        if not isinstance(data, list):
            return []
        return data

    async def send_text(self, thread_id: str, text: str) -> tuple[bool, str]:
        response = await self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "approvalPolicy": None,
                "approvalsReviewer": None,
                "sandboxPolicy": None,
                "cwd": None,
                "effort": None,
                "summary": None,
                "model": None,
                "outputSchema": None,
                "serviceTier": None,
                "personality": None,
                "clientUserMessageId": None,
            },
        )
        turn = response.get("turn") or {}
        turn_id = turn.get("id") or turn.get("turnId") or ""
        return True, f"sent to Codex App thread {thread_id}" + (f" turn {turn_id}" if turn_id else "")

    async def _request(self, method: str, params: dict) -> dict:
        import asyncio

        request_id = self._next_id
        self._next_id += 1
        payload = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n"
        process = await asyncio.create_subprocess_exec(
            self.codex_command,
            "app-server",
            "proxy",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(payload.encode("utf-8")), timeout=self.timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError("codex app-server proxy timed out")
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace").strip() or "codex app-server proxy failed")

        for line in stdout.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("id") != request_id:
                continue
            if item.get("error"):
                raise RuntimeError(str(item["error"]))
            result = item.get("result")
            return result if isinstance(result, dict) else {}
        raise RuntimeError("codex app-server proxy returned no matching response")


class CodexAppBridge:
    """Experimental per-window bridge state.

    The first implementation provides safe, explicit enable/disable/status and
    read-only discovery from Codex's local session index. App-server write support
    can be layered behind this interface without changing terminal dispatch.
    """

    def __init__(
        self,
        kv,
        *,
        codex_home: Path | None = None,
        enabled: bool = True,
        app_server: AppServerLike | None = None,
    ):
        self.kv = kv
        self.codex_home = codex_home or (Path.home() / ".codex")
        self.feature_enabled = enabled
        self.app_server = app_server if app_server is not None else CodexAppServerClient()
        self.windows: set[str] = set()
        self.bindings: dict[str, BridgeBinding] = {}
        self.app_bindings: dict[str, str] = {}

    async def load(self):
        stored = await self.kv.get_kv_data("codexbridge_windows", [])
        self.windows = {str(x) for x in stored or []}
        for umo in list(self.windows):
            app_thread_id = await self.kv.get_kv_data(f"codexbridge_app_thread_{umo}", "")
            if app_thread_id:
                self.app_bindings[umo] = str(app_thread_id)
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
        app_message = await self._try_bind_app_server(umo)
        if app_message:
            return app_message
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
        self.app_bindings.pop(umo, None)
        await self.kv.put_kv_data("codexbridge_windows", sorted(self.windows))
        return "Codex Bridge off"

    async def status(self, umo: str) -> str:
        on = umo in self.windows
        thread_id = await self.kv.get_kv_data(f"codexbridge_thread_{umo}", "")
        return f"Codex Bridge: {'on' if on else 'off'}" + (f"\nthread: {thread_id}" if thread_id else "")

    async def send_to_bound_thread(self, umo: str, text: str) -> tuple[bool, str]:
        if umo not in self.windows:
            return False, "No selected HAPI session. Use /codex new, /cc new, /use, or /codexbridge on."
        app_thread_id = self.app_bindings.get(umo)
        if app_thread_id:
            try:
                return await self.app_server.send_text(app_thread_id, text)
            except Exception as exc:
                return False, f"Codex App write failed, JSONL bridge is read-only: {exc}"
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

    async def _try_bind_app_server(self, umo: str) -> str:
        try:
            threads = await self.app_server.list_threads()
        except Exception:
            return ""
        if not threads:
            return ""
        thread = threads[0]
        thread_id = str(thread.get("id") or thread.get("threadId") or "")
        if not thread_id:
            return ""
        self.app_bindings[umo] = thread_id
        await self.kv.put_kv_data(f"codexbridge_app_thread_{umo}", thread_id)
        title = thread.get("title") or thread.get("name") or thread.get("thread_name") or thread_id
        return f"Codex Bridge on\n绑定 Codex App thread: {title}"

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
