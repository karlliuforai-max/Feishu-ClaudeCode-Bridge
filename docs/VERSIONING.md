# 版本管理

本项目遵循语义化版本。

当前版本是 `v0.2.0`。

## 版本来源

运行时版本定义在：

```text
src/feishu_claude_bridge.py
```

当前变量：

```python
__version__ = "0.2.0"
```

## 发布策略

版本号格式为 `MAJOR.MINOR.PATCH`。

- Patch：缺陷修复和不改变行为的维护。
- Minor：向后兼容的新能力。
- Major：配置、状态、命令或部署方式存在不兼容变更。

v0.2.0 属于 Minor：它新增 Codex 后端和 `/agent` 切换，但保留现有 Claude 行为，并兼容旧配置与旧 session 状态。

## v0.2.0 范围

v0.2.0 包含：

- 同一个飞书 `app_id` 可在 Claude Code 和 Codex 之间切换。
- 新增 `/agent` 命令。
- Claude 默认模型改为 `claude-sonnet-4-6`。
- Codex 只使用 `gpt-5.5`。
- Agent 独立 session 持久化。
- 旧 Claude-only `sessions.json` 自动迁移。

v0.2.0 不包含：

- 多个 Codex 模型。
- Codex 侧 `/model` 切换。
- 新飞书应用或第二个机器人。
- 替换现有 Claude stream-json 渲染器。
- 改变 `allowed_tools` 含义；它仍只作用于 Claude。

## 状态兼容

v0.2.0 的状态迁移必须是懒迁移，并在数据结构层面保守：

- 不删除旧 Claude session id。
- 保留 chat 元数据。
- 保留现有 session key。
- Claude 和 Codex session 分别存储在各自 Agent 名下。
- `/agent` 切换不会清空未激活 Agent 的上下文。

如果状态迁移失败，桥应该给出清晰错误，而不是静默新建 session 导致上下文丢失。

## Changelog 规则

`CHANGELOG.md` 继续遵循 Keep a Changelog 风格。

发布后续版本前：

1. 将对应版本条目从 `Unreleased` 移出。
2. 新增带日期的 `## [X.Y.Z] - YYYY-MM-DD` 小节。
3. 更新底部 compare 链接。
4. 保留 `Unreleased` 供下一轮开发使用。

建议后续功能版本小节：

- `新增`：`/agent`、Codex adapter、Agent 独立 session。
- `变更`：Claude 默认模型改为 `claude-sonnet-4-6`。
- `迁移`：`sessions.json` 升级为多 Agent 状态结构。
- `兼容`：旧配置和旧 sessions 继续支持。

## 发布前验证

运行单元测试：

```bash
python -m unittest discover -s tests
```

手工验证：

- `claude --help` 或真实 Claude prompt 在部署环境可用。
- `codex --version` 可用。
- `codex debug models` 列出 `gpt-5.5`。
- `codex exec --model gpt-5.5` 能在配置的 `workdir` 运行。
- 飞书私聊在两个 Agent 下都能回复。
- 飞书群聊只有 @ 机器人时回复。
- `/agent codex`、`/agent claude`、`/agent`、`/new`、`/model` 行为符合文档。
