"""界面主题令牌与全局样式（唯一配色/间距/字号来源）——浅色版"""

# ============ 色板（浅色 + 微信绿强调） ============
BG = "#f5f5f5"        # 页面背景
PANEL = "#ffffff"     # 面板/顶栏/侧边栏
CARD = "#f0f0f0"      # 卡片/按钮基底
LINE = "#e0e0e0"      # 边框/分隔
TXT = "#333333"       # 主文字
TXT2 = "#666666"      # 次要文字
TXT3 = "#999999"      # 弱化文字
ACC = "#07C160"       # 强调绿（运行/激活/连接）
ACC_D = "#06AD56"     # 强调绿 hover
RED = "#fa5151"       # 停止/错误
RED_D = "#E04444"     # 停止红 hover
AMBER = "#FFC300"     # 警告/自动模式

# ============ 字体 ============
FONT = "'Noto Sans CJK SC','WenQuanYi Zen Hei',sans-serif"
MONO = "'DejaVu Sans Mono','Noto Sans Mono',monospace"

# ============ 间距阶梯 ============
SP_1, SP_2, SP_3, SP_4, SP_5 = 4, 8, 12, 16, 24


def page_qss() -> str:
    """页面级全局 QSS：一次挂到 page.setStyleSheet，收编全部控件外观"""
    return f"""
    QWidget {{
        background-color: {BG};
        color: {TXT};
        font-family: {FONT};
        font-size: 12px;
    }}
    QLabel {{ background: transparent; }}
    QGroupBox {{
        background-color: {PANEL};
        border: 1px solid {LINE};
        border-radius: 6px;
        margin-top: 16px;
        padding: 8px 8px 8px 8px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {TXT2};
        font-size: 12px;
    }}
    QLineEdit {{
        background-color: {BG};
        color: {TXT};
        border: 1px solid {LINE};
        border-radius: 4px;
        padding: 5px 8px;
        selection-background-color: {ACC};
    }}
    QLineEdit:focus {{ border-color: {ACC}; }}
    QPushButton {{
        background-color: {CARD};
        color: {TXT};
        border: 1px solid {LINE};
        border-radius: 4px;
        padding: 8px 12px;
        font-weight: bold;
    }}
    QPushButton:hover {{ background-color: {CARD}; border-color: {TXT3}; }}
    QPushButton:pressed {{ background-color: {ACC}; }}
    QPushButton:checked {{ background-color: {ACC}; border-color: {ACC}; color: #fff; }}
    QPushButton:disabled {{ color: {TXT3}; border-color: {LINE}; }}
    QSlider::groove:horizontal {{
        height: 6px; background: {LINE}; border-radius: 3px;
    }}
    QSlider::handle:horizontal {{
        width: 18px; margin: -7px 0; border-radius: 9px;
        background: {ACC};
    }}
    QSlider::sub-page:horizontal {{ background: {ACC_D}; border-radius: 3px; }}
    QSplitter::handle {{ background-color: {LINE}; }}
    QSplitter::handle:hover {{ background-color: {ACC}; }}
    QComboBox {{
        background: {CARD}; color: {TXT};
        border: 1px solid {LINE}; border-radius: 4px; padding: 4px 8px;
    }}
    QComboBox QAbstractItemView {{
        background: {PANEL}; color: {TXT};
        selection-background-color: {ACC};
        border: 1px solid {LINE};
    }}
    QTextEdit, QPlainTextEdit {{
        background: {BG}; color: {TXT};
        border: 1px solid {LINE}; border-radius: 4px;
        font-family: {MONO}; font-size: 11px;
    }}
    QScrollArea {{ border: none; background: {BG}; }}
    """


def mono_label(color: str = TXT, size: int = 12, bold: bool = False) -> str:
    """等宽字体标签样式（状态数值/坐标等数字显示专用）"""
    w = "font-weight:bold;" if bold else ""
    return f"color:{color}; font-family:{MONO}; font-size:{size}px; {w} background:transparent;"


def solid_btn(color: str, hover: str, text_color: str = "#fff") -> str:
    """实心语义按钮（连接/停止等）"""
    return (f"QPushButton{{background:{color}; color:{text_color}; border:none;"
            f"border-radius:4px; padding:8px 14px; font-weight:bold;}}"
            f"QPushButton:hover{{background:{hover};}}")


def ghost_btn() -> str:
    """描边按钮（模式切换等次要操作）"""
    return (f"QPushButton{{background:transparent; color:{ACC};"
            f"border:1px solid {ACC}; border-radius:4px; padding:8px 14px; font-weight:bold;}}"
            f"QPushButton:hover{{background:{ACC}; color:#fff;}}")


def toggle_btn(padding: str = "8px 12px", font: int = 12) -> str:
    """工具栏方形切换按钮（浅色 / 选中变绿）"""
    return (f"QPushButton{{background:{CARD}; border:none; border-radius:6px;"
            f"padding:{padding}; font-size:{font}px; color:{TXT};}}"
            f"QPushButton:hover{{background:{LINE};}}"
            f"QPushButton:checked{{background:{ACC}; border:2px solid {ACC_D}; color:#fff;}}"
            f"QPushButton:checked:hover{{background:{ACC_D};}}")
