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


if __name__ == "__main__":
    unittest.main()
