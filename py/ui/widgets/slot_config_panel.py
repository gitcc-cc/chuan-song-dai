"""
单槽位参数配置面板组件
======================
职责：为每个产线槽位提供完整的参数配置界面，包括：
- 摄像头选择下拉框（从系统摄像头列表中选择；1号槽位含「深度相机 (HP60C)」选项）
- 置信度阈值滑块（0.0-1.0，默认0.5）
- IoU阈值滑块（0.0-1.0，默认0.45）
- 推理间隔滑块（1-30帧，默认1，即每帧推理）
- 扫码间隔滑块（1-30帧，默认1，即每次检测都扫码）

三个槽位各复制一份此面板，通过slot_id区分。

深度相机融合说明：不再用复选框，深度相机就是下拉框里的一个选项
（data=-2 标记）。选它走 ROS2 DepthWorker；选普通相机走 OpenCV。
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QComboBox, QLabel, QPushButton
)
from PyQt6.QtCore import pyqtSignal

from ui.widgets.param_slider import ParamSlider, IntParamSlider

# 深度相机在下拉框 data 中的特殊标记（camera_changed/-2 不会混入真实索引）
DEPTH_TAG = -2


class SlotConfigPanel(QWidget):
    """
    单槽位参数配置面板
    每个槽位对应一个此面板，包含摄像头选择和各种参数配置
    """

    # 信号定义（用于与其他组件通信）
    camera_changed = pyqtSignal(int, int)    # 摄像头变更信号：(槽位ID, 摄像头索引)
    param_changed = pyqtSignal(int, str, float)  # 参数变更信号：(槽位ID, 参数名, 参数值)
    depth_mode_changed = pyqtSignal(int, bool)   # 深度模式变更信号：(槽位ID, 是否启用)
    restart_camera_clicked = pyqtSignal(int)     # 手动重启相机节点：(槽位ID)

    # 面板样式定义（QGroupBox和QComboBox的样式）
    _STYLE = """
        QGroupBox {
            font-weight: bold;
            font-size: 11px;
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
            font-size: 11px;
        }
    """

    def __init__(self, slot_id: int, yolo_params: bool = True, parent=None):
        """
        初始化槽位配置面板
        slot_id: 槽位编号（1-3）
        yolo_params: 是否显示 YOLO 相关参数（置信度/IoU/推理间隔）。
                     目前只有槽位1跑 YOLO（深度相机检测），槽位2/3 只扫码不检测。
        """
        super().__init__(parent)
        self._slot_id = slot_id  # 保存槽位ID
        self._yolo_params = yolo_params  # YOLO 参数开关（决定滑块创建与存取）
        self._camera_indices = []  # 摄像头索引列表（与下拉框选项对应）
        self._depth_active = False  # 深度相机模式状态（选中下拉里的深度项）

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

        # 摄像头选择行（标签+下拉框；1号槽位多一个「深度相机」选项）
        cam_row = QHBoxLayout()
        cam_row.addWidget(QLabel("摄像头:"))
        self.cam_combo = QComboBox()  # 摄像头选择下拉框（含深度相机选项，槽位1）
        self.cam_combo.setMinimumWidth(120)  # 最小宽度
        # 连接下拉框变更信号到槽函数
        self.cam_combo.currentIndexChanged.connect(self._on_camera_changed)
        cam_row.addWidget(self.cam_combo, stretch=1)  # 拉伸填充
        group_layout.addLayout(cam_row)

        # 手动重启相机节点（仅1号槽位；运行中拔插后用，无自动重连）
        if self._slot_id == 1:
            self.btn_restart_cam = QPushButton("重启相机")
            self.btn_restart_cam.setToolTip("拔插深度相机后手动重启 ROS2 相机节点\n（约 11 秒，期间预览会中断）")
            self.btn_restart_cam.clicked.connect(self._on_restart_camera_clicked)
            group_layout.addWidget(self.btn_restart_cam)

        # YOLO 参数滑块（仅跑 YOLO 的槽位显示——目前只有槽位1/深度相机）
        if self._yolo_params:
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
        else:
            self.slider_conf = None
            self.slider_iou = None
            self.slider_inf_interval = None

        # 扫码间隔滑块（1-30帧，默认1）——只给不跑 YOLO 的槽位（2/3 槽只扫码）；
        # 槽位1（深度相机只检测，不扫码）不显示
        if not self._yolo_params:
            self.slider_qr_interval = IntParamSlider("扫码间隔", 1, 30, default=1, unit="帧")
            self.slider_qr_interval.setToolTip(
                "每读满 N 帧触发一次解码（单位=帧，不是取帧间隔，1=每次都解）。\n"
                "停带扫码时调小出结果更快；解码瞬间预览会短暂顿一下")
            self.slider_qr_interval.value_changed.connect(
                lambda v: self.param_changed.emit(self._slot_id, "qr_interval", float(v)))
            group_layout.addWidget(self.slider_qr_interval)
        else:
            self.slider_qr_interval = None

        layout.addWidget(group)  # 将分组框添加到主布局

    def _on_camera_changed(self, index: int):
        """
        摄像头选择变更回调（深度/普通统一入口）。

        - 选中「深度相机」(data=-2) → 发 depth_mode_changed(True)
        - 从深度切回普通相机 → 先发 depth_mode_changed(False) 再发 camera_changed
        - 纯普通相机切换 → 只发 camera_changed
        """
        if index < 0:
            return
        data = self.cam_combo.itemData(index)

        if data == DEPTH_TAG:
            # 深度相机选项：进入深度模式（page 端负责起 DepthWorker）
            if not self._depth_active:
                self._depth_active = True
                self.depth_mode_changed.emit(self._slot_id, True)
            return

        # 普通相机（或"请选择"）：若之前是深度 → 先退出深度模式
        if self._depth_active:
            self._depth_active = False
            self.depth_mode_changed.emit(self._slot_id, False)

        if data == -1 or data is None:
            self.camera_changed.emit(self._slot_id, -1)  # "请选择摄像头"
        else:
            self.camera_changed.emit(self._slot_id, int(data))

    def _on_restart_camera_clicked(self):
        """点击「重启相机」按钮"""
        self.restart_camera_clicked.emit(self._slot_id)

    def set_restart_enabled(self, enabled: bool):
        """启用/禁用重启按钮（重启过程中禁用，防重复点击）"""
        self.btn_restart_cam.setEnabled(enabled)

    def set_restart_text(self, text: str):
        """更新重启按钮文字（状态提示）"""
        self.btn_restart_cam.setText(text)

    def is_depth_mode(self) -> bool:
        """检查是否为深度相机模式（下拉选中了深度项）"""
        return self._depth_active

    def set_depth_mode(self, enabled: bool):
        """程序化设置深度相机模式（选中/取消下拉里的深度项，不发信号）"""
        self._depth_active = enabled
        self.cam_combo.blockSignals(True)
        if enabled:
            # 找到深度项选中
            for i in range(self.cam_combo.count()):
                if self.cam_combo.itemData(i) == DEPTH_TAG:
                    self.cam_combo.setCurrentIndex(i)
                    break
        else:
            # 退出深度 → 回到"请选择摄像头"（具体相机由调用方再选）
            self.cam_combo.setCurrentIndex(0)
        self.cam_combo.blockSignals(False)

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
        # 深度相机作为一个选项融合进下拉（仅1号槽位；data=DEPTH_TAG）
        if self._slot_id == 1:
            self.cam_combo.addItem("深度相机 (HP60C)", DEPTH_TAG)
        self.cam_combo.blockSignals(False)  # 恢复信号

    def get_camera_index(self) -> int:
        """获取当前选中的摄像头索引（-1=未选择，DEPTH_TAG=-2=深度相机模式）"""
        if self._depth_active:
            return DEPTH_TAG
        idx = self.cam_combo.currentIndex()
        if idx <= 0:
            return -1  # 默认选项或无效索引
        # 从_camera_indices列表获取真实索引
        if 0 < idx <= len(self._camera_indices):
            return self._camera_indices[idx - 1]
        return -1

    def get_params(self) -> dict:
        """获取当前所有配置参数（YOLO 参数仅 yolo_params 槽位，扫码间隔仅 2/3 槽）"""
        params = {"camera_index": self.get_camera_index()}
        if self._yolo_params:
            params["conf"] = self.slider_conf.get_value()
            params["iou"] = self.slider_iou.get_value()
            params["inference_interval"] = self.slider_inf_interval.get_value()
        else:
            params["qr_interval"] = self.slider_qr_interval.get_value()
        return params

    def set_params(self, params: dict):
        """
        批量设置参数（屏蔽信号防止触发camera_changed）
        用于加载保存的配置或外部设置参数
        """
        # 设置摄像头索引（-2=深度相机，也是下拉里的一个选项）
        if "camera_index" in params:
            cam_idx = params["camera_index"]
            self.cam_combo.blockSignals(True)  # 阻塞信号
            if cam_idx == DEPTH_TAG:
                # 深度相机：选中深度项
                for i in range(self.cam_combo.count()):
                    if self.cam_combo.itemData(i) == DEPTH_TAG:
                        self.cam_combo.setCurrentIndex(i)
                        self._depth_active = True
                        break
            elif cam_idx < 0:
                self.cam_combo.setCurrentIndex(0)  # 选择默认项
            else:
                # 查找对应的索引并设置
                for i, idx in enumerate(self._camera_indices):
                    if idx == cam_idx:
                        self.cam_combo.setCurrentIndex(i + 1)
                        self._depth_active = False
                        break
            self.cam_combo.blockSignals(False)  # 恢复信号
        # 设置各滑块（YOLO 参数只有 yolo_params 槽位有滑块才设）
        if "conf" in params and self.slider_conf is not None:
            self.slider_conf.set_value(params["conf"], emit=False)
        if "iou" in params and self.slider_iou is not None:
            self.slider_iou.set_value(params["iou"], emit=False)
        if "inference_interval" in params and self.slider_inf_interval is not None:
            self.slider_inf_interval.set_value(float(params["inference_interval"]), emit=False)
        if "qr_interval" in params and self.slider_qr_interval is not None:
            self.slider_qr_interval.set_value(float(params["qr_interval"]), emit=False)
