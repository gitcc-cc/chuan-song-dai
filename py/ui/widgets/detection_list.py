"""
检测结果列表（产线控制用）
=========================
6 列表格：#、类别、置信度、X、Y、二维码
深度模式下显示 7 列：#、类别、置信度、X、Y、距离、二维码
每帧完全替换，不做历史累积。
"""
from typing import List, Dict, Any

from PyQt6.QtWidgets import (
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QBrush


class DetectionList(QTableWidget):
    """产线控制检测结果列表"""

    HEADERS_RGB = ["#", "类别", "置信度", "X", "Y", "二维码"]
    HEADERS_DEPTH = ["#", "类别", "置信度", "X", "Y", "距离(mm)", "二维码"]
    MAX_DETS = 10  # 最多显示10个检测结果

    def __init__(self, parent=None):
        super().__init__(parent)

        self._depth_mode = False
        self._setup_columns(self.HEADERS_RGB)

        self.setStyleSheet("""
            QTableWidget {
                background-color: #ffffff;
                alternate-background-color: #f8f8f8;
                border: 1px solid #e0e0e0;
                border-radius: 4px;
                gridline-color: #e0e0e0;
                font-size: 12px;
            }
            QHeaderView::section {
                background-color: #f0f0f0;
                border: none;
                border-right: 1px solid #e0e0e0;
                border-bottom: 1px solid #e0e0e0;
                padding: 6px;
                font-weight: bold;
                color: #333333;
            }
            QTableWidget::item:selected {
                background-color: #e8f5e9;
                color: #07C160;
            }
        """)

        # 空状态：不显示任何内容
        self.setRowCount(0)

    def _setup_columns(self, headers: list):
        """设置列头"""
        self.setColumnCount(len(headers))
        self.setHorizontalHeaderLabels(headers)
        header = self.horizontalHeader()
        for i in range(len(headers)):
            if i == 0:
                header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
            elif i == len(headers) - 1:
                header.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)

    def set_depth_mode(self, enabled: bool):
        """切换深度模式（增加/隐藏距离列）"""
        if self._depth_mode == enabled:
            return
        self._depth_mode = enabled
        headers = self.HEADERS_DEPTH if enabled else self.HEADERS_RGB
        self._setup_columns(headers)

    def update_detections(self, detections: List[Dict[str, Any]],
                          qr_bindings: Dict[int, str] = None):
        """
        替换整帧检测结果
        detections: [{"bbox": [x1,y1,x2,y2], "conf": float, "cls": int, "cls_name": str, "depth_mm": int}, ...]
        qr_bindings: {detection_index: "二维码内容"}
        """
        if qr_bindings is None:
            qr_bindings = {}

        if not detections:
            self.setRowCount(0)
            return

        sorted_dets = sorted(detections, key=lambda d: d.get("conf", 0), reverse=True)
        # 最多显示 MAX_DETS 个
        sorted_dets = sorted_dets[:self.MAX_DETS]
        self.setRowCount(len(sorted_dets))

        for i, det in enumerate(sorted_dets):
            bbox = det.get("bbox", [0, 0, 0, 0])
            cx = int((bbox[0] + bbox[2]) / 2)
            cy = int((bbox[1] + bbox[3]) / 2)
            conf = det.get("conf", 0)
            cls_name = det.get("cls_name", str(det.get("cls", 0)))
            qr_content = qr_bindings.get(i, "")
            depth_mm = det.get("depth_mm", -1)

            if self._depth_mode:
                depth_text = f"{depth_mm}" if depth_mm > 0 else "N/A"
                items = [
                    QTableWidgetItem(str(i + 1)),
                    QTableWidgetItem(cls_name),
                    QTableWidgetItem(f"{conf:.3f}"),
                    QTableWidgetItem(str(cx)),
                    QTableWidgetItem(str(cy)),
                    QTableWidgetItem(depth_text),
                    QTableWidgetItem(qr_content),
                ]
            else:
                items = [
                    QTableWidgetItem(str(i + 1)),
                    QTableWidgetItem(cls_name),
                    QTableWidgetItem(f"{conf:.3f}"),
                    QTableWidgetItem(str(cx)),
                    QTableWidgetItem(str(cy)),
                    QTableWidgetItem(qr_content),
                ]

            for idx, item in enumerate(items):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.setItem(i, idx, item)

            # 二维码高亮
            qr_col = 6 if self._depth_mode else 5
            if qr_content:
                qr_item = self.item(i, qr_col)
                if qr_item:
                    qr_item.setBackground(QBrush(QColor(0x18, 0x6D, 0x3F)))
                    qr_item.setForeground(QBrush(QColor(255, 255, 255)))

    def clear_all(self):
        """清空列表"""
        self.setRowCount(0)
