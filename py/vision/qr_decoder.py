"""
二维码/条形码解码器
==================
本文件的职责：
- 提供 QRDecoder 类，用于从图像帧中解码二维码和条形码
- 支持 QR Code、Code128、EAN 等主流码制
- 提供 match_qr_to_detections 函数，将二维码与 YOLO 检测框进行空间匹配
- 依赖库：pyzbar（必须安装）、numpy
- 使用场景：摄像头实时识别时，先用 YOLO 检测物体，再用本模块读取物体上的二维码

典型调用流程：
1. 创建 QRDecoder 实例
2. 在摄像头循环中调用 decoder.decode(frame) 获取所有二维码
3. 调用 match_qr_to_detections() 将二维码绑定到 YOLO 检测结果
4. 根据绑定关系显示或处理二维码内容
"""
from typing import List, Dict, Any  # 类型提示，帮助 IDE 理解数据结构

import numpy as np  # 用于图像数组操作

# 尝试导入 pyzbar 库，如果未安装则设置标志位
# pyzbar 是 Python 的条形码/二维码识别库，底层依赖 libzbar
try:
    from pyzbar import pyzbar
    _HAS_PYZBAR = True  # 标记：pyzbar 可用
except ImportError:
    _HAS_PYZBAR = False  # 标记：pyzbar 不可用，解码功能将禁用


class QRDecoder:
    """
    二维码/条形码解码器类
    
    使用方法：
        decoder = QRDecoder()  # 创建实例
        if decoder.available:  # 检查 pyzbar 是否可用
            results = decoder.decode(image)  # 解码图像
    """

    def __init__(self):
        """
        构造函数
        
        参数：无
        
        功能：
        - 检查 pyzbar 库是否已安装
        - 初始化解码器状态
        """
        self._available = _HAS_PYZBAR  # 保存 pyzbar 可用状态

    @property
    def available(self) -> bool:
        """
        只读属性：判断解码器是否可用
        
        返回：bool 类型
            - True：pyzbar 已安装，可以正常解码
            - False：pyzbar 未安装，解码功能禁用
        
        使用示例：
            if decoder.available:
                results = decoder.decode(frame)
            else:
                print("请先安装 pyzbar: pip install pyzbar")
        """
        return self._available

    def decode(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        """
        解码一帧图像中的所有二维码/条形码
        
        参数：
            frame: np.ndarray 类型，OpenCV 格式的图像数组
                   - 可以是灰度图（2D 数组，shape=(H, W)）
                   - 可以是彩色图（3D 数组，shape=(H, W, 3)，BGR 格式）
                   - 通常是摄像头捕获的一帧画面
        
        返回：List[Dict[str, Any]] 类型，字典列表
            - 空列表 []：没有检测到任何码
            - 非空列表：每个字典代表一个检测到的码，包含：
                - "bbox": [x1, y1, x2, y2] 四个整数，码的边界框坐标（左上角和右下角）
                - "content": str 类型，码的内容（已解码为字符串）
                - "type": str 类型，码的类型（如 "QRCODE"、"CODE128"）
        
        注意：
            - 如果 pyzbar 不可用或图像为空，直接返回空列表
            - 解码失败时返回空列表，不会抛出异常
            - 单帧可能检测到多个码（如图像中有多个二维码）
        """
        # 检查解码器是否可用，以及图像是否有效
        if not self._available or frame is None:
            return []

        try:
            # pyzbar 只能处理灰度图，需要将彩色图转换为灰度图
            # frame.shape == 3 表示是彩色图（高度、宽度、通道数）
            if len(frame.shape) == 3:
                # 计算 RGB 三通道的平均值，转换为灰度图
                # axis=2 表示对第3个维度（通道）求平均
                # astype(np.uint8) 将浮点数转换为 0-255 的整数
                gray = np.mean(frame, axis=2).astype(np.uint8)
            else:
                # 已经是灰度图，直接使用
                gray = frame

            # 调用 pyzbar 的 decode 函数进行解码
            # 返回 pyzbar 的 Decoded 对象列表，每个对象包含一个码的信息
            codes = pyzbar.decode(gray)
            results = []  # 存储最终结果

            # 遍历每个检测到的码
            for code in codes:
                # code.rect 是 pyzbar 返回的矩形区域，格式为 (x, y, width, height)
                # x, y 是左上角坐标，width, height 是宽高
                x, y, w, h = code.rect
                
                # code.data 是码的原始二进制数据，解码为 UTF-8 字符串
                # errors="replace" 表示遇到无法解码的字符时用替代字符替换，避免报错
                content = code.data.decode("utf-8", errors="replace")
                
                # 构建结果字典，将 bbox 转换为 xyxy 格式（左上角和右下角坐标）
                # xyxy 格式更通用，便于后续与 YOLO 检测框进行比较
                results.append({
                    "bbox": [x, y, x + w, y + h],  # [x1, y1, x2, y2]
                    "content": content,  # 码的内容字符串
                    "type": code.type,   # 码的类型，如 "QRCODE"
                })

            return results  # 返回所有检测到的码

        except Exception as e:
            # 捕获所有异常，打印错误信息并返回空列表
            # 这样即使解码失败，程序也不会崩溃
            print(f"[QRDecoder] 解码异常: {e}")
            return []


def match_qr_to_detections(qr_results: List[Dict[str, Any]],
                            yolo_detections: List[Dict[str, Any]]) -> Dict[int, str]:
    """
    将二维码与 YOLO 检测框进行空间匹配
    
    用途：
        在产线控制中，YOLO 负责检测物体，二维码负责识别物体信息。
        本函数将二维码内容绑定到对应的 YOLO 检测框，实现"这个二维码属于这个物体"的关联。
    
    参数：
        qr_results: List[Dict[str, Any]] 类型
                    - QRDecoder.decode() 的返回值
                    - 每个字典包含一个二维码的 bbox、content、type
        
        yolo_detections: List[Dict[str, Any]] 类型
                        - YOLO 检测器的返回值
                        - 每个字典包含一个检测框的 bbox、class（类别）、confidence（置信度）等
    
    返回：Dict[int, str] 类型
        - 键：YOLO 检测框的索引（从 0 开始）
        - 值：二维码的内容字符串
        - 示例：{0: "产品A", 2: "批次123"} 表示第0个检测框包含"产品A"的二维码，
                第2个检测框包含"批次123"的二维码，第1个检测框没有二维码
    
    匹配逻辑：
        1. 遍历每个二维码
        2. 计算二维码的中心点坐标
        3. 检查中心点是否落在某个 YOLO 检测框内
        4. 如果落在框内，建立绑定关系
        5. 一个二维码只绑定一个框（第一个匹配的框）
    
    注意：
        - 一个二维码只能绑定到一个 YOLO 框（即使中心点落在多个框的重叠区域）
        - 一个 YOLO 框可以被多个二维码绑定（如果多个二维码都在这个框内）
        - 返回的字典可能不包含所有 YOLO 框的索引（没有二维码的框不会出现）
    """
    bindings = {}  # 存储绑定结果：{YOLO框索引: 二维码内容}

    # 遍历每个二维码
    for qr in qr_results:
        # 获取二维码的边界框坐标
        qr_bbox = qr.get("bbox", [])
        
        # 检查边界框格式是否正确（必须是4个值）
        if len(qr_bbox) != 4:
            continue  # 跳过格式错误的二维码

        # 计算二维码的中心点坐标
        # cx = (左边界 + 右边界) / 2
        # cy = (上边界 + 下边界) / 2
        cx = (qr_bbox[0] + qr_bbox[2]) / 2
        cy = (qr_bbox[1] + qr_bbox[3]) / 2

        # 遍历每个 YOLO 检测框
        for i, det in enumerate(yolo_detections):
            # 获取 YOLO 检测框的边界框坐标
            det_bbox = det.get("bbox", [])
            
            # 检查边界框格式是否正确
            if len(det_bbox) != 4:
                continue  # 跳过格式错误的检测框

            # 解包 YOLO 框的四个坐标
            x1, y1, x2, y2 = det_bbox

            # 判断二维码中心点是否在 YOLO 框内
            # 条件：中心点的 x 坐标在 [x1, x2] 之间，y 坐标在 [y1, y2] 之间
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                # 匹配成功！将二维码内容绑定到这个 YOLO 框
                bindings[i] = qr.get("content", "")
                break  # 一个码只绑一个框，跳出内层循环

    return bindings  # 返回所有绑定关系
