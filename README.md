# astrbot_plugin_local_remote_control

AstrBot local remote control plugin with two independent per-window modes:

- `/term on|off`: terminal mode. When enabled, all messages from the current chat window are intercepted by this plugin and do not fall through to AstrBot LLM or other plugins.
- `/codexbridge on|off`: experimental Codex App Bridge mode. It is independent from terminal mode.

## Install

Copy `astrbot_plugin_local_remote_control/` into AstrBot's plugin directory, then restart AstrBot.

Example:

```powershell
Copy-Item -Recurse -Force .\astrbot_plugin_local_remote_control "$env:USERPROFILE\.astrbot\data\plugins\astrbot_plugin_local_remote_control"
```

## Commands

Terminal mode:

```text
/term on
/term off
/term status
dir
cd <path>
where codex
codex --version
codex
```

Codex Bridge:

```text
/codexbridge on
/codexbridge off
/codexbridge status
```

`/term on` starts a persistent shell for the current chat window. On Windows the
default shell is `cmd.exe`, so messages such as `where codex` and
`codex --version` are sent directly to that shell. `/term off` closes only this
terminal mode.

`/codexbridge on` is independent from `/term on`. It binds the latest local Codex
thread it can find and pushes new assistant messages from Codex rollout JSONL
files to the current chat window. When Codex app-server is available, bridge
messages can be sent to the bound thread; otherwise it clearly reports that the
fallback JSONL bridge is read-only.

## Safety

`/term on` intentionally exposes a real shell to configured administrators. Treat
it as equivalent to giving that chat window access to the AstrBot host user's
terminal. Do not add untrusted UIDs to `admin_uids`.
