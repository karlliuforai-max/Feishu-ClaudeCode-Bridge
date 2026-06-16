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
  - 每个会话（私聊=每人 / 群=每群）首条自动新建 session，之后自动续接
  - 持久化 session：chat 映射落盘 + claude 会话 --resume，重启不丢上下文
  - 多 session 并行隔离：不同会话独立进程；同会话加锁串行
  - 命令：发送 /new 或 /reset 在当前会话开启一个全新 session

配置：全部读自脚本同目录的 config.json（不入库），字段见 README。
  必填: app_id, app_secret
  可选: model(默认 claude-opus-4-8) / workdir / state_dir / timeout /
        session_scope(chat_user|chat|user) / allowed_tools / allowed_chats
  可用环境变量 BRIDGE_CONFIG 指定其它配置文件路径。
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
import uuid

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

__version__ = "0.0.1"

# ---------- 配置 ----------
# 所有可变项集中在脚本同目录的 config.json（不入库，见 README「配置」）。
# 只有 app_id / app_secret 必填，其余缺省即可。可用 BRIDGE_CONFIG 指定别的路径。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("BRIDGE_CONFIG", os.path.join(BASE_DIR, "config.json"))
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
WORKDIR = _conf.get("workdir") or BASE_DIR
# session 持久化目录
STATE_DIR = _conf.get("state_dir") or os.path.expanduser("~/.feishu_bridge")
SESSIONS_FILE = os.path.join(STATE_DIR, "sessions.json")
CLAUDE_TIMEOUT = int(_conf.get("timeout", 600))
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

# 群白名单：config.json 的 allowed_chats(数组)；空/缺省=对所有会话响应
_allow = _conf.get("allowed_chats")
ALLOWED_CHATS = set(_allow) if _allow else None

client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
_MENTION_RE = re.compile(r"@_user_\d+")


def _get_bot_open_id() -> str | None:
    """启动时取 bot 自身 open_id，用于精确判断群里是否 @ 了机器人。"""
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
        print(f"[warn] 取 bot open_id 失败，群内将退化为“任意@即响应”：{e}")
        return None


BOT_OPEN_ID = _get_bot_open_id()

# ---------- session 持久化 + 并发隔离 ----------
_sessions_guard = threading.Lock()
_keylocks_guard = threading.Lock()
_keylocks: dict[str, threading.Lock] = {}
# 消息去重：记录已处理的消息 ID + 时间戳，防 WebSocket 重连后重复消费
_DEDUP: dict[str, float] = {}
_DEDUP_LOCK = threading.Lock()
_DEDUP_TTL = 30  # 秒
# 进程启动时刻（毫秒）。飞书 at-least-once 投递：重启后会重投上次未 ack 的旧事件，
# 内存去重表已清空认不出，导致重复执行。用它丢弃“启动之前产生”的消息。
START_TIME_MS = time.time() * 1000


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
            return json.load(f)
    except Exception:  # noqa: BLE001
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


def ask_claude(key: str, text: str, chat_id: str = "", chat_type: str = "") -> str:
    sid, is_new = _get_or_create_sid(key, chat_id, chat_type)
    flag = "--session-id" if is_new else "--resume"
    cmd = ["claude", "-p", text, flag, sid, "--output-format", "text", "--model", CLAUDE_MODEL]
    if ALLOWED_TOOLS:
        cmd += ["--allowedTools", *ALLOWED_TOOLS]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT, cwd=WORKDIR
        )
        out = (r.stdout or "").strip()
        return out or ("(claude 无输出) " + (r.stderr or "")[:300])
    except subprocess.TimeoutExpired:
        return "⌛ 处理超时了，换个更具体的问题再试。"
    except Exception as e:  # noqa: BLE001
        return f"❌ 处理出错：{e}"


# ---------- 飞书收发 ----------
_CARD_MARKER = "<<<CARD>>>"


def _iter_parts(text: str):
    """把 Claude 输出拆成 (msg_type, content_json) 序列。

    两种格式：
    1. <<<CARD>>> 开头 → Claude 直接给出完整卡片 JSON，bridge 透传，不再包装。
       适用于需要按钮、多列、标题栏等 markdown 无法表达的场景。
    2. 普通文本 → 按 20000 字符分块，每块包成 schema 2.0 markdown card。
    """
    if text.startswith(_CARD_MARKER):
        card_json = text[len(_CARD_MARKER):].strip()
        try:
            json.loads(card_json)  # 校验合法性
            yield "interactive", card_json
            return
        except json.JSONDecodeError as e:
            print(f"[warn] <<<CARD>>> 后 JSON 非法，退回 markdown: {e}")
    for i in range(0, max(len(text), 1), 20000):
        part = text[i: i + 20000]
        card = {
            "schema": "2.0",
            "config": {"width_mode": "fill"},
            "body": {"elements": [{"tag": "markdown", "content": part}]},
        }
        yield "interactive", json.dumps(card, ensure_ascii=False)


def send_text(chat_id: str, text: str) -> None:
    """直接发到指定会话（兜底用）。"""
    for msg_type, content in _iter_parts(text):
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


def reply_to(message_id: str, chat_id: str, text: str) -> None:
    """回复原消息：必然落在消息来源的会话，避免群消息回到私聊。失败兜底直发。"""
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
            send_text(chat_id, text)


def _bot_mentioned(msg) -> bool:
    mentions = getattr(msg, "mentions", None) or []
    if not mentions:
        return False
    if BOT_OPEN_ID is None:
        return True  # 退化：群里有任意 @ 就响应
    for m in mentions:
        mid = getattr(m, "id", None)
        if mid and getattr(mid, "open_id", None) == BOT_OPEN_ID:
            return True
    return False


def on_message(data: P2ImMessageReceiveV1) -> None:
    msg = data.event.message
    chat_id = msg.chat_id
    message_id = msg.message_id
    chat_type = getattr(msg, "chat_type", "")  # p2p | group
    if ALLOWED_CHATS is not None and chat_id not in ALLOWED_CHATS:
        return
    if msg.message_type != "text":
        return
    # 群聊：仅 @ 机器人才响应；私聊：始终响应
    if chat_type == "group" and not _bot_mentioned(msg):
        return
    try:
        text = json.loads(msg.content).get("text", "")
    except Exception:  # noqa: BLE001
        return
    text = _MENTION_RE.sub("", text).strip()
    if not text:
        return

    # 启动闸门：丢弃“桥启动之前产生”的消息。
    # 飞书 at-least-once：重启后会重投上次未 ack 的旧事件，否则会被重复执行一次。
    try:
        create_ms = float(getattr(msg, "create_time", 0) or 0)
    except (TypeError, ValueError):
        create_ms = 0.0
    if create_ms and create_ms < START_TIME_MS:
        print(f"[skip stale] chat={chat_id} create={create_ms:.0f} < start={START_TIME_MS:.0f} | {text[:40]}")
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

    # 命令：开启新 session
    if text in RESET_CMDS:
        _reset_session(key)
        reply_to(message_id, chat_id, "🆕 已开启新会话，之前上下文已清空。")
        return

    where = "群聊" if chat_type == "group" else "私聊"
    print(f"\n[{_ts()}] 📩 飞书（{where}）收到: {text}")

    # chat_id / message_id 用默认参数绑定，杜绝闭包串扰
    # 直接喂用户原文：不告诉 Claude 它在飞书、不给 chat_id，它就不会想着自己发消息
    def worker(_key=key, _chat=chat_id, _mid=message_id, _text=text, _type=chat_type):
        print(f"[{_ts()}] 🤔 Claude 处理中……", flush=True)
        t0 = time.monotonic()
        reaction_id = _add_reaction(_mid)  # ⌨️ 正在输入中…
        with _key_lock(_key):  # 同会话串行；不同会话并行隔离
            reply = ask_claude(_key, _text, _chat, _type)
        if reaction_id:
            _del_reaction(_mid, reaction_id)
        reply_to(_mid, _chat, reply)
        print(f"[{_ts()}] 💬 Claude 回复（耗时 {time.monotonic() - t0:.0f}s）:\n{reply}\n")

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
