"""
推理工作线程
===========
QThread 循环：持续读取摄像头 -> 发射预览帧 -> 可选推理+解码 -> 发射检测结果。
支持跳帧推理（取模）、单次检测、二维码/条形码解码。

本文件职责：
    定义 InferenceWorker 类，继承自 QThread，在独立线程中运行摄像头读取循环。
    主要功能：
    1. 持续从摄像头读取帧，并通过信号发射给 GUI 显示预览
    2. 可选地对每帧进行 YOLO 目标检测（可开启/关闭/单次触发）
    3. 可选地对每帧进行二维码/条形码解码
    4. 实时计算并发射 FPS（每秒帧数）
    5. 通过信号槽机制与主线程通信，线程安全

线程模型：
    - QThread 是 PyQt6 的线程类，run() 方法在子线程中执行
    - 通过 pyqtSignal 信号将数据发射回主线程（GUI），避免跨线程直接操作 UI
    - 使用 QMutex 互斥锁保护共享变量，防止多线程竞争

依赖：
    - vision.camera.BaseCamera: 摄像头基类，提供 read() 方法读取帧
    - vision.detector.Detector: YOLO 检测器，提供 infer() 方法进行推理
    - vision.qr_decoder.QRDecoder: 二维码解码器，提供 decode() 方法
"""
import time
from typing import Optional

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal, QMutex

from vision.camera import BaseCamera
from vision.detector import Detector
from vision.qr_decoder import QRDecoder


class InferenceWorker(QThread):
    """
    推理工作线程：持续预览，推理可开关，支持跳帧和单次检测

    使用方法：
        worker = InferenceWorker()
        worker.set_camera(camera)        # 设置摄像头
        worker.set_detector(detector)    # 设置检测器
        worker.frame_ready.connect(on_frame)      # 连接预览帧信号
        worker.detections_ready.connect(on_detect) # 连接检测结果信号
        worker.start_preview()           # 启动预览循环

    注意事项：
        - 必须先调用 set_camera() 和 set_detector() 再启动
        - stop() 会等待线程结束（最多3秒），之后摄像头会自动关闭
        - 所有配置方法都是线程安全的（内部使用 QMutex）
    """

    # ==================== 信号定义 ====================
    # pyqtSignal 是 PyQt6 的信号机制，用于子线程向主线程发送数据
    # 信号一旦定义就是类属性，通过 emit() 发射，通过 connect() 连接槽函数

    frame_ready = pyqtSignal(np.ndarray)              # 原始 BGR 帧（预览用）
    # 当摄像头读取到一帧时发射，携带 numpy 数组格式的 BGR 图像
    # GUI 收到后可以显示在 QLabel 或 QGraphicsView 上

    detections_ready = pyqtSignal(list)               # YOLO 检测结果
    # 当 YOLO 推理完成时发射，携带检测结果列表
    # 每个元素是字典，包含 bbox、confidence、class_id、class_name 等

    qr_results_ready = pyqtSignal(list)               # 二维码解码结果
    # 当二维码解码完成时发射，携带解码结果列表
    # 每个元素是字典，包含 type、data、bbox 等

    fps_updated = pyqtSignal(float)                   # 实时 FPS
    # 每 0.5 秒发射一次当前帧率，用于 GUI 显示

    error_occurred = pyqtSignal(str)                  # 错误信息
    # 当摄像头未打开或检测器未加载时发射错误消息

    def __init__(self, parent=None):
        """
        初始化推理工作线程

        参数：
            parent: 父对象，用于 Qt 对象树管理，通常传 None 或 QWidget
        """
        super().__init__(parent)

        # ==================== 核心组件 ====================
        self._camera: Optional[BaseCamera] = None      # 摄像头实例，负责读取帧
        self._detector: Optional[Detector] = None      # YOLO 检测器实例
        self._qr_decoder = QRDecoder()                  # 二维码解码器，始终创建

        # ==================== 推理参数 ====================
        self._conf_threshold = 0.5   # 置信度阈值：低于此值的检测框会被过滤（0~1）
        self._iou_threshold = 0.45   # IoU 阈值：用于 NMS（非极大值抑制）去重（0~1）
        self._classes = None         # 类别过滤：None 表示检测所有类别，或传入类别 ID 列表

        # ==================== 运行控制标志 ====================
        self._running = False            # 主循环是否运行，False 时退出 run() 方法
        self._inference_enabled = False  # 是否启用连续推理（True=每帧都推理）
        self._one_shot = False           # 单次触发标志，True 时推理一帧后自动复位
        self._qr_enabled = True          # 二维码解码开关
        self._inference_interval = 1     # 推理间隔（帧取模），1=每帧推理，N=每 N 帧推理 1 次
        self._qr_interval = 1            # 二维码解码间隔（帧取模）
        self._frame_count = 0            # 已读取的帧计数器，用于取模判断
        self._mutex = QMutex()           # 互斥锁，保护上述共享变量

        # ==================== FPS 统计 ====================
        self._frame_times = []       # 存储最近 1 秒内每帧的时间戳
        self._last_fps_emit = 0      # 上次发射 FPS 信号的时间，用于控制发射频率

    # ==================== 配置方法 ====================
    # 这些方法在主线程调用，修改线程内的参数，内部使用 QMutex 保证线程安全

    def set_camera(self, camera: BaseCamera):
        """
        设置摄像头实例

        参数：
            camera: BaseCamera 子类实例（如 OpenCVCamera），必须支持 read() 方法
                    read() 返回 (bool, np.ndarray) 元组

        注意：必须在 start_preview() 之前调用
        """
        self._camera = camera

    def set_detector(self, detector: Detector):
        """
        设置 YOLO 检测器实例

        参数：
            detector: Detector 类实例，必须支持 infer() 方法
                      infer() 返回检测结果列表

        注意：必须在 start_preview() 之前调用
        """
        self._detector = detector

    def set_params(self, conf: float = None, iou: float = None, classes: list = None):
        """
        批量设置推理参数

        参数：
            conf: 置信度阈值（0~1），低于此值的检测框被过滤，None 表示不修改
            iou:  IoU 阈值（0~1），用于 NMS 去重，None 表示不修改
            classes: 类别 ID 列表，只检测指定类别，None 表示检测所有类别

        示例：
            worker.set_params(conf=0.6, classes=[0, 1])  # 只检测 person 和 bicycle
        """
        if conf is not None:
            self._conf_threshold = conf
        if iou is not None:
            self._iou_threshold = iou
        if classes is not None:
            self._classes = classes

    def set_inference_interval(self, interval: int):
        """
        设置推理间隔（帧取模）

        参数：
            interval: 推理间隔帧数，必须 >= 1
                      1 = 每帧都推理（最频繁，CPU/GPU 负载最高）
                      2 = 每 2 帧推理 1 次
                      5 = 每 5 帧推理 1 次（适合高分辨率或慢速场景）

        原理：
            主循环每读取一帧，frame_count 加 1
            当 frame_count % interval == 0 时才执行推理
            这样可以在帧率和推理频率之间做权衡
        """
        self._mutex.lock()                          # 加锁，防止与 run() 中的读取冲突
        self._inference_interval = max(1, interval)  # 确保最小值为 1
        self._mutex.unlock()                        # 解锁

    def set_qr_enabled(self, enabled: bool):
        """
        设置二维码解码开关

        参数：
            enabled: True=启用解码，False=禁用解码
        """
        self._mutex.lock()
        self._qr_enabled = enabled
        self._mutex.unlock()

    def set_qr_interval(self, interval: int):
        """
        设置二维码解码间隔（帧取模）

        参数：
            interval: 解码间隔帧数，必须 >= 1
        """
        self._mutex.lock()
        self._qr_interval = max(1, interval)
        self._mutex.unlock()

    def set_qr_slice(self, slice_ms: int, fp_thresh: float = None):
        """
        设置 v1.2 渐进式搜索的时间片（v1.2 已并入 QRDecoder）

        参数：
            slice_ms: 时间片毫秒数。0=关闭（v1.1 原行为，解不出帧烧满
                      rectify_budget_ms=5000，预览冻结）；600=启用时间片+
                      搜索游标+帧指纹门（单次最坏 ~1s，跨帧续搜）
            fp_thresh: 指纹门阈值（仅 slice_ms>0 生效），None=保持当前值
        """
        self._qr_decoder.set_slice(slice_ms, fp_thresh)

    # ==================== 线程控制方法 ====================
    # 这些方法控制线程的启动、停止和推理模式

    def start_preview(self):
        """
        启动预览循环（不做推理）

        行为：
            1. 设置 _running=True，_inference_enabled=False
            2. 调用 QThread.start() 启动子线程
            3. 子线程执行 run() 方法中的主循环
            4. 主循环持续读取帧并发射 frame_ready 信号
            5. 由于 _inference_enabled=False，不会执行推理

        注意：如果线程已在运行，此方法不会重复启动
        """
        if not self._running:                       # 防止重复启动
            self._running = True                    # 标记线程运行
            self._inference_enabled = False         # 仅预览，不推理
            self._one_shot = False
            self._frame_count = 0                   # 重置帧计数
            self.start()                            # QThread.start() → 调用 run()

    def enable_inference(self):
        """
        启用连续推理

        启用后，主循环会根据 _inference_interval 的设置，
        每隔 N 帧自动执行一次 YOLO 推理。
        """
        self._mutex.lock()
        self._inference_enabled = True
        self._one_shot = False     # 连续模式会覆盖单次触发
        self._mutex.unlock()

    def disable_inference(self):
        """
        禁用推理（仅预览）

        禁用后，主循环不再执行推理，只读取帧并发射预览。
        已有的检测结果不会被清除，只是不再产生新的结果。
        """
        self._mutex.lock()
        self._inference_enabled = False
        self._mutex.unlock()

    def trigger_once(self):
        """
        触发单次检测（只推理一帧）

        用途：用户点击"检测一次"按钮时调用
        原理：设置 _one_shot=True，run() 中检测到后自动复位为 False

        注意：如果推理间隔 > 1，可能需要等待几帧才会执行
        """
        self._mutex.lock()
        self._one_shot = True
        self._mutex.unlock()

    def stop(self):
        """
        停止线程并释放资源

        流程：
            1. 设置 _running=False，通知 run() 中的循环退出
            2. 调用 self.wait(3000) 等待线程结束，最多等 3 秒
            3. 如果摄像头已打开，调用 camera.close() 释放硬件资源

        注意：wait() 是阻塞调用，会等待线程实际退出
        """
        self._mutex.lock()
        self._running = False           # 通知循环退出
        self._mutex.unlock()
        self.wait(3000)                 # 阻塞等待线程结束（最多 3 秒）

        if self._camera:
            self._camera.close()        # 释放摄像头硬件资源

    # ==================== 主循环 ====================
    # run() 方法由 QThread.start() 触发，在子线程中执行
    # 这是整个线程的核心，包含帧读取、推理、解码、FPS 计算等逻辑

    def run(self):
        """
        线程主循环（由 QThread.start() 自动调用）

        执行流程：
            1. 检查摄像头和检测器是否就绪
            2. 预热检测器（第一次推理通常较慢）
            3. 进入 while True 循环：
               a. 读取控制标志（加锁）
               b. 如果 _running=False，退出循环
               c. 从摄像头读取一帧
               d. 发射原始帧信号（预览用）
               e. 判断是否需要推理（连续模式 或 单次触发）
               f. 如果需要推理，执行 YOLO 检测并发射结果
               g. 同时检查是否需要解码二维码
               h. 更新 FPS 统计并定期发射
            4. 循环结束后打印退出消息
        """
        # ========== 初始化检查 ==========
        if not self._camera or not self._camera.is_opened():
            # 摄像头未打开，发射错误信号并退出
            self.error_occurred.emit("摄像头未打开")
            return
        if not self._detector:
            # 检测器未加载，发射错误信号并退出
            self.error_occurred.emit("检测器未加载")
            return

        # 预热检测器：第一次推理会加载模型到 GPU/内存，通常较慢
        # 提前执行可以避免第一帧推理延迟
        self._detector.warmup()
        print("[InferenceWorker] 启动预览循环")

        # ========== 主循环 ==========
        while True:
            # --- 第一步：加锁读取所有控制标志 ---
            # 为什么要在循环开头读取？
            # 因为配置方法（如 enable_inference）在主线程调用，
            # 而 run() 在子线程执行，需要通过互斥锁同步
            self._mutex.lock()
            running = self._running               # 是否继续运行
            inference_enabled = self._inference_enabled  # 是否启用推理
            one_shot = self._one_shot             # 是否单次触发
            qr_enabled = self._qr_enabled         # 是否启用二维码解码
            inf_interval = self._inference_interval  # 推理间隔
            qr_interval = self._qr_interval        # 二维码解码间隔
            if one_shot:
                self._one_shot = False            # 单次触发：读取后立即复位
            self._mutex.unlock()                  # 读取完毕，解锁

            # --- 第二步：检查是否需要退出 ---
            if not running:
                break                             # 退出 while 循环，线程结束

            # --- 第三步：从摄像头读取一帧 ---
            # camera.read() 返回 (成功标志, 图像数组)
            # 图像格式通常是 BGR 的 numpy 数组，shape 为 (height, width, 3)
            ret, frame = self._camera.read()
            if not ret or frame is None:
                # 读取失败（可能是摄像头断开），短暂休眠后重试
                time.sleep(0.01)                  # 10ms，避免空转占用 CPU
                continue                          # 跳过本次循环，继续下一次

            self._frame_count += 1                # 帧计数加 1，用于取模判断

            # --- 第四步：发射原始帧（预览用） ---
            # 发射 .copy() 是为了防止后续帧覆盖同一块内存
            # 因为摄像头 read() 可能复用内部缓冲区
            self.frame_ready.emit(frame.copy())

            # --- 第五步：判断是否需要推理 ---
            # 两个条件同时满足才推理：
            #   1. 推理已启用（连续模式）或 单次触发
            #   2. 帧计数是间隔的整数倍（取模判断）
            # 例如：inference_interval=5 时，第 5、10、15... 帧才会推理
            should_infer = (inference_enabled or one_shot) and (self._frame_count % inf_interval == 0)

            # --- 第六步：执行 YOLO 推理 ---
            detections = []                       # 默认空结果
            if should_infer:
                # detector.infer() 接收原始帧和参数，返回检测结果列表
                # 每个结果是字典，包含：bbox, confidence, class_id, class_name
                detections = self._detector.infer(
                    frame,                        # 原始 BGR 帧
                    conf_threshold=self._conf_threshold,  # 置信度阈值
                    iou_threshold=self._iou_threshold,    # IoU 阈值
                    classes=self._classes                  # 类别过滤
                )
                # 发射检测结果，GUI 收到后会在预览帧上绘制检测框
                self._last_detections = detections  # 缓存：供 QR 结果匹配检测框用
                self.detections_ready.emit(detections)

                # --- 第七步：YOLO 触发时同时执行二维码解码 ---
                # 有检测框时优先 ROI 裁剪放大解码，失败再整帧
                if qr_enabled and self._frame_count % qr_interval == 0:
                    rois = [d.get("bbox") for d in detections if d.get("bbox")]
                    qr_results = self._qr_decoder.decode(frame, rois=rois)
                    self.qr_results_ready.emit(qr_results)

            # --- 第八步：FPS 统计 ---
            # 记录当前帧的时间戳
            self._frame_times.append(time.perf_counter())

            # 计算 1 秒窗口内的帧数：
            # 移除 1 秒前的时间戳，只保留最近 1 秒内的
            cutoff = self._frame_times[-1] - 1.0
            self._frame_times = [t for t in self._frame_times if t > cutoff]

            # 帧数即为 FPS（因为只统计 1 秒内的帧数）
            fps = len(self._frame_times)

            # 每 0.5 秒发射一次 FPS 信号，避免过于频繁地更新 GUI
            now = time.time()
            if now - self._last_fps_emit > 0.5:
                self.fps_updated.emit(fps)        # 发射给 GUI 显示
                self._last_fps_emit = now

        print("[InferenceWorker] 预览循环结束")
