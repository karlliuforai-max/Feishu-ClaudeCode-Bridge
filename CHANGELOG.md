# 更新日志

本项目的所有重要变更都记录在此文件。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

- 暂无

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

[Unreleased]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/compare/v0.0.5...HEAD
[0.0.5]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/compare/v0.0.4...v0.0.5
[0.0.4]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/compare/v0.0.3...v0.0.4
[0.0.3]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/compare/v0.0.2...v0.0.3
[0.0.2]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/releases/tag/v0.0.1
