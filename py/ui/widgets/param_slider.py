"""
参数滑块组件
============
职责：提供滑块+数值输入框的双向同步控件，支持：
- 浮点数参数（如置信度0.0-1.0，IoU阈值等）
- 整数参数（如推理间隔、扫码间隔等）
- 滑块拖动实时更新输入框数值
- 输入框直接输入数值同步到滑块
- 支持单位后缀显示（如"帧"、"%"等）
- 可控制是否发射value_changed信号（用于批量设置时避免重复触发）
"""


from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QSlider, QLineEdit, QLabel, QSizePolicy
)
from PyQt6.QtCore import Qt, pyqtSignal


class ParamSlider(QWidget):
    """
    参数滑块组件：滑块 + 数值输入框双向同步
    支持浮点数和整数参数，用于置信度、IoU等参数的配置
    """

    # 值变更信号（float类型）
    value_changed = pyqtSignal(float)

    def __init__(self, label: str, minimum: float = 0.0, maximum: float = 1.0,
                 default: float = 0.5, step: float = 0.01,
                 decimals: int = 2, unit: str = "",
                 parent=None):
        """
        初始化参数滑块
        参数:
            label: 前缀标签文字（如"置信度"、"IoU"）
            minimum: 参数最小值
            maximum: 参数最大值
            default: 默认值
            step: 步长（滑块分辨率，决定滑块的精度）
            decimals: 小数位数（显示精度）
            unit: 单位后缀（如"帧"、"%"等，可选）
        """
        super().__init__(parent)

        # 保存参数配置
        self._minimum = minimum
        self._maximum = maximum
        self._step = step
        self._decimals = decimals
        self._unit = unit
        self._updating = False  # 防止循环触发标志（避免滑块和输入框互相触发）

        # 计算滑块的整数范围（QSlider只支持整数）
        self._slider_min = 0
        self._slider_max = int((maximum - minimum) / step)
        self._value = default  # 当前值

        self._setup_ui(label)  # 构建UI
        self.set_value(default, emit=False)  # 初始化为默认值（不发射信号）

    def _setup_ui(self, label: str):
        """构建滑块+输入框的UI布局"""
        # 水平布局（无边距，间距8像素）
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 参数名称标签（固定宽度80像素）
        self.lbl_name = QLabel(label)
        self.lbl_name.setFixedWidth(80)
        self.lbl_name.setStyleSheet("font-size: 14px; color: #333;")
        layout.addWidget(self.lbl_name)

        # 水平滑块（拉伸填充）
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(self._slider_min, self._slider_max)  # 设置整数范围
        self.slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # 连接滑块值变更信号到槽函数
        self.slider.valueChanged.connect(self._on_slider_changed)
        layout.addWidget(self.slider, stretch=1)

        # 数值输入框（固定宽度70像素，居中对齐）
        self.edit = QLineEdit()
        self.edit.setFixedWidth(70)
        self.edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.edit.setStyleSheet("font-size: 13px;")
        # 连接回车键按下信号到槽函数
        self.edit.returnPressed.connect(self._on_edit_finished)
        layout.addWidget(self.edit)

        # 单位标签（如果有单位）
        if self._unit:
            self.lbl_unit = QLabel(self._unit)
            self.lbl_unit.setFixedWidth(30)
            self.lbl_unit.setStyleSheet("font-size: 13px; color: #999;")  # 灰色单位文字
            layout.addWidget(self.lbl_unit)

    def _slider_to_value(self, slider_val: int) -> float:
        """将滑块的整数值转换为实际参数值"""
        # 公式：实际值 = 最小值 + 滑块值 × 步长
        return round(self._minimum + slider_val * self._step, self._decimals)

    def _value_to_slider(self, value: float) -> int:
        """将实际参数值转换为滑块的整数值"""
        # 公式：滑块值 = (实际值 - 最小值) / 步长
        return int(round((value - self._minimum) / self._step))

    def _on_slider_changed(self, slider_val: int):
        """滑块值变更回调（拖动滑块时触发）"""
        if self._updating:
            return  # 正在程序化更新，跳过
        # 将滑块整数值转换为实际参数值
        value = self._slider_to_value(slider_val)
        self._value = value
        # 同步更新输入框显示
        self.edit.setText(f"{value:.{self._decimals}f}")
        # 发射值变更信号
        self.value_changed.emit(value)

    def _on_edit_finished(self):
        """输入框回车键按下回调（直接输入数值时触发）"""
        if self._updating:
            return  # 正在程序化更新，跳过
        try:
            # 移除单位后缀并去除空格
            text = self.edit.text().replace(self._unit, "").strip()
            value = float(text)  # 转换为浮点数
            # 限制在有效范围内
            value = max(self._minimum, min(self._maximum, value))
            # 更新值（emit=True发射信号）
            self.set_value(value, emit=True)
        except ValueError:
            # 输入无效，恢复为当前值
            self.edit.setText(f"{self._value:.{self._decimals}f}")

    def set_value(self, value: float, emit: bool = True):
        """
        程序化设置参数值
        value: 目标值
        emit: 是否发射value_changed信号（批量设置时设为False避免重复触发）
        """
        # 限制在有效范围内
        value = max(self._minimum, min(self._maximum, value))
        self._value = value
        self._updating = True  # 设置更新标志，防止循环触发
        # 同步更新滑块和输入框
        self.slider.setValue(self._value_to_slider(value))
        self.edit.setText(f"{value:.{self._decimals}f}")
        self._updating = False  # 清除更新标志
        # 根据参数决定是否发射信号
        if emit:
            self.value_changed.emit(value)

    def get_value(self) -> float:
        """获取当前参数值"""
        return self._value

    def set_enabled(self, enabled: bool):
        """设置组件的启用/禁用状态"""
        self.slider.setEnabled(enabled)
        self.edit.setEnabled(enabled)
        self.lbl_name.setEnabled(enabled)
        # 如果有单位标签，也一起控制
        if hasattr(self, 'lbl_unit'):
            self.lbl_unit.setEnabled(enabled)


class IntParamSlider(ParamSlider):
    """整数参数滑块（继承自ParamSlider，简化整数参数的使用）"""

    def __init__(self, label: str, minimum: int = 0, maximum: int = 100,
                 default: int = 50, unit: str = "", parent=None):
        """
        初始化整数参数滑块
        label: 参数名称
        minimum: 最小值（整数）
        maximum: 最大值（整数）
        default: 默认值（整数）
        unit: 单位后缀（如"帧"）
        """
        # 调用父类构造函数，step=1.0, decimals=0
        super().__init__(label, float(minimum), float(maximum),
                         float(default), step=1.0, decimals=0, unit=unit, parent=parent)
        # 重写显示格式为整数
        self._decimals = 0

    def _slider_to_value(self, slider_val: int) -> float:
        """滑块整数值直接转换为参数值（无需乘以步长）"""
        return float(slider_val + self._minimum)

    def _value_to_slider(self, value: float) -> int:
        """参数值直接转换为滑块整数值（无需除以步长）"""
        return int(value - self._minimum)

    def set_value(self, value: float, emit: bool = True):
        """设置整数值（重写父类方法，显示无小数点）"""
        super().set_value(value, emit)
        # 强制显示为整数格式（无小数点）
        self.edit.setText(str(int(self._value)))

    def get_value(self) -> int:
        """获取整数值（重写父类方法，返回int类型）"""
        return int(self._value)