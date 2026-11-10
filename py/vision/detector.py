"""
YOLO 检测器封装模块
===================

这个文件的主要职责：
1. 封装 Ultralytics YOLO 模型，提供统一的检测接口
2. 自动选择计算设备（GPU/CPU），优化推理性能
3. 处理模型加载、预热、推理等核心功能
4. 为上层应用（如摄像头页面）提供简单的检测调用方式

使用方式：
    # 创建检测器实例
    detector = Detector(model_path="yolov8n.pt")
    
    # 或者使用共享模型（多个检测器共用一个模型）
    shared_model = YOLO("yolov8n.pt")
    detector1 = Detector(external_model=shared_model)
    detector2 = Detector(external_model=shared_model)
    
    # 进行检测
    detections = detector.infer(frame)

依赖关系：
    - ultralytics：YOLO 模型库
    - torch：PyTorch 深度学习框架（用于 GPU 加速）
    - numpy：数组处理

注意：这个模块不直接处理 PyQt6 的 GUI 元素，只负责模型推理。
      GUI 相关的集成在上层模块（如 camera_page.py）中实现。
"""
import os
from typing import List, Dict, Any, Optional

import numpy as np

# 尝试导入 PyTorch，用于 GPU 加速
# 如果导入失败，_HAS_TORCH 会设为 False，后续会使用 CPU
try:
    import torch
    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False

# 尝试导入 ultralytics YOLO 库
# 这是 YOLO 模型的官方实现库
try:
    from ultralytics import YOLO
    _HAS_ULTRALYTICS = True
except ImportError:
    _HAS_ULTRALYTICS = False


class Detector:
    """
    YOLO 检测器类
    
    这个类封装了 YOLO 模型的所有功能，提供：
    1. 模型加载和管理
    2. 自动设备选择（GPU/CPU）
    3. 单帧图像推理
    4. 模型预热（优化首次推理速度）
    
    属性：
        model_path: 模型文件路径
        model: YOLO 模型实例
        names: 类别名称字典（如 {0: 'person', 1: 'bicycle', ...}）
        device: 推理设备（'cpu' 或 'cuda:0'）
    
    使用示例：
        # 创建检测器
        detector = Detector(model_path="yolov8n.pt")
        
        # 进行检测
        frame = cv2.imread("image.jpg")  # BGR 格式图像
        detections = detector.infer(frame)
        
        for det in detections:
            print(f"检测到 {det['cls_name']}，置信度 {det['conf']:.2f}")
    """

    def __init__(self, model_path: str = "", device: str = "", external_model=None):
        """
        初始化 YOLO 检测器
        
        参数：
            model_path: str
                模型文件路径，通常是 .pt 文件（PyTorch 模型格式）
                例如："yolov8n.pt" 或 "/path/to/model.pt"
                如果为空，则需要后续调用 load() 方法加载模型
                
            device: str
                推理设备，可以是以下值：
                - "" (空字符串)：自动选择最佳设备
                - "cpu"：强制使用 CPU（较慢，但兼容性好）
                - "cuda:0"：使用第一个 NVIDIA GPU（需要 CUDA 支持）
                - "cuda:1"：使用第二个 NVIDIA GPU（如果有）
                - "mps"：使用 Apple Silicon 的 Metal Performance Shaders（Mac）
                
            external_model: YOLO 实例 或 None
                外部传入的 YOLO 模型实例，用于共享模式
                - 传入 None：默认行为，会加载 model_path 指定的模型
                - 传入 YOLO 实例：跳过模型加载，直接使用传入的实例
                - 优点：多个检测器可以共享同一个模型，节省内存
                
        异常：
            RuntimeError: 如果 ultralytics 库未安装
            
        注意：
            - 这个方法不会立即加载模型，除非提供了 model_path 或 external_model
            - 模型加载可能需要几秒钟，取决于模型大小和磁盘速度
            - GPU 加速需要安装 CUDA 和对应的 PyTorch 版本
        """
        # ultralytics 未安装时不 raise（BPU/无 YOLO 环境下 GUI 照常起，
        # infer() 返回空，扫码等功能不受影响）
        self.available = _HAS_ULTRALYTICS

        # 保存模型路径，用于后续加载
        self.model_path = model_path
        
        # YOLO 模型实例，初始为 None，会在 load() 方法中加载
        self.model = None
        
        # 类别名称字典，格式：{类别ID: 类别名称}
        # 例如：{0: 'person', 1: 'bicycle', 2: 'car', ...}
        self.names = {}
        
        # 推理设备，如果未指定则自动选择
        self._device = device or self._auto_device()

        # 根据参数决定如何初始化模型
        if external_model is not None:
            # 使用外部传入的模型（共享模式）
            self.model = external_model
            # 从外部模型获取类别名称
            self.names = external_model.names if hasattr(external_model, 'names') else {}
        elif model_path:
            # 如果提供了模型路径，立即加载模型
            self.load(model_path)

    def _auto_device(self) -> str:
        """
        自动选择最佳推理设备
        
        设备选择优先级：
        1. 如果 PyTorch 已安装且 CUDA 可用，使用第一个 GPU（cuda:0）
        2. 否则使用 CPU
        
        返回：
            str: 设备名称，如 "cuda:0" 或 "cpu"
            
        注意：
            - CUDA 需要 NVIDIA GPU 和对应的 CUDA 驱动
            - GPU 推理速度通常比 CPU 快 10-100 倍
            - 如果没有 GPU，CPU 也可以正常工作，只是较慢
        """
        # 检查 PyTorch 是否可用且 CUDA 是否可用
        if _HAS_TORCH and torch.cuda.is_available():
            return "cuda:0"  # 使用第一个 NVIDIA GPU
        return "cpu"  # 回退到 CPU

    @property
    def device(self) -> str:
        """
        获取当前推理设备
        
        返回：
            str: 设备名称，如 "cuda:0" 或 "cpu"
        """
        return self._device

    @device.setter
    def device(self, value: str):
        """
        设置推理设备
        
        参数：
            value: str
                设备名称，如 "cuda:0" 或 "cpu"
                
        注意：
            - 如果模型已加载，会自动将模型移动到新设备
            - 切换设备可能需要几秒钟，取决于模型大小
            - GPU 到 CPU 的切换通常很快，但 CPU 到 GPU 可能较慢
        """
        self._device = value
        # 如果模型已加载，将其移动到新设备
        if self.model is not None:
            self.model.to(value)

    def set_model(self, model, names: dict = None):
        """
        设置外部模型实例（共享模式）
        
        参数：
            model: YOLO 实例
                外部传入的 YOLO 模型实例
                
            names: dict 或 None
                类别名称字典，格式：{类别ID: 类别名称}
                如果为 None，会尝试从模型中获取
                
        使用场景：
            - 多个检测器需要共享同一个模型实例
            - 节省内存和加载时间
            - 例如：三个摄像头槽位共享同一个 YOLO 模型
        """
        self.model = model
        # 如果没有提供 names，尝试从模型中获取
        self.names = names if names is not None else (model.names if hasattr(model, 'names') else {})

    def load(self, model_path: str) -> bool:
        """
        加载 YOLO 模型文件
        
        参数：
            model_path: str
                模型文件路径，通常是 .pt 文件
                例如："yolov8n.pt" 或 "/path/to/model.pt"
                
        返回：
            bool: 加载是否成功（目前总是返回 True）
            
        异常：
            FileNotFoundError: 如果模型文件不存在
            
        注意：
            - 首次加载可能需要几秒钟，取决于模型大小
            - 模型会自动加载到指定设备（GPU 或 CPU）
            - 加载后会自动获取类别名称信息
        """
        # 检查模型文件是否存在
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"模型文件不存在: {model_path}")

        # 保存模型路径
        self.model_path = model_path

        # .bin → BPU 模型（RDK 板上 hbm_runtime）；.pt → ultralytics
        if model_path.endswith(".bin"):
            from vision.bpu_model import BpuModel
            self.model = BpuModel(model_path)  # 类别名从产线配置 bpu_names 读取
            self.names = dict(self.model.names)
            self.available = True  # BPU 路径不依赖 ultralytics
            print(f"[Detector] BPU 模型已加载: {model_path}")
            return True

        # 加载 YOLO 模型（这会从磁盘读取模型文件）
        if not self.available:
            print("[Detector] 警告: ultralytics 未安装，无法加载 .pt 模型")
            return []
        self.model = YOLO(model_path)
        
        # 将模型移动到指定设备（GPU 或 CPU）
        self.model.to(self._device)
        
        # 获取类别名称字典
        # 例如：{0: 'person', 1: 'bicycle', 2: 'car', ...}
        self.names = self.model.names if hasattr(self.model, 'names') else {}
        return True

    def infer(self, frame: np.ndarray,
              conf_threshold: float = 0.5,
              iou_threshold: float = 0.45,
              classes: Optional[List[int]] = None,
              max_det: int = 300) -> List[Dict[str, Any]]:
        """
        对单帧图像进行目标检测推理
        
        参数：
            frame: np.ndarray
                输入图像，格式为 BGR（OpenCV 默认格式）
                形状：(高度, 宽度, 3)，例如 (480, 640, 3)
                数据类型：uint8（0-255）
                
            conf_threshold: float
                置信度阈值，范围 0.0-1.0
                只保留置信度高于此值的检测结果
                - 0.5：默认值，平衡精确率和召回率
                - 0.3：更多检测结果，但可能有更多误检
                - 0.7：更少检测结果，但更准确
                
            iou_threshold: float
                NMS（非极大值抑制）的 IoU 阈值，范围 0.0-1.0
                用于去除重叠的检测框
                - 0.45：默认值
                - 值越小，去除的重叠框越多
                
            classes: List[int] 或 None
                只检测指定类别的 ID 列表
                - None：检测所有类别
                - [0, 1]：只检测类别 0 和 1（如 person 和 bicycle）
                
            max_det: int
                最大检测框数量，防止检测过多目标
                默认 300，对于大多数场景足够
                
        返回：
            List[Dict[str, Any]]
                检测结果列表，每个检测结果是一个字典：
                {
                    "bbox": [x1, y1, x2, y2],  # 边界框坐标（像素）
                    "conf": float,              # 置信度（0.0-1.0）
                    "cls": int,                 # 类别 ID
                    "cls_name": str             # 类别名称（如 "person"）
                }
                
        使用示例：
            # 检测图像中的所有人
            detections = detector.infer(frame, classes=[0], conf_threshold=0.5)
            
            # 绘制检测结果
            for det in detections:
                x1, y1, x2, y2 = det["bbox"]
                cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 0), 2)
        """
        # 如果模型未加载，返回空列表
        if self.model is None:
            return []

        # 调用 YOLO 模型进行推理
        # Ultralytics 库会自动处理预处理、推理和后处理
        results = self.model(
            frame,
            conf=conf_threshold,
            iou=iou_threshold,
            classes=classes,
            max_det=max_det,
            verbose=False,  # 不输出日志信息
            save=False,     # 不保存检测结果图像
            device=self._device
        )

        # 初始化检测结果列表
        detections = []
        
        # 检查是否有检测结果
        if results and len(results) > 0:
            # 获取第一个图像的结果（单帧推理只有一个结果）
            r = results[0]
            
            # 检查是否有检测框
            if r.boxes is not None and len(r.boxes) > 0:
                # 提取边界框坐标（xyxy 格式：左上角 x1,y1，右下角 x2,y2）
                boxes = r.boxes.xyxy.cpu().numpy()   # 形状：(N, 4)
                
                # 提取置信度分数
                confs = r.boxes.conf.cpu().numpy()   # 形状：(N,)
                
                # 提取类别 ID
                clss  = r.boxes.cls.cpu().numpy().astype(int)  # 形状：(N,)
                
                # 遍历每个检测结果，转换为字典格式
                for bbox, conf, cls in zip(boxes, confs, clss):
                    detections.append({
                        "bbox": bbox.tolist(),      # 转换为 Python 列表 [x1, y1, x2, y2]
                        "conf": float(conf),         # 转换为 Python 浮点数
                        "cls": int(cls),             # 转换为 Python 整数
                        "cls_name": self.names.get(int(cls), str(int(cls)))  # 获取类别名称
                    })

        return detections

    def warmup(self, input_size: tuple = (640, 640)) -> None:
        """
        模型预热，优化首次推理速度
        
        参数：
            input_size: tuple
                预热图像尺寸，格式：(宽度, 高度)
                默认 (640, 640)，这是 YOLO 的标准输入尺寸
                
        注意：
            - 首次推理通常较慢，因为需要初始化 GPU 内存和编译模型
            - 预热可以提前完成这些初始化工作
            - 建议在程序启动时调用一次
            - 预热使用一张全黑图像，不影响实际检测结果
            
        使用场景：
            # 在程序启动时调用
            detector = Detector(model_path="yolov8n.pt")
            detector.warmup()  # 预热，后续推理会更快
        """
        # 如果模型未加载，直接返回
        if self.model is None:
            return
        
        # 创建一张全黑的虚拟图像（用于预热）
        # 形状：(高度, 宽度, 3)，数据类型：uint8
        dummy = np.zeros((input_size[1], input_size[0], 3), dtype=np.uint8)
        
        # 使用低置信度阈值进行推理（确保至少有一个检测结果）
        # 这样可以充分预热模型
        _ = self.infer(dummy, conf_threshold=0.25)

    def __del__(self):
        """
        析构函数，对象销毁时自动调用
        
        用途：
            - 清理模型资源，释放 GPU 内存
            - Python 垃圾回收器会自动调用此方法
            
        注意：
            - 通常不需要手动调用，Python 会自动管理
            - 在 PyQt6 中，当窗口关闭时会自动调用
        """
        # 将模型设为 None，释放资源
        self.model = None