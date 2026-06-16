# feishu-claude-bridge

> **当前版本：v0.0.5** · 核心单文件实现 · 见 [CHANGELOG](./CHANGELOG.md)

把**飞书消息**接到 **Claude Code 无头模式**（`claude -p`）的轻量桥。
群里 @ 机器人或私聊发消息，桥转给 Claude，再把回复包成飞书卡片发回——
等于给飞书装了一个能读写文件、跑工具、带持久记忆的 Claude 助手。

- **大脑** = `claude -p`：复用本机已登录授权的 Claude Code CLI，**无需 Anthropic API key**。
- **收发** = `lark-oapi`：飞书消息 encode/decode + WebSocket 长连接，断线自动重连。
- **形态** = 核心单文件 `src/feishu_claude_bridge.py`，零框架、易部署、易审计。

---

## 📌 项目状态（v0.0.5）

核心链路已跑通并在本地稳定运行。**当前能力：**

| 能力 | 状态 | 说明 |
|------|:---:|------|
| 飞书 ↔ Claude 双向桥接 | ✅ | 私聊/群聊收消息 → `claude -p` → 回复 |
| WebSocket 长连接 + 断线重连 | ✅ | 基于 lark-oapi，自动恢复 |
| 持久化会话（重启不丢上下文） | ✅ | 映射落盘 + `claude --resume` |
| 多会话并行隔离 | ✅ | 不同会话并行，同会话加锁串行 |
| 消息去重 / 防重投 | ✅ | TTL 去重 + 启动闸门 |
| 富文本卡片输出 | ✅ | schema 2.0 markdown card，支持自定义卡片透传 |
| 图片发送 | ✅ | 本地图片自动上传换 image_key，作为图片消息发出 |
| 图片/文件接收 | ✅ | 收到的图片、文件、富文本附件自动下载交给 Claude 解析处理 |
| 终端流式输出 | ✅ | 终端实时显示 Claude 可见输出，飞书只收最终结果 |
| 群白名单 | ✅ | 可限定只响应指定会话 |
| 会话作用域可配 | ✅ | 群+人 / 整群 / 按人 三种 |
| macOS 双击启动器 | ✅ | `run_bridge.command`，崩溃自动重启 |

> 仅供学习与自建使用。后续功能规划见文末 [路线图](#-路线图)。

---

## ✨ 特性详解

- **自动连接 + 断线重连**：lark-oapi WebSocket 长连接，网络抖动自动恢复。
- **私聊直接回复；群聊仅在 @ 机器人时回复**：避免群内刷屏。
- **持久化会话**：每个会话首条自动新建 session，之后 `claude --resume` 续接；映射落盘到 `state_dir`，桥重启后上下文不丢。
- **多会话并行隔离**：不同会话独立执行互不阻塞；同一会话加锁串行，保证上下文顺序。
- **消息去重 + 启动闸门**：飞书 at-least-once 投递下，WebSocket 重连/桥重启都不会重复执行历史消息。
- **富文本输出**：回复默认包成飞书 interactive card（schema 2.0），原生渲染 Markdown；也可让 Claude 输出 `<<<CARD>>>` + 卡片 JSON 直接透传，实现按钮、多列等复杂卡片。
- **安全默认**：工具白名单默认**不含 `Bash`**，从能力上禁止 Claude 自行执行命令；发送只由桥经「回复原消息」完成，永远回到来源会话，杜绝发错群。

---

## 🗂️ 项目结构

```text
.
├── src/feishu_claude_bridge.py   # 桥核心实现与运行入口
├── tests/                        # 不连飞书网络的核心单元测试
├── config.example.json           # 配置模板；复制为 config.json 后填真实凭证
├── run_bridge.command            # macOS 双击启动器
├── README.md
├── CHANGELOG.md
└── requirements.txt
```

---

## 🚀 安装

```bash
pip install -r requirements.txt
# 并确保本机已安装并登录 Claude Code CLI（命令 `claude` 可用）
```

依赖：仅 `lark-oapi>=1.4`（飞书开放平台官方 SDK）。运行需 **Python ≥ 3.10**（代码使用 `str | None` 语法）。

---

## ⚙️ 配置

**所有配置集中在项目根目录的 `config.json`**（已被 `.gitignore`，不会上传，请勿提交）。
仓库提供 `config.example.json`，首次部署可复制一份再填真实凭证：

```bash
cp config.example.json config.json
```

```jsonc
{
  "app_id": "cli_xxxxxxxxxxxx",     // 必填：飞书应用凭证
  "app_secret": "xxxxxxxxxxxxxx",   // 必填

  // 以下可选，不写就用默认值
  "model": "claude-opus-4-8",       // claude -p 使用的模型
  "workdir": "/path/to/workdir",    // claude 运行目录(决定会话上下文/CLAUDE.md归属)，默认项目根目录
  "state_dir": "~/.feishu_bridge",  // session 持久化目录
  "timeout": 600,                   // 单条超时秒
  "max_attachment_bytes": 26214400, // 单个附件下载上限，默认 25MB
  "stream_terminal": true,          // 终端实时打印 Claude 可见输出，飞书仍只收最终结果
  "terminal_stream_format": "text", // text(基础档) | json(结构化档)
  "session_scope": "chat_user",     // 会话隔离: chat_user(群按"群+人") | chat(整群共享) | user(按人)
  "allowed_tools": "Read Write Edit Glob Grep WebSearch WebFetch Skill TodoWrite Task",
  "allowed_chats": ["oc_xxx"]       // 群白名单; 省略=对所有会话响应
}
```

- `app_id` / `app_secret`：在飞书开放平台「凭证与基础信息」获取。
- `workdir` / `state_dir` / `BRIDGE_CONFIG` 支持 `~` 和环境变量展开。
- 应用需开启：机器人能力、接收消息事件（`im.message.receive_v1`）、`im:message` 等读写权限，并加入目标群。
- 如需把配置放别处，设环境变量 `BRIDGE_CONFIG=/path/to/your.json`。

---

## 🖥️ 终端流式输出

长任务执行时，桥可以把 Claude Code 暴露的可见输出实时打印到本机终端，飞书消息仍然只发送最终结果。

- `stream_terminal: true`：开启终端流式输出；设为 `false` 可恢复为结束后一次性打印。
- `terminal_stream_format: "text"`：基础档，实时打印 Claude 最终文本流，最稳妥。
- `terminal_stream_format: "json"`：结构化档，使用 `claude -p --output-format stream-json --verbose`，终端会尽量展示系统事件、工具调用、工具结果和最终结果。

说明：这里展示的是 Claude Code CLI 对外暴露的执行事件与输出，不会展示模型不可见的私密思考链。

---

## ▶️ 运行

```bash
cd feishu-claude-bridge
python3 src/feishu_claude_bridge.py
```

macOS 用户也可**双击 `run_bridge.command`** 在独立 Terminal 窗口运行（关闭窗口即停止；异常崩溃会自动重启，致命错误如缺配置则不重启）。

---

## 💬 会话命令

群里或私聊发送 `/new`、`/reset`、`新会话`、`重置会话`，可在当前会话开启一个全新 session（清空上下文）。

---

## 🎨 回复格式

- **默认**：Claude 直接输出 Markdown，桥自动按 20000 字符分块并包成飞书 schema 2.0 卡片。
- **图片**：回复中写 `<<<IMG>>>相对路径.png`（整行）或 Markdown `![](本地路径)`，桥会把本地图片上传飞书并作为独立图片消息发出；指向网络 URL 的图片则留在文本里由 Markdown 渲染。
- **进阶**：Claude 输出以 `<<<CARD>>>` 开头紧跟合法 interactive card JSON，桥原样透传（用于按钮、多列、标题栏等 Markdown 无法表达的场景）。

---

## 📥 接收消息（图片 / 文件）

支持文本、**图片**、**文件**，以及富文本（`post`，含其中夹带的图片/文件）。收到附件时：

1. 桥把图片/文件下载到 `WORKDIR/.inbox`（已 gitignore）；
2. 把本地路径拼进提示词交给 `claude -p`，由 Claude 用 Read 等工具查看后处理；
3. 处理完自动删除临时文件。

典型用途：发截图让我识别/诊断、发营业执照或名片让我提取信息、发 PDF 让我阅读总结等。

> 说明：下载目录放在 `WORKDIR` 内（而非 `state_dir`），以适配 `claude -p` 沙箱通常只允许读工作目录内文件的限制。群聊中仍需 @ 机器人才会处理；私聊直接处理。

---

## 🔒 安全说明

- `config.json` 含真实密钥，**已 gitignore，切勿提交**。
- 默认工具白名单不含 `Bash`：从能力上禁止 Claude 自行执行命令；发送只由桥经「回复原消息」完成，永远回到来源会话。
- 群聊只在确认 @ 的对象是机器人自身时响应；若临时无法获取机器人身份，会保守地暂不响应群聊 @，避免误触发。
- 附件下载有大小上限，默认单个 25MB，可通过 `max_attachment_bytes` 调整。

---

## 🧭 版本管理

- 版本号定义在 `src/feishu_claude_bridge.py` 的 `__version__`，启动时打印。
- 变更记录见 [CHANGELOG.md](./CHANGELOG.md)，遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## 🗺️ 路线图

> 以下为后续计划方向，尚未实现，欢迎补充。

- [x] 发送图片（v0.0.2）
- [x] 接收图片 / 文件 / 富文本附件并交给 Claude 处理（v0.0.3）
- [x] 终端流式输出：基础文本档 + stream-json 结构化档（v0.0.5）
- [ ] 丰富 stream-json 结构化事件展示
  - 统一事件时间线格式：`system` / `assistant` / `tool` / `tool_result` / `hook` / `error` / `result`
  - 常见工具友好摘要：`Read` 显示文件路径，`Edit` 显示目标文件，`Bash` 显示命令，`WebFetch` 显示 URL
  - 工具调用与结果配对，显示每次工具调用耗时
  - 长工具结果自动折叠，只显示摘要或前几行
  - 错误、超时、权限失败使用更醒目的终端日志
  - 并发会话增加 request/session 前缀，避免多人同时使用时日志混杂
- [ ] 接收语音 / 音视频等其它媒体类型
- [ ] 流式回复（边生成边更新卡片）
- [ ] 更细的权限与速率控制
- [ ] 可选的 systemd / launchd 守护进程方式部署
