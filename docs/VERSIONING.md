# 版本管理

本项目遵循语义化版本。

当前版本是 `v0.4.1`。

## 版本来源

运行时版本定义在：

```text
src/feishu_agent_bridge.py
```

当前变量：

```python
__version__ = "0.4.1"
```

## 发布策略

版本号格式为 `MAJOR.MINOR.PATCH`。

- Patch：缺陷修复和不改变行为的维护。
- Minor：向后兼容的新能力。
- Major：配置、状态、命令或部署方式存在不兼容变更。

v0.4.1 属于 Patch：修复多应用同时首启的 sessions 迁移竞态（旧文件只被同 `app_id` 的归属应用认领），并消除「机器人/成员进出群」事件的 `processor not found` 日志噪音；不改配置、状态结构或命令语义。

v0.4.0 属于 Minor：它新增「多飞书应用并行」能力（一进程一应用 + `run_multi.py` 监控启动器），并把 `state_dir`/`workdir` 的默认目录改为按 `app_id` 分子目录以实现会话自动隔离；通过一次性迁移把旧顶层 `sessions.json` 搬入应用子目录，保证现有单应用不丢上下文。`/agent`、`/model`、飞书消息语义与 session 状态结构保持兼容。

v0.3.0 属于 Minor：它把单文件拆成 `config.py` / `agents.py` / `feishu_agent_bridge.py` 三模块、把 agent 工作目录默认改到项目根下的 `workspace/`，并修复 Codex 启动崩溃；`/agent`、`/model`、飞书消息语义与 session 状态结构保持兼容。注意主文件已由 `feishu_claude_bridge.py` 改名为 `feishu_agent_bridge.py`，自定义启动方式需同步更新路径。

v0.2.2 属于 Patch：它增强 CLI 自动发现和错误提示，并新增 Windows 双击启动脚本，不改变 `/agent` 行为、配置兼容性或状态结构。

v0.2.1 属于 Patch：它修复 Windows 普通终端中 CLI 路径不可见导致的 Codex 启动问题，不改变 `/agent` 行为、配置兼容性或状态结构。

v0.2.0 属于 Minor：它新增 Codex 后端和 `/agent` 切换，但保留现有 Claude 行为，并兼容旧配置与旧 session 状态。

## v0.4.0 范围

v0.4.0 包含：

- 同时接多个飞书应用：一进程一应用，会话完全隔离。
- `run_multi.py` 监控启动器 + `run_multi.command`，自动发现 `configs/*.json`、崩溃自重启。
- `state_dir` / `workdir` 默认按 `app_id` 分目录；旧顶层 `sessions.json` 一次性迁移。

v0.4.0 不包含：

- 「单进程多应用」（多 ws.Client 同进程）——隔离更弱，不采用。
- 改变 `/agent`、`/model`、飞书消息语义或 session 记录结构。
- 把 `app_id` 编进 session key（隔离由每应用独立 sessions.json 保证）。

## v0.3.0 范围

v0.3.0 包含：

- 主文件改名 `feishu_claude_bridge.py` → `feishu_agent_bridge.py`。
- 单文件拆分为三模块：`config.py`（配置/路径/Agent 配置）、`agents.py`（Agent 抽象 + 统一 `run()`）、`feishu_agent_bridge.py`（IM 收发/会话/派发）。
- agent 工作目录默认改为项目根下的 `workspace/`，下载附件落 `workspace/inbox/`，启动时清扫残留。
- 修复 Codex 因误植 Claude 横幅导致的 `NameError`（v0.2.2 回归）。
- `sessions.json` 写盘补 `encoding="utf-8"`，避免 Windows 上中文群名写读编码不一致。

v0.3.0 不包含：

- 改变 `/agent`、`/model` 或飞书消息处理语义。
- 改变 session 状态结构（仍兼容旧 `sessions.json`）。
- 新增第三个 Agent 或多 Codex 模型。

## v0.2.2 范围

v0.2.2 包含：

- 新增 Windows 双击启动脚本 `run_bridge.cmd`。
- 增强 `claude_bin` / `codex_bin` 自动发现逻辑。
- Claude/Codex CLI 缺失时返回明确配置提示，不再直接暴露 `[WinError 2]`。

v0.2.2 不包含：

- 自动安装 Claude Code 或 Codex CLI。
- 改变 Claude/Codex 模型策略。
- 改变 session 状态结构。

## v0.2.1 范围

v0.2.1 包含：

- 新增 `claude_bin` / `codex_bin` 配置项。
- 修复 `codex` 不在 `PATH` 时的 `[WinError 2]`。
- README 和配置模板补充 CLI 路径说明。

v0.2.1 不包含：

- 改变 Claude/Codex 模型策略。
- 改变 session 状态结构。
- 改变 `/agent`、`/model` 或飞书消息处理语义。

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
