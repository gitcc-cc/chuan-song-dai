"""
产线控制页面 (ProductionPage)
============================
本文件负责「产线控制」页面的完整 UI 和业务逻辑。

职责概述：
  1. 管理 3 个独立的检测槽位（相机1/2/3），每个槽位可：
     - 选择一个普通 USB 摄像头（OpenCV），或
     - 1号槽位额外支持深度相机模式（ROS2 HP60C，通过 DepthWorker 订阅 RGB+深度话题）
  2. 加载 YOLO 模型并共享给所有槽位（单例模型，多 Worker 共享）
  3. 通过 MQTT 与主控传送带通信：
     - 接收指令：xj1/xj2/xj3 + 检测/扫码开关
     - 发送检测结果和二维码匹配结果
  4. UI 布局：左侧参数面板（可折叠） + 顶部工具栏（MQTT状态、启停按钮、日志） + 右侧三列预览与检测列表

页面生命周期（由 AppShell 控制）：
  on_show() → 显示时：检测摄像头、同步MQTT、自动打开已保存摄像头预览
  on_hide() → 隐藏时：关闭所有摄像头和深度Worker，断开信号

核心依赖：
  - ProductionManager：模型管理、Worker 启停、MQTT 收发（单例，跨页面共享）
  - CameraView：视频预览组件（显示帧 + FPS + 检测框）
  - DetectionList：检测结果列表组件
  - SlotConfigPanel：单个槽位的参数配置面板（摄像头选择、置信度、IoU等）
  - MqttClient：MQTT 客户端，通过 get_shared_mqtt() 获取全局单例
"""
"""
注意：本文件只添加注释，不修改任何代码逻辑。
所有注释面向 PyQt6 初学者，假设读者了解基本的 Python 和 Qt 概念。
"""
import os
from typing import Optional

# ============ PyQt6 核心模块导入 ============
# QWidget: 所有 UI 控件的基类
# QVBoxLayout/QHBoxLayout: 垂直/水平布局管理器
# QGroupBox: 带标题的分组框，用于将相关控件组合在一起
# QPushButton: 按钮控件，可连接 clicked 信号执行槽函数
# QLabel: 文本/图片标签，用于显示静态或动态信息
# QFileDialog: 文件选择对话框，让用户选择模型文件
# QSizePolicy: 控件尺寸策略，决定控件在布局中如何伸缩
# QFrame: 简单的框架控件，常用于分隔线
# QTextEdit: 多行文本编辑框，本文件用它显示运行日志（只读模式）
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QPushButton, QLabel, QFileDialog, QSizePolicy, QFrame, QTextEdit
)

# Qt: Qt 枚举命名空间（如 AlignmentFlag.AlignCenter）
# pyqtSignal: 定义自定义信号（类属性），可在运行时 emit 发射
# QThread: 后台线程基类，用于在不阻塞 UI 的情况下执行耗时操作（如模型加载）
# QTimer: 定时器，每隔固定毫秒触发一次 timeout 信号
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QTimer

# ============ 项目内部模块导入 ============
# BasePage: 所有页面的基类，提供 on_show/on_hide 生命周期方法
from ui.pages.base_page import BasePage
# CameraView: 视频预览组件，支持显示帧、FPS、检测框和二维码
from ui.widgets.camera_view import CameraView
# DetectionList: 检测结果列表组件，显示检测到的物体名称、置信度等
from ui.widgets.detection_list import DetectionList
# SlotConfigPanel: 单个槽位的参数配置面板（摄像头选择、置信度滑块等）
from ui.widgets.slot_config_panel import SlotConfigPanel
# ProductionManager: 产线管理器（单例），负责模型加载、Worker 启停、MQTT 收发
# RestartCameraThread: 后台重启深度相机 ROS2 节点的线程
from vision.production_manager import ProductionManager, RestartCameraThread
# BaseCamera: 摄像头基类，提供 list_cameras() 静态方法枚举系统摄像头
from vision.camera import BaseCamera
# match_qr_to_detections: 将二维码扫描结果与当前帧的检测框进行匹配绑定
from vision.qr_decoder import match_qr_to_detections
# get_production_config: 获取产线配置的全局单例（JSON 存储）
from core.config import get_production_config
# MqttClient: MQTT 客户端类，封装了与主控传送带的通信协议
from core.mqtt_client import MqttClient
# get_shared_mqtt: 获取全局唯一的 MqttClient 实例（所有页面共用）
# connect_shared: 通过全局实例连接 MQTT broker
from core.mqtt_shared import get_shared_mqtt, connect_shared

# ============ 全局共享摄像头缓存 ============
# 全局变量，在模块级别存储，所有页面实例共享同一份摄像头列表
# 避免重复检测系统摄像头（检测耗时约 1-2 秒）
# None 表示尚未检测，[] 表示检测过但未找到摄像头
_shared_cameras = None


def get_shared_cameras():
    """
    获取共享的摄像头列表（首次调用时检测）。

    工作流程：
    1. 检查 _shared_cameras 是否为 None（首次调用）
    2. 如果是，调用 BaseCamera.list_cameras(10) 枚举系统摄像头（最多 10 个）
    3. 将结果缓存到 _shared_cameras 全局变量
    4. 返回摄像头列表，格式：[{"index": int, "name": str}, ...]

    返回值示例：
    [{"index": 0, "name": "USB Camera 1"}, {"index": 2, "name": "USB Camera 2"}]
    """
    global _shared_cameras
    if _shared_cameras is None:
        cams = BaseCamera.list_cameras(10)  # 枚举系统摄像头，最多 10 个
        _shared_cameras = cams if cams else []
    return _shared_cameras


def reset_shared_cameras():
    """
    清除共享摄像头缓存，下次调用 get_shared_cameras() 时重新检测。

    使用场景：用户点击「刷新摄像头列表」按钮后调用，
    强制重新枚举系统摄像头（例如热插拔了新的 USB 摄像头）。
    """
    global _shared_cameras
    _shared_cameras = None


class ModelLoadThread(QThread):
    """
    后台加载 YOLO 模型的线程。

    为什么需要这个线程？
    YOLO 模型加载（ultralytics.YOLO(path)）可能耗时 1-5 秒，
    如果在主线程执行，会冻结整个 UI，让用户感觉程序卡死。
    用 QThread 在后台加载，UI 保持响应，加载完成后通过信号通知主线程。

    PyQt6 多线程通信规则：
    - 子线程不能直接操作 UI 控件（会崩溃）
    - 必须通过 pyqtSignal 发射信号，主线程的槽函数接收后更新 UI
    - loaded 信号的参数：(bool 成功/失败, str 消息)

    使用方式：
    thread = ModelLoadThread("model.pt")
    thread.loaded.connect(self._on_model_loaded)  # 连接槽函数
    thread.start()  # 启动后台线程
    """

    # 定义信号：模型加载完成后发射
    # 参数：(success: bool, message: str)
    # connected 到槽函数后，槽函数在主线程执行，可以安全操作 UI
    loaded = pyqtSignal(bool, str)  # (success, message)

    def __init__(self, path: str, parent=None):
        """
        初始化模型加载线程。

        参数：
            path: YOLO 模型文件的绝对路径（.pt 文件）
            parent: 父对象（Qt 对象树，用于自动内存管理）
        """
        super().__init__(parent)
        self._path = path  # 保存模型路径，供 run() 方法使用

    def run(self):
        """
        线程的主函数，start() 后在新线程中执行。

        注意：这个方法在子线程中运行，绝对不能操作任何 UI 控件！
        只能做计算/IO 操作，结果通过信号发射回主线程。
        """
        try:
            # 动态导入 ultralytics（避免启动时就加载庞大的 PyTorch）
            from ultralytics import YOLO
            # 加载模型（这是耗时操作，可能需要几秒）
            model = YOLO(self._path)
            # 获取模型支持的类别名称（如 {"0": "person", "1": "car", ...}）
            names = model.names if hasattr(model, 'names') else {}
            # 发射成功信号，主线程的 _on_model_loaded 会收到
            self.loaded.emit(True, f"已加载 {len(names)} 个类别")
        except Exception as e:
            # 加载失败，发射错误信号（不会崩溃，只是报告错误）
            self.loaded.emit(False, str(e))


class ProductionPage(BasePage):
    """
    产线控制页面 —— 本文件的核心类。

    继承自 BasePage，实现 on_show()/on_hide() 生命周期方法，
    由 AppShell 的 QStackedWidget 在页面切换时自动调用。

    整体架构：
    ┌─────────────────────────────────────────────────┐
    │  左面板（可折叠）                                │
    │  ├─ 模型选择按钮 + 路径显示                      │
    │  ├─ 刷新摄像头列表按钮                           │
    │  ├─ 槽位1 配置面板（SlotConfigPanel）            │
    │  ├─ 槽位2 配置面板                              │
    │  └─ 槽位3 配置面板                              │
    ├─────────────────────────────────────────────────┤
    │  顶部工具栏                                      │
    │  ├─ MQTT 状态指示灯 + 标签                       │
    │  ├─ 相机1 [检测] [扫码] | 相机2 [检测] [扫码]   │
    │  ├─ 相机3 [检测] [扫码] | [XY] 坐标开关         │
    │  ├─ 运行日志（单行）                             │
    │  └─ 通讯日志（MQTT 收发，单行）                  │
    ├─────────────────────────────────────────────────┤
    │  右侧三列（每列 = 一个槽位）                     │
    │  ├─ 标题（相机X）                               │
    │  ├─ CameraView 预览画面（右上角 FPS）            │
    │  └─ DetectionList 检测结果列表                   │
    └─────────────────────────────────────────────────┘

    关键组件关系：
    - ProductionManager（单例）：管理模型和所有 Worker，处理 MQTT 收发
    - 3 个 SlotConfigPanel：每个槽位的参数配置（摄像头、置信度、IoU 等）
    - 3 个 CameraView：视频预览（帧来自 Worker 的 frame_ready 信号）
    - 3 个 DetectionList：检测结果展示（数据来自 Worker 的 detections_ready 信号）
    """

    # 按钮通用样式表（可复用，避免重复定义）
    # QPushButton:checked 状态表示按钮被「选中/激活」（绿色背景 + 白色文字）
    _BTN_STYLE = """
        QPushButton {
            background-color: #f0f0f0;
            border: none;
            border-radius: 6px;
            padding: 4px 10px;
            font-size: 12px;
        }
        QPushButton:hover { background-color: #e0e0e0; }
        QPushButton:checked {
            background-color: #07C160;
            color: white;
        }
    """

    def __init__(self, parent=None):
        """
        初始化产线控制页面。

        参数：
            parent: 父 QWidget（通常是 AppShell 的 QStackedWidget）

        初始化流程：
        1. 调用父类构造函数
        2. 获取产线配置单例
        3. 创建 ProductionManager（模型管理器）
        4. 连接 Manager 的信号到页面槽函数
        5. 初始化各种 UI 组件引用列表
        6. 启动 MQTT 状态轮询定时器
        7. 构建 UI 布局
        8. 加载保存的配置
        """
        super().__init__(parent)

        # 获取产线配置的全局单例（JSON 文件存储，跨页面共享）
        self.config = get_production_config()

        # MQTT 客户端引用（初始为 None，连接后赋值）
        self._mqtt: Optional[MqttClient] = None

        # 创建产线管理器（单例）—— 核心业务逻辑都在这里
        self._manager = ProductionManager()

        # ============ 连接 Manager 信号到页面槽函数 ============
        # 信号-槽是 PyQt6 的核心机制：当 Manager 发射信号时，页面的对应方法会被调用
        # 这实现了 Manager（数据层）和 Page（UI层）的解耦
        self._manager.slot_started.connect(self._on_slot_started)        # 槽位启动成功
        self._manager.mqtt_cmd_received.connect(self._on_mqtt_cmd_received)  # 收到 MQTT 指令
        self._manager.detection_toggled.connect(self._on_detection_toggled)  # 检测开关变化
        self._manager.qr_toggled.connect(self._on_qr_toggled)          # 扫码开关变化

        # 模型加载线程引用（加载完成后自动销毁，这里只是保留引用防止被 GC 回收）
        self._model_load_thread: Optional[ModelLoadThread] = None

        # ============ 每个槽位的 UI 组件引用列表 ============
        # 3 个槽位对应 3 个元素，索引 0-2 对应 槽位1-3
        self._slot_views: list[CameraView] = []           # 视频预览组件
        self._slot_lists: list[DetectionList] = []        # 检测结果列表
        self._slot_start_btns: list[QPushButton] = []     # 检测启停按钮
        self._slot_qr_btns: list[QPushButton] = []        # 扫码启停按钮
        self._slot_status_labels: list[QLabel] = []       # 状态标签（预留，未使用）
        self._slot_config_panels: list[SlotConfigPanel] = []  # 参数配置面板
        self._slot_qr_bindings: list[dict] = [{} for _ in range(3)]  # 二维码与检测框的绑定关系

        # 日志面板（运行日志 + 通讯日志，在 _setup_ui 中创建）
        self._log_widget: QTextEdit = None

        # ============ MQTT 状态轮询定时器 ============
        # 每秒检查一次共享 MQTT 连接状态，更新 UI 指示灯
        # 为什么不用信号？因为 MQTT 连接状态可能在页面外部变化（如传送带页面连接）
        self._mqtt_timer = QTimer(self)
        self._mqtt_timer.timeout.connect(self._poll_mqtt_status)  # 每次超时执行轮询
        self._mqtt_timer.start(1000)  # 间隔 1000 毫秒 = 1 秒

        # 构建 UI 布局（所有控件的创建和排列）
        self._setup_ui()
        # 加载已保存的配置（模型路径、各槽位参数等）
        self._load_config()

    def _setup_ui(self):
        """
        构建整个页面的 UI 布局。

        布局结构：
        主布局（QHBoxLayout）：
        ├─ 左面板容器（QWidget）
        │   ├─ 折叠按钮（16px 宽的竖条，点击可隐藏/显示左面板）
        │   └─ 左面板（QWidget，固定宽度 280px）
        │       ├─ 模型设置分组框
        │       ├─ 刷新摄像头列表按钮
        │       ├─ 槽位1 配置面板
        │       ├─ 槽位2 配置面板
        │       └─ 槽位3 配置面板
        └─ 右侧容器（QWidget，stretch=1 占满剩余空间）
            ├─ 顶部工具栏（QHBoxLayout）
            │   ├─ MQTT 状态圆点 + 标签
            │   ├─ 分隔线
            │   ├─ 相机1 [检测] [扫码] | 相机2 [检测] [扫码] | 相机3 [检测] [扫码]
            │   ├─ [XY] 坐标开关按钮
            │   ├─ 分隔线
            │   ├─ 运行日志 QTextEdit
            │   └─ 通讯日志 QTextEdit
            └─ 三列预览（QHBoxLayout）
                ├─ 列1（相机1：标题 + CameraView + DetectionList）
                ├─ 列2（相机2）
                └─ 列3（相机3）

        注意：QHBoxLayout 中的 stretch 参数控制控件如何分配剩余空间。
        stretch=1 表示等比例分配，不设置 stretch 的控件保持固定宽度。
        """
        # 主布局：水平排列（左面板 + 右侧面板）
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)  # 外边距：上下左右各 6px
        main_layout.setSpacing(6)  # 控件间距：6px

        # ============ 左面板（参数配置区） ============
        # 左面板容器：包含折叠按钮和实际内容面板
        left_container = QWidget()
        left_container_layout = QHBoxLayout(left_container)  # 水平排列：折叠按钮 + 内容
        left_container_layout.setContentsMargins(0, 0, 0, 0)  # 无外边距
        left_container_layout.setSpacing(0)  # 无间距，折叠按钮紧贴内容

        # --- 折叠按钮（竖条） ---
        # 点击后隐藏/显示左面板，给右侧更多空间
        # ▶ 表示面板已折叠（点击展开），◀ 表示面板已展开（点击折叠）
        self._left_toggle_btn = QPushButton("◀")
        self._left_toggle_btn.setFixedWidth(16)  # 固定宽度 16px，很窄的竖条
        self._left_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)  # 鼠标悬停时显示手型光标
        self._left_toggle_btn.setStyleSheet("""
            QPushButton {
                background-color: #e0e0e0;
                border: none;
                font-size: 10px;
                color: #666;
            }
            QPushButton:hover { background-color: #ccc; }
        """)
        # 连接点击信号到折叠方法
        self._left_toggle_btn.clicked.connect(self._on_toggle_left_panel)
        left_container_layout.addWidget(self._left_toggle_btn)

        # --- 左侧内容面板 ---
        # 固定宽度 280px，不会随窗口大小变化
        self._left_panel = QWidget()
        self._left_panel.setFixedWidth(280)
        # QSizePolicy.Fixed 表示控件不希望被拉伸或压缩
        self._left_panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        left_layout = QVBoxLayout(self._left_panel)  # 垂直排列各配置项
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)  # 控件间距 8px

        # --- 模型选择分组框 ---
        # QGroupBox 自带标题和边框，将模型选择相关控件组合在一起
        model_group = QGroupBox("模型设置")
        model_layout = QVBoxLayout(model_group)  # 内部垂直布局

        # 模型选择按钮行
        model_row = QHBoxLayout()
        self.btn_model = QPushButton("选择模型 (.pt)")  # 点击后弹出文件选择对话框
        self.btn_model.clicked.connect(self._on_select_model)  # 连接点击信号
        model_row.addWidget(self.btn_model, stretch=1)  # stretch=1 让按钮占满整行
        model_layout.addLayout(model_row)

        # 模型路径显示标签（初始显示"未加载模型"，灰色文字）
        self.lbl_model_path = QLabel("未加载模型")
        self.lbl_model_path.setStyleSheet("color: #999; font-size: 11px;")
        self.lbl_model_path.setWordWrap(True)  # 长路径自动换行
        model_layout.addWidget(self.lbl_model_path)

        left_layout.addWidget(model_group)

        # --- 刷新摄像头列表按钮 ---
        # 热插拔摄像头后点击此按钮重新枚举
        self.btn_refresh_cam = QPushButton("刷新摄像头列表")
        self.btn_refresh_cam.clicked.connect(self._on_refresh_cameras)
        left_layout.addWidget(self.btn_refresh_cam)

        # --- 三个槽位的参数配置面板 ---
        # 循环创建 3 个 SlotConfigPanel，每个面板管理一个槽位的参数
        # SlotConfigPanel 内部包含：摄像头下拉列表、置信度滑块、IoU 滱块、深度模式开关等
        for i in range(3):
            panel = SlotConfigPanel(slot_id=i + 1)  # slot_id 从 1 开始
            # 连接面板的信号到页面的槽函数：
            panel.camera_changed.connect(self._on_camera_selected)      # 摄像头选择变化
            panel.param_changed.connect(self._on_param_changed)        # 参数滑块变化
            panel.depth_mode_changed.connect(self._on_depth_mode_changed)  # 深度模式开关变化
            panel.restart_camera_clicked.connect(self._on_restart_camera)   # 手动重启相机节点
            self._slot_config_panels.append(panel)  # 保存引用，后续需要访问
            left_layout.addWidget(panel)

        # addStretch() 在底部添加弹性空间，让上面的控件靠上排列
        left_layout.addStretch()
        left_container_layout.addWidget(self._left_panel, stretch=1)
        main_layout.addWidget(left_container)

        # ============ 右侧区域（工具栏 + 三列预览） ============
        right_container = QWidget()
        right_container_layout = QVBoxLayout(right_container)  # 垂直排列：工具栏 + 三列
        right_container_layout.setContentsMargins(0, 0, 0, 0)
        right_container_layout.setSpacing(6)

        # --- 顶部工具栏 ---
        # 水平排列：MQTT状态 + 槽位按钮 + XY开关 + 日志
        toolbar_layout = QHBoxLayout()
        toolbar_layout.setContentsMargins(8, 4, 8, 4)
        toolbar_layout.setSpacing(12)

        # --- MQTT 状态指示灯 ---
        # ● 圆点符号，绿色表示已连接，红色表示未连接
        # 通过 CSS 动态切换颜色
        self._mqtt_status_dot = QLabel("●")
        self._mqtt_status_dot.setFixedSize(16, 16)  # 固定 16x16 像素
        self._mqtt_status_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mqtt_status_dot.setStyleSheet("color: #ccc; font-size: 14px;")  # 初始灰色（未知状态）
        toolbar_layout.addWidget(self._mqtt_status_dot)

        # MQTT 状态文字标签
        self._mqtt_status_label = QLabel("MQTT 未连接")
        self._mqtt_status_label.setStyleSheet("font-size: 12px; color: #999;")
        toolbar_layout.addWidget(self._mqtt_status_label)

        # --- 分隔线 ---
        # QFrame.Shape.VLine 创建一条垂直分隔线，视觉上分隔不同功能区
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #ddd;")
        sep.setFixedHeight(24)  # 高度 24px，与按钮高度匹配
        toolbar_layout.addWidget(sep)

        # --- 三个槽位的启停 + 扫码按钮 ---
        # 每个槽位有 2 个按钮：「检测」控制 YOLO 推理，「扫码」控制二维码识别
        # setCheckable(True) 让按钮有选中/未选中两种状态（toggle 效果）
        for i in range(3):
            slot_id = i + 1  # 槽位编号从 1 开始

            # 槽位标签（"相机1"、"相机2"、"相机3"）
            lbl = QLabel(f"相机{slot_id}")
            lbl.setStyleSheet("font-weight: bold; font-size: 12px;")
            toolbar_layout.addWidget(lbl)

            # 「检测」按钮 —— 控制 YOLO 检测的开启/关闭
            # lambda checked, s=slot_id: ... 是为了在循环中正确捕获 slot_id 值
            # 不用默认参数 s=slot_id 的话，所有按钮都会用最后一个 i 的值
            btn_start = QPushButton("检测")
            btn_start.setCheckable(True)  # 可切换状态
            btn_start.setFixedHeight(30)
            btn_start.setStyleSheet(self._BTN_STYLE)
            btn_start.clicked.connect(lambda checked, s=slot_id: self._on_toggle_slot(s))
            toolbar_layout.addWidget(btn_start)
            self._slot_start_btns.append(btn_start)

            # 「扫码」按钮 —— 控制二维码识别的开启/关闭
            # 初始选中（setChecked(True)），表示默认开启扫码
            btn_qr = QPushButton("扫码")
            btn_qr.setCheckable(True)
            btn_qr.setChecked(True)  # 默认开启
            btn_qr.setFixedHeight(30)
            btn_qr.setStyleSheet(self._BTN_STYLE)
            btn_qr.clicked.connect(lambda checked, s=slot_id: self._on_toggle_qr(s))
            toolbar_layout.addWidget(btn_qr)
            self._slot_qr_btns.append(btn_qr)

            # 槽位之间加分隔线（最后一个槽位后面不加）
            if i < 2:
                sep2 = QFrame()
                sep2.setFrameShape(QFrame.Shape.VLine)
                sep2.setStyleSheet("color: #ddd;")
                sep2.setFixedHeight(24)
                toolbar_layout.addWidget(sep2)

        # --- XY 坐标显示开关 ---
        # 点击后在所有 CameraView 上显示/隐藏鼠标坐标
        self.btn_show_xy = QPushButton("XY")
        self.btn_show_xy.setCheckable(True)
        self.btn_show_xy.setFixedHeight(30)
        self.btn_show_xy.setFixedWidth(40)
        self.btn_show_xy.setStyleSheet(self._BTN_STYLE)
        self.btn_show_xy.setToolTip("显示/隐藏坐标")  # 鼠标悬停提示
        self.btn_show_xy.clicked.connect(self._on_toggle_xy)
        toolbar_layout.addWidget(self.btn_show_xy)

        # --- 分隔线 ---
        sep3 = QFrame()
        sep3.setFrameShape(QFrame.Shape.VLine)
        sep3.setStyleSheet("color: #ddd;")
        sep3.setFixedHeight(24)
        toolbar_layout.addWidget(sep3)

        # --- 运行日志面板 ---
        # 只读 QTextEdit，固定高度 28px（单行显示），用于显示程序运行日志
        # append() 添加新行，自动滚动到底部
        self._log_widget = QTextEdit()
        self._log_widget.setReadOnly(True)
        self._log_widget.setFixedHeight(28)
        self._log_widget.setStyleSheet("""
            QTextEdit {
                background-color: transparent;
                color: #000000;
                border: 1px solid #eee;
                font-family: Monospace, Consolas, monospace;  # 等宽字体
                font-size: 10px;
                padding: 2px 4px;
            }
        """)
        toolbar_layout.addWidget(self._log_widget, stretch=1)  # stretch=1 占满剩余空间

        # --- 通讯日志面板 ---
        # 专门显示 MQTT 收发的消息，与运行日志分开便于调试
        self._com_log_widget = QTextEdit()
        self._com_log_widget.setReadOnly(True)
        self._com_log_widget.setFixedHeight(28)
        self._com_log_widget.setStyleSheet("""
            QTextEdit {
                background-color: transparent;
                color: #000000;
                border: 1px solid #eee;
                font-family: Monospace, Consolas, monospace;
                font-size: 10px;
                padding: 2px 4px;
            }
        """)
        toolbar_layout.addWidget(self._com_log_widget, stretch=1)

        # 工具栏右侧添加弹性空间，让左边的控件靠左排列
        toolbar_layout.addStretch()
        right_container_layout.addLayout(toolbar_layout)

        # --- 三列预览 + 检测列表 ---
        # 每列 = 一个槽位的预览画面 + 检测结果列表
        columns_layout = QHBoxLayout()  # 水平排列三列
        columns_layout.setSpacing(6)

        for i in range(3):
            slot_id = i + 1
            # 调用辅助方法创建单列 UI
            col = self._create_slot_column(slot_id)
            columns_layout.addWidget(col, stretch=1)  # stretch=1 三列等宽

        right_container_layout.addLayout(columns_layout, stretch=1)  # stretch=1 占满剩余高度
        main_layout.addWidget(right_container, stretch=1)  # stretch=1 占满剩余宽度

    def _create_slot_column(self, slot_id: int) -> QWidget:
        """
        创建单个槽位的列 UI。

        每个槽位的列布局（垂直排列）：
        ┌─────────────────┐
        │  相机X（标题）    │
        ├─────────────────┤
        │                 │
        │  CameraView     │  ← 视频预览（stretch=3，占大部分空间）
        │  （预览画面）     │
        │                 │
        ├─────────────────┤
        │  DetectionList  │  ← 检测结果列表（stretch=1，固定高度上限 180px）
        └─────────────────┘

        参数：
            slot_id: 槽位编号（1/2/3）

        返回：
            包含标题、预览、检测列表的 QWidget
        """
        col = QWidget()
        layout = QVBoxLayout(col)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # 槽位标题（"相机1"、"相机2"、"相机3"）
        title = QLabel(f"相机{slot_id}")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)  # 居中对齐
        title.setStyleSheet("font-weight: bold; font-size: 14px; padding: 2px;")
        layout.addWidget(title)

        # --- 视频预览组件 ---
        # CameraView 是自定义组件，内部用 QLabel 显示帧图像
        # 支持：帧显示、FPS 计算、检测框绘制、二维码标记、鼠标坐标显示
        view = CameraView()
        view.setMinimumSize(300, 200)  # 最小尺寸，防止窗口太小时预览变形
        layout.addWidget(view, stretch=3)  # stretch=3 占 3 份空间
        self._slot_views.append(view)  # 保存引用，后续更新帧时需要访问

        # --- 检测结果列表 ---
        # DetectionList 是自定义组件，显示检测到的物体信息
        # 每行：物体名称 + 置信度 + 深度值（如果有）+ 二维码（如果有）
        det_list = DetectionList()
        det_list.setMaximumHeight(180)  # 最大高度 180px，防止列表太长挤压预览
        layout.addWidget(det_list, stretch=1)  # stretch=1 占 1 份空间
        self._slot_lists.append(det_list)

        return col

    # ============ 模型加载 ============
    def _on_select_model(self):
        """
        点击「选择模型」按钮后执行。

        流程：
        1. 弹出文件选择对话框，让用户选择 .pt 文件
        2. 禁用按钮（防止重复点击）
        3. 启动 ModelLoadThread 在后台加载模型
        4. 加载完成后 _on_model_loaded 会被调用

        注意：QFileDialog.getOpenFileName() 是阻塞调用，
        但只阻塞当前方法，不会冻结整个 UI。
        """
        # 弹出文件选择对话框
        # 参数：父窗口、标题、初始目录、文件过滤器
        # 返回：(文件路径, 过滤器描述)；用户取消则路径为空字符串
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 YOLO 模型", "",
            "模型文件 (*.pt);;所有文件 (*)"  # 两种过滤器，;; 分隔
        )
        if not path:  # 用户取消了选择
            return

        # 禁用按钮，防止用户在加载过程中重复点击
        self.btn_model.setEnabled(False)
        # 显示加载状态（模型文件名 + "加载中..."）
        self.lbl_model_path.setText(f"加载中: {os.path.basename(path)} ...")

        # 创建后台线程加载模型
        self._model_load_thread = ModelLoadThread(path)
        # 连接 loaded 信号到回调
        # lambda 中的 p=path 是为了在闭包中正确捕获 path 值
        self._model_load_thread.loaded.connect(
            lambda ok, msg, p=path: self._on_model_loaded(ok, msg, p))
        self._model_load_thread.start()  # 启动线程，run() 方法开始执行

    def _on_model_loaded(self, ok: bool, message: str, path: str):
        """
        模型加载完成的回调（在主线程执行，可以安全操作 UI）。

        参数：
            ok: 是否加载成功
            message: 成功时显示类别数量，失败时显示错误信息
            path: 模型文件路径
        """
        # 重新启用按钮
        self.btn_model.setEnabled(True)
        if ok:
            # 加载成功 → 把模型注册到 ProductionManager
            # Manager 内部会保存模型引用，后续所有 Worker 共享这个模型
            if self._manager:
                self._manager.load_model(path)
            # 更新 UI 显示
            self.lbl_model_path.setText(f"已加载: {os.path.basename(path)}")
            self.lbl_model_path.setStyleSheet("color: #07C160; font-size: 11px;")  # 绿色表示成功
            # 保存模型路径到配置文件，下次启动自动加载
            self.config.set("model_path", path)
            self.config.save()
        else:
            # 加载失败 → 显示红色错误信息
            self.lbl_model_path.setText(f"加载失败: {message}")
            self.lbl_model_path.setStyleSheet("color: #e74c3c; font-size: 11px;")  # 红色表示失败

    # ============ 槽位启停 ============
    def _on_camera_selected(self, slot_id: int, cam_idx: int):
        """
        用户在 SlotConfigPanel 中选择了摄像头后执行。

        两种情况：
        1. cam_idx < 0：用户选择了「请选择摄像头」→ 停止该槽位的预览和检测
        2. cam_idx >= 0：用户选择了具体摄像头 → 启动预览（不启动检测）

        注意：这里只启动预览（显示画面），不启动 YOLO 检测。
        检测需要用户再点击「检测」按钮才会启动。

        参数：
            slot_id: 槽位编号（1/2/3）
            cam_idx: 摄像头索引（-1 表示未选择，>=0 表示具体摄像头）
        """
        idx = slot_id - 1  # 转为 0-based 索引，方便访问列表

        if cam_idx < 0:
            # 用户选择了「请选择摄像头」→ 停止该槽位
            if self._manager and self._manager.is_running(slot_id):
                # 先断开 Worker 的信号连接，防止残留帧覆盖 clear 操作
                # 这是一个常见的 PyQt6 坑：信号未断开时，旧的帧会继续发送到已清空的 View
                worker = self._manager._workers[idx] or self._manager._depth_workers[idx]
                if worker:
                    try:
                        # disconnect() 不带参数会断开该信号的所有连接
                        # 如果信号本来就没连接，会抛 TypeError，用 try/except 忽略
                        worker.frame_ready.disconnect()
                        worker.detections_ready.disconnect()
                    except TypeError:
                        pass  # 信号未连接，忽略错误
                self._manager.stop_slot(slot_id)  # 停止 Worker
                # 重置按钮状态
                self._slot_start_btns[idx].setChecked(False)
                self._slot_start_btns[idx].setText("检测")
                self._slot_lists[idx].clear_all()  # 清空检测列表
                self._slot_views[idx].clear()       # 清空预览画面
            return

        # 检查模型是否已加载（没有模型就无法预览，因为 Worker 需要模型来处理帧）
        if not self._manager or not self._manager.model_loaded:
            self._log(f"槽位{slot_id}: 模型未加载，跳过")
            return

        # 如果该槽位已在运行，先停止（再用新摄像头启动）
        if self._manager.is_running(slot_id):
            self._manager.stop_slot(slot_id)

        # 获取用户配置的参数（置信度、IoU、推理间隔等）
        params = self._slot_config_panels[idx].get_params()
        # 启动槽位预览（Manager 内部创建 Worker 线程，打开摄像头，开始采集帧）
        ok = self._manager.start_slot(slot_id, cam_idx, params)

        # 更新按钮状态（启动成功后按钮显示"启动"，表示可以点击启动检测）
        btn = self._slot_start_btns[slot_id - 1]
        btn.setChecked(False)  # 未选中 = 检测未开启
        btn.setText("启动")
        self._log(f"槽位{slot_id}: 预览{'成功' if ok else '失败'}")

    def _on_toggle_slot(self, slot_id: int):
        """
        点击「检测」按钮后执行 —— 启动/停止 YOLO 检测。

        这是最复杂的方法之一，因为要处理两种模式（普通/深度）和两种状态（已运行/未运行）。

        状态转换：
        ┌──────────────┐     点击按钮     ┌──────────────┐
        │  未运行       │  ───────────→  │  启动中...    │
        │  （按钮=检测） │                │  （按钮禁用）  │
        └──────────────┘                └──────┬───────┘
                                               │
                                    Manager.start_slot() 或
                                    Manager.start_slot_depth()
                                               │
                              ┌─────────────────┼─────────────────┐
                              ↓                 ↓                 ↓
                         启动失败           启动成功           启动成功
                      （按钮恢复）      （Worker 运行中）   （Worker 运行中）
                                        按钮=停止          按钮=停止
                                        等待再次点击       等待再次点击

        参数：
            slot_id: 槽位编号（1/2/3）
        """
        idx = slot_id - 1  # 0-based 索引
        btn = self._slot_start_btns[idx]

        if self._manager and self._manager.is_running(slot_id):
            # ===== 槽位已运行 → 切换检测开关（开/关）=====
            # 已经在预览了，点击按钮只是切换 YOLO 推理的开启/关闭
            if self._manager._depth_mode[idx]:
                # 深度相机模式：操作 DepthWorker
                dw = self._manager._depth_workers[idx]
                if dw and dw._inference_enabled:
                    # 检测已开启 → 关闭
                    self._manager.disable_detection(slot_id)
                    self._log(f"槽位{slot_id}: 检测已关闭")
                else:
                    # 检测已关闭 → 开启（需要先检查模型是否加载）
                    if not self._manager.model_loaded:
                        self._log(f"槽位{slot_id}: 请先加载模型")
                        btn.setChecked(False)  # 取消选中状态
                        return
                    self._manager.enable_detection(slot_id)
                    self._log(f"槽位{slot_id}: 检测已开启")
            else:
                # 普通摄像头模式：操作普通 Worker
                worker = self._manager._workers[idx]
                if worker and worker._inference_enabled:
                    self._manager.disable_detection(slot_id)
                    self._log(f"槽位{slot_id}: 检测已关闭")
                else:
                    if not self._manager.model_loaded:
                        self._log(f"槽位{slot_id}: 请先加载模型")
                        btn.setChecked(False)
                        return
                    self._manager.enable_detection(slot_id)
                    self._log(f"槽位{slot_id}: 检测已开启")
        else:
            # ===== 槽位未运行 → 启动 =====
            # 第一次点击：启动摄像头/深度相机 + YOLO 检测

            # 检查是否为深度相机模式（仅 1 号槽位支持）
            is_depth = (idx == 0 and self._slot_config_panels[idx].is_depth_mode())

            if is_depth:
                # --- 深度相机模式 ---
                # 深度相机不需要选择摄像头（通过 ROS2 话题订阅，不走 OpenCV）
                if not self._manager or not self._manager.model_loaded:
                    self._log(f"槽位{slot_id}: 请先加载模型")
                    btn.setChecked(False)
                    return
                # 显示启动中状态
                btn.setText("启动中...")
                btn.setEnabled(False)  # 禁用按钮，防止重复点击
                self._log(f"槽位{slot_id}: 正在启动深度相机...")
                params = self._slot_config_panels[idx].get_params()
                # start_slot_depth() 内部会创建 DepthWorker，订阅 ROS2 话题
                ok = self._manager.start_slot_depth(slot_id, params)
                if not ok:
                    # 启动失败 → 恢复按钮状态
                    btn.setText("检测")
                    btn.setEnabled(True)
                    btn.setChecked(False)
            else:
                # --- 普通摄像头模式 ---
                # 需要先选择摄像头
                cam_idx = self._slot_config_panels[idx].get_camera_index()
                if cam_idx < 0:
                    self._log(f"槽位{slot_id}: 请先选择摄像头")
                    btn.setChecked(False)
                    return
                if not self._manager or not self._manager.model_loaded:
                    self._log(f"槽位{slot_id}: 请先加载模型")
                    btn.setChecked(False)
                    return
                btn.setText("启动中...")
                btn.setEnabled(False)
                self._log(f"槽位{slot_id}: 正在打开摄像头...")
                params = self._slot_config_panels[idx].get_params()
                # start_slot() 内部会创建普通 Worker，打开 OpenCV 摄像头
                ok = self._manager.start_slot(slot_id, cam_idx, params)
                if not ok:
                    btn.setText("检测")
                    btn.setEnabled(True)
                    btn.setChecked(False)

    def _on_toggle_qr(self, slot_id: int):
        """
        点击「扫码」按钮后执行 —— 开启/关闭二维码识别。

        二维码识别与 YOLO 检测是独立的，可以单独开关。
        二维码结果会与当前帧的检测框进行匹配（match_qr_to_detections）。

        参数：
            slot_id: 槽位编号（1/2/3）
        """
        idx = slot_id - 1
        btn = self._slot_qr_btns[idx]
        enabled = btn.isChecked()  # 获取按钮当前选中状态

        if self._manager and self._manager.is_running(slot_id):
            # 根据当前模式（深度/普通）操作对应的 Worker
            if self._manager._depth_mode[idx]:
                dw = self._manager._depth_workers[idx]
                if dw:
                    dw.set_qr_enabled(enabled)  # 通知 Worker 开启/关闭二维码识别
            else:
                worker = self._manager._workers[idx]
                if worker:
                    worker.set_qr_enabled(enabled)

    def _on_depth_mode_changed(self, slot_id: int, enabled: bool):
        """
        深度相机模式开关变化后执行。

        勾选「深度相机 (HP60C)」→ 启动深度相机模式：
        1. 停止普通摄像头预览（如果有）
        2. 启动 DepthWorker（订阅 ROS2 话题：RGB + 深度图像）
        3. 更新检测列表的列头（添加"深度"列）

        取消勾选 → 停止深度相机：
        1. 停止 DepthWorker
        2. 清空预览和检测列表
        3. 恢复检测列表的列头

        限制：只有 1 号槽位支持深度相机模式（因为 ROS2 节点只订阅了 1 号槽位的话题）。

        参数：
            slot_id: 槽位编号（目前只有 1 号槽位支持）
            enabled: True=开启深度模式, False=关闭深度模式
        """
        idx = slot_id - 1
        # 更新检测列表的列头显示（添加/移除"深度"列）
        self._slot_lists[idx].set_depth_mode(enabled)

        if enabled:
            # ===== 开启深度模式 =====
            if self._manager and self._manager.model_loaded:
                # 如果该槽位已在运行（普通模式），先停止
                if self._manager.is_running(slot_id):
                    self._manager.stop_slot(slot_id)
                # 获取参数并启动深度相机
                params = self._slot_config_panels[idx].get_params()
                self._log(f"槽位{slot_id}: 启动深度相机预览")
                # start_slot_depth() 会创建 DepthWorker，订阅 ROS2 RGB/深度话题
                self._manager.start_slot_depth(slot_id, params)
            self._log(f"槽位{slot_id}: 深度相机模式已开启")
        else:
            # ===== 关闭深度模式 =====
            if self._manager and self._manager.is_running(slot_id) and self._manager._depth_mode[idx]:
                # 先断开 Worker 信号（防止残留帧覆盖清空操作）
                dw = self._manager._depth_workers[idx]
                if dw:
                    try:
                        dw.frame_ready.disconnect()
                        dw.detections_ready.disconnect()
                    except TypeError:
                        pass
                # 停止 DepthWorker
                self._manager.stop_slot(slot_id)
                # 重置按钮状态
                self._slot_start_btns[idx].setChecked(False)
                self._slot_start_btns[idx].setText("检测")
                # 清空 UI
                self._slot_lists[idx].clear_all()
                self._slot_views[idx].clear()
            self._log(f"槽位{slot_id}: 深度相机模式已关闭")

    def _on_restart_camera(self, slot_id: int):
        """
        点击「重启相机」按钮：手动重启深度相机 ROS2 节点（拔插后用）。
        无自动重连；整个过程在 RestartCameraThread 后台执行，约 11 秒。
        """
        if slot_id != 1:
            return
        if getattr(self, "_restarting_camera", False):
            return  # 防重复点击
        self._restarting_camera = True

        panel = self._slot_config_panels[0]
        panel.set_restart_enabled(False)
        panel.set_restart_text("重启中…")
        self._log("深度相机: 正在重启 ROS2 节点（约 11 秒）…")

        # 记录重启前深度模式是否在跑，完成后自动恢复预览
        was_depth = bool(self._manager and self._manager._depth_mode[0]
                         and self._manager.is_running(1))
        if was_depth and self._manager:
            self._manager.stop_slot(1)

        self._restart_thread = RestartCameraThread(self)
        self._restart_thread.done.connect(
            lambda ok, msg, was=was_depth: self._on_restart_camera_done(ok, msg, was)
        )
        self._restart_thread.start()

    def _on_restart_camera_done(self, ok: bool, message: str, resume_depth: bool):
        """重启线程结束回调（主线程）：恢复按钮状态，按需恢复深度预览。"""
        self._restarting_camera = False
        panel = self._slot_config_panels[0]
        panel.set_restart_enabled(True)
        panel.set_restart_text("重启相机")

        if not ok:
            self._log(f"深度相机: 重启失败（{message}），详见 /tmp/hp60c_camera.log")
            return

        self._log(f"深度相机: {message}")
        # 若重启前深度模式开着，自动重新拉起 DepthWorker 预览
        if resume_depth and self._manager and self._manager.model_loaded:
            params = self._slot_config_panels[0].get_params()
            self._manager.start_slot_depth(1, params)
            self._log("深度相机: 预览已恢复")

    def _on_toggle_left_panel(self):
        """
        点击折叠按钮后执行 —— 隐藏/显示左侧参数面板。

        切换逻辑：
        - 面板当前可见 → 隐藏面板，按钮文字改为 ▶（表示可展开）
        - 面板当前隐藏 → 显示面板，按钮文字改为 ◀（表示可折叠）

        使用 isVisible() 检查当前状态，setVisible() 切换显示。
        """
        visible = self._left_panel.isVisible()
        self._left_panel.setVisible(not visible)  # 切换显示/隐藏
        self._left_toggle_btn.setText("▶" if visible else "◀")  # 更新按钮图标

    def _on_toggle_xy(self):
        """
        点击「XY」按钮后执行 —— 切换所有 CameraView 的坐标显示。

        坐标显示模式：鼠标悬停在预览画面上时，显示像素坐标 (x, y)。
        这在调试检测框位置时很有用。
        """
        show = self.btn_show_xy.isChecked()
        # 遍历所有槽位的 CameraView，统一设置坐标显示状态
        for view in self._slot_views:
            view.set_show_coords(show)

    def _on_param_changed(self, slot_id: int, param_name: str, value: float):
        """
        用户拖动参数滑块后实时执行。

        支持的参数：
        - conf: 置信度阈值（0-1），低于此值的检测结果会被过滤
        - iou: IoU 阈值（0-1），用于 NMS（非极大值抑制）去除重叠框
        - inference_interval: 推理间隔（帧数），每隔 N 帧执行一次 YOLO 推理
        - qr_interval: 二维码扫描间隔（帧数），每隔 N 帧执行一次二维码识别

        注意：参数变化会立即应用到正在运行的 Worker（实时生效），
        同时保存到配置文件（下次启动自动加载）。

        参数：
            slot_id: 槽位编号（1/2/3）
            param_name: 参数名称（如 "conf"、"iou"）
            value: 参数值（浮点数）
        """
        if not self._manager:
            return

        # 获取该槽位的 Worker（如果正在运行）
        # 深度模式下普通 _workers[i] 为空，实际跑的是 _depth_workers[i]
        worker = self._manager._workers[slot_id - 1] or \
            self._manager._depth_workers[slot_id - 1]
        if not worker:
            return  # Worker 未启动，参数变化无意义

        # 根据参数名称调用 Worker 的对应方法
        if param_name == "conf":
            worker.set_params(conf=value)  # 设置置信度阈值
        elif param_name == "iou":
            worker.set_params(iou=value)   # 设置 IoU 阈值
        elif param_name == "inference_interval":
            worker.set_inference_interval(int(value))  # 设置推理间隔
        elif param_name == "qr_interval":
            worker.set_qr_interval(int(value))  # 设置二维码扫描间隔

        # 保存到配置文件（JSON），下次启动自动加载
        self.config.set(f"slot_{slot_id}_{param_name}", value)
        # 同步进 slot_N 嵌套字典（_load_config 读的是这个）
        nested = self.config.get(f"slot_{slot_id}", {})
        if isinstance(nested, dict):
            nested[param_name] = value
            self.config.set(f"slot_{slot_id}", nested)
        self.config.save()

    def _log(self, msg: str):
        """
        写运行日志到右上角的日志面板。

        使用 QTextEdit.append() 添加新行（自动换行）。
        添加后立即滚动到底部，确保最新日志可见。
        日志面板固定高度 28px，只显示最新一行，但可以滚动查看历史。

        参数：
            msg: 日志消息文本
        """
        if self._log_widget:
            self._log_widget.append(msg)  # 添加新行
            # 滚动到底部（确保最新消息可见）
            sb = self._log_widget.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _com_log(self, msg: str):
        """
        写通讯日志（MQTT 收发消息）到通讯日志面板。

        与 _log() 类似，但专门用于显示 MQTT 通信内容，
        方便调试 MQTT 问题（如指令丢失、格式错误等）。

        参数：
            msg: 通讯日志消息（通常以 ← 或 → 开头表示方向）
        """
        if self._com_log_widget:
            self._com_log_widget.append(msg)
            sb = self._com_log_widget.verticalScrollBar()
            sb.setValue(sb.maximum())

    # ============ Worker 信号连接（通过 slot_started 信号自动触发） ============
    # 当 ProductionManager 成功启动一个槽位时，会发射 slot_started 信号，
    # 页面的 _on_slot_started 槽函数被调用，负责将 Worker 的信号连接到 UI 组件。

    def _on_detections(self, slot_id: int, detections: list):
        """
        处理 Worker 发送的检测结果。

        这是检测流程的核心回调，每次 YOLO 推理完成后触发。

        流程：
        1. 更新 DetectionList 组件（显示检测到的物体列表）
        2. 更新 CameraView 组件（在预览画面上绘制检测框）
        3. 通过 Manager 发送检测结果到 MQTT（主控传送带会根据结果做分拣）

        参数：
            slot_id: 槽位编号（1/2/3）
            detections: 检测结果列表，每个元素是一个字典，包含：
                - class_id: 类别 ID
                - class_name: 类别名称（如 "apple"、"box"）
                - confidence: 置信度（0-1）
                - bbox: 边界框 [x1, y1, x2, y2]
                - depth: 深度值（毫米，仅深度模式）
        """
        idx = slot_id - 1
        # 获取该槽位的二维码绑定关系（二维码与检测框的匹配结果）
        qr_bindings = self._slot_qr_bindings[idx]

        # 1. 更新检测列表（显示物体名称、置信度、深度值、二维码）
        #    update_detections() 会根据 qr_bindings 在对应行显示二维码信息
        self._slot_lists[idx].update_detections(detections, qr_bindings)

        # 2. 更新预览画面上的检测框（在 CameraView 上绘制矩形框 + 类别标签）
        self._slot_views[idx].update_detections(detections)
        # 注意：不在这里清空二维码图层——_on_detections 可能先于 _on_qr_results 触发，
        # 清空会导致扫码框闪一下就没了。二维码图层由 _on_qr_results 单独更新。

        # 3. 通过 Manager 发送检测结果到 MQTT
        #    Manager 会将结果格式化为主控能理解的协议格式
        if self._manager:
            self._manager.send_detection_result(slot_id, detections, qr_bindings)

    def _on_qr_results(self, slot_id: int, qr_results: list):
        """
        处理 Worker 发送的二维码扫描结果。

        二维码结果需要与当前帧的检测框进行匹配（match_qr_to_detections），
        确定每个二维码属于哪个检测到的物体。

        匹配逻辑：二维码的中心点落在哪个检测框内，就绑定到哪个物体。

        参数：
            slot_id: 槽位编号（1/2/3）
            qr_results: 二维码扫描结果列表，每个元素包含：
                - data: 二维码内容（字符串）
                - bbox: 二维码边界框 [x1, y1, x2, y2]
        """
        idx = slot_id - 1

        # 获取该槽位最近一次的检测结果（用于匹配）
        # 注意：检测结果和二维码结果可能不同步（不同帧），
        # 但 match_qr_to_detections 会尽量匹配当前帧的结果
        detections = []
        if self._manager and self._manager._depth_mode[idx]:
            # 深度模式：从 DepthWorker 获取最近的检测结果
            dw = self._manager._depth_workers[idx]
            if dw and hasattr(dw, '_last_detections'):
                detections = dw._last_detections
        else:
            # 普通模式：从普通 Worker 获取最近的检测结果
            worker = self._manager._workers[idx] if self._manager else None
            if worker and hasattr(worker, '_last_detections'):
                detections = worker._last_detections

        # 执行匹配：将二维码绑定到检测框
        # 返回值是一个字典：{检测框索引: 二维码数据}
        self._slot_qr_bindings[idx] = match_qr_to_detections(qr_results, detections)

        # 在预览画面上绘制二维码框和内容（无物体时也能看到扫码结果，方便调试）
        self._slot_views[idx].update_qr_results(qr_results)

        # 立即刷新检测列表，让二维码内容马上显示（否则要等下一帧 _on_detections）
        if detections:
            self._slot_lists[idx].update_detections(detections, self._slot_qr_bindings[idx])

    def _on_worker_error(self, slot_id: int, error: str):
        """
        Worker 发生错误时的回调。

        错误场景：摄像头断开、YOLO 推理异常、ROS2 话题接收失败等。
        目前只是打印到控制台，后续可以考虑显示到 UI。

        参数：
            slot_id: 槽位编号（1/2/3）
            error: 错误信息字符串
        """
        print(f"[产线] 槽位{slot_id} 错误: {error}")

    def _on_slot_started(self, slot_id: int, worker):
        """
        槽位启动成功后的回调（由 Manager 的 slot_started 信号触发）。

        这是 Worker 信号连接的关键方法：
        Manager 启动 Worker 后，发射 slot_started 信号，
        页面在这里将 Worker 的各种信号连接到 UI 组件。

        信号连接图：
        Worker.frame_ready      → CameraView.update_frame()     显示视频帧
        Worker.fps_updated      → CameraView.update_fps()       更新 FPS 显示
        Worker.detections_ready → _on_detections()               处理检测结果
        Worker.qr_results_ready → _on_qr_results()              处理二维码结果
        Worker.error_occurred   → _on_worker_error()             处理错误

        参数：
            slot_id: 槽位编号（1/2/3）
            worker: 启动的 Worker 实例（DepthWorker 或普通 Worker）
        """
        idx = slot_id - 1
        btn = self._slot_start_btns[idx]

        # 更新按钮状态：从"启动中..."变为"检测"（可点击状态）
        btn.setText("检测")
        btn.setEnabled(True)
        btn.setChecked(False)  # 未选中 = 检测未开启（只是预览）

        self._log(f"槽位{slot_id}: 预览已启动")

        # 获取该槽位的 CameraView 组件
        view = self._slot_views[idx]

        # ============ 连接 Worker 信号到 UI 组件 ============
        # 注意：这些连接在槽位停止时需要断开（否则旧 Worker 的信号会干扰新 Worker）
        # 断开操作在 _on_camera_selected 和 on_hide 中完成

        # 帧信号：每采集一帧就发送到 CameraView 显示
        worker.frame_ready.connect(view.update_frame)

        # FPS 信号：每秒更新一次 FPS 数值
        worker.fps_updated.connect(view.update_fps)

        # 检测结果信号：YOLO 推理完成后发送检测结果
        # lambda 中的 s=slot_id 是为了在闭包中正确捕获 slot_id 值
        worker.detections_ready.connect(
            lambda dets, s=slot_id: self._on_detections(s, dets))

        # 二维码结果信号：二维码识别完成后发送结果
        worker.qr_results_ready.connect(
            lambda qrs, s=slot_id: self._on_qr_results(s, qrs))

        # 错误信号：Worker 发生错误时通知页面
        worker.error_occurred.connect(
            lambda err, s=slot_id: self._on_worker_error(s, err))

    def _on_mqtt_cmd_received(self, payload: str):
        """
        收到 MQTT 指令后的回调。

        当主控传送带通过 MQTT 发送指令时（如 "xj1 1 1"），
        Manager 会解析指令并发射 mqtt_cmd_received 信号，
        页面在这里将指令内容记录到通讯日志。

        指令格式：<槽位> <检测> <扫码>
        示例：xj1 1 1 = 槽位1 检测开+扫码开

        参数：
            payload: MQTT 消息内容（字符串）
        """
        self._com_log(f"← {payload}")  # ← 表示收到的消息

    def _on_detection_toggled(self, slot_id: int, enabled: bool):
        """
        检测状态变化后的回调（MQTT 指令或按钮触发都走这里）。

        当主控通过 MQTT 发送检测开关指令时（如 "xj1 1"），
        Manager 会启用/禁用对应槽位的检测，
        然后发射 detection_toggled 信号，页面在这里更新按钮状态。

        注意：MQTT 指令和按钮点击都会触发这个方法，
        所以按钮状态会与实际检测状态保持同步。

        参数：
            slot_id: 槽位编号（1/2/3）
            enabled: True=检测已开启, False=检测已关闭
        """
        idx = slot_id - 1
        btn = self._slot_start_btns[idx]
        if enabled:
            btn.setChecked(True)   # 选中状态 = 检测已开启
            btn.setText("停止")     # 按钮文字变为"停止"
        else:
            btn.setChecked(False)  # 未选中状态 = 检测已关闭
            btn.setText("检测")     # 按钮文字恢复为"检测"
            # 注意：不清理检测框和列表，保留最后一帧的结果供用户查看

    def _on_qr_toggled(self, slot_id: int, enabled: bool):
        """
        扫码状态变化后的回调。

        与 _on_detection_toggled 类似，更新扫码按钮的选中状态。

        参数：
            slot_id: 槽位编号（1/2/3）
            enabled: True=扫码已开启, False=扫码已关闭
        """
        idx = slot_id - 1
        btn = self._slot_qr_btns[idx]
        btn.setChecked(enabled)  # 同步按钮选中状态

    def _poll_mqtt_status(self):
        """
        定时器触发的 MQTT 状态轮询（每秒执行一次）。

        为什么需要轮询而不是信号？
        因为 MQTT 连接状态可能在页面外部变化（如传送带页面连接了 MQTT），
        产线页面需要主动检查共享连接的状态。

        流程：
        1. 获取全局共享的 MqttClient 实例
        2. 检查连接状态
        3. 更新 UI 指示灯（绿色=已连接，红色=未连接）
        4. 如果已连接，同步 Manager 的 MQTT 引用并设置指令回调

        注意：即使连接成功，也只在连接状态变化时才更新回调，
        避免每次轮询都重复设置。
        """
        # 获取全局共享的 MQTT 实例（所有页面共用同一个）
        shared = get_shared_mqtt()
        connected = shared.is_connected()

        # 更新 UI 状态指示灯
        if connected:
            # 已连接 → 绿色
            self._mqtt_status_dot.setStyleSheet("color: #07C160; font-size: 14px;")
            self._mqtt_status_label.setText("MQTT 已连接")
            self._mqtt_status_label.setStyleSheet("font-size: 12px; color: #07C160;")
        else:
            # 未连接 → 红色
            self._mqtt_status_dot.setStyleSheet("color: #e74c3c; font-size: 14px;")
            self._mqtt_status_label.setText("MQTT 未连接")
            self._mqtt_status_label.setStyleSheet("font-size: 12px; color: #e74c3c;")

        # 同步 Manager 的 MQTT 引用
        # 只在 Manager 的 MQTT 引用不是当前共享实例时才更新（避免重复赋值）
        if connected and self._manager:
            if self._manager._mqtt is not shared:
                self._manager._mqtt = shared  # 绑定 MQTT 实例到 Manager
                # 设置产线指令回调：当收到 MQTT 指令时，Manager 会调用 _parse_mqtt_cmd
                shared.set_prod_cmd_callback(self._manager._parse_mqtt_cmd)
        elif connected:
            if self._manager:
                shared.set_prod_cmd_callback(self._manager._parse_mqtt_cmd)

    # ============ MQTT 连接 ============
    def connect_mqtt(self, broker: str = "127.0.0.1", port: int = 1883):
        """
        连接 MQTT Broker（使用全局共享连接）。

        全局共享连接机制：
        - 整个应用只有一个 MqttClient 实例（通过 get_shared_mqtt() 获取）
        - 传送带页面和产线页面共用这个实例
        - 连接状态通过 is_connected() 检查
        - 断开连接通过 disconnect_shared() 执行

        流程：
        1. 获取共享实例
        2. 如果已连接，直接设置回调并返回
        3. 如果未连接，调用 connect_shared() 连接
        4. 连接成功后，设置 Manager 的 MQTT 引用和指令回调
        5. 如果有保存的模型路径，自动加载模型

        参数：
            broker: MQTT Broker 地址（默认 127.0.0.1，本地）
            port: MQTT Broker 端口（默认 1883）

        返回：
            bool: 连接是否成功
        """
        self._mqtt = get_shared_mqtt()

        # 如果已经连接，直接设置回调并返回
        if self._mqtt.is_connected():
            self._set_mqtt_status(True)
            self._manager._mqtt = self._mqtt
            # 设置产线指令回调：当收到 MQTT 指令时，Manager 会调用 _parse_mqtt_cmd
            self._mqtt.set_prod_cmd_callback(self._manager._parse_mqtt_cmd)
            return True

        # 尝试连接（connect_shared 内部管理连接状态）
        ok = connect_shared(broker, port)
        if ok:
            # 连接成功
            self._mqtt = get_shared_mqtt()
            self._set_mqtt_status(True)
            self._manager._mqtt = self._mqtt
            self._mqtt.set_prod_cmd_callback(self._manager._parse_mqtt_cmd)

            # 如果有保存的模型路径且模型未加载，自动加载
            # 失败时也要更新标签，不能停留在错误的"已加载"状态
            model_path = self.config.get("model_path", "")
            if model_path and not self._manager.model_loaded:
                if not os.path.exists(model_path):
                    self._log(f"模型文件不存在: {model_path}")
                    self.lbl_model_path.setText(f"文件不存在: {os.path.basename(model_path)}")
                    self.lbl_model_path.setStyleSheet("color: #e74c3c; font-size: 11px;")
                else:
                    try:
                        ok = self._manager.load_model(model_path)
                    except Exception as e:
                        ok = False
                        self._log(f"模型加载异常: {e}")
                    if ok:
                        self.lbl_model_path.setText(f"已加载: {os.path.basename(model_path)}")
                        self.lbl_model_path.setStyleSheet("color: #07C160; font-size: 11px;")
                    else:
                        self._log(f"模型加载失败: {model_path}")
                        self.lbl_model_path.setText(f"加载失败: {os.path.basename(model_path)}")
                        self.lbl_model_path.setStyleSheet("color: #e74c3c; font-size: 11px;")
        else:
            # 连接失败
            self._set_mqtt_status(False)
        return ok

    def disconnect_mqtt(self):
        """
        断开 MQTT 连接。

        注意：disconnect() 只是断开当前连接，不会影响全局共享实例。
        全局实例会保持存在，其他页面仍可使用。
        """
        if self._mqtt:
            self._mqtt.disconnect()
            self._mqtt = None
        self._set_mqtt_status(False)

    def _set_mqtt_status(self, connected: bool):
        """
        更新 MQTT 状态指示灯的显示。

        参数：
            connected: True=已连接（绿色）, False=未连接（红色）
        """
        if connected:
            self._mqtt_status_dot.setStyleSheet("color: #07C160; font-size: 14px;")
            self._mqtt_status_label.setText("MQTT 已连接")
            self._mqtt_status_label.setStyleSheet("font-size: 12px; color: #07C160;")
        else:
            self._mqtt_status_dot.setStyleSheet("color: #e74c3c; font-size: 14px;")
            self._mqtt_status_label.setText("MQTT 未连接")
            self._mqtt_status_label.setStyleSheet("font-size: 12px; color: #e74c3c;")

    # ============ 摄像头检测 ============
    def _detect_cameras(self):
        """
        获取共享的摄像头列表。

        返回值格式：[(index, name), ...]
        例如：[(0, "USB Camera 1"), (2, "USB Camera 2")]

        注意：返回的是元组列表，不是字典列表。
        index 是 OpenCV 的摄像头索引（用于 VideoCapture），
        name 是设备名称（用于显示在下拉列表中）。
        """
        cams = get_shared_cameras()  # 获取共享缓存
        if not cams:
            return [(0, "默认摄像头")]  # 无摄像头时返回默认项
        # 将字典列表转换为元组列表
        return [(d["index"], d["name"]) for d in cams]

    def _on_refresh_cameras(self):
        """
        点击「刷新摄像头列表」按钮后执行。

        流程：
        1. 清除共享摄像头缓存（强制重新检测）
        2. 重新检测系统摄像头
        3. 更新所有槽位配置面板的摄像头下拉列表

        使用场景：热插拔了新的 USB 摄像头后，需要刷新列表才能看到新设备。
        """
        reset_shared_cameras()  # 清除缓存
        cameras = self._detect_cameras()  # 重新检测
        # 更新所有槽位配置面板的摄像头下拉列表
        for panel in self._slot_config_panels:
            panel.set_cameras(cameras)

    # ============ 页面生命周期 ============
    def on_show(self):
        """
        页面显示时执行（由 AppShell 的 QStackedWidget 在切换页面时调用）。

        这是页面的「初始化」入口，每次切换到产线页面都会执行。

        执行顺序：
        1. 同步共享 MQTT 连接状态（可能在传送带页面已连接）
        2. 检测系统摄像头（共享缓存，避免重复检测）
        3. 加载已保存的配置（模型路径、各槽位参数）
        4. 自动加载已保存的模型到 Manager
        5. 自动打开已保存的摄像头并预览（不启动检测）

        注意：第 5 步只启动预览，不启动 YOLO 检测。
        检测需要用户手动点击「检测」按钮才会启动。
        """
        # 1. 同步共享 MQTT 连接状态
        # 全局 MQTT 实例可能在传送带页面已连接，这里直接复用
        shared = get_shared_mqtt()
        if shared.is_connected():
            self._mqtt = shared
            self._set_mqtt_status(True)
            if self._manager:
                self._manager._mqtt = shared
                # 设置产线指令回调
                shared.set_prod_cmd_callback(self._manager._parse_mqtt_cmd)

        # 2. 检测摄像头（使用共享缓存，首次检测后缓存结果）
        cameras = self._detect_cameras()
        for panel in self._slot_config_panels:
            panel.set_cameras(cameras)

        # 3. 加载已保存的配置（模型路径、各槽位参数等）
        self._load_config()

        # 4. 自动加载已保存的模型
        model_path = self.config.get("model_path", "")
        if model_path and not self._manager.model_loaded:
            if not os.path.exists(model_path):
                # 文件不存在（如外置盘未挂载）→ 红色提示，不再假装已加载
                self._log(f"模型文件不存在: {model_path}")
                self.lbl_model_path.setText(f"文件不存在: {os.path.basename(model_path)}")
                self.lbl_model_path.setStyleSheet("color: #e74c3c; font-size: 11px;")
            else:
                try:
                    ok = self._manager.load_model(model_path)
                except Exception as e:
                    ok = False
                    self._log(f"模型加载异常: {e}")
                if ok:
                    self.lbl_model_path.setText(f"已加载: {os.path.basename(model_path)}")
                    self.lbl_model_path.setStyleSheet("color: #07C160; font-size: 11px;")
                else:
                    self._log(f"模型加载失败: {model_path}")
                    self.lbl_model_path.setText(f"加载失败: {os.path.basename(model_path)}")
                    self.lbl_model_path.setStyleSheet("color: #e74c3c; font-size: 11px;")

        # 5. 自动打开已保存的摄像头并预览（不启动检测）
        # 遍历 3 个槽位，如果配置了摄像头就自动打开
        for i in range(3):
            slot_id = i + 1

            # 跳过已在运行的槽位（避免重复启动）
            if self._manager and self._manager.is_running(slot_id):
                continue

            # 深度模式（仅 1 号槽位支持）
            is_depth = (i == 0 and self._slot_config_panels[i].is_depth_mode())
            if is_depth:
                params = self._slot_config_panels[i].get_params()
                self._log(f"槽位{slot_id}: 自动启动深度相机")
                self._manager.start_slot_depth(slot_id, params)
                continue

            # 普通模式：检查是否配置了摄像头
            cam_idx = self._slot_config_panels[i].get_camera_index()
            if cam_idx < 0:
                continue  # 未配置摄像头，跳过

            params = self._slot_config_panels[i].get_params()
            self._log(f"槽位{slot_id}: 自动打开摄像头 {cam_idx}")
            self._manager.start_slot(slot_id, cam_idx, params)

    def on_hide(self):
        """
        页面隐藏时执行（由 AppShell 在切换到其他页面时调用）。

        这是页面的「清理」入口，每次离开产线页面都会执行。

        执行内容：
        1. 遍历 3 个槽位，停止所有正在运行的 Worker
        2. 断开 Worker 的信号连接（防止残留帧覆盖清空操作）
        3. 重置按钮状态（检测按钮恢复为"检测"）
        4. 清空预览画面和检测列表

        为什么需要断开信号？
        Worker 停止后可能还有残留帧在信号队列中，
        如果不断开信号，这些帧会继续发送到已清空的 CameraView，
        导致画面显示异常。
        """
        for slot_id in range(1, 4):  # 遍历槽位 1-3
            if self._manager and self._manager.is_running(slot_id):
                idx = slot_id - 1

                # 先断开 Worker 信号（防止残留帧干扰）
                worker = self._manager._workers[idx] or self._manager._depth_workers[idx]
                if worker:
                    try:
                        worker.frame_ready.disconnect()
                        worker.detections_ready.disconnect()
                    except TypeError:
                        pass  # 信号未连接，忽略

                # 停止 Worker（关闭摄像头/深度相机）
                self._manager.stop_slot(slot_id)

                # 重置按钮状态
                btn = self._slot_start_btns[idx]
                btn.setChecked(False)   # 取消选中
                btn.setText("检测")      # 恢复文字
                btn.setEnabled(True)    # 启用按钮

                # 清空 UI
                self._slot_lists[idx].clear_all()  # 清空检测列表
                self._slot_views[idx].clear()       # 清空预览画面

    def save_state(self):
        """
        保存所有槽位参数（由 AppShell 在关闭前调用）。

        这是外部调用的入口，内部调用 _save_config()。
        AppShell 会在应用退出前遍历所有页面调用 save_state()，
        确保用户修改的参数被持久化保存。
        """
        self._save_config()

    # ============ 配置存储 ============
    def _load_config(self):
        """
        加载产线配置（JSON 文件）。

        加载内容：
        1. 模型路径（model_path）→ 更新 UI 显示
        2. 各槽位参数（slot_1, slot_2, slot_3）→ 传递给 SlotConfigPanel

        配置文件格式示例：
        {
            "model_path": "/path/to/model.pt",
            "slot_1": {"camera_index": 0, "conf": 0.5, "iou": 0.45, ...},
            "slot_2": {"camera_index": 2, "conf": 0.5, ...},
            "slot_3": {"camera_index": -1, ...}
        }
        """
        # 加载模型路径
        # 注意：不能只看配置里有没有路径就显示"已加载"，
        # 必须以 manager.model_loaded 的真实状态为准，否则会出现
        # 标签显示"已加载"但实际未加载的矛盾
        model_path = self.config.get("model_path", "")
        if model_path:
            if self._manager and self._manager.model_loaded:
                # 模型确实已加载 → 绿色"已加载"
                self.lbl_model_path.setText(f"已加载: {os.path.basename(model_path)}")
                self.lbl_model_path.setStyleSheet("color: #07C160; font-size: 11px;")
            else:
                # 有保存路径但还没加载 → 灰色提示（on_show 会尝试自动加载）
                self.lbl_model_path.setText(f"未加载: {os.path.basename(model_path)}（自动加载中...）")
                self.lbl_model_path.setStyleSheet("color: #999; font-size: 11px;")

        # 加载各槽位参数
        for i in range(3):
            slot_id = i + 1
            params = self.config.get(f"slot_{slot_id}", {})
            if params:
                # set_params() 会更新 SlotConfigPanel 内部的所有控件值
                self._slot_config_panels[i].set_params(params)

    def _save_config(self):
        """
        保存产线配置到 JSON 文件。

        遍历 3 个槽位，从每个 SlotConfigPanel 获取当前参数，
        保存到配置文件。

        注意：每次保存都会覆盖整个配置文件，
        所以不需要担心增量更新的问题。
        """
        for i in range(3):
            slot_id = i + 1
            # get_params() 返回当前面板的所有参数字典
            params = self._slot_config_panels[i].get_params()
            self.config.set(f"slot_{slot_id}", params)
        self.config.save()  # 写入 JSON 文件

    # ============ 关闭清理 ============
    def cleanup(self):
        """
        清理所有资源（由 AppShell 在关闭前调用）。

        执行内容：
        1. 调用 Manager 的 cleanup()（停止所有 Worker、释放模型）
        2. 断开 MQTT 连接

        这是最后的清理步骤，确保没有残留的线程或连接。
        """
        if self._manager:
            self._manager.cleanup()  # 停止所有 Worker、释放模型
        self.disconnect_mqtt()       # 断开 MQTT 连接
