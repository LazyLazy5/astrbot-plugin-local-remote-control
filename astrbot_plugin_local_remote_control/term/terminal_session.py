from __future__ import annotations

import asyncio
import codecs
import locale
import os
from pathlib import Path


def default_shell_command() -> list[str]:
    if os.name == "nt":
        return ["cmd.exe", "/Q", "/K", "prompt $P$G"]
    return ["/bin/sh" if Path("/bin/sh").exists() else "sh"]


class PersistentTerminalSession:
    """A small persistent shell process backed by stdin/stdout pipes."""

    def __init__(self, command: list[str], cwd: Path | str):
        if not command:
            raise ValueError("terminal command is empty")
        self.command = command
        self.cwd = Path(cwd).expanduser().resolve()
        self.process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._buffer: list[str] = []
        self._lock = asyncio.Lock()
        self._encoding = locale.getpreferredencoding(False) or "utf-8"

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self) -> None:
        if self.is_running:
            return
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=str(self.cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def send_line(self, text: str) -> None:
        if not self.is_running or self.process is None or self.process.stdin is None:
            raise RuntimeError("terminal is not running")
        data = (text.rstrip("\r\n") + os.linesep).encode(self._encoding, errors="replace")
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def collect_output(self, *, timeout: float = 1.0, idle: float = 0.15, limit: int = 6000) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        pieces: list[str] = []
        last_seen = 0.0
        while asyncio.get_running_loop().time() < deadline:
            chunk = await self.drain_output()
            if chunk:
                pieces.append(chunk)
                last_seen = asyncio.get_running_loop().time()
            if pieces and last_seen and asyncio.get_running_loop().time() - last_seen >= idle:
                break
            if self.process is not None and self.process.returncode is not None:
                break
            await asyncio.sleep(0.05)
        text = "".join(pieces).strip()
        if len(text) > limit:
            return text[:limit] + "\n...[output truncated]"
        return text

    async def drain_output(self, *, limit: int = 6000) -> str:
        async with self._lock:
            if not self._buffer:
                return ""
            text = "".join(self._buffer)
            self._buffer.clear()
        text = text.strip()
        if len(text) > limit:
            return text[:limit] + "\n...[output truncated]"
        return text

    async def close(self) -> None:
        process = self.process
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

    async def _reader_loop(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        decoder = codecs.getincrementaldecoder(self._encoding)(errors="replace")
        while True:
            chunk = await self.process.stdout.read(4096)
            if not chunk:
                tail = decoder.decode(b"", final=True)
                if tail:
                    await self._append(tail)
                break
            await self._append(decoder.decode(chunk))

    async def _append(self, text: str) -> None:
        if not text:
            return
        async with self._lock:
            self._buffer.append(text)
