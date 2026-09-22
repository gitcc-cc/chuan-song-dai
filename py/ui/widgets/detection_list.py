"""
检测结果列表组件（产线控制专用）
===============================
职责：以表格形式实时显示YOLO检测结果，支持以下功能：
- RGB模式：6列显示（序号、类别、置信度、X坐标、Y坐标、二维码内容）
- 深度模式：7列显示（增加距离(mm)列）
- 每帧完全替换数据，不做历史累积
- 最多显示10个检测结果（按置信度降序排列）
- 二维码内容高亮显示（绿色背景）
- 支持深度相机模式切换
"""
from typing import List, Dict, Any

from PyQt6.QtWidgets import (
    QTableWidget, QTableWidgetItem, QHeaderView
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QBrush


class DetectionList(QTableWidget):
    """
    产线控制检测结果列表
    继承自QTableWidget，以表格形式显示YOLO检测结果
    """

    # RGB模式列头定义
    HEADERS_RGB = ["#", "类别", "置信度", "X", "Y", "二维码"]
    # 深度模式列头定义（增加距离列）
    HEADERS_DEPTH = ["#", "类别", "置信度", "X", "Y", "距离(mm)", "二维码"]
    MAX_DETS = 10  # 最多显示的检测结果数量

    def __init__(self, parent=None):
        """初始化检测结果列表"""
        super().__init__(parent)

        self._depth_mode = False  # 深度模式标志
        self._setup_columns(self.HEADERS_RGB)  # 默认使用RGB模式列头

        # 设置表格样式（白色背景、灰色边框、交替行背景色）
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

        # 初始化为空表格
        self.setRowCount(0)

    def _setup_columns(self, headers: list):
        """设置表格列头和列宽调整模式"""
        self.setColumnCount(len(headers))  # 设置列数
        self.setHorizontalHeaderLabels(headers)  # 设置列标题
        header = self.horizontalHeader()
        # 设置列宽调整模式
        for i in range(len(headers)):
            if i == 0:
                # 第一列（序号）：根据内容自适应宽度
                header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
            elif i == len(headers) - 1:
                # 最后一列（二维码）：拉伸填充剩余空间
                header.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
            else:
                # 其他列：根据内容自适应宽度
                header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)

    def set_depth_mode(self, enabled: bool):
        """
        切换深度模式
        启用时增加距离(mm)列，禁用时隐藏该列
        """
        if self._depth_mode == enabled:
            return  # 状态未变化，直接返回
        self._depth_mode = enabled
        # 根据模式选择对应的列头
        headers = self.HEADERS_DEPTH if enabled else self.HEADERS_RGB
        self._setup_columns(headers)

    def update_detections(self, detections: List[Dict[str, Any]],
                          qr_bindings: Dict[int, str] = None):
        """
        替换整帧检测结果（每帧完全刷新）
        detections: 检测结果列表，每个元素包含：
            - bbox: [x1, y1, x2, y2] 边界框坐标
            - conf: 置信度分数
            - cls: 类别ID
            - cls_name: 类别名称
            - depth_mm: 深度值（毫米，仅深度模式）
        qr_bindings: 二维码绑定映射 {检测索引: 二维码内容}
        """
        if qr_bindings is None:
            qr_bindings = {}

        # 如果没有检测结果，清空表格
        if not detections:
            self.setRowCount(0)
            return

        # 按置信度降序排序（高置信度优先显示）
        sorted_dets = sorted(detections, key=lambda d: d.get("conf", 0), reverse=True)
        # 限制最多显示MAX_DETS个结果
        sorted_dets = sorted_dets[:self.MAX_DETS]
        self.setRowCount(len(sorted_dets))  # 设置行数

        # 填充表格数据
        for i, det in enumerate(sorted_dets):
            bbox = det.get("bbox", [0, 0, 0, 0])
            # 计算边界框中心点坐标
            cx = int((bbox[0] + bbox[2]) / 2)
            cy = int((bbox[1] + bbox[3]) / 2)
            conf = det.get("conf", 0)  # 置信度
            cls_name = det.get("cls_name", str(det.get("cls", 0)))  # 类别名称
            qr_content = qr_bindings.get(i, "")  # 绑定的二维码内容
            depth_mm = det.get("depth_mm", -1)  # 深度值

            # 根据模式构建不同的列数据
            if self._depth_mode:
                # 深度模式：7列（增加距离列）
                depth_text = f"{depth_mm}" if depth_mm > 0 else "N/A"  # 无效深度显示N/A
                items = [
                    QTableWidgetItem(str(i + 1)),  # 序号
                    QTableWidgetItem(cls_name),     # 类别
                    QTableWidgetItem(f"{conf:.3f}"), # 置信度（3位小数）
                    QTableWidgetItem(str(cx)),      # X坐标
                    QTableWidgetItem(str(cy)),      # Y坐标
                    QTableWidgetItem(depth_text),   # 深度值
                    QTableWidgetItem(qr_content),   # 二维码内容
                ]
            else:
                # RGB模式：6列
                items = [
                    QTableWidgetItem(str(i + 1)),  # 序号
                    QTableWidgetItem(cls_name),     # 类别
                    QTableWidgetItem(f"{conf:.3f}"), # 置信度
                    QTableWidgetItem(str(cx)),      # X坐标
                    QTableWidgetItem(str(cy)),      # Y坐标
                    QTableWidgetItem(qr_content),   # 二维码内容
                ]

            # 设置所有单元格居中对齐
            for idx, item in enumerate(items):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.setItem(i, idx, item)

            # 二维码内容高亮显示（绿色背景+白色文字）
            qr_col = 6 if self._depth_mode else 5  # 二维码列索引
            if qr_content:
                qr_item = self.item(i, qr_col)
                if qr_item:
                    # 深绿色背景（#186D3F）
                    qr_item.setBackground(QBrush(QColor(0x18, 0x6D, 0x3F)))
                    qr_item.setForeground(QBrush(QColor(255, 255, 255)))

    def clear_all(self):
        """清空表格所有内容"""
        self.setRowCount(0)
