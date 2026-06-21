from __future__ import annotations

import subprocess
from pathlib import Path


class SafeShell:
    """Small path-jailed command surface used by terminal mode."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve_inside(self, cwd: Path | str, target: str = ".") -> Path:
        base = Path(cwd).expanduser()
        if not base.is_absolute():
            base = self.root / base
        base = base.resolve()
        base.relative_to(self.root)

        raw_target = target.strip() if target else "."
        candidate = Path(raw_target).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (base / candidate).resolve()
        resolved.relative_to(self.root)
        return resolved

    def cd(self, cwd: Path | str, target: str) -> Path:
        resolved = self.resolve_inside(cwd, target)
        if not resolved.exists():
            raise ValueError(f"path does not exist: {target}")
        if not resolved.is_dir():
            raise ValueError(f"not a directory: {target}")
        return resolved

    def dir_list(self, cwd: Path | str, target: str = "") -> str:
        resolved = self.resolve_inside(cwd, target or ".")
        if not resolved.exists():
            raise ValueError(f"path does not exist: {target or '.'}")
        if not resolved.is_dir():
            return f"[FILE] {resolved.name}"

        entries = sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        if not entries:
            return "(empty)"
        lines = []
        for entry in entries:
            kind = "[DIR]" if entry.is_dir() else "[FILE]"
            lines.append(f"{kind} {entry.name}")
        return "\n".join(lines)

    def git_status(self, cwd: Path | str) -> str:
        resolved = self.resolve_inside(cwd, ".")
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(resolved),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        output = result.stdout.strip() or result.stderr.strip()
        return output or "(clean)"

