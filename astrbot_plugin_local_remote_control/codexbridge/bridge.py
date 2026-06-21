from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from ..common.delivery_queue import DeliveryQueue
from ..common.platform_strategy import format_platform_status


@dataclass
class BridgeBinding:
    thread_id: str
    rollout_path: Path
    offset: int
    pending_offset: int | None = None


@dataclass
class BridgeTurnState:
    status: str = "idle"
    active_turn_id: str = ""
    pending_user_inputs: list[str] = field(default_factory=list)
    assistant_delta: str = ""
    last_event_at: float = 0.0
    last_error: str = ""
    recent_output_keys: list[str] = field(default_factory=list)


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
        self.codex_command = default_codex_command() if codex_command == "codex" else codex_command
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
        await self._request("thread/resume", self._thread_resume_params(thread_id))
        response = await self._request(
            "turn/start",
            self._turn_start_params(thread_id, text),
        )
        turn = response.get("turn") or {}
        turn_id = turn.get("id") or turn.get("turnId") or ""
        return True, f"sent to Codex App thread {thread_id}" + (f" turn {turn_id}" if turn_id else "")

    @staticmethod
    def _default_cwd() -> str:
        return str(Path.cwd())

    @staticmethod
    def _thread_resume_params(thread_id: str) -> dict:
        return {
            "threadId": thread_id,
            "excludeTurns": True,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "cwd": CodexAppServerClient._default_cwd(),
        }

    @staticmethod
    def _turn_start_params(thread_id: str, text: str) -> dict:
        return {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
            "approvalPolicy": "never",
            "approvalsReviewer": None,
            "sandboxPolicy": {"type": "dangerFullAccess"},
            "cwd": CodexAppServerClient._default_cwd(),
            "effort": None,
            "summary": None,
            "model": None,
            "outputSchema": None,
            "serviceTier": None,
            "personality": None,
            "clientUserMessageId": None,
        }

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


class CodexStdioAppServerClient(CodexAppServerClient):
    """Best-effort JSON-RPC client for `codex app-server --stdio`."""

    def __init__(self, codex_command: str = "codex", timeout: int = 10):
        super().__init__(codex_command=codex_command, timeout=timeout)
        self._thread_paths: dict[str, str] = {}

    async def list_threads(self) -> list[dict]:
        threads = await super().list_threads()
        for thread in threads:
            thread_id = str(thread.get("id") or thread.get("threadId") or "")
            path = str(thread.get("path") or "")
            if thread_id and path:
                self.remember_thread_path(thread_id, path)
        return threads

    def remember_thread_path(self, thread_id: str, path: str) -> None:
        if thread_id and path:
            self._thread_paths[str(thread_id)] = str(path)

    async def send_text(self, thread_id: str, text: str) -> tuple[bool, str]:
        resume_params = self._thread_resume_params(thread_id)
        path = self._thread_paths.get(thread_id)
        if path:
            resume_params["path"] = path
        response = await self._request_sequence(
            [
                ("thread/resume", resume_params),
                ("turn/start", self._turn_start_params(thread_id, text)),
            ],
            wait_for_turn_completion=True,
        )
        turn = response.get("turn") or {}
        turn_id = turn.get("id") or turn.get("turnId") or ""
        return True, f"sent to Codex App thread {thread_id}" + (f" turn {turn_id}" if turn_id else "")

    async def _request(self, method: str, params: dict) -> dict:
        return await self._request_sequence([(method, params)])

    async def _request_sequence(
        self,
        requests: list[tuple[str, dict]],
        *,
        wait_for_turn_completion: bool = False,
    ) -> dict:
        import asyncio

        init_id = self._next_id
        self._next_id += 1
        process = await asyncio.create_subprocess_exec(
            self.codex_command,
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            await self._stdio_write(
                process.stdin,
                {
                    "jsonrpc": "2.0",
                    "id": init_id,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "astrbot_plugin_local_remote_control",
                            "version": "0.3.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                },
            )
            await self._stdio_read_response(process, init_id)
            await self._stdio_write(process.stdin, {"jsonrpc": "2.0", "method": "initialized"})
            last_result: dict = {}
            for method, params in requests:
                request_id = self._next_id
                self._next_id += 1
                await self._stdio_write(
                    process.stdin,
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                )
                last_result = await self._stdio_read_response(process, request_id)
            if wait_for_turn_completion:
                turn = last_result.get("turn") or {}
                turn_id = str(turn.get("id") or turn.get("turnId") or "")
                if turn_id:
                    await self._stdio_wait_for_turn_completion(process, turn_id)
            return last_result
        finally:
            if process.stdin is not None:
                process.stdin.close()
                wait_closed = getattr(process.stdin, "wait_closed", None)
                if wait_closed:
                    try:
                        await asyncio.wait_for(wait_closed(), timeout=1)
                    except Exception:
                        pass
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

    @staticmethod
    async def _stdio_write(stdin, payload: dict) -> None:
        stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await stdin.drain()

    async def _stdio_read_response(self, process, request_id: int) -> dict:
        import asyncio

        assert process.stdout is not None
        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=self.timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f"codex app-server stdio timed out waiting for {request_id}") from exc
            if not line:
                stderr = await process.stderr.read() if process.stderr else b""
                raise RuntimeError(stderr.decode("utf-8", errors="replace").strip() or "codex app-server stdio closed")
            try:
                item = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if item.get("id") != request_id:
                continue
            if item.get("error"):
                raise RuntimeError(str(item["error"]))
            result = item.get("result")
            return result if isinstance(result, dict) else {}

    async def _stdio_wait_for_turn_completion(self, process, turn_id: str) -> None:
        import asyncio

        assert process.stdout is not None
        deadline = time.time() + max(self.timeout, 10)
        while time.time() < deadline:
            timeout = max(0.1, min(1.0, deadline - time.time()))
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
            except asyncio.TimeoutError:
                continue
            if not line:
                return
            try:
                item = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if item.get("method") == "turn/completed":
                params = item.get("params") or {}
                turn = params.get("turn") or {}
                seen_id = str(turn.get("id") or params.get("turnId") or "")
                if seen_id == turn_id:
                    return

class PersistentCodexStdioAppServerClient(CodexStdioAppServerClient):
    """Persistent JSON-RPC client for `codex app-server --stdio`.

    Unlike the short-lived client above, this keeps the app-server process open
    after `turn/start` so the turn can complete naturally and notifications can
    be observed by the bridge.
    """

    def __init__(self, codex_command: str = "codex", timeout: int = 10):
        super().__init__(codex_command=codex_command, timeout=timeout)
        self._process = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._early_responses: dict[int, dict] = {}
        self._events: list[dict] = []
        self.last_error = ""

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
        for thread in data:
            thread_id = str(thread.get("id") or thread.get("threadId") or "")
            path = str(thread.get("path") or "")
            if thread_id and path:
                self.remember_thread_path(thread_id, path)
        return data

    async def send_text(self, thread_id: str, text: str) -> tuple[bool, str]:
        resume_params = self._thread_resume_params(thread_id)
        path = self._thread_paths.get(thread_id)
        if path:
            resume_params["path"] = path
        await self._request("thread/resume", resume_params)
        try:
            response = await self._request("turn/start", self._turn_start_params(thread_id, text))
        except TimeoutError as exc:
            self.last_error = str(exc)
            return True, f"sent to Codex App thread {thread_id} turn unknown"
        turn = response.get("turn") or {}
        turn_id = turn.get("id") or turn.get("turnId") or ""
        return True, f"sent to Codex App thread {thread_id}" + (f" turn {turn_id}" if turn_id else "")

    async def poll_events(self) -> list[dict]:
        events = self._events
        self._events = []
        return events

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None and getattr(process, "stdin", None) is not None:
            try:
                process.stdin.close()
                wait_closed = getattr(process.stdin, "wait_closed", None)
                if wait_closed:
                    await asyncio.wait_for(wait_closed(), timeout=1)
            except Exception:
                pass
        if process is not None and getattr(process, "returncode", None) is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        task = self._reader_task
        self._reader_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, RuntimeError):
                pass

    async def _ensure_started(self) -> None:
        process = self._process
        if process is not None and getattr(process, "returncode", None) is None:
            return
        self._pending.clear()
        self._early_responses.clear()
        self._events.clear()
        self._process = await asyncio.create_subprocess_exec(
            self.codex_command,
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop(self._process))
        init_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[init_id] = future
        self._deliver_early_response(init_id)
        await self._write_payload(
            {
                "jsonrpc": "2.0",
                "id": init_id,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "astrbot_plugin_local_remote_control",
                        "version": "0.3.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        try:
            await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(init_id, None)
            raise TimeoutError(f"codex app-server stdio timed out waiting for {init_id}") from exc
        await self._write_payload({"jsonrpc": "2.0", "method": "initialized"})

    async def _request(self, method: str, params: dict) -> dict:
        await self._ensure_started()
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = future
        self._deliver_early_response(request_id)
        await self._write_payload({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            result = await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise TimeoutError(f"codex app-server stdio timed out waiting for {method}") from exc
        return result if isinstance(result, dict) else {}

    async def _write_payload(self, payload: dict) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("codex app-server stdio is not running")
        self._process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self._process.stdin.drain()

    async def _read_loop(self, process) -> None:
        assert process.stdout is not None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    item = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                request_id = item.get("id")
                if request_id is not None:
                    if request_id in self._pending:
                        self._deliver_response(int(request_id), item)
                    else:
                        self._early_responses[int(request_id)] = item
                    continue
                if item.get("method"):
                    self._events.append(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)
        finally:
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(RuntimeError(self.last_error or "codex app-server stdio closed"))
            self._pending.clear()

    def _deliver_early_response(self, request_id: int) -> None:
        item = self._early_responses.pop(request_id, None)
        if item is not None:
            self._deliver_response(request_id, item)

    def _deliver_response(self, request_id: int, item: dict) -> None:
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return
        if item.get("error"):
            future.set_exception(RuntimeError(str(item["error"])))
            return
        result = item.get("result")
        future.set_result(result if isinstance(result, dict) else {})


def default_codex_command() -> str:
    if sys.platform.startswith("win"):
        if shutil.which("codex.cmd"):
            return "codex.cmd"
        if shutil.which("codex.exe"):
            return "codex.exe"
    return "codex"


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
        fallback_app_server: AppServerLike | None = None,
        delivery_queue: DeliveryQueue | None = None,
        now: Callable[[], float] | None = None,
    ):
        self.kv = kv
        self.codex_home = codex_home or (Path.home() / ".codex")
        self.feature_enabled = enabled
        self.app_server = app_server if app_server is not None else CodexAppServerClient()
        self.fallback_app_server = fallback_app_server if fallback_app_server is not None else PersistentCodexStdioAppServerClient()
        self.windows: set[str] = set()
        self.bindings: dict[str, BridgeBinding] = {}
        self.app_bindings: dict[str, str] = {}
        self.turn_states: dict[str, BridgeTurnState] = {}
        self.delivery_queue = delivery_queue
        self._outbound_texts: dict[str, list[str]] = {}
        self._active_app_servers: dict[str, AppServerLike] = {}
        self._app_server_modes: dict[str, str] = {}
        self._last_threads: dict[str, list[dict]] = {}
        self._last_thread_modes: dict[str, str] = {}
        self._last_app_server_error = ""
        self._now = now or time.time
        self._send_failure_notice_at: dict[tuple[str, str], float] = {}

    async def load(self):
        stored = await self.kv.get_kv_data("codexbridge_windows", [])
        self.windows = {str(x) for x in stored or []}
        for umo in list(self.windows):
            app_thread_id = await self.kv.get_kv_data(f"codexbridge_app_thread_{umo}", "")
            if app_thread_id:
                app_thread_id = str(app_thread_id)
                self.app_bindings[umo] = app_thread_id
                transport = str(await self.kv.get_kv_data(f"codexbridge_app_transport_{umo}", "") or "")
                thread_path = str(await self.kv.get_kv_data(f"codexbridge_app_thread_path_{umo}", "") or "")
                server = self._server_for_mode(transport)
                if server is not None:
                    self._active_app_servers[umo] = server
                    self._app_server_modes[umo] = transport
                    self._remember_path(server, app_thread_id, thread_path)
                    await self._bind_jsonl_path(umo, app_thread_id, thread_path)
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
        self.turn_states.pop(umo, None)
        await self.kv.put_kv_data("codexbridge_windows", sorted(self.windows))
        if not self.windows:
            await self._close_app_servers()
        return "Codex Bridge off"

    async def status(self, umo: str) -> str:
        await self._refresh_stale_turn_state(umo)
        on = umo in self.windows
        thread_id = await self.kv.get_kv_data(f"codexbridge_thread_{umo}", "")
        app_thread = self.app_bindings.get(umo) or await self.kv.get_kv_data(f"codexbridge_app_thread_{umo}", "")
        mode = "read-write" if app_thread else "read-only"
        lines = [f"Codex Bridge: {'on' if on else 'off'}", f"mode: {mode}", format_platform_status(umo)]
        if app_thread:
            lines.append(f"transport: {self._app_server_modes.get(umo, 'app-server')}")
        if thread_id:
            lines.append(f"thread: {thread_id}")
        if app_thread:
            lines.append(f"app_thread: {app_thread}")
        turn_state = self._turn_state(umo)
        lines.append(f"turn_status: {turn_state.status}")
        if turn_state.active_turn_id:
            lines.append(f"active_turn_id: {turn_state.active_turn_id}")
        lines.append(f"pending_user_inputs: {len(turn_state.pending_user_inputs)}")
        if turn_state.last_event_at:
            lines.append(f"last_event_at: {turn_state.last_event_at:.0f}")
        if turn_state.last_error:
            lines.append(f"turn_error: {turn_state.last_error}")
        binding = self.bindings.get(umo)
        if binding:
            lines.append(f"jsonl_offset: {binding.offset}")
        if self._last_app_server_error:
            lines.append(f"app_server_error: {self._last_app_server_error}")
        if self.delivery_queue:
            status = self.delivery_queue.status(umo, "codexbridge")
            lines.append(f"queue: {status['queue_length']}")
            if status["last_error"]:
                lines.append(f"last_error: {status['last_error']}")
            if status["next_retry_at"]:
                lines.append(f"next_retry_at: {status['next_retry_at']:.0f}")
            if status.get("needs_user_refresh"):
                lines.append("needs_user_refresh: yes")
        return "\n".join(lines)

    async def list_threads(self, umo: str) -> str:
        mode, threads, errors = await self._fetch_app_threads()
        if threads:
            self._last_threads[umo] = threads
            self._last_thread_modes[umo] = mode
            return self._format_threads(threads)
        detail = "\n".join(errors) if errors else "no Codex App threads"
        self._last_app_server_error = detail
        return f"No Codex App threads available\n{detail}".strip()

    async def use_thread(self, umo: str, target: str) -> str:
        threads = self._last_threads.get(umo)
        mode = self._last_thread_modes.get(umo, "")
        if not threads:
            mode, threads, errors = await self._fetch_app_threads()
            if not threads:
                detail = "\n".join(errors) if errors else "no Codex App threads"
                self._last_app_server_error = detail
                return f"No Codex App threads available\n{detail}".strip()
            self._last_threads[umo] = threads
            self._last_thread_modes[umo] = mode
        chosen = self._choose_thread(threads, target)
        if isinstance(chosen, str):
            return chosen
        server = self._server_for_mode(mode)
        if server is None:
            return "No app-server transport available for selected thread"
        thread_id = self._thread_id(chosen)
        path = self._thread_path(chosen)
        self.windows.add(umo)
        self.app_bindings[umo] = thread_id
        self._active_app_servers[umo] = server
        self._app_server_modes[umo] = mode
        self._remember_path(server, thread_id, path)
        await self.kv.put_kv_data("codexbridge_windows", sorted(self.windows))
        await self.kv.put_kv_data(f"codexbridge_app_thread_{umo}", thread_id)
        await self.kv.put_kv_data(f"codexbridge_app_transport_{umo}", mode)
        if path:
            await self.kv.put_kv_data(f"codexbridge_app_thread_path_{umo}", path)
            await self._bind_jsonl_path(umo, thread_id, path)
        return f"Codex Bridge using {thread_id} {self._thread_title(chosen)}".rstrip()

    async def send_to_bound_thread(self, umo: str, text: str) -> tuple[bool, str]:
        if umo not in self.windows:
            return False, "Codex Bridge is not enabled for this window. Use /codexbridge on first."
        app_thread_id = self.app_bindings.get(umo)
        if app_thread_id:
            await self._process_app_server_events()
            state = self._turn_state(umo)
            if state.status == "running":
                state.pending_user_inputs.append(text)
                return True, f"queued for Codex App thread {app_thread_id}; pending={len(state.pending_user_inputs)}"
            return await self._send_text_now(umo, app_thread_id, text)
        return False, "Codex Bridge is read-only in this build; app-server write support unavailable."

    async def _send_text_now(self, umo: str, app_thread_id: str, text: str, *, poll_after: bool = True) -> tuple[bool, str]:
        errors: list[str] = []
        modes = self._send_modes_for(umo)
        thread_path = str(await self.kv.get_kv_data(f"codexbridge_app_thread_path_{umo}", "") or "")
        for mode in modes:
            server = self._server_for_mode(mode)
            if server is None:
                continue
            self._remember_path(server, app_thread_id, thread_path)
            try:
                ok, message = await server.send_text(app_thread_id, text)
            except Exception as exc:
                errors.append(f"{mode}: {exc}")
                continue
            if ok:
                self._remember_outbound(umo, text)
                self._active_app_servers[umo] = server
                self._app_server_modes[umo] = mode
                await self.kv.put_kv_data(f"codexbridge_app_transport_{umo}", mode)
                self._mark_turn_started_from_message(umo, message)
                if poll_after:
                    await self.poll_once()
                return ok, message
            errors.append(f"{mode}: {message}")
        detail = "; ".join(errors) or "no app-server transport available"
        self._last_app_server_error = detail
        self._turn_state(umo).last_error = detail
        return False, f"Codex App write failed, JSONL bridge is read-only: {detail}"

    def _mark_turn_started_from_message(self, umo: str, message: str) -> None:
        match = re.search(r"\bturn\s+([^\s]+)", message or "")
        if not match:
            return
        state = self._turn_state(umo)
        state.status = "running"
        turn_id = match.group(1)
        state.active_turn_id = "" if turn_id == "unknown" else turn_id
        state.last_event_at = self._now()
        state.last_error = ""

    async def _process_app_server_events(self) -> list[tuple[str, str]]:
        notifications: list[tuple[str, str]] = []
        servers: dict[int, AppServerLike] = {}
        for server in self._active_app_servers.values():
            poll_events = getattr(server, "poll_events", None)
            if poll_events:
                servers[id(server)] = server
        for server in servers.values():
            try:
                events = await server.poll_events()
            except Exception as exc:
                self._last_app_server_error = str(exc)
                for umo, active in self._active_app_servers.items():
                    if active is server:
                        state = self._turn_state(umo)
                        state.status = "failed"
                        state.last_error = str(exc)
                continue
            for event in events or []:
                notifications.extend(await self._handle_app_server_event(server, event))
        for umo in list(self.app_bindings):
            await self._flush_pending_inputs(umo)
        return notifications

    async def _handle_app_server_event(self, server: AppServerLike, event: dict) -> list[tuple[str, str]]:
        notifications: list[tuple[str, str]] = []
        method = str(event.get("method") or event.get("type") or "")
        params = event.get("params") or event.get("payload") or {}
        if not isinstance(params, dict):
            params = {}
        thread_id = self._event_thread_id(params)
        for umo, bound_thread in list(self.app_bindings.items()):
            if self._active_app_servers.get(umo) is not server:
                continue
            if thread_id and thread_id != bound_thread:
                continue
            state = self._turn_state(umo)
            state.last_event_at = self._now()
            turn_id = self._event_turn_id(params)
            if method == "thread/status/changed":
                status_type = self._thread_status_type(params)
                if status_type in {"idle", "notLoaded"}:
                    state.status = "idle"
                    state.active_turn_id = ""
                    state.last_error = ""
                elif status_type == "active":
                    state.status = "running"
                elif status_type == "systemError":
                    state.status = "failed"
                    state.active_turn_id = ""
                    state.last_error = self._event_error(params) or "thread system error"
            elif method == "turn/started":
                state.status = "running"
                state.active_turn_id = turn_id
                state.assistant_delta = ""
                state.last_error = ""
            elif method == "turn/completed":
                state.status = "idle"
                state.active_turn_id = ""
                state.last_error = ""
            elif method == "turn/failed":
                state.status = "failed"
                state.active_turn_id = ""
                state.last_error = self._event_error(params) or "turn failed"
            elif method == "turn/aborted":
                state.status = "aborted"
                state.active_turn_id = ""
                state.last_error = self._event_error(params) or "turn aborted"
            if self._is_delta_event(method):
                delta = self._event_assistant_text(method, params)
                if delta:
                    state.assistant_delta += delta
                continue
            text = state.assistant_delta.strip() if method == "turn/completed" else self._event_assistant_text(method, params)
            if method == "turn/completed":
                state.assistant_delta = ""
            if text and not self._should_suppress_bridge_output_text(text):
                await self._emit_bridge_output(notifications, umo, bound_thread, "assistant", text)
        return notifications

    async def _refresh_stale_turn_state(self, umo: str) -> None:
        state = self.turn_states.get(umo)
        if not state or state.status != "running":
            return
        app_thread_id = self.app_bindings.get(umo)
        server = self._active_app_servers.get(umo)
        if not app_thread_id or server is None:
            return
        status_fn = getattr(server, "thread_status", None)
        if not status_fn:
            return
        try:
            status = await status_fn(app_thread_id)
        except Exception as exc:
            state.last_error = str(exc)
            return
        if self._status_indicates_idle(status):
            state.status = "idle"
            state.active_turn_id = ""
            state.last_error = ""
            state.last_event_at = self._now()
            await self._flush_pending_inputs(umo)

    @staticmethod
    def _status_indicates_idle(status) -> bool:
        if isinstance(status, str):
            return status in {"idle", "notLoaded", "completed"}
        if isinstance(status, dict):
            return str(status.get("type") or "") in {"idle", "notLoaded", "completed"}
        return False

    @staticmethod
    def _thread_status_type(params: dict) -> str:
        status = params.get("status")
        if isinstance(status, dict):
            return str(status.get("type") or "")
        if isinstance(status, str):
            return status
        return ""

    async def _flush_pending_inputs(self, umo: str) -> None:
        state = self._turn_state(umo)
        app_thread_id = self.app_bindings.get(umo)
        if not app_thread_id or state.status == "running":
            return
        while state.pending_user_inputs and state.status != "running":
            text = state.pending_user_inputs.pop(0)
            ok, message = await self._send_text_now(umo, app_thread_id, text, poll_after=False)
            if not ok:
                state.pending_user_inputs.insert(0, text)
                state.last_error = message
                return

    def _turn_state(self, umo: str) -> BridgeTurnState:
        state = self.turn_states.get(umo)
        if state is None:
            state = BridgeTurnState()
            self.turn_states[umo] = state
        return state

    @staticmethod
    def _event_thread_id(params: dict) -> str:
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        return str(params.get("threadId") or params.get("thread_id") or turn.get("threadId") or turn.get("thread_id") or "")

    @staticmethod
    def _event_turn_id(params: dict) -> str:
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        return str(params.get("turnId") or params.get("turn_id") or turn.get("id") or turn.get("turnId") or "")

    @staticmethod
    def _event_error(params: dict) -> str:
        error = params.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(error or params.get("message") or "")

    @staticmethod
    def _event_assistant_text(method: str, params: dict) -> str:
        if method not in {
            "item/agentMessage/delta",
            "item/agent_message/delta",
            "agent_message",
            "agent/message",
        }:
            return ""
        for key in ("delta", "text", "message"):
            value = params.get(key)
            if isinstance(value, str):
                if key == "delta" and value:
                    return value
                if value.strip():
                    return value.strip()
        item = params.get("item")
        if isinstance(item, dict):
            for key in ("delta", "text", "message"):
                value = item.get(key)
                if isinstance(value, str):
                    if key == "delta" and value:
                        return value
                    if value.strip():
                        return value.strip()
        return ""

    @staticmethod
    def _is_delta_event(method: str) -> bool:
        return method in {"item/agentMessage/delta", "item/agent_message/delta"}

    async def retry(self, umo: str) -> str:
        if not self.delivery_queue:
            return "Codex Bridge queue unavailable"
        count = await self.delivery_queue.retry(umo, "codexbridge")
        return f"Codex Bridge retry queued: {count}"

    def should_report_send_failure(self, umo: str, message: str) -> bool:
        normalized = str(message or "").strip()
        if "read-only" not in normalized.lower():
            return True
        key = (umo, normalized)
        now = self._now()
        stale_before = now - 3600
        self._send_failure_notice_at = {
            existing_key: noticed_at
            for existing_key, noticed_at in self._send_failure_notice_at.items()
            if noticed_at >= stale_before
        }
        last = self._send_failure_notice_at.get(key, 0.0)
        if now - last < 30:
            return False
        self._send_failure_notice_at[key] = now
        return True

    async def probe(self) -> str:
        errors: list[str] = []
        for mode, server in (("proxy", self.app_server), ("stdio", self.fallback_app_server)):
            try:
                threads = await server.list_threads()
            except Exception as exc:
                errors.append(f"{mode}: failed: {exc}")
                continue
            return f"{mode}: read-write\nthreads: {len(threads)}"
        if self._latest_thread():
            lines = ["jsonl: read-only"]
        else:
            lines = ["jsonl: read-only waiting for thread"]
        lines.extend(errors)
        if self._last_app_server_error and self._last_app_server_error not in "\n".join(lines):
            lines.append(self._last_app_server_error)
        return "\n".join(lines)

    async def _fetch_app_threads(self) -> tuple[str, list[dict], list[str]]:
        errors: list[str] = []
        for mode, server in (("proxy", self.app_server), ("stdio", self.fallback_app_server)):
            try:
                threads = await server.list_threads()
            except Exception as exc:
                errors.append(f"{mode}: {exc}")
                continue
            if not threads:
                errors.append(f"{mode}: no threads")
                continue
            for thread in threads:
                self._remember_path(server, self._thread_id(thread), self._thread_path(thread))
            return mode, threads, errors
        return "", [], errors

    @staticmethod
    def _format_threads(threads: list[dict]) -> str:
        lines = []
        for index, thread in enumerate(threads, 1):
            thread_id = CodexAppBridge._thread_id(thread)
            title = CodexAppBridge._thread_title(thread)
            lines.append(f"{index}. {thread_id} {title}".rstrip())
        return "\n".join(lines) if lines else "(no threads)"

    @staticmethod
    def _choose_thread(threads: list[dict], target: str) -> dict | str:
        target = (target or "").strip()
        if target.isdigit():
            index = int(target)
            if 1 <= index <= len(threads):
                return threads[index - 1]
            return "thread index out of range"
        matches = [thread for thread in threads if CodexAppBridge._thread_id(thread).startswith(target)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return "matched multiple threads; use a longer id prefix"
        return "thread not found; run /codexbridge ls first"

    @staticmethod
    def _thread_id(thread: dict) -> str:
        return str(thread.get("id") or thread.get("threadId") or "")

    @staticmethod
    def _thread_path(thread: dict) -> str:
        return str(thread.get("path") or "")

    @staticmethod
    def _thread_title(thread: dict) -> str:
        return str(thread.get("title") or thread.get("name") or thread.get("thread_name") or "")

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
        errors: list[str] = []
        for mode, server in (("proxy", self.app_server), ("stdio", self.fallback_app_server)):
            try:
                threads = await server.list_threads()
            except Exception as exc:
                errors.append(f"{mode}: {exc}")
                continue
            if not threads:
                errors.append(f"{mode}: no threads")
                continue
            thread = threads[0]
            thread_id = str(thread.get("id") or thread.get("threadId") or "")
            if not thread_id:
                errors.append(f"{mode}: thread has no id")
                continue
            remember_path = getattr(server, "remember_thread_path", None)
            if remember_path and thread.get("path"):
                remember_path(thread_id, str(thread.get("path")))
            self.app_bindings[umo] = thread_id
            self._active_app_servers[umo] = server
            self._app_server_modes[umo] = mode
            await self.kv.put_kv_data(f"codexbridge_app_thread_{umo}", thread_id)
            await self.kv.put_kv_data(f"codexbridge_app_transport_{umo}", mode)
            if thread.get("path"):
                thread_path = str(thread.get("path"))
                await self.kv.put_kv_data(f"codexbridge_app_thread_path_{umo}", thread_path)
                await self._bind_jsonl_path(umo, thread_id, thread_path)
            title = thread.get("title") or thread.get("name") or thread.get("thread_name") or thread_id
            mode_label = "Codex App thread" if mode == "proxy" else "Codex App thread (stdio)"
            return f"Codex Bridge on\n绑定 {mode_label}: {title}"
        self._last_app_server_error = "; ".join(errors)
        return ""

    async def _bind_jsonl_path(self, umo: str, thread_id: str, raw_path: str) -> None:
        if not raw_path:
            return
        path = Path(raw_path)
        if not path.exists():
            return
        offset = path.stat().st_size
        self.bindings[umo] = self.create_binding(thread_id, path, offset)
        await self.kv.put_kv_data(f"codexbridge_thread_{umo}", thread_id)
        await self.kv.put_kv_data(f"codexbridge_offset_{umo}", offset)

    def _send_modes_for(self, umo: str) -> list[str]:
        current = self._app_server_modes.get(umo)
        modes = [current] if current else []
        for mode in ("proxy", "stdio"):
            if mode not in modes:
                modes.append(mode)
        return [mode for mode in modes if mode]

    def _server_for_mode(self, mode: str) -> AppServerLike | None:
        if mode == "proxy":
            return self.app_server
        if mode == "stdio":
            return self.fallback_app_server
        return None

    async def close(self) -> None:
        await self._close_app_servers()

    async def _close_app_servers(self) -> None:
        servers: dict[int, AppServerLike] = {}
        for server in self._active_app_servers.values():
            servers[id(server)] = server
        for server in (self.app_server, self.fallback_app_server):
            servers[id(server)] = server
        for server in servers.values():
            close = getattr(server, "close", None)
            if close:
                await close()
        self._active_app_servers.clear()
        self._app_server_modes.clear()

    @staticmethod
    def _remember_path(server: AppServerLike, thread_id: str, path: str) -> None:
        if not path:
            return
        remember_path = getattr(server, "remember_thread_path", None)
        if remember_path:
            remember_path(thread_id, path)

    async def poll_once(self) -> list[tuple[str, str]]:
        notifications: list[tuple[str, str]] = await self._process_app_server_events()
        for umo, binding in list(self.bindings.items()):
            if umo not in self.windows or not binding.rollout_path.exists():
                continue
            notification_count = len(notifications)
            size = binding.rollout_path.stat().st_size
            if size < binding.offset:
                binding.offset = 0
                binding.pending_offset = None
            if size == binding.offset:
                continue
            with binding.rollout_path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(binding.offset)
                chunk = f.read()
                binding.pending_offset = f.tell()
            for line in chunk.splitlines():
                event_type = await self._apply_jsonl_status_event(umo, line)
                if event_type == "task_complete":
                    state = self._turn_state(umo)
                    text = state.assistant_delta.strip()
                    state.assistant_delta = ""
                    if text and not self._should_suppress_bridge_output_text(text):
                        await self._emit_bridge_output(notifications, umo, binding.thread_id, "assistant", text)
                    continue
                role, text = self.extract_visible_text(line)
                if not text:
                    continue
                if role == "system":
                    await self._emit_bridge_output(notifications, umo, binding.thread_id, role, text)
                elif role == "user":
                    if self._is_outbound_echo(umo, text) or self._should_suppress_bridge_output_text(text):
                        continue
                    await self._emit_bridge_output(notifications, umo, binding.thread_id, role, text)
                elif role == "agent" and self._is_jsonl_agent_fragment(line):
                    if not self._should_suppress_bridge_output_text(text):
                        self._turn_state(umo).assistant_delta += text
                else:
                    if self._should_suppress_bridge_output_text(text):
                        continue
                    await self._emit_bridge_output(notifications, umo, binding.thread_id, role, text)
            if self.delivery_queue:
                await self.ack(umo)
            elif len(notifications) == notification_count:
                await self.ack(umo)
        return notifications

    async def _apply_jsonl_status_event(self, umo: str, line: str) -> str:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            return ""
        if item.get("type") != "event_msg":
            return ""
        payload = item.get("payload") or {}
        if not isinstance(payload, dict):
            return ""
        event_type = str(payload.get("type") or "")
        state = self._turn_state(umo)
        if event_type == "task_started":
            state.status = "running"
            state.active_turn_id = str(payload.get("turn_id") or payload.get("turnId") or "")
            state.last_event_at = self._now()
            state.last_error = ""
        elif event_type == "task_complete":
            state.status = "idle"
            state.active_turn_id = ""
            state.last_event_at = self._now()
            state.last_error = ""
            await self._flush_pending_inputs(umo)
        elif event_type == "turn_aborted":
            state.status = "aborted"
            state.active_turn_id = ""
            state.last_event_at = self._now()
            state.last_error = str(payload.get("reason") or "turn aborted")
        return event_type

    async def _emit_bridge_output(
        self,
        notifications: list[tuple[str, str]],
        umo: str,
        thread_id: str,
        role: str,
        text: str,
    ) -> None:
        text = text.strip()
        if not text:
            return
        display_role = "system" if role == "system" else "user" if role == "user" else "assistant"
        if not self._remember_output_key(umo, thread_id, display_role, text):
            return
        if display_role == "system":
            formatted = f"[Codex App/System]\n{text}"
        elif display_role == "user":
            formatted = f"[Codex App/User]\n{text}"
        else:
            formatted = f"[Codex App]\n{text}"
        dedupe_key = self._output_dedupe_key(umo, thread_id, display_role, text)
        if self.delivery_queue:
            await self.delivery_queue.enqueue(umo, "codexbridge", formatted, dedupe_key=dedupe_key)
        else:
            notifications.append((umo, formatted))

    def _remember_output_key(self, umo: str, thread_id: str, role: str, text: str) -> bool:
        key = self._output_dedupe_key(umo, thread_id, role, text)
        state = self._turn_state(umo)
        if key in state.recent_output_keys:
            return False
        state.recent_output_keys.append(key)
        del state.recent_output_keys[:-100]
        return True

    @staticmethod
    def _output_dedupe_key(umo: str, thread_id: str, role: str, text: str) -> str:
        normalized = re.sub(r"\s+", " ", text.strip())
        digest = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()
        return f"codexbridge:{umo}:{thread_id}:{role}:{digest}"

    @staticmethod
    def _is_jsonl_agent_fragment(line: str) -> bool:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            return False
        payload = item.get("payload") or {}
        if item.get("type") != "event_msg" or not isinstance(payload, dict):
            return False
        if payload.get("type") != "agent_message":
            return False
        phase = str(payload.get("phase") or "").lower()
        return phase not in {"final", "final_answer", "answer", "completed"}

    @staticmethod
    def _should_suppress_bridge_output_text(text: str) -> bool:
        normalized = text.strip()
        if not normalized:
            return True
        lowered = normalized.lower()
        if lowered.startswith("<environment_context>") and lowered.endswith("</environment_context>"):
            return True
        if lowered.startswith("# agents.md instructions") or lowered.startswith("<instructions>"):
            return True
        if "another language model started to solve this problem" in lowered:
            return True
        if "produced a summary of its thinking process" in lowered:
            return True
        if lowered.startswith("using `") and (
            "systematic-debugging" in lowered
            or "test-driven-development" in lowered
            or "using-superpowers" in lowered
            or "verification-before-completion" in lowered
        ):
            return True
        if "context compact" in lowered or "context transition" in lowered or "compaction" in lowered:
            return True
        if "<system" in lowered or "</system" in lowered or "<developer" in lowered or "</developer" in lowered:
            return True
        if "knowledge cutoff:" in lowered and "current date:" in lowered:
            return True
        if lowered.startswith("[bridge diagnostic]"):
            return True
        if lowered.startswith("bridge diagnostic:"):
            return True
        return False

    async def ack(self, umo: str) -> None:
        binding = self.bindings.get(umo)
        if not binding or binding.pending_offset is None:
            return
        binding.offset = binding.pending_offset
        binding.pending_offset = None
        await self.kv.put_kv_data(f"codexbridge_offset_{umo}", binding.offset)

    @staticmethod
    def extract_assistant_text(line: str) -> str:
        role, text = CodexAppBridge.extract_visible_text(line)
        return text if role in ("assistant", "agent") else ""

    @staticmethod
    def extract_visible_text(line: str) -> tuple[str, str]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            return "", ""
        payload = item.get("payload") or {}
        if item.get("type") == "event_msg" and payload.get("type") == "agent_message":
            message = payload.get("message")
            return "agent", message.strip() if isinstance(message, str) else ""
        if item.get("type") != "response_item":
            return "", ""
        if payload.get("type") == "function_call_output":
            output = payload.get("output")
            text = CodexAppBridge._extract_function_error_text(output)
            return ("system", text) if text else ("", "")
        if payload.get("type") != "message":
            return "", ""
        role = str(payload.get("role") or "")
        if role not in ("assistant", "agent", "user"):
            return "", ""
        parts = []
        for content in payload.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return role, "\n".join(parts).strip()

    @staticmethod
    def _extract_function_error_text(output) -> str:
        if isinstance(output, dict):
            text = output.get("text") or output.get("output") or output.get("error")
        else:
            text = output
        if not isinstance(text, str):
            return ""
        normalized = text.strip()
        lowered = normalized.lower()
        if "execution error" in lowered or "createprocessasuserw failed" in lowered:
            return normalized
        return ""

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

    def _remember_outbound(self, umo: str, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        values = self._outbound_texts.setdefault(umo, [])
        values.append(normalized)
        del values[:-20]

    def _is_outbound_echo(self, umo: str, text: str) -> bool:
        normalized = text.strip()
        values = self._outbound_texts.get(umo, [])
        if normalized in values:
            values.remove(normalized)
            return True
        return False
