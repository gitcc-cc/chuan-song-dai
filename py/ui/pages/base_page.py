"""
页面基类
========
统一页面生命周期接口：on_show / on_hide / save_state
所有页面继承此类，保证切换时正确启停资源。
"""
from PyQt6.QtWidgets import QWidget


class BasePage(QWidget):
    """页面基类"""

    def __init__(self, parent=None):
        super().__init__(parent)

    def on_show(self):
        """
        页面显示时调用。
        子类重写：启动定时器、连接信号、开始线程等。
        """
        pass

    def on_hide(self):
        """
        页面隐藏/切换离开时调用。
        子类重写：停止定时器、断开信号、停止线程、释放摄像头等。
        """
        pass

    def save_state(self):
        """
        保存页面状态（关闭应用前调用）。
        子类重写：保存滑块值、勾选状态、输入框内容等。
        """
        pass