import importlib
import io
import json
import os
import sys
import tempfile
import unittest
import warnings
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
warnings.filterwarnings("ignore", category=DeprecationWarning)


class BridgeCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.home = cls.root / "home"
        cls.workdir = cls.root / "workdir"
        cls.home.mkdir()
        cls.workdir.mkdir()

        cls.old_env = {
            "BRIDGE_CONFIG": os.environ.get("BRIDGE_CONFIG"),
            "BRIDGE_TEST_WORKDIR": os.environ.get("BRIDGE_TEST_WORKDIR"),
            "HOME": os.environ.get("HOME"),
        }
        os.environ["HOME"] = str(cls.home)
        os.environ["BRIDGE_TEST_WORKDIR"] = str(cls.workdir)
        config_file = cls.root / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "app_id": "cli_test",
                    "app_secret": "secret_test",
                    "workdir": "$BRIDGE_TEST_WORKDIR",
                    "state_dir": "~/bridge-state",
                }
            ),
            encoding="utf-8",
        )
        os.environ["BRIDGE_CONFIG"] = str(config_file)

        sys.path.insert(0, str(SRC_ROOT))
        sys.modules.pop("feishu_claude_bridge", None)
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        cls.bridge = importlib.import_module("feishu_claude_bridge")

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("feishu_claude_bridge", None)
        if sys.path and sys.path[0] == str(SRC_ROOT):
            sys.path.pop(0)
        for key, value in cls.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.tmp.cleanup()

    def test_config_paths_are_expanded(self):
        self.assertEqual(self.bridge.WORKDIR, os.path.abspath(str(self.workdir)))
        self.assertEqual(
            self.bridge.STATE_DIR,
            os.path.abspath(str(self.home / "bridge-state")),
        )

    def test_default_agent_models_are_v020_defaults(self):
        self.assertEqual(self.bridge.CLAUDE_MODEL, "claude-sonnet-4-6")
        self.assertEqual(self.bridge.CODEX_MODEL, "gpt-5.5")
        self.assertEqual(self.bridge.DEFAULT_AGENT, "claude")
        self.assertEqual(self.bridge._agent_model("codex"), "gpt-5.5")
        self.assertEqual(self.bridge.CLAUDE_BIN, "claude")
        self.assertEqual(self.bridge.CODEX_BIN, "codex")

    def test_legacy_session_records_are_migrated_to_agent_shape(self):
        rec, changed = self.bridge._normalize_session_record("old-claude-sid")
        self.assertTrue(changed)
        self.assertEqual(rec["agent"], "claude")
        self.assertEqual(rec["sessions"]["claude"]["sid"], "old-claude-sid")

        rec, changed = self.bridge._normalize_session_record(
            {"sid": "old-object-sid", "chat_id": "oc_x"}
        )
        self.assertTrue(changed)
        self.assertEqual(rec["chat_id"], "oc_x")
        self.assertEqual(rec["sessions"]["claude"]["sid"], "old-object-sid")

    def test_agent_command_switches_only_current_session(self):
        key = "test-agent-switch"
        self.bridge.SESSIONS.pop(key, None)
        try:
            self.assertIn("当前 Agent：Claude", self.bridge._handle_agent_command(key, ""))
            msg = self.bridge._handle_agent_command(key, "/agent codex")
            self.assertIn("Codex", msg)
            self.assertEqual(self.bridge._get_active_agent(key), "codex")
            self.assertEqual(self.bridge._agent_model("codex", key), "gpt-5.5")

            msg = self.bridge._handle_agent_command(key, "/agent claude")
            self.assertIn("Claude", msg)
            self.assertEqual(self.bridge._get_active_agent(key), "claude")
        finally:
            self.bridge.SESSIONS.pop(key, None)
            self.bridge._save_sessions()

    def test_bot_mention_fails_closed_when_open_id_unknown(self):
        original = self.bridge._get_bot_open_id_cached
        msg = SimpleNamespace(
            mentions=[SimpleNamespace(id=SimpleNamespace(open_id="ou_bot"))]
        )
        try:
            self.bridge._get_bot_open_id_cached = lambda: None
            self.assertFalse(self.bridge._bot_mentioned(msg))

            self.bridge._get_bot_open_id_cached = lambda: "ou_bot"
            self.assertTrue(self.bridge._bot_mentioned(msg))

            self.bridge._get_bot_open_id_cached = lambda: "ou_someone_else"
            self.assertFalse(self.bridge._bot_mentioned(msg))
        finally:
            self.bridge._get_bot_open_id_cached = original

    def test_corrupt_sessions_file_is_backed_up(self):
        sessions_file = Path(self.bridge.SESSIONS_FILE)
        sessions_file.write_text("{broken", encoding="utf-8")

        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.bridge._load_sessions(), {})
        self.assertFalse(sessions_file.exists())
        backups = list(sessions_file.parent.glob("sessions.json.corrupt-*"))
        self.assertTrue(backups)

    def test_parse_message_removes_mentions_and_extracts_post_attachments(self):
        msg = SimpleNamespace(
            message_type="post",
            content=json.dumps(
                {
                    "title": "Title",
                    "content": [
                        [
                            {"tag": "text", "text": "hello @_user_1"},
                            {"tag": "img", "image_key": "img_key"},
                        ],
                        [
                            {
                                "tag": "media",
                                "file_key": "file_key",
                                "file_name": "../report.pdf",
                            }
                        ],
                    ],
                }
            ),
        )

        text, attachments = self.bridge._parse_message(msg)
        self.assertEqual(text, "Title hello")
        self.assertEqual(
            attachments,
            [
                {"file_key": "img_key", "type": "image", "name": ""},
                {"file_key": "file_key", "type": "file", "name": "../report.pdf"},
            ],
        )

    def test_download_resource_rejects_oversized_attachment(self):
        class FakeResponse:
            headers = {"Content-Type": "application/pdf", "Content-Length": "10"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, size=-1):
                return b"0123456789"

        original_token = self.bridge._get_tenant_token
        original_urlopen = self.bridge.urllib.request.urlopen
        original_limit = self.bridge.MAX_ATTACHMENT_BYTES
        try:
            self.bridge._get_tenant_token = lambda: "tenant-token"
            self.bridge.urllib.request.urlopen = lambda req, timeout=60: FakeResponse()
            self.bridge.MAX_ATTACHMENT_BYTES = 5

            with redirect_stdout(io.StringIO()):
                result = self.bridge._download_resource("msg/1", "file_key", "file", "../a.pdf")
            self.assertIsNone(result)
            self.assertFalse(list(Path(self.bridge.INBOX_DIR).glob("*a.pdf")))
        finally:
            self.bridge._get_tenant_token = original_token
            self.bridge.urllib.request.urlopen = original_urlopen
            self.bridge.MAX_ATTACHMENT_BYTES = original_limit

    def test_build_claude_cmd_supports_stream_json_mode(self):
        cmd = self.bridge._build_claude_cmd(
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
        original_claude = self.bridge.CLAUDE_BIN
        original_codex = self.bridge.CODEX_BIN
        try:
            self.bridge.CLAUDE_BIN = "C:/Tools/claude.exe"
            self.bridge.CODEX_BIN = "C:/Tools/codex.exe"
            claude_cmd = self.bridge._build_claude_cmd("hello", "sid", True)
            codex_cmd = self.bridge._build_codex_cmd("hello", None, str(self.root / "out.txt"))
            self.assertEqual(claude_cmd[0], "C:/Tools/claude.exe")
            self.assertEqual(codex_cmd[0], "C:/Tools/codex.exe")
        finally:
            self.bridge.CLAUDE_BIN = original_claude
            self.bridge.CODEX_BIN = original_codex

    def test_build_codex_cmd_new_and_resume(self):
        out = str(self.root / "codex-last.txt")
        new_cmd = self.bridge._build_codex_cmd("hello", None, out)
        self.assertEqual(new_cmd[:2], ["codex", "exec"])
        self.assertIn("--model", new_cmd)
        self.assertIn("gpt-5.5", new_cmd)
        self.assertIn("--cd", new_cmd)
        self.assertIn(self.bridge.WORKDIR, new_cmd)
        self.assertIn("--sandbox", new_cmd)
        self.assertIn(self.bridge.CODEX_SANDBOX, new_cmd)
        self.assertIn("--output-last-message", new_cmd)
        self.assertIn(out, new_cmd)
        self.assertEqual(new_cmd[-1], "-")
        self.assertNotIn("hello", new_cmd)

        resume_cmd = self.bridge._build_codex_cmd("again", "codex-session-id", out)
        self.assertEqual(resume_cmd[:3], ["codex", "exec", "resume"])
        self.assertIn("codex-session-id", resume_cmd)
        self.assertIn("--output-last-message", resume_cmd)
        self.assertEqual(resume_cmd[-1], "-")
        self.assertNotIn("again", resume_cmd)

    def test_codex_event_helpers_extract_text_and_errors(self):
        text = self.bridge._extract_codex_agent_text({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "OK"},
        })
        self.assertEqual(text, "OK")

        err = self.bridge._extract_codex_error({
            "type": "turn.failed",
            "error": {"message": "network failed"},
        })
        self.assertEqual(err, "network failed")

    def test_clean_codex_final_text_removes_cli_diagnostics(self):
        raw = "\n".join([
            "OK",
            "Reading additional input from stdin...",
            "2026-06-18T09:57:27.817679Z ERROR codex_api::endpoint::responses_websocket: failed",
        ])
        self.assertEqual(self.bridge._clean_codex_final_text(raw), "OK")

    def test_summarize_tool_picks_friendly_field(self):
        s = self.bridge._summarize_tool
        self.assertEqual(s("Read", {"file_path": "a.txt"}), "a.txt")
        self.assertEqual(s("Bash", {"command": "ls -la"}), "ls -la")
        self.assertEqual(s("WebFetch", {"url": "https://x.dev"}), "https://x.dev")
        self.assertEqual(s("WebSearch", {"query": "feishu"}), "feishu")
        self.assertEqual(s("TodoWrite", {"todos": [1, 2, 3]}), "3 项待办")

    def test_collapse_folds_long_results(self):
        long = "\n".join(f"line{i}" for i in range(20))
        out = self.bridge._collapse(long, max_lines=3)
        self.assertEqual(out.count("│ line"), 3)
        self.assertIn("已折叠", out)
        self.assertEqual(self.bridge._collapse("short"), "    │ short")
        self.assertEqual(self.bridge._collapse(""), "")

    def test_stream_json_renderer_pairs_tools_and_streams_text(self):
        deltas = []
        r = self.bridge._StreamJsonRenderer(echo=False, on_text_delta=deltas.append)
        # 助手发起工具调用
        r.feed(json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.txt"}},
            ]},
        }))
        self.assertIn("t1", r.tools)
        # 工具结果配对，消费掉记录
        r.feed(json.dumps({
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            ]},
        }))
        self.assertNotIn("t1", r.tools)
        # 增量文本通过回调流出
        r.feed(json.dumps({
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}},
        }))
        r.feed(json.dumps({
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}},
        }))
        self.assertEqual(deltas, ["Hel", "lo"])
        # result 事件给出权威最终文本
        r.feed(json.dumps({"type": "result", "subtype": "success", "result": "final answer"}))
        self.assertEqual(r.final_text, "final answer")

    def test_stream_json_renderer_marks_errors(self):
        r = self.bridge._StreamJsonRenderer(echo=False)
        r.feed(json.dumps({"type": "result", "subtype": "error_max_turns", "result": "x"}))
        self.assertTrue(r.error)

    def test_streaming_card_json_uses_element_id_and_streaming_mode(self):
        card = json.loads(self.bridge._streaming_card_json("hi"))
        self.assertTrue(card["config"]["streaming_mode"])
        el = card["body"]["elements"][0]
        self.assertEqual(el["element_id"], self.bridge.CardStreamer.ELEMENT_ID)
        self.assertEqual(el["content"], "hi")

    def test_card_streamer_custom_card_falls_back_to_normal_reply(self):
        st = self.bridge.CardStreamer("msg/1", "oc_x")
        st.failed = True  # 模拟卡片通道不可用 / 自定义卡片
        handled = st.finish(self.bridge._CARD_MARKER + '{"schema":"2.0"}')
        self.assertFalse(handled)


if __name__ == "__main__":
    unittest.main()
