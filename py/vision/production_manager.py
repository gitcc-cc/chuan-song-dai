"""
产线管理器
==========
- 模型单例：一个 YOLO 实例，三个 Worker 共享
- 三个 Worker 独立运行，支持独立启停
- 1号槽位支持深度相机模式（HP60C ROS2）
- MQTT 指令解析：ht/cmd/2 → xj1/xj2/xj3/all
- 检测结果发送：ht/status/2
"""
import json
import os
from typing import Optional, Callable

from PyQt6.QtCore import QObject, pyqtSignal, QThread

from vision.detector import Detector
from vision.worker import InferenceWorker
from vision.depth_worker import DepthWorker
from vision.camera import OpenCVCamera, BaseCamera
from core.esp32_lib import ESP32


class SlotCameraOpenThread(QThread):
    """后台线程打开摄像头，不阻塞 UI"""
    opened = pyqtSignal(int, object)  # (slot_id, camera)
    failed = pyqtSignal(int, str)     # (slot_id, error)

    def __init__(self, slot_id: int, camera: OpenCVCamera, camera_index: int, parent=None):
        super().__init__(parent)
        self._slot_id = slot_id
        self._camera = camera
        self._camera_index = camera_index

    def run(self):
        ok = self._camera.open(self._camera_index)
        if ok:
            self.opened.emit(self._slot_id, self._camera)
        else:
            self.failed.emit(self._slot_id, f"摄像头 {self._camera_index} 打开失败")


class ProductionManager(QObject):
    """产线管理器：模型单例 + 3 Worker + MQTT"""

    slot_started = pyqtSignal(int, object)  # (slot_id, worker)
    mqtt_cmd_received = pyqtSignal(str)     # 收到的 MQTT 指令原文
    detection_toggled = pyqtSignal(int, bool)  # (slot_id, enabled)
    qr_toggled = pyqtSignal(int, bool)         # (slot_id, enabled)

    SLOT_COUNT = 3
    SLOT_MAP = {"xj1": 0, "xj2": 1, "xj3": 2}

    def __init__(self, mqtt_client: Optional[ESP32] = None):
        super().__init__()
        self._mqtt: Optional[ESP32] = mqtt_client
        self._model = None          # YOLO model 实例（共享）
        self._model_loaded = False
        self._model_path = ""

        self._workers: list[InferenceWorker] = [None] * self.SLOT_COUNT
        self._depth_workers: list[Optional[DepthWorker]] = [None] * self.SLOT_COUNT
        self._cameras: list[Optional[BaseCamera]] = [None] * self.SLOT_COUNT
        self._detectors: list[Optional[Detector]] = [None] * self.SLOT_COUNT
        self._running = [False] * self.SLOT_COUNT
        self._depth_mode = [False] * self.SLOT_COUNT
        self._pending_params: list[dict] = [{}] * self.SLOT_COUNT
        self._open_threads: list = [None] * self.SLOT_COUNT

        # 回调
        self.on_detections: Optional[Callable] = None  # (slot_id, detections, qr_bindings)

        # 注册 MQTT 指令回调
        if self._mqtt:
            self._mqtt.set_prod_cmd_callback(self._parse_mqtt_cmd)

    # ============ 模型管理 ============
    def load_model(self, model_path: str) -> bool:
        """
        加载 YOLO 模型（单例）
        三个槽位共享同一个 model 实例
        """
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"模型文件不存在: {model_path}")

        try:
            from ultralytics import YOLO
            self._model = YOLO(model_path)
            self._model_path = model_path
            self._model_loaded = True

            # 更新已有的 detector
            for det in self._detectors:
                if det is not None:
                    det.set_model(self._model, self._model.names if hasattr(self._model, 'names') else {})
            return True
        except Exception as e:
            print(f"[ProductionManager] 模型加载失败: {e}")
            return False

    @property
    def model_loaded(self) -> bool:
        return self._model_loaded

    @property
    def model_path(self) -> str:
        return self._model_path

    # ============ 槽位管理 ============
    def start_slot(self, slot_id: int, camera_index: int, params: dict) -> bool:
        """
        启动指定槽位（异步打开摄像头，不阻塞 UI）
        slot_id: 1/2/3
        camera_index: 摄像头索引
        params: {"conf": 0.5, "iou": 0.45, "inference_interval": 1, "qr_interval": 1}
        """
        idx = slot_id - 1
        if idx < 0 or idx >= self.SLOT_COUNT:
            return False

        if self._running[idx]:
            self.stop_slot(slot_id)

        if not self._model_loaded:
            print(f"[ProductionManager] 槽位{slot_id}: 模型未加载")
            return False

        # 保存参数，等摄像头打开后使用
        self._pending_params[idx] = params

        # 异步打开摄像头
        camera = OpenCVCamera()
        thread = SlotCameraOpenThread(slot_id, camera, camera_index)
        thread.opened.connect(self._on_slot_camera_opened)
        thread.failed.connect(self._on_slot_camera_failed)
        self._open_threads[idx] = thread
        thread.start()
        print(f"[ProductionManager] 槽位{slot_id}: 正在打开摄像头 {camera_index}...")
        return True

    def _on_slot_camera_opened(self, slot_id: int, camera):
        """摄像头打开成功，创建 Worker 并启动预览"""
        idx = slot_id - 1
        params = self._pending_params[idx]
        self._cameras[idx] = camera

        # 创建 Detector（共享 model）
        detector = Detector(external_model=self._model)
        self._detectors[idx] = detector

        # 创建 Worker（仅预览，不推理）
        worker = InferenceWorker()
        worker.set_camera(camera)
        worker.set_detector(detector)
        worker.set_params(
            conf=params.get("conf", 0.5),
            iou=params.get("iou", 0.45)
        )
        worker.set_inference_interval(params.get("inference_interval", 1))
        worker.set_qr_interval(params.get("qr_interval", 1))
        worker.set_qr_enabled(False)
        worker.disable_inference()

        # 连接信号
        worker.detections_ready.connect(
            lambda dets, s=slot_id: self._on_slot_detections(s, dets))
        worker.qr_results_ready.connect(
            lambda qrs, s=slot_id: self._on_slot_qr(s, qrs))

        self._workers[idx] = worker
        self._running[idx] = True
        worker.start_preview()
        self.slot_started.emit(slot_id, worker)
        print(f"[ProductionManager] 槽位{slot_id}: 预览已启动")

    def _on_slot_camera_failed(self, slot_id: int, error: str):
        """摄像头打开失败"""
        print(f"[ProductionManager] 槽位{slot_id}: {error}")

    def start_slot_depth(self, slot_id: int, params: dict) -> bool:
        """
        以深度相机模式启动指定槽位（仅1号槽位）
        不需要 OpenCVCamera，ROS2 订阅直接获取图像。
        params: {"conf": 0.5, "iou": 0.45, "inference_interval": 1, "qr_interval": 1}
        """
        idx = slot_id - 1
        if idx != 0:
            print(f"[ProductionManager] 深度模式仅支持槽位1")
            return False

        if self._running[idx]:
            self.stop_slot(slot_id)

        if not self._model_loaded:
            print(f"[ProductionManager] 槽位{slot_id}: 模型未加载")
            return False

        # 创建 Detector（共享 model）
        detector = Detector(external_model=self._model)
        self._detectors[idx] = detector

        # 创建 DepthWorker
        worker = DepthWorker()
        worker.set_detector(detector)
        worker.set_params(
            conf=params.get("conf", 0.5),
            iou=params.get("iou", 0.45)
        )
        worker.set_inference_interval(params.get("inference_interval", 1))
        worker.set_qr_interval(params.get("qr_interval", 1))
        worker.set_qr_enabled(False)
        worker.disable_inference()

        # 连接信号
        worker.detections_ready.connect(
            lambda dets, s=slot_id: self._on_slot_detections(s, dets))
        worker.qr_results_ready.connect(
            lambda qrs, s=slot_id: self._on_slot_qr(s, qrs))

        self._depth_workers[idx] = worker
        self._depth_mode[idx] = True
        self._running[idx] = True
        worker.start_worker()
        self.slot_started.emit(slot_id, worker)
        print(f"[ProductionManager] 槽位{slot_id}: 深度相机模式已启动")
        return True

    def enable_detection(self, slot_id: int):
        """开启指定槽位的检测（YOLO + 扫码）"""
        idx = slot_id - 1
        if 0 <= idx < self.SLOT_COUNT:
            if self._depth_mode[idx] and self._depth_workers[idx]:
                self._depth_workers[idx].enable_inference()
                self._depth_workers[idx].set_qr_enabled(True)
            elif self._workers[idx]:
                self._workers[idx].enable_inference()
                self._workers[idx].set_qr_enabled(True)
            self.detection_toggled.emit(slot_id, True)
            print(f"[ProductionManager] 槽位{slot_id}: 检测已开启")

    def disable_detection(self, slot_id: int):
        """关闭指定槽位的检测（预览继续）"""
        idx = slot_id - 1
        if 0 <= idx < self.SLOT_COUNT:
            if self._depth_mode[idx] and self._depth_workers[idx]:
                self._depth_workers[idx].disable_inference()
                self._depth_workers[idx].set_qr_enabled(False)
            elif self._workers[idx]:
                self._workers[idx].disable_inference()
                self._workers[idx].set_qr_enabled(False)
            self.detection_toggled.emit(slot_id, False)
            print(f"[ProductionManager] 槽位{slot_id}: 检测已关闭")

    def stop_slot(self, slot_id: int):
        """停止指定槽位"""
        import traceback
        idx = slot_id - 1
        if idx < 0 or idx >= self.SLOT_COUNT:
            return

        print(f"[ProductionManager] 槽位{slot_id} 停止")
        traceback.print_stack(limit=5)

        # 停止普通 Worker
        if self._workers[idx]:
            self._workers[idx].stop()
            self._workers[idx] = None

        # 停止深度 Worker
        if self._depth_workers[idx]:
            self._depth_workers[idx].stop()
            self._depth_workers[idx] = None

        if self._cameras[idx]:
            self._cameras[idx].close()
            self._cameras[idx] = None

        self._detectors[idx] = None
        self._running[idx] = False
        self._depth_mode[idx] = False
        print(f"[ProductionManager] 槽位 {slot_id} 停止")

    def stop_all(self):
        """停止所有槽位"""
        for i in range(1, self.SLOT_COUNT + 1):
            self.stop_slot(i)

    def is_running(self, slot_id: int) -> bool:
        idx = slot_id - 1
        if 0 <= idx < self.SLOT_COUNT:
            return self._running[idx]
        return False

    def update_slot_params(self, slot_id: int, params: dict):
        """更新运行中槽位的参数"""
        idx = slot_id - 1
        if idx < 0 or idx >= self.SLOT_COUNT:
            return

        # 普通 Worker
        worker = self._workers[idx]
        if worker and self._running[idx]:
            if "conf" in params:
                worker.set_params(conf=params["conf"])
            if "iou" in params:
                worker.set_params(iou=params["iou"])
            if "inference_interval" in params:
                worker.set_inference_interval(int(params["inference_interval"]))
            if "qr_interval" in params:
                worker.set_qr_interval(int(params["qr_interval"]))

        # 深度 Worker
        depth_worker = self._depth_workers[idx]
        if depth_worker and self._running[idx]:
            if "conf" in params:
                depth_worker.set_params(conf=params["conf"])
            if "iou" in params:
                depth_worker.set_params(iou=params["iou"])
            if "inference_interval" in params:
                depth_worker.set_inference_interval(int(params["inference_interval"]))
            if "qr_interval" in params:
                depth_worker.set_qr_interval(int(params["qr_interval"]))

    # ============ 检测结果处理 ============
    def _on_slot_detections(self, slot_id: int, detections: list):
        """某槽位检测结果（仅用于通知上层，实际MQTT发送在帧级）"""
        pass

    def _on_slot_qr(self, slot_id: int, qr_results: list):
        """某槽位二维码结果"""
        pass

    def send_detection_result(self, slot_id: int, detections: list, qr_bindings: dict):
        """
        发送检测结果到 MQTT ht/status/2
        格式: {"slot_id": 1, "detections": [{"cls":"纸盒","center":[200,150],"qr":"xxx"}]}
        最多发送10个
        """
        if not self._mqtt or not self._mqtt.is_connected():
            return

        # 按置信度排序，最多10个
        sorted_dets = sorted(detections, key=lambda d: d.get("conf", 0), reverse=True)[:10]

        result_list = []
        for i, det in enumerate(sorted_dets):
            bbox = det.get("bbox", [0, 0, 0, 0])
            cx = int((bbox[0] + bbox[2]) / 2)
            cy = int((bbox[1] + bbox[3]) / 2)
            cls_name = det.get("cls_name", str(det.get("cls", "")))
            qr = qr_bindings.get(i, "")
            depth = det.get("depth_mm", -1)
            item = {"cls": cls_name, "center": [cx, cy], "qr": qr}
            if depth > 0:
                item["depth_mm"] = depth
            result_list.append(item)

        if result_list:
            self._mqtt.send_prod_result(slot_id, result_list)

    # ============ MQTT 指令解析 ============
    def _parse_mqtt_cmd(self, payload: str):
        """
        解析 ht/cmd/2 指令
        格式: xj1 1 0 / xj2 0 1 / all 1 1
        三段：槽位 检测开关 扫码开关
        """
        self.mqtt_cmd_received.emit(payload)
        parts = payload.strip().split()
        if len(parts) != 3:
            print(f"[ProductionManager] 指令格式错误: {payload}")
            return

        slot_cmd, detect_str, qr_str = parts

        try:
            detect_on = int(detect_str)
            qr_on = int(qr_str)
        except ValueError:
            print(f"[ProductionManager] 参数格式错误: {payload}")
            return

        if detect_on not in (0, 1) or qr_on not in (0, 1):
            print(f"[ProductionManager] 参数值无效: {payload}")
            return

        # 确定目标槽位
        if slot_cmd == "all":
            slots = [1, 2, 3]
        elif slot_cmd in self.SLOT_MAP:
            slots = [self.SLOT_MAP[slot_cmd] + 1]
        else:
            print(f"[ProductionManager] 未知槽位: {slot_cmd}")
            return

        for slot_id in slots:
            if detect_on:
                self.enable_detection(slot_id)
            else:
                self.disable_detection(slot_id)
            # 扫码独立控制
            idx = slot_id - 1
            # 普通 Worker
            worker = self._workers[idx]
            if worker and self._running[idx]:
                worker.set_qr_enabled(bool(qr_on))
                self.qr_toggled.emit(slot_id, bool(qr_on))
            # 深度 Worker
            depth_worker = self._depth_workers[idx]
            if depth_worker and self._running[idx]:
                depth_worker.set_qr_enabled(bool(qr_on))
                self.qr_toggled.emit(slot_id, bool(qr_on))

    # ============ 清理 ============
    def cleanup(self):
        """清理所有资源"""
        self.stop_all()
        self._model = None
        self._model_loaded = False
        self._depth_workers = [None] * self.SLOT_COUNT
