# feishu-agent-bridge

> **v0.4.0** · 飞书 ↔ Claude Code / Codex 网关 · [CHANGELOG](./CHANGELOG.md) · [开发文档](./docs/DEVELOPMENT.md)

把**飞书消息**接到本机已登录的 **Claude Code CLI** 或 **Codex CLI**：飞书私聊/群里发消息 → 交给 Agent 处理 → 回复原消息。收发由 `lark-oapi` 的 WebSocket 长连接完成（自动重连），大脑复用你本机已登录的 CLI，无需 API Key。

- **Claude**：`claude -p`，默认模型 `claude-sonnet-4-6`，支持运行时 `/model` 切换与流式卡片。
- **Codex**：`codex exec`，固定模型 `gpt-5.5`，通过沙箱控制执行能力。
- 同一会话用 `/agent` 在两者间无缝切换，各自独立保存上下文。

## 能力一览

| 能力 | 状态 |
|------|:---:|
| 飞书 ↔ Agent 双向桥接（私聊直达 / 群聊 @ 触发） | ✅ |
| `/agent` 运行时切换 Claude / Codex，各自独立会话 | ✅ |
| `/model`、`[m:...]` 临时切换 Claude 模型 | ✅ |
| 会话持久化 + 重启续接（旧状态自动迁移） | ✅ |
| 多会话并行隔离（异会话并行，同会话串行） | ✅ |
| 图片/文件接收、图片发送、富文本卡片、自定义卡片透传 | ✅ |
| Claude 流式卡片回复（边生成边更新） | ✅ |
| **多飞书应用并行，会话完全隔离** | ✅ |
| Codex 流式卡片回复 | ↩️ 降级为最终回复 |

## 快速开始

需要 **Python ≥ 3.10**，且本机已安装并登录 `claude` 与 `codex` CLI。

```bash
pip install -r requirements.txt          # 唯一依赖：lark-oapi
cp config.example.json config.json       # 填入 app_id / app_secret
python src/feishu_agent_bridge.py         # 或双击 run_bridge.command (macOS) / run_bridge.cmd (Windows)
```

飞书应用需开启机器人能力、订阅 `im.message.receive_v1` 事件、具备 `im:message` 读写权限，并把机器人加入目标群。

## 配置

`config.json`（不入库）只有 `app_id` / `app_secret` 必填，其余可缺省：

| 字段 | 默认 | 说明 |
|------|------|------|
| `default_agent` | `claude` | 默认后端：`claude` 或 `codex` |
| `model` | `claude-sonnet-4-6` | Claude 默认模型 |
| `codex_model` | `gpt-5.5` | Codex 模型（当前只允许 `gpt-5.5`） |
| `codex_sandbox` | `workspace-write` | 传给 `codex exec --sandbox` |
| `claude_bin` / `codex_bin` | `claude` / `codex` | CLI 路径；先查 `PATH`，找不到可填绝对路径 |
| `workdir` | `<项目>/workspace/<app_id>` | Agent 工作目录（cwd），下载与产物都落这里 |
| `state_dir` | `~/.feishu_bridge/<app_id>` | 会话状态目录（`sessions.json`） |
| `allowed_tools` | 见示例（不含 `Bash`） | 仅作用于 Claude |
| `session_scope` | `chat_user` | `chat_user`(群+人) / `chat`(整群共享) / `user`(按人) |
| `stream_reply` | `false` | 开启 Claude 流式卡片回复 |
| `timeout` | `600` | Agent 单次处理超时（秒） |
| `max_attachment_bytes` | `26214400` | 附件下载上限（25MB） |
| `allowed_chats` | `[]` | 群白名单；空=响应所有会话 |

完整字段见 [config.example.json](./config.example.json)。`workdir` / `state_dir` 不设时**按 `app_id` 自动分目录**，这是多应用隔离的关键；环境变量 `BRIDGE_CONFIG` 可指定其它配置文件路径。

## 会话命令

```text
/agent              查看当前 Agent 与模型
/agent claude       切到 Claude        /agent codex   切到 Codex      /agent reset  恢复默认
/model              查看 Claude 会话模型
/model opus|sonnet|haiku|fable | <完整ID>     /model reset 恢复默认
[m:opus] 你的问题    仅这一条用指定模型
/new  /reset  新会话  重置会话           只清当前 Agent 的上下文
```

`/model` 仅当前 Agent 为 Claude 时生效；Codex 固定 `gpt-5.5`。

## 接多个飞书应用（会话完全隔离）

「一进程一应用」：每应用一份独立 config、独立进程、独立 WebSocket。只要不显式设 `state_dir` / `workdir`，各应用的 `sessions.json` 与 `workspace` 会按各自 `app_id` 自动分目录，彼此不串扰。

```bash
cp configs/app-a.example.json configs/app-a.json   # 填 A 应用凭证
cp configs/app-a.example.json configs/app-b.json   # 填 B 应用凭证
python run_multi.py                                  # 自动发现 configs/*.json，一进程一应用
```

`run_multi.py` 会为每个 config 拉起一个独立子进程、崩溃自动重启、日志按应用名前缀、Ctrl-C 全停；macOS 可双击 `run_multi.command`。也可手动 `BRIDGE_CONFIG=configs/app-a.json python src/feishu_agent_bridge.py` 分别启动。`configs/*.json` 含密钥，已被忽略，仓库仅留 `*.example.json`。

## 回复与附件

- **文本**：Agent 输出的 Markdown 自动按 2 万字符分块包成飞书 schema 2.0 卡片。
- **发图片**：回复里写 `<<<IMG>>>路径` 或 `![](本地路径)`，自动上传为飞书图片消息。
- **自定义卡片**：输出以 `<<<CARD>>>` 开头并跟合法 interactive card JSON 时原样透传。
- **收附件**：文本/图片/文件/富文本里的图片与文件下载到 `workspace/inbox`，把本地路径交给 Agent，处理完即删（启动时也清扫残留）。

## 项目结构

```text
src/config.py               配置加载、运行目录、各 Agent 静态配置(AgentConfig)
src/agents.py               Agent 抽象：基类 + ClaudeAgent + CodexAgent + 统一 run() + 注册表
src/feishu_agent_bridge.py  飞书 IM 收发、会话持久化与并发隔离、消息派发、进程入口
run_bridge.command / .cmd   单应用启动器（macOS / Windows）
run_multi.py / .command     多应用监控启动器
configs/                    多应用模式：每应用一份 config（gitignore，仅留 *.example.json）
tests/                      单元测试（unittest / pytest 均可）
docs/                       VERSIONING.md（版本策略）、DEVELOPMENT.md（架构与路线图）
```

依赖单向无环 `config ← agents ← feishu_agent_bridge`；新增一个 Agent 只需在 `agents.py` 加一个 `Agent` 子类、在 `config.py` 的 `AGENT_CONFIGS` 加一条配置，派发层不动。详见 [docs/DEVELOPMENT.md](./docs/DEVELOPMENT.md)。

## 安全

- `config.json` / `configs/*.json` 含密钥，已被 `.gitignore` 忽略，勿提交。
- 回复只走「回复原消息」，从机制上避免 Agent 把消息发错群；Claude 默认工具白名单不含 `Bash`。
- Codex 默认 `workspace-write` 沙箱；附件下载有大小上限（默认 25MB）。

## 文档

- 变更记录：[CHANGELOG.md](./CHANGELOG.md)
- 版本策略与各版本范围：[docs/VERSIONING.md](./docs/VERSIONING.md)
- 架构快照与开发/优化路线图：[docs/DEVELOPMENT.md](./docs/DEVELOPMENT.md)
