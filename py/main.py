"""
ESP32 控制中心 - 主程序入口
============================
启动 PyQt6 应用，创建并显示主窗口 (AppShell)。
"""
import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QColor, QPalette

from ui.app_shell import AppShell


if __name__ == "__main__":
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