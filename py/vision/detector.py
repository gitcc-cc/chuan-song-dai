"""
YOLO 检测器封装
==============
基于 Ultralytics YOLO，自动选择 GPU/CPU。
"""
import os
from typing import List, Dict, Any, Optional

import numpy as np

try:
    import torch
    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False

try:
    from ultralytics import YOLO
    _HAS_ULTRALYTICS = True
except ImportError:
    _HAS_ULTRALYTICS = False


class Detector:
    """YOLO 检测器"""

    def __init__(self, model_path: str = "", device: str = ""):
        """
        参数:
            model_path: .pt 模型文件路径
            device: 推理设备，空字符串=自动选择
                   "cpu" / "cuda:0" / "cuda:1" / "mps" 等
        """
        if not _HAS_ULTRALYTICS:
            raise RuntimeError("未安装 ultralytics: pip install ultralytics")

        self.model_path = model_path
        self.model = None
        self.names = {}
        self._device = device or self._auto_device()

        if model_path:
            self.load(model_path)

    def _auto_device(self) -> str:
        """自动选择设备：优先 CUDA，否则 CPU"""
        if _HAS_TORCH and torch.cuda.is_available():
            return "cuda:0"
        return "cpu"

    @property
    def device(self) -> str:
        return self._device

    @device.setter
    def device(self, value: str):
        self._device = value
        if self.model is not None:
            self.model.to(value)

    def load(self, model_path: str) -> bool:
        """
        加载模型
        返回: 是否成功
        """
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"模型文件不存在: {model_path}")

        self.model_path = model_path
        self.model = YOLO(model_path)
        self.model.to(self._device)
        self.names = self.model.names if hasattr(self.model, 'names') else {}
        return True

    def infer(self, frame: np.ndarray,
              conf_threshold: float = 0.5,
              iou_threshold: float = 0.45,
              classes: Optional[List[int]] = None,
              max_det: int = 300) -> List[Dict[str, Any]]:
        """
        单帧推理
        参数:
            frame: BGR 格式 numpy 数组 (H, W, 3)
            conf_threshold: 置信度阈值
            iou_threshold: NMS IoU 阈值
            classes: 只检测指定类别 ID 列表，None=全部
            max_det: 最大检测框数
        返回:
            列表，每项: {"bbox": [x1,y1,x2,y2], "conf": float, "cls": int, "cls_name": str}
        """
        if self.model is None:
            return []

        # Ultralytics 推理
        results = self.model(
            frame,
            conf=conf_threshold,
            iou=iou_threshold,
            classes=classes,
            max_det=max_det,
            verbose=False,
            save=False,
            device=self._device
        )

        detections = []
        if results and len(results) > 0:
            r = results[0]
            if r.boxes is not None and len(r.boxes) > 0:
                boxes = r.boxes.xyxy.cpu().numpy()   # (N, 4) xyxy
                confs = r.boxes.conf.cpu().numpy()   # (N,)
                clss  = r.boxes.cls.cpu().numpy().astype(int)  # (N,)

                for bbox, conf, cls in zip(boxes, confs, clss):
                    detections.append({
                        "bbox": bbox.tolist(),      # [x1, y1, x2, y2]
                        "conf": float(conf),
                        "cls": int(cls),
                        "cls_name": self.names.get(int(cls), str(int(cls)))
                    })

        return detections

    def warmup(self, input_size: tuple = (640, 640)) -> None:
        """预热模型（首次推理较慢，建议启动时调用）"""
        if self.model is None:
            return
        dummy = np.zeros((input_size[1], input_size[0], 3), dtype=np.uint8)
        _ = self.infer(dummy, conf_threshold=0.25)

    def __del__(self):
        # 清理
        self.model = None