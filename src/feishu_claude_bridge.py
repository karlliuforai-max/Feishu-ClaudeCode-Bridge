#!/usr/bin/env python3
"""
feishu_claude_bridge.py

飞书消息 <-> Claude Code 无头模式 桥。
大脑 = `claude -p`（复用 Claude Code 已登录授权，无需 Anthropic API key）。
收发 = lark_oapi（飞书消息 encode/decode + WebSocket 长连接，断线自动重连）。

自动行为：
  - 自动连接飞书并自动重连
  - 私聊(P2P)：直接回复
  - 群聊：仅当 @ 机器人时回复（避免刷屏）
  - 收文本/图片/文件/富文本(post)：图片与文件自动下载到本地交给 claude 查看处理
  - 发图片：回复里写 <<<IMG>>>路径 或 ![](本地路径)，自动上传成飞书图片消息
  - 每个会话（私聊=每人 / 群=每群）首条自动新建 session，之后自动续接
  - 持久化 session：chat 映射落盘 + claude 会话 --resume，重启不丢上下文
  - 多 session 并行隔离：不同会话独立进程；同会话加锁串行
  - 命令：发送 /new 或 /reset 在当前会话开启一个全新 session

配置：默认读项目根目录的 config.json（不入库），字段见 README。
  必填: app_id, app_secret
  可选: model(默认 claude-opus-4-8) / workdir / state_dir / timeout /
        max_attachment_bytes / stream_terminal / terminal_stream_format /
        session_scope(chat_user|chat|user) / allowed_tools / allowed_chats
  可用环境变量 BRIDGE_CONFIG 指定其它配置文件路径。
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

__version__ = "0.0.5"
# 进程启动时刻（毫秒）。尽早记录，避免启动期的网络探测把新消息误判成旧事件。
START_TIME_MS = time.time() * 1000


def _expand_path(path: str) -> str:
    """展开配置里的 ~ 和环境变量，并转成绝对路径。"""
    return os.path.abspath(os.path.expandvars(os.path.expanduser(str(path))))


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


# ---------- 配置 ----------
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

# 以下均可选：config.json 没写就用默认值
# claude -p 使用的模型 ID
CLAUDE_MODEL = _conf.get("model", "claude-opus-4-8")
# claude 固定运行目录（决定会话上下文/CLAUDE.md 归属），默认脚本所在目录
WORKDIR = _expand_path(_conf.get("workdir") or BASE_DIR)
# session 持久化目录
STATE_DIR = _expand_path(_conf.get("state_dir") or "~/.feishu_bridge")
SESSIONS_FILE = os.path.join(STATE_DIR, "sessions.json")
# 用户发来的图片/文件下载到这里，处理完即删。
# 放在 WORKDIR 下（而非 STATE_DIR）：claude -p 以 WORKDIR 为工作目录，
# 沙箱通常只允许读工作目录内的文件，放这里才能被 Read 工具读到。
INBOX_DIR = os.path.join(WORKDIR, ".inbox")
CLAUDE_TIMEOUT = int(_conf.get("timeout", 600))
MAX_ATTACHMENT_BYTES = int(_conf.get("max_attachment_bytes", 25 * 1024 * 1024))
if MAX_ATTACHMENT_BYTES <= 0:
    raise SystemExit("❌ 配置错误: max_attachment_bytes 必须大于 0")
STREAM_TERMINAL = _as_bool(_conf.get("stream_terminal"), True)
TERMINAL_STREAM_FORMAT = str(_conf.get("terminal_stream_format", "text")).strip().lower()
if TERMINAL_STREAM_FORMAT not in {"text", "json"}:
    raise SystemExit("❌ 配置错误: terminal_stream_format 只能是 text 或 json")
# 会话作用域：
#   chat_user(默认) 群里按“群+人”分 session：同群不同人各自独立、并行执行
#   chat            整个群共用一个 session（群内串行、共享上下文）
#   user            按人分，不区分群/私聊（同一人的群与私聊会并成一个上下文）
SESSION_SCOPE = _conf.get("session_scope", "chat_user")
# 预授权工具，避免无头模式卡在看不见的授权框。可在 config.json 里写字符串或数组。
# 默认不含 Bash：从能力上断掉 Claude 自己 curl 飞书 API 发消息（这是“发错群”的根因）。
# 发送只由桥经 reply_to(原消息) 完成，永远回到来源会话。
_tools = _conf.get("allowed_tools",
                   "Read Write Edit Glob Grep WebSearch WebFetch Skill TodoWrite Task")
ALLOWED_TOOLS = _tools.split() if isinstance(_tools, str) else list(_tools)
RESET_CMDS = {"/new", "/reset", "/新会话", "新会话", "重置会话"}
os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(INBOX_DIR, exist_ok=True)

# 群白名单：config.json 的 allowed_chats(数组)；空/缺省=对所有会话响应
_allow = _conf.get("allowed_chats")
ALLOWED_CHATS = set(_allow) if _allow else None

client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
_MENTION_RE = re.compile(r"@_user_\d+")
_BOT_OPEN_ID: str | None = None
_BOT_OPEN_ID_RETRY_SECONDS = 60
_BOT_OPEN_ID_LAST_ATTEMPT = -_BOT_OPEN_ID_RETRY_SECONDS
_BOT_OPEN_ID_LOCK = threading.Lock()


def _get_bot_open_id() -> str | None:
    """取 bot 自身 open_id，用于精确判断群里是否 @ 了机器人。"""
    try:
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}).encode(),
            headers={"Content-Type": "application/json"},
        )
        tok = json.loads(urllib.request.urlopen(req, timeout=15).read())["tenant_access_token"]
        req2 = urllib.request.Request(
            "https://open.feishu.cn/open-apis/bot/v3/info",
            headers={"Authorization": "Bearer " + tok},
        )
        info = json.loads(urllib.request.urlopen(req2, timeout=15).read())
        return (info.get("bot") or {}).get("open_id")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 取 bot open_id 失败，群聊将暂不响应 @ 以避免误触发：{e}")
        return None


def _get_bot_open_id_cached() -> str | None:
    """懒加载 bot open_id；失败后限频重试，群聊在拿不到时 fail-closed。"""
    global _BOT_OPEN_ID, _BOT_OPEN_ID_LAST_ATTEMPT
    if _BOT_OPEN_ID:
        return _BOT_OPEN_ID
    now = time.monotonic()
    if now - _BOT_OPEN_ID_LAST_ATTEMPT < _BOT_OPEN_ID_RETRY_SECONDS:
        return None
    with _BOT_OPEN_ID_LOCK:
        now = time.monotonic()
        if _BOT_OPEN_ID or now - _BOT_OPEN_ID_LAST_ATTEMPT < _BOT_OPEN_ID_RETRY_SECONDS:
            return _BOT_OPEN_ID
        _BOT_OPEN_ID_LAST_ATTEMPT = now
        _BOT_OPEN_ID = _get_bot_open_id()
        return _BOT_OPEN_ID

# ---------- session 持久化 + 并发隔离 ----------
_sessions_guard = threading.Lock()
_keylocks_guard = threading.Lock()
_keylocks: dict[str, threading.Lock] = {}
# 消息去重：记录已处理的消息 ID + 时间戳，防 WebSocket 重连后重复消费
_DEDUP: dict[str, float] = {}
_DEDUP_LOCK = threading.Lock()
_DEDUP_TTL = 30  # 秒


def _ts() -> str:
    """本地时间 HH:MM:SS，用于终端对话日志。"""
    return time.strftime("%H:%M:%S")


def _seen_recently(message_id: str) -> bool:
    """同一 message_id 在 TTL 内只处理一次（按时间过期）。"""
    now = time.monotonic()
    with _DEDUP_LOCK:
        for mid in [m for m, ts in _DEDUP.items() if now - ts > _DEDUP_TTL]:
            del _DEDUP[mid]
        if message_id in _DEDUP:
            return True
        _DEDUP[message_id] = now
        return False


def _load_sessions() -> dict:
    try:
        with open(SESSIONS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        print(f"[warn] sessions 文件不是对象，忽略: {SESSIONS_FILE}")
        return {}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        backup = SESSIONS_FILE + ".corrupt-" + time.strftime("%Y%m%d-%H%M%S")
        try:
            os.replace(SESSIONS_FILE, backup)
            print(f"[warn] sessions 文件损坏，已备份到 {backup}: {e}")
        except OSError as oe:
            print(f"[warn] sessions 文件损坏且备份失败: {e}; backup_error={oe}")
        return {}
    except OSError as e:
        print(f"[warn] 读取 sessions 文件失败，临时使用空会话映射: {e}")
        return {}


SESSIONS = _load_sessions()


def _save_sessions() -> None:
    tmp = SESSIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(SESSIONS, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SESSIONS_FILE)


def _key_lock(key: str) -> threading.Lock:
    with _keylocks_guard:
        lk = _keylocks.get(key)
        if lk is None:
            lk = _keylocks[key] = threading.Lock()
        return lk


def _reset_session(key: str) -> None:
    with _sessions_guard:
        if key in SESSIONS:
            del SESSIONS[key]
            _save_sessions()


def _get_or_create_sid(key: str, chat_id: str = "", chat_type: str = "") -> tuple[str, bool]:
    with _sessions_guard:
        rec = SESSIONS.get(key)
        if isinstance(rec, str):  # 旧格式：纯 sid 字符串，原地升级为对象
            rec = SESSIONS[key] = {"sid": rec}
            _save_sessions()
        if rec:
            return rec["sid"], False
    # 新 session：标记来源（可能调飞书 API），放在锁外避免网络阻塞其他会话
    mark = _mark_chat(chat_id, chat_type)
    with _sessions_guard:
        rec = SESSIONS.get(key)
        if isinstance(rec, dict):  # 并发竞争：别的线程已创建
            return rec["sid"], False
        rec = {
            "sid": str(uuid.uuid4()),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            **mark,
        }
        SESSIONS[key] = rec
        _save_sessions()
        print(f"[{_ts()}] 🆕 开启新会话（{mark.get('chat_name') or '私聊'}）")
        return rec["sid"], True


# ---------- tenant token 缓存（供 urllib patch 使用）----------
_TOKEN_CACHE: dict = {"token": "", "exp": 0.0}
_TOKEN_LOCK = threading.Lock()


def _get_tenant_token() -> str:
    with _TOKEN_LOCK:
        now = time.monotonic()
        if float(_TOKEN_CACHE["exp"]) > now + 60:
            return str(_TOKEN_CACHE["token"])
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}).encode(),
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        _TOKEN_CACHE["token"] = data["tenant_access_token"]
        _TOKEN_CACHE["exp"] = now + data.get("expire", 7200)
        return str(_TOKEN_CACHE["token"])


def _get_chat_info(chat_id: str) -> dict:
    """通过飞书 API 获取会话信息（chat_mode: group/p2p、群名等）。"""
    try:
        token = _get_tenant_token()
        req = urllib.request.Request(
            f"https://open.feishu.cn/open-apis/im/v1/chats/{chat_id}",
            headers={"Authorization": "Bearer " + token},
        )
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return data.get("data") or {}
    except Exception as e:  # noqa: BLE001
        print(f"[chat info error] {e}")
        return {}


def _mark_chat(chat_id: str, chat_type: str) -> dict:
    """标记会话来源：优先用事件自带的 chat_type，缺失时调飞书 API 查 chat_mode；
    群聊顺带记录群名，便于在 sessions.json 里直接看出 session 归属。"""
    mark = {"chat_id": chat_id, "chat_type": chat_type or ""}
    if not chat_id:
        return mark
    if not mark["chat_type"] or mark["chat_type"] == "group":
        info = _get_chat_info(chat_id)
        mark["chat_type"] = mark["chat_type"] or info.get("chat_mode") or "unknown"
        if info.get("name"):
            mark["chat_name"] = info["name"]
    return mark


def _add_reaction(message_id: str, emoji_type: str = "Typing") -> str | None:
    """在用户消息上加表情 reaction，返回 reaction_id（用于后续删除）。"""
    try:
        token = _get_tenant_token()
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions"
        payload = json.dumps({"reaction_type": {"emoji_type": emoji_type}}).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return data.get("data", {}).get("reaction_id")
    except Exception as e:  # noqa: BLE001
        print(f"[reaction add error] {e}")
        return None


def _del_reaction(message_id: str, reaction_id: str) -> None:
    """删除之前加的 reaction。"""
    try:
        token = _get_tenant_token()
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}"
        req = urllib.request.Request(
            url, method="DELETE",
            headers={"Authorization": f"Bearer {token}"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"[reaction del error] {e}")


def _build_claude_cmd(text: str, sid: str, is_new: bool, output_format: str = "text") -> list[str]:
    flag = "--session-id" if is_new else "--resume"
    cmd = ["claude", "-p", text, flag, sid, "--output-format", output_format, "--model", CLAUDE_MODEL]
    if output_format == "stream-json":
        cmd += ["--verbose", "--include-partial-messages", "--include-hook-events"]
    if ALLOWED_TOOLS:
        cmd += ["--allowedTools", *ALLOWED_TOOLS]
    return cmd


def _fallback_no_output(stderr: str) -> str:
    return "(claude 无输出) " + (stderr or "")[:300]


def _run_claude_buffered(cmd: list[str]) -> str:
    r = subprocess.run(
        cmd, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT, cwd=WORKDIR
    )
    out = (r.stdout or "").strip()
    return out or _fallback_no_output(r.stderr or "")


def _pipe_to_queue(pipe, stream_name: str, out_q: "queue.Queue[tuple[str, str]]", by_line: bool) -> None:
    try:
        if by_line:
            for chunk in iter(pipe.readline, ""):
                out_q.put((stream_name, chunk))
        else:
            while True:
                chunk = pipe.read(1)
                if not chunk:
                    break
                out_q.put((stream_name, chunk))
    finally:
        try:
            pipe.close()
        except Exception:  # noqa: BLE001
            pass


def _short_json(value, limit: int = 600) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except TypeError:
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _extract_text_from_content(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        delta = item.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            parts.append(delta["text"])
    return "".join(parts)


def _handle_stream_json_line(line: str) -> tuple[str, str, bool]:
    """返回 (终端展示文本, 可作为最终回复候选的文本片段, 是否最终 result)。"""
    raw = line.strip()
    if not raw:
        return "", "", False
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return f"[claude:raw] {raw}\n", raw + "\n", False
    if not isinstance(event, dict):
        return f"[claude:event] {_short_json(event)}\n", "", False

    etype = str(event.get("type") or event.get("event") or "event")
    subtype = event.get("subtype")

    if etype == "system":
        model = event.get("model") or event.get("message", {}).get("model", "")
        sid = event.get("session_id") or event.get("sessionId") or ""
        detail = " ".join(p for p in [f"subtype={subtype}" if subtype else "", f"model={model}" if model else "", f"session={sid}" if sid else ""] if p)
        return f"[claude:system] {detail or _short_json(event)}\n", "", False

    if etype == "assistant":
        message = event.get("message") if isinstance(event.get("message"), dict) else event
        content = message.get("content")
        text = _extract_text_from_content(content)
        tool_lines: list[str] = []
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    name = item.get("name") or item.get("tool_name") or "tool"
                    tool_input = item.get("input", {})
                    tool_lines.append(f"[claude:tool] {name} input={_short_json(tool_input)}")
        terminal = ("\n".join(tool_lines) + ("\n" if tool_lines else ""))
        if text:
            terminal += text
            if not terminal.endswith("\n"):
                terminal += "\n"
        return terminal, text, False

    if etype in {"content_block_delta", "message_delta"}:
        delta = event.get("delta")
        text = delta.get("text") if isinstance(delta, dict) else ""
        return (text or ""), (text or ""), False

    if etype == "user":
        message = event.get("message") if isinstance(event.get("message"), dict) else event
        content = message.get("content")
        terminal = _extract_text_from_content(content)
        return (f"[claude:user] {terminal}\n" if terminal else ""), "", False

    if etype in {"tool_result", "hook", "hook_event"}:
        return f"[claude:{etype}] {_short_json(event)}\n", "", False

    if etype == "result":
        result = event.get("result")
        duration = event.get("duration_ms")
        cost = event.get("total_cost_usd")
        meta = " ".join(
            p for p in [
                f"subtype={subtype}" if subtype else "",
                f"duration_ms={duration}" if duration is not None else "",
                f"cost_usd={cost}" if cost is not None else "",
            ] if p
        )
        terminal = f"[claude:result] {meta or _short_json(event)}\n"
        return terminal, result if isinstance(result, str) else "", True

    return f"[claude:{etype}] {_short_json(event)}\n", "", False


def _run_claude_streaming(cmd: list[str], stream_format: str) -> str:
    print(f"[{_ts()}] ▶ Claude 终端流开始（format={stream_format}，飞书仅发送最终结果）", flush=True)
    by_line = stream_format == "json"
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=WORKDIR,
        bufsize=1,
    )
    out_q: "queue.Queue[tuple[str, str]]" = queue.Queue()
    threads = [
        threading.Thread(target=_pipe_to_queue, args=(proc.stdout, "stdout", out_q, by_line), daemon=True),
        threading.Thread(target=_pipe_to_queue, args=(proc.stderr, "stderr", out_q, by_line), daemon=True),
    ]
    for t in threads:
        t.start()

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    final_candidates: list[str] = []
    result_text = ""
    deadline = time.monotonic() + CLAUDE_TIMEOUT
    timed_out = False

    while True:
        try:
            stream_name, chunk = out_q.get(timeout=0.1)
        except queue.Empty:
            stream_name = chunk = ""

        if chunk:
            if stream_name == "stderr":
                stderr_chunks.append(chunk)
                sys.stderr.write(chunk)
                sys.stderr.flush()
            elif stream_format == "json":
                terminal, final_piece, is_result = _handle_stream_json_line(chunk)
                if terminal:
                    print(terminal, end="", flush=True)
                if final_piece:
                    if is_result:
                        result_text = final_piece
                    else:
                        final_candidates.append(final_piece)
            else:
                stdout_chunks.append(chunk)
                print(chunk, end="", flush=True)

        if proc.poll() is not None and out_q.empty() and all(not t.is_alive() for t in threads):
            break
        if time.monotonic() > deadline:
            timed_out = True
            proc.kill()
            break

    for t in threads:
        t.join(timeout=1)

    if timed_out:
        raise subprocess.TimeoutExpired(cmd, CLAUDE_TIMEOUT)

    print(f"\n[{_ts()}] ■ Claude 终端流结束", flush=True)
    if stream_format == "json":
        out = (result_text or "".join(final_candidates)).strip()
    else:
        out = "".join(stdout_chunks).strip()
    return out or _fallback_no_output("".join(stderr_chunks))


def ask_claude(key: str, text: str, chat_id: str = "", chat_type: str = "") -> str:
    sid, is_new = _get_or_create_sid(key, chat_id, chat_type)
    output_format = "stream-json" if STREAM_TERMINAL and TERMINAL_STREAM_FORMAT == "json" else "text"
    cmd = _build_claude_cmd(text, sid, is_new, output_format)
    try:
        if STREAM_TERMINAL:
            return _run_claude_streaming(cmd, TERMINAL_STREAM_FORMAT)
        return _run_claude_buffered(cmd)
    except subprocess.TimeoutExpired:
        return "⌛ 处理超时了，换个更具体的问题再试。"
    except Exception as e:  # noqa: BLE001
        return f"❌ 处理出错：{e}"


# ---------- 飞书收发 ----------
_CARD_MARKER = "<<<CARD>>>"
_IMG_MARKER = "<<<IMG>>>"
# 整行图片指令：<<<IMG>>>路径
_IMG_LINE_RE = re.compile(r"^\s*<<<IMG>>>(.+?)\s*$", re.MULTILINE)
# markdown 图片：![alt](路径)
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def _resolve_local_image(ref: str) -> str | None:
    """把图片引用解析成存在的本地文件路径；http(s) 链接或不存在则返回 None。"""
    p = ref.strip().strip('"').strip("'")
    if not p or p.startswith(("http://", "https://", "data:")):
        return None
    if not os.path.isabs(p):
        p = os.path.join(WORKDIR, p)
    return p if os.path.isfile(p) else None


def _extract_images(text: str) -> tuple[str, list[str]]:
    """抽出文本里指向本地文件的图片引用（<<<IMG>>>路径 / ![](本地路径)），
    返回 (移除这些引用后的文本, 本地图片路径列表)。
    指向网络 URL 的 markdown 图片原样保留（飞书 markdown 能渲染外链图）。"""
    paths: list[str] = []

    def _sub(m: "re.Match") -> str:
        local = _resolve_local_image(m.group(1))
        if local:
            paths.append(local)
            return ""  # 从文本里摘掉，改为独立 image 消息发送
        return m.group(0)

    text = _IMG_LINE_RE.sub(_sub, text)
    text = _MD_IMG_RE.sub(_sub, text)
    return text.strip(), paths


def _upload_image(path: str) -> str | None:
    """上传本地图片到飞书，返回 image_key；失败返回 None。"""
    try:
        with open(path, "rb") as f:
            body = CreateImageRequestBody.builder().image_type("message").image(f).build()
            req = CreateImageRequest.builder().request_body(body).build()
            resp = client.im.v1.image.create(req)
        if resp.success() and resp.data and resp.data.image_key:
            return resp.data.image_key
        print(f"[image upload fail] code={resp.code} msg={resp.msg} path={path}")
    except Exception as e:  # noqa: BLE001
        print(f"[image upload error] {e} path={path}")
    return None


def _md_card(content: str) -> str:
    """把一段文本包成 schema 2.0 markdown 卡片 JSON。"""
    card = {
        "schema": "2.0",
        "config": {"width_mode": "fill"},
        "body": {"elements": [{"tag": "markdown", "content": content}]},
    }
    return json.dumps(card, ensure_ascii=False)


def _iter_parts(text: str):
    """把 Claude 输出拆成 (msg_type, content_json) 序列。

    三类格式：
    1. <<<CARD>>> 开头 → Claude 直接给出完整卡片 JSON，bridge 透传，不再包装。
       适用于需要按钮、多列、标题栏等 markdown 无法表达的场景。
    2. 文本里夹带本地图片（<<<IMG>>>路径 或 ![](本地路径)）→ 先发文字，再把每张图
       上传换取 image_key，作为独立 image 消息发出。上传失败则降级为一行文字提示。
    3. 其余普通文本 → 按 20000 字符分块，每块包成 schema 2.0 markdown card。
    """
    if text.startswith(_CARD_MARKER):
        card_json = text[len(_CARD_MARKER):].strip()
        try:
            json.loads(card_json)  # 校验合法性
            yield "interactive", card_json
            return
        except json.JSONDecodeError as e:
            print(f"[warn] <<<CARD>>> 后 JSON 非法，退回 markdown: {e}")

    text, img_paths = _extract_images(text)

    # 先发文字（去掉图片引用后若仍有内容）
    if text:
        for i in range(0, len(text), 20000):
            yield "interactive", _md_card(text[i: i + 20000])

    # 再发图片：逐张上传换 key，失败降级为文字提示，绝不让整条消息发不出
    for p in img_paths:
        key = _upload_image(p)
        if key:
            yield "image", json.dumps({"image_key": key})
        else:
            yield "interactive", _md_card(f"(图片发送失败，本地路径：{p})")


def _send_part(chat_id: str, msg_type: str, content: str) -> bool:
    """直发单条消息到指定会话。返回是否成功。"""
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(chat_id)
        .msg_type(msg_type)
        .content(content)
        .build()
    )
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(body)
        .build()
    )
    resp = client.im.v1.message.create(req)
    if not resp.success():
        print(f"[send fail] code={resp.code} msg={resp.msg}")
        return False
    return True


def send_text(chat_id: str, text: str) -> None:
    """直接发到指定会话（兜底用）。"""
    for msg_type, content in _iter_parts(text):
        _send_part(chat_id, msg_type, content)


def reply_to(message_id: str, chat_id: str, text: str) -> None:
    """回复原消息：必然落在消息来源的会话，避免群消息回到私聊。
    单条 part 回复失败时只兜底直发该 part（而非整段重发，避免重复）。"""
    for msg_type, content in _iter_parts(text):
        body = (
            ReplyMessageRequestBody.builder()
            .msg_type(msg_type)
            .content(content)
            .build()
        )
        req = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        resp = client.im.v1.message.reply(req)
        if not resp.success():
            print(f"[reply fail] code={resp.code} msg={resp.msg} -> 兜底直发 {chat_id}")
            _send_part(chat_id, msg_type, content)


def _bot_mentioned(msg) -> bool:
    mentions = getattr(msg, "mentions", None) or []
    if not mentions:
        return False
    bot_open_id = _get_bot_open_id_cached()
    if bot_open_id is None:
        return False
    for m in mentions:
        mid = getattr(m, "id", None)
        if mid and getattr(mid, "open_id", None) == bot_open_id:
            return True
    return False


_IMG_EXT_BY_CTYPE = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/bmp": ".bmp",
}


def _safe_filename(name: str, fallback: str = "file") -> str:
    """把飞书文件名压成安全的单文件名，避免目录穿越和奇怪控制字符。"""
    safe = os.path.basename(name or fallback).strip()
    safe = re.sub(r"[^0-9A-Za-z._ -]+", "_", safe).strip(" .")
    return (safe or fallback)[:120]


def _download_resource(message_id: str, file_key: str, rtype: str, name: str = "") -> str | None:
    """下载消息里的图片/文件资源到 INBOX_DIR，返回本地路径；失败返回 None。
    rtype: 'image'（图片）| 'file'（文件）。"""
    path = ""
    try:
        token = _get_tenant_token()
        url = (f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
               f"/resources/{file_key}?type={rtype}")
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=60) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            clen = resp.headers.get("Content-Length")
            try:
                expected_size = int(clen) if clen else 0
            except ValueError:
                expected_size = 0
            if expected_size > MAX_ATTACHMENT_BYTES:
                raise ValueError(f"附件过大: {expected_size} > {MAX_ATTACHMENT_BYTES} bytes")

            safe = _safe_filename(name, file_key)
            if "." not in safe:  # 没扩展名（多为图片）→ 按 Content-Type 补一个
                safe += _IMG_EXT_BY_CTYPE.get(ctype, ".png" if rtype == "image" else ".bin")
            msg_prefix = _safe_filename(message_id, "message")
            path = os.path.join(INBOX_DIR, f"{msg_prefix}_{uuid.uuid4().hex[:8]}_{safe}")
            total = 0
            with open(path, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ATTACHMENT_BYTES:
                        raise ValueError(f"附件过大: > {MAX_ATTACHMENT_BYTES} bytes")
                    f.write(chunk)
        return path
    except Exception as e:  # noqa: BLE001
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        print(f"[resource download error] {e} key={file_key} type={rtype}")
        return None


def _parse_message(msg) -> tuple[str | None, list[dict]]:
    """把消息解析成 (文字, 附件列表)；附件为 {file_key,type,name}。
    支持 text / image / file / post（富文本）；不支持的类型返回 (None, [])。"""
    try:
        content = json.loads(msg.content)
    except Exception:  # noqa: BLE001
        return None, []
    mtype = msg.message_type
    if mtype == "text":
        return _MENTION_RE.sub("", content.get("text", "")).strip(), []
    if mtype == "image":
        key = content.get("image_key")
        return "", [{"file_key": key, "type": "image", "name": ""}] if key else []
    if mtype == "file":
        key = content.get("file_key")
        return "", [{"file_key": key, "type": "file", "name": content.get("file_name", "")}] if key else []
    if mtype == "post":
        texts: list[str] = []
        atts: list[dict] = []
        if content.get("title"):
            texts.append(content["title"])
        for line in content.get("content", []) or []:
            for seg in line or []:
                tag = seg.get("tag")
                if tag in ("text", "a"):
                    texts.append(seg.get("text", ""))
                elif tag == "img" and seg.get("image_key"):
                    atts.append({"file_key": seg["image_key"], "type": "image", "name": ""})
                elif tag == "media" and seg.get("file_key"):
                    atts.append({"file_key": seg["file_key"], "type": "file", "name": seg.get("file_name", "")})
        caption = _MENTION_RE.sub("", " ".join(t for t in texts if t)).strip()
        return caption, atts
    return None, []  # audio / sticker / 其它：暂不支持


def _build_media_prompt(caption: str, paths: list[str]) -> str:
    """把下载好的本地文件路径拼进给 claude 的提示词。"""
    listing = "\n".join(f"- {p}" for p in paths)
    head = ("用户发来以下本地文件（已下载到本机，可用 Read 等工具查看后处理）：\n"
            f"{listing}")
    if caption:
        return f"{head}\n\n用户附言：{caption}"
    return head + "\n\n用户没有附文字说明，请先解析/描述这些文件的内容，再等待进一步指示。"


def on_message(data: P2ImMessageReceiveV1) -> None:
    msg = data.event.message
    chat_id = msg.chat_id
    message_id = msg.message_id
    chat_type = getattr(msg, "chat_type", "")  # p2p | group
    if ALLOWED_CHATS is not None and chat_id not in ALLOWED_CHATS:
        return
    # 群聊：仅 @ 机器人才响应；私聊：始终响应
    if chat_type == "group" and not _bot_mentioned(msg):
        return
    caption, attachments = _parse_message(msg)
    if caption is None:  # 不支持的消息类型
        return
    text = caption
    if not text and not attachments:  # 空消息
        return

    # 启动闸门：丢弃“桥启动之前产生”的消息。
    # 飞书 at-least-once：重启后会重投上次未 ack 的旧事件，否则会被重复执行一次。
    try:
        create_ms = float(getattr(msg, "create_time", 0) or 0)
    except (TypeError, ValueError):
        create_ms = 0.0
    if create_ms and create_ms < START_TIME_MS:
        print(f"[skip stale] chat={chat_id} create={create_ms:.0f} < start={START_TIME_MS:.0f} | {(text or '[媒体]')[:40]}")
        return

    # 消息去重：同一 message_id 在 TTL 内只处理一次
    if _seen_recently(message_id):
        return

    # session key
    sid_obj = data.event.sender.sender_id
    uid = (getattr(sid_obj, "open_id", None) or getattr(sid_obj, "user_id", None) or "anon")
    if SESSION_SCOPE == "user":
        key = "u:" + uid
    elif SESSION_SCOPE == "chat":
        key = "c:" + chat_id
    else:  # chat_user：群+人；私聊里同一人始终一致
        key = "c:" + chat_id + ":u:" + uid

    # 命令：开启新 session（仅纯文本命令、无附件时）
    if text in RESET_CMDS and not attachments:
        _reset_session(key)
        reply_to(message_id, chat_id, "🆕 已开启新会话，之前上下文已清空。")
        return

    where = "群聊" if chat_type == "group" else "私聊"
    desc = text or f"[{len(attachments)} 个附件]"
    print(f"\n[{_ts()}] 📩 飞书（{where}）收到: {desc}")

    # chat_id / message_id 用默认参数绑定，杜绝闭包串扰
    # 直接喂用户原文：不告诉 Claude 它在飞书、不给 chat_id，它就不会想着自己发消息
    def worker(_key=key, _chat=chat_id, _mid=message_id, _text=text, _type=chat_type,
               _atts=attachments):
        print(f"[{_ts()}] 🤔 Claude 处理中……", flush=True)
        t0 = time.monotonic()
        reaction_id = _add_reaction(_mid)  # ⌨️ 正在输入中…
        # 下载附件（图片/文件）到本地，把路径拼进提示词
        paths: list[str] = []
        for att in _atts:
            p = _download_resource(_mid, att["file_key"], att["type"], att.get("name", ""))
            if p:
                paths.append(p)
        prompt = _build_media_prompt(_text, paths) if paths else _text
        if not prompt:  # 附件全部下载失败、又无文字
            prompt = "用户发来了附件但下载失败，请告知用户重发或换种方式。"
        try:
            with _key_lock(_key):  # 同会话串行；不同会话并行隔离
                reply = ask_claude(_key, prompt, _chat, _type)
            if reaction_id:
                _del_reaction(_mid, reaction_id)
            reply_to(_mid, _chat, reply)
            print(f"[{_ts()}] 💬 Claude 回复（耗时 {time.monotonic() - t0:.0f}s）:\n{reply}\n")
        finally:
            for p in paths:  # 处理完清理下载的临时文件
                try:
                    os.remove(p)
                except OSError:
                    pass

    threading.Thread(target=worker, daemon=True).start()


def _ignore_event(data) -> None:
    """空处理器：吞掉 bot 自己加 Typing reaction 触发的回推事件，
    否则 lark SDK 找不到 processor 会刷 ERROR 日志（无害但噪音）。"""
    return None


_dispatch = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message)
# reaction 事件订阅了却没人处理会报 'processor not found'，注册空处理器消音
if hasattr(_dispatch, "register_p2_im_message_reaction_created_v1"):
    _dispatch = _dispatch.register_p2_im_message_reaction_created_v1(_ignore_event)
if hasattr(_dispatch, "register_p2_im_message_reaction_deleted_v1"):
    _dispatch = _dispatch.register_p2_im_message_reaction_deleted_v1(_ignore_event)
handler = _dispatch.build()

if __name__ == "__main__":
    print(f"✅ 飞书 ↔ Claude 桥 v{__version__} 已就绪，正在连接飞书…… 在飞书里私聊机器人、或群里 @ 它发消息即可。")
    print("   （下面只显示你的消息、Claude 处理与回复；关闭本窗口即停止服务）\n")
    # 只显示 WARNING 及以上的 SDK 日志，过滤掉 connected / 心跳等噪音
    ws = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler, log_level=lark.LogLevel.WARNING)
    ws.start()  # 阻塞、自动重连
