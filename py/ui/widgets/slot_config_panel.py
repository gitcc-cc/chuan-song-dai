"""
单槽位参数面板
=============
一个槽位的完整参数配置：摄像头选择 + 置信度 + IoU + 推理间隔 + 扫码间隔
1号槽位额外支持「深度相机模式」（HP60C ROS2）。
三个槽位各复制一份。
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QComboBox, QLabel, QPushButton, QCheckBox
)
from PyQt6.QtCore import Qt, pyqtSignal

from ui.widgets.param_slider import ParamSlider, IntParamSlider


class SlotConfigPanel(QWidget):
    """单槽位参数面板"""

    # 信号
    camera_changed = pyqtSignal(int, int)    # (slot_id, camera_index)
    param_changed = pyqtSignal(int, str, float)  # (slot_id, param_name, value)
    depth_mode_changed = pyqtSignal(int, bool)   # (slot_id, enabled)

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
        super().__init__(parent)
        self._slot_id = slot_id
        self._camera_indices = []

        self.setStyleSheet(self._STYLE)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        group = QGroupBox(f"槽位 {self._slot_id}")
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(8)

        # 摄像头选择
        cam_row = QHBoxLayout()
        cam_row.addWidget(QLabel("摄像头:"))
        self.cam_combo = QComboBox()
        self.cam_combo.setMinimumWidth(120)
        self.cam_combo.currentIndexChanged.connect(self._on_camera_changed)
        cam_row.addWidget(self.cam_combo, stretch=1)
        group_layout.addLayout(cam_row)

        # 深度相机模式（仅1号槽位显示）
        self._depth_check = QCheckBox("深度相机 (HP60C)")
        self._depth_check.setToolTip("启用后通过 ROS2 订阅 HP60C 深度相机\n彩色图 + 深度图，检测框中心显示深度值")
        self._depth_check.setVisible(self._slot_id == 1)
        self._depth_check.toggled.connect(self._on_depth_mode_changed)
        group_layout.addWidget(self._depth_check)

        # 置信度
        self.slider_conf = ParamSlider("置信度", 0.0, 1.0,
                                       default=0.5, step=0.01, decimals=2)
        self.slider_conf.value_changed.connect(
            lambda v: self.param_changed.emit(self._slot_id, "conf", v))
        group_layout.addWidget(self.slider_conf)

        # IoU
        self.slider_iou = ParamSlider("IoU", 0.0, 1.0,
                                      default=0.45, step=0.01, decimals=2)
        self.slider_iou.value_changed.connect(
            lambda v: self.param_changed.emit(self._slot_id, "iou", v))
        group_layout.addWidget(self.slider_iou)

        # 推理间隔
        self.slider_inf_interval = IntParamSlider("推理间隔", 1, 30, default=1, unit="帧")
        self.slider_inf_interval.setToolTip("数值越大越省性能，1=每帧推理")
        self.slider_inf_interval.value_changed.connect(
            lambda v: self.param_changed.emit(self._slot_id, "inference_interval", float(v)))
        group_layout.addWidget(self.slider_inf_interval)

        # 扫码间隔
        self.slider_qr_interval = IntParamSlider("扫码间隔", 1, 30, default=1, unit="帧")
        self.slider_qr_interval.setToolTip("跟随检测触发，1=每次检测都扫码")
        self.slider_qr_interval.value_changed.connect(
            lambda v: self.param_changed.emit(self._slot_id, "qr_interval", float(v)))
        group_layout.addWidget(self.slider_qr_interval)

        layout.addWidget(group)

    def _on_camera_changed(self, index: int):
        if index == 0:
            # 默认项 "请选择摄像头"
            self.camera_changed.emit(self._slot_id, -1)
        elif 0 < index <= len(self._camera_indices):
            cam_idx = self._camera_indices[index - 1]
            self.camera_changed.emit(self._slot_id, cam_idx)

    def _on_depth_mode_changed(self, checked: bool):
        """深度相机模式切换"""
        # 深度模式下禁用摄像头选择
        self.cam_combo.setEnabled(not checked)
        self.depth_mode_changed.emit(self._slot_id, checked)

    def is_depth_mode(self) -> bool:
        """是否为深度相机模式"""
        return self._depth_check.isChecked()

    def set_depth_mode(self, enabled: bool):
        """设置深度相机模式"""
        self._depth_check.setChecked(enabled)

    def set_cameras(self, cameras: list):
        """
        设置摄像头列表
        cameras: [(index, name), ...]
        """
        self.cam_combo.blockSignals(True)
        self.cam_combo.clear()
        self._camera_indices = []
        self.cam_combo.addItem("请选择摄像头", -1)
        for cam_idx, cam_name in cameras:
            self.cam_combo.addItem(cam_name, cam_idx)
            self._camera_indices.append(cam_idx)
        self.cam_combo.blockSignals(False)

    def get_camera_index(self) -> int:
        """获取当前选中的摄像头索引（-1 表示未选择）"""
        idx = self.cam_combo.currentIndex()
        if idx <= 0:
            return -1
        if 0 < idx <= len(self._camera_indices):
            return self._camera_indices[idx - 1]
        return -1

    def get_params(self) -> dict:
        """获取当前所有参数"""
        return {
            "camera_index": self.get_camera_index(),
            "conf": self.slider_conf.get_value(),
            "iou": self.slider_iou.get_value(),
            "inference_interval": self.slider_inf_interval.get_value(),
            "qr_interval": self.slider_qr_interval.get_value(),
        }

    def set_params(self, params: dict):
        """批量设置参数（屏蔽信号防止触发 camera_changed）"""
        if "camera_index" in params:
            cam_idx = params["camera_index"]
            self.cam_combo.blockSignals(True)
            if cam_idx < 0:
                self.cam_combo.setCurrentIndex(0)
            else:
                for i, idx in enumerate(self._camera_indices):
                    if idx == cam_idx:
                        self.cam_combo.setCurrentIndex(i + 1)
                        break
            self.cam_combo.blockSignals(False)
        if "conf" in params:
            self.slider_conf.set_value(params["conf"], emit=False)
        if "iou" in params:
            self.slider_iou.set_value(params["iou"], emit=False)
        if "inference_interval" in params:
            self.slider_inf_interval.set_value(float(params["inference_interval"]), emit=False)
        if "qr_interval" in params:
            self.slider_qr_interval.set_value(float(params["qr_interval"]), emit=False)
