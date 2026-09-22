"""
检测结果表格
===========
QTableWidget 显示：ID、类别、置信度、坐标(x1,y1,x2,y2)、二维码内容
支持：复制、自动滚动、最大行数限制
"""
from typing import List, Dict, Any

from PyQt6.QtWidgets import (
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QMenu
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QColor, QBrush


class DetectionTable(QTableWidget):
    """检测结果表格"""

    HEADERS = ["#", "类别", "置信度", "X1", "Y1", "X2", "Y2", "二维码"]
    MAX_ROWS = 100

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setColumnCount(len(self.HEADERS))
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setShowGrid(True)
        self.setSortingEnabled(False)

        # 列宽
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)  # ID
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)           # 类别
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)  # 置信度
        for i in range(3, 7):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)           # 二维码

        # 样式
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

        # 右键菜单
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def add_detection(self, detection: Dict[str, Any], det_id: int,
                      qr_content: str = ""):
        """添加一行检测结果（插入到顶部）"""
        self.insertRow(0)

        bbox = detection.get("bbox", [0, 0, 0, 0])
        conf = detection.get("conf", 0)
        cls_name = detection.get("cls_name", str(detection.get("cls", 0)))

        items = [
            QTableWidgetItem(str(det_id)),
            QTableWidgetItem(cls_name),
            QTableWidgetItem(f"{conf:.3f}"),
            QTableWidgetItem(f"{int(bbox[0])}"),
            QTableWidgetItem(f"{int(bbox[1])}"),
            QTableWidgetItem(f"{int(bbox[2])}"),
            QTableWidgetItem(f"{int(bbox[3])}"),
            QTableWidgetItem(qr_content),
        ]

        # 居中对齐
        for idx, item in enumerate(items):
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setItem(0, idx, item)

        # 有二维码内容时高亮显示
        if qr_content:
            qr_item = self.item(0, 7)
            if qr_item:
                qr_item.setBackground(QBrush(QColor(0x18, 0x6D, 0x3F)))
                qr_item.setForeground(QBrush(QColor(255, 255, 255)))

        if self.rowCount() > self.MAX_ROWS:
            self.removeRow(self.MAX_ROWS)

    def add_detections(self, detections: List[Dict[str, Any]],
                       qr_bindings: Dict[int, str] = None,
                       start_id: int = 1):
        """批量添加（用于单帧多目标）"""
        if qr_bindings is None:
            qr_bindings = {}
        sorted_dets = sorted(detections, key=lambda d: d.get("conf", 0), reverse=True)
        for i, det in enumerate(sorted_dets):
            qr_content = qr_bindings.get(i, "")
            self.add_detection(det, start_id + i, qr_content)

    def clear_all(self):
        """清空表格"""
        self.setRowCount(0)

    def _show_context_menu(self, pos):
        """右键菜单：复制选中行"""
        menu = QMenu(self)
        copy_action = QAction("复制", self)
        copy_action.triggered.connect(self._copy_selected)
        menu.addAction(copy_action)

        copy_all_action = QAction("复制全部", self)
        copy_all_action.triggered.connect(self._copy_all)
        menu.addAction(copy_all_action)

        menu.exec(self.viewport().mapToGlobal(pos))

    def _copy_selected(self):
        """复制选中行到剪贴板（TSV 格式）"""
        selected = self.selectedItems()
        if not selected:
            return
        rows = {}
        for item in selected:
            rows.setdefault(item.row(), []).append(item)

        lines = []
        for row in sorted(rows.keys()):
            row_data = []
            for col in range(self.columnCount()):
                item = self.item(row, col)
                row_data.append(item.text() if item else "")
            lines.append("\t".join(row_data))

        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText("\n".join(lines))

    def _copy_all(self):
        """复制全部行"""
        lines = ["\t".join(self.HEADERS)]
        for row in range(self.rowCount()):
            row_data = []
            for col in range(self.columnCount()):
                item = self.item(row, col)
                row_data.append(item.text() if item else "")
            lines.append("\t".join(row_data))

        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText("\n".join(lines))
