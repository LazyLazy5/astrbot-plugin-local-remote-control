# Merged Full Chain Bridge Human Test Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge the existing full-chain human simulation plan with Codex Bridge stability, UI review visibility, permission, and tool-error diagnostics.

**Architecture:** The plan keeps the existing layered gates: local pytest/compileall, live AstrBot with fake OneBot reverse WebSocket, and real NapCat/QQ manual smoke. It adds bridge-specific gates for stale running recovery, pending input flush, explicit app-server permissions, foreground UI review evidence, and user-visible tool execution failures.

**Tech Stack:** Python 3, pytest, PowerShell, AstrBot, OneBot v11 reverse WebSocket, NapCatQQ, Codex App `app-server`, Codex session JSONL, SQLite AstrBot state database.

---

## Summary

This plan verifies more than "a reply arrives." It must prove that real `/codexbridge` input reaches the intended Codex App thread/UI path, that stale `running` state cannot trap pending input forever, that app-server turns request full local permissions, and that tool execution failures are surfaced to QQ/WeChat instead of hiding in JSONL.

Recommended execution artifact folder:

```text
data\e2e-runs\<yyyyMMdd-HHmmss>\
```

## Key Changes

- Add stale running recovery tests:
  - Reproduce `turn_status: running`, `pending_user_inputs: 1`, with the target Codex thread already idle.
  - Recover to `idle` from app-server event, JSONL `task_complete`, or thread/readback status evidence.
  - Flush pending input immediately after recovery.

- Add real plugin-path UI review tests:
  - Keep Codex App delegation UI visibility as known-good baseline.
  - Send a unique `PLUGIN_UI_VISIBILITY_PROBE_<timestamp>` through `/codexbridge use <thread>`.
  - Check fake OneBot output, `.codex\sessions`, `codex_app.read_thread`, and manual Codex App UI search.
  - If marker is absent, classify as not sent, queued, write failed, wrong thread, or UI hidden.

- Add permission tests:
  - `turn/start` must include `approvalPolicy: "never"`.
  - `turn/start` must include `sandboxPolicy: {"type": "dangerFullAccess"}`.
  - `thread/resume` must include `approvalPolicy: "never"`, `sandbox: "danger-full-access"`, and `cwd`.

- Add tool-error surfacing tests:
  - JSONL `function_call_output` execution errors emit `[Codex App/System]`.
  - Cover `CreateProcessAsUserW failed: 5`.
  - Errors must not be silently kept only in JSONL.

## Test Matrix

- Local static gates:
  - `python -m pytest --collect-only -q`
  - `python -m pytest -q`
  - `python -m compileall -q astrbot_plugin_local_remote_control scripts tests`

- Bridge unit gates:
  - stale running + JSONL `task_complete` restores idle.
  - stale running + pending input flushes after recovery.
  - missed `turn/completed` can self-heal on `status` or `poll_once`.
  - event delta is emitted only as a complete output.
  - event + JSONL duplicate text is emitted once.
  - `<environment_context>`, outbound echo, and diagnostic input are suppressed.
  - tool execution errors are extracted and delivered as system messages.

- Fake OneBot gates:
  - `/term off/on/status`, `/pwd`, `/dir`, `/cd .`, `/git status`.
  - `/codexbridge on/off/status/probe/ls/use/retry/queue clear`.
  - blank input, Chinese text, `/codexbrideg`, normal text, and quick consecutive input.
  - Strict run:

```powershell
python scripts\simulate_onebot_e2e.py --timeout 90 --command-timeout 10 --thread-target 019eda31 --status-after-probe --self-id 1904439708 --dual-role --strict-ws-output
```

- Real plugin UI marker gate:
  - First require `/codexbridge status` to show `turn_status: idle` and `pending_user_inputs: 0`.
  - Bind a known safe thread with `/codexbridge use 019ee497` or a chosen test thread.
  - Send `PLUGIN_UI_VISIBILITY_PROBE_<timestamp>`.
  - Verify fake OneBot ACK, `.codex\sessions`, `codex_app.read_thread`, and manual Codex App UI search.
  - If `pending_user_inputs > 0`, classify as bridge state failure first.

- Real NapCat/QQ smoke:
  - Start `C:\Users\15036\Desktop\Start NapCat.bat`.
  - Confirm NapCat WS client is `ws://127.0.0.1:6199/ws`.
  - Human sends `/sid`, `/term off`, `/codexbridge off`, `/term on`, `/pwd`, `/dir`, `你好`, `/term off`, `/codexbridge status`.
  - Record screenshot or notes.

- Optional HAPI/WeChat:
  - Run active HAPI only when endpoint and token are configured.
  - For WeChat, observe natural `ret=-2` recovery only; do not intentionally break account state.

## Acceptance Criteria

- All local tests and compile checks pass.
- Strict fake OneBot E2E passes with no fragments, duplicates, or empty messages.
- `/codexbridge status` does not stay indefinitely in stale `running`.
- Pending input is sent after recovery to idle.
- Real `/codexbridge` marker receives a concrete classification; "not seen" is not enough.
- Permission tests prove app-server turns request full-access context.
- Tool execution failures are delivered to QQ/WeChat or fake OneBot.
- Cleanup leaves tested UMO with `/term` and `/codexbridge` off and no unexpected queued items.

## Assumptions

- Real QQ/NapCat UI and account state need manual confirmation.
- Codex App delegation UI visibility has already been proven; this plan focuses on the plugin app-server path.
- If real NapCat and fake OneBot compete for API actions, JSONL/read_thread/UI evidence remains mandatory.
