"""
深度相机工作线程 (DepthWorker)
==============================

文件职责：
    本文件实现了一个基于 QThread 的深度相机工作线程，用于在后台处理 ROS2 深度相机数据。
    主要功能：
    1. 订阅 HP60C 深度相机的 RGB 彩色图像和深度图像话题
    2. 使用 YOLO 目标检测模型对 RGB 图像进行推理
    3. 对检测到的目标读取中心点的深度值（毫米）
    4. 支持二维码解码功能
    5. 通过 Qt 信号将处理结果（帧、检测结果、深度值等）发送给主线程

技术栈：
    - QThread: PyQt6 的线程类，用于在后台运行耗时的 ROS2 处理循环
    - ROS2 (rclpy): 机器人操作系统，用于订阅深度相机的话题
    - OpenCV (cv_bridge): 用于将 ROS2 图像消息转换为 OpenCV 格式
    - NumPy: 用于深度图像数据的处理

工作流程：
    1. 启动线程 -> 初始化 ROS2 节点 -> 订阅 RGB 和深度话题
    2. 主循环：spin_once 触发回调 -> 获取最新帧 -> YOLO 推理 -> 读取深度值
    3. 通过 Qt 信号将 RGB 帧、深度帧、检测结果（含深度值）发送给 GUI

注意：
    - 深度图像是 uint16 格式，单位是毫米（mm），最大范围约 1.5 米
    - RGB 图像是 BGR8 格式（OpenCV 默认格式）
    - 线程安全通过 QMutex 实现，多个线程可以安全访问共享数据
"""
import time
from typing import Optional

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal, QMutex

from vision.detector import Detector
from vision.qr_decoder import QRDecoder


class DepthWorker(QThread):
    """
    深度相机工作线程：ROS2 订阅 + YOLO + 深度值

    本类继承自 QThread，是一个独立的后台工作线程，负责：
    1. 通过 ROS2 订阅深度相机的 RGB 和深度图像
    2. 执行 YOLO 目标检测
    3. 读取检测框中心点的深度值
    4. 通过 Qt 信号将结果发送给主线程（GUI）

    主要属性：
        RGB_TOPIC: RGB 图像的 ROS2 话题名称
        DEPTH_TOPIC: 深度图像的 ROS2 话题名称
        frame_ready: 当 RGB 帧准备好时发射的信号
        depth_frame_ready: 当深度帧准备好时发射的信号
        detections_ready: 当 YOLO 检测完成时发射的信号（包含深度值）
        qr_results_ready: 当二维码解码完成时发射的信号
        fps_updated: 定期更新 FPS 的信号
        error_occurred: 发生错误时发射的信号
    """

    # Qt 信号定义：用于线程间通信
    # 注意：信号必须定义为类属性，不能在 __init__ 中定义
    frame_ready = pyqtSignal(np.ndarray)              # RGB BGR 帧：当有新的 RGB 图像帧时发射
    depth_frame_ready = pyqtSignal(np.ndarray)        # 深度帧（16UC1 mm）：当有新的深度图像帧时发射
    detections_ready = pyqtSignal(list)               # YOLO 检测结果（含 depth_mm）：当检测完成后发射
    qr_results_ready = pyqtSignal(list)               # 二维码解码结果：当二维码解码完成后发射
    fps_updated = pyqtSignal(float)                   # 实时 FPS：定期发射当前帧率
    error_occurred = pyqtSignal(str)                  # 错误信息：当发生错误时发射

    # ROS2 话题常量：定义要订阅的深度相机话题
    # HP60C 相机发布的话题格式：/ascamera_hp60c/camera_publisher/<类型>/<编号>
    RGB_TOPIC = "/ascamera_hp60c/camera_publisher/rgb0/image"  # RGB 彩色图像话题（BGR8格式）
    DEPTH_TOPIC = "/ascamera_hp60c/camera_publisher/depth0/image_raw"  # 深度图像话题（16UC1格式，单位毫米）

    def __init__(self, parent=None):
        """
        初始化深度相机工作线程

        参数：
            parent: 父对象，用于 Qt 对象树管理（通常传 None）

        注意：
            此时不会启动 ROS2 或开始处理，只是初始化属性。
            实际的 ROS2 订阅和处理循环在 start_worker() 和 run() 中启动。
        """
        super().__init__(parent)

        # YOLO 检测器：用于目标检测（延迟初始化，由外部设置）
        self._detector: Optional[Detector] = None
        # 二维码解码器：用于识别二维码（QRDecoder 类）
        self._qr_decoder = QRDecoder()

        # ROS2 相关变量
        self._node = None          # ROS2 节点对象
        self._rgb_sub = None       # RGB 图像订阅器
        self._depth_sub = None     # 深度图像订阅器
        self._bridge = None        # CvBridge 对象：用于将 ROS2 图像消息转换为 OpenCV 格式

        # 帧缓存（线程安全）
        # ROS2 回调在 spin_once 中触发，会更新这些变量
        # 主循环读取这些变量进行处理
        self._rgb_frame = None     # 最新的 RGB 帧（OpenCV BGR 格式）
        self._depth_frame = None   # 最新的深度帧（NumPy uint16，单位毫米）
        self._frame_lock = QMutex()  # 帧缓存锁：防止同时读写导致的数据竞争

        # YOLO 推理参数
        self._conf_threshold = 0.5    # 置信度阈值：低于此值的检测结果会被过滤
        self._iou_threshold = 0.45    # IoU 阈值：用于非极大值抑制（NMS），去除重叠框
        self._inference_enabled = False  # 是否启用 YOLO 推理（默认关闭，由外部开启）
        self._qr_enabled = False      # 是否启用二维码解码（默认关闭）
        self._inference_interval = 1  # 推理间隔：每隔几帧进行一次推理（1=每帧都推理）
        self._qr_interval = 1         # 二维码解码间隔：每隔几帧解码一次
        self._frame_count = 0         # 帧计数器：记录已处理的帧数
        self._last_detections = []    # 最近一次检测结果：供 QR 解码匹配使用

        # 运行控制
        self._running = False     # 线程运行标志：False=停止，True=运行中
        self._mutex = QMutex()    # 互斥锁：保护配置参数的线程安全访问

        # FPS 计算
        self._frame_times = []        # 帧时间戳列表：用于计算过去 1 秒内的帧数
        self._last_fps_emit = 0       # 上次发射 FPS 信号的时间：防止频繁发射

    # ============ 配置方法 ============
    # 这些方法用于在运行前或运行中修改参数
    # 注意：多线程环境下修改共享变量需要加锁（QMutex）

    def set_detector(self, detector: Detector):
        """
        设置 YOLO 检测器实例

        参数：
            detector: Detector 对象，已加载 YOLO 模型

        用途：
            在启动线程前调用，将外部加载好的 YOLO 检测器设置到本线程中。
        """
        self._detector = detector

    def set_params(self, conf: float = None, iou: float = None):
        """
        设置 YOLO 推理参数

        参数：
            conf: 置信度阈值（0-1），低于此值的检测结果会被过滤
            iou: IoU 阈值（0-1），用于非极大值抑制，去除重叠的检测框

        用途：
            调整 YOLO 检测的灵敏度。conf 越高，检测越严格；iou 越高，去重越严格。
        """
        if conf is not None:
            self._conf_threshold = conf
        if iou is not None:
            self._iou_threshold = iou

    def set_inference_interval(self, interval: int):
        """
        设置 YOLO 推理间隔

        参数：
            interval: 推理间隔（帧数），例如 3 表示每 3 帧推理一次

        用途：
            降低推理频率以节省 CPU/GPU 资源。interval=1 表示每帧都推理。
        """
        self._mutex.lock()
        self._inference_interval = max(1, interval)  # 至少为 1
        self._mutex.unlock()

    def set_qr_enabled(self, enabled: bool):
        """
        启用/禁用二维码解码功能

        参数：
            enabled: True=启用，False=禁用

        用途：
            控制是否在检测结果上进行二维码解码（需要先启用推理）。
        """
        self._mutex.lock()
        self._qr_enabled = enabled
        self._mutex.unlock()

    def set_qr_interval(self, interval: int):
        """
        设置二维码解码间隔

        参数：
            interval: 解码间隔（帧数），例如 5 表示每 5 帧解码一次

        用途：
            降低二维码解码频率，因为解码比推理更耗时。
        """
        self._mutex.lock()
        self._qr_interval = max(1, interval)  # 至少为 1
        self._mutex.unlock()

    def enable_inference(self):
        """
        启用 YOLO 推理

        用途：
            开启目标检测功能，线程会开始执行 YOLO 推理。
        """
        self._mutex.lock()
        self._inference_enabled = True
        self._mutex.unlock()

    def disable_inference(self):
        """
        禁用 YOLO 推理

        用途：
            关闭目标检测功能，线程只输出 RGB/深度帧，不进行推理。
        """
        self._mutex.lock()
        self._inference_enabled = False
        self._mutex.unlock()

    # ============ ROS2 回调函数 ============
    # ROS2 使用发布-订阅模型：
    # - 相机节点发布图像消息到话题（Topic）
    # - 本线程订阅话题，收到消息时调用回调函数
    # - 回调函数在 spin_once() 中被触发，不是实时触发

    def _rgb_callback(self, msg):
        """
        RGB 图像回调函数

        参数：
            msg: sensor_msgs/Image 消息，包含 ROS2 格式的 RGB 图像数据

        工作流程：
            1. 使用 CvBridge 将 ROS2 图像消息转换为 OpenCV 格式
            2. 转换为 BGR8 格式（OpenCV 默认颜色顺序）
            3. 将帧存入缓存（线程安全）

        注意：
            - 此函数在 spin_once() 中被调用，不是实时触发
            - 异常会被静默忽略（可能导致帧丢失，但不会崩溃）
        """
        try:
            # imgmsg_to_cv2: 将 ROS2 Image 消息转换为 OpenCV 图像
            # "bgr8" 表示目标格式：8位 BGR 颜色（OpenCV 默认）
            frame = self._bridge.imgmsg_to_cv2(msg, "bgr8")
            self._frame_lock.lock()
            self._rgb_frame = frame  # 更新 RGB 帧缓存
            self._frame_lock.unlock()
        except Exception:
            pass  # 转换失败时静默处理，可能帧格式不匹配

    def _depth_callback(self, msg):
        """
        深度图像回调函数

        参数：
            msg: sensor_msgs/Image 消息，包含 ROS2 格式的深度图像数据

        工作流程：
            1. 使用 CvBridge 将 ROS2 图像消息转换为 NumPy 数组
            2. 使用 "passthrough" 保持原始数据格式（uint16，单位毫米）
            3. 将深度帧存入缓存（线程安全）

        深度值说明：
            - 数据类型：uint16（无符号16位整数）
            - 单位：毫米（mm）
            - 有效范围：约 0-1500mm（0-1.5米）
            - 0 表示无效/超出范围
            - 像素值 = 实际距离（毫米），例如 500 表示 0.5 米

        注意：
            - "passthrough" 保持原始格式，不做任何转换
            - 深度图像通常是灰度图，但值代表距离而不是亮度
        """
        try:
            # imgmsg_to_cv2: 将 ROS2 Image 消息转换为 OpenCV 图像
            # "passthrough" 表示保持原始数据格式，不做颜色转换
            depth = self._bridge.imgmsg_to_cv2(msg, "passthrough")
            self._frame_lock.lock()
            self._depth_frame = depth  # 更新深度帧缓存
            self._frame_lock.unlock()
        except Exception:
            pass  # 转换失败时静默处理

    # ============ 深度值读取 ============

    @staticmethod
    def read_depth_center(depth_frame: np.ndarray, bbox: list) -> int:
        """
        读取检测框中心点的深度值（毫米）

        原理：
            YOLO 检测返回的边界框 (bbox) 定义了目标在 RGB 图像中的位置。
            深度图像和 RGB 图像是对齐的（同一坐标系），所以可以直接读取
            边界框中心点在深度图像中的值，得到目标的深度（距离）。

        参数：
            depth_frame: 深度图像（NumPy 数组，uint16 格式，单位毫米）
            bbox: 边界框坐标 [x1, y1, x2, y2]（左上角 x,y 和右下角 x,y）

        返回值：
            int: 深度值（毫米），有效范围约 0-1500mm
            int: -1 表示无效（深度帧为空、边界框无效、坐标越界、深度值为0）

        使用场景：
            在 YOLO 检测完成后调用，为每个检测结果添加深度信息。
            例如：det["depth_mm"] = DepthWorker.read_depth_center(depth, det["bbox"])

        深度值精度：
            - 原始值是 uint16，精度为 1mm
            - 如果深度帧是 float 格式（米），会自动转换为毫米
            - 0 值通常表示无效/超出量程
        """
        # 检查输入有效性
        if depth_frame is None or len(bbox) != 4:
            return -1  # 深度帧为空或边界框格式错误

        # 解析边界框坐标并转换为整数（像素坐标必须是整数）
        x1, y1, x2, y2 = [int(v) for v in bbox]

        # 计算边界框中心点坐标
        # 中心点 = (左上角 + 右下角) / 2
        cx = (x1 + x2) // 2  # 中心点 x 坐标
        cy = (y1 + y2) // 2  # 中心点 y 坐标

        # 获取深度图像尺寸（高度 h，宽度 w）
        h, w = depth_frame.shape[:2]

        # 边界检查：确保中心点在深度图像范围内
        if not (0 <= cy < h and 0 <= cx < w):
            return -1  # 坐标越界，无法读取深度值

        # 读取中心点的深度值
        raw = depth_frame[cy, cx]  # NumPy 索引：[行, 列] = [y, x]

        # 根据深度图像数据类型处理
        if depth_frame.dtype == np.uint16:
            # uint16 格式：值已经是毫米，直接返回
            # 0 表示无效/超出量程
            return int(raw) if raw > 0 else -1
        else:
            # float 格式：值单位是米，需要转换为毫米
            # 例如：0.5（米）-> 500（毫米）
            mm = int(raw * 1000)
            return mm if mm > 0 else -1

    # ============ 线程控制方法 ============

    def start_worker(self):
        """
        启动深度相机工作线程

        用途：
            外部调用此方法启动后台处理线程。
            线程启动后会执行 run() 方法中的主循环。

        注意：
            - 只有在 _running 为 False 时才能启动
            - 启动后 _running 会被设为 True
            - 帧计数器会被重置为 0
        """
        if not self._running:
            self._running = True
            self._frame_count = 0
            self.start()  # 调用 QThread.start()，启动线程并执行 run()

    def stop(self):
        """
        停止深度相机工作线程并释放资源

        用途：
            停止后台处理线程，释放 ROS2 节点等资源。
            通常在页面关闭或切换时调用。

        工作流程：
            1. 设置 _running = False，通知主循环退出
            2. 等待线程结束（最多等待 3 秒）
            3. 销毁 ROS2 节点，释放资源

        注意：
            - 使用 wait(3000) 等待线程结束，避免资源泄漏
            - 如果线程在 3 秒内未结束，会强制继续执行（可能有问题）
        """
        self._mutex.lock()
        self._running = False  # 通知主循环退出
        self._mutex.unlock()

        # 等待线程结束（最多 3 秒）
        # wait() 是 QThread 的方法，会阻塞当前线程直到目标线程结束
        self.wait(3000)

        # 销毁 ROS2 节点，释放 ROS2 资源
        if self._node:
            self._node.destroy_node()  # 销毁节点，取消所有订阅
            self._node = None

    # ============ 主循环 ============

    def run(self):
        """
        线程主循环：初始化 ROS2 → 订阅话题 → 处理帧

        这是 QThread 的核心方法，线程启动后自动执行。
        主循环会持续运行，直到 _running 被设为 False。

        工作流程：
            1. 导入 ROS2 依赖（延迟导入，避免未安装时崩溃）
            2. 初始化 ROS2 节点
            3. 创建 CvBridge（用于图像格式转换）
            4. 订阅 RGB 和深度话题
            5. 主循环：
               - 检查运行标志
               - spin_once 触发回调，更新帧缓存
               - 获取最新帧
               - 执行 YOLO 推理（如果启用）
               - 读取深度值
               - 计算 FPS
               - 发射信号到主线程
            6. 退出时销毁 ROS2 节点
        """
        # 延迟导入 ROS2 依赖（避免未安装时导入失败）
        try:
            import rclpy
            from rclpy.node import Node
            from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
            from sensor_msgs.msg import Image
            from cv_bridge import CvBridge
        except ImportError as e:
            # ROS2 依赖缺失时，发射错误信号并退出
            self.error_occurred.emit(f"ROS2 依赖缺失: {e}")
            return

        # 初始化 ROS2
        # rclpy.init() 只能调用一次，如果已初始化会抛出异常
        try:
            if not rclpy.ok():  # 检查 ROS2 是否已初始化
                rclpy.init()    # 初始化 ROS2 客户端
        except Exception:
            pass  # 可能已初始化，忽略异常

        # 创建 ROS2 节点
        # 节点是 ROS2 的基本通信单元，每个节点可以发布/订阅话题
        self._node = Node("depth_worker_node")  # 节点名称：depth_worker_node

        # 创建 CvBridge 对象
        # CvBridge 用于在 ROS2 Image 消息和 OpenCV 图像之间转换
        self._bridge = CvBridge()

        # 创建 QoS（Quality of Service）配置
        # QoS 决定了消息传递的可靠性、持久性等特性
        # 对于实时相机流，通常使用 BEST_EFFORT + VOLATILE
        qos = QoSProfile(
            depth=1,  # 队列深度：只保留最新 1 条消息（避免延迟）
            reliability=ReliabilityPolicy.BEST_EFFORT,  # 尽力交付：不保证送达，但延迟低
            durability=DurabilityPolicy.VOLATILE,  # 易失性：新订阅者不会收到历史消息
        )

        # 订阅 RGB 图像话题
        # 当相机发布 RGB 图像时，会调用 _rgb_callback 函数
        self._rgb_sub = self._node.create_subscription(
            Image,              # 消息类型：sensor_msgs/Image
            self.RGB_TOPIC,     # 话题名称：/ascamera_hp60c/camera_publisher/rgb0/image
            self._rgb_callback, # 回调函数：收到消息时调用
            qos                 # QoS 配置
        )

        # 订阅深度图像话题
        # 当相机发布深度图像时，会调用 _depth_callback 函数
        self._depth_sub = self._node.create_subscription(
            Image,              # 消息类型：sensor_msgs/Image
            self.DEPTH_TOPIC,   # 话题名称：/ascamera_hp60c/camera_publisher/depth0/image_raw
            self._depth_callback, # 回调函数：收到消息时调用
            qos                 # QoS 配置
        )

        print("[DepthWorker] ROS2 节点已启动，等待图像...")
        self._node.get_logger().info("DepthWorker 订阅已建立")

        last_print = 0  # 上次打印日志的时间

        # 主循环：持续处理帧，直到 _running 被设为 False
        while True:
            # 读取配置参数（线程安全）
            # 使用互斥锁保护，防止在读取时被其他线程修改
            self._mutex.lock()
            running = self._running
            inference_enabled = self._inference_enabled
            qr_enabled = self._qr_enabled
            inf_interval = self._inference_interval
            qr_interval = self._qr_interval
            self._mutex.unlock()

            # 检查运行标志，如果为 False 则退出循环
            if not running:
                break

            # spin_once：触发一次 ROS2 回调
            # 这会检查所有订阅的话题，如果有新消息就调用对应的回调函数
            # timeout_sec=0.001 表示最多等待 1 毫秒（避免阻塞）
            rclpy.spin_once(self._node, timeout_sec=0.001)

            # 获取最新的 RGB 和深度帧（线程安全）
            # 使用 copy() 创建副本，防止回调函数同时修改帧数据
            self._frame_lock.lock()
            rgb = self._rgb_frame.copy() if self._rgb_frame is not None else None
            depth = self._depth_frame.copy() if self._depth_frame is not None else None
            self._frame_lock.unlock()

            # 如果还没有收到帧，等待一段时间再重试
            if rgb is None or depth is None:
                time.sleep(0.01)  # 等待 10 毫秒
                continue  # 跳过本次循环，继续下一次

            # 帧计数器加 1
            self._frame_count += 1

            # 发射 RGB 和深度帧到主线程
            # 使用 copy() 确保主线程收到的是独立副本
            self.frame_ready.emit(rgb.copy())
            self.depth_frame_ready.emit(depth.copy())

            # YOLO 推理逻辑
            # should_infer: 是否应该在这一帧进行推理
            # 条件：推理已启用 且 帧计数是推理间隔的倍数
            should_infer = inference_enabled and (self._frame_count % inf_interval == 0)
            detections = []

            if should_infer and self._detector:
                # 执行 YOLO 推理
                # infer() 方法接收 RGB 图像，返回检测结果列表
                detections = self._detector.infer(
                    rgb,
                    conf_threshold=self._conf_threshold,
                    iou_threshold=self._iou_threshold,
                )

                # 为每个检测结果添加深度值
                # YOLO 只检测 RGB 图像，不包含深度信息
                # 我们需要从深度图像中读取每个检测框中心点的深度值
                for det in detections:
                    bbox = det.get("bbox", [])  # 获取边界框坐标 [x1, y1, x2, y2]
                    det["depth_mm"] = self.read_depth_center(depth, bbox)  # 添加深度值

                # 保存最近一次检测结果（供 QR 解码匹配使用）
                self._last_detections = detections

                # 发射检测结果到主线程
                self.detections_ready.emit(detections)

                # 二维码解码（可选）
                # 只有在启用 QR 且帧计数是 QR 间隔的倍数时才执行
                if qr_enabled and self._frame_count % qr_interval == 0:
                    qr_results = self._qr_decoder.decode(rgb)  # 解码 RGB 图像中的二维码
                    self.qr_results_ready.emit(qr_results)  # 发射解码结果

            # 计算 FPS（每秒帧数）
            # 使用最近 1 秒内的时间戳数量来估算 FPS
            self._frame_times.append(time.perf_counter())  # 记录当前时间戳
            cutoff = self._frame_times[-1] - 1.0  # 1 秒前的时间点
            # 过滤掉超过 1 秒的时间戳
            self._frame_times = [t for t in self._frame_times if t > cutoff]
            fps = len(self._frame_times)  # 剩余时间戳数量就是 FPS

            # 定期发射 FPS 信号（每 0.5 秒一次）
            now = time.time()
            if now - self._last_fps_emit > 0.5:
                self.fps_updated.emit(fps)  # 发射 FPS 到主线程
                self._last_fps_emit = now

            # 限频打印日志（每 1 秒一次）
            if now - last_print > 1.0:
                det_info = f", {len(detections)} dets" if detections else ""
                print(f"[DepthWorker] 帧{self._frame_count}, FPS:{fps}{det_info}")
                last_print = now

        # 循环退出后的清理工作
        if self._node:
            self._node.destroy_node()  # 销毁 ROS2 节点
            self._node = None
        print("[DepthWorker] 已停止")
