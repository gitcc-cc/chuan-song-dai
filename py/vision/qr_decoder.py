"""
二维码/条形码解码器
==================
基于 pyzbar，支持 QR Code / Code128 / EAN 等主流码制。
"""
from typing import List, Dict, Any, Optional

import numpy as np

try:
    from pyzbar import pyzbar
    _HAS_PYZBAR = True
except ImportError:
    _HAS_PYZBAR = False


class QRDecoder:
    """二维码/条形码解码器"""

    def __init__(self):
        self._available = _HAS_PYZBAR

    @property
    def available(self) -> bool:
        return self._available

    def decode(self, frame: np.ndarray) -> List[Dict[str, Any]]:
        """
        解码一帧图像中的所有二维码/条形码。
        返回: [{"bbox": [x,y,w,h], "content": str, "type": str}, ...]
        """
        if not self._available or frame is None:
            return []

        try:
            # pyzbar 需要灰度图
            if len(frame.shape) == 3:
                gray = np.mean(frame, axis=2).astype(np.uint8)
            else:
                gray = frame

            codes = pyzbar.decode(gray)
            results = []
            for code in codes:
                # code.rect: (x, y, w, h)
                x, y, w, h = code.rect
                content = code.data.decode("utf-8", errors="replace")
                results.append({
                    "bbox": [x, y, x + w, y + h],  # 转为 xyxy 格式
                    "content": content,
                    "type": code.type,
                })
            return results
        except Exception as e:
            print(f"[QRDecoder] 解码异常: {e}")
            return []


def match_qr_to_detections(qr_results: List[Dict[str, Any]],
                            yolo_detections: List[Dict[str, Any]]) -> Dict[int, str]:
    """
    空间匹配：将二维码绑定到 YOLO 检测框。
    逻辑：二维码中心点落在某个 YOLO 框内 → 绑定。
    返回: {yolo_det_index: qr_content}
    """
    bindings = {}
    for qr in qr_results:
        qr_bbox = qr.get("bbox", [])
        if len(qr_bbox) != 4:
            continue
        # 二维码中心点
        cx = (qr_bbox[0] + qr_bbox[2]) / 2
        cy = (qr_bbox[1] + qr_bbox[3]) / 2

        for i, det in enumerate(yolo_detections):
            det_bbox = det.get("bbox", [])
            if len(det_bbox) != 4:
                continue
            x1, y1, x2, y2 = det_bbox
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                bindings[i] = qr.get("content", "")
                break  # 一个码只绑一个框
    return bindings
