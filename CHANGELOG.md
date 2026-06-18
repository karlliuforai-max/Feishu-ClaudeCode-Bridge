# 更新日志

本项目的所有重要变更都记录在此文件。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- 新增 Windows 双击启动脚本 `run_bridge.cmd`：在前台控制台运行服务，关闭窗口即停止服务。

## [0.2.1] - 2026-06-18

### 新增

- 新增 `claude_bin` / `codex_bin` 配置项，可显式指定 Claude Code CLI 与 Codex CLI 的可执行文件路径。

### 修复

- 修复 Windows 普通终端启动桥接服务时，因 `codex` 不在 `PATH` 中导致 Codex 后端报 `[WinError 2] 系统找不到指定的文件` 的问题。

### 文档

- README 和配置模板补充 CLI 路径配置说明。

## [0.2.0] - 2026-06-18

### 新增

- **Agent Gateway**：同一个飞书机器人、同一个 `app_id` 现在可通过 `/agent` 在 Claude Code 与 Codex 之间切换。
- 新增 `/agent` 命令：
  - `/agent` 查询当前 Agent 与模型。
  - `/agent claude` 切换到 Claude Code。
  - `/agent codex` 切换到 Codex。
  - `/agent reset` 恢复配置里的默认 Agent。
- 新增 Codex 后端：使用 `codex exec` / `codex exec resume` 无头执行，并通过 `--output-last-message` 捕获最终回复。
- 新增 Agent 独立 session 状态：Claude 和 Codex 的会话 ID 分开保存，切换 Agent 不污染对方上下文。
- 新增 Codex 配置项：`codex_model`、`codex_sandbox`、`codex_skip_git_repo_check`。
- 新增 `docs/VERSIONING.md` 与 v0.1.0 设计文档，记录版本边界、状态迁移和发布检查项。

### 变更

- 运行时版本升为 `0.2.0`。
- Claude 默认模型从 `claude-opus-4-8` 调整为 `claude-sonnet-4-6`。
- Codex 在 v0.2.0 固定使用 `gpt-5.5`，不参与 `/model` 或 `[m:...]` 切换。
- `/new` / `/reset` 现在只重置当前激活 Agent 的 session，不清空另一个 Agent 的上下文。
- README 更新为飞书 Agent Gateway 说明，补充 Claude/Codex 双后端配置与命令。

### 迁移

- 旧 `sessions.json` 中的字符串 sid 和顶层 `sid` 对象会懒迁移为多 Agent 状态结构。
- 旧配置继续兼容；不配置 `default_agent` 时默认仍为 `claude`。

### 测试

- 补充默认模型、旧 session 迁移、`/agent` 切换、Codex 命令构造等单元测试。
- 在缺少 `lark-oapi` 的本地测试环境中，模块可用最小 fallback 完成核心单元测试导入。

## [0.0.7] - 2026-06-17

### 新增

- **临时模型切换**：无需改配置文件即可在运行时动态切换 Claude 模型，支持两种粒度：
  - **会话级**：发送 `/model <名称>` 切换当前会话后续所有消息使用的模型；`/model reset` 恢复全局默认；`/model`（不带参数）查询当前生效的模型。
  - **单条级**：消息以 `[m:<名称>]` 开头，仅本条使用该模型，下一条自动恢复，前缀不传入提示词。
  - 支持短名（`opus` / `sonnet` / `haiku` / `fable`）和完整 model ID（如 `claude-sonnet-4-6`）。
  - 优先级：单条前缀 > 会话级 `/model` > `config.json` 全局默认。会话级覆盖仅存内存，重启自动失效。
  - 输入无法识别的名称时回复提示，不会拿错模型运行。

## [0.0.6] - 2026-06-16

### 新增

- **流式回复（边生成边更新卡片）**：新增配置 `stream_reply`，开启后用飞书 CardKit 流式卡片把 Claude 的输出「边生成边更新」，用户可实时看到打字机式回复，无需等到全部生成完。
  - 默认关闭，保留「生成完一次性回复」作为稳妥兜底；建卡 / 发卡 / 更新任一环节失败都会自动退回普通卡片回复，不会丢消息。
  - 开启时强制走 `stream-json` 获取增量文本（与 `terminal_stream_format` 无关）。
  - 新增 `stream_reply_interval`（默认 0.7s）控制卡片最小刷新间隔，避免触发飞书更新限频（50 次/秒、1000 次/分）。
  - 收尾时写入去掉本地图片引用后的最终文本并关闭流式；本地图片仍作为独立图片消息补发。自定义 `<<<CARD>>>` 卡片不参与流式，自动退回普通透传。
- **stream-json 结构化事件展示增强**：终端日志改为统一时间线格式。
  - 常见工具友好摘要：`Read`/`Write`/`Edit` 显示文件、`Bash` 显示命令、`Grep`/`Glob` 显示模式、`WebFetch`/`WebSearch` 显示 URL/查询、`TodoWrite` 显示待办数。
  - 工具调用 `[tool ▶]` 与结果 `[tool ✓ Ns]` / `[tool ✗]` 配对，并显示每次调用耗时。
  - 长工具结果自动折叠（默认前 4 行 / 500 字），超出给出统计提示。
  - 错误 / 超时 / 工具失败用 `⚠️` 醒目标记。
  - 每条事件加 `[session 前缀]`（会话短 ID），多会话并发时日志不再混杂。

### 维护

- 重构 stream-json 处理为 `_StreamJsonRenderer`，承担终端渲染、增量文本回调与最终文本汇总三职责；补充渲染器、工具摘要、结果折叠与流式卡片的单元测试。

## [0.0.5] - 2026-06-16

### 新增

- 新增终端流式输出配置：`stream_terminal` 开启后会在终端实时打印 Claude Code 可见输出，飞书仍只发送最终结果。
- 新增两档终端流格式：`terminal_stream_format: "text"` 使用基础文本流，`"json"` 使用 Claude Code `stream-json` 事件流并尽量展示系统事件、工具调用和最终结果。

### 文档

- 在 README 路线图中补充 stream-json 结构化事件展示的后续增强计划。

## [0.0.4] - 2026-06-16

### 修复

- 配置路径现在会展开 `~` 和环境变量，避免照抄 `state_dir: "~/.feishu_bridge"` 时生成字面量 `~` 目录。
- 启动时间闸门提前记录，避免启动期网络探测耗时导致新消息被误判为旧消息。
- 群聊 @ 判断改为 fail-closed：拿不到机器人 `open_id` 时暂不响应群聊 @，避免退化成“任意 @ 都触发”。
- `sessions.json` 损坏时会备份为 `.corrupt-*` 文件并记录警告，不再静默丢失上下文映射。

### 安全

- 附件下载增加默认 25MB 单文件上限，并对落地文件名做清洗和唯一化，减少内存占用和路径/覆盖风险。

### 维护

- 整理项目结构：核心脚本移动到 `src/feishu_claude_bridge.py`，新增 `config.example.json` 配置模板。
- 新增不依赖飞书网络的核心单元测试，覆盖配置路径、群聊 @ 判断、session 损坏备份、消息解析和附件大小限制。

## [0.0.3] - 2026-06-16

### 新增

- **接收图片与文件**：放开此前"仅文本"的限制，现在支持 `image`（图片）、`file`（文件）、`post`（富文本，含其中夹带的图片/文件）消息。桥会把图片/文件下载到本地 `WORKDIR/.inbox`，把路径拼进提示词交给 `claude -p`，由 Claude 用 Read 等工具查看后处理（如截图识别、营业执照/名片提取、PDF 阅读等）。处理完自动删除临时文件。
  - 下载目录放在 `WORKDIR/.inbox`（而非 `state_dir`），以适配 `claude -p` 沙箱通常只允许读工作目录内文件的限制。
  - 群聊中仍需 @ 机器人才响应；私聊直接处理。
  - 下载失败时降级提示用户重发，不静默丢弃。

## [0.0.2] - 2026-06-16

### 新增

- **图片发送**：回复中夹带本地图片时，桥会先上传到飞书换取 `image_key`，再作为独立 image 消息发出。支持两种写法：整行指令 `<<<IMG>>>路径`，或 Markdown `![](本地路径)`（相对路径相对 `workdir` 解析）。指向网络 URL 的图片原样保留在文本中。

### 修复

- 修复回复中含本地图片 Markdown 时**整条消息发送失败**的问题（飞书报 `card contains invalid image keys`，且兜底直发沿用同样内容一并失败，导致连文字都发不出）。现已将本地图片从文本中摘出、单独上传发送。

### 优化

- `reply_to` 回复失败时改为**只兜底重发当前 part**，不再整段重发，避免重复消息。
- 图片上传失败时降级为一行文字路径提示，保证其余内容正常送达。

## [0.0.1] - 2026-06-16

首个可用版本：单文件实现的飞书 ↔ Claude Code 桥。

### 新增

- 飞书消息与 `claude -p` 无头模式的双向桥接，复用本机已登录的 Claude Code CLI（无需 Anthropic API key）。
- 基于 `lark-oapi` WebSocket 长连接，自动连接 + 断线自动重连。
- 私聊直接回复；群聊仅在 @ 机器人时回复，避免刷屏。
- 持久化会话：chat→session 映射落盘 + `claude --resume` 续接，重启不丢上下文。
- 多会话并行隔离：不同会话独立执行，同一会话加锁串行。
- 消息去重 + 启动闸门：抵御飞书 at-least-once 投递在重连/重启后的重复消费。
- 富文本输出：回复默认包成飞书 schema 2.0 interactive card；支持 `<<<CARD>>>` 前缀透传自定义卡片 JSON。
- 安全默认：工具白名单默认不含 `Bash`，发送只由桥经"回复原消息"完成，杜绝发错会话。
- 会话命令：`/new`、`/reset`、`新会话`、`重置会话` 可在当前会话开启全新 session。
- 启动器 `run_bridge.command`：macOS 双击即在 Terminal 运行，异常崩溃自动重启。

### 优化

- 启动器改用 `python3`（不再写死 `python3.12`），并对致命退出码（配置缺失、找不到解释器等）不再无限重启。
- 修复 `_load_sessions` 的文件句柄泄漏（改用 `with open`）。
- 终端日志精简，SDK 日志降到 WARNING，过滤连接/心跳噪音。

### 其他

- 新增 `requirements.txt` 锁定依赖 `lark-oapi>=1.4`。

[Unreleased]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/compare/v0.0.7...v0.2.0
[0.0.7]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/compare/v0.0.6...v0.0.7
[0.0.6]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/compare/v0.0.5...v0.0.6
[0.0.5]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/compare/v0.0.4...v0.0.5
[0.0.4]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/compare/v0.0.3...v0.0.4
[0.0.3]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/compare/v0.0.2...v0.0.3
[0.0.2]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/releases/tag/v0.0.1
