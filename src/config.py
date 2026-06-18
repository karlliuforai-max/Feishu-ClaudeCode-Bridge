#!/usr/bin/env python3
"""config.py — 飞书 Agent Gateway 的配置与路径中心。

职责：
  - 读取 config.json（路径可用 BRIDGE_CONFIG 覆盖），解析所有可变项；
  - 解析并创建运行目录：workspace(=WORKDIR) / inbox / state；
  - 定义每个 Agent 的静态配置（AgentConfig）+ 注册表 AGENT_CONFIGS；
  - 提供与配置强相关的共享小工具（路径展开、CLI 发现、模型别名解析、时间戳）。

本模块不依赖 lark，也不做任何 IM 收发，处于依赖链最底层：
  config  ←  agents  ←  feishu_agent_bridge
"""
import json
import os
import shutil
import time
from dataclasses import dataclass


def _expand_path(path: str) -> str:
    """展开配置里的 ~ 和环境变量，并转成绝对路径。"""
    raw = str(path)
    if raw == "~" or raw.startswith(("~/", "~\\")):
        home = os.environ.get("HOME")
        if home:
            rest = raw[2:] if len(raw) > 1 else ""
            raw = os.path.join(home, rest)
    return os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))


def _common_cli_candidates(command: str) -> list[str]:
    command = command.strip()
    if os.name != "nt" or command not in {"claude", "codex"}:
        return []

    candidates: list[str] = []
    appdata = os.environ.get("APPDATA", "")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    userprofile = os.environ.get("USERPROFILE", "")

    if appdata:
        npm_dir = os.path.join(appdata, "npm")
        candidates += [
            os.path.join(npm_dir, f"{command}.cmd"),
            os.path.join(npm_dir, f"{command}.exe"),
            os.path.join(npm_dir, command),
        ]

    if command == "claude":
        if userprofile:
            candidates += [
                os.path.join(userprofile, ".claude", "local", "claude.cmd"),
                os.path.join(userprofile, ".claude", "local", "claude.exe"),
            ]
        if localappdata:
            candidates += [
                os.path.join(localappdata, "claude-cli-nodejs", "claude.cmd"),
                os.path.join(localappdata, "claude-cli-nodejs", "claude.exe"),
            ]
    elif command == "codex" and userprofile:
        ext_root = os.path.join(userprofile, ".vscode", "extensions")
        if os.path.isdir(ext_root):
            try:
                for name in os.listdir(ext_root):
                    if name.startswith("openai.chatgpt-"):
                        candidates.append(os.path.join(
                            ext_root, name, "bin", "windows-x86_64", "codex.exe"
                        ))
            except OSError:
                pass

    return candidates


def _cli_bin(value, default: str) -> str:
    raw = str(value or default).strip() or default
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if (
        raw == "~"
        or raw.startswith(("~/", "~\\"))
        or os.path.isabs(expanded)
        or "/" in raw
        or "\\" in raw
    ):
        return os.path.abspath(expanded)
    found = shutil.which(expanded)
    if found:
        return found
    for candidate in _common_cli_candidates(expanded):
        if os.path.isfile(candidate):
            return candidate
    return expanded


def _cli_missing_message(agent_name: str, configured_bin: str, config_key: str) -> str:
    return (
        f"❌ 找不到 {agent_name} CLI：{configured_bin}\n"
        f"请先安装并登录 {agent_name} CLI，或在 config.json 中配置 `{config_key}` 为可执行文件绝对路径。"
    )


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _ts() -> str:
    """本地时间 HH:MM:SS，用于终端对话日志。"""
    return time.strftime("%H:%M:%S")


# ---------- 加载 config.json ----------
# 所有可变项集中在项目根目录的 config.json（不入库，见 README「配置」）。
# 只有 app_id / app_secret 必填，其余缺省即可。可用 BRIDGE_CONFIG 指定别的路径。
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SRC_DIR)
CONFIG_FILE = _expand_path(os.environ.get("BRIDGE_CONFIG", os.path.join(BASE_DIR, "config.json")))
try:
    with open(CONFIG_FILE, encoding="utf-8") as _f:
        _conf = json.load(_f)
except FileNotFoundError:
    raise SystemExit(
        f"❌ 缺少配置文件: {CONFIG_FILE}\n"
        f'   请按 README 创建，至少包含飞书应用凭证：\n'
        f'   {{"app_id": "cli_xxx", "app_secret": "xxxxxx"}}'
    )

# 飞书应用凭证（必填）
APP_ID = _conf["app_id"]
APP_SECRET = _conf["app_secret"]

# ---------- Agent 选择与模型 ----------
VALID_AGENTS = {"claude", "codex"}
DEFAULT_AGENT = str(_conf.get("default_agent", "claude")).strip().lower()
if DEFAULT_AGENT not in VALID_AGENTS:
    raise SystemExit("❌ 配置错误: default_agent 只能是 claude 或 codex")

# claude -p 使用的默认模型 ID
CLAUDE_MODEL = _conf.get("model", "claude-sonnet-4-6")
# Codex 固定单模型，避免与 Claude 的 /model 体系混在一起。
CODEX_MODEL = str(_conf.get("codex_model", "gpt-5.5")).strip()
if CODEX_MODEL != "gpt-5.5":
    raise SystemExit("❌ 配置错误: 当前版本的 codex_model 只能是 gpt-5.5")
CODEX_SANDBOX = str(_conf.get("codex_sandbox", "workspace-write")).strip() or "workspace-write"
CODEX_SKIP_GIT_REPO_CHECK = _as_bool(_conf.get("codex_skip_git_repo_check"), True)
CLAUDE_BIN = _cli_bin(_conf.get("claude_bin"), "claude")
CODEX_BIN = _cli_bin(_conf.get("codex_bin"), "codex")

# 模型短名 → 完整 model ID，临时切换时少打字（也可直接传完整 ID）
MODEL_ALIASES = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
    "fable": "claude-fable-5",
}


def _normalize_agent(name: str | None) -> str:
    agent = (name or DEFAULT_AGENT).strip().lower()
    return agent if agent in VALID_AGENTS else DEFAULT_AGENT


def _resolve_model(name: str) -> str | None:
    """把用户输入解析成完整 model ID：支持短名(opus/sonnet/haiku/fable)或直接传完整 ID。
    无法识别返回 None。"""
    n = (name or "").strip()
    if not n:
        return None
    low = n.lower()
    if low in MODEL_ALIASES:
        return MODEL_ALIASES[low]
    if low.startswith("claude-"):  # 直接给完整 model ID
        return n
    return None


# ---------- 运行目录 ----------
# claude/codex 的运行目录（cwd）默认是项目根下的 workspace/：agent 下载与生成的文件都
# 落在这里，仓库根目录保持干净。仍可用 config.json 的 workdir 覆盖。
WORKDIR = _expand_path(_conf.get("workdir") or os.path.join(BASE_DIR, "workspace"))
WORKSPACE_DIR = WORKDIR
# session 持久化目录（内部状态，与用户可见的 workspace 分开）
STATE_DIR = _expand_path(_conf.get("state_dir") or "~/.feishu_bridge")
SESSIONS_FILE = os.path.join(STATE_DIR, "sessions.json")
# 用户发来的图片/文件下载到这里，处理完即删。
# 放在 WORKDIR 下：claude -p 以 WORKDIR 为工作目录，沙箱通常只允许读工作目录内的文件，
# 放这里才能被 Read 工具读到。
INBOX_DIR = os.path.join(WORKDIR, "inbox")

# ---------- 其它运行参数 ----------
CLAUDE_TIMEOUT = int(_conf.get("timeout", 600))
MAX_ATTACHMENT_BYTES = int(_conf.get("max_attachment_bytes", 25 * 1024 * 1024))
if MAX_ATTACHMENT_BYTES <= 0:
    raise SystemExit("❌ 配置错误: max_attachment_bytes 必须大于 0")
STREAM_TERMINAL = _as_bool(_conf.get("stream_terminal"), True)
TERMINAL_STREAM_FORMAT = str(_conf.get("terminal_stream_format", "text")).strip().lower()
if TERMINAL_STREAM_FORMAT not in {"text", "json"}:
    raise SystemExit("❌ 配置错误: terminal_stream_format 只能是 text 或 json")
# 流式回复：开启后用飞书 CardKit 流式卡片「边生成边更新」，让用户实时看到 Claude 打字。
# 默认关闭：保留稳妥的「生成完一次性回复」路径作为兜底，任一环节失败都会自动退回普通回复。
STREAM_REPLY = _as_bool(_conf.get("stream_reply"), False)
# 卡片最小刷新间隔（秒）。飞书 CardKit 文本更新限频 50 次/秒、1000 次/分，默认 0.7s 足够稳。
STREAM_REPLY_INTERVAL = float(_conf.get("stream_reply_interval", 0.7))
# 会话作用域：
#   chat_user(默认) 群里按“群+人”分 session：同群不同人各自独立、并行执行
#   chat            整个群共用一个 session（群内串行、共享上下文）
#   user            按人分，不区分群/私聊（同一人的群与私聊会并成一个上下文）
SESSION_SCOPE = _conf.get("session_scope", "chat_user")
# 预授权工具，避免无头模式卡在看不见的授权框。可在 config.json 里写字符串或数组。
# 默认不含 Bash：从能力上断掉 Claude 自己 curl 飞书 API 发消息（这是“发错群”的根因）。
_tools = _conf.get("allowed_tools",
                   "Read Write Edit Glob Grep WebSearch WebFetch Skill TodoWrite Task")
ALLOWED_TOOLS = _tools.split() if isinstance(_tools, str) else list(_tools)
RESET_CMDS = {"/new", "/reset", "/新会话", "新会话", "重置会话"}

# 群白名单：config.json 的 allowed_chats(数组)；空/缺省=对所有会话响应
_allow = _conf.get("allowed_chats")
ALLOWED_CHATS = set(_allow) if _allow else None

# 启动期创建所有运行目录（含 workspace 与 inbox）
for _d in (STATE_DIR, WORKDIR, INBOX_DIR):
    os.makedirs(_d, exist_ok=True)


# ---------- 每个 Agent 的静态配置 ----------
@dataclass(frozen=True)
class AgentConfig:
    """描述一个 Agent 的静态属性。新增 Agent 只需在 AGENT_CONFIGS 里加一条。"""
    name: str                      # 内部标识：claude / codex
    display_name: str              # 展示名：Claude / Codex
    bin: str                       # 可执行文件
    default_model: str             # 默认模型 ID
    supports_stream_reply: bool    # 是否支持飞书流式卡片回复
    supports_model_switch: bool    # 是否支持 /model 运行时切换
    pregenerate_sid: bool          # True=由 bridge 预生成 uuid 会话(claude)；False=CLI 自己生成(codex)
    # Codex 专属（claude 用默认空值）
    sandbox: str = ""
    skip_git_repo_check: bool = False


AGENT_CONFIGS: dict[str, AgentConfig] = {
    "claude": AgentConfig(
        name="claude",
        display_name="Claude",
        bin=CLAUDE_BIN,
        default_model=CLAUDE_MODEL,
        supports_stream_reply=True,
        supports_model_switch=True,
        pregenerate_sid=True,
    ),
    "codex": AgentConfig(
        name="codex",
        display_name="Codex",
        bin=CODEX_BIN,
        default_model=CODEX_MODEL,
        supports_stream_reply=False,
        supports_model_switch=False,
        pregenerate_sid=False,
        sandbox=CODEX_SANDBOX,
        skip_git_repo_check=CODEX_SKIP_GIT_REPO_CHECK,
    ),
}
