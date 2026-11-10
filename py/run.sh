#!/bin/bash
# 启动产线控制中心（Ubuntu + ROS2 HP60C 深度相机）
# 用法：./run.sh
# 相机 ROS 节点由 main.py 管（启动时开、退出时关），脚本只置环境。

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 1. 源 ROS 2 环境
if [ -f /opt/ros/humble/setup.bash ]; then
    source /opt/ros/humble/setup.bash
    echo "[run.sh] ROS 2 Humble 已加载"
else
    echo "[run.sh] 警告: 未找到 ROS 2 Humble，深度相机将不可用"
fi

# 2. 激活 Python 虚拟环境
VENV_DIR="$SCRIPT_DIR/.venv"
if [ -d "$VENV_DIR" ]; then
    source "$VENV_DIR/bin/activate"
    echo "[run.sh] venv 已激活: $(python --version)"
else
    echo "[run.sh] 警告: 未找到 .venv，使用系统 Python"
fi

# 3. 检查依赖
echo "[run.sh] 检查依赖..."
python3 -c "import PyQt6" 2>/dev/null || { echo "[run.sh] 缺少 PyQt6: pip install PyQt6"; exit 1; }
python3 -c "import paho.mqtt" 2>/dev/null || { echo "[run.sh] 缺少 paho-mqtt: pip install paho-mqtt"; exit 1; }
python3 -c "import cv2" 2>/dev/null || { echo "[run.sh] 缺少 opencv: pip install opencv-python-headless"; exit 1; }

# 4. 启动主程序（相机节点在 main.py 内启动/关闭）
echo "[run.sh] 启动产线控制中心..."
cd "$SCRIPT_DIR"
python3 main.py "$@"
