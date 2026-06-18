"""共享测试基类：搭一套临时 HOME/workdir/config 环境，重新导入三模块。

三模块（config / agents / feishu_agent_bridge）在 import 期就会读 config.json、
解析路径、建目录，所以每个测试类都 pop 并重新导入它们，保证彼此隔离、对当前临时
环境生效。子类通过 cls.config / cls.agents / cls.bridge 访问。
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
# 反向依赖顺序 pop，确保重新导入时拿到的是针对当前临时环境的新模块对象
_MODULES = ("feishu_agent_bridge", "agents", "config")
warnings.filterwarnings("ignore", category=DeprecationWarning)


class BridgeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.home = cls.root / "home"
        cls.workdir = cls.root / "workdir"
        cls.home.mkdir()
        cls.workdir.mkdir()

        cls.old_env = {
            key: os.environ.get(key)
            for key in (
                "BRIDGE_CONFIG", "BRIDGE_TEST_WORKDIR", "APPDATA",
                "HOME", "LOCALAPPDATA", "PATH", "USERPROFILE",
            )
        }
        os.environ["HOME"] = str(cls.home)
        os.environ["USERPROFILE"] = str(cls.home)
        os.environ["APPDATA"] = str(cls.root / "appdata")
        os.environ["LOCALAPPDATA"] = str(cls.root / "localappdata")
        os.environ["PATH"] = ""
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

        if str(SRC_ROOT) not in sys.path:
            sys.path.insert(0, str(SRC_ROOT))
        for name in _MODULES:
            sys.modules.pop(name, None)
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        cls.config = importlib.import_module("config")
        cls.agents = importlib.import_module("agents")
        cls.bridge = importlib.import_module("feishu_agent_bridge")

    @classmethod
    def tearDownClass(cls):
        for name in _MODULES:
            sys.modules.pop(name, None)
        if sys.path and sys.path[0] == str(SRC_ROOT):
            sys.path.pop(0)
        for key, value in cls.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.tmp.cleanup()
