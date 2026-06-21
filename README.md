# astrbot_plugin_local_remote_control

AstrBot 远程开发控制插件。它把 QQ、微信等聊天窗口变成一个轻量控制入口，用来远程查看和操作本机开发会话。

主要功能：

- `/term`：窗口级终端模式，支持本地受限 shell 和 HAPI 托管的 Codex CLI / Claude Code session。
- `/codexbridge`：连接 Codex App/Desktop thread，把聊天窗口中的消息发送到 Codex App，并把 Codex 输出推回聊天窗口；也可简写为 `/cb`。
- Delivery Queue：后台输出持久化排队，支持重试、分片、失败后恢复。
- OneBot/NapCat 适配：推荐用于 QQ 持续推送。
- 微信通道保护：识别微信主动推送受限状态，避免失败消息刷屏。

## 安装

把插件目录复制到 AstrBot 插件目录：

```powershell
Copy-Item -Recurse -Force .\astrbot_plugin_local_remote_control "$env:USERPROFILE\.astrbot\data\plugins\astrbot_plugin_local_remote_control"
```

然后在 AstrBot WebUI 中重载或启用插件。

## 依赖关系

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)：必需。插件运行在 AstrBot 内，所有功能都依赖 AstrBot。
- [HAPI](https://github.com/tiann/hapi)：仅 `/term agent codex|cc` 需要。用于托管 Codex CLI / Claude Code 等本机会话；只使用 `/term shell` 或 `/codexbridge` 时可以不配置。
- [OneBot v11](https://onebots.pages.dev/en/protocol/onebot-v11/)：仅 OneBot 通道需要。用于 AstrBot 与协议端之间的消息收发。
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)：仅 QQ / NapCat 接入需要。通常通过 OneBot v11 反向 WebSocket 连接 AstrBot。

## 配置

插件配置来自 `_conf_schema.json`：

- `admin_uids`：允许使用插件的用户 ID 列表。留空时使用 AstrBot 全局管理员。
- `work_dir`：本地受限 shell 的工作目录根路径。留空时使用插件目录下 `workspace`。
- `allow_git`：是否允许 `/git status`。
- `hapi_endpoint`：HAPI Hub 地址，例如 `http://127.0.0.1:3000`。
- `access_token`：HAPI access token。
- `proxy_url`：HAPI HTTP 代理，可留空。
- `jwt_lifetime`：HAPI JWT 有效期秒数。
- `refresh_before_expiry`：JWT 过期前多少秒刷新。
- `enable_codex_app_bridge`：是否允许使用 `/codexbridge on`。

如果只使用 `/codexbridge`，可以不配置 HAPI；HAPI 只影响 `/term agent codex|cc` 这类托管会话功能。

## QQ / OneBot / NapCat

推荐使用 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 的 [OneBot v11](https://onebots.pages.dev/en/protocol/onebot-v11/) 反向 WebSocket 作为 QQ 推送通道。

典型配置：

```text
AstrBot OneBot reverse WS: ws://127.0.0.1:6199/ws
NapCat WebSocket Client URL: ws://127.0.0.1:6199/ws
Token: 空或按你的 AstrBot 配置填写
```

在聊天窗口发送 `/sid`，确认自己的 UMO/UID，并确保该 UID 是 AstrBot 管理员或已加入 `admin_uids`。

## 命令

### `/term`

```text
/term on
/term off
/term status
/term retry
/term queue clear
/term shell
/term hapi reload
/term hapi status
/term agent codex [路径]
/term agent cc [路径]
/term ls [codex|cc|all]
/term use <序号|id前缀>
/term stop
/term send <内容>
```

本地受限 shell 命令：

```text
/pwd
/dir [相对路径]
/cd <相对路径>
/git status
```

### `/codexbridge` / `/cb`

```text
/codexbridge on
/codexbridge off
/codexbridge status
/codexbridge retry
/codexbridge queue clear
/codexbridge probe
/codexbridge ls
/codexbridge use <序号|id前缀>
```

`/cb` 是 `/codexbridge` 的等价简写，例如 `/cb on`、`/cb status`。

普通输入行为：

- `/term on` 时，普通消息优先交给 `/term`。
- `/term off` 且 `/codexbridge on` 时，普通消息会发送到当前绑定的 Codex App thread。
- 两个模式可以同时开启，但控制命令总是优先处理。

## 当前限制

- `/term agent codex|cc` 依赖 HAPI Hub/Runner、access token 和 runner workspace roots 配置。
- `/codexbridge` 依赖本机 Codex App / Codex CLI 的 app-server 能力；不可用时会降级为 JSONL 只读。
- Codex App 后台 turn 可能已经写入 thread，但前台 UI 不一定实时刷新。
- 微信通道可能出现主动推送限制；用户需要在对应聊天窗口发消息后才能恢复队列推送。
- 本地 shell 是受限 shell，不支持任意 shell 命令。
- 插件不提供删除、移动、复制、格式化、关机等危险操作。

## 安全说明

- 不使用 `shell=True` 执行用户输入。
- 本地路径会限制在 `work_dir` 内。
- HAPI token、Codex 本地状态、Claude 本地状态不应提交进 Git。
- 发布或共享前，请确认配置文件中没有个人 token、UID 或本机绝对路径。
