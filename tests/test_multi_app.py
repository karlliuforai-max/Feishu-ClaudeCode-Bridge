import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bridge_test_base import BridgeTestCase  # noqa: E402


class SafeAppIdTests(BridgeTestCase):
    def test_safe_app_id_keeps_valid_and_sanitizes_invalid(self):
        f = self.config._safe_app_id
        self.assertEqual(f("cli_aaa7d6da7538dcef"), "cli_aaa7d6da7538dcef")
        self.assertEqual(f("a b/c:d"), "a_b_c_d")
        self.assertEqual(f(""), "default")
        self.assertEqual(f("..."), "default")


class ResolveDirsTests(BridgeTestCase):
    def test_default_dirs_are_namespaced_by_app_id(self):
        workdir, state = self.config._resolve_dirs("cli_app_one", None, None)
        self.assertEqual(
            workdir, os.path.join(self.config.BASE_DIR, "workspace", "cli_app_one")
        )
        self.assertTrue(state.endswith(os.path.join(".feishu_bridge", "cli_app_one")))
        # 两个不同 app_id → 不同目录
        workdir2, state2 = self.config._resolve_dirs("cli_app_two", None, None)
        self.assertNotEqual(workdir, workdir2)
        self.assertNotEqual(state, state2)

    def test_explicit_dirs_used_verbatim(self):
        wd = str(self.root / "explicit-wd")
        sd = str(self.root / "explicit-sd")
        workdir, state = self.config._resolve_dirs("cli_x", wd, sd)
        self.assertEqual(workdir, os.path.abspath(wd))
        self.assertEqual(state, os.path.abspath(sd))


class MigrateLegacySessionsTests(BridgeTestCase):
    OWNER = "cli_owner_app"

    def setUp(self):
        # 把“归属应用”固定为 OWNER，隔离掉真实根 config.json 的影响
        self._orig_owner = self.config._legacy_owner_app_id
        self.config._legacy_owner_app_id = lambda: self.OWNER

    def tearDown(self):
        self.config._legacy_owner_app_id = self._orig_owner

    def _legacy_path(self):
        return self.config._expand_path(os.path.join("~/.feishu_bridge", "sessions.json"))

    def _make_legacy(self, content='{"k": 1}'):
        legacy = Path(self._legacy_path())
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(content, encoding="utf-8")
        return legacy

    def test_migrates_legacy_into_owner_app_subdir(self):
        legacy = self._make_legacy('{"k": 1}')
        target = self.root / "state-mig-1"
        self.config._migrate_legacy_sessions(self.OWNER, str(target), False)
        moved = target / "sessions.json"
        self.assertTrue(moved.is_file())
        self.assertEqual(moved.read_text(encoding="utf-8"), '{"k": 1}')
        self.assertFalse(legacy.exists())  # 已搬走

    def test_no_migration_when_app_is_not_owner(self):
        # 竞态根因守卫：非归属应用绝不认领旧文件
        legacy = self._make_legacy('{"real": "history"}')
        target = self.root / "state-mig-other"
        self.config._migrate_legacy_sessions("cli_some_other_app", str(target), False)
        self.assertFalse((target / "sessions.json").exists())
        self.assertTrue(legacy.exists())  # 原位不动

    def test_no_migration_when_state_dir_explicit(self):
        legacy = self._make_legacy("{}")
        target = self.root / "state-mig-2"
        self.config._migrate_legacy_sessions(self.OWNER, str(target), True)  # explicit → 不动
        self.assertFalse((target / "sessions.json").exists())
        self.assertTrue(legacy.exists())

    def test_does_not_overwrite_existing_target(self):
        legacy = self._make_legacy('{"legacy": true}')
        target = self.root / "state-mig-3"
        target.mkdir(parents=True, exist_ok=True)
        (target / "sessions.json").write_text('{"existing": true}', encoding="utf-8")
        self.config._migrate_legacy_sessions(self.OWNER, str(target), False)
        # 目标已有 → 不覆盖，旧文件保持原位
        self.assertEqual((target / "sessions.json").read_text(encoding="utf-8"), '{"existing": true}')
        self.assertTrue(legacy.exists())


if __name__ == "__main__":
    unittest.main()
