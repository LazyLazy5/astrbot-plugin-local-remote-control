from astrbot_plugin_local_remote_control.common.platform_strategy import platform_strategy_from_umo


def test_aiocqhttp_is_onebot_strategy():
    strategy = platform_strategy_from_umo("onebot_napcat:FriendMessage:12345")

    assert strategy.platform == "aiocqhttp"
    assert strategy.strategy == "onebot"
    assert strategy.note == ""


def test_qq_official_is_restricted_strategy_with_note():
    strategy = platform_strategy_from_umo("default_1904439708:FriendMessage:user-openid")

    assert strategy.platform == "qq_official"
    assert strategy.strategy == "restricted_qq_official"
    assert "主动消息额度限制" in strategy.note


def test_aiocqhttp_parenthesized_platform_is_onebot_strategy():
    strategy = platform_strategy_from_umo("onebot_napcat(aiocqhttp):FriendMessage:12345")

    assert strategy.platform == "aiocqhttp"
    assert strategy.strategy == "onebot"


def test_format_platform_status_includes_note_for_restricted_channel():
    from astrbot_plugin_local_remote_control.common.platform_strategy import format_platform_status

    text = format_platform_status("default_1904439708:FriendMessage:user-openid")

    assert "platform: qq_official" in text
    assert "strategy: restricted_qq_official" in text
    assert "主动消息额度限制" in text
