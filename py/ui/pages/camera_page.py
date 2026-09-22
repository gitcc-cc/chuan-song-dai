"""
相机识别页面 — 单摄像头 YOLO 目标检测 + 二维码识别
====================================================

【文件职责】
本文件是"相机识别"页面的完整实现，负责：
1. 用户选择本地 YOLO 模型文件（.pt）
2. 枚举并打开系统摄像头（通过 OpenCV）
3. 实时预览摄像头画面，同时在后台线程进行 YOLO 推理
4. 将检测结果（类别、置信度、坐标）绘制在画面上，并显示在下方表格
5. 可选地对每帧进行二维码/条形码扫描
6. 支持拍照保存当前帧

【页面布局】三分栏
┌─────────────┬─────────────────────────────┐
│  左侧控制面板  │  右上：预览画面 + 工具栏        │
│  - 模型选择   │  ┌───────────────────────┐   │
│  - 摄像头选择 │  │     CameraView         │   │
│  - 推理参数   │  └───────────────────────┘   │
│  - 预留配置   ├─────────────────────────────┤
│              │  右下：检测结果表格             │
│              │  (ID, 类别, 置信度, 坐标, 二维码)│
└─────────────┴─────────────────────────────┘

【关键依赖】
- vision.worker.InferenceWorker：QThread 后台推理线程，避免卡 UI
- vision.detector.Detector：封装 YOLO 推理
- vision.camera.OpenCVCamera：封装 OpenCV 摄像头操作
- ui.widgets.CameraView：自定义 QLabel，用于显示画面+叠加检测框
- ui.widgets.DetectionTable：自定义表格，显示检测结果列表
- ui.widgets.ParamSlider：自定义滑块，用于调节推理参数

【数据流】
摄像头帧 → InferenceWorker → YOLO 推理 → 检测结果
                                    ↓
                        CameraView 叠加绘制检测框
                        DetectionTable 显示结果列表
"""
import os
from typing import List, Dict, Any

# ==================== PyQt6 控件导入 ====================
# QWidget: 所有 UI 控件的基类
# QVBoxLayout / QHBoxLayout: 垂直/水平布局管理器
# QSplitter: 可拖拽分割器，把区域分成可调大小的几块
# QGroupBox: 带标题的分组框，视觉上把相关控件归为一组
# QPushButton: 按钮
# QComboBox: 下拉选择框
# QLabel: 文本标签，也用来显示图片
# QFileDialog: 文件选择对话框（选模型文件用）
# QMessageBox: 弹窗提示（警告、错误等）
# QSizePolicy: 控件大小策略（Fixed=固定尺寸，Expanding=自动拉伸）
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QGroupBox,
    QPushButton, QComboBox, QLabel, QFileDialog, QMessageBox,
    QSizePolicy
)
# Qt: 对齐方式等枚举
# pyqtSlot: 装饰器，标记方法为 Qt 槽函数（接收信号用）
# QThread: Qt 线程类，用于后台任务不阻塞 UI
from PyQt6.QtCore import Qt, pyqtSlot, QThread
# QFont: 字体设置
from PyQt6.QtGui import QFont

# ==================== qtawesome 图标库（可选） ====================
# qtawesome 提供 FontAwesome 图标，如果没安装就用文字/emoji 代替
try:
    import qtawesome as qta
    _HAS_QTA = True
except ImportError:
    _HAS_QTA = False

# ==================== 项目内部模块 ====================
# BasePage: 所有页面的基类，提供 on_show/on_hide 生命周期方法
from ui.pages.base_page import BasePage
# ParamSlider: 自定义参数滑块（带数值显示）
# CameraView: 自定义画面显示控件（QLabel + 叠加检测框/二维码）
# DetectionTable: 检测结果表格控件
from ui.widgets import ParamSlider, CameraView, DetectionTable
# OpenCVCamera: 基于 OpenCV 的摄像头封装
# BaseCamera: 摄像头基类（定义 open/close/read 等接口）
from vision.camera import OpenCVCamera, BaseCamera
# Detector: YOLO 模型加载与推理封装
from vision.detector import Detector
# InferenceWorker: 后台推理线程，负责读帧→推理→发信号
from vision.worker import InferenceWorker
# get_config: 获取全局配置对象（读写 JSON 配置文件）
from core.config import get_config
# QObject: Qt 对象基类（信号必须定义在 QObject 子类中）
# pyqtSignal: 定义自定义信号
from PyQt6.QtCore import QObject, pyqtSignal


class WorkerSignals(QObject):
    """
    自定义信号集合 — 用于在非 QObject 类（如 Worker）中发射信号
    
    Qt 的信号/槽机制要求：
    1. 信号必须定义在 QObject 的子类中
    2. 但我们的 Worker 类可能继承自 QThread（也是 QObject 子类）
    3. 这里单独定义一个信号类，方便其他地方复用
    
    用法：self.signals.log_message.emit("消息")
    """
    log_message = pyqtSignal(str)  # 日志消息信号，携带字符串参数


class CameraOpenThread(QThread):
    """
    后台线程：异步打开摄像头
    
    【为什么需要这个线程？】
    OpenCV 的 cv2.VideoCapture.open() 是阻塞操作，如果摄像头硬件响应慢
    （比如 USB 摄像头需要初始化），会卡住整个界面。
    把 open() 放到 QThread 里执行，界面保持流畅。
    
    【信号】
    - opened(camera, index, w, h): 摄像头打开成功，传递摄像头对象、索引、分辨率
    - failed(msg): 打开失败，传递错误消息
    
    【继承关系】
    QThread → QObject → ...（所以可以直接发射信号）
    """
    opened = pyqtSignal(object, int, int, int)  # camera对象, 摄像头索引, 宽度, 高度
    failed = pyqtSignal(str)  # 失败原因

    def __init__(self, camera, index, parent=None):
        """
        参数:
            camera: OpenCVCamera 实例（已经创建但还没 open）
            index: 摄像头设备索引（如 0, 1, 2...）
            parent: 父控件（Qt 内存管理用，传 None 也行）
        """
        super().__init__(parent)
        self._camera = camera  # 保存摄像头引用
        self._index = index    # 保存要打开的摄像头索引

    def run(self):
        """
        线程执行体 — QThread.start() 时自动调用此方法
        
        注意：此方法在子线程中执行，不能直接操作 UI 控件！
        只能通过信号（emit）通知主线程更新界面。
        """
        # 调用 OpenCV 打开摄像头，width/height/fps=-1 表示使用默认值
        ok = self._camera.open(self._index, width=-1, height=-1, fps=-1)
        if ok:
            # 获取实际分辨率（摄像头可能不支持请求的分辨率）
            w, h = self._camera.get_resolution()
            # 发射成功信号 → 主线程的 _on_camera_opened() 会被调用
            self.opened.emit(self._camera, self._index, w, h)
        else:
            # 发射失败信号 → 主线程的 _on_camera_open_failed() 会被调用
            self.failed.emit(f"摄像头 {self._index} 打开失败")


class CameraPage(BasePage):
    """
    相机识别页面 — 单摄像头 YOLO 检测 + 二维码扫描
    
    继承自 BasePage，需要实现 on_show() 和 on_hide() 生命周期方法。
    
    【核心组件关系】
    CameraPage
    ├── OpenCVCamera      — 摄像头硬件接口（读帧）
    ├── Detector          — YOLO 模型（推理）
    ├── InferenceWorker   — 后台线程（协调摄像头和模型，循环推理）
    ├── CameraView        — 画面显示控件（叠加检测框/二维码标注）
    └── DetectionTable    — 结果表格控件（显示检测列表）
    
    【状态变量】
    - _running: 是否正在持续检测（相机按钮 checkable）
    - _model_loaded: 模型是否已加载成功
    - _det_id_counter: 检测批次 ID 计数器（每次检测递增）
    - _open_thread: 当前正在执行的摄像头打开线程
    - _cameras_detected: 是否已完成首次摄像头枚举
    """

    def __init__(self, parent=None):
        """
        初始化相机识别页面
        
        执行顺序：
        1. 创建信号对象
        2. 加载全局配置
        3. 创建核心组件（摄像头、检测器、推理线程）
        4. 初始化状态变量
        5. 构建界面（_setup_ui）
        6. 连接信号槽（_connect_signals）
        7. 加载保存的配置并打开摄像头（_load_saved_config）
        """
        super().__init__(parent)

        # ========== 信号 ==========
        # WorkerSignals 实例，用于发射日志等信号给上层（AppShell）
        self.signals = WorkerSignals()

        # ========== 配置 ==========
        # get_config() 返回全局配置单例，读写 JSON 文件保存用户设置
        self.config = get_config()

        # ========== 核心组件 ==========
        # _camera: 摄像头对象，负责打开硬件、读取帧
        self._camera: BaseCamera = OpenCVCamera()
        # _detector: YOLO 检测器，负责加载模型、执行推理
        self._detector = Detector()
        # _worker: 推理工作线程，循环读帧→推理→发射结果信号
        self._worker = InferenceWorker()

        # ========== 状态 ==========
        # _running: True=正在持续检测，False=仅预览或未打开
        self._running = False
        # _model_loaded: 模型是否加载成功（没模型就不能检测）
        self._model_loaded = False
        # _det_id_counter: 检测批次 ID，每开始一次新检测就重置为 0，每次检测递增
        self._det_id_counter = 0
        # _open_thread: 保存当前正在执行的摄像头打开线程引用（防止重复打开）
        self._open_thread: CameraOpenThread = None
        # _cameras_detected: 标记是否已完成首次摄像头枚举（避免重复枚举）
        self._cameras_detected = False  # 是否已检测过摄像头列表

        # ========== 构建界面 ==========
        self._setup_ui()          # 创建所有 UI 控件并布局
        self._connect_signals()   # 连接 worker 的信号到对应的槽函数
        self._load_saved_config() # 从配置文件恢复设置，并打开上次使用的摄像头

    def _setup_ui(self):
        """
        构建三分栏界面 — 这是整个页面的 UI 骨架
        
        布局结构：
        ┌──────────┬────────────────────────┐
        │  left_    │     right_splitter     │
        │  panel    │  ┌──────────────────┐  │
        │  (300px   │  │ top_widget       │  │
        │   固定宽)  │  │ (工具栏+预览画面)  │  │
        │           │  ├──────────────────┤  │
        │           │  │ bottom_widget    │  │
        │           │  │ (检测结果表格)     │  │
        │           │  └──────────────────┘  │
        └──────────┴────────────────────────┘
        
        PyQt6 布局要点：
        - QHBoxLayout: 水平排列子控件
        - QVBoxLayout: 垂直排列子控件
        - QSplitter: 可拖拽调整上下比例
        - addWidget(stretch=N): stretch 控制空间分配比例
        """
        # 整体水平布局（左面板 + 右侧面板）
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)  # 外边距（左上右下）
        main_layout.setSpacing(8)  # 子控件之间的间距

        # ============ 左侧控制面板 ============
        # 固定宽度 300px，不会随窗口缩放
        left_panel = QWidget()
        left_panel.setFixedWidth(300)
        # Fixed: 固定尺寸，Expanding: 自动拉伸填满剩余空间
        left_panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        left_layout = QVBoxLayout(left_panel)  # 面板内部垂直排列
        left_layout.setContentsMargins(0, 0, 0, 0)  # 去掉内边距，更紧凑
        left_layout.setSpacing(12)  # 各分组之间的间距

        # --- 模型选择分组 ---
        # QGroupBox 带标题边框，视觉上把相关控件归为一组
        model_group = QGroupBox("模型设置")
        model_layout = QVBoxLayout(model_group)

        # 模型选择按钮 + 路径标签（水平排列）
        model_row = QHBoxLayout()
        self.btn_model = QPushButton("选择模型 (.pt)")
        # clicked 信号连接到 _on_select_model 槽函数
        self.btn_model.clicked.connect(self._on_select_model)
        model_row.addWidget(self.btn_model, stretch=1)  # stretch=1 让按钮占满水平空间
        # 显示当前加载的模型路径（灰色小字）
        self.lbl_model_path = QLabel("未加载模型")
        self.lbl_model_path.setStyleSheet("color: #999; font-size: 11px;")
        self.lbl_model_path.setWordWrap(True)  # 路径太长时自动换行
        model_layout.addLayout(model_row)
        model_layout.addWidget(self.lbl_model_path)

        left_layout.addWidget(model_group)

        # --- 摄像头选择分组 ---
        cam_group = QGroupBox("摄像头设置")
        cam_layout = QVBoxLayout(cam_group)

        # 下拉选择框：列出系统检测到的所有摄像头
        self.cam_combo = QComboBox()
        self.cam_combo.addItem("正在检测...", -1)  # 初始占位文本，-1 表示无效索引
        # currentIndexChanged 信号：用户切换下拉选项时触发
        self.cam_combo.currentIndexChanged.connect(self._on_camera_changed)
        cam_layout.addWidget(QLabel("选择摄像头:"))
        cam_layout.addWidget(self.cam_combo)

        # 刷新按钮：重新枚举摄像头列表
        self.btn_refresh_cam = QPushButton("刷新列表")
        self.btn_refresh_cam.clicked.connect(self._refresh_cameras)
        cam_layout.addWidget(self.btn_refresh_cam)

        left_layout.addWidget(cam_group)

        # --- 推理参数分组 ---
        # ParamSlider 是自定义控件，带标题、滑块、数值显示
        param_group = QGroupBox("推理参数")
        param_layout = QVBoxLayout(param_group)

        # 置信度阈值：YOLO 输出的检测框，置信度低于此值的会被过滤掉
        self.slider_conf = ParamSlider("置信度", 0.0, 1.0,
                                       default=0.5, step=0.01, decimals=2)
        self.slider_conf.value_changed.connect(self._on_param_changed)
        param_layout.addWidget(self.slider_conf)

        # IoU 阈值：用于非极大值抑制（NMS），重叠度高于此值的检测框会被合并
        self.slider_iou = ParamSlider("IoU 阈值", 0.0, 1.0,
                                       default=0.45, step=0.01, decimals=2)
        self.slider_iou.value_changed.connect(self._on_param_changed)
        param_layout.addWidget(self.slider_iou)

        # 推理间隔：每隔多少帧做一次 YOLO 推理
        # 值为 1 = 每帧都推理（最准但最耗性能）
        # 值为 5 = 每 5 帧推理一次（省性能但可能漏检）
        self.slider_interval = ParamSlider("推理间隔（帧）", 1, 30,
                                           default=1, step=1, decimals=0)
        self.slider_interval.setToolTip("数值越大越省性能，1=每帧推理")
        self.slider_interval.value_changed.connect(self._on_interval_changed)
        param_layout.addWidget(self.slider_interval)

        # 扫码间隔：每隔多少帧做一次二维码扫描（跟随检测触发）
        self.slider_qr_interval = ParamSlider("扫码间隔（帧）", 1, 30,
                                              default=1, step=1, decimals=0)
        self.slider_qr_interval.setToolTip("跟随检测触发，1=每次检测都扫码")
        self.slider_qr_interval.value_changed.connect(self._on_qr_interval_changed)
        param_layout.addWidget(self.slider_qr_interval)

        left_layout.addWidget(param_group)

        # --- 预留：类别→指令配置 ---
        # 未来功能：检测到某个类别时自动发送 MQTT 指令
        future_group = QGroupBox("类别触发配置 (预留)")
        future_layout = QVBoxLayout(future_group)
        lbl = QLabel("此处后续添加：\n检测到 [类别] → 发送 [指令]")
        lbl.setStyleSheet("color: #999; font-size: 12px;")
        lbl.setWordWrap(True)
        future_layout.addWidget(lbl)
        left_layout.addWidget(future_group)

        # addStretch() 在底部添加弹性空间，把上面的控件"顶"到顶部
        left_layout.addStretch()
        main_layout.addWidget(left_panel)

        # ============ 右侧：上下分割 ============
        # QSplitter 可以让用户拖拽调整上下区域的比例
        right_splitter = QSplitter(Qt.Orientation.Vertical)  # Vertical = 上下分割
        right_splitter.setHandleWidth(6)  # 分割线宽度（像素）

        # 分割线样式：灰色细线，悬停变绿色
        right_splitter.setStyleSheet("""
            QSplitter::handle { background-color: #e0e0e0; height: 3px; }
            QSplitter::handle:hover { background-color: #07C160; }
        """)

        # ---- 右上：预览画面 + 工具栏 ----
        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(4)

        # ============ 工具栏 ============
        # 工具栏是一排小按钮，水平排列在预览画面上方
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)

        # ---- 拍照按钮（保存当前帧为图片） ----
        self.btn_snapshot = QPushButton()
        self.btn_snapshot.setFixedSize(40, 40)  # 固定按钮大小
        self.btn_snapshot.setToolTip("保存当前帧为图片")
        # QPushButton 样式表：正常状态灰色背景，悬停加深
        self.btn_snapshot.setStyleSheet("""
            QPushButton {
                background-color: #f0f0f0;
                border: none;           /* 去掉边框 */
                border-radius: 6px;     /* 圆角 */
            }
            QPushButton:hover { background-color: #e0e0e0; }
        """)
        if _HAS_QTA:
            # qtawesome 图标：fa5s.camera = 相机图标，scale_factor=1.2 稍微放大
            self.btn_snapshot.setIcon(qta.icon("fa5s.camera", color="#333", scale_factor=1.2))
            self.btn_snapshot.setIconSize(self.btn_snapshot.size() * 0.6)
        else:
            self.btn_snapshot.setText("📷")  # 没装 qtawesome 就用 emoji
        self.btn_snapshot.clicked.connect(self._on_snapshot)
        toolbar.addWidget(self.btn_snapshot)

        # ---- 检测一次按钮（只推理一帧，不持续检测） ----
        self.btn_detect_once = QPushButton()
        self.btn_detect_once.setFixedSize(40, 40)
        self.btn_detect_once.setToolTip("检测一次（只推理一帧）")
        self.btn_detect_once.setStyleSheet("""
            QPushButton {
                background-color: #f0f0f0;
                border: none;
                border-radius: 6px;
            }
            QPushButton:hover { background-color: #e0e0e0; }
        """)
        if _HAS_QTA:
            self.btn_detect_once.setIcon(qta.icon("fa5s.hand-pointer", color="#333", scale_factor=1.2))
            self.btn_detect_once.setIconSize(self.btn_detect_once.size() * 0.6)
        else:
            self.btn_detect_once.setText("1x")
        self.btn_detect_once.clicked.connect(self._on_detect_once)
        toolbar.addWidget(self.btn_detect_once)

        # ---- 相机按钮（开始/停止持续检测） ----
        # setCheckable(True) 让按钮有"选中/未选中"两种状态
        # 选中=绿色=正在检测，未选中=灰色=停止检测
        self.btn_camera = QPushButton()
        self.btn_camera.setCheckable(True)
        self.btn_camera.setFixedSize(40, 40)
        self.btn_camera.setStyleSheet("""
            QPushButton {
                background-color: #f0f0f0;
                border: none;
                border-radius: 6px;
            }
            QPushButton:hover { background-color: #e0e0e0; }
            QPushButton:checked {                          /* 选中状态 */
                background-color: #07C160;                 /* 绿色背景 */
                border: 2px solid #059648;                 /* 深绿色边框 */
            }
            QPushButton:checked:hover { background-color: #06AD56; }
        """)
        self._update_camera_btn_icon(False)  # 初始图标：播放图标
        # toggled 信号：按钮选中状态改变时触发（checked=True/False）
        self.btn_camera.toggled.connect(self._on_camera_toggled)
        toolbar.addWidget(self.btn_camera)

        # ---- 坐标显示按钮 ----
        # 切换是否在画面上显示检测框中心的 (x, y) 坐标
        self.btn_coords = QPushButton()
        self.btn_coords.setCheckable(True)
        self.btn_coords.setFixedSize(40, 40)
        self.btn_coords.setToolTip("显示检测框中心坐标")
        self.btn_coords.setStyleSheet("""
            QPushButton {
                background-color: #f0f0f0;
                border: none;
                border-radius: 6px;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #e0e0e0; }
            QPushButton:checked {
                background-color: #07C160;
                border: 2px solid #059648;
                color: white;
            }
            QPushButton:checked:hover { background-color: #06AD56; }
        """)
        if _HAS_QTA:
            self.btn_coords.setIcon(qta.icon("fa5s.crosshairs", color="#333", scale_factor=1.2))
            self.btn_coords.setIconSize(self.btn_coords.size() * 0.6)
        else:
            self.btn_coords.setText("XY")
        self.btn_coords.toggled.connect(self._on_coords_toggled)
        toolbar.addWidget(self.btn_coords)

        # ---- 扫码开关按钮 ----
        # 控制是否对每帧进行二维码/条形码扫描
        self.btn_qr = QPushButton()
        self.btn_qr.setCheckable(True)
        self.btn_qr.setChecked(True)  # 默认开启扫码
        self.btn_qr.setFixedSize(40, 40)
        self.btn_qr.setToolTip("启用/禁用二维码扫描")
        self.btn_qr.setStyleSheet("""
            QPushButton {
                background-color: #f0f0f0;
                border: none;
                border-radius: 6px;
                font-size: 10px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #e0e0e0; }
            QPushButton:checked {
                background-color: #07C160;
                border: 2px solid #059648;
                color: white;
            }
            QPushButton:checked:hover { background-color: #06AD56; }
        """)
        if _HAS_QTA:
            self.btn_qr.setIcon(qta.icon("fa5s-qrcode", color="#333", scale_factor=1.2))
            self.btn_qr.setIconSize(self.btn_qr.size() * 0.6)
        else:
            self.btn_qr.setText("QR")
        self.btn_qr.toggled.connect(self._on_qr_toggled)
        toolbar.addWidget(self.btn_qr)

        # addStretch() 把后面的控件推到右边
        toolbar.addStretch()

        # ---- FPS 显示 ----
        # 实时显示推理帧率（每秒处理多少帧）
        self.lbl_fps = QLabel("FPS: 0.0")
        self.lbl_fps.setFont(QFont("", 11, QFont.Weight.Bold))
        self.lbl_fps.setStyleSheet("color: #07C160;")  # 绿色文字
        toolbar.addWidget(self.lbl_fps)

        toolbar.addSpacing(10)  # 右边距

        # ---- 分辨率显示 ----
        self.lbl_res = QLabel("--x--")
        self.lbl_res.setStyleSheet("color: #999; font-size: 11px;")
        toolbar.addWidget(self.lbl_res)

        # ---- 状态消息 ----
        # 显示当前状态（"就绪"、"正在检测..."、"摄像头已关闭" 等）
        self.lbl_status = QLabel("就绪")
        self.lbl_status.setStyleSheet("color: #333; font-size: 11px;")
        self.lbl_status.setMinimumWidth(200)  # 防止文字变化时控件宽度跳动
        toolbar.addWidget(self.lbl_status)

        # 工具栏添加到顶部布局
        top_layout.addLayout(toolbar)

        # ---- 预览画面 ----
        # CameraView 是自定义的 QLabel，可以显示视频帧并叠加检测框
        self.camera_view = CameraView()
        # stretch=1 让预览画面占满剩余垂直空间
        top_layout.addWidget(self.camera_view, stretch=1)

        # 把右上区域添加到分割器
        right_splitter.addWidget(top_widget)

        # ---- 右下：检测结果表格 ----
        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(0, 0, 0, 0)

        table_label = QLabel("检测结果列表 (右键可复制)")
        table_label.setFont(QFont("", 11, QFont.Weight.Bold))
        table_label.setStyleSheet("color: #333; padding-bottom: 4px;")
        bottom_layout.addWidget(table_label)

        # DetectionTable：显示检测结果的表格（ID, 类别, 置信度, 坐标, 二维码）
        self.detection_table = DetectionTable()
        bottom_layout.addWidget(self.detection_table, stretch=1)

        right_splitter.addWidget(bottom_widget)

        # 设置默认分割比例：上 70%，下 30%（550:250 ≈ 68.75%:31.25%）
        right_splitter.setSizes([550, 250])

        # 右侧分割器占满剩余水平空间（stretch=1）
        main_layout.addWidget(right_splitter, stretch=1)

        # ============ 绑定 worker ============
        # 告诉推理线程使用哪个摄像头和检测器
        self._worker.set_camera(self._camera)
        self._worker.set_detector(self._detector)

    def _connect_signals(self):
        """
        连接 InferenceWorker 的信号到本页面的槽函数
        
        【信号/槽机制简介】
        - 信号(signal): 某个事件发生时发射，如 "帧准备好了"
        - 槽(slot): 接收信号并处理的函数
        - connect(): 把信号和槽连接起来
        - 当信号发射时，对应的槽函数自动被调用
        
        【这里的信号流】
        InferenceWorker (子线程)          CameraPage (主线程)
        ─────────────────────            ─────────────────────
        frame_ready ──────────────────→ camera_view.update_frame()
        detections_ready ─────────────→ _on_detections_ready()
        qr_results_ready ─────────────→ _on_qr_results()
        fps_updated ──────────────────→ _on_fps_updated()
        error_occurred ───────────────→ _on_worker_error()
        """
        # 帧就绪 → 更新预览画面（每帧都发）
        self._worker.frame_ready.connect(self.camera_view.update_frame)
        # 检测结果就绪 → 更新检测框叠加层和结果表格
        self._worker.detections_ready.connect(self._on_detections_ready)
        # 二维码结果就绪 → 更新二维码标注
        self._worker.qr_results_ready.connect(self._on_qr_results)
        # FPS 更新 → 更新 FPS 显示
        self._worker.fps_updated.connect(self._on_fps_updated)
        # 错误发生 → 显示错误消息并停止检测
        self._worker.error_occurred.connect(self._on_worker_error)

    def _load_saved_config(self):
        """
        加载保存的配置 — 启动时自动恢复上次的设置
        
        执行顺序：
        1. 尝试加载上次使用的模型路径
        2. 恢复推理参数（置信度、IoU、推理间隔、扫码间隔）
        3. 检测摄像头列表并打开上次使用的摄像头
        
        配置存储在 JSON 文件中，通过 get_config() 访问。
        """
        # --- 模型路径 ---
        model_path = self.config.get("model_path", "")
        if model_path and os.path.exists(model_path):
            self._load_model(model_path)  # 加载模型（如果文件还在的话）

        # --- 推理参数 ---
        # set_value(值, emit=False)：设置滑块值但不触发 value_changed 信号
        # 避免在初始化阶段重复执行参数更新逻辑
        self.slider_conf.set_value(self.config.get("conf_threshold", 0.5), emit=False)
        self.slider_iou.set_value(self.config.get("iou_threshold", 0.45), emit=False)
        self.slider_interval.set_value(self.config.get("inference_interval", 1), emit=False)
        # 把推理间隔同步给 worker
        self._worker.set_inference_interval(int(self.slider_interval.get_value()))

        self.slider_qr_interval.set_value(self.config.get("qr_interval", 1), emit=False)
        self._worker.set_qr_interval(int(self.slider_qr_interval.get_value()))

        # --- 摄像头 ---
        # 检测摄像头列表并打开上次使用的那个
        self._detect_and_open()

    # ============ 事件处理（槽函数） ============
    # 以下方法都是"槽函数"，由按钮点击或信号触发调用
    # 命名约定：_on_xxx 表示"当 xxx 事件发生时"

    def _on_select_model(self):
        """
        选择模型文件 — 弹出文件选择对话框
        
        用户点击"选择模型 (.pt)"按钮时调用。
        弹出系统文件选择器，让用户浏览并选择 YOLO 模型文件。
        """
        # getOpenFileName 返回 (文件路径, 过滤器)
        path, _ = QFileDialog.getOpenFileName(
            self,  # 父窗口
            "选择 YOLO 模型",  # 对话框标题
            self.config.get("last_model_dir", os.path.expanduser("~")),  # 初始目录
            "PyTorch Model (*.pt);;All Files (*)"  # 文件过滤器
        )
        if path:  # 用户没有取消
            # 记住这次的目录，下次打开时默认在这里
            self.config.set("last_model_dir", os.path.dirname(path))
            self._load_model(path)

    def _load_model(self, path: str):
        """
        加载 YOLO 模型文件
        
        参数:
            path: .pt 模型文件的完整路径
        
        执行流程：
        1. 更新状态栏显示"正在加载..."
        2. 调用 Detector.load() 加载模型（可能需要几秒）
        3. 成功：更新标签文字、保存配置、发射日志信号
        4. 失败：弹出错误窗口、重置状态
        
        注意：load() 是阻塞操作，耗时取决于模型大小和硬件。
        """
        self._set_status(f"正在加载模型: {os.path.basename(path)}")
        try:
            self._detector.load(path)  # 核心：加载 YOLO 模型到内存
            self._model_loaded = True  # 标记模型已加载（允许开始检测）
            # 更新标签显示模型文件名（绿色文字）
            self.lbl_model_path.setText(f"已加载: {os.path.basename(path)}")
            self.lbl_model_path.setStyleSheet("color: #07C160; font-size: 11px;")
            # 保存模型路径到配置文件
            self.config.set("model_path", path)
            self.config.save()
            self.signals.log_message.emit(f"模型加载成功: {path}")
            self._set_status(f"模型加载成功: {os.path.basename(path)}")
        except Exception as e:
            # 加载失败：弹窗提示 + 标签变红
            QMessageBox.critical(self, "加载失败", f"模型加载失败:\n{e}")
            self.lbl_model_path.setText("加载失败")
            self.lbl_model_path.setStyleSheet("color: #fa5151; font-size: 11px;")
            self._model_loaded = False
            self._set_status("模型加载失败")

    def _refresh_cameras(self):
        """
        手动刷新摄像头列表 — 点击"刷新列表"按钮时调用
        
        重新枚举系统摄像头，更新下拉框选项。
        如果有摄像头，自动打开第一个。
        """
        self._refresh_and_open()

    def _on_camera_changed(self, index: int):
        """
        切换摄像头 — 用户在下拉框选择不同摄像头时调用
        
        参数:
            index: 下拉框当前选中项的索引（不是摄像头设备索引！）
        
        每个下拉项都绑定了摄像头设备索引（通过 itemData() 获取）。
        """
        text = self.cam_combo.currentText()  # 获取显示的文字
        cam_idx = self.cam_combo.itemData(index)  # 获取绑定的摄像头设备索引
        print(f"[CameraPage] 下拉框选中: text='{text}', itemData={cam_idx}, comboIndex={index}")
        if cam_idx is not None and cam_idx >= 0:  # -1 表示"正在检测..."占位项
            self._set_status(f"正在切换到摄像头 {cam_idx}...")
            self._open_camera(cam_idx)

    def _open_camera(self, index: int):
        """
        异步打开指定摄像头 — 不阻塞 UI 界面
        
        参数:
            index: 摄像头设备索引（如 0, 1, 2...）
        
        流程：
        1. 停止当前推理 + 关闭旧摄像头
        2. 创建 CameraOpenThread 在后台打开新摄像头
        3. 打开成功/失败后通过信号回调处理结果
        
        为什么用异步？
        OpenCV 的 cv2.VideoCapture.open() 可能耗时较长（特别是 USB 摄像头），
        如果在主线程执行，界面会卡死几秒。
        """
        # ---- 步骤 1：清理旧状态 ----
        self._stop_inference()   # 停止推理循环
        self._camera.close()     # 关闭旧摄像头
        if self._worker.isRunning():
            self._worker.stop()  # 等待 worker 线程退出

        # ---- 步骤 2：启动后台线程打开摄像头 ----
        self._set_status(f"正在打开摄像头 {index}...")
        self.camera_view.clear()  # 清空预览画面

        # 创建打开线程，连接成功/失败信号
        self._open_thread = CameraOpenThread(self._camera, index)
        self._open_thread.opened.connect(self._on_camera_opened)
        self._open_thread.failed.connect(self._on_camera_open_failed)
        self._open_thread.start()  # 开始执行 run() 方法

    def _on_camera_opened(self, camera, index, w, h):
        """
        摄像头打开成功 — 由 CameraOpenThread.opened 信号触发
        
        参数:
            camera: 打开成功的摄像头对象
            index: 摄像头设备索引
            w: 实际分辨率宽度
            h: 实际分辨率高度
        
        注意：此方法在主线程执行（信号跨线程自动回到主线程），可以安全操作 UI。
        """
        self._open_thread = None  # 清理线程引用（已完成使命）
        # 更新分辨率显示
        self.lbl_res.setText(f"{w}x{h}")
        # 保存本次选择的摄像头索引到配置文件
        self.config.set("camera_index", index)
        self.config.save()

        print(f"[CameraPage] 打开摄像头 {index} ({w}x{h})")
        self._set_status(f"摄像头 {index} 已连接 ({w}x{h})")

        # 启动 worker 预览：开始循环读帧并发射 frame_ready 信号
        # 此时还不做推理，只是预览
        self._worker.start_preview()

    def _on_camera_open_failed(self, msg):
        """
        摄像头打开失败 — 由 CameraOpenThread.failed 信号触发
        
        参数:
            msg: 错误消息
        
        常见原因：
        - 摄像头被其他程序占用
        - USB 摄像头未正确连接
        - 设备权限不足（Linux 下需要用户在 video 组）
        """
        self._open_thread = None
        self._set_status(msg)
        QMessageBox.warning(self, "打开失败", msg)

    def _on_param_changed(self, value: float):
        """
        推理参数变化 — 置信度或 IoU 滑块被拖动时调用
        
        参数:
            value: 滑块当前值（float）
        
        做两件事：
        1. 把新参数同步给 worker（下次推理就用新值）
        2. 保存到配置文件（下次启动恢复）
        """
        self._worker.set_params(
            conf=self.slider_conf.get_value(),  # 置信度阈值
            iou=self.slider_iou.get_value()     # IoU 阈值
        )
        # 持久化到配置文件
        self.config.set("conf_threshold", self.slider_conf.get_value())
        self.config.set("iou_threshold", self.slider_iou.get_value())
        self.config.save()

    def _on_interval_changed(self, value: float):
        """
        推理间隔变化 — "推理间隔"滑块被拖动时调用
        
        值越大 → 越省性能（每 N 帧推理一次）
        值为 1 → 每帧都推理（最准但最耗性能）
        """
        interval = int(value)
        self._worker.set_inference_interval(interval)
        self.config.set("inference_interval", interval)
        self.config.save()

    def _on_qr_interval_changed(self, value: float):
        """
        扫码间隔变化 — "扫码间隔"滑块被拖动时调用
        
        控制二维码扫描的频率，跟随检测触发。
        值为 1 = 每次检测都扫码
        """
        interval = int(value)
        self._worker.set_qr_interval(interval)
        self.config.set("qr_interval", interval)
        self.config.save()

    def _on_camera_toggled(self, checked: bool):
        """
        相机按钮状态切换 — 开始/停止持续检测
        
        参数:
            checked: True=按钮被选中（绿色）=开始检测
                     False=按钮取消选中（灰色）=停止检测
        
        前置条件检查：
        - 必须已加载模型（_model_loaded=True）
        - 必须已打开摄像头（_camera.is_opened()=True）
        
        blockSignals(True/False) 的作用：
        当检查不通过时，需要把按钮恢复为未选中状态。
        但 setChecked(False) 会再次触发 toggled 信号，导致递归调用。
        用 blockSignals 临时屏蔽信号，避免死循环。
        """
        if checked:
            # ---- 开始检测 ----
            if not self._model_loaded:
                QMessageBox.warning(self, "未加载模型", "请先选择 .pt 模型文件")
                # 恢复按钮状态（屏蔽信号防止递归）
                self.btn_camera.blockSignals(True)
                self.btn_camera.setChecked(False)
                self.btn_camera.blockSignals(False)
                return
            if not self._camera.is_opened():
                QMessageBox.warning(self, "摄像头未打开", "请先选择摄像头")
                self.btn_camera.blockSignals(True)
                self.btn_camera.setChecked(False)
                self.btn_camera.blockSignals(False)
                return

            self._start_inference()  # 启动持续检测
        else:
            # ---- 停止检测 ----
            self._stop_inference()

        # 更新按钮图标（绿色/灰色）
        self._update_camera_btn_icon(checked)

    def _start_inference(self):
        """
        开始持续检测 — 启动 YOLO 推理循环
        
        流程：
        1. 重置检测批次 ID
        2. 清空检测结果表格
        3. 把当前参数同步给 worker
        4. 告诉 worker 开始推理
        """
        self._det_id_counter = 0  # 检测批次 ID 从 0 开始
        self.detection_table.clear_all()  # 清空旧结果
        self._worker.set_params(
            conf=self.slider_conf.get_value(),
            iou=self.slider_iou.get_value()
        )
        self._worker.enable_inference()  # 核心：worker 开始做 YOLO 推理
        self._running = True
        self._set_status("正在检测...")

    def _stop_inference(self):
        """
        停止持续检测 — worker 停止推理，但继续预览
        
        注意：只是停止 YOLO 推理，摄像头预览还在继续。
        用户仍然可以看到实时画面，只是不再叠加检测框。
        """
        self._worker.disable_inference()  # 告诉 worker 停止推理
        self._running = False
        self._set_status("预览中")

    def _update_camera_btn_icon(self, checked: bool):
        """
        更新相机按钮图标 — 根据选中状态切换图标
        
        参数:
            checked: True=正在检测（显示停止图标），False=未检测（显示播放图标）
        
        使用 qtawesome 图标库（FontAwesome），如果没安装就用文字替代。
        """
        if _HAS_QTA:
            # fa5s.video-slash = 停止视频图标（检测中）
            # fa5s.video = 播放视频图标（未检测）
            icon_name = "fa5s.video-slash" if checked else "fa5s.video"
            color = "#ffffff" if checked else "#333333"  # 选中时白色，未选中时深灰
            icon = qta.icon(icon_name, color=color, scale_factor=1.2)
            self.btn_camera.setIcon(icon)
            self.btn_camera.setIconSize(self.btn_camera.size() * 0.6)
        else:
            # 没装 qtawesome 就用简单文字
            self.btn_camera.setText("■" if checked else "▶")

    def _on_coords_toggled(self, checked: bool):
        """
        坐标显示切换 — 控制是否在画面上显示检测框中心坐标
        
        参数:
            checked: True=显示坐标，False=隐藏坐标
        """
        self.camera_view.set_show_coords(checked)  # 通知 CameraView 更新显示模式
        if _HAS_QTA:
            color = "#ffffff" if checked else "#333333"
            self.btn_coords.setIcon(qta.icon("fa5s.crosshairs", color=color, scale_factor=1.2))
            self.btn_coords.setIconSize(self.btn_coords.size() * 0.6)

    def _on_qr_toggled(self, checked: bool):
        """
        扫码开关切换 — 启用/禁用二维码扫描
        
        参数:
            checked: True=启用扫码，False=禁用扫码
        """
        self._worker.set_qr_enabled(checked)  # 通知 worker 是否执行二维码解码
        if _HAS_QTA:
            color = "#ffffff" if checked else "#333333"
            self.btn_qr.setIcon(qta.icon("fa5s-qrcode", color=color, scale_factor=1.2))
            self.btn_qr.setIconSize(self.btn_qr.size() * 0.6)
        status = "扫码已启用" if checked else "扫码已禁用"
        self._set_status(status)

    def _on_snapshot(self):
        """
        保存当前帧为图片 — 点击拍照按钮时调用
        
        流程：
        1. 检查摄像头是否打开
        2. 读取当前帧（camera.read()）
        3. 生成带时间戳的文件名
        4. 用 OpenCV 的 imwrite() 保存为 JPG
        
        保存位置：py/ui/snapshots/ 目录（自动创建）
        """
        if not self._camera.is_opened():
            QMessageBox.warning(self, "拍照失败", "摄像头未打开")
            return
        # read() 返回 (成功标志, 帧数据(numpy数组))
        ret, frame = self._camera.read()
        if not ret or frame is None:
            self._set_status("拍照失败：无法读取帧")
            return
        import cv2
        from datetime import datetime
        # 构建保存路径
        snapshot_dir = os.path.join(os.path.dirname(__file__), "..", "snapshots")
        os.makedirs(snapshot_dir, exist_ok=True)  # 目录不存在就创建
        # 用时间戳命名，避免覆盖
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(snapshot_dir, f"snapshot_{timestamp}.jpg")
        cv2.imwrite(filename, frame)  # 保存为 JPG 文件
        self._set_status(f"已保存: {filename}")

    def _on_detect_once(self):
        """
        单次检测 — 只对当前帧做一次 YOLO 推理，不持续检测
        
        适用场景：
        - 快速查看当前画面的检测结果
        - 调试模型参数（置信度、IoU）
        
        与持续检测的区别：
        - 持续检测：worker 循环推理，每帧都做
        - 单次检测：只触发一帧推理，结果出来就停
        """
        if not self._model_loaded:
            QMessageBox.warning(self, "未加载模型", "请先选择 .pt 模型文件")
            return
        if not self._camera.is_opened():
            QMessageBox.warning(self, "摄像头未打开", "请先选择摄像头")
            return
        self._det_id_counter = 0
        self.detection_table.clear_all()
        self._worker.set_params(
            conf=self.slider_conf.get_value(),
            iou=self.slider_iou.get_value()
        )
        # trigger_once()：worker 只执行一次推理就停止
        self._worker.trigger_once()
        self._set_status("单次检测中...")

    @pyqtSlot(list)  # @pyqtSlot 装饰器声明参数类型，Qt 内部会做类型检查
    def _on_detections_ready(self, detections: List[Dict[str, Any]]):
        """
        收到 YOLO 检测结果 — 由 worker.detections_ready 信号触发
        
        参数:
            detections: 检测结果列表，每个元素是一个字典：
                {
                    "class": "类别名",
                    "confidence": 0.95,  # 置信度
                    "bbox": [x1, y1, x2, y2],  # 边界框坐标（像素）
                    ...
                }
        
        做两件事：
        1. 把检测框叠加到预览画面上（CameraView 负责绘制）
        2. 把检测结果添加到表格中（DetectionTable 负责显示）
        
        qr_bindings: 二维码与检测框的绑定关系
        （哪个二维码属于哪个检测框，用于在表格中显示）
        """
        # 在画面上绘制检测框
        self.camera_view.update_detections(detections)

        # 更新检测结果表格
        if detections:
            self._det_id_counter += 1  # 检测批次 ID 递增
            # 获取二维码绑定信息（CameraView 内部维护）
            qr_bindings = self.camera_view._qr_bindings
            self.detection_table.add_detections(detections, qr_bindings, self._det_id_counter)

    @pyqtSlot(list)
    def _on_qr_results(self, qr_results: List[Dict[str, Any]]):
        """
        收到二维码解码结果 — 由 worker.qr_results_ready 信号触发
        
        参数:
            qr_results: 二维码解码结果列表，每个元素是一个字典：
                {
                    "type": "QRCODE",
                    "data": "二维码内容",
                    "bbox": [x1, y1, x2, y2],
                    ...
                }
        
        把二维码信息叠加到预览画面上（显示内容文字）。
        """
        self.camera_view.update_qr_results(qr_results)

    @pyqtSlot(float)
    def _on_fps_updated(self, fps: float):
        """
        FPS 更新 — 由 worker.fps_updated 信号触发
        
        参数:
            fps: 当前推理帧率（每秒处理帧数）
        
        同时更新工具栏的 FPS 标签和 CameraView 上的 FPS 叠加层。
        """
        self.lbl_fps.setText(f"FPS: {fps:.1f}")
        self.camera_view.update_fps(fps)

    @pyqtSlot(str)
    def _on_worker_error(self, msg: str):
        """
        Worker 错误处理 — 由 worker.error_occurred 信号触发
        
        常见错误场景：
        - 摄像头在检测过程中被拔出
        - 模型推理异常（显存不足等）
        - 帧读取失败
        
        处理方式：
        1. 打印错误到终端
        2. 更新状态栏
        3. 发射日志信号给上层
        4. 自动停止检测
        5. 恢复相机按钮为未选中状态
        """
        print(f"[CameraPage] 错误: {msg}")
        self._set_status(f"错误: {msg}")
        self.signals.log_message.emit(f"错误: {msg}")
        # 自动停止检测（防止错误状态下继续运行）
        self._stop_inference()
        # 恢复相机按钮为未选中状态
        self.btn_camera.blockSignals(True)   # 屏蔽信号防止递归
        self.btn_camera.setChecked(False)
        self.btn_camera.blockSignals(False)
        self._update_camera_btn_icon(False)  # 更新图标为播放状态

    def _set_status(self, msg: str):
        """
        更新工具栏状态栏 — 同时打印到终端方便调试
        
        参数:
            msg: 状态消息文本
        
        每次状态变化都调用此方法，保持终端日志和界面同步。
        终端日志格式：[CameraPage] 消息内容
        """
        print(f"[CameraPage] {msg}")
        self.lbl_status.setText(msg)

    # ============ BasePage 生命周期方法 ============
    # 这两个方法由 AppShell（主窗口）在切换页面时自动调用：
    # - on_show(): 切换到本页面时调用
    # - on_hide(): 切换离开本页面时调用
    
    def on_show(self):
        """
        页面显示 — 切换到本页面时调用
        
        执行逻辑：
        - 如果摄像头未打开且已完成首次枚举，则自动打开上次使用的摄像头
        - 不重新枚举摄像头列表（避免重复检测耗时）
        
        为什么不在 on_show 里枚举摄像头？
        因为枚举需要扫描所有设备，可能耗时较长。
        首次枚举在 _load_saved_config() 中完成，后续 on_show 只打开已知摄像头。
        """
        if not self._camera.is_opened() and self._cameras_detected:
            cam_idx = self.cam_combo.currentData()  # 获取当前选中的摄像头索引
            cam_name = self.cam_combo.currentText()
            print(f"[CameraPage] on_show: 选中 '{cam_name}', index={cam_idx}")
            if cam_idx is not None and cam_idx >= 0:
                self._open_camera(cam_idx)
        self._set_status("就绪")

    def on_hide(self):
        """
        页面隐藏 — 切换离开本页面时调用
        
        执行清理操作（防止后台线程继续运行浪费资源）：
        1. 停止推理
        2. 停止 worker 线程
        3. 如果正在打开摄像头，等待打开线程结束
        4. 关闭摄像头
        5. 清空预览画面
        
        为什么要在离开页面时关闭摄像头？
        因为同一时间只能有一个程序使用摄像头（除非用 GStreamer 等多路方案）。
        切换到其他页面时释放摄像头，避免冲突。
        """
        self._stop_inference()  # 停止推理
        if self._worker.isRunning():
            self._worker.stop()  # 等待 worker 线程退出
        if self._open_thread and self._open_thread.isRunning():
            # 如果正在打开摄像头，给 1 秒等待退出
            self._open_thread.quit()
            self._open_thread.wait(1000)
        self._camera.close()  # 释放摄像头硬件
        self.camera_view.clear()  # 清空画面
        self._set_status("摄像头已关闭")

    def _detect_and_open(self):
        """
        检测摄像头列表（不打开） — 填充下拉框选项
        
        流程：
        1. 清空下拉框
        2. 从共享摄像头列表获取已枚举的摄像头（避免重复检测）
        3. 把每个摄像头添加为下拉项（名称 + 设备索引）
        4. 自动选中上次保存的摄像头
        
        为什么用共享摄像头列表？
        production_page.py 也用摄像头，共享列表避免重复枚举。
        """
        self._set_status("正在检测摄像头...")
        self.cam_combo.blockSignals(True)  # 屏蔽信号，防止填充时触发 camera_changed
        self.cam_combo.clear()

        saved_idx = self.config.get("camera_index", 0)  # 上次使用的摄像头索引
        # get_shared_cameras() 返回共享的摄像头列表（来自 production_page 模块）
        from ui.pages.production_page import get_shared_cameras
        cams = get_shared_cameras()
        if not cams:
            # 没有检测到摄像头
            self.cam_combo.addItem("未检测到摄像头", -1)
            self._set_status("未检测到可用摄像头，请检查连接")
            self.cam_combo.blockSignals(False)
            return

        # 把每个摄像头添加为下拉项
        for dev in cams:
            # addItem(text, userData): userData 绑定摄像头设备索引
            self.cam_combo.addItem(dev["name"], dev["index"])
        cam_names = [d["name"] for d in cams]
        self._set_status(f"检测到 {len(cams)} 个摄像头: {cam_names}")

        # 自动选中保存的索引（如果还在列表中的话），否则选第一个
        target = saved_idx if saved_idx in [d["index"] for d in cams] else cams[0]["index"]
        for i in range(self.cam_combo.count()):
            if self.cam_combo.itemData(i) == target:
                self.cam_combo.setCurrentIndex(i)
                break
        self.cam_combo.blockSignals(False)  # 恢复信号
        self._cameras_detected = True  # 标记已完成首次枚举

    def _refresh_and_open(self):
        """
        刷新摄像头列表并打开 — 点击"刷新列表"按钮时调用
        
        与 _detect_and_open() 的区别：
        - 先重置共享摄像头列表（强制重新枚举）
        - 然后重新检测并打开
        
        适用场景：
        - 用户热插拔了摄像头
        - 首次启动时没检测到摄像头，后来插入了
        """
        from ui.pages.production_page import reset_shared_cameras
        reset_shared_cameras()  # 清空共享列表，下次获取时会重新枚举
        self._detect_and_open()
        cam_idx = self.cam_combo.currentData()
        if cam_idx is not None and cam_idx >= 0:
            self._open_camera(cam_idx)

    def save_state(self):
        """
        保存页面状态 — 退出时由上层调用
        
        把当前所有参数保存到配置文件：
        - 置信度阈值
        - IoU 阈值
        - 推理间隔
        - 扫码间隔
        
        摄像头索引和模型路径在使用时已实时保存，这里不需要再保存。
        """
        self.config.set("conf_threshold", self.slider_conf.get_value())
        self.config.set("iou_threshold", self.slider_iou.get_value())
        self.config.set("inference_interval", int(self.slider_interval.get_value()))
        self.config.set("qr_interval", int(self.slider_qr_interval.get_value()))
        self.config.save()