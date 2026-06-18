#!/usr/bin/env python3
"""
feishu_agent_bridge.py

飞书消息 <-> Claude Code / Codex 无头模式 Agent Gateway。
大脑 = `claude -p` 或 `codex exec`（复用本机已登录 CLI，见 agents.py）。
收发 = lark_oapi（飞书消息 encode/decode + WebSocket 长连接，断线自动重连）。

模块分工（依赖链：config ← agents ← feishu_agent_bridge）：
  - config.py   配置加载、运行目录(workspace/inbox/state)、各 Agent 静态配置
  - agents.py   Agent 抽象与底层 CLI 调用（统一 run() 接口）
  - 本文件      飞书 IM 收发、会话持久化与并发隔离、消息派发、进程入口

自动行为：
  - 自动连接飞书并自动重连
  - 私聊(P2P)：直接回复；群聊：仅当 @ 机器人时回复（避免刷屏）
  - /agent claude|codex：同一飞书会话内切换 Agent
  - 收文本/图片/文件/富文本(post)：图片与文件自动下载到 workspace/inbox 交给当前 Agent
  - 发图片：回复里写 <<<IMG>>>路径 或 ![](本地路径)，自动上传成飞书图片消息
  - 每个会话和每个 Agent 独立维护 session，之后自动续接；落盘持久化，重启不丢上下文
  - 多 session 并行隔离：不同会话独立进程；同会话加锁串行
  - 命令：/new 或 /reset 开新 session；/model 临时切换 Claude 模型

配置：默认读项目根目录的 config.json（不入库），字段见 README 与 config.py。
  必填: app_id, app_secret；可用环境变量 BRIDGE_CONFIG 指定其它配置文件路径。
"""
import json
import os
import re
import threading
import time
import urllib.request
import uuid
from types import SimpleNamespace

try:
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
    LARK_AVAILABLE = True
except ModuleNotFoundError:
    LARK_AVAILABLE = False

    class _MissingLarkBuilder:
        def __getattr__(self, _name):
            def _method(*_args, **_kwargs):
                return self
            return _method

        def build(self):
            return SimpleNamespace()

    class _MissingLarkRequest:
        @classmethod
        def builder(cls):
            return _MissingLarkBuilder()

    class _MissingLarkClient:
        @classmethod
        def builder(cls):
            return _MissingLarkBuilder()

    class _MissingLarkDispatcher:
        @classmethod
        def builder(cls, *_args, **_kwargs):
            return _MissingLarkBuilder()

    lark = SimpleNamespace(
        Client=_MissingLarkClient,
        EventDispatcherHandler=_MissingLarkDispatcher,
        ws=SimpleNamespace(Client=_MissingLarkClient),
        LogLevel=SimpleNamespace(WARNING="WARNING"),
    )
    CreateImageRequest = _MissingLarkRequest
    CreateImageRequestBody = _MissingLarkRequest
    CreateMessageRequest = _MissingLarkRequest
    CreateMessageRequestBody = _MissingLarkRequest
    P2ImMessageReceiveV1 = object
    ReplyMessageRequest = _MissingLarkRequest
    ReplyMessageRequestBody = _MissingLarkRequest

from agents import get_agent
from config import (
    AGENT_CONFIGS,
    ALLOWED_CHATS,
    APP_ID,
    APP_SECRET,
    CLAUDE_MODEL,
    CODEX_MODEL,
    DEFAULT_AGENT,
    INBOX_DIR,
    MAX_ATTACHMENT_BYTES,
    RESET_CMDS,
    SESSION_SCOPE,
    SESSIONS_FILE,
    STREAM_REPLY,
    STREAM_REPLY_INTERVAL,
    VALID_AGENTS,
    WORKDIR,
    _normalize_agent,
    _resolve_model,
    _ts,
)

__version__ = "0.4.1"
# 进程启动时刻（毫秒）。尽早记录，避免启动期的网络探测把新消息误判成旧事件。
START_TIME_MS = time.time() * 1000

client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
_MENTION_RE = re.compile(r"@_user_\d+")
_BOT_OPEN_ID: str | None = None
_BOT_OPEN_ID_RETRY_SECONDS = 60
_BOT_OPEN_ID_LAST_ATTEMPT = -_BOT_OPEN_ID_RETRY_SECONDS
_BOT_OPEN_ID_LOCK = threading.Lock()

# 会话级临时模型覆盖：/model <name> 设置、/model reset 清除；仅存内存，重启即失效
_SESSION_MODELS: dict[str, str] = {}


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
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(SESSIONS, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SESSIONS_FILE)


def _key_lock(key: str) -> threading.Lock:
    with _keylocks_guard:
        lk = _keylocks.get(key)
        if lk is None:
            lk = _keylocks[key] = threading.Lock()
        return lk


def _normalize_session_record(rec) -> tuple[dict, bool]:
    """把旧 sessions.json 记录懒迁移成 v0.2.0 多 Agent 结构。"""
    changed = False
    if isinstance(rec, str):
        return {
            "agent": DEFAULT_AGENT,
            "sessions": {"claude": {"sid": rec}},
            "models": {},
        }, True
    if not isinstance(rec, dict):
        return {
            "agent": DEFAULT_AGENT,
            "sessions": {},
            "models": {},
        }, True

    out = dict(rec)
    legacy_sid = out.pop("sid", None)
    sessions = out.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
        changed = True
    if legacy_sid and not sessions.get("claude"):
        sessions["claude"] = {"sid": legacy_sid}
        changed = True
    for agent, value in list(sessions.items()):
        if agent not in VALID_AGENTS:
            del sessions[agent]
            changed = True
            continue
        if isinstance(value, str):
            sessions[agent] = {"sid": value}
            changed = True
        elif not isinstance(value, dict):
            sessions[agent] = {}
            changed = True
    out["sessions"] = sessions

    agent = _normalize_agent(out.get("agent"))
    if out.get("agent") != agent:
        out["agent"] = agent
        changed = True
    if not isinstance(out.get("models"), dict):
        out["models"] = {}
        changed = True
    return out, changed


def _ensure_session_record(key: str, chat_id: str = "", chat_type: str = "") -> dict:
    with _sessions_guard:
        rec = SESSIONS.get(key)
        if rec is not None:
            normalized, changed = _normalize_session_record(rec)
            SESSIONS[key] = normalized
            if changed:
                _save_sessions()
            return normalized

    mark = _mark_chat(chat_id, chat_type)
    with _sessions_guard:
        rec = SESSIONS.get(key)
        if rec is not None:
            normalized, changed = _normalize_session_record(rec)
            SESSIONS[key] = normalized
            if changed:
                _save_sessions()
            return normalized
        rec = {
            "agent": DEFAULT_AGENT,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            **mark,
            "sessions": {},
            "models": {},
        }
        SESSIONS[key] = rec
        _save_sessions()
        return rec


def _get_active_agent(key: str, chat_id: str = "", chat_type: str = "") -> str:
    rec = _ensure_session_record(key, chat_id, chat_type)
    return _normalize_agent(rec.get("agent"))


def _set_active_agent(key: str, agent: str, chat_id: str = "", chat_type: str = "") -> str:
    agent = _normalize_agent(agent)
    _ensure_session_record(key, chat_id, chat_type)
    with _sessions_guard:
        rec, _changed = _normalize_session_record(SESSIONS.get(key))
        rec["agent"] = agent
        SESSIONS[key] = rec
        _save_sessions()
    return agent


def _get_agent_sid(key: str, agent: str, chat_id: str = "", chat_type: str = "") -> str | None:
    agent = _normalize_agent(agent)
    rec = _ensure_session_record(key, chat_id, chat_type)
    session = rec.get("sessions", {}).get(agent)
    return session.get("sid") if isinstance(session, dict) else None


def _save_agent_sid(key: str, agent: str, sid: str, chat_id: str = "", chat_type: str = "") -> None:
    if not sid:
        return
    agent = _normalize_agent(agent)
    _ensure_session_record(key, chat_id, chat_type)
    with _sessions_guard:
        rec, _changed = _normalize_session_record(SESSIONS.get(key))
        sessions = rec.setdefault("sessions", {})
        sessions[agent] = {"sid": sid}
        SESSIONS[key] = rec
        _save_sessions()


def _reset_agent_session(key: str, agent: str | None = None,
                         chat_id: str = "", chat_type: str = "") -> str:
    active = _normalize_agent(agent) if agent else _get_active_agent(key, chat_id, chat_type)
    _ensure_session_record(key, chat_id, chat_type)
    with _sessions_guard:
        rec, _changed = _normalize_session_record(SESSIONS.get(key))
        sessions = rec.setdefault("sessions", {})
        sessions.pop(active, None)
        SESSIONS[key] = rec
        _save_sessions()
    if active == "claude":
        _SESSION_MODELS.pop(key, None)
    return active


def _get_or_create_agent_sid(key: str, agent: str, chat_id: str = "",
                             chat_type: str = "") -> tuple[str, bool]:
    agent = _normalize_agent(agent)
    sid = _get_agent_sid(key, agent, chat_id, chat_type)
    if sid:
        return sid, False
    sid = str(uuid.uuid4())
    _save_agent_sid(key, agent, sid, chat_id, chat_type)
    rec = _ensure_session_record(key, chat_id, chat_type)
    print(f"[{_ts()}] 🆕 开启新 {agent} 会话（{rec.get('chat_name') or '私聊'}）")
    return sid, True


# ---------- Agent 状态/模型展示 ----------
def _agent_model(agent: str, key: str = "", override: str = "") -> str:
    cfg = AGENT_CONFIGS[_normalize_agent(agent)]
    if not cfg.supports_model_switch:
        return cfg.default_model
    return override or _SESSION_MODELS.get(key, "") or cfg.default_model


def _agent_display_name(agent: str) -> str:
    return AGENT_CONFIGS[_normalize_agent(agent)].display_name


def _agent_supports_stream_reply(agent: str) -> bool:
    return AGENT_CONFIGS[_normalize_agent(agent)].supports_stream_reply


def _format_agent_status(key: str, chat_id: str = "", chat_type: str = "") -> str:
    agent = _get_active_agent(key, chat_id, chat_type)
    model = _agent_model(agent, key)
    lines = [
        f"当前 Agent：{_agent_display_name(agent)}",
        f"当前模型：{model}",
        "可用命令：/agent claude、/agent codex、/agent reset",
    ]
    if not AGENT_CONFIGS[_normalize_agent(agent)].supports_model_switch:
        lines.append(f"{_agent_display_name(agent)} 固定使用 {model}，不支持 /model 切换。")
    return "\n".join(lines)


def _handle_agent_command(key: str, text: str, chat_id: str = "", chat_type: str = "") -> str:
    arg = text[len("/agent"):].strip().lower()
    if not arg:
        return _format_agent_status(key, chat_id, chat_type)
    if arg in {"reset", "default", "默认"}:
        agent = _set_active_agent(key, DEFAULT_AGENT, chat_id, chat_type)
        return f"已恢复默认 Agent：{_agent_display_name(agent)}（模型：{_agent_model(agent, key)}）"
    if arg not in VALID_AGENTS:
        return "无法识别的 Agent。可用：/agent claude、/agent codex、/agent reset"
    agent = _set_active_agent(key, arg, chat_id, chat_type)
    return f"已切换到 {_agent_display_name(agent)}（模型：{_agent_model(agent, key)}）"


def _is_slash_command(text: str, command: str) -> bool:
    return text == command or text.startswith(command + " ")


# ---------- tenant token 缓存 ----------
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


# ---------- 飞书收发 ----------
_CARD_MARKER = "<<<CARD>>>"
_IMG_MARKER = "<<<IMG>>>"
# 整行图片指令：<<<IMG>>>路径
_IMG_LINE_RE = re.compile(r"^\s*<<<IMG>>>(.+?)\s*$", re.MULTILINE)
# markdown 图片：![alt](路径)
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
# 单条临时模型前缀：以 [m:模型名] 开头，仅本条生效
_MODEL_PREFIX_RE = re.compile(r"^\s*\[m:([^\]]+)\]\s*", re.IGNORECASE)


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
    """把 Agent 输出拆成 (msg_type, content_json) 序列。

    三类格式：
    1. <<<CARD>>> 开头 → Agent 直接给出完整卡片 JSON，bridge 透传，不再包装。
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


# ---------- 流式回复：飞书 CardKit 流式卡片 ----------
# 思路：先创建一张「streaming_mode」卡片实体，回复用户消息把它发出去；随后随着 Agent
# 产出增量文本，按 PUT 全量文本 + 递增 sequence 更新卡片，飞书端做打字机渲染；结束时
# 写入最终（去掉本地图片引用的）文本并关闭流式，再把本地图片作为独立消息补发。
# 任何一步失败都 fail-soft：CardStreamer.finish 返回 False，调用方退回普通 reply_to。
_CARDKIT_BASE = "https://open.feishu.cn/open-apis/cardkit/v1/cards"


def _cardkit_request(url: str, payload: dict, method: str = "POST") -> dict | None:
    try:
        token = _get_tenant_token()
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode(), method=method,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        if data.get("code") == 0:
            return data
        print(f"[cardkit {method} fail] code={data.get('code')} msg={data.get('msg')} url={url}")
    except Exception as e:  # noqa: BLE001
        print(f"[cardkit {method} error] {e} url={url}")
    return None


def _streaming_card_json(initial: str = "⌛ 正在生成…") -> str:
    card = {
        "schema": "2.0",
        "config": {
            "width_mode": "fill",
            "streaming_mode": True,
            "streaming_config": {
                "print_frequency_ms": {"default": 30},
                "print_step": {"default": 2},
                "print_strategy": "fast",
            },
        },
        "body": {"elements": [
            {"tag": "markdown", "content": initial, "element_id": CardStreamer.ELEMENT_ID},
        ]},
    }
    return json.dumps(card, ensure_ascii=False)


def _reply_card(message_id: str, card_id: str) -> bool:
    """以「回复原消息」的方式把卡片实体发出去，确保落在来源会话。"""
    content = json.dumps({"type": "card", "data": {"card_id": card_id}})
    try:
        body = ReplyMessageRequestBody.builder().msg_type("interactive").content(content).build()
        req = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        resp = client.im.v1.message.reply(req)
        if resp.success():
            return True
        print(f"[reply card fail] code={resp.code} msg={resp.msg}")
    except Exception as e:  # noqa: BLE001
        print(f"[reply card error] {e}")
    return False


class CardStreamer:
    """把 Agent 的增量文本流式更新到一张飞书卡片上。线程内串行使用（同会话 worker）。"""

    ELEMENT_ID = "bridge_md"

    def __init__(self, message_id: str, chat_id: str, interval: float = 0.7):
        self.message_id = message_id
        self.chat_id = chat_id
        self.interval = max(0.2, interval)
        self.card_id: str | None = None
        self.failed = False
        self.buf = ""
        self.seq = 0
        self.last_push = 0.0
        self.lock = threading.Lock()

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def _ensure_card(self) -> bool:
        """懒创建卡片并发出。任一步失败标记 failed，后续直接放弃流式。"""
        if self.card_id:
            return True
        if self.failed:
            return False
        data = _cardkit_request(_CARDKIT_BASE, {"type": "card_json", "data": _streaming_card_json()})
        card_id = (data or {}).get("data", {}).get("card_id")
        if not card_id or not _reply_card(self.message_id, card_id):
            self.failed = True
            return False
        self.card_id = card_id
        return True

    def _update(self, content: str) -> bool:
        url = f"{_CARDKIT_BASE}/{self.card_id}/elements/{self.ELEMENT_ID}/content"
        ok = _cardkit_request(
            url, {"content": content, "sequence": self._next_seq(), "uuid": uuid.uuid4().hex},
            method="PUT",
        )
        return ok is not None

    def push(self, delta: str) -> None:
        """收到增量文本：累积，按最小间隔限频地把全量文本刷到卡片。"""
        with self.lock:
            if self.failed:
                return
            self.buf += delta
            now = time.monotonic()
            if now - self.last_push < self.interval:
                return
            if not self._ensure_card():
                return
            if self._update(self.buf):
                self.last_push = now

    def finish(self, final_text: str) -> bool:
        """收尾。返回 True=已通过卡片完成发送；False=请调用方退回普通 reply_to。"""
        with self.lock:
            text = (final_text or "").strip()
            # 自定义卡片（<<<CARD>>>）无法塞进流式 markdown：收掉占位卡片，交还普通通道透传
            if text.startswith(_CARD_MARKER):
                if self.card_id:
                    self._update("（见下方卡片）")
                    _cardkit_request(f"{_CARDKIT_BASE}/{self.card_id}/settings",
                                     {"settings": json.dumps({"config": {"streaming_mode": False}}),
                                      "sequence": self._next_seq(), "uuid": uuid.uuid4().hex},
                                     method="PATCH")
                return False
            cleaned, img_paths = _extract_images(text)
            if not self._ensure_card():  # 整段未能建卡 → 退回普通回复
                return False
            self._update(cleaned or "(无文本输出)")
            _cardkit_request(f"{_CARDKIT_BASE}/{self.card_id}/settings",
                             {"settings": json.dumps({"config": {"streaming_mode": False}}),
                              "sequence": self._next_seq(), "uuid": uuid.uuid4().hex},
                             method="PATCH")
            # 本地图片仍走独立 image 消息（卡片里渲染不了本地路径）
            for p in img_paths:
                key = _upload_image(p)
                if key:
                    _send_part(self.chat_id, "image", json.dumps({"image_key": key}))
                else:
                    _send_part(self.chat_id, "interactive", _md_card(f"(图片发送失败，本地路径：{p})"))
            return True


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
    """把下载好的本地文件路径拼进给当前 Agent 的提示词。"""
    listing = "\n".join(f"- {p}" for p in paths)
    head = ("用户发来以下本地文件（已下载到本机，可用 Read 等工具查看后处理）：\n"
            f"{listing}")
    if caption:
        return f"{head}\n\n用户附言：{caption}"
    return head + "\n\n用户没有附文字说明，请先解析/描述这些文件的内容，再等待进一步指示。"


def ask_agent(key: str, text: str, chat_id: str = "", chat_type: str = "",
              text_delta_cb=None, model: str = "", agent: str = "") -> str:
    """统一派发：选 Agent → 取/建会话 sid → 解析模型 → 调 agent.run → 持久化会话。"""
    agent_name = _normalize_agent(agent) if agent else _get_active_agent(key, chat_id, chat_type)
    impl = get_agent(agent_name)
    if impl.pregenerate_sid:
        sid, is_new = _get_or_create_agent_sid(key, agent_name, chat_id, chat_type)
    else:  # CLI 自管会话 id（codex）：传入已有 sid 或 None
        sid = _get_agent_sid(key, agent_name, chat_id, chat_type)
        is_new = sid is None
    # 模型优先级：单条覆盖(model) > 会话级覆盖 > Agent 默认（仅支持切换的 Agent 生效）
    eff_model = (model or _SESSION_MODELS.get(key, "")) if impl.supports_model_switch else ""
    cb = text_delta_cb if impl.supports_stream_reply else None
    result = impl.run(prompt=text, sid=sid, is_new=is_new, model=eff_model, text_delta_cb=cb)
    if result.new_sid and result.new_sid != sid:
        _save_agent_sid(key, agent_name, result.new_sid, chat_id, chat_type)
    elif not impl.pregenerate_sid and sid is None and not result.new_sid:
        print(f"[warn] {impl.display_name} 未返回 session_id，本轮回复可用但无法持久续接")
    return result.reply


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

    active_agent = _get_active_agent(key, chat_id, chat_type)

    # 命令：切换/查询当前会话使用的 Agent（仅纯文本、无附件时）
    if _is_slash_command(text, "/agent") and not attachments:
        reply_to(message_id, chat_id, _handle_agent_command(key, text, chat_id, chat_type))
        return

    # 命令：开启新 session（仅纯文本命令、无附件时）
    if text in RESET_CMDS and not attachments:
        reset_agent = _reset_agent_session(key, active_agent, chat_id, chat_type)
        reply_to(message_id, chat_id, f"🆕 已开启新的 {_agent_display_name(reset_agent)} 会话，之前该 Agent 的上下文已清空。")
        return

    # 命令：切换/查询本会话的临时模型（仅纯文本、无附件时）
    if _is_slash_command(text, "/model") and not attachments:
        if not AGENT_CONFIGS[active_agent].supports_model_switch:
            reply_to(
                message_id,
                chat_id,
                f"当前 Agent：{_agent_display_name(active_agent)}\n"
                f"当前模型：{_agent_model(active_agent, key)}\n"
                f"{_agent_display_name(active_agent)} 固定模型，不支持 /model 切换。"
                "如需切换 Claude 模型，请先发送 /agent claude。",
            )
            return
        arg = text[len("/model"):].strip()
        if not arg:  # 查询当前
            cur = _SESSION_MODELS.get(key)
            line = (f"当前会话模型：{cur}（临时覆盖）" if cur
                    else f"当前会话模型：{CLAUDE_MODEL}（全局默认）")
            reply_to(message_id, chat_id,
                     line + "\n用法：/model opus|sonnet|haiku|fable 或完整 ID（如 claude-sonnet-4-6）；"
                            "/model reset 恢复默认。单条临时用前缀 [m:opus] 你的问题")
            return
        if arg.lower() in ("reset", "default", "默认"):
            _SESSION_MODELS.pop(key, None)
            reply_to(message_id, chat_id, f"↩️ 已恢复全局默认模型：{CLAUDE_MODEL}")
            return
        resolved = _resolve_model(arg)
        if not resolved:
            reply_to(message_id, chat_id,
                     f"❓ 无法识别的模型：{arg}\n可用短名：opus / sonnet / haiku / fable，或传完整 model ID")
            return
        _SESSION_MODELS[key] = resolved
        reply_to(message_id, chat_id, f"✅ 本会话模型已切换为：{resolved}（/model reset 或重启后失效）")
        return

    # 单条临时模型：消息以 [m:xxx] 开头，仅本条生效，识别后剥掉前缀
    per_msg_model = ""
    pm = _MODEL_PREFIX_RE.match(text)
    if pm:
        if not AGENT_CONFIGS[active_agent].supports_model_switch:
            reply_to(
                message_id,
                chat_id,
                f"{_agent_display_name(active_agent)} 固定模型，不支持 [m:...] 单条模型前缀。"
                "请去掉前缀，或先发送 /agent claude。",
            )
            return
        resolved = _resolve_model(pm.group(1))
        if resolved:
            per_msg_model = resolved
            text = text[pm.end():]
            if not text and not attachments:  # 只发了前缀、没正文
                reply_to(message_id, chat_id, "ℹ️ 检测到模型前缀但没有内容，请在 [m:xxx] 后面带上你的问题。")
                return

    where = "群聊" if chat_type == "group" else "私聊"
    desc = text or f"[{len(attachments)} 个附件]"
    print(f"\n[{_ts()}] 📩 飞书（{where}）收到: {desc}")

    # chat_id / message_id 用默认参数绑定，杜绝闭包串扰
    # 直接喂用户原文：不告诉 Agent 它在飞书、不给 chat_id，它就不会想着自己发消息
    def worker(_key=key, _chat=chat_id, _mid=message_id, _text=text, _type=chat_type,
               _atts=attachments, _model=per_msg_model, _agent=active_agent):
        agent_name = _agent_display_name(_agent)
        print(f"[{_ts()}] 🤔 {agent_name} 处理中……", flush=True)
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
        # 流式回复：Claude 保持边生成边更新卡片；Codex 暂以最终回复兜底。
        streamer = (CardStreamer(_mid, _chat, STREAM_REPLY_INTERVAL)
                    if STREAM_REPLY and _agent_supports_stream_reply(_agent) else None)
        delta_cb = streamer.push if streamer else None
        try:
            with _key_lock(_key):  # 同会话串行；不同会话并行隔离
                reply = ask_agent(
                    _key, prompt, _chat, _type, text_delta_cb=delta_cb, model=_model, agent=_agent
                )
            if reaction_id:
                _del_reaction(_mid, reaction_id)
            if not (streamer and streamer.finish(reply)):
                reply_to(_mid, _chat, reply)
            print(f"[{_ts()}] 💬 {agent_name} 回复（耗时 {time.monotonic() - t0:.0f}s）:\n{reply}\n")
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


def _clean_inbox() -> None:
    """启动时清空 inbox 里的残留下载（崩溃遗留的孤儿；启动时无在途任务，安全）。"""
    try:
        for name in os.listdir(INBOX_DIR):
            p = os.path.join(INBOX_DIR, name)
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


_dispatch = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message)
# 这些事件一旦在飞书后台被订阅，没人处理就会刷 'processor not found' ERROR（无害但吵）。
# 注册空处理器消音：表情回复、机器人/成员进出群等都不是本桥要处理的业务。
for _ignored_event in (
    "register_p2_im_message_reaction_created_v1",
    "register_p2_im_message_reaction_deleted_v1",
    "register_p2_im_chat_member_bot_added_v1",
    "register_p2_im_chat_member_bot_deleted_v1",
    "register_p2_im_chat_member_user_added_v1",
    "register_p2_im_chat_member_user_deleted_v1",
):
    _register = getattr(_dispatch, _ignored_event, None)
    if _register:
        _dispatch = _register(_ignore_event)
handler = _dispatch.build()

if __name__ == "__main__":
    if not LARK_AVAILABLE:
        raise SystemExit("❌ 缺少依赖 lark-oapi，请先运行: pip install -r requirements.txt")
    _clean_inbox()
    print(f"✅ 飞书 Agent Gateway v{__version__} 已就绪，正在连接飞书……")
    print(f"   默认 Agent：{_agent_display_name(DEFAULT_AGENT)}；Claude={CLAUDE_MODEL}；Codex={CODEX_MODEL}")
    print(f"   工作目录(workspace)：{WORKDIR}")
    print("   在飞书里私聊机器人、或群里 @ 它发消息即可；用 /agent claude|codex 切换。\n")
    # 只显示 WARNING 及以上的 SDK 日志，过滤掉 connected / 心跳等噪音
    ws = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler, log_level=lark.LogLevel.WARNING)
    ws.start()  # 阻塞、自动重连
