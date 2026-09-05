"""
摄像头预览视图
=============
QLabel 显示画面，支持：
- 保持比例缩放
- YOLO 检测框绘制（彩色）
- 二维码/条形码框绘制（白色）
- 二维码与 YOLO 框绑定显示
- FPS 叠加
"""
from typing import List, Dict, Any, Optional

import numpy as np
from PyQt6.QtWidgets import QLabel, QSizePolicy
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QFont, QBrush

from vision.qr_decoder import match_qr_to_detections


class CameraView(QLabel):
    """摄像头画面显示组件"""

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 240)
        self.setStyleSheet("background-color: #1a1a1a; border: 1px solid #333; border-radius: 4px;")

        # 当前帧
        self._frame: Optional[np.ndarray] = None
        # YOLO 检测结果
        self._detections: List[Dict[str, Any]] = []
        # 二维码解码结果
        self._qr_results: List[Dict[str, Any]] = []
        # 二维码绑定映射 {yolo_index: qr_content}
        self._qr_bindings: Dict[int, str] = {}
        # 类别颜色缓存
        self._cls_colors: Dict[int, QColor] = {}
        # 显示选项
        self._show_fps = True
        self._show_labels = True
        self._show_coords = False
        self._current_fps = 0.0

        # 画笔
        self._box_pen = QPen(QColor(0, 255, 0), 2)
        self._qr_pen = QPen(QColor(255, 255, 255), 2)  # 白色二维码框
        self._text_font = QFont("Microsoft YaHei", 10, QFont.Weight.Bold)
        self._qr_font = QFont("Microsoft YaHei", 9, QFont.Weight.Bold)
        self._fps_font = QFont("Microsoft YaHei", 11, QFont.Weight.Bold)

    # ============ 公共接口 ============
    def update_frame(self, frame: np.ndarray):
        """更新原始帧（BGR numpy）"""
        self._frame = frame
        self.update()

    def update_detections(self, detections: List[Dict[str, Any]]):
        """更新 YOLO 检测结果"""
        self._detections = detections
        for det in detections:
            cls_id = det.get("cls", 0)
            if cls_id not in self._cls_colors:
                self._cls_colors[cls_id] = self._color_for_class(cls_id)
        # 重新计算绑定
        self._qr_bindings = match_qr_to_detections(self._qr_results, self._detections)

    def update_qr_results(self, qr_results: List[Dict[str, Any]]):
        """更新二维码解码结果"""
        self._qr_results = qr_results
        # 重新计算绑定
        self._qr_bindings = match_qr_to_detections(self._qr_results, self._detections)

    def update_fps(self, fps: float):
        self._current_fps = fps

    def set_show_fps(self, show: bool):
        self._show_fps = show

    def set_show_labels(self, show: bool):
        self._show_labels = show

    def set_show_coords(self, show: bool):
        self._show_coords = show

    def clear(self):
        self._frame = None
        self._detections = []
        self._qr_results = []
        self._qr_bindings = {}
        super().clear()

    # ============ 绘制 ============
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # 背景
        painter.fillRect(self.rect(), QColor(26, 26, 26))

        if self._frame is None:
            painter.setPen(QColor(128, 128, 128))
            painter.setFont(QFont("Microsoft YaHei", 14))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                           "等待摄像头画面...")
            return

        # 1) 转换帧为 QImage
        h, w, ch = self._frame.shape
        bytes_per_line = ch * w
        rgb_frame = self._frame[:, :, ::-1].copy()
        qimg = QImage(rgb_frame.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)

        # 2) 计算保持比例的目标矩形
        pixmap = QPixmap.fromImage(qimg)
        target_rect = self._fit_pixmap(pixmap)

        # 3) 绘制画面
        painter.drawPixmap(target_rect.toRect(), pixmap)

        # 4) 绘制 YOLO 检测框（带绑定的二维码内容）
        if self._detections:
            self._draw_detections(painter, target_rect, w, h)

        # 5) 绘制未匹配的二维码框（白色）
        if self._qr_results:
            self._draw_qr_boxes(painter, target_rect, w, h)

        # 6) 绘制 FPS
        if self._show_fps:
            self._draw_fps(painter)

    def _fit_pixmap(self, pixmap: QPixmap) -> QRectF:
        """计算保持比例的目标矩形（居中）"""
        label_rect = self.rect()
        scaled = pixmap.scaled(
            label_rect.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        x = (label_rect.width() - scaled.width()) / 2
        y = (label_rect.height() - scaled.height()) / 2
        return QRectF(x, y, scaled.width(), scaled.height())

    def _draw_detections(self, painter: QPainter, target_rect: QRectF,
                         orig_w: int, orig_h: int):
        """绘制 YOLO 检测框和标签（含绑定的二维码内容）"""
        scale_x = target_rect.width() / orig_w
        scale_y = target_rect.height() / orig_h
        offset_x = target_rect.left()
        offset_y = target_rect.top()

        for i, det in enumerate(self._detections):
            bbox = det.get("bbox", [])
            if len(bbox) != 4:
                continue
            x1, y1, x2, y2 = bbox
            conf = det.get("conf", 0)
            cls_id = det.get("cls", 0)
            cls_name = det.get("cls_name", str(cls_id))

            # 映射到显示坐标
            rx1 = offset_x + x1 * scale_x
            ry1 = offset_y + y1 * scale_y
            rx2 = offset_x + x2 * scale_x
            ry2 = offset_y + y2 * scale_y

            # 类别颜色
            color = self._cls_colors.get(cls_id, QColor(0, 255, 0))
            self._box_pen.setColor(color)
            painter.setPen(self._box_pen)

            # 画框
            painter.drawRect(QRectF(rx1, ry1, rx2 - rx1, ry2 - ry1))

            if self._show_labels:
                # 标签：类别 + 置信度
                label_text = f"{cls_name} {conf:.2f}"
                if self._show_coords:
                    cx, cy = int((x1 + x2) / 2), int((y1 + y2) / 2)
                    label_text += f" ({cx},{cy})"

                # 绑定的二维码内容
                qr_content = self._qr_bindings.get(i, "")
                if qr_content:
                    label_text += f" | {qr_content}"

                painter.setFont(self._text_font)
                fm = painter.fontMetrics()
                text_w = fm.horizontalAdvance(label_text) + 8
                text_h = fm.height() + 4

                label_rect = QRectF(rx1, ry1 - text_h - 2, text_w, text_h)
                if label_rect.top() < target_rect.top():
                    label_rect.moveTop(ry1 + 2)

                painter.fillRect(label_rect, QBrush(color))
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, label_text)

    def _draw_qr_boxes(self, painter: QPainter, target_rect: QRectF,
                       orig_w: int, orig_h: int):
        """绘制未匹配到 YOLO 框的二维码（白色框）"""
        scale_x = target_rect.width() / orig_w
        scale_y = target_rect.height() / orig_h
        offset_x = target_rect.left()
        offset_y = target_rect.top()

        # 已匹配的二维码跳过（已在 _draw_detections 中显示）
        matched_contents = set(self._qr_bindings.values())

        self._qr_pen.setColor(QColor(255, 255, 255))
        painter.setPen(self._qr_pen)

        for qr in self._qr_results:
            content = qr.get("content", "")
            if content in matched_contents:
                continue  # 已绑定到 YOLO 框，跳过

            bbox = qr.get("bbox", [])
            if len(bbox) != 4:
                continue
            x1, y1, x2, y2 = bbox

            # 映射到显示坐标
            rx1 = offset_x + x1 * scale_x
            ry1 = offset_y + y1 * scale_y
            rx2 = offset_x + x2 * scale_x
            ry2 = offset_y + y2 * scale_y

            # 白色框
            painter.drawRect(QRectF(rx1, ry1, rx2 - rx1, ry2 - ry1))

            # 标签背景 + 内容
            qr_type = qr.get("type", "QR")
            label_text = f"[{qr_type}] {content}"
            painter.setFont(self._qr_font)
            fm = painter.fontMetrics()
            text_w = fm.horizontalAdvance(label_text) + 8
            text_h = fm.height() + 4

            label_rect = QRectF(rx1, ry2 + 2, text_w, text_h)
            # 防止超出下边界
            if label_rect.bottom() > target_rect.bottom():
                label_rect.moveBottom(ry2 - 2)

            painter.fillRect(label_rect, QBrush(QColor(255, 255, 255, 200)))
            painter.setPen(QColor(0, 0, 0))
            painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, label_text)

    def _draw_fps(self, painter: QPainter):
        """右上角绘制 FPS"""
        painter.setFont(self._fps_font)
        painter.setPen(QColor(0, 255, 0))
        fps_text = f"FPS: {self._current_fps:.1f}"
        fm = painter.fontMetrics()
        text_w = fm.horizontalAdvance(fps_text) + 10
        text_h = fm.height() + 6
        rect = QRectF(self.width() - text_w - 10, 10, text_w, text_h)
        painter.fillRect(rect, QBrush(QColor(0, 0, 0, 180)))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, fps_text)

    def _color_for_class(self, cls_id: int) -> QColor:
        """为类别生成固定颜色（基于 HSV）"""
        hue = (cls_id * 137) % 360
        return QColor.fromHsv(hue, 220, 255)
