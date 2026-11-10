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
    QPushButton, QLabel, QFileDialog, QSizePolicy, QFrame, QTextEdit,
    QDialog, QScrollArea, QComboBox, QButtonGroup, QStackedWidget
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
from vision.qr_decoder import match_qr_to_detections, set_scan_mode, get_scan_mode
# get_production_config: 获取产线配置的全局单例（JSON 存储）
from core.config import get_production_config
# MqttClient: MQTT 客户端类，封装了与主控传送带的通信协议
from core.mqtt_client import MqttClient
# get_shared_mqtt: 获取全局唯一的 MqttClient 实例（所有页面共用）
# connect_shared: 通过全局实例连接 MQTT broker
from core.mqtt_shared import get_shared_mqtt, connect_shared
# 图标库（缺包不炸：_HAS_QTA=False 时按钮退化为无图标）
import ui.theme as theme

try:
    import qtawesome as qta
    _HAS_QTA = True
except ImportError:
    qta = None
    _HAS_QTA = False

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
            # .bin → BPU 模型验证（RDK 板上）；.pt → ultralytics YOLO
            if self._path.endswith(".bin"):
                from vision.bpu_model import BpuModel
                BpuModel(self._path)  # 真实加载一次做验证
                self.loaded.emit(True, "BPU 模型验证通过")
                return
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
    _BTN_STYLE = None  # 已收编：见 theme.toggle_btn()（__init__ 时解析）

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

        from ui import theme
        ProductionPage._BTN_STYLE = theme.toggle_btn(padding="4px 10px", font=12)  # 紧凑版适配小屏

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
        self._manager.slot_activated.connect(self._on_slot_activated)   # MQTT 指令 → 激活/预览切槽
        self._manager.detection_toggled.connect(self._on_detection_toggled)  # 检测开关变化
        self._manager.qr_toggled.connect(self._on_qr_toggled)          # 扫码开关变化

        # 模型加载线程引用（加载完成后自动销毁，这里只是保留引用防止被 GC 回收）
        self._model_load_thread: Optional[ModelLoadThread] = None

        # ============ 每个槽位的 UI 组件引用列表 ============
        # 3 个槽位对应 3 个元素，索引 0-2 对应 槽位1-3
        self._cameras_cache: list = []                    # 摄像头枚举缓存（预览下拉取名）
        self._views = None                                # 兼容占位（后续若需要统一接口可用）
        self._slot_views: list[CameraView] = []           # 视频预览组件
        self._slot_lists: list[DetectionList] = []        # 检测结果列表
        self._slot_start_btns: list[QPushButton] = []     # 检测启停按钮
        self._slot_qr_btns: list[QPushButton] = []        # 扫码启停按钮
        self._slot_status_labels: list[QLabel] = []       # 状态标签（预留，未使用）
        self._slot_config_panels: list[SlotConfigPanel] = []  # 参数配置面板
        self._slot_qr_bindings: list[dict] = [{} for _ in range(3)]  # 二维码与检测框的绑定关系

        # 日志面板（运行日志 + 通讯日志，在 _setup_ui 中创建）
        self._log_widget: QTextEdit = None

        # 设置弹窗（模型/摄像头/参数配置，点工具栏「⚙ 设置」打开）
        self._settings_dialog: QDialog = None

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
        from ui import theme
        self.setStyleSheet(theme.page_qss())  # 全局浅色 QSS（tokens 统一管理）
        # 主布局：水平排列（右侧：工具栏 + 三列预览）
        # 参数配置全部收进弹窗（工具栏「⚙ 设置」按钮打开）
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)  # 外边距：上下左右各 6px
        main_layout.setSpacing(6)  # 控件间距：6px

        # ============ 参数配置面板（塞进弹窗，不占主界面） ============
        # --- 左侧内容面板 ---
        # 只在「设置」弹窗里显示；宽度随弹窗拉伸（min 280 保证滑块可读）
        self._left_panel = QWidget()
        self._left_panel.setMinimumWidth(280)
        self._left_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        left_layout = QVBoxLayout(self._left_panel)  # 垂直排列各配置项
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)  # 控件间距 8px

        # --- 模型选择分组框 ---
        # QGroupBox 自带标题和边框，将模型选择相关控件组合在一起
        self.model_group = QGroupBox("模型设置")
        model_layout = QVBoxLayout(self.model_group)  # 内部垂直布局

        # 模型选择按钮行
        model_row = QHBoxLayout()
        self.btn_model = QPushButton("选择模型 (.pt/.bin)")  # 点击后弹出文件选择对话框
        self.btn_model.clicked.connect(self._on_select_model)  # 连接点击信号
        model_row.addWidget(self.btn_model, stretch=1)  # stretch=1 让按钮占满整行
        model_layout.addLayout(model_row)

        # 模型路径显示标签（初始显示"未加载模型"，灰色文字）
        self.lbl_model_path = QLabel("未加载模型")
        self.lbl_model_path.setStyleSheet("color: #999; font-size: 11px;")
        self.lbl_model_path.setWordWrap(True)  # 长路径自动换行
        model_layout.addWidget(self.lbl_model_path)

        left_layout.addWidget(self.model_group)  # （工具栏模型图标钮替代了它，这里保留隐藏）

        # --- 刷新摄像头列表按钮 ---
        # 热插拔摄像头后点击此按钮重新枚举
        self.btn_refresh_cam = QPushButton("刷新摄像头列表")
        self.btn_refresh_cam.clicked.connect(self._on_refresh_cameras)
        left_layout.addWidget(self.btn_refresh_cam)

        # --- 三个槽位的参数配置面板 ---
        # 循环创建 3 个 SlotConfigPanel，每个面板管理一个槽位的参数
        # SlotConfigPanel 内部包含：摄像头下拉列表、置信度滑块、IoU 滱块、深度模式开关等
        for i in range(3):
            # YOLO 参数（置信度/IoU/推理间隔）只给槽位1（唯一跑 YOLO 的槽位）；
            # 槽位2/3 只扫码，面板仅剩相机选择+扫码间隔
            panel = SlotConfigPanel(slot_id=i + 1, yolo_params=(i == 0))  # slot_id 从 1 开始
            # 连接面板的信号到页面的槽函数：
            panel.camera_changed.connect(self._on_camera_selected)      # 摄像头选择变化
            panel.param_changed.connect(self._on_param_changed)        # 参数滑块变化
            panel.depth_mode_changed.connect(self._on_depth_mode_changed)  # 深度模式开关变化
            panel.restart_camera_clicked.connect(self._on_restart_camera)   # 手动重启相机节点
            self._slot_config_panels.append(panel)  # 保存引用，后续需要访问
            left_layout.addWidget(panel)

        # addStretch() 在底部添加弹性空间，让上面的控件靠上排列
        left_layout.addStretch()
        # 左面板内容整体套 QScrollArea：弹窗矮时内容可滚动而不是被裁掉
        self._settings_scroll = QScrollArea()
        self._settings_scroll.setWidgetResizable(True)
        self._settings_scroll.setWidget(self._left_panel)
        self._settings_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

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

        # --- MQTT 状态指示灯（只有灯；文字 tooltip 悬停显示） ---
        # ● 圆点符号，绿色=已连接，红色=未连接，灰=未知
        # 通过 CSS 动态切换颜色
        self._mqtt_status_dot = QLabel("●")
        self._mqtt_status_dot.setFixedSize(16, 16)  # 固定 16x16 像素
        self._mqtt_status_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mqtt_status_dot.setStyleSheet("color: #ccc; font-size: 14px;")  # 初始灰色（未知状态）
        self._mqtt_status_dot.setToolTip("MQTT 未连接")
        toolbar_layout.addWidget(self._mqtt_status_dot)

        # --- 分隔线 ---
        # QFrame.Shape.VLine 创建一条垂直分隔线，视觉上分隔不同功能区
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #ddd;")
        sep.setFixedHeight(18)  # 分隔线与按钮高度匹配
        toolbar_layout.addWidget(sep)

        # --- 功能钮（同一格，按预览源二选一显示） ---
        # 深度相机只检测（YOLO 调试用）、RGB 只扫码（独立开关）——严格分开
        # 图标风格对齐相机识别页（qtawesome fa5s）
        self.btn_func_detect = QPushButton()
        self.btn_func_detect.setCheckable(True)
        self.btn_func_detect.setFixedSize(48, 48)
        self.btn_func_detect.setStyleSheet(theme.toggle_btn())
        self.btn_func_detect.clicked.connect(self._on_tool_detect_clicked)
        self.btn_func_detect.setToolTip("检测 (YOLO)")
        toolbar_layout.addWidget(self.btn_func_detect)

        self.btn_func_scan = QPushButton()
        self.btn_func_scan.setCheckable(True)
        self.btn_func_scan.setFixedSize(48, 48)
        self.btn_func_scan.setStyleSheet(theme.toggle_btn())
        self.btn_func_scan.clicked.connect(self._on_tool_scan_clicked)
        self.btn_func_scan.setToolTip("扫码（独立开关，不走检测）")
        self.btn_func_scan.hide()  # 初始隐藏，_refresh_func_button 按预览源切换
        toolbar_layout.addWidget(self.btn_func_scan)

        # --- 分隔线 ---
        sep_fa = QFrame()
        sep_fa.setFrameShape(QFrame.Shape.VLine)
        sep_fa.setStyleSheet("color: #ddd;")
        sep_fa.setFixedHeight(18)
        toolbar_layout.addWidget(sep_fa)

        # --- 预览相机下拉框 ---
        # 选项 = 摄像头枚举名（槽位面板同款名单）+ "深度相机 (HP60C)"
        # 选项背后挂 (槽位, 相机索引)：没被任何槽绑定的相机 → 绑到当前激活槽（兼做标定）
        # 同名相机自动带槽位标注（xjN）区分
        toolbar_layout.addWidget(QLabel("预览"))
        self.combo_preview = QComboBox()
        self.combo_preview.setFixedHeight(32)
        self.combo_preview.setMinimumWidth(150)
        self.combo_preview.setToolTip("选择要预览的相机：绑定中的槽位直接激活；未绑定的绑到当前槽")
        self.combo_preview.activated.connect(self._on_preview_cam_selected)
        toolbar_layout.addWidget(self.combo_preview)

        # --- 分隔线 ---
        sep_mb = QFrame()
        sep_mb.setFrameShape(QFrame.Shape.VLine)
        sep_mb.setStyleSheet("color: #ddd;")
        sep_mb.setFixedHeight(18)
        toolbar_layout.addWidget(sep_mb)

        # --- 模型选择（从左面板上移到工具栏，图标钮） ---
        self.btn_model = QPushButton()
        self.btn_model.setFixedSize(48, 48)
        self.btn_model.setStyleSheet(theme.toggle_btn())
        self.btn_model.clicked.connect(self._on_select_model)
        self.btn_model.setToolTip("选择模型 (.pt/.bin)")
        toolbar_layout.addWidget(self.btn_model)
        # lbl_model_path 保留对象（多处 setText 引用链），状态同步到 tooltip 而非显示

        # --- 设置（图标钮） ---
        self.btn_settings = QPushButton()
        self.btn_settings.setFixedSize(48, 48)
        self.btn_settings.setStyleSheet(theme.toggle_btn())
        self.btn_settings.setToolTip("设置")
        self.btn_settings.clicked.connect(self._on_open_settings)
        toolbar_layout.addWidget(self.btn_settings)

        # --- XY 坐标显示开关（图标化） ---
        self.btn_show_xy = QPushButton()
        self.btn_show_xy.setCheckable(True)
        self.btn_show_xy.setFixedSize(48, 48)
        self.btn_show_xy.setStyleSheet(theme.toggle_btn())
        self.btn_show_xy.setToolTip("显示/隐藏坐标")
        self.btn_show_xy.clicked.connect(self._on_toggle_xy)
        toolbar_layout.addWidget(self.btn_show_xy)

        # --- 分隔线 ---
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setStyleSheet("color: #ddd;")
        sep2.setFixedHeight(18)
        toolbar_layout.addWidget(sep2)

        # --- 扫码模式（全局三档，与相机页共享同一 config） ---
        # qr=只认二维码（默认，最省）；barcode=只认条形码；all=全部尝试
        # 下拉切换会同步写 vision.qr_decoder 全局状态 + production_config 存档
        self._scan_mode_labels = ("二维码", "条形码", "全部")
        self._scan_mode_values = ("qr", "barcode", "all")
        self.combo_scan_mode = QComboBox()
        self.combo_scan_mode.addItems(self._scan_mode_labels)
        # 读 config 恢复（杀进程后记忆上次数值），再同步到解码器全局状态
        _saved_mode = self.config.get("scan_mode", "qr")
        set_scan_mode(_saved_mode)
        self.combo_scan_mode.setCurrentIndex(
            self._scan_mode_values.index(_saved_mode) if _saved_mode in self._scan_mode_values else 0)
        self.combo_scan_mode.currentTextChanged.connect(self._on_scan_mode_changed)
        self.combo_scan_mode.setFixedHeight(32)
        self.combo_scan_mode.setToolTip("扫码模式：二维码(qr)/条形码(barcode)/全部(all)")
        toolbar_layout.addWidget(self.combo_scan_mode)

        # --- 三个槽位的检测/扫码按钮对象（不再显示，仅做状态桶） ---
        # _slot_start_btns/_slot_qr_btns 在页内各处被引用（状态同步/复位），
        # 对象保留，去工具栏化；面板里的检测/扫码按钮不动（槽位设置区保持原样）
        for i in range(3):
            slot_id = i + 1
            btn_start = QPushButton()
            btn_start.setCheckable(True)
            btn_start.clicked.connect(lambda checked, s=slot_id: self._on_toggle_slot(s))
            self._slot_start_btns.append(btn_start)

            btn_qr = QPushButton()
            btn_qr.setCheckable(True)
            btn_qr.setChecked(False)  # 扫码默认关闭（检测/扫码分开，手动开）
            btn_qr.clicked.connect(lambda checked, s=slot_id: self._on_toggle_qr(s))
            self._slot_qr_btns.append(btn_qr)

        # --- 激活槽位状态桶（原三按钮组去 UI 化） ---
        # 不再创建 QButtonGroup 界面；_active_slot/_set_active_slot 走整型状态
        self._slot_switch_labels = ("xj1", "xj2", "xj3")
        self._active_slot_id = 1  # 默认 xj1（后面 on_show 按配置恢复）


        # --- 运行日志 / 通讯日志 ---
        # 小屏下工具栏放不下：只创建不显示（log() 仍写入，需要时 tail /tmp/gui.log）
        # widget 保留对象是为了 _log/_com_log 直接 append 不判空炸
        self._log_widget = QTextEdit()
        self._log_widget.setReadOnly(True)
        self._log_widget.hide()  # 不加入工具栏布局 → 不显示

        self._com_log_widget = QTextEdit()
        self._com_log_widget.setReadOnly(True)
        self._com_log_widget.hide()

        # 工具栏右侧添加弹性空间，让左边的控件靠左排列
        toolbar_layout.addStretch()
        right_container_layout.addLayout(toolbar_layout)

        # 给图标钮上色（qtawesome 存在时）
        self._refresh_icons()
        self._refresh_func_button()   # 按初始激活槽位切功能钮二选一
        self._sync_func_button_checked()

        # --- 单预览窗 + 检测列表（串行架构） ---
        # 只有激活槽位在预览/检测；三个 DetectionList 各自缓存本槽结果，切槽换页
        right_container_layout.addWidget(self._create_preview_area(), stretch=1)

        main_layout.addWidget(right_container, stretch=1)  # stretch=1 占满剩余宽度

    def _create_preview_area(self) -> QWidget:
        """
        创建单预览区（串行架构重构）：
        ┌─────────────────────┐
        │ 槽位xjN · 相机M 标题 │
        ├─────────────────────┤
        │                     │
        │    CameraView       │ ← 只有一个预览窗（激活槽位独占）
        │                     │
        ├─────────────────────┤
        │  DetectionList      │ ← QStackedWidget：3 页，切槽位换页显示
        │  （当前槽位）        │
        └─────────────────────┘

        返回：
            组装好的 QWidget
        """
        col = QWidget()
        layout = QVBoxLayout(col)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # 预览窗标题（激活槽位 + 其绑定相机，随切换更新）
        self.lbl_preview_slot = QLabel("槽位xj1 · 相机：未激活")
        self.lbl_preview_slot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_preview_slot.setStyleSheet("font-weight: bold; font-size: 12px; padding: 2px;")
        layout.addWidget(self.lbl_preview_slot)

        # --- 视频预览组件（全局唯一个） ---
        # CameraView 自定义组件：帧显示/FPS/检测框/二维码标记/鼠标坐标
        view = CameraView()
        view.setMinimumSize(320, 220)
        layout.addWidget(view, stretch=3)
        self._slot_views.append(view)  # page 内全部 _slot_views[idx] 引用统一走 [0]

        # --- 检测结果列表堆栈 ---
        # 一页一个槽位各自的 DetectionList：切激活槽位时换页，切回时原结果还在
        self._slot_list_stack = QStackedWidget()
        self._slot_list_stack.setMaximumHeight(110)
        for list_slot in range(3):
            det_list = DetectionList()
            det_list.setMaximumHeight(104)
            self._slot_lists.append(det_list)
            self._slot_list_stack.addWidget(det_list)
        layout.addWidget(self._slot_list_stack, stretch=1)

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
            "模型文件 (*.bin *.pt);;所有文件 (*)"  # 两种过滤器，;; 分隔
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
            # 更新 UI 显示（模型组已隐藏，仅作为状态缓存；tooltip 同步到图标钮）
            self.lbl_model_path.setText(f"已加载: {os.path.basename(path)}")
            self.lbl_model_path.setStyleSheet("color: #07C160; font-size: 11px;")  # 绿色表示成功
            self.btn_model.setToolTip(f"模型已加载: {os.path.basename(path)}\n点击选择 .pt/.bin 模型")
            # 保存模型路径到配置文件，下次启动自动加载
            self.config.set("model_path", path)
            self.config.save()
        else:
            # 加载失败 → 显示红色错误信息
            self.lbl_model_path.setText(f"加载失败: {message}")
            self.lbl_model_path.setStyleSheet("color: #e74c3c; font-size: 11px;")  # 红色表示失败
            self.btn_model.setToolTip(f"模型加载失败: {message}\n点击重新选择")

    # ============ 槽位启停 ============
    # ============ 工具栏图标钮/功能钮 ============
    def _refresh_icons(self):
        """给全部图标钮上色（启用白、禁用灰）。qtawesome 缺失时留空按钮但可用。"""
        if not _HAS_QTA:
            return
        self._qta_color_on = "#ffffff"   # checked 绿底白图标
        self._qta_color_off = "#333"   # 未选中黑灰（对齐相机页）
        for _b in (self.btn_model, self.btn_settings, self.btn_show_xy,
                   self.btn_func_detect, self.btn_func_scan):
            _b.setIconSize(_b.size() * 0.6)  # 48px 按钮 → 图标 ~29px（对齐相机页）
        self.btn_model.setIcon(qta.icon("fa5s.brain", color="#333", scale_factor=1.2))
        self.btn_settings.setIcon(qta.icon("fa5s.cog", color="#333", scale_factor=1.2))
        self.btn_show_xy.setIcon(qta.icon("fa5s.map-marker", color="#333", scale_factor=1.2))
        self.btn_func_detect.setIcon(qta.icon("fa5s.bullseye", color="#333", scale_factor=1.2))
        self.btn_func_scan.setIcon(qta.icon("fa5s.qrcode", color="#333", scale_factor=1.2))

    def _refresh_func_button(self):
        """按当前预览源切换功能钮二选一：深度=检测(YOLO)、RGB=扫码（严格分开）。"""
        slot = self._active_slot()
        is_depth = (0 < slot <= len(self._slot_config_panels)
                    and self._slot_config_panels[slot - 1].is_depth_mode())
        # 列表/预览标题同步显示深度模式样式（沿用原有机制）
        if len(self._slot_lists) >= slot > 0:
            self._slot_lists[slot - 1].set_depth_mode(is_depth)
        self.btn_func_detect.setVisible(is_depth)
        self.btn_func_scan.setVisible(not is_depth)
        self._sync_func_button_checked()

    def _sync_func_button_checked(self):
        """功能钮的选中态从对应 Worker 读出（与其检测/扫码是否在跑一致）。"""
        slot = self._active_slot()
        idx = slot - 1
        m = self._manager
        det_on = False
        scan_on = self._slot_qr_btns[idx].isChecked() if 0 <= idx < 3 else False
        start_on = self._slot_start_btns[idx].isChecked() if 0 <= idx < 3 else False
        if m:
            det_on = start_on and (
                (m._depth_mode[idx] and m._depth_workers[idx] and m._depth_workers[idx]._inference_enabled)
                or (not m._depth_mode[idx] and m._workers[idx] and m._workers[idx]._inference_enabled))
            # 扫码真值=worker 的 qr_enabled（检测关时扫码仍可独立开）
            if m._depth_mode[idx] and m._depth_workers[idx]:
                scan_on = m._depth_workers[idx]._qr_enabled
            elif m._workers[idx]:
                scan_on = m._workers[idx]._qr_enabled
        if det_on != self.btn_func_detect.isChecked():
            self.btn_func_detect.setChecked(det_on)
        if scan_on != self.btn_func_scan.isChecked():
            self.btn_func_scan.setChecked(scan_on)
        # 对应的隐藏状态桶按钮也同步（避免面板与工具栏显示分叉）
        if 0 <= idx < 3:
            self._slot_start_btns[idx].setChecked(det_on)
            self._slot_qr_btns[idx].setChecked(scan_on)
        # 图标着色
        if _HAS_QTA:
            self.btn_func_detect.setIcon(qta.icon(
                "fa5s.bullseye", color=self._qta_color_on if det_on else self._qta_color_off))
            self.btn_func_scan.setIcon(qta.icon(
                "fa5s.qrcode", color=self._qta_color_on if scan_on else self._qta_color_off))

    def _on_tool_detect_clicked(self):
        """功能钮=检测（深度模式专用）：走 per-slot 检测切换（含启动预览逻辑）。"""
        slot = self._active_slot()
        # 用隐藏状态桶按钮按下：_on_toggle_slot 内部会按 is_running 分支启动/切推理
        self._on_toggle_slot(slot)
        self._sync_func_button_checked()

    def _on_tool_scan_clicked(self):
        """功能钮=扫码（RGB 模式专用）：独立开关，不开检测也直接扫。"""
        slot = self._active_slot()
        m = self._manager
        if not (m and m.is_running(slot)):
            # worker 不在 → 先激活（打开相机预览），再开扫码
            self._activate_slot(slot, reason="扫码")
        idx = slot - 1
        enable = not self.btn_func_scan.isChecked()
        self._slot_qr_btns[idx].setChecked(enable)
        self._on_toggle_qr(slot)
        self._sync_func_button_checked()

    # ============ 槽位激活（单预览串行架构核心） ============
    def _refresh_preview_combo(self):
        """
        重建预览下拉选项（选项背后挂 (槽位, 相机索引)）。

        条目构成：
        - 每个槽位绑定的相机构成一条（相机枚举名）
        - 同名相机在多槽绑定或不绑定重叠时，名称带 xjN 标注
        - 槽位1额外加一条深度相机（若面板勾了深度模式）
        """
        if not hasattr(self, "combo_preview") or not self._slot_config_panels:
            return
        self.combo_preview.blockSignals(True)   # 防 currentIndexChanged 死循环
        self.combo_preview.clear()
        act = self._active_slot()

        # 收集绑定条目 [(cam_idx, name, slot_id)]
        rows = []
        for i, panel in enumerate(self._slot_config_panels):
            cam_idx = panel.get_camera_index()
            if cam_idx < 0:
                continue  # 该槽未绑定相机，跳过
            # 深度模式下槽位1显示为深度条目而非相机
            if i == 0 and panel.is_depth_mode():
                continue
            name = self._camera_name(cam_idx)
            rows.append((cam_idx, name, i + 1))

        # 深度条目（槽位1勾了深度 → 第一条）
        if self._slot_config_panels[0].is_depth_mode():
            self.combo_preview.addItem("深度相机 (HP60C)", (-2, -2))

        # 同名区分：同 name 出现 >1 次 → 加 xjN 标注
        name_cnt = {}
        for _, name, _slot in rows:
            name_cnt[name] = name_cnt.get(name, 0) + 1
        for cam_idx, name, slot_id in rows:
            label = f"{name} · xj{slot_id}" if name_cnt.get(name, 0) > 1 else name
            self.combo_preview.addItem(label, (slot_id, cam_idx))

        # 激活槽位的绑定条目置为当前选择（无槽满足时保持不选）
        for i in range(self.combo_preview.count()):
            s, c = self.combo_preview.itemData(i)
            if s == act:
                self.combo_preview.setCurrentIndex(i)
                break
        self.combo_preview.blockSignals(False)

    def _camera_name(self, cam_idx: int) -> str:
        """从枚举缓存里取相机显示名（找不到也兜个名字）。"""
        for idx, name in self._cameras_cache:
            if idx == cam_idx:
                return name
        return f"相机 {cam_idx}"

    def _on_preview_cam_selected(self, index: int):
        """预览下拉选中 → 查绑定槽激活/或把该相机绑到当前激活槽（下拉兼做标定）。"""
        if index < 0 or not self._slot_config_panels:
            return
        data = self.combo_preview.itemData(index)
        if not data:
            return
        slot_flag, cam_idx = data

        # 深度条目 → 切激活槽1（绑定/深度面板已含开/关逻辑）
        if slot_flag == -2:
            self._activate_slot(1, reason="预览下拉(深度)")
            self._refresh_func_button()
            self._sync_func_button_checked()
            return

        # 找绑了这台相机的槽位（同名不区分，第一个绑定优先）
        for i, panel in enumerate(self._slot_config_panels):
            if panel.get_camera_index() == cam_idx:
                self._activate_slot(i + 1, reason="预览下拉")
                self._refresh_func_button()
                self._sync_func_button_checked()
                return

        # 没有任何槽绑它 → 把它绑到当前激活槽（下拉兼做标定）
        cur = self._active_slot()
        idx = cur - 1
        panel = self._slot_config_panels[idx]
        # 程序化把该相机写进槽位面板下拉（blockSignals 防触发 camera_changed 级联）
        cmb = panel.cam_combo
        found = False
        for i in range(cmb.count()):
            if cmb.itemData(i) == cam_idx:
                cmb.blockSignals(True)
                cmb.setCurrentIndex(i)
                cmb.blockSignals(False)
                found = True
                break
        if not found:
            self._log(f"槽位{cur}: 该相机不在枚举列表，无法绑定")
            return
        self._log(f"槽位{cur}: 绑定相机 {cam_idx}（预览下拉标定）")
        self._activate_slot(cur, reason="预览下拉(绑定)")
    def _active_slot(self) -> int:
        """当前激活槽位（1/2/3）。取自激活状态桶（原三按钮组去 UI 化）。"""
        return self._active_slot_id

    def _set_active_slot(self, slot_id: int):
        """程序化设置激活槽位（UI 对齐：状态桶/标题/列表页/预览下拉）。"""
        if not (1 <= slot_id <= 3):
            return
        self._active_slot_id = slot_id
        # 检测列表切到该槽位的结果页
        self._slot_list_stack.setCurrentIndex(slot_id - 1)
        # 预览下拉对齐到该槽位绑定的相机名
        self._refresh_preview_combo()

    def _stop_slot_preview(self, slot_id: int):
        """停一个槽位的预览（断信号 → stop_slot → 清空其列表/按钮）。

        不动单预览窗画面（下一个槽位的新帧会覆盖）。
        """
        if not self._manager or not self._manager.is_running(slot_id):
            return
        idx = slot_id - 1
        worker = self._manager._workers[idx] or self._manager._depth_workers[idx]
        if worker:
            try:
                worker.frame_ready.disconnect()
                worker.detections_ready.disconnect()
            except TypeError:
                pass
        self._manager.stop_slot(slot_id)
        # 重置该槽位按钮 + 清空该槽位结果列表
        if 0 <= idx < 3:
            self._slot_start_btns[idx].setChecked(False)
            self._slot_start_btns[idx].setText("检测")
            self._slot_lists[idx].clear_all()

    def _open_slot_preview(self, slot_id: int) -> bool:
        """打开一个槽位的预览（复用绑定相机/深度开关；返回是否成功启动）。

        语义 = 自动打开（on_show 的第 5 步）和激活切换共用的预览入口：
        - 未配置相机 → 失败（False），提示
        - 模型未加载 → 失败
        - 深度模式（槽位1勾选）→ manager.start_slot_depth
        - 普通模式 → manager.start_slot（不启动检测，只预览）
        """
        if not self._manager or not self._manager.model_loaded:
            self._log(f"槽位{slot_id}: 模型未加载，无法预览")
            return False
        idx = slot_id - 1
        is_depth = (idx == 0 and self._slot_config_panels[idx].is_depth_mode())
        if is_depth:
            params = self._slot_config_panels[idx].get_params()
            self._log(f"槽位{slot_id}: 启动深度相机预览")
            self._manager.start_slot_depth(slot_id, params)
            return True
        cam_idx = self._slot_config_panels[idx].get_camera_index()
        if cam_idx < 0:
            self._log(f"槽位{slot_id}: 未绑定摄像头（在左侧面板下拉里选）")
            return False
        params = self._slot_config_panels[idx].get_params()
        self._log(f"槽位{slot_id}: 预览相机 {cam_idx}")
        self._manager.start_slot(slot_id, cam_idx, params)
        return True

    def _on_activate_slot_clicked(self, btn_id: int):
        """工具栏槽位按钮点击（id is 0-based）→ 切激活槽位。"""
        self._activate_slot(self._slot_switch_labels.index(
            self.slot_switch_group.button(btn_id).text()) + 1)

    def _activate_slot(self, slot_id: int, reason: str = "manual"):
        """
        激活一个槽位（单预览窗切换核心）。

        流程：
        1. 已是激活槽且在预览 → no-op
        2. 停上一个激活槽位的相机/Worker（释放 USB 和 CPU）
        3. 打开新槽位预览（未配置相机则提示、画面停在黑屏）
        4. UI 对齐：按钮/标题/列表堆栈/面板折叠 + config 存档

        参数：
            slot_id: 目标槽位（1/2/3）
            reason: 触发来源（manual / mqtt / restore），仅日志可见
        """
        if not (1 <= slot_id <= 3):
            return
        cur = self._active_slot()
        # 同槽位且在跑 → 无操作
        if cur != slot_id and self._manager:
            self._stop_slot_preview(cur)          # 释放上一个槽位的相机
        if self._manager and not self._manager.is_running(slot_id):
            opened = self._open_slot_preview(slot_id)
        else:
            opened = True
        self._set_active_slot(slot_id)
        # 预览标题
        idx = slot_id - 1
        if self._slot_config_panels[idx].is_depth_mode():
            cam_desc = "深度相机 (HP60C)"
        else:
            cam_idx = self._slot_config_panels[idx].get_camera_index()
            cam_desc = f"相机 {cam_idx}" if cam_idx >= 0 else "未绑定相机"
        self.lbl_preview_slot.setText(f"槽位xj{slot_id} · {cam_desc}")
        self.config.set("active_slot", slot_id)
        self.config.save()
        self._log(f"激活槽位xj{slot_id} [{reason}]")
        # 激活源变化 → 功能钮"检测/扫码"二选一切换 + 预览下拉对齐
        self._refresh_func_button()
        self._sync_func_button_checked()

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
                self._slot_views[0].clear()       # 清空预览画面
            return

        # 检查模型是否已加载（没有模型就无法预览，因为 Worker 需要模型来处理帧）
        if not self._manager or not self._manager.model_loaded:
            self._log(f"槽位{slot_id}: 模型未加载，跳过")
            return

        # 串行架构：绑定相机 ≠ 立刻打开预览。
        # 槽位在焦点（正被激活/预览）时 → 用新相机重启预览（标定效果：选了就能看到）
        # 槽位不在焦点 → 仅保存绑定，打开预览要走激活流程（避免屏幕出现两路相机）
        if slot_id != self._active_slot():
            self._log(f"槽位{slot_id}: 已绑定相机{cam_idx}（点工具栏 xj{slot_id} 激活预览）")
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

        # 面板相机绑定变化 → 预览下拉同步（标定联动）
        self._refresh_preview_combo()

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

        # 串行单预览：点非激活槽的「检测」= 先切激活到该槽（相机切换在 _activate_slot 完成）
        # 之后按 is_running 继续原流程：已在预览 → 切推理开关；否则启动
        if slot_id != self._active_slot():
            self._activate_slot(slot_id, reason="检测按钮")

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

        # 串行单预览：点非激活槽的「扫码」= 先切激活到该槽（相机会跟着切换）
        if slot_id != self._active_slot():
            self._activate_slot(slot_id, reason="扫码按钮")

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
            # 深度相机节点（ascamera）没跑的话 DepthWorker 订不到话题，预览一直是黑的。
            # 这里自动拉起节点（后台线程复用「重启相机」流程，约 11 秒），
            # 节点一起来 DepthWorker 就自动接上画面。
            if not self._depth_node_running():
                self._log("槽位1: 深度相机节点未运行，自动启动（约 11 秒，期间预览暂黑）...")
                self._on_restart_camera(1)
            # 深度预览与模型无关：没加载模型也能看画面
            if self._manager:
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
                self._slot_views[0].clear()
            self._log(f"槽位{slot_id}: 深度相机模式已关闭")

        # 深度模式变 → 功能钮"检测/扫码"二选一切换 + 预览下拉重建（深度条目增减）
        self._refresh_func_button()
        self._sync_func_button_checked()
        self._refresh_preview_combo()

    def _depth_node_running(self) -> bool:
        """
        检测深度相机 ROS2 节点（ascamera/hp60c.launch.py）是否在跑。

        用 pgrep 而不是 ros2 topic 查询（ros2 命令要 source 环境，慢且易挂）。

        返回:
            True=节点进程存在
        """
        try:
            import subprocess
            r = subprocess.run(["pgrep", "-f", "hp60c.launch.py"],
                               capture_output=True)
            return r.returncode == 0
        except Exception:
            return False

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
        # 若重启前深度模式开着，自动重新拉起 DepthWorker 预览（与模型无关）
        if resume_depth and self._manager:
            params = self._slot_config_panels[0].get_params()
            self._manager.start_slot_depth(1, params)
            self._log("深度相机: 预览已恢复")

    def _on_open_settings(self):
        """
        点击「⚙ 设置」按钮 —— 打开参数配置弹窗（非模态）。

        非模态（show 而非 exec）：弹窗打开时预览/检测照常跑，可以边看画面边调参。
        首次点击创建 QDialog，之后仅 show/raise，控件状态在弹窗关闭后保留。
        """
        if self._settings_dialog is None:
            d = QDialog(self)
            d.setWindowTitle("产线设置")
            v = QVBoxLayout(d)
            v.setContentsMargins(6, 6, 6, 6)
            v.addWidget(self._settings_scroll)
            d.setFixedWidth(320)  # 调宽：扫码间隔等滑块不再挤扁
            self._settings_dialog = d
        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

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

    def _on_scan_mode_changed(self, text: str):
        """
        扫码模式下拉变化后执行（与相机页共享同一个 config）。

        三档语义：
        - 二维码(qr)：ZBar 只注册 QRCODE，条形码 100% 拼不出（默认，最省）
        - 条形码(barcode)：只注册 1D 符号，QR 100% 拼不出
        - 全部(all)：全符号 + 全链（成本最高）

        流程：写 vision.qr_decoder 全局状态（所有 Worker 即时生效）+ config 存档。
        """
        idx = self._scan_mode_labels.index(text) if text in self._scan_mode_labels else 0
        mode = self._scan_mode_values[idx]
        set_scan_mode(mode)
        self.config.set("scan_mode", mode)
        self.config.save()
        self._log(f"扫码模式: {text}")

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
        self._slot_views[0].update_detections(detections)
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
        self._slot_views[0].update_qr_results(qr_results)

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
        view = self._slot_views[0]

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

    def _on_slot_activated(self, slot_id: int):
        """
        MQTT 指令指定槽位 → 预览切到该槽位（单预览串行架构）。

        流程：调 _activate_slot —— 若该槽未在预览则按面板绑定打开相机，
        UI 全面切到位（按钮/标题/列表/面板折叠）。检测/扫码开关由
        Manager._parse_mqtt_cmd 自己处理（enable/disable_detection）。

        参数：
            slot_id: MQTT 指令的目标槽位（1/2/3）
        """
        self._activate_slot(slot_id, reason="mqtt")

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

        # 更新 UI 状态指示灯（文字进 tooltip）
        self._set_mqtt_status(connected)

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
        更新 MQTT 状态指示灯的显示（只有灯：文字/详情在 tooltip 里）。

        参数：
            connected: True=已连接（绿）, False=未连接（红）
        """
        dot = self._mqtt_status_dot
        if connected:
            dot.setStyleSheet("color: #07C160; font-size: 14px;")
            dot.setToolTip("MQTT 已连接")
        else:
            # 连接失败原因在连接对象的最后错误记录里（若有）
            reason = ""
            try:
                cli = self._mqtt or get_shared_mqtt()
                reason = getattr(cli, "last_error", "") or ""
            except Exception:
                pass
            dot.setStyleSheet("color: #e74c3c; font-size: 14px;")
            dot.setToolTip(f"MQTT 未连接{('：' + reason) if reason else ''}")
        dot.setToolTipDuration(6000)

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
        self._cameras_cache = cameras  # 存缓存（预览下拉取名用）
        # 更新所有槽位配置面板的摄像头下拉列表
        for panel in self._slot_config_panels:
            panel.set_cameras(cameras)
        self._refresh_preview_combo()

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
        # 0. 扫码模式下拉先对齐全局状态（可能刚在相机页切过）
        _mode_idx = self._scan_mode_values.index(get_scan_mode()) if get_scan_mode() in self._scan_mode_values else 0
        self.combo_scan_mode.blockSignals(True)   # 防触发 _on_scan_mode_changed 再写一遍
        self.combo_scan_mode.setCurrentIndex(_mode_idx)
        self.combo_scan_mode.blockSignals(False)

        # 0.1 左面板的模型分组框不再使用（模型选择已上移工具栏）
        if hasattr(self, "model_group"):
            self.model_group.hide()

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
        self._cameras_cache = cameras
        for panel in self._slot_config_panels:
            panel.set_cameras(cameras)
        self._refresh_preview_combo()

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
                self.btn_model.setToolTip(f"模型文件不存在: {model_path}\n点击重新选择")
            else:
                try:
                    ok = self._manager.load_model(model_path)
                except Exception as e:
                    ok = False
                    self._log(f"模型加载异常: {e}")
                if ok:
                    self.lbl_model_path.setText(f"已加载: {os.path.basename(model_path)}")
                    self.lbl_model_path.setStyleSheet("color: #07C160; font-size: 11px;")
                    self.btn_model.setToolTip(f"模型已加载: {os.path.basename(model_path)}\n点击选择 .pt/.bin 模型")
                else:
                    self._log(f"模型加载失败: {model_path}")
                    self.lbl_model_path.setText(f"加载失败: {os.path.basename(model_path)}")
                    self.lbl_model_path.setStyleSheet("color: #e74c3c; font-size: 11px;")
                    self.btn_model.setToolTip(f"模型加载失败: {os.path.basename(model_path)}\n点击重新选择")

        # 5. 自动打开激活槽位的预览（单预览串行架构：只开这一个）
        # 上次激活槽位从 config 恢复（默认 1），其余槽位不开（要用再点工具栏切换）
        try:
            saved_active = int(self.config.get("active_slot", 1))
        except (TypeError, ValueError):
            saved_active = 1
        saved_active = saved_active if 1 <= saved_active <= 3 else 1
        # _activate_slot 内部做 stop(旧)/open(新)/UI 对齐全套；
        # on_hide 已全停过后，这里 stop 分支 no-op；_activate_slot 自身用
        # is_running 判断防重复开启
        self._activate_slot(saved_active, reason="restore")

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
                self._slot_views[0].clear()       # 清空预览画面

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
