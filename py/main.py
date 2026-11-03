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


if __name__ == "__main__":
    # 启动即打印 broker 连接信息：别人的上位机/主控照着填就能连进来
    print_broker_info()

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
    window.showMaximized()

    sys.exit(app.exec())