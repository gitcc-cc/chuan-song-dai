"""
产线管理器 (ProductionManager)
============================
本文件是产线控制系统的核心管理器，负责：
1. 模型单例管理：一个 YOLO 模型实例，三个槽位共享使用
2. Worker 生命周期：管理三个检测 Worker（普通摄像头或深度相机）的启动、停止、参数更新
3. 槽位管理：支持普通摄像头模式（OpenCVCamera）和深度相机模式（HP60C ROS2）
4. MQTT 指令解析：接收并执行来自 ht/cmd/2 的控制指令
5. 检测结果发送：将检测结果通过 MQTT 发送到 ht/status/2

核心设计思想：
- 模型单例：避免重复加载模型，节省内存和 GPU 资源
- 槽位独立：每个槽位可以独立启停，互不影响
- 异步操作：摄像头打开等耗时操作在后台线程执行，不阻塞 UI
- 深度模式：1号槽位支持深度相机，通过 ROS2 获取深度信息

使用方式：
- 先调用 load_model() 加载模型
- 调用 start_slot() 或 start_slot_depth() 启动槽位
- 通过 MQTT 指令或 UI 按钮控制检测开关
- 调用 send_detection_result() 发送检测结果
- 结束时调用 cleanup() 释放所有资源
"""
import os
import subprocess
import time
from typing import Optional, Callable

from PyQt6.QtCore import QObject, pyqtSignal, QThread

from vision.detector import Detector          # 检测器：封装 YOLO 模型进行推理
from vision.worker import InferenceWorker    # 推理 Worker：普通摄像头模式下的检测线程
from vision.depth_worker import DepthWorker  # 深度 Worker：深度相机模式下的检测线程
from vision.camera import OpenCVCamera, BaseCamera  # 摄像头：OpenCVCamera 为普通摄像头，BaseCamera 为基类
from core.mqtt_client import MqttClient       # 主控通信库：用于 MQTT 通信

# 相机 ROS2 launch 包/文件名（写死，避免拼接注入）
_CAM_PKG = "ascamera"
_CAM_LAUNCH = "hp60c.launch.py"
_CAM_LOG = "/tmp/hp60c_camera.log"
_ROS_SETUP = "/opt/ros/humble/setup.bash"


def restart_camera_node() -> None:
    """
    手动重启深度相机 ROS2 节点（拔插相机后用）。
    流程：杀 launch/node → 等 3s 释放 UVC → 重新 launch → 等 8s 出流。
    同步阻塞约 11 秒，调用方应在 QThread 里执行。
    """
    # 杀干净旧进程（只杀 launch 会残留 ascamera_node 占着 UVC）
    subprocess.run(["pkill", "-f", "hp60c.launch.py"],
                   capture_output=True, check=False)
    subprocess.run(["pkill", "-f", "ascamera_node"],
                   capture_output=True, check=False)
    time.sleep(3)  # 等 UVC 设备释放

    # 重新启动（shell 里 source ROS 环境后后台拉起，日志追加）
    cmd = (
        f'source {_ROS_SETUP} && '
        f'setsid ros2 launch {_CAM_PKG} {_CAM_LAUNCH} >> {_CAM_LOG} 2>&1 &'
    )
    subprocess.Popen(cmd, shell=True, executable="/bin/bash",
                     start_new_session=True)
    time.sleep(8)  # 等相机初始化出流


class RestartCameraThread(QThread):
    """后台重启相机节点，不阻塞 UI。"""
    done = pyqtSignal(bool, str)  # (success, message)

    def run(self):
        try:
            restart_camera_node()
            self.done.emit(True, "已重启")
        except Exception as e:
            self.done.emit(False, f"失败: {e}")


class SlotCameraOpenThread(QThread):
    """
    后台线程打开摄像头，不阻塞 UI
    为什么需要这个线程？
    - 摄像头打开可能耗时（特别是 USB 摄像头）
    - 如果在主线程打开，会卡住界面
    - 使用 QThread 在后台打开，完成后通过信号通知主线程
    """
    # 信号定义：打开成功或失败时发射
    opened = pyqtSignal(int, object)  # (slot_id, camera) - 成功时发射，传递槽位ID和摄像头对象
    failed = pyqtSignal(int, str)     # (slot_id, error)  - 失败时发射，传递槽位ID和错误信息

    def __init__(self, slot_id: int, camera: OpenCVCamera, camera_index: int, parent=None):
        """
        初始化线程
        参数：
            slot_id: 槽位ID（1, 2, 3）
            camera: 摄像头对象（OpenCVCamera 实例）
            camera_index: 摄像头设备索引（如 0, 1, 2...）
            parent: 父对象（Qt 对象树）
        """
        super().__init__(parent)
        self._slot_id = slot_id
        self._camera = camera
        self._camera_index = camera_index

    def run(self):
        """
        线程执行体：尝试打开摄像头
        这个方法在新线程中执行，不会阻塞主线程
        """
        # 优先 720p；不支持时 OpenCVCamera.open() 内部回退设备默认分辨率
        ok = self._camera.open(self._camera_index, width=1280, height=720, fps=30)
        if ok:
            # 打开成功，发射 opened 信号，传递槽位ID和摄像头对象
            self.opened.emit(self._slot_id, self._camera)
        else:
            # 打开失败，发射 failed 信号，传递槽位ID和错误信息
            self.failed.emit(self._slot_id, f"摄像头 {self._camera_index} 打开失败")


class ProductionManager(QObject):
    """
    产线管理器：模型单例 + 3 Worker + MQTT
    
    核心职责：
    1. 管理 YOLO 模型单例，三个槽位共享
    2. 管理三个槽位的 Worker 生命周期（启动、停止、参数更新）
    3. 支持普通摄像头和深度相机两种模式
    4. 解析 MQTT 指令，控制槽位的检测和扫码开关
    5. 发送检测结果到 MQTT
    
    信号说明：
    - slot_started: 槽位启动成功时发射，传递槽位ID和Worker对象
    - mqtt_cmd_received: 收到MQTT指令时发射，传递指令原文
    - detection_toggled: 检测开关变化时发射，传递槽位ID和开关状态
    - qr_toggled: 扫码开关变化时发射，传递槽位ID和开关状态
    """
    
    # Qt信号定义：用于与其他组件通信
    slot_started = pyqtSignal(int, object)  # (slot_id, worker) - 槽位启动成功
    mqtt_cmd_received = pyqtSignal(str)     # 收到的MQTT指令原文 - 用于UI显示
    detection_toggled = pyqtSignal(int, bool)  # (slot_id, enabled) - 检测开关变化
    qr_toggled = pyqtSignal(int, bool)         # (slot_id, enabled) - 扫码开关变化

    SLOT_COUNT = 3  # 总槽位数
    SLOT_MAP = {"xj1": 0, "xj2": 1, "xj3": 2}  # MQTT指令中的槽位名称到索引的映射

    def __init__(self, mqtt_client: Optional[MqttClient] = None):
        """
        初始化产线管理器
        参数：
            mqtt_client: MqttClient 实例，用于接收指令和发送结果
                       如果为None，则不处理MQTT通信
        """
        super().__init__()
        self._mqtt: Optional[MqttClient] = mqtt_client  # MQTT客户端引用
        self._model = None          # YOLO model 实例（共享）- 三个槽位共用同一个模型
        self._model_loaded = False  # 模型是否已加载
        self._model_path = ""       # 模型文件路径

        # 以下列表用于管理三个槽位的资源，索引0对应槽位1，索引1对应槽位2，索引2对应槽位3
        self._workers: list[InferenceWorker] = [None] * self.SLOT_COUNT  # 普通Worker列表
        self._depth_workers: list[Optional[DepthWorker]] = [None] * self.SLOT_COUNT  # 深度Worker列表
        self._cameras: list[Optional[BaseCamera]] = [None] * self.SLOT_COUNT  # 摄像头对象列表
        self._detectors: list[Optional[Detector]] = [None] * self.SLOT_COUNT  # 检测器列表
        self._running = [False] * self.SLOT_COUNT  # 槽位是否运行中
        self._depth_mode = [False] * self.SLOT_COUNT  # 槽位是否为深度模式
        self._pending_params: list[dict] = [{}] * self.SLOT_COUNT  # 待处理的参数（摄像头打开后使用）
        self._open_threads: list = [None] * self.SLOT_COUNT  # 摄像头打开线程列表

        # 回调函数：当检测结果准备好时调用
        self.on_detections: Optional[Callable] = None  # (slot_id, detections, qr_bindings)

        # 注册 MQTT 指令回调：当收到 ht/cmd/2 消息时调用 _parse_mqtt_cmd
        if self._mqtt:
            self._mqtt.set_prod_cmd_callback(self._parse_mqtt_cmd)

    # ============ 模型管理 ============
    def load_model(self, model_path: str) -> bool:
        """
        加载 YOLO 模型（单例模式）
        为什么是单例？
        - YOLO 模型通常很大（几十MB到几百MB）
        - 三个槽位共享一个模型可以节省内存和GPU显卡显存
        - 避免重复加载，提高启动速度
        
        参数：
            model_path: 模型文件路径（.pt格式）
        
        返回：
            bool: 加载成功返回True，失败返回False
        
        异常：
            FileNotFoundError: 模型文件不存在时抛出
        """
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"模型文件不存在: {model_path}")

        try:
            from ultralytics import YOLO
            self._model = YOLO(model_path)  # 加载YOLO模型

            # 强制使用 GPU（如果可用），避免静默回退到 CPU
            import torch
            if torch.cuda.is_available():
                self._model.to("cuda:0")
                print("[ProductionManager] YOLO 已加载到 GPU: cuda:0")
            else:
                print("[ProductionManager] 警告: CUDA 不可用，YOLO 使用 CPU")

            self._model_path = model_path
            self._model_loaded = True

            # 更新已有的检测器：如果槽位已经创建了检测器，需要更新它们的模型引用
            for det in self._detectors:
                if det is not None:
                    # set_model 更新检测器的模型和类别名称
                    det.set_model(self._model, self._model.names if hasattr(self._model, 'names') else {})
            return True
        except Exception as e:
            print(f"[ProductionManager] 模型加载失败: {e}")
            return False

    @property
    def model_loaded(self) -> bool:
        """模型是否已加载的属性，只读"""
        return self._model_loaded

    @property
    def model_path(self) -> str:
        """模型文件路径的属性，只读"""
        return self._model_path

    # ============ 槽位管理 ============
    def start_slot(self, slot_id: int, camera_index: int, params: dict) -> bool:
        """
        启动指定槽位（普通摄像头模式）
        这是一个异步操作：摄像头在后台线程打开，不会阻塞UI
        
        参数：
            slot_id: 槽位ID（1, 2, 3）
            camera_index: 摄像头设备索引（如0, 1, 2...）
            params: 检测参数字典，包含：
                - conf: 置信度阈值（0-1），默认0.5
                - iou: IOU阈值（0-1），默认0.45
                - inference_interval: 推理间隔（帧），默认1
                - qr_interval: 扫码间隔（帧），默认1
        
        返回：
            bool: 启动成功返回True，失败返回False
        
        工作流程：
        1. 检查槽位是否已运行，如果运行则先停止
        2. 检查模型是否已加载
        3. 保存参数到待处理列表
        4. 创建摄像头对象和打开线程
        5. 启动线程，线程完成后会调用 _on_slot_camera_opened 或 _on_slot_camera_failed
        """
        idx = slot_id - 1  # 转换为0-based索引
        if idx < 0 or idx >= self.SLOT_COUNT:
            return False

        if self._running[idx]:
            self.stop_slot(slot_id)  # 如果槽位已运行，先停止

        if not self._model_loaded:
            print(f"[ProductionManager] 槽位{slot_id}: 模型未加载")
            return False

        # 保存参数，等摄像头打开后使用（因为摄像头打开是异步的）
        self._pending_params[idx] = params

        # 异步打开摄像头：创建摄像头对象和线程
        camera = OpenCVCamera()
        thread = SlotCameraOpenThread(slot_id, camera, camera_index)
        # 连接信号：线程完成时调用对应的处理函数
        thread.opened.connect(self._on_slot_camera_opened)
        thread.failed.connect(self._on_slot_camera_failed)
        self._open_threads[idx] = thread
        thread.start()  # 启动线程，开始异步打开摄像头
        print(f"[ProductionManager] 槽位{slot_id}: 正在打开摄像头 {camera_index}...")
        return True

    def _on_slot_camera_opened(self, slot_id: int, camera):
        """
        摄像头打开成功后的处理
        这个方法由 SlotCameraOpenThread 的 opened 信号触发
        
        参数：
            slot_id: 槽位ID（1, 2, 3）
            camera: 已打开的摄像头对象
        
        工作流程：
        1. 保存摄像头对象到列表
        2. 创建检测器（共享YOLO模型）
        3. 创建Worker（用于视频预览和检测）
        4. 配置Worker参数（置信度、IOU、间隔等）
        5. 连接信号（检测结果、扫码结果）
        6. 启动预览（仅显示视频，不进行检测）
        7. 发射槽位启动信号
        """
        idx = slot_id - 1
        params = self._pending_params[idx]
        self._cameras[idx] = camera  # 保存摄像头对象

        # 创建检测器：共享YOLO模型，但每个槽位有自己的检测器实例
        # 为什么需要独立检测器？因为每个槽位可能有不同的检测参数
        detector = Detector(external_model=self._model)
        self._detectors[idx] = detector

        # 创建Worker：用于视频预览和检测
        worker = InferenceWorker()
        worker.set_camera(camera)           # 设置摄像头
        worker.set_detector(detector)       # 设置检测器
        worker.set_params(
            conf=params.get("conf", 0.5),           # 置信度阈值
            iou=params.get("iou", 0.45)              # IOU阈值
        )
        worker.set_inference_interval(params.get("inference_interval", 1))  # 推理间隔
        worker.set_qr_interval(params.get("qr_interval", 1))                # 扫码间隔
        worker.set_qr_slice(params.get("qr_slice_ms", 600))   # v1.2 时间片(缺省600=qr_decoder._DEFAULT_SLICE_MS；配置显式 0=旧行为)
        worker.set_qr_enabled(False)         # 初始禁用扫码
        worker.disable_inference()           # 初始禁用检测，只显示预览

        # 连接信号：当Worker有检测结果或扫码结果时，调用对应的处理函数
        # 使用lambda和默认参数s=slot_id是为了在回调中传递槽位ID
        worker.detections_ready.connect(
            lambda dets, s=slot_id: self._on_slot_detections(s, dets))
        worker.qr_results_ready.connect(
            lambda qrs, s=slot_id: self._on_slot_qr(s, qrs))

        self._workers[idx] = worker
        self._running[idx] = True
        worker.start_preview()  # 启动预览，开始显示视频流
        self.slot_started.emit(slot_id, worker)  # 发射信号，通知UI槽位已启动
        print(f"[ProductionManager] 槽位{slot_id}: 预览已启动")

    def _on_slot_camera_failed(self, slot_id: int, error: str):
        """
        摄像头打开失败后的处理
        这个方法由 SlotCameraOpenThread 的 failed 信号触发
        
        参数：
            slot_id: 槽位ID（1, 2, 3）
            error: 错误信息
        """
        print(f"[ProductionManager] 槽位{slot_id}: {error}")

    def start_slot_depth(self, slot_id: int, params: dict) -> bool:
        """
        以深度相机模式启动指定槽位（仅1号槽位）
        深度相机模式使用HP60C相机，通过ROS2获取RGB和深度图像
        
        参数：
            slot_id: 槽位ID（必须为1）
            params: 检测参数字典，包含：
                - conf: 置信度阈值（0-1），默认0.5
                - iou: IOU阈值（0-1），默认0.45
                - inference_interval: 推理间隔（帧），默认1
                - qr_interval: 扫码间隔（帧），默认1
        
        返回：
            bool: 启动成功返回True，失败返回False
        
        与普通模式的区别：
        - 不需要OpenCVCamera，使用ROS2订阅获取图像
        - 使用DepthWorker代替InferenceWorker
        - 可以获取深度信息（毫米单位）
        - 目前只支持1号槽位
        """
        idx = slot_id - 1
        if idx != 0:
            print(f"[ProductionManager] 深度模式仅支持槽位1")
            return False

        if self._running[idx]:
            self.stop_slot(slot_id)  # 如果槽位已运行，先停止

        if not self._model_loaded:
            print(f"[ProductionManager] 槽位{slot_id}: 模型未加载")
            return False

        # 创建检测器：共享YOLO模型
        detector = Detector(external_model=self._model)
        self._detectors[idx] = detector

        # 创建深度Worker：专门处理深度相机
        worker = DepthWorker()
        worker.set_detector(detector)
        worker.set_params(
            conf=params.get("conf", 0.5),           # 置信度阈值
            iou=params.get("iou", 0.45)              # IOU阈值
        )
        worker.set_inference_interval(params.get("inference_interval", 1))  # 推理间隔
        worker.set_qr_interval(params.get("qr_interval", 1))                # 扫码间隔
        worker.set_qr_slice(params.get("qr_slice_ms", 600))   # v1.2 时间片(缺省600=qr_decoder._DEFAULT_SLICE_MS；配置显式 0=旧行为)
        worker.set_qr_enabled(False)         # 初始禁用扫码
        worker.disable_inference()           # 初始禁用检测

        # 连接信号：当Worker有检测结果或扫码结果时，调用对应的处理函数
        worker.detections_ready.connect(
            lambda dets, s=slot_id: self._on_slot_detections(s, dets))
        worker.qr_results_ready.connect(
            lambda qrs, s=slot_id: self._on_slot_qr(s, qrs))

        self._depth_workers[idx] = worker
        self._depth_mode[idx] = True         # 标记为深度模式
        self._running[idx] = True
        worker.start_worker()                # 启动深度Worker
        self.slot_started.emit(slot_id, worker)  # 发射信号，通知UI槽位已启动
        print(f"[ProductionManager] 槽位{slot_id}: 深度相机模式已启动")
        return True

    def enable_detection(self, slot_id: int):
        """
        开启指定槽位的检测功能（YOLO检测 + 二维码扫描）
        这个方法同时开启检测和扫码功能
        
        参数：
            slot_id: 槽位ID（1, 2, 3）
        
        工作流程：
        1. 检查槽位是否在运行
        2. 根据槽位模式（普通或深度）调用对应Worker的方法
        3. 启用推理（YOLO检测）
        4. 启用扫码功能
        5. 发射检测开关变化信号
        """
        idx = slot_id - 1
        if 0 <= idx < self.SLOT_COUNT:
            # 根据槽位模式选择不同的Worker
            if self._depth_mode[idx] and self._depth_workers[idx]:
                # 深度模式：使用DepthWorker
                self._depth_workers[idx].enable_inference()  # 启用YOLO检测
                self._depth_workers[idx].set_qr_enabled(True)  # 启用二维码扫描
            elif self._workers[idx]:
                # 普通模式：使用InferenceWorker
                self._workers[idx].enable_inference()  # 启用YOLO检测
                self._workers[idx].set_qr_enabled(True)  # 启用二维码扫描
            self.detection_toggled.emit(slot_id, True)  # 发射信号，通知UI检测已开启
            print(f"[ProductionManager] 槽位{slot_id}: 检测已开启")

    def disable_detection(self, slot_id: int):
        """
        关闭指定槽位的检测功能（预览继续）
        这个方法同时关闭检测和扫码功能，但视频预览继续显示
        
        参数：
            slot_id: 槽位ID（1, 2, 3）
        
        工作流程：
        1. 检查槽位是否在运行
        2. 根据槽位模式（普通或深度）调用对应Worker的方法
        3. 禁用推理（YOLO检测）
        4. 禁用扫码功能
        5. 发射检测开关变化信号
        """
        idx = slot_id - 1
        if 0 <= idx < self.SLOT_COUNT:
            # 根据槽位模式选择不同的Worker
            if self._depth_mode[idx] and self._depth_workers[idx]:
                # 深度模式：使用DepthWorker
                self._depth_workers[idx].disable_inference()  # 禁用YOLO检测
                self._depth_workers[idx].set_qr_enabled(False)  # 禁用二维码扫描
            elif self._workers[idx]:
                # 普通模式：使用InferenceWorker
                self._workers[idx].disable_inference()  # 禁用YOLO检测
                self._workers[idx].set_qr_enabled(False)  # 禁用二维码扫描
            self.detection_toggled.emit(slot_id, False)  # 发射信号，通知UI检测已关闭
            print(f"[ProductionManager] 槽位{slot_id}: 检测已关闭")

    def stop_slot(self, slot_id: int):
        """
        停止指定槽位，释放所有资源
        这个方法会停止Worker、关闭摄像头、清理检测器
        
        参数：
            slot_id: 槽位ID（1, 2, 3）
        
        工作流程：
        1. 停止普通Worker（如果存在）
        2. 停止深度Worker（如果存在）
        3. 关闭摄像头（如果存在）
        4. 清理检测器引用
        5. 重置运行状态和深度模式标志
        """
        idx = slot_id - 1
        if idx < 0 or idx >= self.SLOT_COUNT:
            return

        print(f"[ProductionManager] 槽位{slot_id} 停止")

        # 停止普通Worker：调用stop方法，然后置为None释放资源
        if self._workers[idx]:
            self._workers[idx].stop()
            self._workers[idx] = None

        # 停止深度Worker：调用stop方法，然后置为None释放资源
        if self._depth_workers[idx]:
            self._depth_workers[idx].stop()
            self._depth_workers[idx] = None

        # 关闭摄像头：调用close方法释放设备资源
        if self._cameras[idx]:
            self._cameras[idx].close()
            self._cameras[idx] = None

        self._detectors[idx] = None  # 清理检测器引用
        self._running[idx] = False   # 标记为未运行
        self._depth_mode[idx] = False  # 重置深度模式标志
        print(f"[ProductionManager] 槽位 {slot_id} 停止")

    def stop_all(self):
        """停止所有槽位，遍历调用 stop_slot"""
        for i in range(1, self.SLOT_COUNT + 1):
            self.stop_slot(i)

    def is_running(self, slot_id: int) -> bool:
        """
        检查指定槽位是否正在运行
        
        参数：
            slot_id: 槽位ID（1, 2, 3）
        
        返回：
            bool: 槽位运行中返回True，否则返回False
        """
        idx = slot_id - 1
        if 0 <= idx < self.SLOT_COUNT:
            return self._running[idx]
        return False

    def update_slot_params(self, slot_id: int, params: dict):
        """
        更新运行中槽位的检测参数
        这个方法可以在槽位运行时动态调整参数
        
        参数：
            slot_id: 槽位ID（1, 2, 3）
            params: 要更新的参数字典，包含以下可选键：
                - conf: 置信度阈值（0-1）
                - iou: IOU阈值（0-1）
                - inference_interval: 推理间隔（帧）
                - qr_interval: 扫码间隔（帧）

        注意：只更新params中包含的参数，其他参数保持不变
        """
        idx = slot_id - 1
        if idx < 0 or idx >= self.SLOT_COUNT:
            return

        # 更新普通Worker的参数
        worker = self._workers[idx]
        if worker and self._running[idx]:
            if "conf" in params:
                worker.set_params(conf=params["conf"])  # 更新置信度阈值
            if "iou" in params:
                worker.set_params(iou=params["iou"])    # 更新IOU阈值
            if "inference_interval" in params:
                worker.set_inference_interval(int(params["inference_interval"]))  # 更新推理间隔
            if "qr_interval" in params:
                worker.set_qr_interval(int(params["qr_interval"]))  # 更新扫码间隔
            if "qr_slice_ms" in params:
                worker.set_qr_slice(int(params["qr_slice_ms"]))  # 更新 v1.2 时间片

        # 更新深度Worker的参数
        depth_worker = self._depth_workers[idx]
        if depth_worker and self._running[idx]:
            if "conf" in params:
                depth_worker.set_params(conf=params["conf"])  # 更新置信度阈值
            if "iou" in params:
                depth_worker.set_params(iou=params["iou"])    # 更新IOU阈值
            if "inference_interval" in params:
                depth_worker.set_inference_interval(int(params["inference_interval"]))  # 更新推理间隔
            if "qr_interval" in params:
                depth_worker.set_qr_interval(int(params["qr_interval"]))  # 更新扫码间隔
            if "qr_slice_ms" in params:
                depth_worker.set_qr_slice(int(params["qr_slice_ms"]))  # 更新 v1.2 时间片

    # ============ 检测结果处理 ============
    def _on_slot_detections(self, slot_id: int, detections: list):
        """
        某槽位检测结果回调
        这个方法由Worker的detections_ready信号触发
        
        参数：
            slot_id: 槽位ID（1, 2, 3）
            detections: 检测结果列表，每个元素包含：
                - bbox: 边界框 [x1, y1, x2, y2]
                - cls: 类别ID
                - cls_name: 类别名称
                - conf: 置信度
                - depth_mm: 深度值（仅深度模式）
        
        注意：这个方法目前为空，实际MQTT发送在帧级处理
        """
        pass

    def _on_slot_qr(self, slot_id: int, qr_results: list):
        """
        某槽位二维码结果回调
        这个方法由Worker的qr_results_ready信号触发
        
        参数：
            slot_id: 槽位ID（1, 2, 3）
            qr_results: 二维码结果列表
        
        注意：这个方法目前为空，实际处理在帧级
        """
        pass

    def send_detection_result(self, slot_id: int, detections: list, qr_bindings: dict):
        """
        发送检测结果到 MQTT ht/status/2
        这个方法将检测结果格式化并发送到MQTT，供其他系统使用
        
        参数：
            slot_id: 槽位ID（1, 2, 3）
            detections: 检测结果列表，每个元素包含：
                - bbox: 边界框 [x1, y1, x2, y2]
                - cls: 类别ID
                - cls_name: 类别名称
                - conf: 置信度
                - depth_mm: 深度值（仅深度模式）
            qr_bindings: 二维码绑定字典，键为检测结果索引，值为二维码内容
        
        发送格式：
        {
            "slot_id": 1,
            "detections": [
                {
                    "cls": "纸盒",
                    "center": [200, 150],
                    "qr": "QR123",
                    "depth_mm": 500  // 仅深度模式
                }
            ]
        }
        
        注意：最多发送10个检测结果，按置信度从高到低排序
        """
        # 检查MQTT连接状态
        if not self._mqtt or not self._mqtt.is_connected():
            return

        # 按置信度排序，最多10个：保留最可靠的检测结果
        sorted_dets = sorted(detections, key=lambda d: d.get("conf", 0), reverse=True)[:10]

        result_list = []
        for i, det in enumerate(sorted_dets):
            # 计算边界框中心点坐标
            bbox = det.get("bbox", [0, 0, 0, 0])
            cx = int((bbox[0] + bbox[2]) / 2)  # 中心点X坐标
            cy = int((bbox[1] + bbox[3]) / 2)  # 中心点Y坐标
            
            # 获取类别名称：优先使用cls_name，否则使用cls
            cls_name = det.get("cls_name", str(det.get("cls", "")))
            
            # 获取二维码内容：从绑定字典中获取
            qr = qr_bindings.get(i, "")
            
            # 获取深度值：仅深度模式有效
            depth = det.get("depth_mm", -1)
            
            # 构建结果项
            item = {"cls": cls_name, "center": [cx, cy], "qr": qr}
            if depth > 0:
                item["depth_mm"] = depth  # 只有有效深度值才添加
            result_list.append(item)

        # 发送结果到MQTT
        if result_list:
            self._mqtt.send_prod_result(slot_id, result_list)

    # ============ MQTT 指令解析 ============
    def _parse_mqtt_cmd(self, payload: str):
        """
        解析来自 ht/cmd/2 的 MQTT 指令
        这个方法由 MqttClient 的 prod_cmd 回调触发
        
        指令格式："<槽位> <检测开关> <扫码开关>"
        示例：
        - "xj1 1 0" = 槽位1 开启检测，关闭扫码
        - "xj2 0 1" = 槽位2 关闭检测，开启扫码
        - "all 1 1" = 所有槽位 开启检测和扫码
        
        参数：
            payload: MQTT消息内容（字符串）
        
        处理流程：
        1. 发射信号通知UI收到指令
        2. 解析指令格式（三段式）
        3. 验证参数有效性
        4. 确定目标槽位（单个或全部）
        5. 执行检测和扫码开关控制
        """
        # 发射信号，通知UI收到MQTT指令（用于显示）
        self.mqtt_cmd_received.emit(payload)
        
        # 解析指令：按空格分割成三部分
        parts = payload.strip().split()
        if len(parts) != 3:
            print(f"[ProductionManager] 指令格式错误: {payload}")
            return

        slot_cmd, detect_str, qr_str = parts

        # 验证参数格式：必须是数字
        try:
            detect_on = int(detect_str)  # 检测开关：0=关，1=开
            qr_on = int(qr_str)          # 扫码开关：0=关，1=开
        except ValueError:
            print(f"[ProductionManager] 参数格式错误: {payload}")
            return

        # 验证参数值：只能是0或1
        if detect_on not in (0, 1) or qr_on not in (0, 1):
            print(f"[ProductionManager] 参数值无效: {payload}")
            return

        # 确定目标槽位
        if slot_cmd == "all":
            slots = [1, 2, 3]  # 所有槽位
        elif slot_cmd in self.SLOT_MAP:
            slots = [self.SLOT_MAP[slot_cmd] + 1]  # 单个槽位，转换为1-based ID
        else:
            print(f"[ProductionManager] 未知槽位: {slot_cmd}")
            return

        # 执行指令：遍历目标槽位
        for slot_id in slots:
            # 控制检测开关
            if detect_on:
                self.enable_detection(slot_id)   # 开启检测
            else:
                self.disable_detection(slot_id)  # 关闭检测
            
            # 独立控制扫码开关（扫码可以独立于检测）
            idx = slot_id - 1
            
            # 普通Worker的扫码控制
            worker = self._workers[idx]
            if worker and self._running[idx]:
                worker.set_qr_enabled(bool(qr_on))  # 设置扫码开关
                self.qr_toggled.emit(slot_id, bool(qr_on))  # 发射信号通知UI
            
            # 深度Worker的扫码控制
            depth_worker = self._depth_workers[idx]
            if depth_worker and self._running[idx]:
                depth_worker.set_qr_enabled(bool(qr_on))  # 设置扫码开关
                self.qr_toggled.emit(slot_id, bool(qr_on))  # 发射信号通知UI

    # ============ 清理 ============
    def cleanup(self):
        """
        清理所有资源
        这个方法在程序退出或产线控制页面关闭时调用
        
        工作流程：
        1. 停止所有槽位（释放Worker、摄像头等资源）
        2. 清空模型引用（让Python垃圾回收器回收）
        3. 重置所有状态标志
        
        注意：调用cleanup后，需要重新load_model才能再次使用
        """
        self.stop_all()  # 停止所有槽位，释放所有资源
        self._model = None  # 释放模型引用
        self._model_loaded = False  # 重置模型加载状态
        self._depth_workers = [None] * self.SLOT_COUNT  # 清空深度Worker列表
