"""
产线控制中心 - 主程序入口
============================
启动 PyQt6 应用，创建并显示主窗口 (AppShell)。
"""
import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QColor, QPalette

from ui.app_shell import AppShell
from core.mqtt_shared import print_broker_info
from core.ros_camera import start_ros_camera, stop_ros_camera


if __name__ == "__main__":
    # 启动即打印 broker 连接信息：别人的上位机/主控照着填就能连进来
    print_broker_info()

    # ROS 相机节点跟软件同生共死：这里 fire-and-forget 启动（约 5~8 秒出流，
    # 期间深度预览黑是正常）；退出时 aboutToQuit 统一关闭
    start_ros_camera()

    app = QApplication(sys.argv)

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#ededed"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#333333"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#333333"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#333333"))
    app.setPalette(palette)

    window = AppShell()

    # 大屏默认最大化；小屏（如 RDK 的 HDMI）保持普通窗口模式，
    # 保证标题栏/边框都在屏内，可自由拖拽缩放
    scr = QApplication.primaryScreen().availableGeometry()
    if scr.width() >= 1366 and scr.height() >= 768:
        window.showMaximized()
    else:
        window.show()

    # 关窗时把 ROS 相机节点一起关掉（跟软件同生共死）
    app.aboutToQuit.connect(stop_ros_camera)

    sys.exit(app.exec())