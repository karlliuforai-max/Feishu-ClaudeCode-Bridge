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

    def test_resolve_mentions_keeps_names_instead_of_deleting(self):
        SN = SimpleNamespace
        mentions = [SN(key="@_user_1", name="PigKarl"), SN(key="@_user_2", name="张三")]
        self.assertEqual(
            self.bridge._resolve_mentions("@_user_1 帮我看 @_user_2 的方案", mentions),
            "@PigKarl 帮我看 @张三 的方案",
        )
        # 没有名字的占位符仍然删掉，不漏 @_user_N
        self.assertEqual(
            self.bridge._resolve_mentions("hi @_user_9 there", [SN(key="@_user_9", name="")]),
            "hi there",
        )
        # 完全没有 mentions 信息时回退为删除占位符（不残留 @_user_N）
        self.assertEqual(self.bridge._resolve_mentions("hi @_user_1", None), "hi")

    def test_parse_text_message_renders_mention_names(self):
        msg = SimpleNamespace(
            message_type="text",
            content=json.dumps({"text": "@_user_1 你好 @_user_2"}),
            mentions=[
                SimpleNamespace(key="@_user_1", name="机器人"),
                SimpleNamespace(key="@_user_2", name="老王"),
            ],
        )
        text, attachments = self.bridge._parse_message(msg)
        self.assertEqual(text, "@机器人 你好 @老王")
        self.assertEqual(attachments, [])

    def test_replied_message_id_reads_direct_parent_only(self):
        self.assertEqual(
            self.bridge._replied_message_id(SimpleNamespace(parent_id="om_p")), "om_p"
        )
        self.assertIsNone(self.bridge._replied_message_id(SimpleNamespace(parent_id="")))
        self.assertIsNone(self.bridge._replied_message_id(SimpleNamespace(parent_id=None)))
        self.assertIsNone(self.bridge._replied_message_id(SimpleNamespace()))

    def test_build_agent_prompt_without_reply_falls_back(self):
        # 无引用、纯文本：原样返回
        self.assertEqual(self.bridge._build_agent_prompt("hi", []), "hi")
        # 无引用、有当前附件：退回 _build_media_prompt 行为
        p = self.bridge._build_agent_prompt("看图", ["/tmp/a.png"])
        self.assertIn("/tmp/a.png", p)
        self.assertIn("用户附言：看图", p)

    def test_build_agent_prompt_with_replied_text_and_file(self):
        p = self.bridge._build_agent_prompt(
            "帮我整理", [], replied_text="会议记录原文", replied_paths=["/tmp/ref.pdf"]
        )
        self.assertIn("【用户本次消息】", p)
        self.assertIn("帮我整理", p)
        self.assertIn("引用的内容", p)
        self.assertIn("会议记录原文", p)
        self.assertIn("/tmp/ref.pdf", p)

    def test_build_agent_prompt_reply_failed_adds_note(self):
        p = self.bridge._build_agent_prompt("看一下", [], reply_failed=True)
        self.assertIn("读取失败", p)
        self.assertIn("看一下", p)

    def test_build_agent_prompt_reply_file_only_without_current_text(self):
        p = self.bridge._build_agent_prompt("", [], replied_paths=["/tmp/ref.pdf"])
        self.assertIn("无文字", p)
        self.assertIn("/tmp/ref.pdf", p)
        self.assertIn("请先解析", p)

    def test_fetch_message_failure_keeps_current_message(self):
        class FakeResp:
            code = 230002
            msg = "no permission"

            def success(self):
                return False

        original = self.bridge.client
        self.bridge.client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(message=SimpleNamespace(get=lambda req: FakeResp()))
            )
        )
        try:
            with redirect_stdout(io.StringIO()):
                self.assertIsNone(self.bridge._fetch_message("om_missing"))
        finally:
            self.bridge.client = original

    def test_fetch_message_success_is_reusable_by_parse_message(self):
        fake_item = SimpleNamespace(
            message_id="om_parent",
            msg_type="text",
            body=SimpleNamespace(content=json.dumps({"text": "原始想法 @_user_1"})),
            mentions=[SimpleNamespace(key="@_user_1", name="老王")],
        )

        class FakeResp:
            data = SimpleNamespace(items=[fake_item])

            def success(self):
                return True

        original = self.bridge.client
        self.bridge.client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(message=SimpleNamespace(get=lambda req: FakeResp()))
            )
        )
        try:
            ref = self.bridge._fetch_message("om_parent")
            self.assertIsNotNone(ref)
            text, attachments = self.bridge._parse_message(ref)
            self.assertEqual(text, "原始想法 @老王")
            self.assertEqual(attachments, [])
        finally:
            self.bridge.client = original

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
