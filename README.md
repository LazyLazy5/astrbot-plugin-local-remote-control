# astrbot_plugin_local_remote_control

AstrBot 窗口级远程编程控制插件。当前定位是把 QQ/微信聊天窗口变成一个受控入口，用来查看和控制：

- HAPI Hub/Runner 托管的 Codex CLI / Claude Code session。
- 本地受限 shell。
- Codex Desktop/App thread 的可选 bridge。

插件是独立 AstrBot 插件，不 import `astrbot_plugin_hapi_connector`。必需依赖是 AstrBot + HAPI Hub/Runner；Codex App Bridge 是内置可选增强，依赖本机 `codex app-server` 可用。

## 当前状态

版本：`v0.3.0`

当前可用性：

- `/term` 与 `/codexbridge` 是两个独立窗口级模式，开关互不影响。
- QQ OneBot/NapCat 通道可用于持续推送，当前推荐使用 QQ/NapCat 做主控窗口。
- 微信通道保留，但属于受限通道，会处理 `ret=-2`、冷却、用户刷新和队列恢复。
- HAPI 配置、认证、machine/runner 状态检测可用。
- Codex App Bridge 当前可通过 `codex app-server --stdio` 读写 thread，并有 JSONL 输出兜底。
- Codex App Bridge 写入路径已验证真实插件 `/codexbridge use <thread>` 后可进入 Codex App thread，并能在前台 UI 搜索到记录。
- app-server 请求显式使用 `approvalPolicy: "never"` 与 full-access sandbox 参数，避免后台 turn 落回 `workspace-write + on-request`。
- stale `running` 状态可通过 app-server event、JSONL `task_complete` 或状态轮询恢复到 `idle`。
- `pending_user_inputs` 会在恢复 `idle` 后自动 flush，避免普通输入无限排队。
- tool execution error 会作为 `[Codex App/System]` 摘要推送给 QQ/微信或 fake OneBot，不再只静默写入 JSONL。
- Bridge 输出已修复流式 token 碎片问题，不再把 `execut`、`ing`、`-pl`、`ans` 这类片段单独推送。
- Bridge 输出已增加稳定去重，同一条 `BRIDGE_E2E_OK` 不会因为 event + JSONL 双路而重复推送。
- OneBot/NapCat 异步投递会保留 `self_id`，真实 NapCat 与 fake OneBot 同时连接时可按 self-id 路由。
- fake OneBot strict E2E 已验证：完整 `[Codex App]\nBRIDGE_E2E_OK` 能推回，且无碎片、无重复。

最新验证（2026-06-21）：

```powershell
python -m pytest --collect-only -q
python -m pytest -q
python -m compileall -q astrbot_plugin_local_remote_control scripts tests
```

结果：`110 tests collected`，`110 passed`，编译通过。

strict fake OneBot E2E 已通过，真实 NapCat 同时连接时仍能捕获 bridge 输出：

```powershell
python scripts\simulate_onebot_e2e.py --timeout 90 --command-timeout 10 --thread-target 019eda31 --status-after-probe --self-id 1904439708 --dual-role --strict-ws-output
```

真实插件 UI marker 链路已通过：

```text
thread: 019eda31-ed09-7633-aa54-4468a8842f44
probe: PLUGIN_UI_VISIBILITY_PROBE_20260621_173345
ack: PLUGIN_UI_VISIBILITY_ACK_173345
```

验证结果：

- fake OneBot 收到 `[Codex App]\nPLUGIN_UI_VISIBILITY_ACK_173345`。
- `.codex\sessions` JSONL 出现 marker。
- Codex App thread readback 出现 marker。
- Codex App 前台 UI 已人工确认可见。

最终清理状态：

```text
Codex Bridge: off
turn_status: idle
pending_user_inputs: 0
queue: 0
```

## 安装

把插件目录复制到 AstrBot 插件目录：

```powershell
Copy-Item -Recurse -Force .\astrbot_plugin_local_remote_control "$env:USERPROFILE\.astrbot\data\plugins\astrbot_plugin_local_remote_control"
```

或者在开发时只同步源码文件到运行目录：

```powershell
$src = 'C:\Users\15036\Desktop\Study\Strange idea\Wechat_Controll\astrbot_plugin_local_remote_control'
$dst = 'C:\Users\15036\.astrbot\data\plugins\astrbot_plugin_local_remote_control'
$files = @('__init__.py','_conf_schema.json','codex_app_bridge.py','commands.py','delivery_queue.py','hapi_client.py','main.py','metadata.yaml','platform_strategy.py','safe_shell.py','terminal_session.py')
foreach ($f in $files) {
  Copy-Item -LiteralPath (Join-Path $src $f) -Destination (Join-Path $dst $f) -Force
}
```

重载插件可通过 AstrBot WebUI，或调用 dashboard API。

## 配置

`_conf_schema.json` 当前配置项：

- `admin_uids`：插件内部兼容管理员列表。留空时使用 AstrBot 全局管理员。
- `work_dir`：本地路径沙盒根目录。留空时使用插件目录下 `workspace`。
- `allow_git`：是否允许 `/git status`。
- `hapi_endpoint`：HAPI Hub 地址，例如 `http://127.0.0.1:3006`。
- `access_token`：HAPI access token。
- `proxy_url`：HAPI HTTP 代理，可留空。
- `jwt_lifetime`：HAPI JWT 有效期秒数。
- `refresh_before_expiry`：JWT 过期前多少秒刷新。
- `enable_codex_app_bridge`：是否允许 `/codexbridge on`。

权限策略：

- 插件命令继续走 AstrBot 原生权限体系。
- 使用 QQ/微信前，必须把对应 UID 加入 AstrBot 管理员列表。
- `admin_uids` 只作为兼容/二次校验，不建议替代 AstrBot 全局管理员配置。

当前 QQ OneBot UID 示例：

```text
UMO: onebot_napcat:FriendMessage:1503663035
UID: 1503663035
```

当前微信 UID 示例：

```text
UMO: weixin_personal_tnco:FriendMessage:o9cq80zn-W8moly-xygJ7b3yUCkw@im.wechat
UID: o9cq80zn-W8moly-xygJ7b3yUCkw@im.wechat
```

## QQ / OneBot / NapCat

推荐使用 NapCatQQ 提供 OneBot v11 反向 WebSocket，不推荐把 QQ 官方机器人 WebSocket 当作高频推送主通道。

当前本机接入方式：

```text
AstrBot OneBot reverse WS: ws://127.0.0.1:6199/ws
NapCat WebSocket Client URL: ws://127.0.0.1:6199/ws
Token: 空
```

启动顺序：

1. 启动 AstrBot，确保 `6199` 端口监听。
2. 启动 NapCat。
3. NapCat WebUI 添加 WebSocket Client，连接到 `ws://127.0.0.1:6199/ws`。
4. 在 QQ 私聊发送 `/sid`，确认 UMO/UID。

检测端口：

```powershell
Get-NetTCPConnection -LocalPort 6199 -State Listen
```

平台策略显示示例：

```text
platform: aiocqhttp
strategy: onebot
```

## 微信通道

微信 `weixin_oc` 是受限主动推送通道。已实现：

- 识别 `ret=-2`。
- 当前微信 UMO 进入 `needs_user_refresh`。
- 暂停该窗口主动推送，避免继续刷失败日志。
- 用户在该微信窗口发送任意消息后解除暂停，并慢速恢复队列。

如果旧消息积压导致重复推送，可在对应聊天窗口执行：

```text
/codexbridge queue clear
/term queue clear
```

## `/term` 模式

`/term` 是主控制模式，包含两个后端：

- `shell backend`：本地受限 shell。
- `hapi backend`：HAPI 托管 Codex/Claude Code session。

基础命令：

```text
/term on
/term off
/term status
/term retry
/term queue clear
/term shell
```

HAPI 命令：

```text
/term hapi reload
/term hapi status
/term agent codex [路径]
/term agent cc [路径]
/term ls [codex|cc|all]
/term use <序号|id前缀>
/term stop
/term send <内容>
```

兼容命令：

```text
/hapi status
/hapi list
/codex ls
/codex new [路径]
/codex use <序号|id前缀>
/cc ls
/cc new [路径]
/cc use <序号|id前缀>
/use <序号|id前缀>
/send <内容>
```

本地受限 shell 命令：

```text
/pwd
/dir [相对路径]
/cd <相对路径>
/git status
```

行为说明：

- `/term on` 后当前窗口进入终端模式，普通消息不会进入 AstrBot LLM。
- shell backend 不提供任意 shell 执行能力。
- 裸 `codex` / `claude` 不支持，因为当前 shell 是管道，不是 TTY。
- 真正交互 Codex CLI / Claude Code 应使用 `/term agent codex|cc`，由 HAPI Runner 托管。
- HAPI session 创建路径必须在 HAPI Runner 允许的 workspace roots 内，否则会返回可操作错误提示。

## `/codexbridge` 模式

`/codexbridge` 是 Codex Desktop/App thread bridge。

命令：

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

兼容错别名：

```text
/codexbrideg status
/codex brideg off
```

行为说明：

- `/codexbridge on` 会尝试绑定 Codex App thread。
- 优先尝试 `codex app-server proxy`。
- proxy 不可用时使用 `codex app-server --stdio`。
- app-server 都不可用时降级 JSONL 只读。
- `/codexbridge ls` 用于列出 Codex App threads。
- `/codexbridge use <序号|id前缀>` 用于显式选择 thread。
- 普通 QQ/微信输入会写入当前绑定的 Codex App thread。
- Codex App assistant 输出会以 `[Codex App]` 推回。
- Codex App 端用户输入会以 `[Codex App/User]` 推回，但默认过滤 `<environment_context>...</environment_context>`、bridge diagnostic 和自身 echo。

输出稳定化：

- event delta 会先聚合，只有完整文本才推送。
- JSONL 中明显的流式 agent message fragment 会缓冲到 `task_complete`。
- event 与 JSONL 的同文输出使用稳定内容 hash 去重。
- 长消息进入 delivery queue 后再分片。

## 输入优先级

两个模式可以同时开启。

优先级：

1. 控制命令优先，例如 `/term off`、`/codexbridge off`。
2. 如果 `/term on`，普通输入优先由 `/term` 处理。
3. 如果 `/term off` 且 `/codexbridge on`，普通输入写入 Codex App thread。
4. Bridge 输出仍可推送到当前窗口，不会抢 `/term` 输入。

## Delivery Queue

所有后台输出统一进入持久化 delivery queue。

特性：

- 按 `(UMO, channel)` FIFO。
- 后续消息不会越过失败消息。
- 微信 `ret=-2` 进入 `needs_user_refresh`。
- QQ OneBot/NapCat 不走微信 refresh 逻辑。
- 长消息按较小片段拆分。
- `/term retry` / `/codexbridge retry` 使当前窗口对应队列立即可重试。
- `/term queue clear` / `/codexbridge queue clear` 只清当前窗口对应 channel。

## 测试

单元测试：

```powershell
python -m pytest -q
```

编译检查：

```powershell
python -m compileall -q astrbot_plugin_local_remote_control scripts tests
```

严格 fake OneBot E2E：

```powershell
python scripts\simulate_onebot_e2e.py --timeout 90 --command-timeout 10 --thread-target 019eda31 --status-after-probe --self-id 1904439708 --dual-role --strict-ws-output
```

这个脚本不经过真实 NapCat，而是直接模拟 OneBot reverse WebSocket 客户端连接 AstrBot `6199`。

严格模式会检查：

- `/term` 基础命令。
- HAPI 状态和 session 命令。
- `/codexbridge` 开关、probe、ls、use、status。
- 空白输入不触发输出。
- bridge 普通输入能推回 `[Codex App]\nBRIDGE_E2E_OK`。
- 不出现碎片输出。
- 不重复推送 `BRIDGE_E2E_OK`。
- `/term` 与 `/codexbridge` 同开时 `/term` 输入优先。
- `/codexbridge status` 不长期停留在 stale `running`。
- `pending_user_inputs` 在恢复 `idle` 后会自动发送。
- JSONL 中的 tool execution error 会推送为 `[Codex App/System]`。

## 注意事项

- `/codexbridge off` 只关闭 bridge，不删除已绑定的 `app_thread`。后续 `status` 仍显示绑定信息属于预期行为。
- 真实 NapCat 与 fake OneBot 同时连接时，OneBot action 需要依赖 `self_id` 路由。当前插件已记忆并透传 `self_id`，如果诊断时看到 action 被真实 NapCat 抢走，优先检查 fake 客户端的 `--self-id`。
- 真实 QQ/NapCat smoke 仍依赖账号在线、NapCat WebSocket Client 连接到 `ws://127.0.0.1:6199/ws`、AstrBot 端口监听和管理员 UID 配置。
- HAPI active roundtrip 依赖 `hapi_endpoint`、`access_token`、runner workspace roots 与 runner 在线状态；配置不完整时只做状态检测。
- 微信通道出现 `ret=-2` 时需要用户在对应窗口发消息刷新；插件会暂停该窗口主动推送并在刷新后恢复队列。

## 安全边界

- 不提供删除、移动、复制、格式化、关机等危险命令。
- 不使用 `shell=True` 来执行用户输入。
- 本地路径通过 `pathlib.Path.resolve()` 和 sandbox 检查限制在 `work_dir` 内。
- HAPI token、Codex 本地状态、Claude 本地状态不得提交进 Git。
