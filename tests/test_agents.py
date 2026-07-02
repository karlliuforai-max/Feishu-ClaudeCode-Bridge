import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bridge_test_base import BridgeTestCase  # noqa: E402


class _FakePipe:
    """立即 EOF 的假管道，给 _pipe_to_queue 用。"""

    def readline(self):
        return ""

    def read(self, _n=-1):
        return ""

    def close(self):
        pass


class _FakeProc:
    def __init__(self, *_args, **_kwargs):
        self.stdin = SimpleNamespace(write=lambda _s: None, close=lambda: None)
        self.stdout = _FakePipe()
        self.stderr = _FakePipe()

    def poll(self):
        return 0

    def kill(self):
        pass


class AgentBuildTests(BridgeTestCase):
    """命令构造 + 事件解析 helper（agents.py）。"""

    def test_build_claude_cmd_supports_stream_json_mode(self):
        cmd = self.agents._build_claude_cmd(
            "hello",
            "00000000-0000-0000-0000-000000000000",
            True,
            "stream-json",
        )
        self.assertIn("--output-format", cmd)
        self.assertIn("stream-json", cmd)
        self.assertIn("--verbose", cmd)
        self.assertIn("--include-partial-messages", cmd)
        self.assertIn("--include-hook-events", cmd)

    def test_build_cmd_uses_configured_cli_bins(self):
        original_claude = self.agents.CLAUDE_BIN
        original_codex = self.agents.CODEX_BIN
        try:
            self.agents.CLAUDE_BIN = "C:/Tools/claude.exe"
            self.agents.CODEX_BIN = "C:/Tools/codex.exe"
            claude_cmd = self.agents._build_claude_cmd("hello", "sid", True)
            codex_cmd = self.agents._build_codex_cmd("hello", None, str(self.root / "out.txt"))
            self.assertEqual(claude_cmd[0], "C:/Tools/claude.exe")
            self.assertEqual(codex_cmd[0], "C:/Tools/codex.exe")
        finally:
            self.agents.CLAUDE_BIN = original_claude
            self.agents.CODEX_BIN = original_codex

    def test_build_codex_cmd_new_and_resume(self):
        out = str(self.root / "codex-last.txt")
        new_cmd = self.agents._build_codex_cmd("hello", None, out)
        self.assertEqual(new_cmd[:2], ["codex", "exec"])
        self.assertIn("--model", new_cmd)
        self.assertIn("gpt-5.5", new_cmd)
        self.assertIn("--cd", new_cmd)
        self.assertIn(self.agents.WORKDIR, new_cmd)
        # 沙箱统一走全局配置覆盖 -c sandbox_mode=...，不再用只对首轮生效的 --sandbox
        sandbox_kv = f'sandbox_mode="{self.agents.CODEX_SANDBOX}"'
        self.assertIn("-c", new_cmd)
        self.assertIn(sandbox_kv, new_cmd)
        self.assertNotIn("--sandbox", new_cmd)
        self.assertIn("--output-last-message", new_cmd)
        self.assertIn(out, new_cmd)
        self.assertEqual(new_cmd[-1], "-")
        self.assertNotIn("hello", new_cmd)

        resume_cmd = self.agents._build_codex_cmd("again", "codex-session-id", out)
        self.assertEqual(resume_cmd[:3], ["codex", "exec", "resume"])
        self.assertIn("codex-session-id", resume_cmd)
        self.assertIn("--output-last-message", resume_cmd)
        # 关键：沙箱在 resume 轮必须与首轮一致（resume 不认 --sandbox，故也走 -c 覆盖）
        self.assertIn(sandbox_kv, resume_cmd)
        self.assertNotIn("--sandbox", resume_cmd)
        self.assertEqual(resume_cmd[-1], "-")
        self.assertNotIn("again", resume_cmd)

    def test_build_codex_cmd_danger_full_access(self):
        out = str(self.root / "codex-last.txt")
        original = self.agents.CODEX_SANDBOX
        try:
            self.agents.CODEX_SANDBOX = "danger-full-access"
            # 关键：bypass 标志要同时出现在首轮和 resume 轮，否则 resume 会退回默认沙箱
            new_cmd = self.agents._build_codex_cmd("hello", None, out)
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", new_cmd)
            self.assertNotIn("--sandbox", new_cmd)
            resume_cmd = self.agents._build_codex_cmd("again", "codex-session-id", out)
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", resume_cmd)
            self.assertNotIn("--sandbox", resume_cmd)
        finally:
            self.agents.CODEX_SANDBOX = original

    def test_codex_event_helpers_extract_text_and_errors(self):
        text = self.agents._extract_codex_agent_text({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "OK"},
        })
        self.assertEqual(text, "OK")

        err = self.agents._extract_codex_error({
            "type": "turn.failed",
            "error": {"message": "network failed"},
        })
        self.assertEqual(err, "network failed")

    def test_codex_event_text_marks_turn_failed(self):
        # 失败事件名统一为 turn.failed，应进入 ⚠️ 高亮分支
        line = self.agents._codex_event_text({"type": "turn.failed", "error": {"message": "boom"}})
        self.assertIn("⚠️", line)

    def test_clean_codex_final_text_removes_cli_diagnostics(self):
        raw = "\n".join([
            "OK",
            "Reading additional input from stdin...",
            "2026-06-18T09:57:27.817679Z ERROR codex_api::endpoint::responses_websocket: failed",
        ])
        self.assertEqual(self.agents._clean_codex_final_text(raw), "OK")

    def test_summarize_tool_picks_friendly_field(self):
        s = self.agents._summarize_tool
        self.assertEqual(s("Read", {"file_path": "a.txt"}), "a.txt")
        self.assertEqual(s("Bash", {"command": "ls -la"}), "ls -la")
        self.assertEqual(s("WebFetch", {"url": "https://x.dev"}), "https://x.dev")
        self.assertEqual(s("WebSearch", {"query": "feishu"}), "feishu")
        self.assertEqual(s("TodoWrite", {"todos": [1, 2, 3]}), "3 项待办")

    def test_collapse_folds_long_results(self):
        long = "\n".join(f"line{i}" for i in range(20))
        out = self.agents._collapse(long, max_lines=3)
        self.assertEqual(out.count("│ line"), 3)
        self.assertIn("已折叠", out)
        self.assertEqual(self.agents._collapse("short"), "    │ short")
        self.assertEqual(self.agents._collapse(""), "")

    def test_stream_json_renderer_pairs_tools_and_streams_text(self):
        deltas = []
        r = self.agents._StreamJsonRenderer(echo=False, on_text_delta=deltas.append)
        r.feed(json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.txt"}},
            ]},
        }))
        self.assertIn("t1", r.tools)
        r.feed(json.dumps({
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            ]},
        }))
        self.assertNotIn("t1", r.tools)
        r.feed(json.dumps({
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}},
        }))
        r.feed(json.dumps({
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}},
        }))
        self.assertEqual(deltas, ["Hel", "lo"])
        r.feed(json.dumps({"type": "result", "subtype": "success", "result": "final answer"}))
        self.assertEqual(r.final_text, "final answer")

    def test_stream_json_renderer_marks_errors(self):
        r = self.agents._StreamJsonRenderer(echo=False)
        r.feed(json.dumps({"type": "result", "subtype": "error_max_turns", "result": "x"}))
        self.assertTrue(r.error)


class AgentInterfaceTests(BridgeTestCase):
    """统一 Agent 接口（registry + run）。"""

    def test_get_agent_returns_typed_instances_with_capabilities(self):
        claude = self.agents.get_agent("claude")
        self.assertEqual(claude.name, "claude")
        self.assertEqual(claude.display_name, "Claude")
        self.assertTrue(claude.supports_model_switch)
        self.assertTrue(claude.supports_stream_reply)
        self.assertTrue(claude.pregenerate_sid)

        codex = self.agents.get_agent("codex")
        self.assertEqual(codex.name, "codex")
        self.assertFalse(codex.supports_model_switch)
        self.assertFalse(codex.supports_stream_reply)
        self.assertFalse(codex.pregenerate_sid)

        # 未知名回退默认 Agent
        self.assertEqual(self.agents.get_agent("nope").name, self.config.DEFAULT_AGENT)

    def test_codex_streaming_runs_without_nameerror(self):
        """回归守卫：v0.2.2 曾在此函数误植 Claude 横幅，导致 NameError。"""
        out_file = str(self.root / "codex-out.txt")
        Path(out_file).write_text("codex reply body", encoding="utf-8")
        orig_popen = self.agents.subprocess.Popen
        orig_stream = self.agents.STREAM_TERMINAL
        try:
            self.agents.subprocess.Popen = lambda *a, **k: _FakeProc()
            self.agents.STREAM_TERMINAL = False
            reply, sid = self.agents._run_codex_streaming(
                ["codex"], out_file, tag="t", input_text="hi"
            )
        finally:
            self.agents.subprocess.Popen = orig_popen
            self.agents.STREAM_TERMINAL = orig_stream
        self.assertEqual(reply, "codex reply body")
        self.assertIsNone(sid)

    def test_codex_agent_run_returns_agentresult_with_new_sid(self):
        orig = self.agents._run_codex_streaming
        try:
            self.agents._run_codex_streaming = lambda *a, **k: ("done", "codex-sid-123")
            result = self.agents.get_agent("codex").run(prompt="hi", sid=None, is_new=True)
        finally:
            self.agents._run_codex_streaming = orig
        self.assertEqual(result.reply, "done")
        self.assertEqual(result.new_sid, "codex-sid-123")

    def test_claude_agent_run_reuses_given_sid(self):
        orig = self.agents._run_claude_streaming
        try:
            self.agents._run_claude_streaming = lambda *a, **k: "claude reply"
            result = self.agents.get_agent("claude").run(
                prompt="hi", sid="sid-abc", is_new=True
            )
        finally:
            self.agents._run_claude_streaming = orig
        self.assertEqual(result.reply, "claude reply")
        self.assertEqual(result.new_sid, "sid-abc")


if __name__ == "__main__":
    unittest.main()
