# feishu-agent-bridge

> **当前版本：v0.2.1** · 飞书 Agent Gateway · 见 [CHANGELOG](./CHANGELOG.md)

把**飞书消息**接到本机已登录的 **Claude Code CLI** 或 **Codex CLI**。同一个飞书机器人、同一个 `app_id`，可在会话内用 `/agent` 无缝切换后端：

- `claude`：使用 `claude -p`，默认模型 `claude-sonnet-4-6`，保留现有 Claude 工具白名单和流式体验。
- `codex`：使用 `codex exec`，固定模型 `gpt-5.5`，通过 Codex 沙箱和审批策略控制执行能力。
- 飞书收发仍由 `lark-oapi` 完成：WebSocket 长连接、消息解析、卡片回复、图片上传、附件下载和断线重连。

## 项目状态

| 能力 | 状态 | 说明 |
|------|:---:|------|
| 飞书 ↔ Agent 双向桥接 | ✅ | 私聊/群聊收消息 → 当前 Agent → 回复 |
| `/agent` 运行时切换 | ✅ | 同一飞书会话可切换 `claude` / `codex` |
| Claude Code 后端 | ✅ | `claude -p` + `--session-id` / `--resume` |
| Codex 后端 | ✅ | `codex exec` / `codex exec resume`，固定 `gpt-5.5` |
| Agent 独立会话 | ✅ | Claude 和 Codex session 分开保存，互不污染 |
| 旧 session 迁移 | ✅ | 旧 `sessions.json` 懒迁移为多 Agent 状态 |
| WebSocket 长连接 + 断线重连 | ✅ | 基于 lark-oapi |
| 多会话并行隔离 | ✅ | 不同会话并行，同会话加锁串行 |
| 消息去重 / 防重投 | ✅ | TTL 去重 + 启动闸门 |
| 图片/文件接收 | ✅ | 附件下载到 `WORKDIR/.inbox` 后交给当前 Agent |
| 富文本卡片输出 | ✅ | schema 2.0 markdown card，支持自定义卡片透传 |
| Claude 流式回复 | ✅ | `stream_reply` 开启后可边生成边更新飞书卡片 |
| Codex 流式回复 | ↩️ | v0.2.0 先稳定最终回复；飞书流式卡片自动降级 |

## 安装

```bash
pip install -r requirements.txt
```

还需要本机 CLI 已安装并登录：

```bash
claude --help
codex --version
codex debug models
```

当前版本要求 `codex debug models` 能看到 `gpt-5.5`。

## 配置

复制模板：

```bash
cp config.example.json config.json
```

核心配置：

```jsonc
{
  "app_id": "cli_xxxxxxxxxxxx",
  "app_secret": "xxxxxxxxxxxxxx",

  "default_agent": "claude",
  "model": "claude-sonnet-4-6",
  "codex_model": "gpt-5.5",
  "codex_sandbox": "workspace-write",
  "codex_skip_git_repo_check": true,
  "claude_bin": "claude",
  "codex_bin": "codex",

  "workdir": ".",
  "state_dir": "~/.feishu_bridge",
  "timeout": 600,
  "max_attachment_bytes": 26214400,
  "stream_terminal": true,
  "terminal_stream_format": "text",
  "stream_reply": false,
  "stream_reply_interval": 0.7,
  "session_scope": "chat_user",
  "allowed_tools": "Read Write Edit Glob Grep WebSearch WebFetch Skill TodoWrite Task",
  "allowed_chats": []
}
```

说明：

- `default_agent`：默认后端，支持 `claude` 或 `codex`。
- `model`：Claude 默认模型；当前默认 `claude-sonnet-4-6`。
- `codex_model`：Codex 模型；当前只允许 `gpt-5.5`。
- `codex_sandbox`：传给 `codex exec --sandbox`，默认 `workspace-write`。
- `codex_skip_git_repo_check`：传给 `codex exec --skip-git-repo-check`，默认开启。
- `claude_bin` / `codex_bin`：CLI 可执行文件路径；默认走 `PATH`。如果普通终端找不到 Codex，可填绝对路径，例如 `C:/Users/asus/.vscode/extensions/.../codex.exe`。
- `allowed_tools`：只作用于 Claude Code，默认不含 `Bash`。
- `workdir` / `state_dir` / `BRIDGE_CONFIG` 支持 `~` 和环境变量展开。

飞书应用需开启机器人能力、接收消息事件（`im.message.receive_v1`）、`im:message` 等读写权限，并加入目标群。

## 会话命令

Agent 切换：

```text
/agent
/agent claude
/agent codex
/agent reset
```

模型切换：

```text
/model
/model opus
/model sonnet
/model reset
[m:sonnet] 只让这一条走指定 Claude 模型
```

`/model` 仅在当前 Agent 为 Claude 时生效。当前 Agent 为 Codex 时，模型固定为 `gpt-5.5`。

重置会话：

```text
/new
/reset
新会话
重置会话
```

v0.2.0 中，重置只清空当前激活 Agent 的上下文；未激活 Agent 的 session 会保留，之后切回仍可续接。

## 运行

```bash
python src/feishu_claude_bridge.py
```

Windows 用户可双击 `run_bridge.cmd` 在独立控制台窗口运行；关闭该窗口或按 Ctrl-C 即停止服务。

macOS 用户可双击 `run_bridge.command` 在独立 Terminal 窗口运行；关闭该窗口或按 Ctrl-C 即停止服务。

## 回复格式

- 默认：当前 Agent 输出 Markdown，桥自动按 20000 字符分块并包成飞书 schema 2.0 卡片。
- 图片：回复中写 `<<<IMG>>>相对路径.png` 或 Markdown `![](本地路径)`，桥会上传本地图片并作为独立 image 消息发出。
- 自定义卡片：输出以 `<<<CARD>>>` 开头并跟合法 interactive card JSON 时，桥原样透传。

## 附件处理

支持文本、图片、文件和富文本 `post` 中夹带的图片/文件。收到附件时：

1. 下载到 `WORKDIR/.inbox`。
2. 把本地路径拼进提示词交给当前 Agent。
3. 处理完删除临时文件。

## 状态文件

`state_dir/sessions.json` 保存飞书会话和 Agent session 的映射。v0.2.0 会把旧 Claude-only 状态懒迁移为：

```json
{
  "c:oc_xxx:u:ou_xxx": {
    "agent": "codex",
    "sessions": {
      "claude": {"sid": "claude-session-id"},
      "codex": {"sid": "codex-session-id"}
    },
    "models": {}
  }
}
```

## 安全说明

- `config.json` 含真实密钥，已被 `.gitignore` 忽略，请勿提交。
- 飞书发送只由桥通过“回复原消息”完成，避免 Agent 自己把消息发错群。
- Claude 默认工具白名单不含 `Bash`。
- Codex 默认使用 `workspace-write` 沙箱，适合无头运行；更高权限需显式修改配置。
- 附件下载有大小上限，默认 25MB。

## 版本管理

- 运行时版本定义在 `src/feishu_claude_bridge.py` 的 `__version__`。
- 变更记录见 [CHANGELOG.md](./CHANGELOG.md)。
- 版本策略见 [docs/VERSIONING.md](./docs/VERSIONING.md)。

## 路线图

- [x] 飞书 ↔ Claude Code 桥接（v0.0.1）
- [x] 图片发送（v0.0.2）
- [x] 图片/文件接收（v0.0.3）
- [x] 终端流式输出（v0.0.5）
- [x] Claude 流式卡片回复（v0.0.6）
- [x] Claude 运行时模型切换（v0.0.7）
- [x] `/agent` 切换 Claude / Codex（v0.2.0）
- [x] Agent 独立 session 与旧状态迁移（v0.2.0）
- [ ] Codex JSONL 事件的结构化终端时间线增强
- [ ] Codex 飞书流式卡片增量更新
- [ ] 更细的权限、审计日志和速率控制
- [ ] systemd / launchd / Docker 部署模板
