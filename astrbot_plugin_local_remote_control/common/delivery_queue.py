from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Callable


QUEUE_KEY = "delivery_queue"
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
FAILURE_DELAYS = (30, 60, 120, 240, 300)
WEIXIN_REFRESH_RECOVERY_SPACING = 2.0


@dataclass
class DeliveryItem:
    id: str
    umo: str
    channel: str
    text: str
    dedupe_key: str
    attempts: int = 0
    next_retry_at: float = 0.0
    last_error: str = ""


def _clean_text(text: str) -> str:
    return CONTROL_CHARS.sub("", text or "").strip()


def split_delivery_text(text: str, *, limit: int = 850) -> list[str]:
    text = _clean_text(text)
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    base_label = "Message"
    first_line, sep, rest = text.partition("\n")
    if first_line.startswith("[") and first_line.endswith("]") and sep:
        base_label = first_line[1:-1]
        text = rest.strip()

    body_limit = max(100, limit - 40)
    chunks: list[str] = []
    remaining = text
    while remaining:
        part = remaining[:body_limit]
        split_at = max(part.rfind("\n\n"), part.rfind("\n"), part.rfind(" "))
        if split_at < body_limit // 2:
            split_at = body_limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    total = len(chunks)
    labelled: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        labelled.append(f"[{base_label} {index}/{total}]\n{chunk}")
    return labelled


class DeliveryQueue:
    def __init__(self, kv, *, now: Callable[[], float] | None = None):
        self.kv = kv
        self._now = now or time.time
        self.items: list[DeliveryItem] = []
        self.needs_user_refresh: set[str] = set()
        self._loaded = False

    async def load(self) -> None:
        raw = await self.kv.get_kv_data(QUEUE_KEY, [])
        refresh_raw = await self.kv.get_kv_data(f"{QUEUE_KEY}_needs_user_refresh", [])
        self.needs_user_refresh = {str(x) for x in refresh_raw or []}
        self.items = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            try:
                self.items.append(
                    DeliveryItem(
                        id=str(item.get("id") or uuid.uuid4().hex),
                        umo=str(item.get("umo") or ""),
                        channel=str(item.get("channel") or ""),
                        text=str(item.get("text") or ""),
                        dedupe_key=str(item.get("dedupe_key") or ""),
                        attempts=int(item.get("attempts") or 0),
                        next_retry_at=float(item.get("next_retry_at") or 0),
                        last_error=str(item.get("last_error") or ""),
                    )
                )
            except (TypeError, ValueError):
                continue
        self._loaded = True

    async def enqueue(self, umo: str, channel: str, text: str, *, dedupe_key: str = "") -> DeliveryItem | None:
        await self._ensure_loaded()
        text = _clean_text(text)
        if not text:
            return None
        chunks = split_delivery_text(text)
        if len(chunks) > 1:
            created: DeliveryItem | None = None
            total = len(chunks)
            for index, chunk in enumerate(chunks, 1):
                chunk_key = f"{dedupe_key}:{index}/{total}" if dedupe_key else ""
                item = await self.enqueue(umo, channel, chunk, dedupe_key=chunk_key)
                created = created or item
            return created
        dedupe_key = dedupe_key or f"{channel}:{umo}:{hash(text)}"
        if any(item.dedupe_key == dedupe_key for item in self.items):
            return None
        item = DeliveryItem(
            id=uuid.uuid4().hex,
            umo=umo,
            channel=channel,
            text=text,
            dedupe_key=dedupe_key,
        )
        self.items.append(item)
        await self._save()
        return item

    async def due_items(self, *, channel: str | None = None) -> list[DeliveryItem]:
        await self._ensure_loaded()
        now = self._now()
        blocked: set[tuple[str, str]] = set()
        due: list[DeliveryItem] = []
        for item in self.items:
            if channel is not None and item.channel != channel:
                continue
            if item.umo in self.needs_user_refresh:
                continue
            key = (item.umo, item.channel)
            if key in blocked:
                continue
            if item.next_retry_at <= now:
                due.append(item)
            else:
                blocked.add(key)
        return due

    async def mark_sent(self, item_id: str) -> None:
        await self._ensure_loaded()
        self.items = [item for item in self.items if item.id != item_id]
        await self._save()

    async def mark_failed(self, item_id: str, error: str) -> None:
        await self._ensure_loaded()
        now = self._now()
        for item in self.items:
            if item.id != item_id:
                continue
            item.attempts += 1
            item.last_error = str(error)[:300]
            if _is_weixin_ret_minus_two(item.umo, str(error)):
                self.needs_user_refresh.add(item.umo)
            else:
                delay = _failure_delay(item.attempts)
                item.next_retry_at = now + delay
            break
        await self._save()

    async def mark_failed_umo(self, umo: str, error: str) -> None:
        await self._ensure_loaded()
        now = self._now()
        changed = False
        for item in self.items:
            if item.umo != umo:
                continue
            item.attempts += 1
            item.last_error = str(error)[:300]
            if _is_weixin_ret_minus_two(item.umo, str(error)):
                self.needs_user_refresh.add(item.umo)
            else:
                delay = _failure_delay(item.attempts)
                item.next_retry_at = now + delay
            changed = True
        if changed:
            await self._save()

    async def mark_user_refreshed(self, umo: str) -> bool:
        await self._ensure_loaded()
        if umo not in self.needs_user_refresh:
            return False
        self.needs_user_refresh.discard(umo)
        now = self._now()
        channel_counts: dict[str, int] = {}
        for item in self.items:
            if item.umo == umo:
                index = channel_counts.get(item.channel, 0)
                item.next_retry_at = 0 if index == 0 else now + WEIXIN_REFRESH_RECOVERY_SPACING * index
                channel_counts[item.channel] = index + 1
        await self._save()
        return True

    async def clear(self, umo: str, channel: str | None = None) -> int:
        await self._ensure_loaded()
        before = len(self.items)
        self.items = [
            item for item in self.items
            if not (item.umo == umo and (channel is None or item.channel == channel))
        ]
        removed = before - len(self.items)
        if removed:
            await self._save()
        return removed

    async def retry(self, umo: str | None = None, channel: str | None = None) -> int:
        await self._ensure_loaded()
        count = 0
        for item in self.items:
            if umo is not None and item.umo != umo:
                continue
            if channel is not None and item.channel != channel:
                continue
            item.next_retry_at = 0
            count += 1
        if count:
            await self._save()
        return count

    def status(self, umo: str, channel: str | None = None) -> dict:
        matching = [
            item for item in self.items
            if item.umo == umo and (channel is None or item.channel == channel)
        ]
        last_error = ""
        next_retry_at = 0.0
        if matching:
            errored = [item for item in matching if item.last_error]
            last = errored[-1] if errored else matching[-1]
            last_error = last.last_error
            next_retry_at = min((item.next_retry_at for item in matching if item.next_retry_at), default=0.0)
        return {
            "queue_length": len(matching),
            "last_error": last_error,
            "next_retry_at": next_retry_at,
            "needs_user_refresh": umo in self.needs_user_refresh,
        }

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            await self.load()

    async def _save(self) -> None:
        await self.kv.put_kv_data(QUEUE_KEY, [asdict(item) for item in self.items])
        await self.kv.put_kv_data(f"{QUEUE_KEY}_needs_user_refresh", sorted(self.needs_user_refresh))


def _failure_delay(attempts: int) -> int:
    index = max(0, min(attempts - 1, len(FAILURE_DELAYS) - 1))
    return FAILURE_DELAYS[index]


def _is_weixin_ret_minus_two(umo: str, error: str) -> bool:
    return str(umo).startswith("weixin_") and "ret=-2" in str(error)
