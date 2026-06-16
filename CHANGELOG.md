# 更新日志

本项目的所有重要变更都记录在此文件。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

- 暂无

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

[Unreleased]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/karlliuforai-max/Feishu-ClaudeCode-Bridge/releases/tag/v0.0.1
