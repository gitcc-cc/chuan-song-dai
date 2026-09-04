"""
相机识别页面
============
三分栏布局：
┌─────────┬───────────────────────┐
│ 左侧控制 │  右上：预览画面 + 工具栏  │
│ 面板    │  ┌──────────────────┐   │
│         │  │  CameraView      │   │
│ 模型选择 │  └──────────────────┘   │
│ 摄像头   ├───────────────────────┤
│ 选择     │  右下：检测结果表格      │
│ 参数滑块 │  (ID, 类别, 置信度, 坐标) │
│         │                         │
└─────────┴───────────────────────┘

工具栏：相机图标按钮（checkable，按下=开始检测）
"""
import os
from typing import List, Dict, Any

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QGroupBox,
    QPushButton, QComboBox, QLabel, QFileDialog, QMessageBox,
    QSizePolicy
)
from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QFont, QIcon

try:
    import qtawesome as qta
    _HAS_QTA = True
except ImportError:
    _HAS_QTA = False

from ui.pages.base_page import BasePage
from ui.widgets import ParamSlider, CameraView, DetectionTable
from vision.camera import OpenCVCamera, BaseCamera
from vision.detector import Detector
from vision.worker import InferenceWorker
from core.config import get_config
from PyQt6.QtCore import QObject, pyqtSignal


class WorkerSignals(QObject):
    """Qt 信号集合"""
    log_message = pyqtSignal(str)


class CameraPage(BasePage):
    """相机识别页面"""

    def __init__(self, parent=None):
        super().__init__(parent)

        # 信号
        self.signals = WorkerSignals()

        # 配置
        self.config = get_config()

        # 核心组件
        self._camera: BaseCamera = OpenCVCamera()
        self._detector = Detector()
        self._worker = InferenceWorker()

        # 状态
        self._running = False
        self._model_loaded = False
        self._det_id_counter = 0

        # 界面
        self._setup_ui()
        self._connect_signals()
        self._load_saved_config()

    def _setup_ui(self):
        """构建三分栏界面"""
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)

        # ============ 左侧控制面板 ============
        left_panel = QWidget()
        left_panel.setFixedWidth(300)
        left_panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)

        # --- 模型选择 ---
        model_group = QGroupBox("模型设置")
        model_layout = QVBoxLayout(model_group)

        model_row = QHBoxLayout()
        self.btn_model = QPushButton("选择模型 (.pt)")
        self.btn_model.clicked.connect(self._on_select_model)
        model_row.addWidget(self.btn_model, stretch=1)
        self.lbl_model_path = QLabel("未加载模型")
        self.lbl_model_path.setStyleSheet("color: #999; font-size: 11px;")
        self.lbl_model_path.setWordWrap(True)
        model_layout.addLayout(model_row)
        model_layout.addWidget(self.lbl_model_path)

        left_layout.addWidget(model_group)

        # --- 摄像头选择 ---
        cam_group = QGroupBox("摄像头设置")
        cam_layout = QVBoxLayout(cam_group)

        self.cam_combo = QComboBox()
        self.cam_combo.addItem("正在检测...", -1)
        self.cam_combo.currentIndexChanged.connect(self._on_camera_changed)
        cam_layout.addWidget(QLabel("选择摄像头:"))
        cam_layout.addWidget(self.cam_combo)

        self.btn_refresh_cam = QPushButton("刷新列表")
        self.btn_refresh_cam.clicked.connect(self._refresh_cameras)
        cam_layout.addWidget(self.btn_refresh_cam)

        left_layout.addWidget(cam_group)

        # --- 推理参数 ---
        param_group = QGroupBox("推理参数")
        param_layout = QVBoxLayout(param_group)

        self.slider_conf = ParamSlider("置信度", 0.0, 1.0,
                                       default=0.5, step=0.01, decimals=2)
        self.slider_conf.value_changed.connect(self._on_param_changed)
        param_layout.addWidget(self.slider_conf)

        self.slider_iou = ParamSlider("IoU 阈值", 0.0, 1.0,
                                      default=0.45, step=0.01, decimals=2)
        self.slider_iou.value_changed.connect(self._on_param_changed)
        param_layout.addWidget(self.slider_iou)

        left_layout.addWidget(param_group)

        # --- 未来扩展：类别→指令配置 ---
        future_group = QGroupBox("类别触发配置 (预留)")
        future_layout = QVBoxLayout(future_group)
        lbl = QLabel("此处后续添加：\n检测到 [类别] → 发送 [指令]")
        lbl.setStyleSheet("color: #999; font-size: 12px;")
        lbl.setWordWrap(True)
        future_layout.addWidget(lbl)
        left_layout.addWidget(future_group)

        left_layout.addStretch()
        main_layout.addWidget(left_panel)

        # ============ 右侧：上下分割 ============
        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.setHandleWidth(6)
        right_splitter.setStyleSheet("""
            QSplitter::handle { background-color: #e0e0e0; height: 3px; }
            QSplitter::handle:hover { background-color: #07C160; }
        """)

        # ---- 右上：预览画面 + 工具栏 ----
        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(4)

        # 工具栏
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)

        # 相机按钮（开始/停止检测）
        self.btn_camera = QPushButton()
        self.btn_camera.setCheckable(True)
        self.btn_camera.setFixedSize(40, 40)
        self.btn_camera.setStyleSheet("""
            QPushButton {
                background-color: #f0f0f0;
                border: none;
                border-radius: 6px;
            }
            QPushButton:hover { background-color: #e0e0e0; }
            QPushButton:checked {
                background-color: #07C160;
                border: 2px solid #059648;
            }
            QPushButton:checked:hover { background-color: #06AD56; }
        """)
        self._update_camera_btn_icon(False)
        self.btn_camera.toggled.connect(self._on_camera_toggled)
        toolbar.addWidget(self.btn_camera)

        toolbar.addStretch()

        # FPS 显示
        self.lbl_fps = QLabel("FPS: 0.0")
        self.lbl_fps.setFont(QFont("Microsoft YaHei", 11, QFont.Weight.Bold))
        self.lbl_fps.setStyleSheet("color: #07C160;")
        toolbar.addWidget(self.lbl_fps)

        toolbar.addSpacing(10)

        # 分辨率显示
        self.lbl_res = QLabel("--x--")
        self.lbl_res.setStyleSheet("color: #999; font-size: 11px;")
        toolbar.addWidget(self.lbl_res)

        # 状态消息
        self.lbl_status = QLabel("就绪")
        self.lbl_status.setStyleSheet("color: #333; font-size: 11px;")
        self.lbl_status.setMinimumWidth(200)
        toolbar.addWidget(self.lbl_status)

        top_layout.addLayout(toolbar)

        # 预览画面
        self.camera_view = CameraView()
        top_layout.addWidget(self.camera_view, stretch=1)

        right_splitter.addWidget(top_widget)

        # ---- 右下：检测结果表格 ----
        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(0, 0, 0, 0)

        table_label = QLabel("检测结果列表 (右键可复制)")
        table_label.setFont(QFont("Microsoft YaHei", 11, QFont.Weight.Bold))
        table_label.setStyleSheet("color: #333; padding-bottom: 4px;")
        bottom_layout.addWidget(table_label)

        self.detection_table = DetectionTable()
        bottom_layout.addWidget(self.detection_table, stretch=1)

        right_splitter.addWidget(bottom_widget)

        # 默认比例：上 70%，下 30%
        right_splitter.setSizes([550, 250])

        main_layout.addWidget(right_splitter, stretch=1)

        # 绑定 worker
        self._worker.set_camera(self._camera)
        self._worker.set_detector(self._detector)

    def _connect_signals(self):
        """连接 worker 信号"""
        self._worker.frame_ready.connect(self.camera_view.update_frame)
        self._worker.detections_ready.connect(self._on_detections_ready)
        self._worker.fps_updated.connect(self._on_fps_updated)
        self._worker.error_occurred.connect(self._on_worker_error)

    def _load_saved_config(self):
        """加载保存的配置（启动时检测摄像头并打开）"""
        # 模型路径
        model_path = self.config.get("model_path", "")
        if model_path and os.path.exists(model_path):
            self._load_model(model_path)

        # 参数
        self.slider_conf.set_value(self.config.get("conf_threshold", 0.5), emit=False)
        self.slider_iou.set_value(self.config.get("iou_threshold", 0.45), emit=False)

        # 启动时检测摄像头并打开
        self._detect_and_open()

    # ============ 事件处理 ============
    def _on_select_model(self):
        """选择模型文件"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 YOLO 模型",
            self.config.get("last_model_dir", os.path.expanduser("~")),
            "PyTorch Model (*.pt);;All Files (*)"
        )
        if path:
            self.config.set("last_model_dir", os.path.dirname(path))
            self._load_model(path)

    def _load_model(self, path: str):
        """加载模型"""
        self._set_status(f"正在加载模型: {os.path.basename(path)}")
        try:
            self._detector.load(path)
            self._model_loaded = True
            self.lbl_model_path.setText(f"已加载: {os.path.basename(path)}")
            self.lbl_model_path.setStyleSheet("color: #07C160; font-size: 11px;")
            self.config.set("model_path", path)
            self.config.save()
            self.signals.log_message.emit(f"模型加载成功: {path}")
            self._set_status(f"模型加载成功: {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.critical(self, "加载失败", f"模型加载失败:\n{e}")
            self.lbl_model_path.setText("加载失败")
            self.lbl_model_path.setStyleSheet("color: #fa5151; font-size: 11px;")
            self._model_loaded = False
            self._set_status("模型加载失败")

    def _refresh_cameras(self):
        """手动刷新摄像头列表"""
        self._detect_and_open()

    def _on_camera_changed(self, index: int):
        """切换摄像头"""
        cam_idx = self.cam_combo.itemData(index)
        if cam_idx is not None and cam_idx >= 0:
            self._set_status(f"正在切换到摄像头 {cam_idx}...")
            self._open_camera(cam_idx)

    def _open_camera(self, index: int):
        """打开指定摄像头"""
        # 先停止推理
        was_running = self._running
        if was_running:
            self._worker.pause()

        # 关闭旧摄像头
        self._camera.close()

        # 打开新摄像头（带后端自动回退），使用摄像头原生分辨率
        ok = self._camera.open(index, width=-1, height=-1, fps=-1)
        if not ok:
            # 如果指定索引失败，尝试列表中的第一个可用摄像头
            cams = BaseCamera.list_cameras(5)
            if cams:
                self._set_status(f"摄像头 {index} 打开失败，尝试摄像头 {cams[0]}...")
                ok = self._camera.open(cams[0], width=-1, height=-1, fps=-1)
                if ok:
                    index = cams[0]
                    # 更新下拉框
                    for i in range(self.cam_combo.count()):
                        if self.cam_combo.itemData(i) == index:
                            self.cam_combo.setCurrentIndex(i)
                            break

        if not ok:
            QMessageBox.warning(self, "打开失败", f"无法打开任何摄像头")
            self.cam_combo.setCurrentIndex(0)
            self._set_status("摄像头打开失败")
            return

        # 更新分辨率显示
        w, h = self._camera.get_resolution()
        self.lbl_res.setText(f"{w}x{h}")

        # 保存配置
        self.config.set("camera_index", index)
        self.config.save()

        # 启动 worker 预览（持续发帧，不做推理）
        print(f"[CameraPage] 打开摄像头 {index} ({w}x{h})")
        self._set_status(f"摄像头 {index} 已连接 ({w}x{h})")
        self._worker.start_preview()

        # 恢复推理状态
        if was_running:
            self._worker.enable_inference()
        else:
            self._worker.disable_inference()

    def _on_param_changed(self, value: float):
        """参数变化 -> 更新 worker"""
        self._worker.set_params(
            conf=self.slider_conf.get_value(),
            iou=self.slider_iou.get_value()
        )
        # 保存配置
        self.config.set("conf_threshold", self.slider_conf.get_value())
        self.config.set("iou_threshold", self.slider_iou.get_value())
        self.config.save()

    def _on_camera_toggled(self, checked: bool):
        """开始/停止检测"""
        if checked:
            if not self._model_loaded:
                QMessageBox.warning(self, "未加载模型", "请先选择 .pt 模型文件")
                self.btn_camera.blockSignals(True)
                self.btn_camera.setChecked(False)
                self.btn_camera.blockSignals(False)
                return
            if not self._camera.is_opened():
                QMessageBox.warning(self, "摄像头未打开", "请先选择摄像头")
                self.btn_camera.blockSignals(True)
                self.btn_camera.setChecked(False)
                self.btn_camera.blockSignals(False)
                return

            self._start_inference()
        else:
            self._stop_inference()

        self._update_camera_btn_icon(checked)

    def _start_inference(self):
        """开始推理"""
        self._det_id_counter = 0
        self.detection_table.clear_all()
        self._worker.set_params(
            conf=self.slider_conf.get_value(),
            iou=self.slider_iou.get_value()
        )
        self._worker.enable_inference()
        self._running = True
        self._set_status("正在检测...")

    def _stop_inference(self):
        """停止推理"""
        self._worker.disable_inference()
        self._running = False
        self._set_status("预览中")

    def _update_camera_btn_icon(self, checked: bool):
        """更新相机按钮图标"""
        if _HAS_QTA:
            icon_name = "fa5s.video-slash" if checked else "fa5s.video"
            color = "#ffffff" if checked else "#333333"
            icon = qta.icon(icon_name, color=color, scale_factor=1.2)
            self.btn_camera.setIcon(icon)
            self.btn_camera.setIconSize(self.btn_camera.size() * 0.6)
        else:
            self.btn_camera.setText("■" if checked else "▶")

    @pyqtSlot(list)
    def _on_detections_ready(self, detections: List[Dict[str, Any]]):
        """收到检测结果"""
        self.camera_view.update_detections(detections)

        # 更新表格
        if detections:
            self._det_id_counter += 1
            self.detection_table.add_detections(detections, self._det_id_counter)

    @pyqtSlot(float)
    def _on_fps_updated(self, fps: float):
        self.lbl_fps.setText(f"FPS: {fps:.1f}")
        self.camera_view.update_fps(fps)

    @pyqtSlot(str)
    def _on_worker_error(self, msg: str):
        print(f"[CameraPage] 错误: {msg}")
        self._set_status(f"错误: {msg}")
        self.signals.log_message.emit(f"错误: {msg}")
        self._stop_inference()
        self.btn_camera.blockSignals(True)
        self.btn_camera.setChecked(False)
        self.btn_camera.blockSignals(False)
        self._update_camera_btn_icon(False)

    def _set_status(self, msg: str):
        """更新工具栏状态栏并打印终端日志"""
        print(f"[CameraPage] {msg}")
        self.lbl_status.setText(msg)

    # ============ BasePage 生命周期 ============
    def on_show(self):
        """页面显示：不重新检测，直接用已打开的摄像头"""
        self._set_status("就绪")

    def on_hide(self):
        """页面隐藏：停止推理，但保留摄像头和 worker（快速恢复）"""
        self._stop_inference()

    def _detect_and_open(self):
        """检测摄像头并打开（启动时或手动刷新时调用）"""
        self._set_status("正在检测摄像头...")
        self.cam_combo.blockSignals(True)
        self.cam_combo.clear()

        saved_idx = self.config.get("camera_index", 0)
        cams = BaseCamera.list_cameras(5)
        if not cams:
            self.cam_combo.addItem("未检测到摄像头", -1)
            self._set_status("未检测到可用摄像头，请检查连接")
            self.cam_combo.blockSignals(False)
            return

        for idx in cams:
            self.cam_combo.addItem(f"摄像头 {idx}", idx)
        self._set_status(f"检测到 {len(cams)} 个摄像头: {cams}")

        # 自动选中保存的索引（或第一个）
        target = saved_idx if saved_idx in cams else cams[0]
        for i in range(self.cam_combo.count()):
            if self.cam_combo.itemData(i) == target:
                self.cam_combo.setCurrentIndex(i)
                break
        self.cam_combo.blockSignals(False)

        # 打开摄像头 + 启动 worker
        self._open_camera(target)

    def save_state(self):
        """保存状态"""
        self.config.set("conf_threshold", self.slider_conf.get_value())
        self.config.set("iou_threshold", self.slider_iou.get_value())
        self.config.save()