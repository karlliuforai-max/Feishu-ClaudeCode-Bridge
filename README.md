# feishu-claude-bridge

把**飞书消息**接到 **Claude Code 无头模式**（`claude -p`）的轻量桥。
群里 @ 机器人或私聊发消息，桥转给 Claude，再把回复包成飞书卡片发回——
等于给飞书装了一个能读写文件、跑工具、带持久记忆的 Claude 助手。

> 单文件实现（`feishu_claude_bridge.py`），无需 Anthropic API key——
> 复用本机已登录授权的 Claude Code CLI。

## 特性

- **自动连接 + 断线重连**（lark-oapi WebSocket 长连接）
- **私聊直接回复；群聊仅在 @ 机器人时回复**（避免刷屏）
- **持久化会话**：每个会话首条自动新建 session，之后 `claude --resume` 续接，重启不丢上下文
- **多会话并行隔离**：不同会话独立进程；同一会话加锁串行
- **消息去重**：WebSocket 重连/重投不会重复执行
- **富文本输出**：回复默认包成飞书 interactive card（schema 2.0），支持 Markdown 原生渲染；
  也可让 Claude 输出 `<<<CARD>>>` + 卡片 JSON 直接透传
- **安全默认**：工具白名单默认不含 `Bash`，禁止 Claude 自行调用外部命令乱发消息

## 安装

```bash
pip install lark-oapi
# 并确保本机已安装并登录 Claude Code CLI（命令 `claude` 可用）
```

## 配置

**所有配置集中在脚本同目录的 `config.json`**（已被 `.gitignore`，不会上传，请勿提交）。
仓库不含此文件，首次部署需自己创建：

```jsonc
{
  "app_id": "cli_xxxxxxxxxxxx",     // 必填：飞书应用凭证
  "app_secret": "xxxxxxxxxxxxxx",   // 必填

  // 以下可选，不写就用默认值
  "model": "claude-opus-4-8",       // claude -p 使用的模型
  "workdir": "/path/to/workdir",    // claude 运行目录(决定会话上下文/CLAUDE.md归属)，默认脚本目录
  "state_dir": "~/.feishu_bridge",  // session 持久化目录
  "timeout": 600,                   // 单条超时秒
  "session_scope": "chat_user",     // 会话隔离: chat_user(群按"群+人") | chat(整群共享) | user(按人)
  "allowed_tools": "Read Write Edit Glob Grep WebSearch WebFetch Skill TodoWrite Task",
  "allowed_chats": ["oc_xxx"]       // 群白名单; 省略=对所有会话响应
}
```

`app_id` / `app_secret` 在飞书开放平台「凭证与基础信息」获取。
应用需开启：机器人能力、接收消息事件（`im.message.receive_v1`）、`im:message` 等读写权限，并加入目标群。
（如需把配置放别处，设环境变量 `BRIDGE_CONFIG=/path/to/your.json`。）

## 运行

```bash
cd feishu-claude-bridge
python3 feishu_claude_bridge.py
```

## 会话命令

群里发 `/new`、`/reset`、`新会话`、`重置会话`，可在当前会话开一个全新 session（清空上下文）。

## 回复格式

- **默认**：Claude 直接输出 Markdown，桥自动包成飞书 schema 2.0 卡片
- **进阶**：Claude 输出以 `<<<CARD>>>` 开头紧跟合法 interactive card JSON，桥原样透传

## 安全说明

- `config.json` 含真实密钥，**已 gitignore，切勿提交**
- 默认工具白名单不含 `Bash`：从能力上禁止 Claude 自行执行命令；发送只由桥经「回复原消息」完成，永远回到来源会话

> 仅供学习与自建使用。
