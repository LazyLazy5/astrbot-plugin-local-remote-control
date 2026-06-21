from astrbot_plugin_local_remote_control.codexbridge.controller import CodexBridgeController
from astrbot_plugin_local_remote_control.common.delivery_queue import DeliveryQueue
from astrbot_plugin_local_remote_control.common.platform_strategy import platform_strategy_from_umo
from astrbot_plugin_local_remote_control.term.controller import TermController
from astrbot_plugin_local_remote_control.term.commands import TerminalState


def test_internal_subprojects_expose_expected_interfaces():
    assert DeliveryQueue
    assert platform_strategy_from_umo
    assert TerminalState

    for name in ("handle_term_command", "intercept_message"):
        assert hasattr(TermController, name)

    for name in ("handle_command", "intercept_message", "send_to_bound_thread"):
        assert hasattr(CodexBridgeController, name)
