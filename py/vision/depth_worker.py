"""
深度相机工作线程
===============
QThread + ROS2 rclpy.spin()：
- 订阅 HP60C 彩色图 + 深度图
- YOLO 检测 + 二维码解码
- 读取检测框中心深度值
- 通过 Qt 信号传给主线程
"""
import time
from typing import List, Dict, Any, Optional

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal, QMutex

from vision.detector import Detector
from vision.qr_decoder import QRDecoder


class DepthWorker(QThread):
    """深度相机工作线程：ROS2 订阅 + YOLO + 深度值"""

    # 信号
    frame_ready = pyqtSignal(np.ndarray)              # RGB BGR 帧
    depth_frame_ready = pyqtSignal(np.ndarray)        # 深度帧（16UC1 mm）
    detections_ready = pyqtSignal(list)               # YOLO 检测结果（含 depth_mm）
    qr_results_ready = pyqtSignal(list)               # 二维码解码结果
    fps_updated = pyqtSignal(float)                   # 实时 FPS
    error_occurred = pyqtSignal(str)                  # 错误信息

    RGB_TOPIC = "/ascamera_hp60c/camera_publisher/rgb0/image"
    DEPTH_TOPIC = "/ascamera_hp60c/camera_publisher/depth0/image_raw"

    def __init__(self, parent=None):
        super().__init__(parent)

        self._detector: Optional[Detector] = None
        self._qr_decoder = QRDecoder()

        # ROS2 节点和订阅
        self._node = None
        self._rgb_sub = None
        self._depth_sub = None
        self._bridge = None

        # 最新帧缓存（线程安全）
        self._rgb_frame = None
        self._depth_frame = None
        self._frame_lock = QMutex()

        # 推理参数
        self._conf_threshold = 0.5
        self._iou_threshold = 0.45
        self._inference_enabled = False
        self._qr_enabled = False
        self._inference_interval = 1
        self._qr_interval = 1
        self._frame_count = 0
        self._last_detections = []  # 最近一次检测结果（供 QR 匹配用）

        # 运行控制
        self._running = False
        self._mutex = QMutex()

        # FPS
        self._frame_times = []
        self._last_fps_emit = 0

    # ============ 配置 ============
    def set_detector(self, detector: Detector):
        self._detector = detector

    def set_params(self, conf: float = None, iou: float = None):
        if conf is not None:
            self._conf_threshold = conf
        if iou is not None:
            self._iou_threshold = iou

    def set_inference_interval(self, interval: int):
        self._mutex.lock()
        self._inference_interval = max(1, interval)
        self._mutex.unlock()

    def set_qr_enabled(self, enabled: bool):
        self._mutex.lock()
        self._qr_enabled = enabled
        self._mutex.unlock()

    def set_qr_interval(self, interval: int):
        self._mutex.lock()
        self._qr_interval = max(1, interval)
        self._mutex.unlock()

    def enable_inference(self):
        self._mutex.lock()
        self._inference_enabled = True
        self._mutex.unlock()

    def disable_inference(self):
        self._mutex.lock()
        self._inference_enabled = False
        self._mutex.unlock()

    # ============ ROS2 回调 ============
    def _rgb_callback(self, msg):
        try:
            frame = self._bridge.imgmsg_to_cv2(msg, "bgr8")
            self._frame_lock.lock()
            self._rgb_frame = frame
            self._frame_lock.unlock()
        except Exception:
            pass

    def _depth_callback(self, msg):
        try:
            depth = self._bridge.imgmsg_to_cv2(msg, "passthrough")
            self._frame_lock.lock()
            self._depth_frame = depth
            self._frame_lock.unlock()
        except Exception:
            pass

    # ============ 深度值读取 ============
    @staticmethod
    def read_depth_center(depth_frame: np.ndarray, bbox: list) -> int:
        """
        读取检测框中心点的深度值（毫米）
        返回: 深度值 mm，无效返回 -1
        """
        if depth_frame is None or len(bbox) != 4:
            return -1
        x1, y1, x2, y2 = [int(v) for v in bbox]
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        h, w = depth_frame.shape[:2]
        if not (0 <= cy < h and 0 <= cx < w):
            return -1
        raw = depth_frame[cy, cx]
        if depth_frame.dtype == np.uint16:
            return int(raw) if raw > 0 else -1
        else:
            mm = int(raw * 1000)
            return mm if mm > 0 else -1

    # ============ 线程控制 ============
    def start_worker(self):
        """启动线程"""
        if not self._running:
            self._running = True
            self._frame_count = 0
            self.start()

    def stop(self):
        """停止线程并释放资源"""
        self._mutex.lock()
        self._running = False
        self._mutex.unlock()
        self.wait(3000)

        # 销毁 ROS2 节点
        if self._node:
            self._node.destroy_node()
            self._node = None

    # ============ 主循环 ============
    def run(self):
        """线程主循环：初始化 ROS2 → spin → 处理帧"""
        try:
            import rclpy
            from rclpy.node import Node
            from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
            from sensor_msgs.msg import Image
            from cv_bridge import CvBridge
        except ImportError as e:
            self.error_occurred.emit(f"ROS2 依赖缺失: {e}")
            return

        # 初始化 ROS2
        try:
            if not rclpy.ok():
                rclpy.init()
        except Exception:
            pass  # 可能已初始化

        self._node = Node("depth_worker_node")
        self._bridge = CvBridge()

        qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )

        self._rgb_sub = self._node.create_subscription(
            Image, self.RGB_TOPIC, self._rgb_callback, qos
        )
        self._depth_sub = self._node.create_subscription(
            Image, self.DEPTH_TOPIC, self._depth_callback, qos
        )

        print("[DepthWorker] ROS2 节点已启动，等待图像...")
        self._node.get_logger().info("DepthWorker 订阅已建立")

        last_print = 0

        while True:
            self._mutex.lock()
            running = self._running
            inference_enabled = self._inference_enabled
            qr_enabled = self._qr_enabled
            inf_interval = self._inference_interval
            qr_interval = self._qr_interval
            self._mutex.unlock()

            if not running:
                break

            # spin 一次，触发回调
            rclpy.spin_once(self._node, timeout_sec=0.001)

            # 取帧
            self._frame_lock.lock()
            rgb = self._rgb_frame.copy() if self._rgb_frame is not None else None
            depth = self._depth_frame.copy() if self._depth_frame is not None else None
            self._frame_lock.unlock()

            if rgb is None or depth is None:
                time.sleep(0.01)
                continue

            self._frame_count += 1

            # 发射帧
            self.frame_ready.emit(rgb.copy())
            self.depth_frame_ready.emit(depth.copy())

            # YOLO 推理
            should_infer = inference_enabled and (self._frame_count % inf_interval == 0)
            detections = []
            if should_infer and self._detector:
                detections = self._detector.infer(
                    rgb,
                    conf_threshold=self._conf_threshold,
                    iou_threshold=self._iou_threshold,
                )
                # 给每个检测结果加深度值
                for det in detections:
                    bbox = det.get("bbox", [])
                    det["depth_mm"] = self.read_depth_center(depth, bbox)

                self._last_detections = detections
                self.detections_ready.emit(detections)

                # QR 解码
                if qr_enabled and self._frame_count % qr_interval == 0:
                    qr_results = self._qr_decoder.decode(rgb)
                    self.qr_results_ready.emit(qr_results)

            # FPS
            self._frame_times.append(time.perf_counter())
            cutoff = self._frame_times[-1] - 1.0
            self._frame_times = [t for t in self._frame_times if t > cutoff]
            fps = len(self._frame_times)

            now = time.time()
            if now - self._last_fps_emit > 0.5:
                self.fps_updated.emit(fps)
                self._last_fps_emit = now

            # 限频打印
            if now - last_print > 1.0:
                det_info = f", {len(detections)} dets" if detections else ""
                print(f"[DepthWorker] 帧{self._frame_count}, FPS:{fps}{det_info}")
                last_print = now

        # 清理
        if self._node:
            self._node.destroy_node()
            self._node = None
        print("[DepthWorker] 已停止")
