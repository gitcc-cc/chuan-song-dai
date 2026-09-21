"""
可折叠侧边栏
============
- 展开宽 240px：图标 + 文字
- 折叠宽 60px：仅图标
- 点击切换页面，顶部有折叠/展开按钮
"""
import os
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QSizePolicy
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QIcon, QFont, QPixmap

try:
    import qtawesome as qta
    _HAS_QTA = True
except ImportError:
    _HAS_QTA = False


class SidebarButton(QPushButton):
    """侧边栏按钮：图标 + 可选文字"""

    def __init__(self, page_id: str, title: str, icon_name: str, parent=None):
        super().__init__(parent)
        self.page_id = page_id
        self._title = title
        self._icon_name = icon_name
        self._expanded = True

        self.setCheckable(True)
        self.setFixedHeight(44)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # 图标
        self._icon_label = QLabel()
        self._icon_label.setFixedSize(40, 40)
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._update_icon()

        # 文字
        self._text_label = QLabel(title)
        self._text_label.setFont(QFont("", 10))
        self._text_label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)

        # 布局
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(12)
        layout.addWidget(self._icon_label)
        layout.addWidget(self._text_label, stretch=1)
        layout.addStretch()

    def _update_icon(self):
        """更新图标（优先PNG文件，其次qtawesome，最后文字首字母）"""
        # 尝试加载 PNG 文件
        if os.path.isfile(self._icon_name):
            pixmap = QPixmap(self._icon_name)
            if not pixmap.isNull():
                self._icon_label.setPixmap(pixmap.scaled(40, 40, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                return
        # 尝试 qtawesome
        if _HAS_QTA:
            icon = qta.icon(self._icon_name, color="#333333", scale_factor=1.2)
            self._icon_label.setPixmap(icon.pixmap(40, 40))
        else:
            # 回退：用文字首字母
            self._icon_label.setText(self._title[0])
            self._icon_label.setFont(QFont("", 16, QFont.Weight.Bold))
            self._icon_label.setStyleSheet("color: #333333;")

    def set_expanded(self, expanded: bool):
        """设置展开/折叠状态"""
        self._expanded = expanded
        self._text_label.setVisible(expanded)
        # 展开时左对齐，折叠时居中
        layout = self.layout()
        if expanded:
            layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        else:
            layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

    def get_page_id(self) -> str:
        return self.page_id


class Sidebar(QWidget):
    """侧边栏组件"""

    page_changed = pyqtSignal(str)      # 页面切换信号
    toggle_requested = pyqtSignal(bool) # 展开/折叠请求

    EXPANDED_WIDTH = 240
    COLLAPSED_WIDTH = 60
    ANIMATION_DURATION = 200

    def __init__(self, parent=None):
        super().__init__(parent)

        self._expanded = True
        self._buttons = {}
        self._current_page = None

        self.setFixedWidth(self.EXPANDED_WIDTH)
        self.setStyleSheet("""
            Sidebar {
                background-color: #ffffff;
                border-right: 1px solid #e0e0e0;
            }
            SidebarButton {
                background-color: transparent;
                border: none;
                border-radius: 6px;
                color: #333333;
            }
            SidebarButton:hover {
                background-color: #f0f0f0;
            }
            SidebarButton:checked {
                background-color: #e8f5e9;
                color: #07C160;
            }
            SidebarButton:checked QLabel {
                color: #07C160;
            }
        """)

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 折叠/展开按钮
        self._toggle_btn = QPushButton()
        self._toggle_btn.setFixedSize(36, 36)
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                border-radius: 6px;
            }
            QPushButton:hover {
                background-color: #f0f0f0;
            }
        """)
        self._update_toggle_icon()
        self._toggle_btn.clicked.connect(self._on_toggle_clicked)

        toggle_container = QWidget()
        toggle_layout = QHBoxLayout(toggle_container)
        toggle_layout.setContentsMargins(12, 8, 12, 8)
        toggle_layout.addWidget(self._toggle_btn)
        toggle_layout.addStretch()
        layout.addWidget(toggle_container)

        # 分隔线
        separator = QWidget()
        separator.setFixedHeight(1)
        separator.setStyleSheet("background-color: #e0e0e0;")
        layout.addWidget(separator)

        # 页面按钮容器
        self._btn_container = QWidget()
        self._btn_layout = QVBoxLayout(self._btn_container)
        self._btn_layout.setContentsMargins(4, 8, 4, 8)
        self._btn_layout.setSpacing(4)
        self._btn_layout.addStretch()
        layout.addWidget(self._btn_container, stretch=1)

    def _update_toggle_icon(self):
        """更新折叠按钮图标"""
        if _HAS_QTA:
            if self._expanded:
                icon = qta.icon("fa5s.chevron-left", color="#333333", scale_factor=1.1)
            else:
                icon = qta.icon("fa5s.chevron-right", color="#333333", scale_factor=1.1)
            self._toggle_btn.setIcon(icon)
            self._toggle_btn.setIconSize(QSize(20, 20))
        else:
            self._toggle_btn.setText("<" if self._expanded else ">")
            self._toggle_btn.setFont(QFont("", 12))

    def _on_toggle_clicked(self):
        """折叠/展开按钮点击"""
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool):
        """设置展开/折叠"""
        if self._expanded == expanded:
            return
        self._expanded = expanded

        # 动画
        self._animation = QPropertyAnimation(self, b"minimumWidth")
        self._animation.setDuration(self.ANIMATION_DURATION)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.setStartValue(self.width())
        self._animation.setEndValue(self.EXPANDED_WIDTH if expanded else self.COLLAPSED_WIDTH)
        self._animation.start()

        # 同步最大宽度
        self.setMaximumWidth(self.EXPANDED_WIDTH if expanded else self.COLLAPSED_WIDTH)

        # 更新所有按钮状态
        for btn in self._buttons.values():
            btn.set_expanded(expanded)

        self._update_toggle_icon()
        self.toggle_requested.emit(expanded)

    def add_page(self, page_id: str, title: str, icon_name: str):
        """添加页面按钮"""
        btn = SidebarButton(page_id, title, icon_name)
        btn.clicked.connect(lambda: self._on_page_clicked(page_id))
        self._buttons[page_id] = btn
        # 插入到 stretch 之前
        self._btn_layout.insertWidget(self._btn_layout.count() - 1, btn)

    def _on_page_clicked(self, page_id: str):
        """页面按钮点击"""
        if page_id == self._current_page:
            return
        # 更新按钮选中状态
        for pid, btn in self._buttons.items():
            btn.setChecked(pid == page_id)
        self._current_page = page_id
        self.page_changed.emit(page_id)

    def set_current(self, page_id: str):
        """外部设置当前页面"""
        if page_id in self._buttons:
            self._buttons[page_id].setChecked(True)
            self._current_page = page_id