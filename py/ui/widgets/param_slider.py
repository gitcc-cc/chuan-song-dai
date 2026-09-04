"""
参数滑块组件
============
滑块 + 输入框双向同步，支持整数/浮点数。
复用于：置信度阈值、IoU 阈值等。
"""
from typing import Union, Callable, Optional

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QSlider, QLineEdit, QLabel, QSizePolicy
)
from PyQt6.QtCore import Qt, pyqtSignal


class ParamSlider(QWidget):
    """
    参数滑块：滑块 + 数值输入框
    - value_changed(float) 信号：值变化时发射
    """

    value_changed = pyqtSignal(float)

    def __init__(self, label: str, minimum: float = 0.0, maximum: float = 1.0,
                 default: float = 0.5, step: float = 0.01,
                 decimals: int = 2, unit: str = "",
                 parent=None):
        """
        参数:
            label: 前缀标签文字
            minimum: 最小值
            maximum: 最大值
            default: 默认值
            step: 步长（滑块分辨率）
            decimals: 小数位数
            unit: 单位后缀（如 "%", "ms"）
        """
        super().__init__(parent)

        self._minimum = minimum
        self._maximum = maximum
        self._step = step
        self._decimals = decimals
        self._unit = unit
        self._updating = False  # 防止循环触发

        # 计算滑块整数范围
        self._slider_min = 0
        self._slider_max = int((maximum - minimum) / step)
        self._value = default

        self._setup_ui(label)
        self.set_value(default, emit=False)

    def _setup_ui(self, label: str):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 标签
        self.lbl_name = QLabel(label)
        self.lbl_name.setFixedWidth(80)
        self.lbl_name.setStyleSheet("font-size: 14px; color: #333;")
        layout.addWidget(self.lbl_name)

        # 滑块
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(self._slider_min, self._slider_max)
        self.slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.slider.valueChanged.connect(self._on_slider_changed)
        layout.addWidget(self.slider, stretch=1)

        # 输入框
        self.edit = QLineEdit()
        self.edit.setFixedWidth(70)
        self.edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.edit.setStyleSheet("font-size: 13px;")
        self.edit.returnPressed.connect(self._on_edit_finished)
        layout.addWidget(self.edit)

        # 单位
        if self._unit:
            self.lbl_unit = QLabel(self._unit)
            self.lbl_unit.setFixedWidth(30)
            self.lbl_unit.setStyleSheet("font-size: 13px; color: #999;")
            layout.addWidget(self.lbl_unit)

    def _slider_to_value(self, slider_val: int) -> float:
        """滑块整数 -> 实际数值"""
        return round(self._minimum + slider_val * self._step, self._decimals)

    def _value_to_slider(self, value: float) -> int:
        """实际数值 -> 滑块整数"""
        return int(round((value - self._minimum) / self._step))

    def _on_slider_changed(self, slider_val: int):
        if self._updating:
            return
        value = self._slider_to_value(slider_val)
        self._value = value
        self.edit.setText(f"{value:.{self._decimals}f}")
        self.value_changed.emit(value)

    def _on_edit_finished(self):
        if self._updating:
            return
        try:
            text = self.edit.text().replace(self._unit, "").strip()
            value = float(text)
            value = max(self._minimum, min(self._maximum, value))
            self.set_value(value, emit=True)
        except ValueError:
            # 恢复当前值
            self.edit.setText(f"{self._value:.{self._decimals}f}")

    def set_value(self, value: float, emit: bool = True):
        """程序化设置值"""
        value = max(self._minimum, min(self._maximum, value))
        self._value = value
        self._updating = True
        self.slider.setValue(self._value_to_slider(value))
        self.edit.setText(f"{value:.{self._decimals}f}")
        self._updating = False
        if emit:
            self.value_changed.emit(value)

    def get_value(self) -> float:
        return self._value

    def set_enabled(self, enabled: bool):
        self.slider.setEnabled(enabled)
        self.edit.setEnabled(enabled)
        self.lbl_name.setEnabled(enabled)
        if hasattr(self, 'lbl_unit'):
            self.lbl_unit.setEnabled(enabled)


class IntParamSlider(ParamSlider):
    """整数参数滑块（step=1, decimals=0）"""

    def __init__(self, label: str, minimum: int = 0, maximum: int = 100,
                 default: int = 50, unit: str = "", parent=None):
        super().__init__(label, float(minimum), float(maximum),
                         float(default), step=1.0, decimals=0, unit=unit, parent=parent)
        # 重写显示格式
        self._decimals = 0

    def _slider_to_value(self, slider_val: int) -> float:
        return float(slider_val + self._minimum)

    def _value_to_slider(self, value: float) -> int:
        return int(value - self._minimum)

    def set_value(self, value: float, emit: bool = True):
        super().set_value(value, emit)
        # 整数显示无小数点
        self.edit.setText(str(int(self._value)))

    def get_value(self) -> int:
        return int(self._value)