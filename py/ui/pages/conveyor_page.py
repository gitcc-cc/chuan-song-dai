"""
传送带控制页面
=============
职责：传送带控制页面，包含 MQTT 连接管理和电机控制功能
功能：
1. MQTT 连接管理：连接/断开 ESP32 设备，显示连接状态
2. 电机控制：正转、反转、停止、自动模式切换
3. 速度控制：通过滑块或输入框调整电机速度
4. 状态监控：实时显示设备状态（电机模式、电位器值、传感器状态等）
5. 指令发送：支持手动输入 MQTT 指令
6. 日志记录：显示通信日志，便于调试

技术栈：PyQt6 + MQTT + ESP32
"""
from datetime import datetime
import threading

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QSlider, QLineEdit, QTextEdit, QFrame, QGroupBox, QMessageBox, QSplitter
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QFont

from core.esp32_client import get_shared_esp32, connect_shared, disconnect_shared
from core.controller import Controller
from ui.pages.base_page import BasePage
from styles import STYLE


# 状态轮询间隔（毫秒）
POLL_INTERVAL = 2000


class WorkerSignals(QObject):
    """
    Qt 信号集合：用于后台线程与主线程之间的通信
    
    为什么需要这个类？
    - PyQt6 中，只有主线程才能更新 UI
    - MQTT 连接、数据收发等操作在后台线程执行
    - 通过信号机制，后台线程可以安全地通知主线程更新 UI
    
    信号说明：
    - connected: 连接成功时发射
    - disconnected: 连接断开时发射
    - connect_failed: 连接失败时发射，携带错误信息
    - status_update: 收到设备状态更新时发射，携带状态数据
    - log_message: 需要记录日志时发射，携带日志消息
    """
    connected = pyqtSignal()           # 连接成功信号
    disconnected = pyqtSignal()        # 连接断开信号
    connect_failed = pyqtSignal(str)   # 连接失败信号，参数：错误信息
    status_update = pyqtSignal(dict)   # 状态更新信号，参数：状态字典
    log_message = pyqtSignal(str)      # 日志消息信号，参数：消息内容


class ConveyorPage(BasePage):
    """
    传送带控制页面
    
    主要功能：
    1. 连接 ESP32 设备（通过 MQTT）
    2. 控制传送带电机（正转、反转、停止、自动）
    3. 调整电机速度（滑块或输入框）
    4. 显示设备实时状态
    5. 记录通信日志
    """

    def __init__(self, parent=None):
        """
        初始化传送带控制页面
        
        参数：
            parent: 父 QWidget，默认为 None
        
        初始化流程：
        1. 调用父类构造函数
        2. 初始化通信客户端（MQTT）
        3. 初始化控制器（业务逻辑）
        4. 初始化信号对象（线程间通信）
        5. 初始化状态数据缓存
        6. 设置页面样式
        7. 创建界面布局
        8. 连接信号与槽函数
        """
        super().__init__(parent)

        # 通信层 & 逻辑层
        # client: MQTT 客户端，负责与 ESP32 设备通信
        # controller: 控制器，封装业务逻辑（如状态文本转换）
        # signals: 信号对象，用于后台线程安全地更新 UI
        self.client = get_shared_esp32()
        self.controller = Controller()
        self.signals = WorkerSignals()

        # 当前缓存数据
        # 存储从设备收到的最新状态，用于对比更新
        self.current_data = {
            "state": 0,    # 电机状态：0=停止, 1=正转, 2=反转, 3=自动
            "speed": 0,    # 舵机速度：0-13
            "pot": 0,      # 电位器值：0-100%
            "sensor": 0,   # 光纤传感器：0=未检测, 1=检测到物体
            "mode": 0      # 速度模式：0=就地(电位器), 1=远程(滑块)
        }

        # 速度模式：0=就地(电位器)，1=远程(滑块)
        # 就地模式：速度由 ESP32 上的电位器控制
        # 远程模式：速度由 GUI 滑块控制
        self.speed_mode = 0
        
        # 滑块更新标志：防止循环触发
        # 当程序更新滑块值时，会触发 valueChanged 信号
        # 设置此标志为 True 可以跳过信号处理，避免循环
        self.slider_updating = False

        # 页面标题
        self.setWindowTitle("传送带控制")
        self.setStyleSheet(STYLE)

        # 创建界面
        self._create_ui()

        # 绑定信号与槽函数
        # 当后台线程发射信号时，对应的槽函数会被调用
        self.signals.connected.connect(self._on_connected)
        self.signals.disconnected.connect(self._on_disconnected)
        self.signals.connect_failed.connect(self._on_connect_failed)
        self.signals.status_update.connect(self._update_display)
        self.signals.log_message.connect(self._append_log)

    # ============ 创建界面 ============
    def _create_ui(self):
        """
        创建完整界面布局
        
        布局结构：
        ┌─────────────────────────────────────┐
        │              顶栏                   │
        │  (标题、IP输入、端口、连接按钮、时钟)  │
        ├──────────────┬──────────────────────┤
        │              │                      │
        │   系统状态   │      控制面板         │
        │   (左侧)     │      (右侧)          │
        │              │  (速度控制、设备控制)  │
        │              │                      │
        ├──────────────┴──────────────────────┤
        │           指令输入 + 日志            │
        └─────────────────────────────────────┘
        
        使用 QSplitter 实现可拖拽分割，用户可以调整各区域大小
        """
        # 主布局：垂直布局
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(8)                    # 控件间距 8px
        main_layout.setContentsMargins(10, 10, 10, 10)  # 边距 10px

        # 顶栏：标题、连接设置、时钟
        header = self._create_header()
        main_layout.addWidget(header)

        # 中间区域：左右分割（系统状态 + 控制面板）
        # QSplitter 允许用户拖拽调整左右区域大小
        horizontal_splitter = QSplitter(Qt.Orientation.Horizontal)
        horizontal_splitter.setHandleWidth(6)  # 分割线宽度 6px
        horizontal_splitter.setStyleSheet("""
            QSplitter::handle { background-color: #e0e0e0; }
            QSplitter::handle:hover { background-color: #07C160; }
        """)

        # 左侧：系统状态面板（显示电机状态、传感器状态等）
        status_group = self._create_status_panel()
        horizontal_splitter.addWidget(status_group)

        # 右侧：控制面板（速度控制、设备控制按钮）
        right_widget = self._create_control_panel()
        horizontal_splitter.addWidget(right_widget)

        # 设置初始宽度比例 1:1
        horizontal_splitter.setSizes([400, 400])
        main_layout.addWidget(horizontal_splitter, stretch=1)

        # 底部：指令输入框 + 通信日志
        bottom_widget = self._create_bottom_panel()
        
        # 垂直分割：上半部分是中间区域，下半部分是底部区域
        vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        vertical_splitter.setHandleWidth(6)
        vertical_splitter.setStyleSheet("""
            QSplitter::handle { background-color: #e0e0e0; height: 3px; }
            QSplitter::handle:hover { background-color: #07C160; }
        """)
        vertical_splitter.addWidget(horizontal_splitter)
        vertical_splitter.addWidget(bottom_widget)
        
        # 设置初始高度比例 5:2（中间区域占大部分）
        vertical_splitter.setSizes([500, 200])
        main_layout.addWidget(vertical_splitter, stretch=1)

    def _create_header(self):
        """
        创建顶栏
        
        顶栏包含：
        1. 页面标题
        2. MQTT Broker IP 地址输入框
        3. 端口号输入框
        4. 连接/断开按钮
        5. 实时时钟
        6. 连接状态显示
        
        布局：水平布局，从左到右依次排列
        """
        header = QFrame()
        header.setFixedHeight(65)  # 固定高度 65px
        header.setStyleSheet(
            "background-color: #ffffff; border-bottom: 1px solid #e0e0e0;"
        )
        # 水平布局，控件从左到右排列
        layout = QHBoxLayout(header)
        layout.setContentsMargins(15, 8, 15, 8)  # 左、上、右、下边距

        # 标题
        title = QLabel("ESP32 控制中心")
        title.setFont(QFont("", 18, QFont.Weight.Bold))  # 字体大小18，加粗
        title.setStyleSheet("color: #333333; border: none;")
        layout.addWidget(title)
        layout.addSpacing(30)  # 标题和后面控件之间留30px间距

        # IP 输入框
        # 导入默认 Broker 地址和端口
        from core.esp32_client import DEFAULT_BROKER, DEFAULT_PORT
        ip_label = QLabel("IP:")
        ip_label.setStyleSheet("color: #333333; font-size: 15px; font-weight: bold;")
        layout.addWidget(ip_label)

        # 输入框：用于输入 MQTT Broker 的 IP 地址
        self.broker_entry = QLineEdit(DEFAULT_BROKER)  # 默认值：127.0.0.1
        self.broker_entry.setFixedWidth(160)  # 固定宽度 160px
        self.broker_entry.setStyleSheet(self._input_style())
        layout.addWidget(self.broker_entry)

        # 端口输入框
        port_label = QLabel("端口:")
        port_label.setStyleSheet("color: #333333; font-size: 15px; font-weight: bold;")
        layout.addWidget(port_label)

        # 输入框：用于输入 MQTT Broker 的端口号
        self.port_entry = QLineEdit(str(DEFAULT_PORT))  # 默认值：1883
        self.port_entry.setFixedWidth(80)  # 固定宽度 80px
        self.port_entry.setStyleSheet(self._input_style())
        layout.addWidget(self.port_entry)

        # 连接按钮：点击后连接或断开 ESP32 设备
        self.btn_connect = QPushButton("连接设备")
        self.btn_connect.setFixedWidth(100)
        self.btn_connect.setStyleSheet(self._connect_btn_style(False))
        # 点击按钮时调用 _on_connect_clicked 方法
        self.btn_connect.clicked.connect(self._on_connect_clicked)
        layout.addWidget(self.btn_connect)

        # 添加弹性空间，让后面的控件靠右对齐
        layout.addStretch()

        # 实时时钟：显示当前时间
        self.lbl_clock = QLabel("00:00:00")
        self.lbl_clock.setFont(QFont("", 15))
        self.lbl_clock.setStyleSheet("color: #999999;")
        layout.addWidget(self.lbl_clock)
        layout.addSpacing(15)

        # 连接状态标签：显示当前连接状态
        self.lbl_conn = QLabel("未连接")
        self.lbl_conn.setFont(QFont("", 15))
        # 红色字体表示未连接
        self.lbl_conn.setStyleSheet("color: #fa5151; font-weight: bold;")
        layout.addWidget(self.lbl_conn)

        # 时钟定时器：每秒更新一次时间显示
        self.clock_timer = QTimer()
        # 超时时调用 _update_clock 方法
        self.clock_timer.timeout.connect(self._update_clock)
        self.clock_timer.start(1000)  # 间隔 1000ms = 1秒
        self._update_clock()  # 立即显示当前时间

        return header

    def _input_style(self):
        """
        返回输入框的样式表（QSS）
        
        样式说明：
        - 正常状态：浅灰色背景，灰色边框，圆角6px
        - 获得焦点时：边框变为绿色（#07C160），表示当前激活
        
        QSS 类似 CSS，用于美化 Qt 控件
        """
        return ("QLineEdit{background:#f5f5f5; border:1px solid #e0e0e0; "
                "border-radius:6px; padding:6px 10px; font-size:15px;}"
                "QLineEdit:focus{border-color:#07C160;}")

    def _connect_btn_style(self, connected: bool):
        """
        返回连接按钮的样式表
        
        参数：
            connected: 当前连接状态
            
        样式逻辑：
        - 已连接：红色背景（表示可以断开）
        - 未连接：绿色背景（表示可以连接）
        
        hover 效果：鼠标悬停时颜色变深
        """
        if connected:
            # 已连接状态：红色按钮，表示"断开"操作
            return ("QPushButton{background:#fa5151; color:#fff; border:none;"
                    "border-radius:6px; padding:8px 16px; font-weight:bold; font-size:15px;}"
                    "QPushButton:hover{background:#E04444;}")
        # 未连接状态：绿色按钮，表示"连接"操作
        return ("QPushButton{background:#07C160; color:#fff; border:none;"
                "border-radius:6px; padding:8px 16px; font-weight:bold; font-size:15px;}"
                "QPushButton:hover{background:#06AD56;}")

    def _create_status_panel(self):
        """
        创建系统状态面板
        
        功能：显示设备的各种状态信息
        - 电机模式：停止/正转/反转/自动
        - 舵机模式：正转/反转
        - 电位器值：0-100%
        - 舵机速度：0-13
        - 光纤传感器：未检测/检测到物体
        - 速度模式：就地/远程
        - 网络连接：断开/已连接
        
        布局：垂直排列的行，每行包含名称和值
        """
        group = QGroupBox("系统状态")  # 带标题的分组框
        layout = QVBoxLayout(group)    # 垂直布局
        layout.setContentsMargins(8, 10, 8, 8)
        layout.setSpacing(0)  # 行间距为0，紧凑排列

        # 状态项定义：(行号, 名称, 默认值, 颜色)
        status_items = [
            (0, "电机模式", "停止",   "#333333"),  # 电机当前状态
            (1, "舵机模式", "—",      "#333333"),  # 舵机方向
            (2, "电位器",   "0 %",    "#333333"),  # 电位器位置
            (3, "舵机速度", "0 / 13", "#333333"),  # 舵机速度
            (4, "光纤传感器", "未检测", "#333333"),  # 传感器状态
            (5, "速度模式", "就地",    "#07C160"),  # 速度控制模式
            (6, "网络连接", "断开",    "#fa5151"),  # MQTT 连接状态
        ]

        # 存储状态标签的字典，用于后续更新
        # 键：状态名称，值：QLabel 控件
        self.status_labels = {}

        # 为每个状态项创建一行
        for i, (row, name, default, color) in enumerate(status_items):
            # 创建一行容器
            row_widget = QWidget()
            # 交替行背景色：偶数行浅灰，奇数行白色
            bg = "#f8f8f8" if i % 2 == 0 else "#ffffff"
            row_widget.setObjectName(f"row{i}")  # 设置对象名，用于样式表
            row_widget.setStyleSheet(f"QWidget#row{i}{{background:{bg}; border:none;}}")

            # 行内水平布局
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(12, 0, 12, 0)  # 左右边距 12px
            row_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

            # 状态名称标签（左侧）
            lbl_name = QLabel(name)
            lbl_name.setStyleSheet(
                "color: #333333; font-size: 16px; font-weight: bold; background:transparent;"
            )
            lbl_name.setFixedWidth(110)  # 固定宽度，对齐整齐
            lbl_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row_layout.addWidget(lbl_name)

            # 状态值标签（右侧），将被动态更新
            lbl_value = QLabel(default)
            lbl_value.setStyleSheet(
                f"color: {color}; font-size: 16px; font-weight: bold; background:transparent;"
            )
            lbl_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row_layout.addWidget(lbl_value, stretch=1)  # stretch=1 占据剩余空间

            # 保存标签引用，以便后续更新
            self.status_labels[name] = lbl_value
            layout.addWidget(row_widget, stretch=1)

        return group

    def _create_control_panel(self):
        """
        创建右侧控制面板
        
        包含：
        1. 速度控制区域：滑块 + 输入框
        2. 设备控制区域：按钮网格
        3. 模式切换按钮：就地/远程模式切换
        
        布局：
        ┌─────────────────────┐
        │     速度控制        │
        │  [滑块] [输入框]    │
        ├─────────────────────┤
        │     设备控制        │
        │  [正转] [反转]      │
        │  [停止] [自动]      │
        │  [解抱闸] [上抱闸]  │
        ├─────────────────────┤
        │  [就地模式] 按钮    │
        └─────────────────────┘
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 速度控制区域
        speed_group = QGroupBox("速度控制")
        speed_layout = QVBoxLayout(speed_group)
        speed_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # 速度控制：滑块 + 输入框
        slider_layout = QHBoxLayout()
        slider_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # 速度滑块：水平方向，范围 0-13
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(0, 13)       # 范围：0 到 13
        self.speed_slider.setValue(0)           # 初始值：0
        # 值改变时调用 _on_slider_change
        self.speed_slider.valueChanged.connect(self._on_slider_change)
        self.speed_slider.setMinimumHeight(30)  # 最小高度 30px
        slider_layout.addWidget(self.speed_slider, stretch=1)  # stretch=1 占据剩余空间

        # 速度输入框：可以手动输入速度值
        self.speed_entry = QLineEdit("0")
        self.speed_entry.setFixedWidth(55)  # 固定宽度 55px
        self.speed_entry.setAlignment(Qt.AlignmentFlag.AlignCenter)  # 居中对齐
        # 回车时调用 _on_entry_change
        self.speed_entry.returnPressed.connect(self._on_entry_change)
        slider_layout.addWidget(self.speed_entry)

        speed_layout.addLayout(slider_layout)
        layout.addWidget(speed_group, stretch=0)  # stretch=0 不伸缩

        # 设备控制区域：按钮网格
        ctrl_group = QGroupBox("设备控制")
        ctrl_grid = QGridLayout(ctrl_group)  # 网格布局

        # 按钮定义：(行, 列, 文字, 背景色, 字体色, 悬停色, MQTT命令)
        buttons = [
            (0, 0, "正转",   "#576B95", "#fff", "#4A5D84", "dj 1"),   # 正转命令
            (0, 1, "反转",   "#576B95", "#fff", "#4A5D84", "dj 2"),   # 反转命令
            (1, 0, "停止",   "#fa5151", "#fff", "#E04444", "dj 0"),   # 停止命令
            (1, 1, "自动",   "#FFC300", "#333", "#E6B000", "dj 3"),   # 自动模式命令
            (2, 0, "解抱闸", "#E67E22", "#fff", "#D35400", "en 0"),   # 解除抱闸
            (2, 1, "上抱闸", "#E67E22", "#fff", "#D35400", "en 1"),   # 启用抱闸
        ]

        # 创建按钮并添加到网格
        for row, col, text, bg, fg, hover, cmd in buttons:
            btn = QPushButton(text)
            btn.setStyleSheet(
                f"QPushButton{{background:{bg}; color:{fg}; border:none;"
                f"border-radius:6px; padding:12px; font-weight:bold; font-size:15px;}}"
                f"QPushButton:hover{{background:{hover};}}"
            )
            btn.setMinimumHeight(48)  # 最小高度 48px
            # 点击按钮时发送对应的 MQTT 命令
            # lambda c=cmd: 捕获当前 cmd 值，避免闭包陷阱
            btn.clicked.connect(lambda checked, c=cmd: self._send_command(c))
            ctrl_grid.addWidget(btn, row, col)

        # 模式切换按钮：就地模式 ↔ 远程模式
        self.btn_mode = QPushButton("就地模式")
        self.btn_mode.setStyleSheet(
            "QPushButton{background:#07C160; color:#fff;"
            "border:none; border-radius:6px; padding:12px;"
            "font-weight:bold; font-size:16px;}"
            "QPushButton:hover{background:#06AD56;}"
        )
        self.btn_mode.setMinimumHeight(52)  # 最小高度 52px
        # 点击时调用 _toggle_mode 切换模式
        self.btn_mode.clicked.connect(self._toggle_mode)
        # 添加到网格第3行，跨2列
        ctrl_grid.addWidget(self.btn_mode, 3, 0, 1, 2)

        layout.addWidget(ctrl_group, stretch=3)  # stretch=3 占据大部分空间
        return widget

    def _create_bottom_panel(self):
        """
        创建底部面板：指令输入 + 通信日志
        
        布局：
        ┌─────────────────────────────────────┐
        │  > [输入指令，回车发送...] [发送]    │  ← 指令输入框
        ├─────────────────────────────────────┤
        │  通信日志                           │
        │  [14:30:01] → status                │  ← 日志显示
        │  [14:30:01] ← {"state":1,...}       │
        └─────────────────────────────────────┘
        
        功能：
        1. 指令输入框：手动输入 MQTT 指令
        2. 发送按钮：发送输入的指令
        3. 通信日志：显示发送和接收的消息
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 指令输入区域
        cmd_frame = QFrame()
        cmd_frame.setStyleSheet(
            "background-color: #fff; border: 1px solid #e0e0e0; border-radius: 8px;"
        )
        cmd_layout = QHBoxLayout(cmd_frame)
        cmd_layout.setContentsMargins(10, 10, 10, 10)
        cmd_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # 提示符 ">"
        prompt = QLabel(">")
        prompt.setStyleSheet("color: #07C160; font-size: 18px; font-weight: bold;")
        cmd_layout.addWidget(prompt)

        # 指令输入框
        self.cmd_entry = QLineEdit()
        self.cmd_entry.setPlaceholderText("输入指令，回车发送...")  # 占位提示文字
        # 按回车时发送指令
        self.cmd_entry.returnPressed.connect(self._on_send_command)
        cmd_layout.addWidget(self.cmd_entry, stretch=1)  # stretch=1 占据剩余空间

        # 发送按钮
        btn_send = QPushButton("发送")
        btn_send.setFixedWidth(80)  # 固定宽度 80px
        btn_send.setStyleSheet(
            "QPushButton{background:#07C160; color:#fff; border:none;"
            "border-radius:6px; padding:10px; font-weight:bold; font-size:15px;}"
            "QPushButton:hover{background:#06AD56;}"
        )
        # 点击按钮发送指令
        btn_send.clicked.connect(self._on_send_command)
        cmd_layout.addWidget(btn_send)

        layout.addWidget(cmd_frame)

        # 通信日志区域
        log_group = QGroupBox("通信日志")
        log_layout = QVBoxLayout(log_group)
        
        # 日志文本框：只读，用于显示通信记录
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)  # 设置为只读
        self.log_text.setFont(QFont("", 13))  # 字体大小 13
        log_layout.addWidget(self.log_text)
        
        # stretch=1 让日志区域占据剩余空间
        layout.addWidget(log_group, stretch=1)

        return widget

    # ============ 基础功能 ============
    def _update_clock(self):
        """
        更新时钟显示
        
        功能：获取当前时间并格式化为 "时:分:秒" 格式
        调用方式：由 clock_timer 每秒调用一次
        
        strftime 格式说明：
        - %H: 小时 (00-23)
        - %M: 分钟 (00-59)
        - %S: 秒 (00-59)
        """
        now = datetime.now().strftime("%H:%M:%S")  # 获取当前时间
        self.lbl_clock.setText(now)  # 更新时钟标签

    # ============ 连接逻辑 ============
    def _on_connect_clicked(self):
        """
        连接按钮点击事件处理
        
        逻辑：
        1. 如果已连接 → 断开连接
        2. 如果未连接 → 尝试连接
        
        为什么使用后台线程？
        - MQTT 连接可能耗时较长（网络延迟）
        - 如果在主线程执行，会阻塞 UI，导致界面卡顿
        - 使用 threading.Thread 在后台执行，保持 UI 响应
        """
        # 如果已连接，则断开
        if self.client.is_connected():
            disconnect_shared()  # 断开全局 MQTT 连接
            self._update_connection_ui(False)  # 更新 UI 状态
            self.signals.log_message.emit("手动断开连接")
            return

        # 获取输入的 Broker 地址和端口
        broker = self.broker_entry.text().strip()
        port_str = self.port_entry.text().strip()
        if not broker:
            QMessageBox.warning(self, "提示", "请输入Broker地址")
            return

        # 禁用连接按钮，防止重复点击
        self.btn_connect.setEnabled(False)
        self.btn_connect.setText("连接中...")

        # 定义连接函数（在后台线程执行）
        def do_connect():
            try:
                self.signals.log_message.emit(f"正在连接 {broker}:{port_str} ...")
                # 调用全局连接函数
                result = connect_shared(
                    broker=broker,
                    port=int(port_str) if port_str else 1883  # 默认端口 1883
                )
                if result:
                    # 连接成功，发射 connected 信号
                    self.signals.connected.emit()
                else:
                    # 连接失败，发射 connect_failed 信号
                    self.signals.connect_failed.emit(f"{broker}:{port_str}")
            except Exception:
                # 连接异常，发射 connect_failed 信号
                self.signals.connect_failed.emit(f"{broker}:{port_str}")

        # 启动后台线程执行连接
        # daemon=True: 主程序退出时线程自动结束
        threading.Thread(target=do_connect, daemon=True).start()

    def _on_connected(self):
        """
        连接成功后的处理
        
        功能：
        1. 更新 UI 显示连接状态
        2. 启动状态轮询定时器
        
        为什么需要状态轮询？
        - ESP32 设备状态可能变化（如传感器触发、电位器调整）
        - 轮询可以主动获取最新状态，保持 UI 同步
        - 轮询间隔 2000ms（2秒），平衡实时性和网络负载
        """
        self._update_connection_ui(True)  # 更新 UI 为已连接状态
        self.btn_connect.setEnabled(True)  # 重新启用连接按钮
        self.signals.log_message.emit("Connected!")

        # 启动状态轮询定时器
        self.poll_timer = QTimer()
        # 每次超时时发送 'status' 命令获取设备状态
        self.poll_timer.timeout.connect(lambda: self._send_command('status'))
        self.poll_timer.start(POLL_INTERVAL)  # 间隔 2000ms
        self.signals.log_message.emit(f"启动状态轮询 ({POLL_INTERVAL}ms间隔)")

    def _on_connect_failed(self, addr):
        """
        连接失败后的处理
        
        参数：
            addr: 连接地址（IP:端口）
        
        功能：
        1. 停止状态轮询定时器
        2. 恢复连接按钮状态
        3. 显示错误对话框，询问是否重试
        
        注意：hasattr 检查防止 AttributeError
        """
        # 停止并清理轮询定时器（如果存在）
        if hasattr(self, 'poll_timer'):
            self.poll_timer.stop()
            self.poll_timer.deleteLater()  # 定时器对象在适当时候被删除
            del self.poll_timer

        # 恢复连接按钮状态
        self.btn_connect.setText("连接设备")
        self.btn_connect.setStyleSheet(self._connect_btn_style(False))
        self.btn_connect.setEnabled(True)

        # 显示错误对话框
        reply = QMessageBox.question(
            self, "连接失败",
            f"无法连接到 {addr}\n\n是否重试？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes  # 默认选中"是"
        )
        if reply == QMessageBox.StandardButton.Yes:
            # 用户选择重试，再次尝试连接
            self._on_connect_clicked()
        else:
            # 用户取消，记录日志
            self.signals.log_message.emit(f"连接失败，已取消：{addr}")

    def _on_disconnected(self):
        """
        连接断开后的处理
        
        功能：
        1. 更新 UI 显示断开状态
        2. 停止状态轮询定时器
        3. 重新启用连接按钮
        
        调用场景：
        - 用户手动点击"断开"按钮
        - 网络异常导致连接断开
        """
        self._update_connection_ui(False)  # 更新 UI 为断开状态
        self.btn_connect.setEnabled(True)  # 重新启用连接按钮
        self.signals.log_message.emit("连接断开")

        # 停止并清理轮询定时器
        if hasattr(self, 'poll_timer'):
            self.poll_timer.stop()
            self.poll_timer.deleteLater()
            del self.poll_timer
            self.signals.log_message.emit("停止状态轮询")

    def _update_connection_ui(self, connected):
        """
        更新连接相关的 UI 显示
        
        参数：
            connected: 连接状态（True=已连接，False=未连接）
        
        更新内容：
        1. 顶栏连接状态标签
        2. 系统状态面板中的网络连接状态
        3. 连接按钮的文字和颜色
        """
        if connected:
            # 已连接状态
            self.lbl_conn.setText("已连接")
            self.lbl_conn.setStyleSheet("color: #07C160; font-weight: bold;")  # 绿色
            self.status_labels["网络连接"].setText("已连接")
            self.status_labels["网络连接"].setStyleSheet(
                "color: #07C160; font-size: 16px; font-weight: bold;"
            )
            # 按钮变为"断开"，红色
            self.btn_connect.setText("断开")
            self.btn_connect.setStyleSheet(self._connect_btn_style(True))
        else:
            # 未连接状态
            self.lbl_conn.setText("未连接")
            self.lbl_conn.setStyleSheet("color: #fa5151; font-weight: bold;")  # 红色
            self.status_labels["网络连接"].setText("断开")
            self.status_labels["网络连接"].setStyleSheet(
                "color: #fa5151; font-size: 16px; font-weight: bold;"
            )
            # 按钮变为"连接设备"，绿色
            self.btn_connect.setText("连接设备")
            self.btn_connect.setStyleSheet(self._connect_btn_style(False))

    # ============ 状态更新显示 ============
    def _update_display(self, data):
        """
        更新状态显示
        
        参数：
            data: 设备状态字典，包含：
                - state: 电机状态 (0=停止, 1=正转, 2=反转, 3=自动)
                - speed: 舵机速度 (0-13)
                - pot: 电位器值 (0-100%)
                - sensor: 光纤传感器状态 (0=未检测, 1=检测到)
                - mode: 速度模式 (0=就地, 1=远程)
        
        优化：只更新变化的字段，避免不必要的 UI 刷新
        
        数据流向：
        1. 轮询定时器发送 'status' 命令
        2. ESP32 返回状态 JSON
        3. 解析后发射 status_update 信号
        4. 此槽函数被调用，更新 UI
        """
        old = self.current_data  # 保存旧数据，用于对比
        self.current_data = data  # 更新为新数据

        # 提取各个状态值
        state  = data.get("state", 0)
        speed  = data.get("speed", 0)
        pot    = data.get("pot", 0)
        sensor = data.get("sensor", 0)
        mode   = data.get("mode", 0)

        # 如果电机状态变化，更新电机模式和舵机模式
        if state != old.get("state"):
            # 获取状态文本和颜色（如："正转" 和 "#07C160"）
            motor_text, color = self.controller.get_state_text(state)
            self.status_labels["电机模式"].setText(motor_text)
            self.status_labels["电机模式"].setStyleSheet(
                f"color: {color}; font-size: 16px; font-weight: bold;"
            )
            # 获取方向文本（如："正转"、"反转"）
            dir_text = self.controller.get_direction_text(state)
            self.status_labels["舵机模式"].setText(dir_text)
            self.status_labels["舵机模式"].setStyleSheet(
                f"color: {color}; font-size: 16px; font-weight: bold;"
            )

        # 如果电位器值变化，更新显示
        if pot != old.get("pot"):
            self.status_labels["电位器"].setText(f"{pot} %")

        # 如果舵机速度变化，更新显示
        if speed != old.get("speed"):
            self.status_labels["舵机速度"].setText(f"{speed} / 13")
            # 如果是远程模式，同步更新滑块和输入框
            if self.speed_mode == 1:
                # 设置 slider_updating=True，防止触发 valueChanged 信号
                self.slider_updating = True
                self.speed_slider.setValue(speed)  # 更新滑块
                self.speed_entry.setText(str(speed))  # 更新输入框
                self.slider_updating = False

        # 如果传感器状态变化，更新显示
        if sensor != old.get("sensor"):
            sensor_text, sensor_color = self.controller.get_sensor_text(sensor)
            self.status_labels["光纤传感器"].setText(sensor_text)
            self.status_labels["光纤传感器"].setStyleSheet(
                f"color: {sensor_color}; font-size: 16px; font-weight: bold;"
            )

        # 如果速度模式变化，更新按钮和状态标签
        if mode != old.get("mode"):
            self.speed_mode = mode  # 更新内部速度模式
            # 获取模式按钮文本、状态文本和颜色
            btn_text, status_text, mode_color = self.controller.get_speed_mode_text(mode)
            self.btn_mode.setText(btn_text)
            self.btn_mode.setStyleSheet(
                "QPushButton{background:#07C160; color:#fff;"
                "border:none; border-radius:6px; padding:12px;"
                "font-weight:bold; font-size:16px;}"
                "QPushButton:hover{background:#06AD56;}"
            )
            self.status_labels["速度模式"].setText(status_text)
            self.status_labels["速度模式"].setStyleSheet(
                f"color: {mode_color}; font-size: 16px; font-weight: bold;"
            )

    # ============ 交互逻辑 ============
    def _toggle_mode(self):
        """
        切换速度模式：就地模式 ↔ 远程模式
        
        就地模式 (mode=0)：速度由 ESP32 上的电位器控制
        远程模式 (mode=1)：速度由 GUI 滑块控制
        
        发送的 MQTT 命令：
        - 就地模式：'mode local'
        - 远程模式：'mode remote'
        """
        # 检查连接状态
        if not self.client.is_connected():
            self.signals.log_message.emit("未连接，无法切换模式")
            return
        
        # 获取模式切换命令
        cmd = self.controller.get_mode_command(self.speed_mode)
        self._send_command(cmd)

    def _on_slider_change(self, value):
        """
        速度滑块值改变时的处理
        
        参数：
            value: 滑块的新值 (0-13)
        
        功能：
        1. 同步更新速度输入框
        2. 如果是远程模式，发送速度命令到 ESP32
        
        为什么需要 slider_updating 标志？
        - 程序更新滑块值时会触发 valueChanged 信号
        - 如果不跳过，会导致循环调用
        - 例如：程序设置值 → 触发信号 → 发送命令 → 更新值 → 触发信号...
        """
        # 如果是程序正在更新滑块，跳过此事件
        if self.slider_updating:
            return
        
        # 同步更新输入框显示
        self.speed_entry.setText(str(value))
        
        # 如果是远程模式，发送速度命令
        if self.speed_mode == 1:
            cmd = self.controller.get_speed_command(value)
            self._send_command(cmd)

    def _on_entry_change(self):
        """
        速度输入框回车时的处理
        
        功能：
        1. 获取输入框的值
        2. 验证并限制在有效范围内 (0-13)
        3. 同步更新滑块
        4. 如果是远程模式，发送速度命令
        
        clamp_speed() 方法：
        - 将字符串转换为整数
        - 限制在 0-13 范围内
        - 如果输入无效，返回当前速度值
        """
        try:
            # 获取并验证输入值
            val = self.controller.clamp_speed(self.speed_entry.text())
            
            # 防止循环触发
            self.slider_updating = True
            self.speed_slider.setValue(val)  # 同步更新滑块
            self.speed_entry.setText(str(val))  # 确保输入框显示正确值
            self.slider_updating = False
            
            # 如果是远程模式，发送速度命令
            if self.speed_mode == 1:
                cmd = self.controller.get_speed_command(val)
                self._send_command(cmd)
        except ValueError:
            # 输入无效，恢复为当前速度值
            self.speed_entry.setText(str(self.current_data.get("speed", 0)))

    def _send_command(self, cmd):
        """
        发送 MQTT 命令到 ESP32 设备
        
        参数：
            cmd: 要发送的命令字符串（如 "dj 1", "speed 5", "status"）
        
        功能：
        1. 在后台线程发送命令（避免阻塞 UI）
        2. 记录发送和接收的消息到日志
        3. 如果是 'status' 命令，解析响应并更新状态显示
        
        为什么使用后台线程？
        - MQTT 发送/接收可能阻塞（网络延迟）
        - 保持 UI 响应性
        """
        def do_send():
            # 记录发送的消息
            self.signals.log_message.emit(f"→ {cmd}")
            
            # 发送命令并获取响应
            response = self.client.send(cmd)
            
            # 如果没有响应，可能是连接断开
            if response is None:
                if not self.client.is_connected():
                    self.signals.disconnected.emit()  # 发射断开信号
                return
            
            # 记录接收的响应
            self.signals.log_message.emit(f"← {response}")
            
            # 如果是 status 命令，解析响应并更新状态
            if cmd.strip() == 'status':
                try:
                    import json
                    data = json.loads(response)  # 解析 JSON
                    self.signals.status_update.emit(data)  # 发射状态更新信号
                except Exception:
                    pass  # JSON 解析失败，忽略

        # 启动后台线程执行发送
        threading.Thread(target=do_send, daemon=True).start()

    def _on_send_command(self):
        """
        发送指令按钮点击或输入框回车时的处理
        
        功能：
        1. 获取输入框中的指令
        2. 验证指令非空且已连接
        3. 发送指令
        4. 清空输入框
        
        支持的指令格式：
        - 状态查询：status
        - 电机控制：dj 0/1/2/3
        - 速度控制：speed 0-13
        - 模式切换：mode local/remote
        - 抱闸控制：en 0/1
        """
        # 获取输入的指令
        cmd = self.cmd_entry.text().strip()
        if not cmd:
            return  # 空指令，忽略
        
        # 检查连接状态
        if not self.client.is_connected():
            self.signals.log_message.emit("未连接，无法发送指令")
            return
        
        # 发送指令
        self._send_command(cmd)
        
        # 清空输入框，准备下一次输入
        self.cmd_entry.clear()

    def _append_log(self, message):
        """
        向通信日志追加一条消息
        
        参数：
            message: 日志消息内容
        
        功能：
        1. 添加时间戳前缀
        2. 追加到日志文本框
        3. 自动滚动到底部（显示最新消息）
        
        日志格式：[HH:MM:SS] 消息内容
        """
        # 获取当前时间作为时间戳
        now = datetime.now().strftime("%H:%M:%S")
        
        # 追加日志消息
        self.log_text.append(f"[{now}] {message}")
        
        # 自动滚动到底部，显示最新消息
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())  # 滚动到最大值

    # ============ BasePage 生命周期 ============
    def on_show(self):
        """
        页面显示时调用（BasePage 接口方法）
        
        调用场景：
        - 从其他页面切换到此页面
        - 应用程序启动时
        
        功能：
        - 启动时钟定时器，更新时间显示
        """
        self.clock_timer.start(1000)

    def on_hide(self):
        """
        页面隐藏时调用（BasePage 接口方法）
        
        调用场景：
        - 切换到其他页面
        - 应用程序退出时
        
        功能：
        - 停止时钟定时器
        - 停止状态轮询（但不断开 MQTT 连接，连接是全局共用的）
        
        注意：MQTT 连接是全局的，切换页面时不断开
        """
        self.clock_timer.stop()  # 停止时钟更新
        # 停止并清理轮询定时器
        if hasattr(self, 'poll_timer'):
            self.poll_timer.stop()
            self.poll_timer.deleteLater()
            del self.poll_timer

    def save_state(self):
        """
        保存页面状态（BasePage 接口方法）
        
        功能：预留接口，当前未实现
        用途：可用于保存用户设置（如窗口大小、最后连接的地址等）
        """
        pass