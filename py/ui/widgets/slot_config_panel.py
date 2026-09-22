"""
单槽位参数配置面板组件
======================
职责：为每个产线槽位提供完整的参数配置界面，包括：
- 摄像头选择下拉框（从系统摄像头列表中选择）
- 置信度阈值滑块（0.0-1.0，默认0.5）
- IoU阈值滑块（0.0-1.0，默认0.45）
- 推理间隔滑块（1-30帧，默认1，即每帧推理）
- 扫码间隔滑块（1-30帧，默认1，即每次检测都扫码）
- 深度相机模式开关（仅1号槽位支持，HP60C ROS2深度相机）

三个槽位各复制一份此面板，通过slot_id区分。
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QComboBox, QLabel, QCheckBox
)
from PyQt6.QtCore import pyqtSignal

from ui.widgets.param_slider import ParamSlider, IntParamSlider


class SlotConfigPanel(QWidget):
    """
    单槽位参数配置面板
    每个槽位对应一个此面板，包含摄像头选择和各种参数配置
    """

    # 信号定义（用于与其他组件通信）
    camera_changed = pyqtSignal(int, int)    # 摄像头变更信号：(槽位ID, 摄像头索引)
    param_changed = pyqtSignal(int, str, float)  # 参数变更信号：(槽位ID, 参数名, 参数值)
    depth_mode_changed = pyqtSignal(int, bool)   # 深度模式变更信号：(槽位ID, 是否启用)

    # 面板样式定义（QGroupBox和QComboBox的样式）
    _STYLE = """
        QGroupBox {
            font-weight: bold;
            font-size: 13px;
            border: 1px solid #d0d0d0;
            border-radius: 6px;
            margin-top: 8px;
            padding: 10px 8px 8px 8px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
        }
        QComboBox {
            padding: 4px 8px;
            border: 1px solid #ccc;
            border-radius: 4px;
            font-size: 13px;
        }
    """

    def __init__(self, slot_id: int, parent=None):
        """
        初始化槽位配置面板
        slot_id: 槽位编号（1-3）
        """
        super().__init__(parent)
        self._slot_id = slot_id  # 保存槽位ID
        self._camera_indices = []  # 摄像头索引列表（与下拉框选项对应）

        self.setStyleSheet(self._STYLE)  # 应用样式
        self._setup_ui()  # 构建UI

    def _setup_ui(self):
        """构建面板UI布局"""
        # 主垂直布局（无边距，间距8像素）
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 分组框（标题显示槽位编号）
        group = QGroupBox(f"槽位 {self._slot_id}")
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(8)

        # 摄像头选择行（标签+下拉框）
        cam_row = QHBoxLayout()
        cam_row.addWidget(QLabel("摄像头:"))
        self.cam_combo = QComboBox()  # 摄像头选择下拉框
        self.cam_combo.setMinimumWidth(120)  # 最小宽度
        # 连接下拉框变更信号到槽函数
        self.cam_combo.currentIndexChanged.connect(self._on_camera_changed)
        cam_row.addWidget(self.cam_combo, stretch=1)  # 拉伸填充
        group_layout.addLayout(cam_row)

        # 深度相机模式复选框（仅1号槽位显示）
        self._depth_check = QCheckBox("深度相机 (HP60C)")
        self._depth_check.setToolTip("启用后通过 ROS2 订阅 HP60C 深度相机\n彩色图 + 深度图，检测框中心显示深度值")
        # 只有1号槽位显示深度相机选项
        self._depth_check.setVisible(self._slot_id == 1)
        # 连接复选框状态变更信号
        self._depth_check.toggled.connect(self._on_depth_mode_changed)
        group_layout.addWidget(self._depth_check)

        # 置信度滑块（0.0-1.0，默认0.5，步长0.01）
        self.slider_conf = ParamSlider("置信度", 0.0, 1.0,
                                       default=0.5, step=0.01, decimals=2)
        # 连接滑块值变更信号，发射param_changed信号
        self.slider_conf.value_changed.connect(
            lambda v: self.param_changed.emit(self._slot_id, "conf", v))
        group_layout.addWidget(self.slider_conf)

        # IoU滑块（0.0-1.0，默认0.45，步长0.01）
        self.slider_iou = ParamSlider("IoU", 0.0, 1.0,
                                      default=0.45, step=0.01, decimals=2)
        self.slider_iou.value_changed.connect(
            lambda v: self.param_changed.emit(self._slot_id, "iou", v))
        group_layout.addWidget(self.slider_iou)

        # 推理间隔滑块（1-30帧，默认1，即每帧都推理）
        self.slider_inf_interval = IntParamSlider("推理间隔", 1, 30, default=1, unit="帧")
        self.slider_inf_interval.setToolTip("数值越大越省性能，1=每帧推理")
        self.slider_inf_interval.value_changed.connect(
            lambda v: self.param_changed.emit(self._slot_id, "inference_interval", float(v)))
        group_layout.addWidget(self.slider_inf_interval)

        # 扫码间隔滑块（1-30帧，默认1，即每次检测都扫码）
        self.slider_qr_interval = IntParamSlider("扫码间隔", 1, 30, default=1, unit="帧")
        self.slider_qr_interval.setToolTip("跟随检测触发，1=每次检测都扫码")
        self.slider_qr_interval.value_changed.connect(
            lambda v: self.param_changed.emit(self._slot_id, "qr_interval", float(v)))
        group_layout.addWidget(self.slider_qr_interval)

        layout.addWidget(group)  # 将分组框添加到主布局

    def _on_camera_changed(self, index: int):
        """摄像头选择变更回调"""
        if index == 0:
            # 第一项是默认的"请选择摄像头"，发射-1表示未选择
            self.camera_changed.emit(self._slot_id, -1)
        elif 0 < index <= len(self._camera_indices):
            # 从_camera_indices列表获取真实的摄像头索引
            cam_idx = self._camera_indices[index - 1]
            self.camera_changed.emit(self._slot_id, cam_idx)

    def _on_depth_mode_changed(self, checked: bool):
        """深度相机模式切换回调"""
        # 深度模式下禁用摄像头选择（深度相机通过ROS2订阅，不需要手动选择）
        self.cam_combo.setEnabled(not checked)
        # 发射深度模式变更信号
        self.depth_mode_changed.emit(self._slot_id, checked)

    def is_depth_mode(self) -> bool:
        """检查是否为深度相机模式"""
        return self._depth_check.isChecked()

    def set_depth_mode(self, enabled: bool):
        """设置深度相机模式状态"""
        self._depth_check.setChecked(enabled)

    def set_cameras(self, cameras: list):
        """
        设置可选摄像头列表
        cameras: [(index, name), ...] 摄像头索引和名称的元组列表
        """
        # 阻塞信号防止设置过程中触发camera_changed
        self.cam_combo.blockSignals(True)
        self.cam_combo.clear()  # 清空现有选项
        self._camera_indices = []  # 清空索引列表
        # 添加默认选项
        self.cam_combo.addItem("请选择摄像头", -1)
        # 添加所有可用摄像头
        for cam_idx, cam_name in cameras:
            self.cam_combo.addItem(cam_name, cam_idx)
            self._camera_indices.append(cam_idx)
        self.cam_combo.blockSignals(False)  # 恢复信号

    def get_camera_index(self) -> int:
        """获取当前选中的摄像头索引（-1表示未选择）"""
        idx = self.cam_combo.currentIndex()
        if idx <= 0:
            return -1  # 默认选项或无效索引
        # 从_camera_indices列表获取真实索引
        if 0 < idx <= len(self._camera_indices):
            return self._camera_indices[idx - 1]
        return -1

    def get_params(self) -> dict:
        """获取当前所有配置参数"""
        return {
            "camera_index": self.get_camera_index(),
            "conf": self.slider_conf.get_value(),
            "iou": self.slider_iou.get_value(),
            "inference_interval": self.slider_inf_interval.get_value(),
            "qr_interval": self.slider_qr_interval.get_value(),
        }

    def set_params(self, params: dict):
        """
        批量设置参数（屏蔽信号防止触发camera_changed）
        用于加载保存的配置或外部设置参数
        """
        # 设置摄像头索引
        if "camera_index" in params:
            cam_idx = params["camera_index"]
            self.cam_combo.blockSignals(True)  # 阻塞信号
            if cam_idx < 0:
                self.cam_combo.setCurrentIndex(0)  # 选择默认项
            else:
                # 查找对应的索引并设置
                for i, idx in enumerate(self._camera_indices):
                    if idx == cam_idx:
                        self.cam_combo.setCurrentIndex(i + 1)
                        break
            self.cam_combo.blockSignals(False)  # 恢复信号
        # 设置置信度（emit=False不发射信号）
        if "conf" in params:
            self.slider_conf.set_value(params["conf"], emit=False)
        # 设置IoU
        if "iou" in params:
            self.slider_iou.set_value(params["iou"], emit=False)
        # 设置推理间隔
        if "inference_interval" in params:
            self.slider_inf_interval.set_value(float(params["inference_interval"]), emit=False)
        # 设置扫码间隔
        if "qr_interval" in params:
            self.slider_qr_interval.set_value(float(params["qr_interval"]), emit=False)
