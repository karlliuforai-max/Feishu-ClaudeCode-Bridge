#!/usr/bin/env python3
"""agents.py — Agent 抽象层。

把「大脑」统一成一个接口：给定 prompt + 会话 sid + 模型，跑底层 CLI（claude -p /
codex exec），把过程渲染到终端、可选地把增量文本推给回调（流式卡片），返回最终回复
和需要持久化的会话 id。

  - Agent          统一接口：run(prompt, sid, is_new, model, text_delta_cb) -> AgentResult
  - ClaudeAgent    包装 claude -p（支持 stream-json 事件流 + 流式回复 + /model 切换）
  - CodexAgent     包装 codex exec（CLI 自管会话 id；固定 gpt-5.5）
  - AGENTS         name -> Agent 实例注册表；get_agent(name) 取实例

本模块只用 subprocess，不依赖 lark、不碰会话持久化：sid / model 由 bridge 解析后传入，
agent 保持无状态。依赖链：config ← agents ← feishu_agent_bridge。
"""
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

from config import (
    AGENT_CONFIGS,
    AgentConfig,
    ALLOWED_TOOLS,
    AGENT_TIMEOUT,
    CLAUDE_BIN,
    CLAUDE_MODEL,
    CODEX_BIN,
    CODEX_MODEL,
    CODEX_SANDBOX,
    CODEX_SKIP_GIT_REPO_CHECK,
    DEFAULT_AGENT,
    STATE_DIR,
    STREAM_TERMINAL,
    TERMINAL_STREAM_FORMAT,
    WORKDIR,
    _cli_missing_message,
    _normalize_agent,
    _ts,
)


# ---------- 通用输出工具 ----------
def _fallback_no_output(stderr: str) -> str:
    return "(claude 无输出) " + (stderr or "")[:300]


def _pipe_to_queue(pipe, stream_name: str, out_q: "queue.Queue[tuple[str, str]]", by_line: bool) -> None:
    try:
        try:
            if by_line:
                for chunk in iter(pipe.readline, ""):
                    out_q.put((stream_name, chunk))
            else:
                # 非逐行档（claude text 流）：按块读，下游只是拼接/回显，逐字符 read(1)
                # 会对一段长回复做上万次 queue.put，纯属浪费。
                while True:
                    chunk = pipe.read(4096)
                    if not chunk:
                        break
                    out_q.put((stream_name, chunk))
        except UnicodeDecodeError as e:
            out_q.put((stream_name, f"[decode error] {e}\n"))
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


def _truncate(value, limit: int = 200) -> str:
    s = value if isinstance(value, str) else str(value)
    s = s.replace("\n", " ").strip()
    return s if len(s) <= limit else s[:limit] + "…"


# 常见工具 → 最能说明「这步在干嘛」的输入字段。其余工具回退到压缩后的 JSON。
_TOOL_SUMMARY_KEY = {
    "Read": "file_path", "Write": "file_path", "Edit": "file_path",
    "MultiEdit": "file_path", "NotebookEdit": "notebook_path",
    "Bash": "command", "Grep": "pattern", "Glob": "pattern",
    "Task": "description",
}


def _summarize_tool(name: str, tool_input) -> str:
    """把一次工具调用压成一行人能看懂的摘要：Read 显示文件、Bash 显示命令等。"""
    i = tool_input if isinstance(tool_input, dict) else {}
    key = _TOOL_SUMMARY_KEY.get(name)
    if key and i.get(key):
        return _truncate(i[key])
    if name in {"WebFetch", "WebSearch"}:
        return _truncate(i.get("url") or i.get("query") or "")
    if name == "Skill":
        return _truncate(i.get("command") or i.get("skill") or "")
    if name == "TodoWrite":
        todos = i.get("todos")
        return f"{len(todos)} 项待办" if isinstance(todos, list) else ""
    return _truncate(_short_json(i, 200))


def _collapse(text: str, max_lines: int = 4, max_chars: int = 500) -> str:
    """长工具结果折叠：只留前几行/若干字符，带「│」缩进前缀，超出给出统计提示。"""
    text = (text or "").strip()
    if not text:
        return ""
    full_len = len(text)
    lines = text.splitlines()
    clipped = len(lines) > max_lines
    lines = lines[:max_lines]
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[:max_chars]
        clipped = True
    out = "\n".join("    │ " + ln for ln in body.splitlines())
    if clipped:
        out += f"\n    │ …（共 {full_len} 字，已折叠）"
    return out


class _StreamJsonRenderer:
    """消费 claude `--output-format stream-json` 的事件流。

    职责：
      1) 把事件渲染成统一时间线打到终端（system / 工具调用 / 工具结果 / 文本 / result）；
      2) 实时把助手文本增量通过 on_text_delta 回调出去（供流式卡片）；
      3) 汇总最终回复文本（final_text，以 result 事件为准，缺失时回退增量/整段）。
    """

    def __init__(self, tag: str = "", echo: bool = True, on_text_delta=None):
        self.tag = tag
        self.echo = echo
        self.on_text_delta = on_text_delta
        self.tools: dict[str, dict] = {}     # tool_use_id -> {name, summary, t0}
        self.final_result = ""
        self.delta_parts: list[str] = []
        self.assistant_parts: list[str] = []
        self.had_delta = False               # 当前助手轮是否已经流过增量，避免整段重复
        self.error = False

    # ---- 终端输出小工具 ----
    def _prefix(self) -> str:
        return f"[{self.tag}] " if self.tag else ""

    def _line(self, text: str) -> None:
        if self.echo:
            print(self._prefix() + text, flush=True)

    def _raw(self, text: str) -> None:
        if self.echo:
            sys.stdout.write(text)
            sys.stdout.flush()

    def _push_text(self, text: str) -> None:
        if not text:
            return
        if self.on_text_delta:
            try:
                self.on_text_delta(text)
            except Exception as e:  # noqa: BLE001
                print(f"[stream-reply error] {e}")

    # ---- 事件分发 ----
    def feed(self, line: str) -> None:
        raw = line.strip()
        if not raw:
            return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            self._line(f"[raw] {_truncate(raw, 300)}")
            return
        if not isinstance(event, dict):
            return
        etype = str(event.get("type") or event.get("event") or "event")
        if etype == "stream_event":  # --include-partial-messages 包了一层
            self._on_partial(event.get("event"))
            return
        handler = getattr(self, f"_on_{etype}", None)
        if handler:
            handler(event)
        elif etype in {"content_block_delta", "message_delta"}:
            self._on_partial(event)
        else:
            self._line(f"[{etype}] {_short_json(event, 200)}")

    def _on_partial(self, ev) -> None:
        if not isinstance(ev, dict):
            return
        if ev.get("type") == "content_block_delta":
            delta = ev.get("delta") or {}
            text = delta.get("text") or ""   # thinking_delta 没有 text 字段，天然跳过
            if text:
                self.had_delta = True
                self.delta_parts.append(text)
                self._raw(text)
                self._push_text(text)

    def _on_system(self, event) -> None:
        sub = event.get("subtype")
        # thinking_tokens 是模型内部思考过程的增量事件，对终端阅读零价值，
        # 只会刷出几十上百行噪音。直接跳过，仅保留有意义的系统事件。
        if sub == "thinking_tokens":
            return
        model = event.get("model") or ""
        bits = [b for b in [f"subtype={sub}" if sub else "", f"model={model}" if model else ""] if b]
        self._line("[system] " + (" ".join(bits) or _short_json(event, 200)))

    def _on_assistant(self, event) -> None:
        message = event.get("message") if isinstance(event.get("message"), dict) else event
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    tid = item.get("id") or ""
                    name = item.get("name") or item.get("tool_name") or "tool"
                    summary = _summarize_tool(name, item.get("input"))
                    self.tools[tid] = {"name": name, "summary": summary, "t0": time.monotonic()}
                    self._line(f"[tool ▶] {name}  {summary}".rstrip())
        text = _extract_text_from_content(content)
        if text:
            self.assistant_parts.append(text)
            if not self.had_delta:  # 没有增量流（理论上不会，但兜底）→ 整段打印+推送一次
                self._raw(text + ("" if text.endswith("\n") else "\n"))
                self._push_text(text)
        self.had_delta = False  # 该助手块结束，下一块重新计

    def _on_user(self, event) -> None:
        message = event.get("message") if isinstance(event.get("message"), dict) else event
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    self._render_tool_result(item)

    def _render_tool_result(self, item: dict) -> None:
        tid = item.get("tool_use_id") or ""
        info = self.tools.pop(tid, None)
        name = info["name"] if info else "tool"
        dur = f" {time.monotonic() - info['t0']:.1f}s" if info and info.get("t0") else ""
        is_err = bool(item.get("is_error"))
        head = f"[tool {'✗' if is_err else '✓'}{dur}] {name}"
        self._line(("⚠️ " + head) if is_err else head)
        body = _collapse(_extract_text_from_content(item.get("content")))
        if body and self.echo:
            print(body, flush=True)
        if is_err:
            self.error = True

    def _on_result(self, event) -> None:
        sub = event.get("subtype")
        dur = event.get("duration_ms")
        cost = event.get("total_cost_usd")
        res = event.get("result")
        if isinstance(res, str):
            self.final_result = res
        if sub and sub != "success":
            self.error = True
        bits = [b for b in [
            f"subtype={sub}" if sub else "",
            f"{dur}ms" if dur is not None else "",
            f"${cost}" if cost is not None else "",
        ] if b]
        mark = "⚠️ " if self.error else ""
        self._line(f"\n{mark}[result] " + " ".join(bits))

    @property
    def final_text(self) -> str:
        return (self.final_result or "".join(self.delta_parts)
                or "".join(self.assistant_parts)).strip()


# ---------- Claude 底层 ----------
def _build_claude_cmd(text: str, sid: str, is_new: bool, output_format: str = "text",
                      model: str = "") -> list[str]:
    flag = "--session-id" if is_new else "--resume"
    cmd = [CLAUDE_BIN, "-p", text, flag, sid, "--output-format", output_format,
           "--model", model or CLAUDE_MODEL]
    if output_format == "stream-json":
        cmd += ["--verbose", "--include-partial-messages", "--include-hook-events"]
    if ALLOWED_TOOLS:
        cmd += ["--allowedTools", *ALLOWED_TOOLS]
    return cmd


def _run_claude_buffered(cmd: list[str]) -> str:
    r = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=AGENT_TIMEOUT, cwd=WORKDIR, stdin=subprocess.DEVNULL
    )
    out = (r.stdout or "").strip()
    return out or _fallback_no_output(r.stderr or "")


def _run_claude_streaming(cmd: list[str], stream_format: str, tag: str = "",
                          text_delta_cb=None, terminal_echo: bool = True) -> str:
    by_line = stream_format == "json"
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=WORKDIR,
        bufsize=1,
    )
    if terminal_echo:
        print(f"[{_ts()}] ▶ Claude 流开始 [{tag}]（format={stream_format}）", flush=True)
    out_q: "queue.Queue[tuple[str, str]]" = queue.Queue()
    threads = [
        threading.Thread(target=_pipe_to_queue, args=(proc.stdout, "stdout", out_q, by_line), daemon=True),
        threading.Thread(target=_pipe_to_queue, args=(proc.stderr, "stderr", out_q, by_line), daemon=True),
    ]
    for t in threads:
        t.start()

    renderer = (_StreamJsonRenderer(tag=tag, echo=terminal_echo, on_text_delta=text_delta_cb)
                if stream_format == "json" else None)
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    deadline = time.monotonic() + AGENT_TIMEOUT
    timed_out = False

    while True:
        try:
            stream_name, chunk = out_q.get(timeout=0.1)
        except queue.Empty:
            stream_name = chunk = ""

        if chunk:
            if stream_name == "stderr":
                stderr_chunks.append(chunk)
                if terminal_echo:
                    sys.stderr.write(chunk)
                    sys.stderr.flush()
            elif renderer is not None:
                renderer.feed(chunk)
            else:
                stdout_chunks.append(chunk)
                if terminal_echo:
                    print(chunk, end="", flush=True)
                if text_delta_cb:  # text 档也支持流式回复：逐块推送
                    try:
                        text_delta_cb(chunk)
                    except Exception as e:  # noqa: BLE001
                        print(f"[stream-reply error] {e}")

        if proc.poll() is not None and out_q.empty() and all(not t.is_alive() for t in threads):
            break
        if time.monotonic() > deadline:
            timed_out = True
            proc.kill()
            break

    for t in threads:
        t.join(timeout=1)

    if timed_out:
        raise subprocess.TimeoutExpired(cmd, AGENT_TIMEOUT)

    if terminal_echo:
        print(f"\n[{_ts()}] ■ Claude 流结束 [{tag}]", flush=True)
    out = renderer.final_text if renderer is not None else "".join(stdout_chunks).strip()
    return out or _fallback_no_output("".join(stderr_chunks))


# ---------- Codex 底层 ----------
def _build_codex_cmd(text: str, sid: str | None, output_file: str,
                     model: str = "") -> list[str]:
    model = model or CODEX_MODEL
    # 沙箱要在首轮(exec)与续接(exec resume)上完全一致，否则同一会话前后权限会漂移。
    # 坑：--sandbox 只在 exec 首轮可用，resume 子命令不认它，只在首轮传会让 resume 轮悄悄退回
    # Codex 默认沙箱。因此统一改用全局配置覆盖 -c sandbox_mode=...，它对 exec 与 resume 都生效。
    # danger-full-access 档用 --dangerously-bypass-approvals-and-sandbox（同时关审批+沙箱，让
    # Codex 能读写任意本地文件并自由联网），该标志两个子命令也都支持。
    full_access = CODEX_SANDBOX == "danger-full-access"
    sandbox_args = (["--dangerously-bypass-approvals-and-sandbox"] if full_access
                    else ["-c", f'sandbox_mode="{CODEX_SANDBOX}"'])
    if sid:
        cmd = [
            CODEX_BIN, "exec", "resume",
            "--json",
            "--output-last-message", output_file,
            "--model", model,
            *sandbox_args,
        ]
        if CODEX_SKIP_GIT_REPO_CHECK:
            cmd.append("--skip-git-repo-check")
        cmd += [sid, "-"]
        return cmd

    cmd = [
        CODEX_BIN, "exec",
        "--json",
        "--output-last-message", output_file,
        "--model", model,
        "--cd", WORKDIR,
        *sandbox_args,
    ]
    if CODEX_SKIP_GIT_REPO_CHECK:
        cmd.append("--skip-git-repo-check")
    cmd.append("-")
    return cmd


def _extract_codex_session_id(value) -> str | None:
    if isinstance(value, dict):
        for key in ("session_id", "conversation_id", "thread_id"):
            val = value.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for val in value.values():
            found = _extract_codex_session_id(val)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _extract_codex_session_id(item)
            if found:
                return found
    return None


def _codex_event_text(event: dict) -> str:
    etype = str(event.get("type") or event.get("event") or "event")
    if etype == "item.completed":
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        itype = item.get("type") or "item"
        text = item.get("text")
        if isinstance(text, str) and text:
            return f"[codex {itype}] {_truncate(text, 300)}"
        return f"[codex {itype}]"
    if etype in {"assistant_message", "agent_message", "message"}:
        text = event.get("message") or event.get("text") or event.get("content") or ""
        if isinstance(text, str):
            return f"[codex {etype}] {_truncate(text, 300)}"
    if etype in {"error", "turn.failed"}:
        return f"⚠️ [codex {etype}] {_short_json(event, 300)}"
    if etype in {"session", "session_created", "turn_started", "turn_completed", "result"}:
        return f"[codex {etype}] {_short_json(event, 220)}"
    return f"[codex {etype}]"


def _extract_codex_agent_text(event: dict) -> str:
    etype = str(event.get("type") or event.get("event") or "")
    if etype == "item.completed":
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            return item["text"]
    if etype in {"assistant_message", "agent_message", "message"}:
        text = event.get("message") or event.get("text") or event.get("content")
        return text if isinstance(text, str) else ""
    return ""


def _extract_codex_error(event: dict) -> str:
    etype = str(event.get("type") or event.get("event") or "")
    if etype == "error":
        msg = event.get("message")
        return msg if isinstance(msg, str) else _short_json(event, 300)
    if etype == "turn.failed":
        err = event.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]
        return _short_json(event, 300)
    return ""


def _clean_codex_final_text(text: str) -> str:
    lines = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped == "Reading additional input from stdin...":
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}T.*\sERROR\s", stripped):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _run_codex_streaming(cmd: list[str], output_file: str, tag: str = "",
                         input_text: str = "") -> tuple[str, str | None]:
    if STREAM_TERMINAL:
        print(f"[{_ts()}] ▶ Codex 开始 [{tag or 'new'}]", flush=True)
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=WORKDIR,
        bufsize=1,
    )
    try:
        proc.stdin.write(input_text or "")
        proc.stdin.close()
    except Exception as e:  # noqa: BLE001
        print(f"[codex stdin error] {e}")
    out_q: "queue.Queue[tuple[str, str]]" = queue.Queue()
    threads = [
        threading.Thread(target=_pipe_to_queue, args=(proc.stdout, "stdout", out_q, True), daemon=True),
        threading.Thread(target=_pipe_to_queue, args=(proc.stderr, "stderr", out_q, True), daemon=True),
    ]
    for t in threads:
        t.start()

    stdout_lines: list[str] = []
    stderr_chunks: list[str] = []
    agent_text_parts: list[str] = []
    error_messages: list[str] = []
    failed = False
    session_id = None
    deadline = time.monotonic() + AGENT_TIMEOUT
    timed_out = False

    while True:
        try:
            stream_name, chunk = out_q.get(timeout=0.1)
        except queue.Empty:
            stream_name = chunk = ""

        if chunk:
            if stream_name == "stderr":
                stderr_chunks.append(chunk)
                if STREAM_TERMINAL:
                    sys.stderr.write(chunk)
                    sys.stderr.flush()
            else:
                stdout_lines.append(chunk)
                raw = chunk.strip()
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    if STREAM_TERMINAL and raw:
                        print(f"[codex raw] {_truncate(raw, 300)}", flush=True)
                else:
                    session_id = session_id or _extract_codex_session_id(event)
                    agent_text = _extract_codex_agent_text(event)
                    if agent_text:
                        agent_text_parts.append(agent_text)
                    err = _extract_codex_error(event)
                    if err:
                        error_messages.append(err)
                    if str(event.get("type") or event.get("event") or "") == "turn.failed":
                        failed = True
                    if STREAM_TERMINAL:
                        print(_codex_event_text(event), flush=True)

        if proc.poll() is not None and out_q.empty() and all(not t.is_alive() for t in threads):
            break
        if time.monotonic() > deadline:
            timed_out = True
            proc.kill()
            break

    for t in threads:
        t.join(timeout=1)

    if timed_out:
        raise subprocess.TimeoutExpired(cmd, AGENT_TIMEOUT)

    try:
        with open(output_file, encoding="utf-8") as f:
            final_text = _clean_codex_final_text(f.read())
    except OSError:
        final_text = ""

    if STREAM_TERMINAL:
        print(f"\n[{_ts()}] ■ Codex 结束 [{tag or (session_id or 'new')[:8]}]", flush=True)
    if failed:
        msg = error_messages[-1] if error_messages else "未知错误"
        return f"❌ Codex 处理失败：{msg}", session_id
    if not final_text and agent_text_parts:
        final_text = "\n".join(agent_text_parts).strip()
    fallback = "".join(stdout_lines).strip() or "".join(stderr_chunks).strip()
    return final_text or "(codex 无输出) " + fallback[:300], session_id


# ---------- 统一 Agent 接口 ----------
@dataclass
class AgentResult:
    """一次 Agent 调用的产物。"""
    reply: str
    new_sid: str | None = None   # 需持久化的会话 id：claude 复用传入 sid；codex 由 CLI 生成


class Agent:
    """所有 Agent 的统一接口。子类只需实现 run()。"""

    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def display_name(self) -> str:
        return self.cfg.display_name

    @property
    def supports_stream_reply(self) -> bool:
        return self.cfg.supports_stream_reply

    @property
    def supports_model_switch(self) -> bool:
        return self.cfg.supports_model_switch

    @property
    def pregenerate_sid(self) -> bool:
        return self.cfg.pregenerate_sid

    def run(self, *, prompt: str, sid: str | None, is_new: bool,
            model: str = "", text_delta_cb=None) -> AgentResult:
        raise NotImplementedError


class ClaudeAgent(Agent):
    def run(self, *, prompt: str, sid: str | None, is_new: bool,
            model: str = "", text_delta_cb=None) -> AgentResult:
        eff_model = model or self.cfg.default_model
        # 终端 json 档 或 流式回复 都需要 stream-json（前者要结构化事件，后者要增量文本）
        want_json = (STREAM_TERMINAL and TERMINAL_STREAM_FORMAT == "json") or (text_delta_cb is not None)
        output_format = "stream-json" if want_json else "text"
        cmd = _build_claude_cmd(prompt, sid, is_new, output_format, eff_model)
        tag = (sid or "")[:8]
        try:
            if STREAM_TERMINAL or text_delta_cb:
                reply = _run_claude_streaming(
                    cmd, "json" if want_json else "text",
                    tag=tag, text_delta_cb=text_delta_cb, terminal_echo=STREAM_TERMINAL,
                )
            else:
                reply = _run_claude_buffered(cmd)
            return AgentResult(reply=reply, new_sid=sid)
        except subprocess.TimeoutExpired:
            # 不回传 new_sid：首轮超时时会话可能根本没在 claude 侧建立，若把预生成 sid 落盘，
            # 之后每条消息都会 --resume 一个不存在的会话而永久报错。留空让本轮不落盘、下轮重开。
            return AgentResult(reply="⌛ 处理超时了，换个更具体的问题再试。")
        except FileNotFoundError:
            return AgentResult(reply=_cli_missing_message("Claude", self.cfg.bin, "claude_bin"))
        except Exception as e:  # noqa: BLE001
            return AgentResult(reply=f"❌ 处理出错：{e}")


class CodexAgent(Agent):
    def run(self, *, prompt: str, sid: str | None, is_new: bool,
            model: str = "", text_delta_cb=None) -> AgentResult:
        tag = sid[:8] if sid else "new"
        fd, output_file = tempfile.mkstemp(prefix="codex-last-", suffix=".txt", dir=STATE_DIR)
        os.close(fd)
        cmd = _build_codex_cmd(prompt, sid, output_file, self.cfg.default_model)
        try:
            reply, seen_sid = _run_codex_streaming(cmd, output_file, tag=tag, input_text=prompt)
            return AgentResult(reply=reply, new_sid=seen_sid)
        except subprocess.TimeoutExpired:
            return AgentResult(reply="⌛ Codex 处理超时了，换个更具体的问题再试。")
        except FileNotFoundError:
            return AgentResult(reply=_cli_missing_message("Codex", self.cfg.bin, "codex_bin"))
        except Exception as e:  # noqa: BLE001
            return AgentResult(reply=f"❌ Codex 处理出错：{e}")
        finally:
            try:
                os.remove(output_file)
            except OSError:
                pass


_AGENT_CLASSES = {"claude": ClaudeAgent, "codex": CodexAgent}
AGENTS: dict[str, Agent] = {
    name: _AGENT_CLASSES[name](cfg) for name, cfg in AGENT_CONFIGS.items()
}


def get_agent(name: str | None) -> Agent:
    """按名取 Agent 实例；无法识别回退到默认 Agent。"""
    return AGENTS.get(_normalize_agent(name)) or AGENTS[DEFAULT_AGENT]
