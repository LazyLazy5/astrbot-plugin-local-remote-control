from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class HapiConfig:
    endpoint: str = ""
    access_token: str = ""
    proxy_url: str = ""
    jwt_lifetime: int = 900
    refresh_before: int = 180


class HapiHttpClient:
    def __init__(self, config: HapiConfig):
        self.config = config
        self._session = None
        self._jwt: str | None = None
        self._jwt_at = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def get_json(self, path: str, params: dict | None = None) -> dict:
        resp = await self._request("GET", path, params=params)
        return await self._json_response(resp)

    async def post_json(self, path: str, json: dict | None = None) -> dict:
        resp = await self._request("POST", path, json=json or {})
        return await self._json_response(resp)

    async def subscribe_events_raw(self):
        await self._ensure_session()
        token = await self._token()
        url = f"{self.config.endpoint.rstrip('/')}/api/events"
        resp = await self._session.get(url, params={"all": "1", "token": token}, timeout=None)
        resp.raise_for_status()
        return resp

    async def _request(self, method: str, path: str, **kwargs):
        await self._ensure_session()
        url = f"{self.config.endpoint.rstrip('/')}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {await self._token()}"
        resp = await self._session.request(method, url, headers=headers, timeout=15, **kwargs)
        if resp.status == 401:
            resp.release()
            async with self._lock:
                self._jwt = None
            headers["Authorization"] = f"Bearer {await self._token()}"
            resp = await self._session.request(method, url, headers=headers, timeout=15, **kwargs)
        return resp

    async def _ensure_session(self) -> None:
        if not self.config.endpoint or not self.config.access_token:
            raise RuntimeError("HAPI endpoint/access_token not configured")
        if self._session is not None and not self._session.closed:
            return
        import aiohttp

        self._session = aiohttp.ClientSession()

    async def _token(self) -> str:
        async with self._lock:
            age = time.time() - self._jwt_at
            if self._jwt and age < self.config.jwt_lifetime - self.config.refresh_before:
                return self._jwt
            await self._ensure_session()
            url = f"{self.config.endpoint.rstrip('/')}/api/auth"
            resp = await self._session.post(url, json={"accessToken": self.config.access_token}, timeout=10)
            data = await self._json_response(resp)
            token = data.get("token")
            if not token:
                raise RuntimeError("HAPI auth returned no token")
            self._jwt = str(token)
            self._jwt_at = time.time()
            return self._jwt

    @staticmethod
    async def _json_response(resp) -> dict:
        try:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {body[:300]}")
            return await resp.json()
        finally:
            resp.release()


class HapiClient:
    def __init__(self, config: HapiConfig, *, raw_client: Any | None = None):
        self.config = config
        self.raw = raw_client if raw_client is not None else HapiHttpClient(config)

    async def close(self) -> None:
        close = getattr(self.raw, "close", None)
        if close:
            await close()

    def update_config(self, config: HapiConfig) -> None:
        self.config = config
        if hasattr(self.raw, "config"):
            self.raw.config = config
        if hasattr(self.raw, "_jwt"):
            self.raw._jwt = None
        if hasattr(self.raw, "_jwt_at"):
            self.raw._jwt_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.config.endpoint and self.config.access_token)

    @staticmethod
    def not_configured_message() -> str:
        return (
            "HAPI 未配置或运行中的插件实例尚未加载新配置。"
            "请确认 hapi_endpoint/access_token 已保存，然后执行 /term hapi reload；"
            "再用 /term hapi status 查看认证和 runner 状态。"
        )

    async def diagnostic_status(self) -> str:
        token = self.config.access_token or ""
        lines = [
            f"endpoint: {self.config.endpoint or '-'}",
            f"token: {'set len=' + str(len(token)) if token else 'missing'}",
        ]
        if ":" in token:
            namespace = token.split(":", 1)[1]
            lines.append(f"namespace: {namespace or '-'}")
        if not self.configured:
            lines.append("configured: no")
            lines.append("hint: set hapi_endpoint/access_token or run /term hapi reload after saving config")
            return "\n".join(lines)
        try:
            machines = await self.fetch_machines()
        except Exception as exc:
            lines.append(f"auth: failed: {exc}")
            return "\n".join(lines)
        lines.append("auth: ok")
        lines.append(f"machines: {len(machines)}")
        if machines:
            runner = machines[0].get("runnerState") or {}
            lines.append(f"runner: {runner.get('status') or 'unknown'}")
            namespace = machines[0].get("namespace")
            if namespace:
                lines.append(f"machine_namespace: {namespace}")
            roots = _workspace_roots(machines[0])
            if roots:
                lines.append("workspace_roots:")
                lines.extend(f"- {root}" for root in roots)
        return "\n".join(lines)

    async def status(self) -> tuple[bool, str]:
        if not self.configured:
            return False, self.not_configured_message()
        try:
            sessions = await self.list_sessions()
            if not sessions[0]:
                return False, str(sessions[1])
            machines = await self.fetch_machines()
            return True, f"HAPI available, machines: {len(machines)}, sessions: {len(sessions[1])}"
        except Exception as exc:
            return False, f"HAPI status failed: {exc}"

    async def fetch_machines(self) -> list[dict]:
        data = await self.raw.get_json("/api/machines")
        machines = data.get("machines", [])
        return [m for m in machines if m.get("active", True)]

    async def list_sessions(self, flavor: str | None = None) -> tuple[bool, str | list[dict]]:
        if not self.configured:
            return False, self.not_configured_message()
        try:
            data = await self.raw.get_json("/api/sessions")
            sessions = data.get("sessions", [])
            if flavor:
                flavor = flavor.lower()
                sessions = [
                    s for s in sessions
                    if str((s.get("metadata") or {}).get("flavor") or "").lower() == flavor
                ]
            return True, sessions
        except Exception as exc:
            return False, f"HAPI list failed: {exc}"

    async def spawn_session(self, flavor: str, directory: Path) -> tuple[bool, str, str | None]:
        if not self.configured:
            return False, self.not_configured_message(), None
        try:
            machines = await self.fetch_machines()
            if not machines:
                return False, "No active HAPI runner machine", None
            machine_id = str(machines[0].get("id") or machines[0].get("machineId") or "")
            if not machine_id:
                return False, "Active HAPI machine has no id", None
            body = {
                "directory": str(directory),
                "agent": flavor,
                "sessionType": "simple",
                "yolo": False,
            }
            roots = _workspace_roots(machines[0])
            data = await self.raw.post_json(f"/api/machines/{machine_id}/spawn", json=body)
            if data.get("type") == "success" or data.get("sessionId"):
                sid = str(data.get("sessionId") or data.get("id"))
                return True, f"created {flavor} {sid}", sid
            message = str(data.get("message") or data)
            return False, f"HAPI spawn failed: {_actionable_spawn_error(message, roots)}", None
        except Exception as exc:
            return False, f"HAPI spawn failed: {_actionable_spawn_error(str(exc))}", None

    async def send_message(self, session_id: str, text: str) -> tuple[bool, str]:
        if not self.configured:
            return False, self.not_configured_message()
        try:
            await self.raw.post_json(f"/api/sessions/{session_id}/messages", json={"text": text})
            return True, f"sent {session_id}"
        except Exception as exc:
            return False, f"HAPI send failed: {exc}"

    async def abort_session(self, session_id: str) -> tuple[bool, str]:
        try:
            await self.raw.post_json(f"/api/sessions/{session_id}/abort", json={})
            return True, f"stopped {session_id}"
        except Exception as exc:
            return False, f"HAPI stop failed: {exc}"

    async def approve_permission(self, session_id: str, request_id: str, answers: dict | None = None) -> tuple[bool, str]:
        body = {"answers": answers} if answers else {}
        try:
            await self.raw.post_json(f"/api/sessions/{session_id}/permissions/{request_id}/approve", json=body)
            return True, "approved"
        except Exception as exc:
            return False, f"HAPI approve failed: {exc}"

    async def deny_permission(self, session_id: str, request_id: str) -> tuple[bool, str]:
        try:
            await self.raw.post_json(f"/api/sessions/{session_id}/permissions/{request_id}/deny", json={})
            return True, "denied"
        except Exception as exc:
            return False, f"HAPI deny failed: {exc}"

    async def fetch_messages(self, session_id: str, limit: int = 50) -> list[dict]:
        data = await self.raw.get_json(f"/api/sessions/{session_id}/messages", params={"limit": limit})
        return data.get("messages", [])


class HapiTermBackend:
    def __init__(self, client: HapiClient):
        self.client = client
        self._window_sessions: dict[str, str] = {}

    def session_for(self, umo: str) -> str | None:
        return self._window_sessions.get(umo)

    def bind_session(self, umo: str, session_id: str) -> None:
        self._window_sessions[umo] = session_id

    async def start_agent(self, umo: str, flavor: str, directory: Path) -> tuple[bool, str]:
        ok, message, sid = await self.client.spawn_session(flavor, directory)
        if ok and sid:
            self.bind_session(umo, sid)
        return ok, message

    async def use_session(self, umo: str, target: str) -> tuple[bool, str]:
        ok, data = await self.client.list_sessions()
        if not ok:
            return False, str(data)
        sessions = data if isinstance(data, list) else []
        chosen = _choose_session(sessions, target)
        if isinstance(chosen, str):
            return False, chosen
        sid = str(chosen.get("id") or "")
        self.bind_session(umo, sid)
        return True, f"using {sid}"

    async def send(self, umo: str, text: str) -> tuple[bool, str]:
        sid = self.session_for(umo)
        if not sid:
            return False, "No selected HAPI session. Use /term agent codex|cc or /term use."
        return await self.client.send_message(sid, text)

    async def stop(self, umo: str) -> tuple[bool, str]:
        sid = self.session_for(umo)
        if not sid:
            return False, "No selected HAPI session."
        return await self.client.abort_session(sid)


def _choose_session(sessions: list[dict], target: str) -> dict | str:
    if target.isdigit():
        index = int(target)
        if 1 <= index <= len(sessions):
            return sessions[index - 1]
        return "session index out of range"
    matches = [s for s in sessions if str(s.get("id", "")).startswith(target)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return "matched multiple sessions; use a longer id prefix"
    return "session not found"


def _actionable_spawn_error(message: str, roots: list[str] | None = None) -> str:
    if "outside this machine's workspace roots" not in message:
        return message
    root_lines = ""
    if roots:
        root_lines = "\nworkspace_roots:\n" + "\n".join(f"- {root}" for root in roots)
    return (
        f"{message}\n"
        "当前路径不在 HAPI Runner 允许的 workspace roots 内。"
        "请用 /term agent codex <路径> 或 /term agent cc <路径> 指向 Runner 允许的项目目录，"
        "或者把本插件配置里的 work_dir 改成该项目目录后重载插件。"
        f"{root_lines}"
    )


def _workspace_roots(machine: dict) -> list[str]:
    for key in ("workspaceRoots", "workspace_roots", "roots", "workspaceRoot"):
        value = machine.get(key)
        if isinstance(value, list):
            return [str(item) for item in value if str(item)]
        if isinstance(value, str) and value:
            return [value]
    runner = machine.get("runnerState") or {}
    if isinstance(runner, dict):
        for key in ("workspaceRoots", "workspace_roots", "roots", "workspaceRoot"):
            value = runner.get(key)
            if isinstance(value, list):
                return [str(item) for item in value if str(item)]
            if isinstance(value, str) and value:
                return [value]
    return []


def extract_hapi_message_text(message: dict) -> tuple[str, str]:
    content = message.get("content") or {}
    role = str(content.get("role") or message.get("role") or "")
    text = ""
    if isinstance(content, dict):
        raw = content.get("text") or content.get("message") or content.get("content")
        if raw is None and isinstance(content.get("parts"), list):
            pieces = []
            for part in content["parts"]:
                if isinstance(part, str):
                    pieces.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    pieces.append(part["text"])
            raw = "\n".join(pieces)
        if isinstance(raw, str):
            text = raw
        elif raw is not None:
            text = json.dumps(raw, ensure_ascii=False)
    return role, text.strip()


def message_seq(message: dict) -> int:
    try:
        return int(message.get("seq") or message.get("sequence") or 0)
    except (TypeError, ValueError):
        return 0
