#!/usr/bin/env bash
# 一键构建单文件二进制(含 Python 运行时 + 全部依赖 + WebUI)。
# 产物 dist/serial-gateway 可拷贝到同系统(同 OS+架构)任意机器直接运行。
#
# 用法:
#   bash scripts/build_pyinstaller.sh
#
# 前置:已 pip install -r requirements.txt -e .
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 确保 pyinstaller 可用
if ! python -c "import PyInstaller" 2>/dev/null; then
  pip install pyinstaller
fi

echo "[1/2] PyInstaller 打包中..."
python -m PyInstaller --onefile --name serial-gateway \
  --add-data "src/serial_gateway/static:serial_gateway/static" \
  --collect-submodules serial_gateway \
  --hidden-import serial_asyncio \
  --hidden-import mcp.server.fastmcp \
  --collect-data mcp \
  src/serial_gateway/__main__.py

echo ""
echo "✓ 构建完成"
echo "  产物: $(pwd)/dist/serial-gateway"
echo "  运行: ./dist/serial-gateway"
echo "  部署: scp dist/serial-gateway <目标机>:~/ && ssh <目标机> 'chmod +x serial-gateway && ./serial-gateway'"
