# 开发文档（Development）

> 项目快照：**v0.4.0**（2026-06-18）。本文记录当前架构、运行时数据流、关键设计决策与测试方式，
> 以及未来的开发与优化计划。版本策略与各版本范围见 [VERSIONING.md](./VERSIONING.md)，逐版本变更见
> [../CHANGELOG.md](../CHANGELOG.md)。

---

## 一、项目快照

### 1.1 定位

把飞书消息接到本机已登录的 Agent CLI（Claude Code / Codex），做成一个无头网关：

```
飞书用户 ──消息──▶ lark-oapi WebSocket ──▶ bridge ──▶ claude -p / codex exec ──▶ 回复原消息
```

大脑复用本机已登录 CLI（无需 API Key）；收发复用 `lark-oapi`（WebSocket 长连接 + 断线重连）。

### 1.2 架构总览（三模块，单向无环依赖）

```
config  ←  agents  ←  feishu_agent_bridge
（配置/路径/Agent配置）（Agent抽象+CLI调用）（飞书IM+会话+派发+入口）
```

| 模块 | 职责 | 关键内容 |
|------|------|----------|
| `src/config.py` | 配置与路径中心，不碰 lark/IM | 读 `config.json`；`_resolve_dirs` 按 `app_id` 分目录；`_migrate_legacy_sessions` 旧状态迁移；`AgentConfig` 数据类 + `AGENT_CONFIGS` 注册表；模型别名 `_resolve_model`；共享小工具 `_ts` / `_cli_bin` / `_safe_app_id` |
| `src/agents.py` | Agent 抽象与底层 CLI 调用，只用 `subprocess` | `Agent` 基类 + `ClaudeAgent` / `CodexAgent`；统一 `run(prompt, sid, is_new, model, text_delta_cb) -> AgentResult`；`get_agent()` / `AGENTS` 注册表；`_StreamJsonRenderer`（Claude stream-json 事件流）；`_run_claude_streaming` / `_run_codex_streaming`；命令构造 `_build_claude_cmd` / `_build_codex_cmd` |
| `src/feishu_agent_bridge.py` | 飞书 IM 收发、会话持久化与并发、派发、进程入口 | lark client；`on_message` 派发；`ask_agent` 薄派发层；会话持久化 (`SESSIONS` / `sessions.json` / `_normalize_session_record`)；`_SESSION_MODELS` 临时模型；`CardStreamer` 流式卡片；附件下载 `_download_resource`；消息解析 `_parse_message`；`reply_to` / 图片上传 |

新增一个 Agent：在 `agents.py` 加一个 `Agent` 子类，在 `config.py` 的 `AGENT_CONFIGS` 加一条
`AgentConfig`（声明 `display_name` / `bin` / `default_model` / `supports_stream_reply` /
`supports_model_switch` / `pregenerate_sid` 等能力位）。派发层 `ask_agent` 依据能力位自动适配，无需改动。

### 1.3 一条消息的生命周期

1. **接收**（lark 派发线程）：WebSocket 收到 `im.message.receive_v1` → `on_message`。
2. **过滤**：`allowed_chats` 白名单 → 群聊必须 @ 机器人（`_bot_mentioned`，取不到 bot open_id 时 **fail-closed** 不响应）。
3. **解析**：`_parse_message` 支持 `text` / `image` / `file` / `post`，剥离 @ 提及，抽出附件。
4. **防重**：启动闸门（丢弃 `create_time < START_TIME_MS` 的旧事件，应对飞书 at-least-once 重投）+ `_seen_recently` 按 message_id TTL 去重。
5. **定位会话**：按 `session_scope` 生成 key（`chat_user` 默认 = `c:<chat>:u:<uid>`）。
6. **命令**：依次处理 `/agent`、`/new`·`/reset`、`/model`、单条前缀 `[m:xxx]`（纯文本且无附件时）。
7. **执行**（每条消息起一个 daemon worker 线程）：加 ⌨️ Typing reaction → 附件下载到 `workspace/inbox` → 拼提示词 → `_key_lock(key)` **同会话串行、异会话并行** → `ask_agent`。
8. **派发** `ask_agent`：`get_agent(name)` →
   - `pregenerate_sid`（Claude）：bridge 预生成 uuid `--session-id` / 续接 `--resume`；
   - 否则（Codex）：传已有 sid 或 None，CLI 自管 session id，运行后回传 `new_sid` 落盘；
   - `supports_model_switch` 决定是否套用会话/单条模型覆盖；`supports_stream_reply` 决定是否传流式回调。
9. **底层**：`subprocess` 起 `claude -p` / `codex exec`，stdout/stderr 经线程+队列读出，渲染到终端时间线；Claude 的 stream-json 增量文本经 `text_delta_cb` 推给 `CardStreamer`。
10. **回复**：`CardStreamer.finish` 收尾流式卡片；失败或未开流式则 `reply_to(message_id, ...)` 回复原消息（**永远落回来源会话**）。worker 结束清理 inbox 临时文件。

### 1.4 会话、状态与隔离

- **状态文件**：`<state_dir>/sessions.json`，记录形如
  `{ "<key>": {agent, created_at, chat_id, chat_type, chat_name, sessions:{claude:{sid}, codex:{sid}}, models} }`。
  原子写（`.tmp` + `os.replace`，utf-8）；损坏自动备份为 `.corrupt-*` 并重建；旧结构经 `_normalize_session_record` 懒迁移。
- **临时模型** `_SESSION_MODELS`：`/model` 设置的会话级覆盖，仅存内存，重启失效；优先级 单条 `[m:]` > 会话级 > Agent 默认。
- **多应用隔离**：「一进程一应用」。`state_dir` / `workdir` 未显式配置时按 `app_id` 落子目录
  （`~/.feishu_bridge/<app_id>/`、`<项目>/workspace/<app_id>/`），保证不同应用的 sessions、workspace、inbox 互不串扰。`run_multi.py` 为 `configs/*.json` 每份起一个独立子进程并监控重启。

### 1.5 并发模型

- lark 派发线程负责 `on_message`（轻量、快速返回）；每条消息的实际处理在独立 daemon worker 线程。
- 锁：`_key_lock(key)` 同会话串行；`_sessions_guard` 保护 `SESSIONS` 读写；另有 dedup / token cache / bot open_id 各自的锁。
- 子进程 I/O：每个 CLI 进程的 stdout/stderr 各起一个读线程灌入 `queue.Queue`，主循环消费并按超时 `kill`。

### 1.6 关键设计决策（为什么这么做）

- **一进程一应用，而非单进程多应用**：独立内存/连接/崩溃域，隔离最彻底，实现最简单；不把 `app_id` 编进 session key。
- **迁移用 move 而非 copy**：旧顶层 `sessions.json` 搬入应用子目录。若用 copy，多个新应用会各自继承同一批旧 session id → 跨应用「串话」，反而破坏隔离。
- **回复只走 `reply_to`（回复原消息）**：从机制上杜绝 Agent 把消息发错群；配合 Claude 默认工具白名单不含 `Bash`，断掉它自己 curl 飞书 API 的能力。
- **群聊 @ 检测 fail-closed**：拿不到 bot open_id 时宁可不响应，避免误触发刷屏。
- **启动闸门 + TTL 去重**：飞书 at-least-once 投递，重启会重投旧事件；两道防线避免重复执行。
- **Agent 无状态**：sid / model 由 bridge 解析后作为参数传入，`agents.py` 不碰会话持久化，便于测试与扩展。

### 1.7 测试

- 布局：`tests/_bridge_test_base.py` 提供共享基类 `BridgeTestCase`——搭临时 `HOME` / `workdir` / `config.json`，
  pop 并重新导入 `config` / `agents` / `feishu_agent_bridge` 三模块（彼此隔离、对临时环境生效），子类用
  `cls.config` / `cls.agents` / `cls.bridge` 访问。
- 用例：`test_bridge_core.py`（配置/会话/IM 层）、`test_agents.py`（命令构造、事件解析、`_StreamJsonRenderer`、
  统一 `run()`、**Codex 流式回归守卫**）、`test_multi_app.py`（`_safe_app_id` / `_resolve_dirs` / 迁移三种情形）。
- 运行（本机默认 `python3` 是 3.9，需 3.10+；示例用 anaconda 3.12）：
  ```bash
  /opt/anaconda3/bin/python3.12 -m pytest tests/ -q       # 或 python3 -m unittest discover -s tests
  /opt/anaconda3/bin/python3.12 -m py_compile src/*.py run_multi.py
  ```
  当前：**32 passed, 1 skipped**（跳过的是 Windows-only 的 CLI 发现用例）。

---

## 二、未来开发与优化计划

### 2.1 近期（小步、低风险）

- **Codex 流式卡片**：解析 `codex exec --json` 的文本增量，接到 `CardStreamer`，让 Codex 也能边生成边更新（目前降级为最终回复一次性发出）。
- **Windows 多应用启动器**：补 `run_multi.cmd`（现仅有跨平台 `run_multi.py` 与 macOS `run_multi.command`）。
- **`run_multi.py` 健壮性**：子进程输出带时间戳/应用色彩；父进程被 `kill -9` 时的孤儿子进程兜底（如写 pid 文件、进程组终止）。
- **配置校验前置**：启动时对 `app_id` 格式、CLI 可达性给更早更清晰的体检输出。

### 2.2 中期（能力扩展）

- **更多 Agent**：接口已就绪，可接入新的 CLI 后端（按 `AgentConfig` 能力位声明即可）。
- **权限 / 审计 / 限流**：按 chat 或 user 维度的速率限制、操作审计日志、更细的工具白名单分层。
- **部署模板**：launchd（macOS）/ systemd（Linux）/ Docker，配合 `run_multi.py` 做托管与开机自启。
- **配置热重载**：当前改 `config.json` 需重启；可加监听或 `/reload` 指令（注意与运行中会话的兼容）。
- **可观测性**：结构化日志输出、基本指标（消息量、时延、失败率）。

### 2.3 已知限制 / 技术债

- `_SESSION_MODELS` 跨线程读写无显式锁：CPython GIL 下单次 dict 操作实际原子，风险极低；如需严格可纳入 `_sessions_guard`。
- 单进程多应用未做（有意取舍，隔离优先）；多应用规模上去后是「N 进程」的资源开销。
- Codex 事件 schema 依赖具体 CLI 版本（如 `turn.failed` 等）；CLI 升级后需回归 `agents.py` 的事件解析。
- `workspace/` 只在启动清扫 `inbox/`，不清理 Agent 生成的产物，长期可能堆积，需要时手动清理。
- 附件仅支持 `image` / `file` / `post` 内嵌媒体；`audio` / `sticker` 等暂不支持。
- 测试不覆盖真实 WebSocket 连接与真实 CLI 调用（靠 monkeypatch + 冒烟）；端到端验证仍需真实环境。
