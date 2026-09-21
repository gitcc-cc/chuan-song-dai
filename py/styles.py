"""
样式表模块
==========
集中管理所有 Qt 样式，方便后续换主题、改颜色。
修改样式只需要改这一个文件，不会影响其他逻辑。
"""

# ============ 微信风格样式表（字体加大） ============
STYLE = """
QMainWindow {
    background-color: #ededed;
}
QWidget {
    background-color: #ededed;
    color: #333333;
    font-family: 'Noto Sans CJK SC', 'Microsoft YaHei', 'Segoe UI', sans-serif;
    font-size: 16px;
}
QGroupBox {
    background-color: #ffffff;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    margin-top: 0px;
    padding-top: 35px;
    font-size: 17px;
    color: #333333;
    font-weight: bold;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 15px;
    top: 8px;
    padding: 4px 10px;
    color: #333333;
}
QLabel {
    background-color: transparent;
    font-size: 16px;
    color: #333333;
}
QPushButton {
    background-color: #ffffff;
    color: #333333;
    border: 1px solid #e0e0e0;
    border-radius: 6px;
    padding: 10px 18px;
    font-size: 15px;
    font-weight: bold;
}
QPushButton:hover {
    background-color: #f5f5f5;
    border-color: #07C160;
}
QPushButton:pressed {
    background-color: #e8e8e8;
}
QSlider::groove:horizontal {
    height: 6px;
    background: #e0e0e0;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #07C160;
    border: none;
    width: 22px;
    height: 22px;
    margin: -8px 0;
    border-radius: 11px;
}
QSlider::sub-page:horizontal {
    background: #07C160;
    border-radius: 3px;
}
QLineEdit {
    background-color: #ffffff;
    color: #333333;
    border: 1px solid #e0e0e0;
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 16px;
    selection-background-color: #07C160;
}
QLineEdit:focus {
    border-color: #07C160;
}
QTextEdit {
    background-color: #f5f5f5;
    color: #333333;
    border: 1px solid #e0e0e0;
    border-radius: 6px;
    font-size: 14px;
    padding: 8px;
    selection-background-color: #07C160;
}
"""