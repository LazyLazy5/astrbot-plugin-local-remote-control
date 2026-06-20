from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class HapiBridge:
    def __init__(
        self,
        *,
        connector_plugin_name: str = "astrbot_plugin_hapi_connector",
        connector_config_path: Path | None = None,
        plugin_instance: Any = None,
    ):
        self.connector_plugin_name = connector_plugin_name
        self.connector_config_path = connector_config_path
        self._client = getattr(plugin_instance, "client", None) if plugin_instance else None
        self._session_ops = None
        self._client_owner = False

    async def init(self):
        try:
            from astrbot_plugin_hapi_connector import session_ops

            self._session_ops = session_ops
        except Exception:
            self._session_ops = None

        if self._client is not None or self._session_ops is None:
            return

        config_path = self.connector_config_path
        if config_path is None:
            config_path = Path.home() / ".astrbot" / "data" / "config" / "astrbot_plugin_hapi_connector_config.json"
        if not config_path.exists():
            return

        try:
            from astrbot_plugin_hapi_connector.hapi_client import AsyncHapiClient

            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            endpoint = str(cfg.get("hapi_endpoint") or "").strip()
            token = str(cfg.get("access_token") or "").strip()
            if not endpoint or not token:
                return
            client = AsyncHapiClient(
                endpoint=endpoint,
                access_token=token,
                proxy_url=str(cfg.get("proxy_url") or "") or None,
                jwt_lifetime=int(cfg.get("jwt_lifetime") or 900),
                refresh_before=int(cfg.get("refresh_before_expiry") or 180),
            )
            await client.init()
            self._client = client
            self._client_owner = True
        except Exception:
            self._client = None
            self._client_owner = False

    async def close(self):
        if self._client_owner and self._client is not None:
            close = getattr(self._client, "close", None)
            if close:
                await close()

    @property
    def available(self) -> bool:
        return self._client is not None and self._session_ops is not None

    async def status(self) -> tuple[bool, str]:
        if not self.available:
            return False, "HAPI connector unavailable"
        try:
            sessions = await self._session_ops.fetch_sessions(self._client)
            return True, f"HAPI available, sessions: {len(sessions)}"
        except Exception as exc:
            return False, f"HAPI status failed: {exc}"

    async def list_sessions(self, flavor: str | None = None) -> tuple[bool, str | list[dict]]:
        if not self.available:
            return False, "HAPI connector unavailable"
        try:
            sessions = await self._session_ops.fetch_sessions(self._client)
            if flavor:
                flavor = flavor.strip().lower()
                sessions = [
                    s for s in sessions
                    if str((s.get("metadata") or {}).get("flavor") or "").lower() == flavor
                ]
            return True, sessions
        except Exception as exc:
            return False, f"HAPI list failed: {exc}"

    async def spawn_session(self, flavor: str, directory: Path) -> tuple[bool, str, str | None]:
        if not self.available:
            return False, "HAPI connector unavailable", None
        try:
            machines = await self._session_ops.fetch_machines(self._client)
            if not machines:
                return False, "No active HAPI runner machine", None
            machine_id = machines[0].get("id") or machines[0].get("machineId")
            if not machine_id:
                return False, "Active HAPI machine has no id", None
            return await self._session_ops.spawn_session(
                self._client,
                str(machine_id),
                str(directory),
                flavor,
                "simple",
                False,
            )
        except Exception as exc:
            return False, f"HAPI spawn failed: {exc}", None

    async def send_message(self, session_id: str, text: str) -> tuple[bool, str]:
        if not self.available:
            return False, "HAPI connector unavailable"
        try:
            return await self._session_ops.send_message(self._client, session_id, text)
        except Exception as exc:
            return False, f"HAPI send failed: {exc}"

