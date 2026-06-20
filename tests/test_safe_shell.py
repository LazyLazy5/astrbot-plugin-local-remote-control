from pathlib import Path

import pytest

from astrbot_plugin_local_remote_control.safe_shell import SafeShell


def test_jail_allows_root_child(tmp_path):
    shell = SafeShell(tmp_path)
    child = tmp_path / "child"
    child.mkdir()

    assert shell.resolve_inside(".", "child") == child.resolve()


def test_jail_rejects_parent_escape(tmp_path):
    shell = SafeShell(tmp_path)

    with pytest.raises(ValueError):
        shell.resolve_inside(".", "..")


def test_cd_updates_directory_inside_root(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    shell = SafeShell(tmp_path)

    assert shell.cd(tmp_path, "child") == child.resolve()


def test_dir_list_shows_directory_entries(tmp_path):
    (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    shell = SafeShell(tmp_path)

    output = shell.dir_list(tmp_path)

    assert "[FILE]" in output
    assert "file.txt" in output
    assert "[DIR]" in output
    assert "folder" in output
