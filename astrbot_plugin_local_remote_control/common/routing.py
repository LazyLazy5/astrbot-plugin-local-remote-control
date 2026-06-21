from __future__ import annotations

from typing import Any

DELIVERY_MAX_ITEMS_PER_TICK = 1


class ModeStore:
    def __init__(self, kv):
        self.kv = kv
        self.terminal_windows: set[str] = set()
        self.bridge_windows: set[str] = set()
        self.term_state_windows: set[str] = set()

    async def load(self):
        terminals = await self.kv.get_kv_data("terminal_windows", [])
        bridges = await self.kv.get_kv_data("codexbridge_windows", [])
        term_states = await self.kv.get_kv_data("term_state_windows", [])
        self.terminal_windows = {str(x) for x in terminals or []}
        self.bridge_windows = {str(x) for x in bridges or []}
        self.term_state_windows = {str(x) for x in term_states or []}

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

    async def remember_term_state_window(self, umo: str):
        self.term_state_windows.add(umo)
        await self.kv.put_kv_data("term_state_windows", sorted(self.term_state_windows))


def split_control_command(text: str) -> tuple[str, str]:
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


def stopped_plain_result(event, text: str):
    return event.plain_result(text).stop_event()


def split_message_chunks(text: str, *, limit: int = 1200) -> list[str]:
    if len(text) <= limit:
        return [text]
    return [text[i:i + limit].rstrip() for i in range(0, len(text), limit)]


def should_dispatch_terminal_text(text: str, state) -> bool:
    raw = (text or "").strip()
    if not raw:
        return True
    return True


def normalize_terminal_dispatch_text(text: str, state) -> str:
    raw = (text or "").strip()
    if not raw or raw.startswith("/"):
        return text
    command, action = split_control_command(raw)
    if command in ("pwd", "dir", "cd"):
        return command_text(command, action)
    if command == "git" and action == "status":
        return "/git status"
    return text


def bridge_should_dispatch_terminal_command(text: str) -> bool:
    command, action = split_control_command(text)
    if command != "term":
        return False
    return action.startswith(("agent ", "ls", "use ", "stop", "send ", "shell", "hapi "))


def command_text(command: str, action: str = "") -> str:
    action = (action or "").strip()
    return f"/{command} {action}".strip()


def action_from_full_command_text(text: str, command: str, fallback: str = "") -> str:
    parsed_command, parsed_action = split_control_command(text)
    if parsed_command == command:
        return parsed_action
    return (fallback or "").strip().lower()


def is_bridge_ack_text(text: str) -> bool:
    normalized = (text or "").strip()
    return normalized.startswith("sent to Codex App thread ") or normalized.startswith("sent to app-thread-")


def should_ignore_bridge_input_text(text: str) -> bool:
    normalized = (text or "").strip()
    return not normalized or is_bridge_ack_text(normalized)


def is_self_event(event) -> bool:
    try:
        sender_id = str(event.get_sender_id() or "").strip()
    except Exception:
        sender_id = ""
    try:
        self_id = str(event.get_self_id() or "").strip()
    except Exception:
        self_id = ""
    raw_message = getattr(getattr(event, "message_obj", None), "raw_message", {}) or {}
    if isinstance(raw_message, dict):
        if not self_id:
            self_id = str(raw_message.get("self_id", "") or "").strip()
        if not sender_id:
            sender = raw_message.get("sender") or {}
            if isinstance(sender, dict):
                sender_id = str(sender.get("user_id", "") or "").strip()
            if not sender_id:
                sender_id = str(raw_message.get("user_id", "") or "").strip()
        if str(raw_message.get("message_type", "")).lower() == "self":
            return True
    return bool(sender_id and self_id and sender_id == self_id)


def onebot_direct_payload(umo: str, messages: list[dict], self_id: str = "") -> dict | None:
    parts = str(umo or "").split(":", 2)
    if len(parts) < 3:
        return None
    platform, message_type, session_id = parts
    if not (platform.startswith("onebot") or "napcat" in platform.lower()):
        return None
    if not session_id.isdigit():
        return None
    params: dict[str, Any] = {"message": messages}
    if self_id:
        params["self_id"] = str(self_id)
    if message_type == "FriendMessage":
        params["user_id"] = int(session_id)
        return {"action": "send_private_msg", "params": params}
    if message_type == "GroupMessage":
        params["group_id"] = int(session_id)
        return {"action": "send_group_msg", "params": params}
    return None


async def deliver_due_items_once(
    delivery_queue,
    send_text,
    *,
    max_items: int = DELIVERY_MAX_ITEMS_PER_TICK,
) -> tuple[int, int]:
    due = await delivery_queue.due_items()
    failed_umos: set[str] = set()
    delivered = 0
    failed = 0
    processed = 0
    for item in due:
        if processed >= max_items:
            break
        if item.umo in failed_umos:
            continue
        try:
            await send_text(item.umo, item.text)
            await delivery_queue.mark_sent(item.id)
            delivered += 1
        except Exception as exc:
            failed += 1
            failed_umos.add(item.umo)
            await delivery_queue.mark_failed_umo(item.umo, str(exc))
        processed += 1
    return delivered, failed


async def send_delivery_text(context, message_chain_cls, umo: str, text: str, self_id: str = "") -> None:
    payload = onebot_direct_payload(umo, [{"type": "text", "data": {"text": text}}], self_id)
    if payload is not None:
        platform_name = str(umo).split(":", 1)[0]
        for platform in context.platform_manager.platform_insts:
            if platform.meta().id == platform_name:
                await platform.bot.call_action(payload["action"], **payload["params"])
                return
    chain = message_chain_cls().message(text)
    await context.send_message(umo, chain)
