"""
摄像头预览视图组件
==================
职责：基于 QLabel 实现摄像头画面的实时显示，支持以下功能：
- 保持比例的画面缩放显示
- YOLO 目标检测框的彩色绘制（按类别着色）
- 二维码/条形码检测框的白色绘制
- 二维码内容与 YOLO 检测结果的自动绑定显示
- 实时 FPS 信息叠加显示
- 支持显示标签、坐标、FPS 等信息的开关控制
"""
from typing import List, Dict, Any, Optional

import numpy as np
from PyQt6.QtWidgets import QLabel, QSizePolicy
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QFont, QBrush

from vision.qr_decoder import match_qr_to_detections


class CameraView(QLabel):
    """
    摄像头画面显示组件
    继承自 QLabel，用于实时显示摄像头画面并叠加检测结果
    """

    def __init__(self, parent=None):
        """初始化摄像头视图组件"""
        super().__init__(parent)

        # 设置居中对齐和自适应大小策略
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 240)  # 最小尺寸限制
        # 设置深色背景和边框样式
        self.setStyleSheet("background-color: #1a1a1a; border: 1px solid #333; border-radius: 4px;")

        # 当前帧数据（BGR格式的numpy数组）
        self._frame: Optional[np.ndarray] = None
        # YOLO检测结果列表，每个元素包含bbox、conf、cls等信息
        self._detections: List[Dict[str, Any]] = []
        # 二维码解码结果列表，每个元素包含content、bbox、type等信息
        self._qr_results: List[Dict[str, Any]] = []
        # 二维码绑定映射：{YOLO检测索引: 二维码内容}，用于将二维码与检测框关联
        self._qr_bindings: Dict[int, str] = {}
        # 类别颜色缓存：{类别ID: 颜色}，确保同一类别显示相同颜色
        self._cls_colors: Dict[int, QColor] = {}
        # 显示选项控制
        self._show_fps = True      # 是否显示FPS
        self._show_labels = True   # 是否显示检测标签
        self._show_coords = False  # 是否显示坐标信息
        self._current_fps = 0.0    # 当前帧率

        # 绘图工具初始化
        self._box_pen = QPen(QColor(0, 255, 0), 2)  # 检测框画笔（绿色，2像素宽）
        self._qr_pen = QPen(QColor(255, 255, 255), 2)  # 二维码框画笔（白色，2像素宽）
        self._text_font = QFont("", 10, QFont.Weight.Bold)  # 检测标签字体
        self._qr_font = QFont("", 9, QFont.Weight.Bold)     # 二维码标签字体
        self._fps_font = QFont("", 11, QFont.Weight.Bold)   # FPS显示字体

    # ============ 公共接口 ============
    def update_frame(self, frame: np.ndarray):
        """
        更新摄像头帧数据
        frame: BGR格式的numpy数组，来自OpenCV的VideoCapture.read()
        """
        self._frame = frame
        self.update()  # 触发重绘事件

    def update_detections(self, detections: List[Dict[str, Any]]):
        """
        更新YOLO检测结果
        detections: 检测结果列表，每个元素包含：
            - bbox: [x1, y1, x2, y2] 边界框坐标
            - conf: 置信度分数
            - cls: 类别ID
            - cls_name: 类别名称
        """
        self._detections = detections
        # 为新出现的类别生成颜色（缓存避免重复计算）
        for det in detections:
            cls_id = det.get("cls", 0)
            if cls_id not in self._cls_colors:
                self._cls_colors[cls_id] = self._color_for_class(cls_id)
        # 重新计算二维码与检测框的绑定关系
        self._qr_bindings = match_qr_to_detections(self._qr_results, self._detections)

    def update_qr_results(self, qr_results: List[Dict[str, Any]]):
        """
        更新二维码解码结果
        qr_results: 二维码结果列表，每个元素包含：
            - content: 解码内容
            - bbox: [x1, y1, x2, y2] 边界框坐标
            - type: 二维码类型（如"QR", "CODE128"等）
        """
        self._qr_results = qr_results
        # 重新计算绑定关系（二维码位置可能变化）
        self._qr_bindings = match_qr_to_detections(self._qr_results, self._detections)

    def update_fps(self, fps: float):
        """更新当前帧率显示"""
        self._current_fps = fps

    def set_show_fps(self, show: bool):
        """设置是否显示FPS信息"""
        self._show_fps = show

    def set_show_labels(self, show: bool):
        """设置是否显示检测标签"""
        self._show_labels = show

    def set_show_coords(self, show: bool):
        """设置是否显示坐标信息"""
        self._show_coords = show

    def clear(self):
        """清空所有数据并重置显示"""
        self._frame = None
        self._detections = []
        self._qr_results = []
        self._qr_bindings = {}
        super().clear()  # 清空QLabel内容

    # ============ 绘制 ============
    def paintEvent(self, event):
        """
        重写QLabel的绘制事件，绘制摄像头画面和检测结果
        按顺序绘制：背景 → 画面 → YOLO检测框 → 二维码框 → FPS信息
        """
        painter = QPainter(self)
        # 启用抗锯齿和光滑像素变换，提升绘制质量
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # 绘制深色背景（RGB 26,26,26）
        painter.fillRect(self.rect(), QColor(26, 26, 26))

        # 如果没有帧数据，显示等待提示
        if self._frame is None:
            painter.setPen(QColor(128, 128, 128))  # 灰色文字
            painter.setFont(QFont("", 14))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                           "等待摄像头画面...")
            return

        # 1) 将OpenCV的BGR格式转换为Qt的RGB格式
        h, w, ch = self._frame.shape  # 获取帧的高、宽、通道数
        bytes_per_line = ch * w  # 计算每行字节数
        rgb_frame = self._frame[:, :, ::-1].copy()  # BGR转RGB（通道反转）
        # 创建QImage，注意内存对齐
        qimg = QImage(rgb_frame.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)

        # 2) 计算保持比例的显示矩形（居中显示）
        pixmap = QPixmap.fromImage(qimg)  # QImage转QPixmap
        target_rect = self._fit_pixmap(pixmap)  # 计算缩放后的目标矩形

        # 3) 绘制摄像头画面到目标矩形
        painter.drawPixmap(target_rect.toRect(), pixmap)

        # 4) 绘制YOLO检测框（包含绑定的二维码内容）
        if self._detections:
            self._draw_detections(painter, target_rect, w, h)

        # 5) 绘制未匹配到YOLO框的独立二维码框（白色）
        if self._qr_results:
            self._draw_qr_boxes(painter, target_rect, w, h)

        # 6) 绘制FPS信息（右上角）
        if self._show_fps:
            self._draw_fps(painter)

    def _fit_pixmap(self, pixmap: QPixmap) -> QRectF:
        """
        计算保持画面比例的显示矩形，实现居中显示
        返回：缩放并居中后的目标矩形（QRectF格式）
        """
        label_rect = self.rect()  # 获取组件的显示区域
        # 按保持比例模式缩放pixmap，使用平滑变换
        scaled = pixmap.scaled(
            label_rect.size(),
            Qt.AspectRatioMode.KeepAspectRatio,  # 保持宽高比
            Qt.TransformationMode.SmoothTransformation  # 平滑缩放
        )
        # 计算居中偏移量
        x = (label_rect.width() - scaled.width()) / 2
        y = (label_rect.height() - scaled.height()) / 2
        return QRectF(x, y, scaled.width(), scaled.height())

    def _draw_detections(self, painter: QPainter, target_rect: QRectF,
                         orig_w: int, orig_h: int):
        """
        绘制YOLO检测框和标签（包含绑定的二维码内容）
        painter: QPainter对象
        target_rect: 画面显示的目标矩形
        orig_w, orig_h: 原始帧的宽高（用于坐标映射）
        """
        # 计算坐标缩放比例（从原始帧坐标到显示坐标）
        scale_x = target_rect.width() / orig_w
        scale_y = target_rect.height() / orig_h
        # 计算坐标偏移量（画面在组件中的起始位置）
        offset_x = target_rect.left()
        offset_y = target_rect.top()

        # 遍历所有检测结果
        for i, det in enumerate(self._detections):
            bbox = det.get("bbox", [])
            if len(bbox) != 4:
                continue  # 跳过无效的边界框
            x1, y1, x2, y2 = bbox  # 原始帧中的边界框坐标
            conf = det.get("conf", 0)  # 置信度
            cls_id = det.get("cls", 0)  # 类别ID
            cls_name = det.get("cls_name", str(cls_id))  # 类别名称

            # 将原始坐标映射到显示坐标
            rx1 = offset_x + x1 * scale_x
            ry1 = offset_y + y1 * scale_y
            rx2 = offset_x + x2 * scale_x
            ry2 = offset_y + y2 * scale_y

            # 获取类别对应的颜色（缓存中不存在则使用默认绿色）
            color = self._cls_colors.get(cls_id, QColor(0, 255, 0))
            self._box_pen.setColor(color)
            painter.setPen(self._box_pen)

            # 绘制检测框矩形
            painter.drawRect(QRectF(rx1, ry1, rx2 - rx1, ry2 - ry1))

            # 绘制标签（如果启用）
            if self._show_labels:
                # 构建标签文本：类别名称 + 置信度
                label_text = f"{cls_name} {conf:.2f}"
                # 如果启用坐标显示，追加中心点坐标
                if self._show_coords:
                    cx, cy = int((x1 + x2) / 2), int((y1 + y2) / 2)
                    label_text += f" ({cx},{cy})"

                # 如果有绑定的二维码内容，追加到标签
                qr_content = self._qr_bindings.get(i, "")
                if qr_content:
                    label_text += f" | {qr_content}"

                # 设置字体并计算标签尺寸
                painter.setFont(self._text_font)
                fm = painter.fontMetrics()
                text_w = fm.horizontalAdvance(label_text) + 8  # 文本宽度+边距
                text_h = fm.height() + 4  # 文本高度+边距

                # 定位标签矩形（默认在检测框上方）
                label_rect = QRectF(rx1, ry1 - text_h - 2, text_w, text_h)
                # 如果标签超出画面上边界，则移到检测框内部
                if label_rect.top() < target_rect.top():
                    label_rect.moveTop(ry1 + 2)

                # 绘制标签背景（类别颜色）和白色文字
                painter.fillRect(label_rect, QBrush(color))
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, label_text)

    def _draw_qr_boxes(self, painter: QPainter, target_rect: QRectF,
                       orig_w: int, orig_h: int):
        """
        绘制未匹配到YOLO框的独立二维码（白色框）
        已经与YOLO框绑定的二维码会在_draw_detections中显示
        """
        # 计算坐标缩放比例
        scale_x = target_rect.width() / orig_w
        scale_y = target_rect.height() / orig_h
        offset_x = target_rect.left()
        offset_y = target_rect.top()

        # 获取已匹配的二维码内容集合（避免重复绘制）
        matched_contents = set(self._qr_bindings.values())

        # 设置白色画笔
        self._qr_pen.setColor(QColor(255, 255, 255))
        painter.setPen(self._qr_pen)

        # 遍历所有二维码结果
        for qr in self._qr_results:
            content = qr.get("content", "")
            if content in matched_contents:
                continue  # 已绑定到YOLO框，跳过绘制

            bbox = qr.get("bbox", [])
            if len(bbox) != 4:
                continue
            x1, y1, x2, y2 = bbox

            # 将原始坐标映射到显示坐标
            rx1 = offset_x + x1 * scale_x
            ry1 = offset_y + y1 * scale_y
            rx2 = offset_x + x2 * scale_x
            ry2 = offset_y + y2 * scale_y

            # 绘制白色矩形框
            painter.drawRect(QRectF(rx1, ry1, rx2 - rx1, ry2 - ry1))

            # 绘制二维码标签（类型 + 内容）
            qr_type = qr.get("type", "QR")  # 二维码类型（默认QR）
            label_text = f"[{qr_type}] {content}"
            painter.setFont(self._qr_font)
            fm = painter.fontMetrics()
            text_w = fm.horizontalAdvance(label_text) + 8
            text_h = fm.height() + 4

            # 标签位置：默认在框下方
            label_rect = QRectF(rx1, ry2 + 2, text_w, text_h)
            # 如果超出画面下边界，则移到框上方
            if label_rect.bottom() > target_rect.bottom():
                label_rect.moveBottom(ry2 - 2)

            # 绘制半透明白色背景和黑色文字
            painter.fillRect(label_rect, QBrush(QColor(255, 255, 255, 200)))
            painter.setPen(QColor(0, 0, 0))
            painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, label_text)

    def _draw_fps(self, painter: QPainter):
        """在画面右上角绘制FPS信息（绿色文字+半透明黑色背景）"""
        painter.setFont(self._fps_font)
        painter.setPen(QColor(0, 255, 0))  # 绿色文字
        fps_text = f"FPS: {self._current_fps:.1f}"  # 保留一位小数
        fm = painter.fontMetrics()
        text_w = fm.horizontalAdvance(fps_text) + 10
        text_h = fm.height() + 6
        # 定位在右上角，距边缘10像素
        rect = QRectF(self.width() - text_w - 10, 10, text_w, text_h)
        # 绘制半透明黑色背景（alpha=180）
        painter.fillRect(rect, QBrush(QColor(0, 0, 0, 180)))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, fps_text)

    def _color_for_class(self, cls_id: int) -> QColor:
        """
        根据类别ID生成固定的HSV颜色
        使用黄金分割角度（137.5度）确保相邻类别颜色差异明显
        """
        hue = (cls_id * 137) % 360  # 色相角度，模360确保在0-359范围内
        return QColor.fromHsv(hue, 220, 255)  # 饱和度220，亮度255
