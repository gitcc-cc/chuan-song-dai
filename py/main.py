"""
ESP32 控制中心 - 主程序入口
============================
启动 PyQt5 应用，创建并显示主窗口。
不包含任何业务逻辑或界面细节。
"""
import sys
from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QColor, QPalette

from ui.main_window import ESP32MainWindow


if __name__ == "__main__":
    # 创建 Qt 应用
    app = QApplication(sys.argv)

    # 设置全局调色板（基础颜色）
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor("#ededed"))
    palette.setColor(QPalette.WindowText, QColor("#333333"))
    palette.setColor(QPalette.Base, QColor("#ffffff"))
    palette.setColor(QPalette.Text, QColor("#333333"))
    palette.setColor(QPalette.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ButtonText, QColor("#333333"))
    app.setPalette(palette)

    # 创建主窗口并最大化显示
    window = ESP32MainWindow()
    window.showMaximized()

    # 启动事件循环
    sys.exit(app.exec_())