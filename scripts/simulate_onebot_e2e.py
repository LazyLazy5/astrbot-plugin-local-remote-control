from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import websockets

DEFAULT_ACTION_TIMEOUT = 75.0


@dataclass
class OneBotHarness:
    url: str
    self_id: int
    user_id: int
    nickname: str
    timeout: float
    command_timeout: float = 8.0
    role: str = "universal"
    dual_role: bool = False
    thread_index: int = 0
    thread_target: str = ""
    skip_bridge_send: bool = False
    status_after_probe: bool = False
    keep_bridge_on: bool = False
    accept_state_observation: bool = True
    message_id: int = 1000
    sent_actions: list[dict[str, Any]] = field(default_factory=list)

    async def run(self) -> int:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if self.dual_role:
            return await self._run_dual_role()
        headers = self._headers(self.role)
        async with websockets.connect(self.url, additional_headers=headers, max_size=16 * 1024 * 1024) as ws:
            readers = [asyncio.create_task(self._read_loop(ws))]
            try:
                results = await self._run_sequence(ws)
                self._print_report(results)
                return 0 if all(ok for _, ok, _ in results) else 1
            finally:
                await self._cancel_readers(readers)

    async def _run_dual_role(self) -> int:
        async with (
            websockets.connect(self.url, additional_headers=self._headers("api"), max_size=16 * 1024 * 1024) as api_ws,
            websockets.connect(self.url, additional_headers=self._headers("event"), max_size=16 * 1024 * 1024) as event_ws,
        ):
            readers = [
                asyncio.create_task(self._read_loop(api_ws)),
                asyncio.create_task(self._read_loop(event_ws)),
            ]
            try:
                await asyncio.sleep(0.5)
                results = await self._run_sequence(event_ws)
                self._print_report(results)
                return 0 if all(ok for _, ok, _ in results) else 1
            finally:
                await self._cancel_readers(readers)

    async def _run_sequence(self, ws) -> list[tuple[str, bool, str]]:
        results = []
        async def step(coro):
            result = await coro
            results.append(result)
            self._print_result(result)
            return result

        await step(self._send_and_expect(ws, "/codexbridge off", "Codex Bridge off"))
        await step(self._send_and_expect(ws, "/codexbridge queue clear", "Codex Bridge queue cleared"))
        await step(self._send_and_expect(ws, "/codexbridge retry", "Codex Bridge retry queued"))
        await step(self._send_and_expect_any(ws, "/codexbridge probe", ["read-write", "read-only"]))
        await step(self._send_and_expect(ws, "/term off", "Terminal mode off"))
        await step(self._send_and_expect(ws, "/term queue clear", "Terminal queue cleared"))
        await step(self._send_and_expect(ws, "/term retry", "Terminal retry queued"))
        await step(self._send_and_expect(ws, "/term status", "Terminal: off"))
        await step(self._send_and_expect(ws, "/term hapi status", "endpoint:"))
        await step(self._send_and_expect_any(ws, "/term hapi reload", ["HAPI config reloaded", "HAPI config reload failed"]))
        await step(self._send_and_expect_any(ws, "/term ls", ["(no sessions)", ". ["]))
        await step(self._send_and_expect_any(ws, "/term use 1", ["已选择", "未找到 session", "session inactive"]))
        await step(self._send_and_expect_any(ws, "/term stop", ["No selected HAPI session.", "aborted", "stopped", "stop"]))
        await step(self._send_and_expect_any(ws, "/term send pre-bridge", ["No selected session", "Codex Bridge is read-only", "sent to Codex App thread"]))
        await step(self._send_and_expect(ws, "/term on", "Terminal mode on"))
        await step(self._send_and_expect_any(ws, "/term status", ["Terminal: on", "Terminal: off"]))
        await step(self._send_and_expect_any(ws, "/pwd", ["workspace", ":\\"]))
        await step(self._send_and_expect_any(ws, "/dir", ["<DIR>", "Directory", "目录", "[DIR]", "[FILE]", "(empty)"]))
        await step(self._send_and_expect_any(ws, "/cd .", ["cwd:", ":\\"]))
        await step(self._send_and_expect_any(ws, "/git status", ["git status", "fatal", "working tree", "nothing to commit", "??", " M "]))
        await step(self._send_and_expect(ws, "codex", "不是 TTY"))
        await step(self._send_and_expect(ws, "你好", "shell backend 只支持"))
        await step(self._send_and_expect_any(ws, "/unknown", ["未识别命令", "shell backend 只支持"]))
        await step(self._send_and_expect_any(ws, "/term agent bad", ["agent 只能是 codex 或 cc/claude"]))
        await step(self._send_and_expect_any(ws, "/term agent codex Z:\\definitely_missing_path", ["not a directory", "Directory is outside"]))
        await step(self._send_and_expect(ws, "/term hapi status", "endpoint:"))
        await step(self._send_and_expect(ws, "/term off", "Terminal mode off"))
        await step(self._send_and_expect(ws, "/term status", "Terminal: off"))
        await step(self._send_and_expect(ws, "/codexbridge on", "Codex Bridge on"))
        await step(self._send_and_expect(ws, "/codexbridge status", "Codex Bridge: on"))
        if self.thread_index or self.thread_target:
            await step(self._send_and_expect_any(ws, "/codexbridge ls", ["1.", self.thread_target], timeout=75.0))
            target = self.thread_target or str(self.thread_index)
            await step(self._send_and_expect_any(ws, f"/codexbridge use {target}", ["Codex Bridge using"], timeout=75.0))
            await step(self._send_and_expect(ws, "/codexbridge queue clear", "Codex Bridge queue cleared"))
        await step(self._send_and_expect_any(ws, "/term status", ["Codex Bridge: on", "Terminal: off"]))
        await step(self._send_and_expect(ws, "/term queue clear", "Terminal queue cleared"))
        await step(self._send_and_expect(ws, "/codexbrideg status", "Codex Bridge: on"))
        blank_before = len(self.sent_actions)
        await self._send_private_message(ws, "   ")
        await asyncio.sleep(2.0)
        blank_after = len(self.sent_actions)
        result = ("blank_event_no_outbound", blank_after == blank_before, f"{blank_after - blank_before} outbound action(s)")
        results.append(result)
        self._print_result(result)
        if not self.skip_bridge_send:
            probe_text = "bridge diagnostic: reply exactly BRIDGE_E2E_OK"
            actions_before_probe = len(self.sent_actions)
            state_probe = self._state_probe()
            await self._send_private_message(ws, probe_text)
            await asyncio.sleep(self.timeout)
            new_texts = self._outbound_texts()[actions_before_probe:]
            has_write_failure = any("Codex App write failed" in text for text in new_texts)
            captured_output = any("[Codex App]" in text and "BRIDGE_E2E_OK" in text for text in new_texts)
            state_output = self.accept_state_observation and self._state_has_bridge_output(state_probe)
            has_bridge_output = captured_output or state_output
            fragment_texts = _bridge_fragment_texts(new_texts)
            bridge_ok_count = sum(1 for text in new_texts if "[Codex App]" in text and "BRIDGE_E2E_OK" in text)
            result = ("bridge_input_no_write_failure", not has_write_failure, "\n".join(new_texts[-5:]))
            results.append(result)
            self._print_result(result)
            detail = "\n".join(new_texts[-5:])
            if state_output and not captured_output:
                detail = (detail + "\n" if detail else "") + "observed BRIDGE_E2E_OK in Codex JSONL/delivery state"
            if not has_bridge_output:
                status_text = await self._status_text(ws)
                classification = _classify_bridge_marker_failure(new_texts, status_text)
                if classification:
                    detail = (detail + "\n" if detail else "") + classification
            for result in (
                ("bridge_output_seen", has_bridge_output, detail),
                ("bridge_output_not_fragmented", not fragment_texts, "\n".join(fragment_texts[-5:])),
                ("bridge_output_not_duplicated", bridge_ok_count <= 1, f"BRIDGE_E2E_OK outbound count={bridge_ok_count}"),
            ):
                results.append(result)
                self._print_result(result)
        await step(self._send_and_expect(ws, "/term on", "Terminal mode on"))
        term_bridge_start = len(self.sent_actions)
        await self._send_private_message(ws, "human priority bridge text")
        await asyncio.sleep(2.0)
        term_bridge_texts = self._outbound_texts()[term_bridge_start:]
        term_priority_ok = any("shell backend 只支持" in text for text in term_bridge_texts)
        no_bridge_write_ok = not any("sent to Codex App thread" in text for text in term_bridge_texts)
        result = ("term_priority_over_bridge_input", term_priority_ok and no_bridge_write_ok, "\n".join(term_bridge_texts[-5:]))
        results.append(result)
        self._print_result(result)
        await step(self._send_and_expect(ws, "/codexbridge status", "Codex Bridge: on"))
        await step(self._send_and_expect(ws, "/codexbridge off", "Codex Bridge off"))
        await step(self._send_and_expect(ws, "/codexbridge status", "Codex Bridge: off"))
        await step(self._send_and_expect(ws, "/term off", "Terminal mode off"))
        if self.status_after_probe:
            await step(self._send_and_expect(ws, "/codexbridge status", "Codex Bridge: off"))
        if not self.keep_bridge_on:
            await step(self._send_and_expect(ws, "/codexbridge off", "Codex Bridge off"))
        return results

    def _headers(self, role: str) -> dict[str, str]:
        return {
            "X-Self-ID": str(self.self_id),
            "X-Client-Role": role,
        }

    def _state_probe(self) -> dict[str, Any]:
        umo = f"onebot_napcat:FriendMessage:{self.user_id}"
        try:
            db = pathlib.Path.home() / ".astrbot" / "data" / "data_v4.db"
            con = sqlite3.connect(db)
            row = con.execute(
                "select value from preferences where key=?",
                (f"codexbridge_app_thread_path_{umo}",),
            ).fetchone()
            if not row:
                return {"umo": umo}
            raw = json.loads(row[0]).get("val") or ""
            path = pathlib.Path(str(raw))
            return {"umo": umo, "path": path, "size": path.stat().st_size if path.exists() else 0}
        except Exception as exc:
            return {"umo": umo, "error": str(exc)}

    def _state_has_bridge_output(self, probe: dict[str, Any]) -> bool:
        path = probe.get("path")
        size = int(probe.get("size") or 0)
        if isinstance(path, pathlib.Path) and path.exists():
            try:
                with path.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(size)
                    if "BRIDGE_E2E_OK" in f.read():
                        return True
            except Exception:
                pass
        return self._queue_has_bridge_output(str(probe.get("umo") or ""))

    @staticmethod
    def _queue_has_bridge_output(umo: str) -> bool:
        if not umo:
            return False
        try:
            db = pathlib.Path.home() / ".astrbot" / "data" / "data_v4.db"
            con = sqlite3.connect(db)
            row = con.execute("select value from preferences where key='delivery_queue'").fetchone()
            if not row:
                return False
            data = json.loads(row[0]).get("val") or []
            for item in data:
                if item.get("umo") == umo and item.get("channel") == "codexbridge" and "BRIDGE_E2E_OK" in str(item.get("text") or ""):
                    return True
        except Exception:
            return False
        return False

    @staticmethod
    async def _cancel_readers(readers) -> None:
        for reader in readers:
            reader.cancel()
        for reader in readers:
            try:
                await reader
            except asyncio.CancelledError:
                pass

    async def _read_loop(self, ws) -> None:
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if "action" in payload:
                self.sent_actions.append(payload)
                await self._ack_action(ws, payload)

    async def _ack_action(self, ws, action: dict[str, Any]) -> None:
        echo = action.get("echo")
        response = {
            "status": "ok",
            "retcode": 0,
            "data": {"message_id": int(time.time() * 1000) % 1000000},
        }
        if echo is not None:
            response["echo"] = echo
        await ws.send(json.dumps(response, ensure_ascii=False))

    async def _send_and_expect(self, ws, text: str, expected: str) -> tuple[str, bool, str]:
        return await self._send_and_expect_any(ws, text, [expected])

    async def _send_and_expect_any(self, ws, text: str, expected: list[str], *, timeout: float | None = None) -> tuple[str, bool, str]:
        start = len(self.sent_actions)
        await self._send_private_message(ws, text)
        action_timeout = max(self.command_timeout, DEFAULT_ACTION_TIMEOUT) if timeout is None else timeout
        deadline = time.time() + action_timeout
        while time.time() < deadline:
            texts = self._outbound_texts()[start:]
            if any(token in item for item in texts for token in expected):
                return text, True, "\n".join(texts[-3:])
            await asyncio.sleep(min(0.05, max(0.01, deadline - time.time())))
        texts = self._outbound_texts()[start:]
        return text, False, "\n".join(texts[-5:]) or "(no outbound action captured)"

    async def _send_private_message(self, ws, text: str) -> None:
        self.message_id += 1
        event = {
            "time": int(time.time()),
            "self_id": self.self_id,
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": self.message_id,
            "user_id": self.user_id,
            "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text,
            "font": 0,
            "sender": {
                "user_id": self.user_id,
                "nickname": self.nickname,
                "card": "",
                "sex": "unknown",
                "age": 0,
            },
        }
        await ws.send(json.dumps(event, ensure_ascii=False))

    def _outbound_texts(self) -> list[str]:
        texts = []
        for action in self.sent_actions:
            params = action.get("params") or {}
            message = params.get("message")
            texts.append(_extract_text(message))
        return texts

    async def _status_text(self, ws) -> str:
        start = len(self.sent_actions)
        await self._send_private_message(ws, "/codexbridge status")
        deadline = time.time() + self.command_timeout
        while time.time() < deadline:
            texts = self._outbound_texts()[start:]
            for text in texts:
                if "Codex Bridge:" in text:
                    return text
            await asyncio.sleep(0.05)
        return "\n".join(self._outbound_texts()[start:])

    @staticmethod
    def _print_report(results: list[tuple[str, bool, str]]) -> None:
        print("OneBot reverse-WS simulated E2E report")
        for name, ok, detail in results:
            OneBotHarness._print_result((name, ok, detail))

    @staticmethod
    def _print_result(result: tuple[str, bool, str]) -> None:
        name, ok, detail = result
        status = "PASS" if ok else "FAIL"
        print(f"- {status} {name}", flush=True)
        if detail:
            print(_indent(detail), flush=True)


def _extract_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for item in message:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                data = item.get("data") or {}
                parts.append(str(data.get("text") or ""))
        return "".join(parts)
    return str(message or "")


def _bridge_fragment_texts(texts: list[str]) -> list[str]:
    fragments = {"execut", "ing", "-pl", "ans", "`", "for", "this"}
    found: list[str] = []
    for text in texts:
        if not text.startswith("[Codex App]\n"):
            continue
        body = text.split("\n", 1)[1].strip()
        if body in fragments:
            found.append(text)
    return found


def _classify_bridge_marker_failure(texts: list[str], status_text: str) -> str:
    if any("Codex App write failed" in text for text in texts):
        return "classified as write_failed"
    if "pending_user_inputs:" in status_text:
        pending = _status_int(status_text, "pending_user_inputs")
        turn_status = _status_value(status_text, "turn_status")
        if pending > 0:
            return f"classified as queued: turn_status={turn_status or '-'} pending_user_inputs={pending}"
    if "turn_status: running" in status_text:
        return "classified as stale_running"
    return "classified as no_marker_observed"


def _status_int(status_text: str, key: str) -> int:
    value = _status_value(status_text, key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _status_value(status_text: str, key: str) -> str:
    prefix = f"{key}:"
    for line in status_text.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def _indent(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate a OneBot reverse-WS client against a running AstrBot.")
    parser.add_argument("--url", default="ws://127.0.0.1:6199/ws")
    parser.add_argument("--self-id", type=int, default=1904439708)
    parser.add_argument("--user-id", type=int, default=1503663035)
    parser.add_argument("--nickname", default="E2E")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--command-timeout", type=float, default=8.0)
    parser.add_argument("--role", choices=["event", "api", "universal"], default="universal")
    parser.add_argument("--dual-role", action="store_true", help="Use separate OneBot event and API reverse-WS clients.")
    parser.add_argument("--thread-index", type=int, default=0, help="Optional /codexbridge ls index to bind before bridge send.")
    parser.add_argument("--thread-target", default="", help="Optional /codexbridge use target id prefix to bind before bridge send.")
    parser.add_argument("--skip-bridge-send", action="store_true", help="Skip the Codex App write/readback probe.")
    parser.add_argument("--status-after-probe", action="store_true", help="Query /codexbridge status after the bridge send probe.")
    parser.add_argument("--keep-bridge-on", action="store_true", help="Leave /codexbridge enabled at the end of the run.")
    parser.add_argument("--strict-ws-output", action="store_true", help="Require the fake OneBot WS to capture bridge output; do not accept JSONL/delivery state evidence.")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    harness = OneBotHarness(
        url=args.url,
        self_id=args.self_id,
        user_id=args.user_id,
        nickname=args.nickname,
        timeout=args.timeout,
        command_timeout=args.command_timeout,
        role=args.role,
        dual_role=args.dual_role,
        thread_index=args.thread_index,
        thread_target=args.thread_target,
        skip_bridge_send=args.skip_bridge_send,
        status_after_probe=args.status_after_probe,
        keep_bridge_on=args.keep_bridge_on,
        accept_state_observation=not args.strict_ws_output,
    )
    return await harness.run()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
