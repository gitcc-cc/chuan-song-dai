"""
推理工作线程
============
QThread 循环：持续读取摄像头 -> 发射预览帧 -> 可选推理 -> 发射检测结果。
支持跳帧推理（取模）和单次检测。
"""
import time
from typing import List, Dict, Any, Optional

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal, QMutex

from vision.camera import BaseCamera
from vision.detector import Detector


class InferenceWorker(QThread):
    """推理工作线程：持续预览，推理可开关，支持跳帧和单次检测"""

    # 信号：原始帧、检测结果、FPS
    frame_ready = pyqtSignal(np.ndarray)              # 原始 BGR 帧（预览用）
    detections_ready = pyqtSignal(list)               # 检测结果列表
    fps_updated = pyqtSignal(float)                   # 实时 FPS
    error_occurred = pyqtSignal(str)                  # 错误信息

    def __init__(self, parent=None):
        super().__init__(parent)

        self._camera: Optional[BaseCamera] = None
        self._detector: Optional[Detector] = None

        # 推理参数
        self._conf_threshold = 0.5
        self._iou_threshold = 0.45
        self._classes = None

        # 运行控制
        self._running = False          # 线程生命周期
        self._inference_enabled = False  # 是否做推理（连续模式）
        self._one_shot = False         # 单次检测标志
        self._inference_interval = 1   # 推理间隔（帧取模），1=每帧都推理
        self._frame_count = 0          # 帧计数器
        self._mutex = QMutex()

        # FPS 统计
        self._frame_times = []
        self._last_fps_emit = 0

    # ============ 配置 ============
    def set_camera(self, camera: BaseCamera):
        self._camera = camera

    def set_detector(self, detector: Detector):
        self._detector = detector

    def set_params(self, conf: float = None, iou: float = None, classes: list = None):
        if conf is not None:
            self._conf_threshold = conf
        if iou is not None:
            self._iou_threshold = iou
        if classes is not None:
            self._classes = classes

    def set_inference_interval(self, interval: int):
        """设置推理间隔（帧取模），1=每帧推理，5=每5帧推理1次"""
        self._mutex.lock()
        self._inference_interval = max(1, interval)
        self._mutex.unlock()

    # ============ 线程控制 ============
    def start_preview(self):
        """启动预览循环（不做推理）"""
        if not self._running:
            self._running = True
            self._inference_enabled = False
            self._one_shot = False
            self._frame_count = 0
            self.start()

    def enable_inference(self):
        """启用连续推理"""
        self._mutex.lock()
        self._inference_enabled = True
        self._one_shot = False
        self._mutex.unlock()

    def disable_inference(self):
        """禁用推理（仅预览）"""
        self._mutex.lock()
        self._inference_enabled = False
        self._mutex.unlock()

    def trigger_once(self):
        """触发单次检测（只推理一帧）"""
        self._mutex.lock()
        self._one_shot = True
        self._mutex.unlock()

    def stop(self):
        """停止线程并释放资源"""
        self._mutex.lock()
        self._running = False
        self._mutex.unlock()
        self.wait(3000)  # 最多等 3 秒

        # 确保摄像头关闭
        if self._camera:
            self._camera.close()

    # ============ 主循环 ============
    def run(self):
        if not self._camera or not self._camera.is_opened():
            self.error_occurred.emit("摄像头未打开")
            return
        if not self._detector:
            self.error_occurred.emit("检测器未加载")
            return

        # 预热
        self._detector.warmup()
        print("[InferenceWorker] 启动预览循环")

        while True:
            # 检查运行标志
            self._mutex.lock()
            running = self._running
            inference_enabled = self._inference_enabled
            one_shot = self._one_shot
            interval = self._inference_interval
            if one_shot:
                self._one_shot = False  # 取走标志
            self._mutex.unlock()

            if not running:
                break

            # 1) 读取帧（摄像头全速，不停）
            ret, frame = self._camera.read()
            if not ret or frame is None:
                time.sleep(0.01)
                continue

            # 2) 发射原始帧（预览，始终发）
            self.frame_ready.emit(frame.copy())

            # 3) 判断是否需要推理（连续模式 或 单次触发）
            should_infer = False
            if inference_enabled or one_shot:
                self._frame_count += 1
                if self._frame_count % interval == 0:
                    should_infer = True

            # 4) 执行推理
            if should_infer:
                detections = self._detector.infer(
                    frame,
                    conf_threshold=self._conf_threshold,
                    iou_threshold=self._iou_threshold,
                    classes=self._classes
                )
                self.detections_ready.emit(detections)

            # 5) 计算并发射 FPS
            self._frame_times.append(time.perf_counter())
            cutoff = self._frame_times[-1] - 1.0
            self._frame_times = [t for t in self._frame_times if t > cutoff]
            fps = len(self._frame_times)

            now = time.time()
            if now - self._last_fps_emit > 0.5:
                self.fps_updated.emit(fps)
                self._last_fps_emit = now

        print("[InferenceWorker] 预览循环结束")