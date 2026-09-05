"""
摄像头抽象层
===========
基于 OpenCV VideoCapture，统一接口，便于后续扩展深度相机。
使用 pyusbcameraindex 快速枚举 DirectShow 设备（毫秒级，正确返回设备名+index）。
"""
from abc import ABC, abstractmethod
from typing import Tuple, Optional, List, Dict
import cv2
import numpy as np


class BaseCamera(ABC):
    """摄像头抽象基类"""

    @abstractmethod
    def open(self, index: int, **kwargs) -> bool:
        """打开摄像头"""
        pass

    @abstractmethod
    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        """读取一帧: (success, frame_bgr)"""
        pass

    @abstractmethod
    def close(self) -> None:
        """关闭摄像头"""
        pass

    @abstractmethod
    def is_opened(self) -> bool:
        """是否已打开"""
        pass

    @abstractmethod
    def get_fps(self) -> float:
        """获取帧率"""
        pass

    @abstractmethod
    def get_resolution(self) -> Tuple[int, int]:
        """获取分辨率 (width, height)"""
        pass

    @staticmethod
    def list_cameras(max_test: int = 10) -> List[Dict]:
        """
        枚举系统摄像头设备。
        优先用 pyusbcameraindex（DirectShow API，毫秒级，正确返回设备名+index）。
        回退到 WMI + OpenCV 探测。
        返回: [{"name": "设备名", "index": OpenCV索引}, ...]
        """
        # 方案 1：pyusbcameraindex（最快、最准确）
        try:
            from pyusbcameraindex import enumerate_usb_video_devices_windows
            devices = enumerate_usb_video_devices_windows()
            if devices:
                result = [{"name": d.name, "index": d.index} for d in devices]
                print(f"[BaseCamera] pyusbcameraindex 检测到 {len(result)} 个摄像头: {[(d['name'], d['index']) for d in result]}")
                return result
        except ImportError:
            print("[BaseCamera] pyusbcameraindex 未安装，回退 WMI...")
        except Exception as e:
            print(f"[BaseCamera] pyusbcameraindex 失败: {e}，回退 WMI...")

        # 方案 2：WMI + OpenCV 探测（回退）
        wmi_devices = []
        try:
            import wmi as wmi_mod
            c = wmi_mod.WMI()
            CAMERA_GUID = "{ca3e7ab9-b4c3-4ae6-8251-579ef933890f}"
            for d in c.Win32_PnPEntity():
                if getattr(d, 'ClassGuid', '') == CAMERA_GUID:
                    name = getattr(d, 'Name', f'Camera_{len(wmi_devices)}')
                    wmi_devices.append({"name": name, "index": -1})
        except Exception as e:
            print(f"[BaseCamera] WMI 读取失败: {e}")

        if wmi_devices:
            print(f"[BaseCamera] WMI 检测到 {len(wmi_devices)} 个摄像头: {[d['name'] for d in wmi_devices]}")

        # OpenCV 探测可用 index
        available = []
        for i in range(max_test):
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if cap.isOpened():
                ret, _ = cap.read()
                cap.release()
                if ret:
                    available.append(i)
        print(f"[BaseCamera] OpenCV 可用 index: {available}")

        if not available:
            return []

        # 按顺序分配 WMI 名称
        result = []
        for idx, i in enumerate(available):
            name = wmi_devices[idx]["name"] if idx < len(wmi_devices) else f"摄像头 {i}"
            result.append({"name": name, "index": i})
        print(f"[BaseCamera] 设备列表: {[(d['name'], d['index']) for d in result]}")
        return result


class OpenCVCamera(BaseCamera):
    """OpenCV VideoCapture 实现"""

    def __init__(self):
        self._cap: Optional[cv2.VideoCapture] = None
        self._index = -1
        self._backend = cv2.CAP_DSHOW  # Windows 默认 DSHOW；Linux 可改 CAP_V4L2

    def open(self, index: int, backend: int = None,
             width: int = -1, height: int = -1, fps: int = -1) -> bool:
        """
        打开摄像头
        参数:
            index: 摄像头索引
            backend: 后端标志，None=自动尝试
            width/height/fps: 请求的分辨率和帧率，-1 表示使用摄像头默认值
        """
        self.close()
        self._index = index

        # 后端优先级：DSHOW > MSMF（DSHOW 在 Windows 上最稳定，跳过默认 0）
        backends = []
        if backend is not None:
            backends.append(backend)
        backends.extend([cv2.CAP_DSHOW, cv2.CAP_MSMF])

        for be in backends:
            self._backend = be
            print(f"[OpenCVCamera] 尝试打开摄像头 {index} (backend={be})")

            self._cap = cv2.VideoCapture(index, be)
            if not self._cap.isOpened():
                print(f"[OpenCVCamera] backend={be} 打开失败 (isOpened=False)")
                continue

            # 验证能否读取（默认分辨率）
            print(f"[OpenCVCamera] backend={be} 打开成功，验证读取...")
            for attempt in range(5):
                ret, frame = self._cap.read()
                if ret and frame is not None and frame.size > 0:
                    h, w = frame.shape[:2]
                    print(f"[OpenCVCamera] backend={be} 读取成功，原生分辨率: {w}x{h}")
                    break
                print(f"[OpenCVCamera] 读取尝试 {attempt+1}/5 失败，等待 0.2s...")
                import time
                time.sleep(0.2)
            else:
                print(f"[OpenCVCamera] backend={be} 连续读取失败")
                self.close()
                continue

            # 只有当用户显式指定了分辨率且与原生不同时，才尝试设置
            native_w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            native_h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            
            if width > 0 and height > 0 and (width != native_w or height != native_h):
                print(f"[OpenCVCamera] 尝试设置分辨率 {width}x{height} (原生: {native_w}x{native_h})...")
                self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                
                # 验证设置后能否读取
                ret, frame = self._cap.read()
                if not ret or frame is None or frame.size == 0:
                    print(f"[OpenCVCamera] 设置分辨率失败，回退原生分辨率")
                    # 回退：不设置分辨率，让摄像头自动协商
                    self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, -1)
                    self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, -1)
                else:
                    print(f"[OpenCVCamera] 分辨率设置成功: {frame.shape[1]}x{frame.shape[0]}")
            
            if fps > 0:
                self._cap.set(cv2.CAP_PROP_FPS, fps)
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            # 最终验证
            actual_w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            actual_fps = self._cap.get(cv2.CAP_PROP_FPS)
            print(f"[OpenCVCamera] 最终参数: {actual_w}x{actual_h}@{actual_fps:.1f}fps")

            # 最终验证读取
            ret, frame = self._cap.read()
            if ret and frame is not None and frame.size > 0:
                print(f"[OpenCVCamera] 摄像头 {index} 完全就绪")
                return True

            print(f"[OpenCVCamera] backend={be} 最终验证失败")
            self.close()
            continue

        print(f"[OpenCVCamera] 所有后端均失败")
        return False

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        if self._cap is None or not self._cap.isOpened():
            return False, None
        ret, frame = self._cap.read()
        if ret:
            return True, frame

        # 读取失败：尝试自动切换后端恢复
        print(f"[OpenCVCamera] 读取失败 (index={self._index}, backend={self._backend})，尝试切换后端...")
        old_backend = self._backend
        self.close()
        # 优先切到 DSHOW
        for be in [cv2.CAP_DSHOW, cv2.CAP_MSMF]:
            if be == old_backend:
                continue
            self._backend = be
            self._cap = cv2.VideoCapture(self._index, be)
            if self._cap.isOpened():
                ret, frame = self._cap.read()
                if ret and frame is not None and frame.size > 0:
                    print(f"[OpenCVCamera] 切换到 backend={be} 恢复成功")
                    self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    return True, frame
            self.close()
        print(f"[OpenCVCamera] 所有后端均失败，无法恢复")
        return False, None

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def is_opened(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def get_fps(self) -> float:
        if self._cap:
            return self._cap.get(cv2.CAP_PROP_FPS)
        return 0.0

    def get_resolution(self) -> Tuple[int, int]:
        if self._cap:
            w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            return w, h
        return 0, 0

    @property
    def index(self) -> int:
        return self._index