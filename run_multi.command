#!/bin/zsh
# 飞书 Agent Gateway · 多应用启动器（双击即在 Terminal.app 打开运行）
# 同时托管 configs/*.json 里的每个飞书应用，一进程一应用、会话完全隔离。
# 关掉本终端窗口 = 停止全部应用。按 Ctrl-C 同样全部停止。

cd "$(dirname "$0")" || exit 1

echo "================================================"
echo "  飞书 Agent Gateway · 多应用"
echo "  目录: $(pwd)"
echo "  开始: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  配置: configs/*.json（每个应用一份，含各自 app_id/app_secret）"
echo "  停止: 关闭本窗口，或按 Ctrl-C"
echo "================================================"

python3 run_multi.py

echo "已停止。按回车关闭窗口。"
read
