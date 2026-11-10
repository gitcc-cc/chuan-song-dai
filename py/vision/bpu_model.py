"""
BPU 模型封装模块（D-Robotics RDK X5）
====================================

这个文件的主要职责：
1. 把地瓜 X5 的 hbm_runtime 包装成 ultralytics.YOLO 同款接口
2. 提供统一的检测调用方式（__call__），返回伪 Result 对象
3. 内置官方预处理（letterbox + NV12）和后处理（topk + NMS + 坐标还原）

这样上层代码（Detector / ProductionManager / Worker）不需要改动，
一份代码同时支持 .pt（笔记本）和 .bin（RDK BPU）两种模型。

适用模型：YOLOv10 裁剪 raw 头版（输出 [1,6,8400]：4 bbox + 2 cls），
input_type_rt=nv12，letterbox 640×640，坐标为 letterbox 空间 xyxy。

使用方式：
    # 仅在 RDK 板上可用（笔记本没有 hbm_runtime，构造时会报错）
    model = BpuModel("/path/model.bin")
    results = model(frame, conf=0.5, iou=0.45)
    for det in results[0].boxes:
        print(det["bbox"], det["conf"], det["cls_name"])

依赖关系：
    - hbm_runtime：RDK 板上 /usr/local/lib/python3.10/dist-packages/
      （conda prod 环境需要 PYTHONPATH 指到该目录，run_rdk.sh 已处理）
    - cv2 / numpy：基础依赖

注意：这个模块不直接处理 PyQt6 的 GUI 元素，只负责模型推理。
"""
import os
from typing import List, Dict, Any, Optional, Tuple

import numpy as np
import cv2

# hbm_runtime 的安装位置（RDK 板系统目录）
_SYS_DIST_PACKAGES = "/usr/local/lib/python3.10/dist-packages"


def _import_hbm_runtime():
    """
    惰性导入 hbm_runtime。

    为什么惰性导入？
    - hbm_runtime 只存在于 RDK 板上
    - 笔记本 GUI 启动时如果本模块在顶层被 import 会直接崩
    - 只在用户真的选择 .bin 模型时才尝试导入

    返回:
        hbm_runtime 模块对象

    异常:
        ImportError: 找不到 hbm_runtime（通常在笔记本上运行时出现）
    """
    try:
        import hbm_runtime
        return hbm_runtime
    except ImportError:
        # 保险起见把系统 dist-packages 挂到 sys.path 再试一次
        import sys
        if _SYS_DIST_PACKAGES not in sys.path:
            sys.path.append(_SYS_DIST_PACKAGES)
        try:
            import hbm_runtime
            return hbm_runtime
        except ImportError as e:
            raise RuntimeError(
                "hbm_runtime 不可用：BPU 模型(.bin)只能在 RDK 板上运行。\n"
                f"内部错误: {e}"
            ) from e


class _FakeArray:
    """
    伪张量容器，模拟 ultralytics 结果中 .cpu().numpy() 的调用链。

    ultralytics 返回 torch 张量，Detector.infer() 用 .cpu().numpy() 取
    numpy 数组；BPU 直接出 numpy，包一层让调用链完全一致。
    """
    def __init__(self, arr: np.ndarray):
        self._arr = arr

    def cpu(self):
        return self  # numpy 没有 GPU/CPU 之分，直接返回自己

    def numpy(self) -> np.ndarray:
        return self._arr

    def __len__(self):
        return len(self._arr)


class _FakeBoxes:
    """
    伪检测框集合，字段与 ultralytics Results.boxes 对齐：
    - xyxy: (N, 4) 边界框坐标
    - conf: (N,) 置信度
    - cls:  (N,) 类别 ID
    """
    def __init__(self, xyxy: np.ndarray, conf: np.ndarray, cls: np.ndarray):
        self.xyxy = _FakeArray(xyxy)
        self.conf = _FakeArray(conf)
        self.cls = _FakeArray(cls)

    def __len__(self):
        return len(self.xyxy)


class _FakeResult:
    """伪单帧结果，只需要 .boxes 字段（Detector.infer 只消费它）。"""
    def __init__(self, boxes: Optional[_FakeBoxes]):
        self.boxes = boxes


class BpuModel:
    """
    BPU 模型类（可调用对象，与 ultralytics.YOLO 用法对齐）

    用法：
        model = BpuModel("model.bin")
        results = model(frame, conf=0.5, iou=0.45, classes=None, max_det=300)

    属性:
        model_path: 模型文件路径
        names: 类别名称字典 {0: '圆', 1: '方', ...}
        H / W: 模型输入高宽（默认 640）

    内部流程（与官方 /app/pydev_demo/02_detection_sample 一致）：
        1. letterbox 缩放原图到 640×640（灰边填充，等比例）
        2. BGR 转 NV12（Y 平面 + 交错 UV 平面）
        3. hbm_runtime.run() BPU 推理
        4. 输出 [1,6,8400] → 拆 bbox/score，逐 anchor 取最大置信度
        5. 置信度过滤 + NMS 去重 + max_det 截断
        6. letterbox 空间坐标反算回原图坐标
    """

    def __init__(self, model_path: str, names: Optional[List[str]] = None):
        """
        初始化 BPU 模型。

        参数：
            model_path: str
                .bin 模型文件路径（RDK 板上的绝对路径）

            names: list[str] 或 None
                类别名称列表。None 时从产线配置 bpu_names 读取，
                配置也没有则用 ["圆", "方"] 兜底。

        异常：
            FileNotFoundError: 模型文件不存在
            RuntimeError: hbm_runtime 不可用（笔记本上运行）
        """
        hbm_runtime = _import_hbm_runtime()

        if not os.path.exists(model_path):
            raise FileNotFoundError(f"模型文件不存在: {model_path}")

        self.model_path = model_path

        # 加载模型（这一步会做 BPU 上的模型初始化）
        self._rt = hbm_runtime.HB_HBMRuntime(model_path)
        self._model_name = self._rt.model_names[0]
        self._input_name = self._rt.input_names[self._model_name][0]
        self._out_name = self._rt.output_names[self._model_name][0]

        # 输入布局 [1,3,640,640]（nv12 模型运行时实际喂 (1,H*1.5,W,1)，
        # 高宽从 dim[2]/dim[3] 取）
        shape = self._rt.input_shapes[self._model_name][self._input_name]
        self.H = int(shape[2])
        self.W = int(shape[3])

        # 类别名称：参数传入 > 产线配置 > 默认
        if names is None:
            try:
                from core.config import get_production_config
                names = get_production_config().get("bpu_names") or ["圆", "方"]
            except Exception:
                names = ["圆", "方"]
        self.names = {i: str(n) for i, n in enumerate(names)}

        # 暖机：先跑一次全黑图，把运行时初始化开销消耗在这里
        # （否则第一帧真实推理会慢很多）
        self._warmup()

    # ============ 预处理 ============
    def _letterbox(self, frame: np.ndarray) -> np.ndarray:
        """
        等比例缩放 + 灰边填充到模型输入尺寸（官方 resized_image type=1 逻辑）。

        返回:
            (H, W, 3) uint8 图像
        """
        h, w = frame.shape[:2]
        scale = min(self.W / w, self.H / h)
        nw, nh = int(w * scale), int(h * scale)
        resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_NEAREST)

        pad_w = self.W - nw
        pad_h = self.H - nh
        left, right = pad_w // 2, pad_w - pad_w // 2
        top, bottom = pad_h // 2, pad_h - pad_h // 2
        return cv2.copyMakeBorder(
            resized, top, bottom, left, right,
            borderType=cv2.BORDER_CONSTANT,
            value=(127, 127, 127),
        )

    def _bgr_to_nv12(self, bgr: np.ndarray) -> np.ndarray:
        """
        BGR 图像转 NV12 连续缓冲（官方 preprocess.bgr_to_nv12_planes 折叠版）。

        返回:
            (1, H*1.5, W, 1) uint8，Y 平面在前 + 交错 UV 在后
        """
        h, w = bgr.shape[:2]
        area = h * w
        yuv420p = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420).reshape(-1)

        y = yuv420p[:area]
        u = yuv420p[area:area + area // 4]
        v = yuv420p[area + area // 4:]

        # UV 交错：NV12 的 UV 平面是 U0,V0,U1,V1,...
        uv = np.empty(area // 2, dtype=np.uint8)
        uv[0::2] = u
        uv[1::2] = v

        nv12 = np.concatenate([y, uv])
        return nv12.reshape((1, h * 3 // 2, w, 1)).astype(np.uint8)

    def _preprocess(self, frame: np.ndarray) -> np.ndarray:
        """完整预处理：letterbox + NV12。"""
        return self._bgr_to_nv12(self._letterbox(frame))

    # ============ 后处理 ============
    @staticmethod
    def _scale_coords_back(xyxy: np.ndarray, img_w: int, img_h: int,
                           input_w: int, input_h: int) -> np.ndarray:
        """
        letterbox 空间坐标反算回原图坐标（官方 scale_coords_back 逻辑）。
        """
        scale = min(input_w / img_w, input_h / img_h)
        pad_w = (input_w - img_w * scale) / 2
        pad_h = (input_h - img_h * scale) / 2
        xyxy[:, [0, 2]] = (xyxy[:, [0, 2]] - pad_w) / scale
        xyxy[:, [1, 3]] = (xyxy[:, [1, 3]] - pad_h) / scale

        # 裁剪到图像范围内
        xyxy[:, [0, 2]] = np.clip(xyxy[:, [0, 2]], 0, img_w)
        xyxy[:, [1, 3]] = np.clip(xyxy[:, [1, 3]], 0, img_h)
        return xyxy

    def _infer_bpu(self, nv12: np.ndarray) -> np.ndarray:
        """
        执行 BPU 推理，返回原始输出。

        返回:
            (6, N) float32 数组：0:4 行是 bbox（xyxy，letterbox 空间），
            4: 行是各类别分数

        异常:
            RuntimeError: 输出是量化 int8（当前模型是 NONE 类型，float 输出）
        """
        outs = self._rt.run({self._model_name: {self._input_name: nv12}})
        raw = np.asarray(outs[self._model_name][self._out_name])

        # 量化输出需要反量化（/app/pydev_demo/utils/postprocess_utils.py 的做法）
        if not np.issubdtype(raw.dtype, np.floating):
            q = self._rt.output_quants[self._model_name].get(self._out_name)
            if q is None or q.scale is None:
                raise RuntimeError(f"BPU 输出为量化格式 ({raw.dtype})，且无量化参数可反算")
            scale = np.asarray(q.scale, dtype=np.float32).reshape(-1, 1)
            # 逐通道 scale（axis 0 = 6 行），排布与输出 reshape 后一致
            scale = scale.reshape(-1, 1)
            raw = raw.astype(np.float32) * scale
        raw = raw.reshape(6, -1)
        return raw

    # ============ 主调用（对齐 ultralytics.YOLO.__call__） ============
    def __call__(self, frame: np.ndarray,
                 conf: float = 0.5,
                 iou: float = 0.45,
                 classes: Optional[List[int]] = None,
                 max_det: int = 300,
                 verbose: bool = False,
                 device: str = None,  # noqa: 兼容调用方传入，BPU 忽略
                 **_kwargs) -> List[_FakeResult]:
        """
        对单帧 BGR 图像推理，返回伪 ultralytics 结果列表。

        参数:
            frame: (H, W, 3) uint8 BGR 图像
            conf: 置信度阈值
            iou: NMS IoU 阈值
            classes: 只保留这些类别 ID（None = 全部）
            max_det: 最多返回的框数
            其余参数（verbose/device 等）与 ultralytics 接口兼容，被忽略。

        返回:
            [ _FakeResult ]，单帧列表（和 ultralytics 一致）

        异常:
            RuntimeError: 模型未加载（warmup 前 frame 为 None 等）
        """
        # 1. 前处理
        nv12 = self._preprocess(frame)

        # 2. BPU 推理
        raw = self._infer_bpu(nv12)   # (6, 8400)

        # 3. 逐 anchor 取最大分数 + 类别
        scores = raw[4:]              # (C, N)
        cls_idx = scores.argmax(0)    # (N,)
        confs = scores.max(0)         # (N,)

        # 4. 阈值过滤 + 类别过滤
        n = len(confs)
        mask = confs >= conf
        if classes is not None:
            mask &= np.isin(cls_idx, np.asarray(classes))
        idx = np.nonzero(mask)[0]
        if idx.size == 0:
            return [_FakeResult(None)]

        # 5. NMS 去重（同一物品被多个相邻 anchor 各报一框）
        boxes = raw[0:4, idx].T            # (M, 4) xyxy，letterbox 空间
        keep_conf = confs[idx]
        keep_cls = cls_idx[idx]
        # cv2.dnn.NMSBoxes 需要 [x, y, w, h]
        wh = np.stack([boxes[:, 0], boxes[:, 1],
                       boxes[:, 2] - boxes[:, 0],
                       boxes[:, 3] - boxes[:, 1]], axis=1).astype(np.float32)
        idxs = cv2.dnn.NMSBoxes(
            bboxes=wh.tolist(), scores=keep_conf.tolist(),
            score_threshold=conf, nms_threshold=iou,
        )
        if len(idxs) > 0:
            idxs = np.asarray(idxs).flatten()
        else:
            return [_FakeResult(None)]

        # 6. max_det 截断（按置信度排序留最高的）
        order = np.argsort(-keep_conf[idxs])[:max_det]
        idxs = idxs[order]

        # 7. 坐标反算回原图
        img_h, img_w = frame.shape[:2]
        xyxy = self._scale_coords_back(
            boxes[idxs].copy(), img_w, img_h, self.W, self.H
        )

        result_cls = keep_cls[idxs].astype(int)
        result_conf = keep_conf[idxs]

        return [_FakeResult(_FakeBoxes(xyxy, result_conf, result_cls))]

    # ============ 兼容 ultralytics.YOLO 的部分属性 ============
    def to(self, device: str):
        """兼容调用方 .to("cuda:0")：BPU 无需搬运，空操作。"""
        return self

    def _warmup(self):
        """跑一次全黑图，预热 BPU 运行时（把初始化开销提前吃掉）。"""
        try:
            dummy = np.zeros((self.H, self.W, 3), dtype=np.uint8)
            self(dummy, conf=0.99, max_det=1)
        except Exception as e:
            print(f"[BpuModel] 暖机警告（不影响后续使用）: {e}")

    def __del__(self):
        """明确释放 hbm_runtime 模型资源。"""
        try:
            if getattr(self, "_rt", None) is not None:
                self._rt = None
        except Exception:
            # 析构路径不做任何可能抛异常的操作
            pass
