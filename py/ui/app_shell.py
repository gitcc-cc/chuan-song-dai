"""
应用主壳
========
左侧可折叠侧边栏 + 中央 QStackedWidget 页面容器。
"""
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QStackedWidget, QApplication
)
from PyQt6.QtCore import Qt, QSettings, QSize
from PyQt6.QtGui import QIcon

from ui.sidebar import Sidebar
from ui.pages.conveyor_page import ConveyorPage
from ui.pages.camera_page import CameraPage
from core.config import get_config


class AppShell(QMainWindow):
    """主窗口壳：侧边栏 + 页面堆栈"""

    def __init__(self):
        super().__init__()

        self.setWindowTitle("ESP32 控制中心")
        self.resize(1200, 800)

        # 配置管理
        self.config = get_config()

        # 中央部件
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 侧边栏
        self.sidebar = Sidebar()
        self.sidebar.page_changed.connect(self._switch_page)
        self.sidebar.toggle_requested.connect(self._on_sidebar_toggle)
        main_layout.addWidget(self.sidebar)

        # 页面堆栈
        self.stack = QStackedWidget()
        main_layout.addWidget(self.stack, stretch=1)

        # 注册页面
        self._pages = {}
        self._register_pages()

        # 恢复窗口状态
        self._restore_state()

    def _register_pages(self):
        """注册所有页面"""
        # 传送带控制
        conveyor = ConveyorPage()
        self._add_page("conveyor", conveyor, "传送带控制", "fa5s.conveyor-belt")

        # 相机识别
        camera = CameraPage()
        self._add_page("camera", camera, "相机识别", "fa5s.camera")

        # 默认显示第一页
        self.stack.setCurrentIndex(0)
        self.sidebar.set_current("conveyor")

    def _add_page(self, page_id: str, page_widget, title: str, icon_name: str):
        """添加页面到堆栈和侧边栏"""
        self._pages[page_id] = page_widget
        self.stack.addWidget(page_widget)
        self.sidebar.add_page(page_id, title, icon_name)

    def _switch_page(self, page_id: str):
        """切换页面"""
        if page_id in self._pages:
            # 隐藏当前页
            current = self.stack.currentWidget()
            if hasattr(current, "on_hide"):
                current.on_hide()

            self.stack.setCurrentWidget(self._pages[page_id])
            self.sidebar.set_current(page_id)

            # 显示新页
            new_page = self._pages[page_id]
            if hasattr(new_page, "on_show"):
                new_page.on_show()

    def _on_sidebar_toggle(self, expanded: bool):
        """侧边栏展开/折叠"""
        self.config.set("sidebar_expanded", expanded)
        self.config.save()

    def _restore_state(self):
        """恢复窗口几何和侧边栏状态"""
        # 窗口几何
        geom_hex = self.config.get("window_geometry")
        if geom_hex:
            from PyQt6.QtCore import QByteArray
            self.restoreGeometry(QByteArray.fromHex(geom_hex.encode()))
        else:
            self.resize(1200, 800)

        # 侧边栏状态
        expanded = self.config.get("sidebar_expanded", True)
        self.sidebar.set_expanded(expanded)

    def closeEvent(self, event):
        """关闭时保存状态"""
        # 保存窗口几何
        self.config.set("window_geometry", self.saveGeometry().toHex().data().decode())
        self.config.save()

        # 通知当前页保存状态
        current = self.stack.currentWidget()
        if hasattr(current, "save_state"):
            current.save_state()

        # 停止当前页
        if hasattr(current, "on_hide"):
            current.on_hide()

        super().closeEvent(event)