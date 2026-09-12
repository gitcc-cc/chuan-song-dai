"""
产线控制页面
============
三槽位并行检测：
- 左面板：模型路径 + 三套槽位参数
- 顶部工具栏：MQTT状态 + 三套启停/扫码按钮
- 右侧三列：每列预览（右上角FPS）+ 下方检测结果列表
"""
import os
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QGroupBox,
    QPushButton, QLabel, QFileDialog, QSizePolicy, QFrame, QTextEdit
)
from PyQt6.QtCore import Qt, pyqtSlot, pyqtSignal, QThread, QTimer
from PyQt6.QtGui import QFont, QColor, QIcon

try:
    import qtawesome as qta
    _HAS_QTA = True
except ImportError:
    _HAS_QTA = False

from ui.pages.base_page import BasePage
from ui.widgets.camera_view import CameraView
from ui.widgets.detection_list import DetectionList
from ui.widgets.slot_config_panel import SlotConfigPanel
from vision.production_manager import ProductionManager
from vision.camera import BaseCamera
from vision.qr_decoder import match_qr_to_detections
from core.config import get_production_config
from core.esp32_lib import ESP32
from core.esp32_client import get_shared_esp32, connect_shared

# 共享摄像头列表（启动时检测一次，两个页面复用）
_shared_cameras = None


def get_shared_cameras():
    """获取共享的摄像头列表（首次调用时检测）"""
    global _shared_cameras
    if _shared_cameras is None:
        cams = BaseCamera.list_cameras(10)
        _shared_cameras = cams if cams else []
    return _shared_cameras


def reset_shared_cameras():
    """清除共享摄像头缓存，下次调用时重新检测"""
    global _shared_cameras
    _shared_cameras = None


class ModelLoadThread(QThread):
    """后台加载模型，不阻塞 UI"""
    loaded = pyqtSignal(bool, str)  # (success, message)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._path = path

    def run(self):
        try:
            from ultralytics import YOLO
            model = YOLO(self._path)
            names = model.names if hasattr(model, 'names') else {}
            self.loaded.emit(True, f"已加载 {len(names)} 个类别")
        except Exception as e:
            self.loaded.emit(False, str(e))


class ProductionPage(BasePage):
    """产线控制页面"""

    _BTN_STYLE = """
        QPushButton {
            background-color: #f0f0f0;
            border: none;
            border-radius: 6px;
            padding: 4px 10px;
            font-size: 12px;
        }
        QPushButton:hover { background-color: #e0e0e0; }
        QPushButton:checked {
            background-color: #07C160;
            color: white;
        }
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        self.config = get_production_config()
        self._mqtt: Optional[ESP32] = None
        self._manager = ProductionManager()
        self._manager.slot_started.connect(self._on_slot_started)
        self._manager.mqtt_cmd_received.connect(self._on_mqtt_cmd_received)
        self._manager.detection_toggled.connect(self._on_detection_toggled)
        self._manager.qr_toggled.connect(self._on_qr_toggled)
        self._model_load_thread: Optional[ModelLoadThread] = None

        # 每个槽位的 UI 组件引用
        self._slot_views: list[CameraView] = []
        self._slot_lists: list[DetectionList] = []
        self._slot_start_btns: list[QPushButton] = []
        self._slot_qr_btns: list[QPushButton] = []
        self._slot_status_labels: list[QLabel] = []
        self._slot_config_panels: list[SlotConfigPanel] = []
        self._slot_qr_bindings: list[dict] = [{} for _ in range(3)]

        # 日志面板
        self._log_widget: QTextEdit = None

        # MQTT 状态轮询定时器
        self._mqtt_timer = QTimer(self)
        self._mqtt_timer.timeout.connect(self._poll_mqtt_status)
        self._mqtt_timer.start(1000)  # 每秒检查一次

        self._setup_ui()
        self._load_config()

    def _setup_ui(self):
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(6)

        # ============ 左面板 ============
        left_container = QWidget()
        left_container_layout = QHBoxLayout(left_container)
        left_container_layout.setContentsMargins(0, 0, 0, 0)
        left_container_layout.setSpacing(0)

        # 折叠按钮（竖条）
        self._left_toggle_btn = QPushButton("◀")
        self._left_toggle_btn.setFixedWidth(16)
        self._left_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._left_toggle_btn.setStyleSheet("""
            QPushButton {
                background-color: #e0e0e0;
                border: none;
                font-size: 10px;
                color: #666;
            }
            QPushButton:hover { background-color: #ccc; }
        """)
        self._left_toggle_btn.clicked.connect(self._on_toggle_left_panel)
        left_container_layout.addWidget(self._left_toggle_btn)

        self._left_panel = QWidget()
        self._left_panel.setFixedWidth(280)
        self._left_panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        left_layout = QVBoxLayout(self._left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        # 模型选择
        model_group = QGroupBox("模型设置")
        model_layout = QVBoxLayout(model_group)

        model_row = QHBoxLayout()
        self.btn_model = QPushButton("选择模型 (.pt)")
        self.btn_model.clicked.connect(self._on_select_model)
        model_row.addWidget(self.btn_model, stretch=1)
        model_layout.addLayout(model_row)

        self.lbl_model_path = QLabel("未加载模型")
        self.lbl_model_path.setStyleSheet("color: #999; font-size: 11px;")
        self.lbl_model_path.setWordWrap(True)
        model_layout.addWidget(self.lbl_model_path)

        left_layout.addWidget(model_group)

        # 摄像头刷新按钮
        self.btn_refresh_cam = QPushButton("刷新摄像头列表")
        self.btn_refresh_cam.clicked.connect(self._on_refresh_cameras)
        left_layout.addWidget(self.btn_refresh_cam)

        # 三个槽位参数面板
        for i in range(3):
            panel = SlotConfigPanel(slot_id=i + 1)
            panel.camera_changed.connect(self._on_camera_selected)
            panel.param_changed.connect(self._on_param_changed)
            self._slot_config_panels.append(panel)
            left_layout.addWidget(panel)

        left_layout.addStretch()
        left_container_layout.addWidget(self._left_panel, stretch=1)
        main_layout.addWidget(left_container)

        # ============ 右侧：工具栏 + 三列 ============
        right_container = QWidget()
        right_container_layout = QVBoxLayout(right_container)
        right_container_layout.setContentsMargins(0, 0, 0, 0)
        right_container_layout.setSpacing(6)

        # --- 顶部工具栏（仅右侧） ---
        toolbar_layout = QHBoxLayout()
        toolbar_layout.setContentsMargins(8, 4, 8, 4)
        toolbar_layout.setSpacing(12)

        # MQTT 状态
        self._mqtt_status_dot = QLabel("●")
        self._mqtt_status_dot.setFixedSize(16, 16)
        self._mqtt_status_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mqtt_status_dot.setStyleSheet("color: #ccc; font-size: 14px;")
        toolbar_layout.addWidget(self._mqtt_status_dot)

        self._mqtt_status_label = QLabel("MQTT 未连接")
        self._mqtt_status_label.setStyleSheet("font-size: 12px; color: #999;")
        toolbar_layout.addWidget(self._mqtt_status_label)

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #ddd;")
        sep.setFixedHeight(24)
        toolbar_layout.addWidget(sep)

        # 三个槽位的启停 + 扫码按钮
        for i in range(3):
            slot_id = i + 1

            lbl = QLabel(f"相机{slot_id}")
            lbl.setStyleSheet("font-weight: bold; font-size: 12px;")
            toolbar_layout.addWidget(lbl)

            btn_start = QPushButton("检测")
            btn_start.setCheckable(True)
            btn_start.setFixedHeight(30)
            btn_start.setStyleSheet(self._BTN_STYLE)
            btn_start.clicked.connect(lambda checked, s=slot_id: self._on_toggle_slot(s))
            toolbar_layout.addWidget(btn_start)
            self._slot_start_btns.append(btn_start)

            btn_qr = QPushButton("扫码")
            btn_qr.setCheckable(True)
            btn_qr.setChecked(True)
            btn_qr.setFixedHeight(30)
            btn_qr.setStyleSheet(self._BTN_STYLE)
            btn_qr.clicked.connect(lambda checked, s=slot_id: self._on_toggle_qr(s))
            toolbar_layout.addWidget(btn_qr)
            self._slot_qr_btns.append(btn_qr)

            if i < 2:
                sep2 = QFrame()
                sep2.setFrameShape(QFrame.Shape.VLine)
                sep2.setStyleSheet("color: #ddd;")
                sep2.setFixedHeight(24)
                toolbar_layout.addWidget(sep2)

        # XY坐标开关
        self.btn_show_xy = QPushButton("XY")
        self.btn_show_xy.setCheckable(True)
        self.btn_show_xy.setFixedHeight(30)
        self.btn_show_xy.setFixedWidth(40)
        self.btn_show_xy.setStyleSheet(self._BTN_STYLE)
        self.btn_show_xy.setToolTip("显示/隐藏坐标")
        self.btn_show_xy.clicked.connect(self._on_toggle_xy)
        toolbar_layout.addWidget(self.btn_show_xy)

        # 分隔线
        sep3 = QFrame()
        sep3.setFrameShape(QFrame.Shape.VLine)
        sep3.setStyleSheet("color: #ddd;")
        sep3.setFixedHeight(24)
        toolbar_layout.addWidget(sep3)

        # 运行日志（单行）
        self._log_widget = QTextEdit()
        self._log_widget.setReadOnly(True)
        self._log_widget.setFixedHeight(28)
        self._log_widget.setStyleSheet("""
            QTextEdit {
                background-color: transparent;
                color: #000000;
                border: 1px solid #eee;
                font-family: Consolas, monospace;
                font-size: 10px;
                padding: 2px 4px;
            }
        """)
        toolbar_layout.addWidget(self._log_widget, stretch=1)

        # 通讯日志（单行）
        self._com_log_widget = QTextEdit()
        self._com_log_widget.setReadOnly(True)
        self._com_log_widget.setFixedHeight(28)
        self._com_log_widget.setStyleSheet("""
            QTextEdit {
                background-color: transparent;
                color: #000000;
                border: 1px solid #eee;
                font-family: Consolas, monospace;
                font-size: 10px;
                padding: 2px 4px;
            }
        """)
        toolbar_layout.addWidget(self._com_log_widget, stretch=1)

        toolbar_layout.addStretch()
        right_container_layout.addLayout(toolbar_layout)

        # --- 三列预览 + 检测列表 ---
        columns_layout = QHBoxLayout()
        columns_layout.setSpacing(6)

        for i in range(3):
            slot_id = i + 1
            col = self._create_slot_column(slot_id)
            columns_layout.addWidget(col, stretch=1)

        right_container_layout.addLayout(columns_layout, stretch=1)
        main_layout.addWidget(right_container, stretch=1)

    def _create_slot_column(self, slot_id: int) -> QWidget:
        """创建单个槽位的列：预览 + 检测列表"""
        col = QWidget()
        layout = QVBoxLayout(col)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # 标题
        title = QLabel(f"相机{slot_id}")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-weight: bold; font-size: 14px; padding: 2px;")
        layout.addWidget(title)

        # 预览画面
        view = CameraView()
        view.setMinimumSize(300, 200)
        layout.addWidget(view, stretch=3)
        self._slot_views.append(view)

        # 检测列表
        det_list = DetectionList()
        det_list.setMaximumHeight(180)
        layout.addWidget(det_list, stretch=1)
        self._slot_lists.append(det_list)

        return col

    # ============ 模型加载 ============
    def _on_select_model(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 YOLO 模型", "",
            "模型文件 (*.pt);;所有文件 (*)"
        )
        if not path:
            return

        self.btn_model.setEnabled(False)
        self.lbl_model_path.setText(f"加载中: {os.path.basename(path)} ...")

        self._model_load_thread = ModelLoadThread(path)
        self._model_load_thread.loaded.connect(
            lambda ok, msg, p=path: self._on_model_loaded(ok, msg, p))
        self._model_load_thread.start()

    def _on_model_loaded(self, ok: bool, message: str, path: str):
        self.btn_model.setEnabled(True)
        if ok:
            # 把模型注册到 Manager
            if self._manager:
                self._manager.load_model(path)
            self.lbl_model_path.setText(f"已加载: {os.path.basename(path)}")
            self.lbl_model_path.setStyleSheet("color: #07C160; font-size: 11px;")
            self.config.set("model_path", path)
            self.config.save()
        else:
            self.lbl_model_path.setText(f"加载失败: {message}")
            self.lbl_model_path.setStyleSheet("color: #e74c3c; font-size: 11px;")

    # ============ 槽位启停 ============
    def _on_camera_selected(self, slot_id: int, cam_idx: int):
        """摄像头选择后启动预览（不启动检测）"""
        self._log(f"槽位{slot_id}: 选择摄像头 {cam_idx}")
        if cam_idx < 0:
            return
        if not self._manager or not self._manager.model_loaded:
            self._log(f"槽位{slot_id}: 模型未加载，跳过")
            return
        if self._manager.is_running(slot_id):
            self._manager.stop_slot(slot_id)
        params = self._slot_config_panels[slot_id - 1].get_params()
        ok = self._manager.start_slot(slot_id, cam_idx, params)
        btn = self._slot_start_btns[slot_id - 1]
        btn.setChecked(False)
        btn.setText("启动")
        self._log(f"槽位{slot_id}: 预览{'成功' if ok else '失败'}")

    def _on_toggle_slot(self, slot_id: int):
        """启动/停止检测（摄像头已在 on_show 中打开）"""
        idx = slot_id - 1
        btn = self._slot_start_btns[idx]

        if self._manager and self._manager.is_running(slot_id):
            worker = self._manager._workers[idx]
            if worker and worker._inference_enabled:
                self._manager.disable_detection(slot_id)
                self._log(f"槽位{slot_id}: 检测已关闭")
            else:
                if not self._manager.model_loaded:
                    self._log(f"槽位{slot_id}: 请先加载模型")
                    btn.setChecked(False)
                    return
                self._manager.enable_detection(slot_id)
                self._log(f"槽位{slot_id}: 检测已开启")
        else:
            cam_idx = self._slot_config_panels[idx].get_camera_index()
            if cam_idx < 0:
                self._log(f"槽位{slot_id}: 请先选择摄像头")
                btn.setChecked(False)
                return
            if not self._manager or not self._manager.model_loaded:
                self._log(f"槽位{slot_id}: 请先加载模型")
                btn.setChecked(False)
                return
            btn.setText("启动中...")
            btn.setEnabled(False)
            self._log(f"槽位{slot_id}: 正在打开摄像头...")
            params = self._slot_config_panels[idx].get_params()
            ok = self._manager.start_slot(slot_id, cam_idx, params)
            if not ok:
                btn.setText("检测")
                btn.setEnabled(True)
                btn.setChecked(False)

    def _on_toggle_qr(self, slot_id: int):
        idx = slot_id - 1
        btn = self._slot_qr_btns[idx]
        enabled = btn.isChecked()
        if self._manager and self._manager.is_running(slot_id):
            worker = self._manager._workers[idx]
            if worker:
                worker.set_qr_enabled(enabled)

    def _on_toggle_left_panel(self):
        """折叠/展开左侧参数面板"""
        visible = self._left_panel.isVisible()
        self._left_panel.setVisible(not visible)
        self._left_toggle_btn.setText("▶" if visible else "◀")

    def _on_toggle_xy(self):
        """切换所有 CameraView 的坐标显示"""
        show = self.btn_show_xy.isChecked()
        for view in self._slot_views:
            view.set_show_coords(show)

    def _on_param_changed(self, slot_id: int, param_name: str, value: float):
        """参数滑块变化 → 实时更新 Worker"""
        if not self._manager:
            return
        worker = self._manager._workers[slot_id - 1]
        if not worker:
            return
        if param_name == "conf":
            worker.set_params(conf=value)
        elif param_name == "iou":
            worker.set_params(iou=value)
        elif param_name == "inference_interval":
            worker.set_inference_interval(int(value))
        elif param_name == "qr_interval":
            worker.set_qr_interval(int(value))
        # 保存配置
        self.config.set(f"slot_{slot_id}_{param_name}", value)
        self.config.save()

    def _log(self, msg: str):
        """写日志到右上角面板（只显示最新一行，可滚动查看历史）"""
        if self._log_widget:
            self._log_widget.append(msg)
            sb = self._log_widget.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _com_log(self, msg: str):
        """写通讯日志（MQTT 收发）"""
        if self._com_log_widget:
            self._com_log_widget.append(msg)
            sb = self._com_log_widget.verticalScrollBar()
            sb.setValue(sb.maximum())

    # ============ Worker 信号连接（通过 slot_started 信号自动触发） ============

    def _on_detections(self, slot_id: int, detections: list):
        """处理检测结果：更新列表 + 画检测框 + 发 MQTT"""
        idx = slot_id - 1
        qr_bindings = self._slot_qr_bindings[idx]
        # 更新检测列表
        self._slot_lists[idx].update_detections(detections, qr_bindings)
        # 更新 CameraView 检测框
        self._slot_views[idx].update_detections(detections)
        self._slot_views[idx].update_qr_results([])
        # 发送 MQTT
        if self._manager:
            self._manager.send_detection_result(slot_id, detections, qr_bindings)

    def _on_qr_results(self, slot_id: int, qr_results: list):
        """处理二维码结果"""
        idx = slot_id - 1
        # 重新绑定（与当前帧的检测结果匹配）
        detections = []
        worker = self._manager._workers[idx] if self._manager else None
        if worker and hasattr(worker, '_last_detections'):
            detections = worker._last_detections
        self._slot_qr_bindings[idx] = match_qr_to_detections(qr_results, detections)

    def _on_worker_error(self, slot_id: int, error: str):
        print(f"[产线] 槽位{slot_id} 错误: {error}")

    def _on_slot_started(self, slot_id: int, worker):
        """槽位启动成功，连接 Worker 信号 + 更新按钮状态"""
        idx = slot_id - 1
        btn = self._slot_start_btns[idx]
        btn.setText("检测")
        btn.setEnabled(True)
        btn.setChecked(False)

        self._log(f"槽位{slot_id}: 预览已启动")
        view = self._slot_views[idx]
        worker.frame_ready.connect(view.update_frame)
        worker.fps_updated.connect(view.update_fps)
        worker.detections_ready.connect(
            lambda dets, s=slot_id: self._on_detections(s, dets))
        worker.qr_results_ready.connect(
            lambda qrs, s=slot_id: self._on_qr_results(s, qrs))
        worker.error_occurred.connect(
            lambda err, s=slot_id: self._on_worker_error(s, err))

    def _on_mqtt_cmd_received(self, payload: str):
        """MQTT 收到指令 → 写通讯日志"""
        self._com_log(f"← {payload}")

    def _on_detection_toggled(self, slot_id: int, enabled: bool):
        """检测状态变化 → 更新按钮（MQTT 或按钮触发都走这里）"""
        idx = slot_id - 1
        btn = self._slot_start_btns[idx]
        if enabled:
            btn.setChecked(True)
            btn.setText("停止")
        else:
            btn.setChecked(False)
            btn.setText("检测")
            self._slot_lists[idx].clear_all()
            self._slot_views[idx].update_detections([])

    def _on_qr_toggled(self, slot_id: int, enabled: bool):
        """扫码状态变化 → 更新按钮"""
        idx = slot_id - 1
        btn = self._slot_qr_btns[idx]
        btn.setChecked(enabled)

    def _poll_mqtt_status(self):
        """定时检查共享 MQTT 连接状态"""
        shared = get_shared_esp32()
        connected = shared.is_connected()
        # 更新 UI
        if connected:
            self._mqtt_status_dot.setStyleSheet("color: #07C160; font-size: 14px;")
            self._mqtt_status_label.setText("MQTT 已连接")
            self._mqtt_status_label.setStyleSheet("font-size: 12px; color: #07C160;")
        else:
            self._mqtt_status_dot.setStyleSheet("color: #e74c3c; font-size: 14px;")
            self._mqtt_status_label.setText("MQTT 未连接")
            self._mqtt_status_label.setStyleSheet("font-size: 12px; color: #e74c3c;")
            print(f"[产线轮询] 未连接 | id={id(shared)} broker={shared.broker} connected_flag={shared.connected} paho={shared.client.is_connected()}")
        # 同步 manager 的 mqtt 引用（只要连上了就绑定回调）
        if connected and self._manager:
            if self._manager._mqtt is not shared:
                self._manager._mqtt = shared
                shared.set_prod_cmd_callback(self._manager._parse_mqtt_cmd)
                print(f"[产线] 绑定共享 MQTT: {shared.broker}")
        elif connected:
            if self._manager:
                shared.set_prod_cmd_callback(self._manager._parse_mqtt_cmd)

    # ============ MQTT 连接 ============
    def connect_mqtt(self, broker: str = "127.0.0.1", port: int = 1883):
        """连接 MQTT（使用全局共享连接）"""
        self._mqtt = get_shared_esp32()
        if self._mqtt.is_connected():
            self._set_mqtt_status(True)
            self._manager._mqtt = self._mqtt
            self._mqtt.set_prod_cmd_callback(self._manager._parse_mqtt_cmd)
            return True

        ok = connect_shared(broker, port)
        if ok:
            self._mqtt = get_shared_esp32()
            self._set_mqtt_status(True)
            self._manager._mqtt = self._mqtt
            self._mqtt.set_prod_cmd_callback(self._manager._parse_mqtt_cmd)
            model_path = self.config.get("model_path", "")
            if model_path and os.path.exists(model_path) and not self._manager.model_loaded:
                self._manager.load_model(model_path)
                self.lbl_model_path.setText(f"已加载: {os.path.basename(model_path)}")
                self.lbl_model_path.setStyleSheet("color: #07C160; font-size: 11px;")
        else:
            self._set_mqtt_status(False)
        return ok

    def disconnect_mqtt(self):
        """断开 MQTT"""
        if self._mqtt:
            self._mqtt.disconnect()
            self._mqtt = None
        self._set_mqtt_status(False)

    def _set_mqtt_status(self, connected: bool):
        if connected:
            self._mqtt_status_dot.setStyleSheet("color: #07C160; font-size: 14px;")
            self._mqtt_status_label.setText("MQTT 已连接")
            self._mqtt_status_label.setStyleSheet("font-size: 12px; color: #07C160;")
        else:
            self._mqtt_status_dot.setStyleSheet("color: #e74c3c; font-size: 14px;")
            self._mqtt_status_label.setText("MQTT 未连接")
            self._mqtt_status_label.setStyleSheet("font-size: 12px; color: #e74c3c;")

    # ============ 摄像头检测 ============
    def _detect_cameras(self):
        """获取共享的摄像头列表"""
        cams = get_shared_cameras()
        if not cams:
            return [(0, "默认摄像头")]
        return [(d["index"], d["name"]) for d in cams]

    def _on_refresh_cameras(self):
        """刷新摄像头列表"""
        reset_shared_cameras()
        cameras = self._detect_cameras()
        for panel in self._slot_config_panels:
            panel.set_cameras(cameras)

    # ============ 页面生命周期 ============
    def on_show(self):
        """页面显示时：同步MQTT + 加载配置 + 自动打开已保存摄像头预览"""
        # 同步共享 MQTT 连接状态
        shared = get_shared_esp32()
        if shared.is_connected():
            self._mqtt = shared
            self._set_mqtt_status(True)
            if self._manager:
                self._manager._mqtt = shared
                shared.set_prod_cmd_callback(self._manager._parse_mqtt_cmd)

        # 检测摄像头（共享缓存）
        cameras = self._detect_cameras()
        for panel in self._slot_config_panels:
            panel.set_cameras(cameras)

        # 加载已保存的配置
        self._load_config()

        # 自动加载已保存的模型到 Manager
        model_path = self.config.get("model_path", "")
        if model_path and os.path.exists(model_path) and not self._manager.model_loaded:
            ok = self._manager.load_model(model_path)
            if ok:
                self.lbl_model_path.setText(f"已加载: {os.path.basename(model_path)}")
                self.lbl_model_path.setStyleSheet("color: #07C160; font-size: 11px;")

        # 自动打开已保存摄像头并预览（不启动检测）
        for i in range(3):
            slot_id = i + 1
            cam_idx = self._slot_config_panels[i].get_camera_index()
            if cam_idx < 0:
                continue
            if self._manager and self._manager.is_running(slot_id):
                continue
            params = self._slot_config_panels[i].get_params()
            self._log(f"槽位{slot_id}: 自动打开摄像头 {cam_idx}")
            self._manager.start_slot(slot_id, cam_idx, params)

    def on_hide(self):
        """页面隐藏时：关闭所有摄像头"""
        for slot_id in range(1, 4):
            if self._manager and self._manager.is_running(slot_id):
                self._manager.stop_slot(slot_id)
                idx = slot_id - 1
                btn = self._slot_start_btns[idx]
                btn.setChecked(False)
                btn.setText("检测")
                btn.setEnabled(True)
                self._slot_lists[idx].clear_all()
                self._slot_views[idx].update_detections([])

    def save_state(self):
        """保存所有槽位参数"""
        self._save_config()

    # ============ 配置存储 ============
    def _load_config(self):
        """加载产线配置"""
        model_path = self.config.get("model_path", "")
        if model_path:
            self.lbl_model_path.setText(f"已加载: {os.path.basename(model_path)}")
            self.lbl_model_path.setStyleSheet("color: #07C160; font-size: 11px;")

        for i in range(3):
            slot_id = i + 1
            params = self.config.get(f"slot_{slot_id}", {})
            if params:
                self._slot_config_panels[i].set_params(params)

    def _save_config(self):
        """保存产线配置"""
        for i in range(3):
            slot_id = i + 1
            params = self._slot_config_panels[i].get_params()
            self.config.set(f"slot_{slot_id}", params)
        self.config.save()

    # ============ 关闭清理 ============
    def cleanup(self):
        """清理所有资源"""
        if self._manager:
            self._manager.cleanup()
        self.disconnect_mqtt()
