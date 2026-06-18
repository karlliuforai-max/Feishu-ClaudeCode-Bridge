import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bridge_test_base import BridgeTestCase  # noqa: E402


class BridgeCoreTests(BridgeTestCase):
    """飞书 IM / 会话 / 配置层（config.py + feishu_agent_bridge.py）。"""

    def test_config_paths_are_expanded(self):
        self.assertEqual(self.config.WORKDIR, os.path.abspath(str(self.workdir)))
        self.assertEqual(
            self.config.STATE_DIR,
            os.path.abspath(str(self.home / "bridge-state")),
        )
        # inbox 在 workspace(WORKDIR) 下，且导入时已创建
        self.assertEqual(self.config.INBOX_DIR, os.path.join(self.config.WORKDIR, "inbox"))
        self.assertTrue(os.path.isdir(self.config.INBOX_DIR))

    def test_default_agent_models_are_v020_defaults(self):
        self.assertEqual(self.config.CLAUDE_MODEL, "claude-sonnet-4-6")
        self.assertEqual(self.config.CODEX_MODEL, "gpt-5.5")
        self.assertEqual(self.config.DEFAULT_AGENT, "claude")
        self.assertEqual(self.bridge._agent_model("codex"), "gpt-5.5")
        self.assertEqual(self.config.CLAUDE_BIN, "claude")
        self.assertEqual(self.config.CODEX_BIN, "codex")

    def test_cli_bin_discovers_windows_npm_shim(self):
        if os.name != "nt":
            self.skipTest("Windows npm shim discovery only applies on Windows")
        npm_dir = self.root / "appdata" / "npm"
        npm_dir.mkdir(parents=True, exist_ok=True)
        shim = npm_dir / "claude.cmd"
        shim.write_text("@echo off\r\n", encoding="utf-8")
        self.assertEqual(self.config._cli_bin(None, "claude"), str(shim))

    def test_cli_missing_message_is_actionable(self):
        msg = self.config._cli_missing_message("Claude", "claude", "claude_bin")
        self.assertIn("找不到 Claude CLI", msg)
        self.assertIn("claude_bin", msg)
        self.assertNotIn("WinError", msg)

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

    def test_model_command_blocked_for_fixed_model_agent(self):
        # Codex 不支持 /model：状态文案应提示固定模型
        self.bridge._set_active_agent("k-codex-status", "codex")
        try:
            status = self.bridge._format_agent_status("k-codex-status")
            self.assertIn("不支持 /model", status)
        finally:
            self.bridge.SESSIONS.pop("k-codex-status", None)
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
