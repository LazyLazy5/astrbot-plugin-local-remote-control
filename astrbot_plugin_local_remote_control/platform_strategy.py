from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformStrategy:
    platform: str
    strategy: str
    note: str = ""


def platform_strategy_from_umo(umo: str) -> PlatformStrategy:
    platform_id = (umo or "").split(":", 1)[0]
    platform = _platform_type_from_id(platform_id)
    if platform == "aiocqhttp":
        return PlatformStrategy(platform, "onebot")
    if platform == "qq_official":
        return PlatformStrategy(
            platform,
            "restricted_qq_official",
            "QQ 官方 WebSocket 有主动消息额度限制，建议使用 OneBot/NapCat/Lagrange 作为持续推送通道。",
        )
    if platform == "weixin_oc":
        return PlatformStrategy(
            platform,
            "restricted_weixin",
            "微信 weixin_oc 可能触发 ret=-2，需要用户发消息刷新 context_token。",
        )
    return PlatformStrategy(platform or "-", "generic")


def format_platform_status(umo: str) -> str:
    strategy = platform_strategy_from_umo(umo)
    lines = [
        f"platform: {strategy.platform}",
        f"strategy: {strategy.strategy}",
    ]
    if strategy.note:
        lines.append(f"note: {strategy.note}")
    return "\n".join(lines)


def _platform_type_from_id(platform_id: str) -> str:
    value = (platform_id or "").strip().lower()
    if value.startswith("onebot") or "napcat" in value:
        return "aiocqhttp"
    if value.startswith("default_") or value.startswith("qq_official"):
        return "qq_official"
    if value.startswith("weixin_"):
        return "weixin_oc"
    return value
