"""
摄像头抽象层 (Camera Abstraction Layer)
====================================

本文件职责：
1. 提供摄像头设备的跨平台枚举（发现系统中有哪些摄像头）
2. 提供统一的摄像头打开、读帧、关闭接口
3. 封装 OpenCV VideoCapture，隐藏平台差异（Linux 用 V4L2，Windows 用 DirectShow）

主要类：
- BaseCamera: 抽象基类，定义摄像头的标准接口
- OpenCVCamera: 具体实现，基于 OpenCV VideoCapture

使用方法：
    # 枚举摄像头
    cameras = BaseCamera.list_cameras()
    print(cameras)  # [{"name": "USB Camera", "index": 0}, ...]
    
    # 打开摄像头并读帧
    cam = OpenCVCamera()
    if cam.open(0):
        success, frame = cam.read()  # frame 是 numpy 数组 (BGR格式)
        cam.close()

注意事项：
- Linux 上优先使用 /sys/class/video4linux 读取设备名（最可靠）
- 回退到 v4l2-ctl 命令行工具
- 最终回退到 OpenCV 索引探测（逐个尝试 index 0-9）
"""
from abc import ABC, abstractmethod
from typing import Tuple, Optional, List, Dict
import os
import sys
import subprocess
import cv2
import numpy as np

# 平台检测 - 根据操作系统选择不同的摄像头枚举策略
_IS_LINUX = sys.platform.startswith("linux")  # Linux 系统标志
_IS_WINDOWS = sys.platform.startswith("win")  # Windows 系统标志


class BaseCamera(ABC):
    """
    摄像头抽象基类 (Abstract Base Class)
    
    这是一个抽象类，不能直接实例化。它定义了所有摄像头实现必须遵循的接口规范。
    
    为什么用抽象类？
    - 统一接口：无论底层用什么实现（OpenCV、FFmpeg等），上层代码调用方式相同
    - 方便扩展：如果以后要支持其他摄像头SDK，只需继承这个类并实现抽象方法
    
    子类必须实现的方法：
    - open(): 打开摄像头
    - read(): 读取一帧图像
    - close(): 关闭摄像头
    - is_opened(): 检查是否已打开
    - get_fps(): 获取帧率
    - get_resolution(): 获取分辨率
    """

    @abstractmethod
    def open(self, index: int, **kwargs) -> bool:
        """
        打开指定索引的摄像头
        
        参数:
            index: 摄像头索引号（从 0 开始，0 通常是默认摄像头）
            **kwargs: 可选参数，如 width、height、fps 等（由子类决定）
            
        返回:
            bool: True 表示打开成功，False 表示失败
            
        注意:
            - 调用前会自动关闭之前打开的摄像头
            - 不同平台/后端对参数的支持程度不同
        """
        pass

    @abstractmethod
    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        """
        读取一帧图像
        
        返回:
            Tuple[bool, Optional[np.ndarray]]: 
                - bool: True 表示读取成功
                - np.ndarray: BGR 格式的图像数组，形状为 (height, width, 3)
                  如果读取失败则返回 None
                  
        注意:
            - 返回的图像是 OpenCV 标准的 BGR 格式（不是 RGB）
            - 如果摄像头未打开或读取失败，返回 (False, None)
        """
        pass

    @abstractmethod
    def close(self) -> None:
        """
        关闭摄像头并释放资源
        
        注意:
            - 调用后 is_opened() 将返回 False
            - 可以安全地多次调用（已关闭时调用不会报错）
        """
        pass

    @abstractmethod
    def is_opened(self) -> bool:
        """
        检查摄像头是否已打开并可用
        
        返回:
            bool: True 表示摄像头已打开，False 表示未打开或已关闭
        """
        pass

    @abstractmethod
    def get_fps(self) -> float:
        """
        获取摄像头的帧率
        
        返回:
            float: 帧率（如 30.0 表示每秒 30 帧）
                   如果未打开或不支持则返回 0.0
        """
        pass

    @abstractmethod
    def get_resolution(self) -> Tuple[int, int]:
        """
        获取摄像头的分辨率
        
        返回:
            Tuple[int, int]: (宽度, 高度)，单位为像素
                            如 (1920, 1080) 表示 1080p
                            如果未打开则返回 (0, 0)
        """
        pass

    @staticmethod
    def _get_default_backend() -> int:
        """
        获取当前平台的默认视频后端
        
        OpenCV 支持多种视频后端（后端 = 底层视频处理库）：
        - CAP_V4L2: Linux 的 Video4Linux2（Linux 专用，性能最好）
        - CAP_DSHOW: Windows 的 DirectShow（Windows 专用）
        - CAP_MSMF: Windows Media Foundation（Windows 备选）
        - CAP_ANY: 自动选择（跨平台通用，但可能不是最优）
        
        返回:
            int: OpenCV 后端标志常量
        """
        if _IS_LINUX:
            return cv2.CAP_V4L2  # Linux 首选 V4L2
        elif _IS_WINDOWS:
            return cv2.CAP_DSHOW  # Windows 首选 DirectShow
        else:
            return cv2.CAP_ANY  # 其他平台自动选择

    @staticmethod
    def _get_backend_list() -> list:
        """
        获取当前平台可用的视频后端列表（按优先级排序）
        
        用于尝试打开摄像头时，按顺序尝试不同的后端
        
        返回:
            list: OpenCV 后端标志常量的列表
        """
        if _IS_LINUX:
            return [cv2.CAP_V4L2]  # Linux 只有 V4L2
        elif _IS_WINDOWS:
            return [cv2.CAP_DSHOW, cv2.CAP_MSMF]  # Windows: DirectShow 优先，MSMF 备选
        else:
            return [cv2.CAP_ANY]

    @staticmethod
    def list_cameras(max_test: int = 10) -> List[Dict]:
        """
        枚举系统中所有可用的摄像头设备
        
        这是主要的摄像头发现方法，会根据操作系统自动选择最佳枚举策略。
        
        参数:
            max_test: 最大测试索引数（默认 10，即测试 index 0-9）
                     如果系统摄像头较多，可以增大此值
                     
        返回:
            List[Dict]: 摄像头信息列表，每个元素格式为：
                {
                    "name": "设备名称（如 'USB Camera'）",
                    "index": OpenCV 使用的索引号（如 0、1、2...）
                }
                如果没有找到摄像头，返回空列表 []
                
        使用示例:
            cameras = BaseCamera.list_cameras()
            for cam in cameras:
                print(f"发现摄像头: {cam['name']} (索引: {cam['index']})")
        """
        # 根据操作系统选择不同的枚举策略
        if _IS_LINUX:
            return BaseCamera._list_cameras_linux(max_test)
        elif _IS_WINDOWS:
            return BaseCamera._list_cameras_windows(max_test)
        else:
            return BaseCamera._list_cameras_opencv_probe(max_test)

    @staticmethod
    def _list_cameras_linux(max_test: int = 10) -> List[Dict]:
        """
        Linux 平台的摄像头枚举实现
        
        采用三级回退策略（按可靠性排序）：
        1. 读取 /sys/class/video4linux/ 目录 - 最可靠，不受设备占用影响
        2. 调用 v4l2-ctl --list-devices 命令 - 需要安装 v4l2-utils
        3. OpenCV 索引探测 - 最后手段，逐个尝试 index 0-9
        
        参数:
            max_test: 最大测试索引数（用于 OpenCV 探测）
            
        返回:
            List[Dict]: 摄像头信息列表
        """
        import glob as glob_mod

        # 方案 1：读取 /sys/class/video4linux（最可靠，不受设备占用影响）
        # /sys/class/video4linux 是 Linux 内核提供的设备信息接口
        # 每个摄像头对应一个 video0、video1... 目录，其中的 name 文件包含设备名
        sys_devices = []
        seen_sys_names = set()  # 用于去重（每个物理摄像头可能有多个 video 节点）
        
        # glob 用于文件路径模式匹配，/sys/class/video4linux/video*/name 会匹配所有摄像头设备名文件
        for name_path in sorted(glob_mod.glob("/sys/class/video4linux/video*/name")):
            try:
                # 读取设备名称文件，如 "HD Pro Webcam C920"
                dev_name = open(name_path).read().strip()
                
                # 从路径提取设备 ID（如 video0、video1）
                # name_path = /sys/class/video4linux/video0/name
                # dev_dir = /sys/class/video4linux/video0
                # dev_id = video0
                dev_dir = os.path.dirname(name_path)
                dev_id = os.path.basename(dev_dir)
                
                # 提取索引号（从 "video0" 提取 0）
                idx = int(dev_id.replace("video", ""))
                
                # 同名设备只保留第一个（避免重复）
                # 一个物理摄像头可能有 video0（视频流）和 video1（元数据）两个节点
                if dev_name in seen_sys_names:
                    continue
                seen_sys_names.add(dev_name)
                
                # 检查设备节点是否存在（/dev/video0）
                devnode = f"/dev/{dev_id}"
                if not os.path.exists(devnode):
                    continue
                    
                sys_devices.append({"name": dev_name, "index": idx})
            except (ValueError, OSError):
                pass  # 忽略解析错误
                
        # 如果通过 /sys 方式找到了摄像头，直接返回
        if sys_devices:
            print(f"[BaseCamera] /sys 检测到 {len(sys_devices)} 个摄像头: {[(d['name'], d['index']) for d in sys_devices]}")
            return sys_devices

        # 方案 2：使用 v4l2-ctl 命令行工具（需要安装 v4l2-utils 包）
        # v4l2-ctl 是 Linux 下 Video4Linux2 的控制工具，可以列出所有视频设备
        v4l2_devices = []
        seen_names = set()  # 用于去重
        try:
            # 执行 v4l2-ctl --list-devices 命令
            # 输出格式示例：
            # USB Camera (usb-xxx):
            #     /dev/video0
            #     /dev/video1
            result = subprocess.run(
                ["v4l2-ctl", "--list-devices"],
                capture_output=True, text=True, timeout=3  # 3秒超时
            )
            output = result.stdout.strip()
            if output:
                current_name = ""  # 当前设备名称
                # 逐行解析输出
                for line in output.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    # 跳过错误信息行
                    if line.startswith("Cannot open") or line.startswith("ERROR"):
                        continue
                    # 非设备节点行视为设备名称（如 "USB Camera (usb-xxx):"）
                    if not line.startswith("/dev/video") and not line.startswith("/dev/media"):
                        current_name = line.rstrip(":")  # 去掉末尾的冒号
                    else:
                        # 设备节点行（如 "/dev/video0, /dev/video1"）
                        # 跳过 media 设备（/dev/media0 是 media controller，不是视频设备）
                        if "/dev/media" in line:
                            continue
                        # 提取第一个设备路径（可能有多个逗号分隔）
                        dev_path = line.split(",")[0].strip()
                        try:
                            # 提取索引号（从 "/dev/video0" 提取 0）
                            idx = int(dev_path.replace("/dev/video", ""))
                            # 同名设备去重
                            if current_name in seen_names:
                                continue
                            seen_names.add(current_name)
                            v4l2_devices.append({
                                "name": current_name or f"Camera {idx}",
                                "index": idx
                            })
                        except ValueError:
                            pass
        except FileNotFoundError:
            print("[BaseCamera] v4l2-ctl 未安装")  # 需要安装 v4l2-utils
        except Exception as e:
            print(f"[BaseCamera] v4l2-ctl 失败: {e}")

        # 如果通过 v4l2-ctl 找到了摄像头，直接返回
        if v4l2_devices:
            print(f"[BaseCamera] v4l2-ctl 检测到 {len(v4l2_devices)} 个摄像头: {[(d['name'], d['index']) for d in v4l2_devices]}")
            return v4l2_devices

        # 方案 3：OpenCV 索引探测
        return BaseCamera._list_cameras_opencv_probe(max_test)

    @staticmethod
    def _list_cameras_windows(max_test: int = 10) -> List[Dict]:
        """
        Windows 平台的摄像头枚举实现
        
        采用三级回退策略：
        1. pyusbcameraindex - 专用库，可获取 USB 设备详细信息
        2. WMI (Windows Management Instrumentation) - 系统级设备管理
        3. OpenCV DirectShow 探测 - 最后手段
        
        参数:
            max_test: 最大测试索引数
            
        返回:
            List[Dict]: 摄像头信息列表
        """
        # 方案 1：使用 pyusbcameraindex 库（如果已安装）
        # 这是一个第三方库，可以枚举 Windows USB 视频设备
        try:
            from pyusbcameraindex import enumerate_usb_video_devices_windows
            devices = enumerate_usb_video_devices_windows()
            if devices:
                result = [{"name": d.name, "index": d.index} for d in devices]
                print(f"[BaseCamera] pyusbcameraindex 检测到 {len(result)} 个摄像头")
                return result
        except ImportError:
            print("[BaseCamera] pyusbcameraindex 未安装，回退 WMI...")
        except Exception as e:
            print(f"[BaseCamera] pyusbcameraindex 失败: {e}，回退 WMI...")

        # 方案 2：使用 WMI (Windows Management Instrumentation)
        # WMI 是 Windows 的系统管理接口，可以查询硬件设备信息
        # CAMERA_GUID 是 Windows 中摄像头设备类的 GUID
        wmi_devices = []
        try:
            import wmi as wmi_mod
            c = wmi_mod.WMI()  # 创建 WMI 连接
            CAMERA_GUID = "{ca3e7ab9-b4c3-4ae6-8251-579ef933890f}"  # 摄像头设备类 GUID
            # Win32_PnPEntity 包含所有即插即用设备
            for d in c.Win32_PnPEntity():
                # 检查设备类 GUID 是否匹配摄像头
                if getattr(d, 'ClassGuid', '') == CAMERA_GUID:
                    name = getattr(d, 'Name', f'Camera_{len(wmi_devices)}')
                    wmi_devices.append({"name": name, "index": -1})  # WMI 不提供 OpenCV 索引
        except Exception as e:
            print(f"[BaseCamera] WMI 读取失败: {e}")

        # 方案 3：使用 OpenCV DirectShow 逐索引探测
        # 逐个尝试 index 0 到 max_test-1，看能否打开并读取
        available = []
        for i in range(max_test):
            # 使用 DirectShow 后端（Windows 专用）
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if cap.isOpened():
                # 尝试读取一帧，验证摄像头真正可用
                ret, _ = cap.read()
                cap.release()  # 立即释放，只是探测
                if ret:
                    available.append(i)

        if not available:
            return []

        # 将 WMI 设备名与 OpenCV 索引对应起来
        result = []
        for idx, i in enumerate(available):
            # 如果 WMI 有对应索引的设备名，使用它；否则使用默认名
            name = wmi_devices[idx]["name"] if idx < len(wmi_devices) else f"摄像头 {i}"
            result.append({"name": name, "index": i})
        return result

    @staticmethod
    def _list_cameras_opencv_probe(max_test: int = 10) -> List[Dict]:
        """
        通用的 OpenCV 索引探测方法（跨平台）
        
        这是最后的回退方案，逐个尝试 index 0 到 max_test-1：
        - 尝试打开摄像头
        - 尝试读取一帧
        - 如果成功，则认为该索引对应一个可用摄像头
        
        优点：跨平台通用
        缺点：速度慢（需要逐个探测），无法获取设备名称
        
        参数:
            max_test: 最大测试索引数（默认 10）
            
        返回:
            List[Dict]: 摄像头信息列表，设备名使用默认格式 "摄像头 X"
        """
        backend = BaseCamera._get_default_backend()  # 获取平台默认后端
        available = []  # 存储可用的索引
        
        # 逐个探测 index 0 到 max_test-1
        for i in range(max_test):
            # 尝试用指定后端打开摄像头
            cap = cv2.VideoCapture(i, backend)
            if cap.isOpened():
                # 尝试读取一帧，验证摄像头真正可用
                # 有些索引可能可以打开但无法读取（如已被占用）
                ret, _ = cap.read()
                cap.release()  # 立即释放，只是探测
                if ret:
                    available.append(i)
                    
        print(f"[BaseCamera] OpenCV 可用 index: {available}")
        # 由于 OpenCV 探测无法获取设备名，使用默认名称
        return [{"name": f"摄像头 {i}", "index": i} for i in available]


class OpenCVCamera(BaseCamera):
    """
    基于 OpenCV VideoCapture 的摄像头实现
    
    这是 BaseCamera 的具体实现类，封装了 OpenCV 的 VideoCapture。
    
    主要特性：
    - 自动尝试多个后端（V4L2、DirectShow 等）
    - 自动验证摄像头是否真正可用（打开后尝试读取）
    - 支持设置分辨率和帧率（可选）
    - 读取失败时自动尝试切换后端恢复
    
    使用示例：
        cam = OpenCVCamera()
        if cam.open(0):  # 打开索引为 0 的摄像头
            success, frame = cam.read()  # 读取一帧
            if success:
                # 处理 frame（BGR 格式的 numpy 数组）
                pass
            cam.close()  # 关闭摄像头
    """

    def __init__(self):
        """
        初始化 OpenCVCamera 实例
        
        注意：此时还未打开任何摄像头，需要调用 open() 方法
        """
        self._cap: Optional[cv2.VideoCapture] = None  # OpenCV VideoCapture 对象
        self._index = -1  # 当前打开的摄像头索引，-1 表示未打开
        self._backend = self._get_default_backend()  # 当前后端

    def open(self, index: int, backend: int = None,
             width: int = -1, height: int = -1, fps: int = -1) -> bool:
        """
        打开指定索引的摄像头
        
        参数:
            index: 摄像头索引号（从 0 开始）
            backend: 指定的视频后端（如 cv2.CAP_V4L2），None 表示自动尝试
            width: 请求的宽度（像素），-1 表示使用默认值
            height: 请求的高度（像素），-1 表示使用默认值
            fps: 请求的帧率，-1 表示使用默认值
            
        返回:
            bool: True 表示打开成功，False 表示失败
            
        注意:
            - 会先关闭之前打开的摄像头
            - 会自动尝试多个后端，直到成功或全部失败
            - 会验证摄像头是否真正可用（尝试读取一帧）
        """
        self.close()  # 关闭之前打开的摄像头
        self._index = index

        # 构建后端尝试列表：用户指定的后端优先，然后是平台默认列表
        backends = []
        if backend is not None:
            backends.append(backend)  # 用户指定的后端优先尝试
        backends.extend(self._get_backend_list())  # 添加平台默认后端列表

        # 逐个尝试不同的后端
        for be in backends:
            self._backend = be
            print(f"[OpenCVCamera] 尝试打开摄像头 {index} (backend={be})")

            # 使用指定后端尝试打开摄像头
            self._cap = cv2.VideoCapture(index, be)
            if not self._cap.isOpened():
                # isOpened() 返回 False 表示打开失败（摄像头不存在、被占用等）
                print(f"[OpenCVCamera] backend={be} 打开失败 (isOpened=False)")
                continue

            # 验证能否真正读取（有些摄像头可以打开但无法读取）
            print(f"[OpenCVCamera] backend={be} 打开成功，验证读取...")
            # 尝试读取 5 次，因为有些摄像头启动需要时间
            for attempt in range(5):
                ret, frame = self._cap.read()
                # ret: 是否成功，frame: 图像数组（可能是 None 或空数组）
                if ret and frame is not None and frame.size > 0:
                    h, w = frame.shape[:2]  # 获取图像高度和宽度
                    print(f"[OpenCVCamera] backend={be} 读取成功，原生分辨率: {w}x{h}")
                    break
                print(f"[OpenCVCamera] 读取尝试 {attempt+1}/5 失败，等待 0.2s...")
                import time
                time.sleep(0.2)  # 等待 200ms 再重试
            else:
                # for-else 语法：如果循环正常结束（没有 break），则执行 else
                # 这里表示 5 次尝试都失败了
                print(f"[OpenCVCamera] backend={be} 连续读取失败")
                self.close()
                continue  # 尝试下一个后端

            # 尝试设置分辨率（可选）
            # 先获取摄像头原生分辨率
            native_w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            native_h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            
            # 如果用户指定了分辨率，尝试设置
            if width > 0 and height > 0 and (width != native_w or height != native_h):
                print(f"[OpenCVCamera] 尝试设置分辨率 {width}x{height} (原生: {native_w}x{native_h})...")
                # 设置新的分辨率
                self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                
                # 验证设置是否成功（读取一帧检查）
                ret, frame = self._cap.read()
                if not ret or frame is None or frame.size == 0:
                    # 设置失败，恢复原生分辨率
                    print(f"[OpenCVCamera] 设置分辨率失败，回退原生分辨率")
                    self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, -1)
                    self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, -1)
                else:
                    print(f"[OpenCVCamera] 分辨率设置成功: {frame.shape[1]}x{frame.shape[0]}")
            
            # 设置帧率（如果指定）
            if fps > 0:
                self._cap.set(cv2.CAP_PROP_FPS, fps)
            
            # 设置缓冲区大小为 1（减少延迟，总是获取最新帧）
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            # 最终验证：获取实际参数并再次读取
            actual_w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            actual_fps = self._cap.get(cv2.CAP_PROP_FPS)
            print(f"[OpenCVCamera] 最终参数: {actual_w}x{actual_h}@{actual_fps:.1f}fps")

            # 最后一次读取验证
            ret, frame = self._cap.read()
            if ret and frame is not None and frame.size > 0:
                print(f"[OpenCVCamera] 摄像头 {index} 完全就绪")
                return True  # 打开成功

            # 最终验证失败
            print(f"[OpenCVCamera] backend={be} 最终验证失败")
            self.close()
            continue  # 尝试下一个后端

        # 所有后端都失败了
        print(f"[OpenCVCamera] 所有后端均失败")
        return False

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        """
        读取一帧图像
        
        这是摄像头的核心方法，每次调用获取一帧新的图像。
        
        返回:
            Tuple[bool, Optional[np.ndarray]]:
                - bool: True 表示读取成功
                - np.ndarray: BGR 格式的图像数组，形状为 (height, width, 3)
                  如果读取失败则返回 None
                  
        注意:
            - 返回的图像是 OpenCV 标准格式（BGR，不是 RGB）
            - 如果摄像头未打开，直接返回 (False, None)
            - 如果读取失败，会自动尝试切换后端恢复
        """
        # 检查摄像头是否已打开
        if self._cap is None or not self._cap.isOpened():
            return False, None
            
        # 尝试读取一帧
        ret, frame = self._cap.read()
        if ret:
            return True, frame  # 读取成功

        # 读取失败：可能原因包括摄像头断开、驱动问题等
        # 尝试自动切换后端恢复
        print(f"[OpenCVCamera] 读取失败 (index={self._index}, backend={self._backend})，尝试切换后端...")
        old_backend = self._backend  # 记录当前后端，避免重复尝试
        self.close()  # 关闭当前连接
        
        # 尝试其他后端
        for be in self._get_backend_list():
            if be == old_backend:
                continue  # 跳过已失败的后端
            self._backend = be
            self._cap = cv2.VideoCapture(self._index, be)
            if self._cap.isOpened():
                # 尝试读取
                ret, frame = self._cap.read()
                if ret and frame is not None and frame.size > 0:
                    print(f"[OpenCVCamera] 切换到 backend={be} 恢复成功")
                    self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 设置缓冲区大小
                    return True, frame  # 恢复成功
            self.close()  # 这个后端也失败，关闭并继续
            
        # 所有后端都失败
        print(f"[OpenCVCamera] 所有后端均失败，无法恢复")
        return False, None

    def close(self) -> None:
        """
        关闭摄像头并释放资源
        
        重要：使用完摄像头后必须调用此方法，否则资源不会释放。
        
        注意:
            - 调用后 is_opened() 将返回 False
            - 可以安全地多次调用（已关闭时调用不会报错）
        """
        if self._cap is not None:
            self._cap.release()  # 释放 OpenCV VideoCapture 资源
            self._cap = None     # 清空引用

    def is_opened(self) -> bool:
        """
        检查摄像头是否已打开并可用
        
        返回:
            bool: True 表示摄像头已打开，False 表示未打开或已关闭
        """
        return self._cap is not None and self._cap.isOpened()

    def get_fps(self) -> float:
        """
        获取摄像头的帧率
        
        返回:
            float: 帧率（如 30.0 表示每秒 30 帧）
                   如果未打开或不支持则返回 0.0
        """
        if self._cap:
            return self._cap.get(cv2.CAP_PROP_FPS)
        return 0.0

    def get_resolution(self) -> Tuple[int, int]:
        """
        获取摄像头的分辨率
        
        返回:
            Tuple[int, int]: (宽度, 高度)，单位为像素
                            如 (1920, 1080) 表示 1080p
                            如果未打开则返回 (0, 0)
        """
        if self._cap:
            w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            return w, h
        return 0, 0

    @property
    def index(self) -> int:
        """
        获取当前打开的摄像头索引
        
        属性装饰器 @property 使方法可以像属性一样访问：cam.index
        
        返回:
            int: 摄像头索引号，未打开时返回 -1
        """
        return self._index