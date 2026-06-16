#!/bin/zsh
# 飞书 ↔ Claude 桥 启动器（双击即在 Terminal.app 打开运行）
# 独立于 VS Code：关掉 VS Code 不影响本窗口。
# 关掉本终端窗口 = 停止桥。崩溃会自动重启（Ctrl-C 可彻底退出）。

cd "$(dirname "$0")" || exit 1

echo "================================================"
echo "  飞书 ↔ Claude 桥"
echo "  目录: $(pwd)"
echo "  开始: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  停止: 关闭本窗口，或按 Ctrl-C"
echo "================================================"

while true; do
  python3 src/feishu_claude_bridge.py
  code=$?
  # 不重启的退出码：0=正常 130=Ctrl-C 1=配置等致命错误 127=找不到 python3
  # 这些重启也无济于事，避免每 3 秒疯狂重启刷屏。
  case $code in
    0|130|1|127)
      echo ">> 桥退出 (code=$code)，不再重启。"
      break
      ;;
  esac
  echo ">> 桥异常退出 (code=$code)，3 秒后自动重启…"
  sleep 3
done

echo "已停止。按回车关闭窗口。"
read
