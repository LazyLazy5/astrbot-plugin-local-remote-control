import asyncio
import os

from astrbot_plugin_local_remote_control.terminal_session import PersistentTerminalSession, default_shell_command


def run(coro):
    return asyncio.run(coro)


async def _exec_echo(tmp_path):
    session = PersistentTerminalSession(default_shell_command(), cwd=tmp_path)
    await session.start()
    try:
        await session.send_line("echo terminal_ready")
        return await session.collect_output(timeout=3.0)
    finally:
        await session.close()


def test_persistent_terminal_session_executes_shell_commands(tmp_path):
    output = run(_exec_echo(tmp_path))

    assert "terminal_ready" in output


def test_default_shell_command_uses_cmd_on_windows():
    command = default_shell_command()

    if os.name == "nt":
        assert command[0].lower().endswith("cmd.exe")
    else:
        assert command[0] in {"/bin/sh", "sh"}
