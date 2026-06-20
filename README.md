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
/pwd
/dir [relative-path]
/cd <relative-path>
/git status
/hapi status
/hapi list
/codex ls
/codex new [relative-path]
/codex use <index|id-prefix>
/cc ls
/cc new [relative-path]
/cc use <index|id-prefix>
/use <index|id-prefix>
/send <message>
```

Codex Bridge:

```text
/codexbridge on
/codexbridge off
/codexbridge status
```

## Safety

The plugin does not expose arbitrary shell execution. File commands are jailed under `work_dir`. `/git status` uses a fixed argv without `shell=True`.

