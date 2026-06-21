# Full Chain Human Simulation Test Plan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute and harden a full-chain, human-like simulation test for `astrbot_plugin_local_remote_control`, from local unit coverage through real AstrBot, fake OneBot reverse WebSocket, NapCat/QQ human operation, Codex App Bridge, HAPI, delivery queue, and restart recovery.

**Architecture:** The plan uses three realism layers. Layer 1 is deterministic local verification (`pytest`, `compileall`, focused unit coverage). Layer 2 is automated live AstrBot verification using the existing fake OneBot reverse-WS harness. Layer 3 is real-channel verification with NapCat/QQ and optional WeChat/HAPI/Codex App dependencies, recorded as manual evidence when the external account or app cannot be automated safely.

**Tech Stack:** Python 3, pytest, PowerShell, AstrBot, OneBot v11 reverse WebSocket, NapCatQQ, Codex App `app-server`, optional HAPI Hub/Runner, SQLite AstrBot state database.

---

## Current Local Facts

- Workspace: `C:\Users\15036\Desktop\Study\Strange idea\Wechat_Controll`
- Plugin source: `astrbot_plugin_local_remote_control`
- E2E harness: `scripts\simulate_onebot_e2e.py`
- NapCat project launcher: `scripts\start-napcat.cmd`
- Desktop NapCat launcher: `C:\Users\15036\Desktop\Start NapCat.bat`
- Current test collection: `101 tests collected`
- AstrBot dashboard port observed: `127.0.0.1:6185`
- AstrBot OneBot reverse-WS port observed: `0.0.0.0:6199`
- `codex` command exists at `C:\Users\15036\AppData\Roaming\npm\codex.cmd`
- Worktree is dirty before this plan; execution must not reset, checkout, or revert unrelated files.

## Scope

This plan covers:

- Static project/test sanity.
- Full local unit regression.
- Real AstrBot plugin sync and reload.
- Strict fake OneBot reverse-WS E2E against live AstrBot.
- Human-like input cadence, malformed input, blank input, Chinese text, typo aliases, and priority conflicts.
- Codex App Bridge write/readback, fragment suppression, duplicate suppression, and noisy background output risk.
- HAPI inactive-session behavior and optional active-session roundtrip.
- Delivery queue retry, long-message chunking, per-window isolation, and channel-specific clearing.
- Real NapCat/QQ channel smoke test.
- Optional WeChat restricted-channel recovery test when a real WeChat channel is available.
- Restart/reload persistence and cleanup.

This plan does not execute destructive shell commands, file deletion, account logout, account relink, or QQ/WeChat mass messaging.

## Success Criteria

A run is successful only when all required criteria pass:

- `python -m pytest -q` exits `0`.
- `python -m compileall -q astrbot_plugin_local_remote_control scripts tests` exits `0`.
- Strict fake OneBot E2E exits `0` and reports no bridge fragments or duplicate `BRIDGE_E2E_OK`.
- `/term` and `/codexbridge` modes end in `off` unless the run explicitly records `--keep-bridge-on`.
- Queue status for the tested UMO has no unexpected stuck item after cleanup.
- Real NapCat/QQ smoke test confirms messages travel through the real client to AstrBot and back.
- Any skipped optional dependency is recorded with the exact blocker, for example "HAPI endpoint not configured" or "WeChat channel not connected".

---

### Task 1: Establish Evidence Folder

**Files:**
- Read: `README.md`
- Create during execution: `data\e2e-runs\<timestamp>\`
- Modify: none

- [ ] **Step 1: Create a timestamped run folder**

Run from `C:\Users\15036\Desktop\Study\Strange idea\Wechat_Controll`:

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$runDir = "data\e2e-runs\$stamp"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$runDir
```

Expected: PowerShell prints a path like `data\e2e-runs\20260621-153000`.

- [ ] **Step 2: Start a transcript**

```powershell
Start-Transcript -Path "$runDir\powershell-transcript.txt"
```

Expected: transcript starts without error. If transcript is already active, record that in `run-notes.md` and continue.

- [ ] **Step 3: Write initial run notes**

```powershell
@"
# Full Chain E2E Run Notes

Run folder: $runDir
Started: $(Get-Date -Format o)
Workspace: C:\Users\15036\Desktop\Study\Strange idea\Wechat_Controll
Operator: local Codex session plus real user when manual channel steps are reached
"@ | Set-Content -Encoding UTF8 "$runDir\run-notes.md"
```

Expected: `$runDir\run-notes.md` exists.

---

### Task 2: Static Project and Test Sanity

**Files:**
- Read: `README.md`
- Read: `scripts\simulate_onebot_e2e.py`
- Read: `tests\*.py`
- Modify: none unless a real mismatch is found and approved for a separate implementation pass

- [ ] **Step 1: Capture git status without changing files**

```powershell
git status --short | Tee-Object -FilePath "$runDir\git-status-before.txt"
```

Expected: command succeeds. Existing dirty files are evidence, not a reason to revert.

- [ ] **Step 2: List test files**

```powershell
Get-ChildItem -LiteralPath tests -File | Select-Object Name,Length | Format-Table -AutoSize | Tee-Object -FilePath "$runDir\test-files.txt"
```

Expected: includes `test_simulate_onebot_e2e.py`, `test_modes.py`, `test_codex_app_bridge.py`, `test_delivery_queue.py`, and the other current test files.

- [ ] **Step 3: Collect tests**

```powershell
python -m pytest --collect-only -q | Tee-Object -FilePath "$runDir\pytest-collect.txt"
```

Expected: output ends with `101 tests collected`.

- [ ] **Step 4: Scan known risk strings**

```powershell
rg -n "TODO|FIXME|ret=-2|BRIDGE_E2E_OK|strict-ws-output|DEFAULT_ACTION_TIMEOUT|_bridge_should_dispatch_terminal_command|needs_user_refresh" README.md scripts tests astrbot_plugin_local_remote_control | Tee-Object -FilePath "$runDir\risk-string-scan.txt"
```

Expected: command may find real references. Treat these as coverage map entries, not automatic failures.

- [ ] **Step 5: Check README test count**

```powershell
Select-String -Path README.md -Pattern "passed|tests collected|simulate_onebot_e2e" | Tee-Object -FilePath "$runDir\readme-test-lines.txt"
```

Expected: README may still say `99 passed` while collection is `101`; record as documentation drift if present.

---

### Task 3: Local Regression Gate

**Files:**
- Test: `tests\*.py`
- Read: `astrbot_plugin_local_remote_control\*.py`
- Modify: none unless a failure is reproduced and a separate debug/fix pass is started

- [ ] **Step 1: Run full pytest**

```powershell
python -m pytest -q | Tee-Object -FilePath "$runDir\pytest.txt"
```

Expected: exit code `0` and output like `101 passed`.

- [ ] **Step 2: Run compile check**

```powershell
python -m compileall -q astrbot_plugin_local_remote_control scripts tests
```

Expected: exit code `0` and no output.

- [ ] **Step 3: If pytest fails, freeze the failure before editing**

```powershell
python -m pytest -q -x --tb=long | Tee-Object -FilePath "$runDir\pytest-first-failure.txt"
```

Expected when there is a failure: one minimal failure captured with traceback. Do not edit code until the failing behavior is understood and a focused test plan is written.

---

### Task 4: AstrBot Runtime Preflight

**Files:**
- Read: `astrbot_plugin_local_remote_control\main.py`
- Read: `astrbot_plugin_local_remote_control\metadata.yaml`
- Runtime target: `C:\Users\15036\.astrbot\data\plugins\astrbot_plugin_local_remote_control`
- Modify during execution: runtime plugin copy only, not repo source

- [ ] **Step 1: Verify AstrBot ports**

```powershell
Get-NetTCPConnection -LocalPort 6185 -State Listen -ErrorAction SilentlyContinue | Tee-Object -FilePath "$runDir\port-6185.txt"
Get-NetTCPConnection -LocalPort 6199 -State Listen -ErrorAction SilentlyContinue | Tee-Object -FilePath "$runDir\port-6199.txt"
```

Expected: `6185` is listening on `127.0.0.1`; `6199` is listening on `0.0.0.0` or `127.0.0.1`.

- [ ] **Step 2: Sync plugin source to AstrBot runtime**

```powershell
$src = 'C:\Users\15036\Desktop\Study\Strange idea\Wechat_Controll\astrbot_plugin_local_remote_control'
$dst = 'C:\Users\15036\.astrbot\data\plugins\astrbot_plugin_local_remote_control'
$files = @('__init__.py','_conf_schema.json','codex_app_bridge.py','commands.py','delivery_queue.py','hapi_client.py','main.py','metadata.yaml','platform_strategy.py','safe_shell.py','terminal_session.py')
New-Item -ItemType Directory -Force -Path $dst | Out-Null
foreach ($f in $files) {
  Copy-Item -LiteralPath (Join-Path $src $f) -Destination (Join-Path $dst $f) -Force
}
```

Expected: no output and no copy error.

- [ ] **Step 3: Reload plugin through AstrBot WebUI**

Open `http://127.0.0.1:6185`, reload plugin `astrbot_plugin_local_remote_control`, then record the result:

```powershell
Add-Content -Encoding UTF8 "$runDir\run-notes.md" "AstrBot plugin reload: completed via WebUI at $(Get-Date -Format o)"
```

Expected: WebUI reports reload success. If WebUI is unavailable, record `AstrBot dashboard unavailable` and stop before live E2E.

- [ ] **Step 4: Verify reload did not remove runtime files**

```powershell
Get-ChildItem -LiteralPath 'C:\Users\15036\.astrbot\data\plugins\astrbot_plugin_local_remote_control' -File | Select-Object Name,Length | Sort-Object Name | Tee-Object -FilePath "$runDir\runtime-plugin-files.txt"
```

Expected: runtime folder contains the same plugin files listed in Step 2.

---

### Task 5: Strict Fake OneBot E2E Baseline

**Files:**
- Execute: `scripts\simulate_onebot_e2e.py`
- Read: `data\e2e-runs\<timestamp>\`
- Modify: none

- [ ] **Step 1: Run strict dual-role E2E**

```powershell
python scripts\simulate_onebot_e2e.py --timeout 90 --command-timeout 10 --thread-target 019eda31 --status-after-probe --self-id 1904439708 --dual-role --strict-ws-output | Tee-Object -FilePath "$runDir\fake-onebot-strict-e2e.txt"
```

Expected:

```text
OneBot reverse-WS simulated E2E report
- PASS ...
```

The command must exit `0`.

- [ ] **Step 2: Confirm required pass markers**

```powershell
Select-String -Path "$runDir\fake-onebot-strict-e2e.txt" -Pattern "bridge_output_seen|bridge_output_not_fragmented|bridge_output_not_duplicated|term_priority_over_bridge_input|blank_event_no_outbound"
```

Expected: each marker appears with `PASS`.

- [ ] **Step 3: Confirm no failed markers**

```powershell
Select-String -Path "$runDir\fake-onebot-strict-e2e.txt" -Pattern "- FAIL "
```

Expected: no matches.

- [ ] **Step 4: Repeat with bridge send skipped**

```powershell
python scripts\simulate_onebot_e2e.py --timeout 30 --command-timeout 10 --skip-bridge-send --self-id 1904439708 --dual-role --strict-ws-output | Tee-Object -FilePath "$runDir\fake-onebot-no-bridge-send.txt"
```

Expected: exits `0`. This separates command routing failures from Codex App output timing failures.

---

### Task 6: Human-Like Fake OneBot Cadence

**Files:**
- Execute: `scripts\simulate_onebot_e2e.py`
- Candidate future test file if failures reveal harness gaps: `tests\test_simulate_onebot_e2e.py`
- Modify: none during this plan execution

- [ ] **Step 1: Run the same path with a different fake user**

```powershell
python scripts\simulate_onebot_e2e.py --timeout 90 --command-timeout 10 --user-id 1503663036 --nickname "E2E-Alt" --self-id 1904439708 --dual-role --strict-ws-output --skip-bridge-send | Tee-Object -FilePath "$runDir\fake-onebot-alt-user.txt"
```

Expected: exits `0`; state must not leak from `1503663035` to `1503663036`.

- [ ] **Step 2: Run concurrent fake users**

```powershell
$jobA = Start-Job -ScriptBlock {
  Set-Location 'C:\Users\15036\Desktop\Study\Strange idea\Wechat_Controll'
  python scripts\simulate_onebot_e2e.py --timeout 90 --command-timeout 10 --user-id 1503663035 --nickname E2E-A --self-id 1904439708 --dual-role --strict-ws-output --skip-bridge-send
}
$jobB = Start-Job -ScriptBlock {
  Set-Location 'C:\Users\15036\Desktop\Study\Strange idea\Wechat_Controll'
  python scripts\simulate_onebot_e2e.py --timeout 90 --command-timeout 10 --user-id 1503663036 --nickname E2E-B --self-id 1904439708 --dual-role --strict-ws-output --skip-bridge-send
}
Receive-Job -Job $jobA -Wait | Tee-Object -FilePath "$runDir\fake-onebot-concurrent-a.txt"
Receive-Job -Job $jobB -Wait | Tee-Object -FilePath "$runDir\fake-onebot-concurrent-b.txt"
Remove-Job $jobA,$jobB
```

Expected: both outputs have only `PASS` markers. If either fails due to rate limiting or action cross-talk, capture both files and classify as a harness or runtime concurrency issue.

- [ ] **Step 3: Probe action-boundary weakness**

Run strict E2E twice back-to-back:

```powershell
python scripts\simulate_onebot_e2e.py --timeout 90 --command-timeout 10 --self-id 1904439708 --dual-role --strict-ws-output --skip-bridge-send | Tee-Object -FilePath "$runDir\fake-onebot-repeat-1.txt"
python scripts\simulate_onebot_e2e.py --timeout 90 --command-timeout 10 --self-id 1904439708 --dual-role --strict-ws-output --skip-bridge-send | Tee-Object -FilePath "$runDir\fake-onebot-repeat-2.txt"
```

Expected: both exit `0`; late output from the first run must not cause false success or failure in the second run.

---

### Task 7: Real NapCat/QQ Smoke Test

**Files:**
- Execute: `C:\Users\15036\Desktop\Start NapCat.bat`
- Read: `scripts\start-napcat.cmd`
- Modify: none

- [ ] **Step 1: Start NapCat**

Double-click:

```text
C:\Users\15036\Desktop\Start NapCat.bat
```

Expected: NapCat starts. If QR login or account confirmation appears, complete it manually.

- [ ] **Step 2: Confirm NapCat WebSocket client**

In NapCat WebUI, confirm a WebSocket Client points to:

```text
ws://127.0.0.1:6199/ws
```

Expected: connection status is connected. If not connected, confirm AstrBot `6199` is listening and reconnect.

- [ ] **Step 3: Send real QQ commands as a human**

From the QQ friend/private chat that maps to the test UID, send these messages with 2-5 seconds between messages:

```text
/sid
/term off
/codexbridge off
/term status
/term on
/pwd
/dir
你好
/term off
/codexbridge status
```

Expected:

- `/sid` shows the real UMO/UID.
- `/term status` replies with `Terminal: off` before `/term on`.
- `/term on` replies `Terminal mode on`.
- `/pwd` and `/dir` return shell-safe output.
- `你好` in terminal mode returns the shell backend restriction text.
- Cleanup leaves terminal off.

- [ ] **Step 4: Record manual evidence**

```powershell
Add-Content -Encoding UTF8 "$runDir\run-notes.md" @"

## Real NapCat/QQ Smoke

Completed at: $(Get-Date -Format o)
Observed UMO from /sid:
Observed UID from /sid:
NapCat WS status:
Unexpected replies:
"@
```

Expected: notes contain the observed UMO and any unexpected reply.

---

### Task 8: Real Codex App Bridge Probe

**Files:**
- Execute: `scripts\simulate_onebot_e2e.py`
- Read: `astrbot_plugin_local_remote_control\codex_app_bridge.py`
- Modify: none unless a separate debug pass is approved

- [ ] **Step 1: Confirm Codex command**

```powershell
where.exe codex | Tee-Object -FilePath "$runDir\codex-path.txt"
```

Expected: includes `codex.cmd` or `codex.exe`.

- [ ] **Step 2: Probe bridge availability through fake OneBot**

```powershell
python scripts\simulate_onebot_e2e.py --timeout 90 --command-timeout 10 --self-id 1904439708 --dual-role --strict-ws-output --skip-bridge-send --status-after-probe | Tee-Object -FilePath "$runDir\bridge-probe-only.txt"
```

Expected: `/codexbridge probe` returns either `read-write` or `read-only`; command exits `0`.

- [ ] **Step 3: Run bridge write/readback when Codex App is available**

```powershell
python scripts\simulate_onebot_e2e.py --timeout 90 --command-timeout 10 --thread-target 019eda31 --status-after-probe --self-id 1904439708 --dual-role --strict-ws-output | Tee-Object -FilePath "$runDir\bridge-write-readback.txt"
```

Expected:

- `bridge_input_no_write_failure` is `PASS`.
- `bridge_output_seen` is `PASS`.
- `bridge_output_not_fragmented` is `PASS`.
- `bridge_output_not_duplicated` is `PASS`.

- [ ] **Step 4: If bridge output is noisy, classify the failure**

```powershell
Select-String -Path "$runDir\bridge-write-readback.txt" -Pattern "\[Codex App\]|BRIDGE_E2E_OK|Codex App write failed|FAIL" | Tee-Object -FilePath "$runDir\bridge-noise-analysis.txt"
```

Expected: any unrelated `[Codex App]` output is recorded as background thread contamination. If the unique marker is missing but JSONL shows it, classify as delivery delay or WebSocket capture gap.

---

### Task 9: HAPI Session Coverage

**Files:**
- Read: `astrbot_plugin_local_remote_control\hapi_client.py`
- Read runtime config: `C:\Users\15036\.astrbot\data\config\astrbot_plugin_local_remote_control_config.json`
- Modify: none

- [ ] **Step 1: Check HAPI config without printing secrets**

```powershell
$configPath = 'C:\Users\15036\.astrbot\data\config\astrbot_plugin_local_remote_control_config.json'
$cfg = Get-Content -Raw $configPath | ConvertFrom-Json
[pscustomobject]@{
  endpoint_configured = -not [string]::IsNullOrWhiteSpace($cfg.hapi_endpoint)
  token_configured = -not [string]::IsNullOrWhiteSpace($cfg.access_token)
  proxy_configured = -not [string]::IsNullOrWhiteSpace($cfg.proxy_url)
} | Tee-Object -FilePath "$runDir\hapi-config-redacted.txt"
```

Expected: booleans only; token value is not printed.

- [ ] **Step 2: Verify inactive-session behavior through E2E**

```powershell
python scripts\simulate_onebot_e2e.py --timeout 60 --command-timeout 10 --self-id 1904439708 --dual-role --strict-ws-output --skip-bridge-send | Tee-Object -FilePath "$runDir\hapi-inactive-path.txt"
```

Expected: `/term use 1` accepts `session inactive` as a valid result, and `/term send pre-bridge` does not send to an inactive HAPI session.

- [ ] **Step 3: Optional active HAPI roundtrip from real QQ or fake OneBot**

Only run this when Step 1 shows both endpoint and token configured and the HAPI Runner has this workspace in allowed roots.

Human message sequence:

```text
/term on
/term agent codex C:\Users\15036\Desktop\Study\Strange idea\Wechat_Controll
/term send Reply exactly HAPI_E2E_OK
/term status
/term off
```

Expected:

- Agent creation reports a session id or a successful selection.
- A later delivery queue message contains `[HAPI/codex]` and `HAPI_E2E_OK`.
- `/term status` shows backend `hapi` while active.
- `/term off` cleans up terminal mode.

- [ ] **Step 4: If active HAPI is skipped, record the exact blocker**

```powershell
Add-Content -Encoding UTF8 "$runDir\run-notes.md" "Active HAPI roundtrip status: skipped or completed. Reason/evidence: "
```

Expected: the reason is concrete, such as missing endpoint, missing token, runner offline, or workspace root rejection.

---

### Task 10: Delivery Queue and Long Output

**Files:**
- Read: `astrbot_plugin_local_remote_control\delivery_queue.py`
- Read: `tests\test_delivery_queue.py`
- Runtime DB: `C:\Users\15036\.astrbot\data\data_v4.db`
- Modify: none

- [ ] **Step 1: Check queue status before stress**

Human or fake OneBot commands:

```text
/term queue clear
/codexbridge queue clear
/term retry
/codexbridge retry
/term status
/codexbridge status
```

Expected: queue counts are `0` or explainable by background output that is immediately delivered.

- [ ] **Step 2: Generate a long Codex App output**

Use real QQ or fake OneBot bridge input while `/codexbridge on`:

```text
bridge diagnostic: reply with exactly LONG_BRIDGE_E2E_OK followed by 1800 letter A characters and no markdown
```

Expected:

- The returned bridge output is split into labelled chunks such as `[Codex App 1/3]`.
- Chunks arrive in order.
- No chunk is duplicated.
- The marker `LONG_BRIDGE_E2E_OK` appears exactly once.

- [ ] **Step 3: Query queue state after delivery**

```powershell
$db = Join-Path $env:USERPROFILE '.astrbot\data\data_v4.db'
python - <<'PY'
import json, pathlib, sqlite3
db = pathlib.Path.home() / ".astrbot" / "data" / "data_v4.db"
con = sqlite3.connect(db)
row = con.execute("select value from preferences where key='delivery_queue'").fetchone()
print(row[0] if row else "[]")
PY
```

Expected: no stuck long-output item remains for the tested UMO after all chunks are delivered.

---

### Task 11: Security and Negative Human Cases

**Files:**
- Read: `astrbot_plugin_local_remote_control\main.py`
- Test references: `tests\test_modes.py`
- Modify: none

- [ ] **Step 1: Verify blank input does not emit output**

Already covered by strict E2E marker:

```text
blank_event_no_outbound
```

Expected: marker is `PASS`.

- [ ] **Step 2: Verify bridge acknowledgement is not echoed back**

Send while `/codexbridge on`:

```text
sent to Codex App thread fake-thread turn fake-turn
```

Expected: no new Codex App write is triggered and no duplicate ack loop appears.

- [ ] **Step 3: Verify typo alias**

Send:

```text
/codexbrideg status
codex brideg off
```

Expected: both route to codexbridge behavior; the second command turns bridge off.

- [ ] **Step 4: Verify non-admin behavior with a safe account if available**

From a non-admin QQ account, send:

```text
/term status
/codexbridge status
```

Expected: replies say `此命令仅限管理员使用`, or the event is cleared without privileged action for bridge-mode intercepted text.

If no non-admin account is available, record the skip reason in `run-notes.md`.

---

### Task 12: Restart and Reload Persistence

**Files:**
- Runtime state DB: `C:\Users\15036\.astrbot\data\data_v4.db`
- Runtime plugin folder: `C:\Users\15036\.astrbot\data\plugins\astrbot_plugin_local_remote_control`
- Modify: runtime process state only

- [ ] **Step 1: Set known state before reload**

Send:

```text
/term on
/codexbridge on
/term status
/codexbridge status
```

Expected: both modes show `on` before reload.

- [ ] **Step 2: Reload plugin through AstrBot WebUI**

Open `http://127.0.0.1:6185`, reload `astrbot_plugin_local_remote_control`.

Expected: reload succeeds.

- [ ] **Step 3: Confirm state after reload**

Send:

```text
/term status
/codexbridge status
```

Expected:

- Terminal window persistence is visible if it was enabled before reload.
- Bridge binding may persist even if bridge mode is later turned off.
- No duplicate background poll loops produce repeated messages.

- [ ] **Step 4: Confirm `/term on` resets stale HAPI state**

Send:

```text
/term on
/term status
```

Expected: backend becomes `shell`, session becomes `-`, and stale inactive HAPI session does not remain selected.

- [ ] **Step 5: Cleanup**

Send:

```text
/term off
/codexbridge off
/term queue clear
/codexbridge queue clear
```

Expected: both modes are off and queues are clear for the tested UMO.

---

### Task 13: Optional WeChat Restricted-Channel Recovery

**Files:**
- Read: `astrbot_plugin_local_remote_control\delivery_queue.py`
- Test reference: `tests\test_delivery_queue.py`
- Modify: none

- [ ] **Step 1: Confirm a real WeChat channel is connected**

Use AstrBot WebUI or `/sid` from the WeChat chat to identify a UMO starting with:

```text
weixin_
```

Expected: a real WeChat UMO exists. If not, skip this task and record `WeChat channel unavailable`.

- [ ] **Step 2: Trigger a safe queue item**

Send in the WeChat chat:

```text
/codexbridge status
/term status
```

Expected: normal replies or known restricted-channel behavior.

- [ ] **Step 3: Observe `ret=-2` recovery if it occurs naturally**

If AstrBot logs show `ret=-2`, send any new user message in that same WeChat chat:

```text
/term status
```

Expected:

- Queue status no longer shows `needs_user_refresh: yes`.
- Delivery resumes slowly rather than flooding old messages.

This task must not force account relogin or intentionally break the WeChat adapter.

---

### Task 14: Failure Debug Protocol

**Files:**
- Failure artifacts: `data\e2e-runs\<timestamp>\*.txt`
- Candidate tests when fixing: relevant file under `tests\`
- Candidate source when fixing: relevant file under `astrbot_plugin_local_remote_control\`

- [ ] **Step 1: Classify the failing layer**

Use this order:

```text
1. Local unit or compile failure
2. AstrBot runtime/reload failure
3. OneBot reverse-WS connection failure
4. Plugin command routing failure
5. Codex App Bridge app-server or JSONL failure
6. HAPI configuration/session failure
7. Delivery queue or adapter failure
8. Real NapCat/QQ account or UI failure
```

Expected: every failure is assigned to exactly one primary layer before editing code.

- [ ] **Step 2: Reproduce with the narrowest command**

Examples:

```powershell
python -m pytest tests\test_modes.py::test_bridge_mode_routes_term_commands_to_terminal_dispatcher -q
python scripts\simulate_onebot_e2e.py --timeout 60 --command-timeout 10 --skip-bridge-send --self-id 1904439708 --dual-role --strict-ws-output
python scripts\simulate_onebot_e2e.py --timeout 90 --command-timeout 10 --thread-target 019eda31 --self-id 1904439708 --dual-role --strict-ws-output
```

Expected: the smallest reproducer still fails before a code fix is attempted.

- [ ] **Step 3: Add or update the focused test before fixing**

Use the nearest test file:

```text
Routing/mode bug: tests\test_modes.py
Command dispatch bug: tests\test_commands.py
Bridge transport/poll bug: tests\test_codex_app_bridge.py
Queue bug: tests\test_delivery_queue.py
E2E harness bug: tests\test_simulate_onebot_e2e.py
HAPI bug: tests\test_hapi_client.py
```

Expected: new or changed test fails for the observed reason before source code changes.

- [ ] **Step 4: Verify after each fix**

```powershell
python -m pytest -q
python -m compileall -q astrbot_plugin_local_remote_control scripts tests
python scripts\simulate_onebot_e2e.py --timeout 90 --command-timeout 10 --thread-target 019eda31 --status-after-probe --self-id 1904439708 --dual-role --strict-ws-output
```

Expected: all required verification commands pass before reporting the issue fixed.

---

### Task 15: Final Report

**Files:**
- Read: `data\e2e-runs\<timestamp>\`
- Create during execution: `data\e2e-runs\<timestamp>\summary.md`
- Modify: none

- [ ] **Step 1: Write result summary**

```powershell
@"
# Full Chain E2E Summary

Completed: $(Get-Date -Format o)

Required gates:
- pytest:
- compileall:
- strict fake OneBot E2E:
- real NapCat/QQ smoke:
- cleanup:

Optional gates:
- Codex App write/readback:
- active HAPI roundtrip:
- WeChat restricted-channel recovery:
- concurrent fake users:

Failures:
- None recorded

Follow-up tests to add:
- None recorded
"@ | Set-Content -Encoding UTF8 "$runDir\summary.md"
```

Expected: summary records pass/fail/skip for every required and optional gate.

- [ ] **Step 2: Stop transcript**

```powershell
Stop-Transcript
```

Expected: transcript ends and is saved in the run folder.

- [ ] **Step 3: Confirm final cleanup state**

Send in the tested QQ chat:

```text
/term status
/codexbridge status
```

Expected: both modes are off unless the summary explicitly records why a mode was left on.

---

## Recommended Execution Order

1. Tasks 1-5 are required and should run first.
2. Task 6 is required for concurrency and action-boundary confidence when time allows.
3. Task 7 is required because it is the real NapCat/QQ channel smoke test.
4. Task 8 is required when Codex App is available; otherwise it is recorded as blocked by dependency.
5. Task 9 active HAPI roundtrip is optional unless HAPI Hub/Runner are known to be configured.
6. Task 10 is required after a successful bridge write/readback.
7. Task 11 is required except the non-admin account subcase, which is optional.
8. Task 12 is required before claiming reload persistence works.
9. Task 13 is optional and only runs when a real WeChat channel is connected.
10. Tasks 14-15 apply to every execution.

## Approach Options

**Recommended: Layered run**

Run local regression, live fake OneBot E2E, real NapCat/QQ smoke, then optional HAPI/WeChat. This gives high confidence without making fragile external dependencies block the whole run.

**Faster: Automated-only run**

Run Tasks 1-6 and 8 with fake OneBot only. This is good for quick regression, but it does not prove NapCat/QQ client behavior.

**Most realistic: Full manual-plus-automated run**

Run every task including real NapCat/QQ, active HAPI, reload persistence, long output, and optional WeChat. This is the best release-candidate run, but it needs manual account access and more time.

## Self-Review

- Spec coverage: required local, AstrBot, fake OneBot, real NapCat/QQ, bridge, HAPI, queue, security, concurrency, reload, and cleanup cases are represented.
- Placeholder scan: this document avoids unresolved implementation placeholders; optional tasks specify concrete skip conditions.
- Type and command consistency: paths, script names, and current CLI flags match the project files inspected on 2026-06-21.
