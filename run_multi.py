#!/usr/bin/env python3
"""run_multi.py — 同时托管多个飞书应用：一进程一应用，会话完全隔离。

每个 config 文件 = 一个独立的 bridge 子进程（自带 app_id/secret；其 state_dir 与
workspace 默认按 app_id 自动分目录，互不串扰）。子进程崩溃自动重启，Ctrl-C 统一停止。
日志按 config 文件名前缀打印，便于区分来自哪个应用。

用法：
  python run_multi.py                  # 自动发现 configs/*.json
  python run_multi.py a.json b.json    # 指定若干 config
"""
import glob
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
BRIDGE = BASE_DIR / "src" / "feishu_agent_bridge.py"
# 与 run_bridge.command 一致：这些退出码重启也无济于事，不再拉起。
# 0=正常 130=Ctrl-C 1=配置等致命错误 127=找不到解释器
NO_RESTART_CODES = {0, 1, 130, 127}
RESTART_DELAY = 3

_children: set[subprocess.Popen] = set()
_children_lock = threading.Lock()


def _pump_output(proc: subprocess.Popen, tag: str) -> None:
    """把子进程输出逐行加上 [tag] 前缀转发到本进程 stdout。"""
    assert proc.stdout is not None
    for line in iter(proc.stdout.readline, ""):
        sys.stdout.write(f"[{tag}] {line if line.endswith(chr(10)) else line + chr(10)}")
        sys.stdout.flush()


def _supervise(config_path: Path, stop: threading.Event) -> None:
    """守护单个应用：起进程、转发日志、崩溃重启，直到 stop 被设置。"""
    tag = config_path.stem
    while not stop.is_set():
        env = dict(os.environ)
        env["BRIDGE_CONFIG"] = str(config_path)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", str(BRIDGE)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as e:
            print(f"[{tag}] 启动失败：{e}")
            return
        with _children_lock:
            _children.add(proc)
        pump = threading.Thread(target=_pump_output, args=(proc, tag), daemon=True)
        pump.start()
        code = proc.wait()
        pump.join(timeout=1)
        with _children_lock:
            _children.discard(proc)

        if stop.is_set() or code in NO_RESTART_CODES:
            print(f"[{tag}] 退出 (code={code})，不再重启。")
            return
        print(f"[{tag}] 异常退出 (code={code})，{RESTART_DELAY} 秒后重启…")
        for _ in range(RESTART_DELAY * 2):  # 0.5s 粒度，便于及时响应 stop
            if stop.is_set():
                return
            time.sleep(0.5)


def _discover_configs(argv: list[str]) -> list[Path]:
    if argv:  # 显式传入的路径原样使用
        return [Path(a).expanduser().resolve() for a in argv]
    # 自动发现 configs/*.json，但跳过 *.example.json 模板（否则会拿占位密钥去连接）
    return [
        Path(p).resolve()
        for p in sorted(glob.glob(str(BASE_DIR / "configs" / "*.json")))
        if not p.endswith(".example.json")
    ]


def main() -> int:
    if not BRIDGE.is_file():
        raise SystemExit(f"❌ 找不到 bridge 入口：{BRIDGE}")
    configs = _discover_configs(sys.argv[1:])
    missing = [str(c) for c in configs if not c.is_file()]
    if missing:
        raise SystemExit("❌ 这些 config 不存在：\n  " + "\n  ".join(missing))
    if not configs:
        raise SystemExit(
            "❌ 未找到任何 config。\n"
            "   把每个应用的 config 放进 configs/*.json（每份含各自的 app_id/app_secret），\n"
            "   或作为参数传入：python run_multi.py a.json b.json"
        )

    print("================================================")
    print(f"  飞书 Agent Gateway · 多应用 ({len(configs)} 个)")
    print(f"  应用：{', '.join(c.stem for c in configs)}")
    print("  停止：按 Ctrl-C（会停止全部应用）")
    print("================================================")

    stop = threading.Event()
    threads = [threading.Thread(target=_supervise, args=(c, stop), daemon=True) for c in configs]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n收到 Ctrl-C，正在停止所有应用…")
    finally:
        stop.set()
        with _children_lock:
            children = list(_children)
        for p in children:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
        for t in threads:
            t.join(timeout=5)
    print("已全部停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
