"""
摄像头预览视图
=============
QLabel 显示画面，支持：
- 保持比例缩放
- 检测框绘制
- FPS 叠加
"""
from typing import List, Dict, Any, Optional

import numpy as np
from PyQt6.QtWidgets import QLabel, QSizePolicy
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QFont, QBrush


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
        # 当前检测结果
        self._detections: List[Dict[str, Any]] = []
        # 类别颜色缓存
        self._cls_colors: Dict[int, QColor] = {}
        # 显示选项
        self._show_fps = True
        self._show_labels = True
        self._show_coords = False  # 是否显示坐标
        self._current_fps = 0.0

        # 画笔
        self._box_pen = QPen(QColor(0, 255, 0), 2)
        self._text_font = QFont("Microsoft YaHei", 10, QFont.Weight.Bold)
        self._fps_font = QFont("Microsoft YaHei", 11, QFont.Weight.Bold)

    # ============ 公共接口 ============
    def update_frame(self, frame: np.ndarray):
        """更新原始帧（BGR numpy）"""
        self._frame = frame
        self.update()  # 触发 paintEvent

    def update_detections(self, detections: List[Dict[str, Any]]):
        """更新检测结果"""
        self._detections = detections
        # 为新类别分配颜色
        for det in detections:
            cls_id = det.get("cls", 0)
            if cls_id not in self._cls_colors:
                self._cls_colors[cls_id] = self._color_for_class(cls_id)

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
        super().clear()

    # ============ 绘制 ============
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # 背景
        painter.fillRect(self.rect(), QColor(26, 26, 26))

        if self._frame is None:
            # 无画面时显示提示
            painter.setPen(QColor(128, 128, 128))
            painter.setFont(QFont("Microsoft YaHei", 14))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                           "等待摄像头画面...")
            # 强制刷新，确保文本可见
            return

        # 1) 转换帧为 QImage
        h, w, ch = self._frame.shape
        bytes_per_line = ch * w
        # OpenCV BGR -> RGB
        rgb_frame = self._frame[:, :, ::-1].copy()
        qimg = QImage(rgb_frame.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)

        # 2) 计算保持比例的目标矩形
        pixmap = QPixmap.fromImage(qimg)
        target_rect = self._fit_pixmap(pixmap)

        # 3) 绘制画面 (QRectF -> QRect)
        painter.drawPixmap(target_rect.toRect(), pixmap)

        # 4) 绘制检测框
        if self._detections:
            self._draw_detections(painter, target_rect, w, h)

        # 5) 绘制 FPS
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
        """绘制检测框和标签"""
        # 缩放比例
        scale_x = target_rect.width() / orig_w
        scale_y = target_rect.height() / orig_h
        offset_x = target_rect.left()
        offset_y = target_rect.top()

        for det in self._detections:
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
                # 标签背景
                label_text = f"{cls_name} {conf:.2f}"
                if self._show_coords:
                    cx, cy = int((x1 + x2) / 2), int((y1 + y2) / 2)
                    label_text += f" ({cx},{cy})"
                painter.setFont(self._text_font)
                fm = painter.fontMetrics()
                text_w = fm.horizontalAdvance(label_text) + 8
                text_h = fm.height() + 4

                label_rect = QRectF(rx1, ry1 - text_h - 2, text_w, text_h)
                # 防止超出上边界
                if label_rect.top() < target_rect.top():
                    label_rect.moveTop(ry1 + 2)

                painter.fillRect(label_rect, QBrush(color))
                painter.setPen(QColor(255, 255, 255))
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
        hue = (cls_id * 137) % 360  # 黄金角度分布
        return QColor.fromHsv(hue, 220, 255)