"""
ESP32 控制中心 - 主窗口界面
============================
负责所有界面相关的逻辑，包括：
- 界面布局和控件创建
- 按钮/滑块等用户交互事件
- 界面显示更新
- 日志输出

这一层不关心业务规则怎么判断（问 Controller），
也不关心数据怎么收发（问 ESP32Client）。
修改界面外观、布局时，只需要改这个文件。

快捷键：Esc 退出
"""
from core.esp32_client import ESP32Client
from core.controller import Controller
from datetime import datetime
import threading

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QSlider, QLineEdit, QTextEdit,
    QFrame, QGroupBox, QMessageBox, QSplitter
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QFont, QColor, QPalette

# 状态轮询间隔（毫秒）
POLL_INTERVAL = 500


# ============================================================
#  信号桥接（线程安全地从后台线程发送信号到 UI 线程）
# ============================================================
class WorkerSignals(QObject):
    """
    Qt 信号集合
    用于后台线程与界面线程之间的安全通信。
    PyQt5 的界面只能在主线程更新，后台线程需要通过信号通知主线程。
    """
    connected = pyqtSignal()       # 连接成功信号
    disconnected = pyqtSignal()    # 断开连接信号
    connect_failed = pyqtSignal(str)  # 连接失败信号，携带错误信息
    status_update = pyqtSignal(dict)  # 状态更新信号，携带数据字典
    log_message = pyqtSignal(str)     # 日志消息信号


# ============================================================
#  主窗口
# ============================================================
class ESP32MainWindow(QMainWindow):
    """
    ESP32 控制中心主窗口
    界面布局：顶栏（标题+IP+连接按钮+时钟+状态）
             中间左侧（系统状态面板）
             中间右侧（速度控制+设备控制）
             底部（指令输入+通信日志）
    """

    def __init__(self):
        super().__init__()

        # 初始化通信层和逻辑层
        self.client = ESP32Client()     # 通信层：负责收发数据
        self.controller = Controller()  # 逻辑层：负责业务判断
        self.signals = WorkerSignals()  # 信号桥接：线程间通信

        # 当前缓存的数据（用于对比变化，避免重复刷新界面）
        self.current_data = {
            "state": 0, "speed": 0, "pot": 0, "sensor": 0, "mode": 0
        }

        # 速度模式：0 = 就地（电位器），1 = 远程（滑块）
        self.speed_mode = 0

        # 防止滑块联动时触发发送命令（程序设值时设为 True）
        self.slider_updating = False

        # 设置窗口标题和样式
        self.setWindowTitle("ESP32 控制中心")
        from styles import STYLE
        self.setStyleSheet(STYLE)

        # 创建界面
        self._create_ui()

        # 绑定信号到槽函数（后台线程触发信号 → 主线程执行槽函数）
        self.signals.connected.connect(self._on_connected)
        self.signals.disconnected.connect(self._on_disconnected)
        self.signals.connect_failed.connect(self._on_connect_failed)
        self.signals.status_update.connect(self._update_display)
        self.signals.log_message.connect(self._append_log)

    # ============ 创建界面 ============
    def _create_ui(self):
        """
        创建整个界面布局
        结构：header（顶栏） → horizontal_splitter（左状态+右控制）
              → vertical_splitter（上半+下半：指令+日志）
        """
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # ---------- 顶栏：标题 + IP输入 + 连接按钮 + 时钟 + 状态 ----------
        header = QFrame()
        header.setStyleSheet(
            "background-color: #ffffff; border-bottom: 1px solid #e0e0e0; border-radius: 0px;"
        )
        header.setFixedHeight(65)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(15, 8, 15, 8)

        # 标题
        title = QLabel("ESP32 控制中心")
        title.setFont(QFont("Microsoft YaHei", 18, QFont.Bold))
        title.setStyleSheet("color: #333333; border: none;")
        header_layout.addWidget(title)
        header_layout.addSpacing(30)

        # IP 输入框
        ip_label = QLabel("IP:")
        ip_label.setStyleSheet("color: #333333; border: none; font-size: 15px; font-weight: bold;")
        header_layout.addWidget(ip_label)

        from core.esp32_client import DEFAULT_BROKER, DEFAULT_PORT
        self.broker_entry = QLineEdit(DEFAULT_BROKER)
        self.broker_entry.setFixedWidth(160)
        self.broker_entry.setStyleSheet(
            "QLineEdit{background:#f5f5f5; border:1px solid #e0e0e0; border-radius:6px;"
            "padding:6px 10px; font-size:15px;}"
            "QLineEdit:focus{border-color:#07C160;}"
        )
        header_layout.addWidget(self.broker_entry)

        # 端口输入框
        port_label = QLabel("端口:")
        port_label.setStyleSheet("color: #333333; border: none; font-size: 15px; font-weight: bold;")
        header_layout.addWidget(port_label)

        self.port_entry = QLineEdit(str(DEFAULT_PORT))
        self.port_entry.setFixedWidth(80)
        self.port_entry.setStyleSheet(
            "QLineEdit{background:#f5f5f5; border:1px solid #e0e0e0; border-radius:6px;"
            "padding:6px 10px; font-size:15px;}"
            "QLineEdit:focus{border-color:#07C160;}"
        )
        header_layout.addWidget(self.port_entry)

        # 连接按钮
        self.btn_connect = QPushButton("连接设备")
        self.btn_connect.setFixedWidth(100)
        self.btn_connect.setStyleSheet(
            "QPushButton{background:#07C160; color:#ffffff; border:none;"
            "border-radius:6px; padding:8px 16px; font-weight:bold; font-size:15px;}"
            "QPushButton:hover{background:#06AD56;}"
        )
        self.btn_connect.clicked.connect(self._on_connect_clicked)
        header_layout.addWidget(self.btn_connect)

        header_layout.addStretch()

        # 右上角时钟
        self.lbl_clock = QLabel("00:00:00")
        self.lbl_clock.setFont(QFont("Microsoft YaHei", 15))
        self.lbl_clock.setStyleSheet("color: #999999; border: none;")
        header_layout.addWidget(self.lbl_clock)
        header_layout.addSpacing(15)

        # 右上角连接状态
        self.lbl_conn = QLabel("未连接")
        self.lbl_conn.setFont(QFont("Microsoft YaHei", 15))
        self.lbl_conn.setStyleSheet("color: #fa5151; border: none; font-weight: bold;")
        header_layout.addWidget(self.lbl_conn)

        # 时钟定时器：每秒更新一次
        self.clock_timer = QTimer()
        self.clock_timer.timeout.connect(self._update_clock)
        self.clock_timer.start(1000)
        self._update_clock()

        main_layout.addWidget(header)

        # ---------- 中间区域：左侧状态 + 右侧控制（可拖拽调整宽度） ----------
        horizontal_splitter = QSplitter(Qt.Horizontal)
        horizontal_splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: #e0e0e0;
            }
            QSplitter::handle:hover {
                background-color: #07C160;
            }
        """)
        horizontal_splitter.setHandleWidth(6)

        # ---- 左侧：系统状态面板 ----
        status_group = QGroupBox("系统状态")
        status_layout = QVBoxLayout(status_group)
        status_layout.setContentsMargins(8, 10, 8, 8)
        status_layout.setSpacing(0)

        # 状态行定义：(行号, 名称, 默认值, 颜色)
        status_items = [
            (0, "电机模式", "停止",   "#333333"),
            (1, "舵机模式", "—",      "#333333"),
            (2, "电位器",   "0 %",    "#333333"),
            (3, "舵机速度", "0 / 13", "#333333"),
            (4, "光纤传感器", "未检测", "#333333"),
            (5, "速度模式", "就地",    "#07C160"),
            (6, "网络连接", "断开",    "#fa5151"),
        ]

        # 用字典保存所有状态标签的引用，方便后续更新
        self.status_labels = {}

        for i, (row, name, default, color) in enumerate(status_items):
            # 交替背景色（斑马纹效果）
            row_container = QWidget()
            bg = "#f8f8f8" if i % 2 == 0 else "#ffffff"
            row_container.setObjectName(f"row{i}")
            row_container.setStyleSheet(
                f"QWidget#row{i}{{background:{bg}; border:none; border-radius:0px;}}"
            )

            row_layout = QHBoxLayout(row_container)
            row_layout.setContentsMargins(12, 0, 12, 0)
            row_layout.setAlignment(Qt.AlignCenter)

            # 状态名称标签（左侧）
            lbl_name = QLabel(name)
            lbl_name.setStyleSheet(
                "color: #333333; font-size: 16px; font-weight: bold; background:transparent;"
            )
            lbl_name.setFixedWidth(110)
            lbl_name.setAlignment(Qt.AlignCenter)
            row_layout.addWidget(lbl_name)

            # 状态值标签（右侧，后续会动态更新）
            lbl_value = QLabel(default)
            lbl_value.setStyleSheet(
                f"color: {color}; font-size: 16px; font-weight: bold; background:transparent;"
            )
            lbl_value.setAlignment(Qt.AlignCenter)
            row_layout.addWidget(lbl_value, stretch=1)

            # 保存引用，用名称作为键
            self.status_labels[name] = lbl_value
            status_layout.addWidget(row_container, stretch=1)

        horizontal_splitter.addWidget(status_group)

        # ---- 右侧：控制面板（速度控制 + 设备控制） ----
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # -- 速度控制区域 --
        speed_group = QGroupBox("速度控制")
        speed_layout = QVBoxLayout(speed_group)
        speed_layout.setAlignment(Qt.AlignVCenter)

        slider_layout = QHBoxLayout()
        slider_layout.setAlignment(Qt.AlignVCenter)

        # 速度滑块（0~13）
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(0, 13)
        self.speed_slider.setValue(0)
        self.speed_slider.valueChanged.connect(self._on_slider_change)
        self.speed_slider.setMinimumHeight(30)
        slider_layout.addWidget(self.speed_slider, stretch=1)

        # 速度输入框（可直接输入数字）
        self.speed_entry = QLineEdit("0")
        self.speed_entry.setFixedWidth(55)
        self.speed_entry.setAlignment(Qt.AlignCenter)
        self.speed_entry.returnPressed.connect(self._on_entry_change)
        slider_layout.addWidget(self.speed_entry)

        speed_layout.addLayout(slider_layout)
        right_layout.addWidget(speed_group, stretch=0)

        # -- 设备控制区域 --
        ctrl_group = QGroupBox("设备控制")
        ctrl_grid = QGridLayout(ctrl_group)

        # 按钮定义：(行, 列, 文字, 背景色, 前景色, 悬停色, 对应命令)
        buttons = [
            (0, 0, "正转",   "#576B95", "#ffffff", "#4A5D84", "dj 1"),
            (0, 1, "反转",   "#576B95", "#ffffff", "#4A5D84", "dj 2"),
            (1, 0, "停止",   "#fa5151", "#ffffff", "#E04444", "dj 0"),
            (1, 1, "自动",   "#FFC300", "#333333", "#E6B000", "dj 3"),
            (2, 0, "解抱闸", "#E67E22", "#ffffff", "#D35400", "en 0"),
            (2, 1, "上抱闸", "#E67E22", "#ffffff", "#D35400", "en 1"),
        ]

        for row, col, text, bg, fg, hover, cmd in buttons:
            btn = QPushButton(text)
            btn.setStyleSheet(
                f"QPushButton{{background:{bg}; color:{fg}; border:none;"
                f"border-radius:6px; padding:12px; font-weight:bold; font-size:15px;}}"
                f"QPushButton:hover{{background:{hover};}}"
            )
            btn.setMinimumHeight(48)
            btn.clicked.connect(lambda checked, c=cmd: self._send_command(c))
            ctrl_grid.addWidget(btn, row, col)

        # -- 速度模式切换按钮 --
        self.btn_mode = QPushButton("就地模式")
        self.btn_mode.setStyleSheet(
            "QPushButton{background:#07C160; color:#ffffff;"
            "border:none; border-radius:6px; padding:12px;"
            "font-weight:bold; font-size:16px;}"
            "QPushButton:hover{background:#06AD56;}"
        )
        self.btn_mode.setMinimumHeight(52)
        self.btn_mode.clicked.connect(self._toggle_mode)
        ctrl_grid.addWidget(self.btn_mode, 3, 0, 1, 2)

        right_layout.addWidget(ctrl_group, stretch=3)

        horizontal_splitter.addWidget(right_widget)
        horizontal_splitter.setSizes([400, 400])

        # ---------- 底部：指令输入 + 日志（可上下拖拽调整高度） ----------
        # -- 指令输入框 --
        cmd_frame = QFrame()
        cmd_frame.setStyleSheet(
            "background-color: #ffffff;"
            "border: 1px solid #e0e0e0; border-radius: 8px;"
        )
        cmd_layout = QHBoxLayout(cmd_frame)
        cmd_layout.setContentsMargins(10, 10, 10, 10)
        cmd_layout.setAlignment(Qt.AlignVCenter)

        prompt = QLabel(">")
        prompt.setStyleSheet("color: #07C160; font-size: 18px; font-weight: bold; border: none;")
        cmd_layout.addWidget(prompt)

        self.cmd_entry = QLineEdit()
        self.cmd_entry.setPlaceholderText("输入指令，回车发送...")
        self.cmd_entry.returnPressed.connect(self._on_send_command)
        cmd_layout.addWidget(self.cmd_entry, stretch=1)

        btn_send = QPushButton("发送")
        btn_send.setFixedWidth(80)
        btn_send.setStyleSheet(
            "QPushButton{background:#07C160; color:#ffffff; border:none;"
            "border-radius:6px; padding:10px; font-weight:bold; font-size:15px;}"
            "QPushButton:hover{background:#06AD56;}"
        )
        btn_send.clicked.connect(self._on_send_command)
        cmd_layout.addWidget(btn_send)

        # -- 日志区域 --
        log_group = QGroupBox("通信日志")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Microsoft YaHei", 13))
        log_layout.addWidget(self.log_text)

        # 纵向分割条：上半部分（状态+控制） vs 下半部分（指令+日志）
        vertical_splitter = QSplitter(Qt.Vertical)
        vertical_splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: #e0e0e0;
                height: 3px;
            }
            QSplitter::handle:hover {
                background-color: #07C160;
            }
        """)
        vertical_splitter.setHandleWidth(6)

        # 上半部分：水平分割条（左状态+右控制）
        vertical_splitter.addWidget(horizontal_splitter)

        # 下半部分：指令输入 + 日志
        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(8)
        bottom_layout.addWidget(cmd_frame)
        bottom_layout.addWidget(log_group, stretch=1)

        vertical_splitter.addWidget(bottom_widget)
        vertical_splitter.setSizes([500, 200])

        main_layout.addWidget(vertical_splitter, stretch=1)

    # ============================================================
    #  时钟更新
    # ============================================================
    def _update_clock(self):
        """每秒更新右上角时钟显示"""
        now = datetime.now().strftime("%H:%M:%S")
        self.lbl_clock.setText(now)

    # ============================================================
    #  连接相关（用户点击连接按钮后的完整流程）
    # ============================================================
    def _on_connect_clicked(self):
        """
        点击连接按钮的处理逻辑：
        - 如果已连接 → 断开连接
        - 如果未连接 → 在后台线程尝试连接（避免卡界面）
        """
        # 已连接状态：点击则断开
        if self.client.is_connected():
            self.client.disconnect()
            self._update_connection_ui(False)
            self.signals.log_message.emit("手动断开连接")
            return

        # 未连接状态：尝试连接
        broker = self.broker_entry.text().strip()
        port_str = self.port_entry.text().strip()
        if not broker:
            QMessageBox.warning(self, "提示", "请输入Broker地址")
            return

        # 禁用按钮，防止重复点击
        self.btn_connect.setEnabled(False)
        self.btn_connect.setText("连接中...")

        # 在后台线程执行网络连接（耗时操作不能放在主线程）
        def do_connect():
            try:
                self.signals.log_message.emit(f"正在连接 {broker}:{port_str} ...")
                result = self.client.connect(
                    broker=broker,
                    port=int(port_str) if port_str else None
                )
                if result:
                    self.signals.connected.emit()     # 成功 → 发信号
                else:
                    self.signals.connect_failed.emit(f"{broker}:{port_str}")  # 失败 → 发信号
            except Exception as e:
                self.signals.connect_failed.emit(f"{broker}:{port_str}")

        threading.Thread(target=do_connect, daemon=True).start()

    def _on_connected(self):
        """连接成功的界面更新"""
        self._update_connection_ui(True)
        self.btn_connect.setEnabled(True)
        self.signals.log_message.emit("Connected!")

        # 启动状态轮询定时器 (2s 间隔)
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(lambda: self._send_command('status'))
        self.poll_timer.start(POLL_INTERVAL)
        self.signals.log_message.emit(f"启动状态轮询 ({POLL_INTERVAL}ms间隔)")

    def _on_connect_failed(self, addr):
        """连接失败：弹窗询问是否重试"""
        # 清理可能残留的轮询定时器
        if hasattr(self, 'poll_timer'):
            self.poll_timer.stop()
            self.poll_timer.deleteLater()
            del self.poll_timer

        self.btn_connect.setText("连接设备")
        self.btn_connect.setStyleSheet(
            "QPushButton{background:#07C160; color:#ffffff; border:none;"
            "border-radius:6px; padding:8px 16px; font-weight:bold; font-size:15px;}"
            "QPushButton:hover{background:#06AD56;}"
        )
        self.btn_connect.setEnabled(True)

        reply = QMessageBox.question(
            self, "连接失败",
            f"无法连接到 {addr}\n\n是否重试？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes
        )
        if reply == QMessageBox.Yes:
            self._on_connect_clicked()
        else:
            self.signals.log_message.emit(f"连接失败，已取消：{addr}")

    def _on_disconnected(self):
        """连接断开的界面更新"""
        self._update_connection_ui(False)
        self.btn_connect.setEnabled(True)
        self.signals.log_message.emit("连接断开")

        # 停止状态轮询定时器
        if hasattr(self, 'poll_timer'):
            self.poll_timer.stop()
            self.poll_timer.deleteLater()
            del self.poll_timer
            self.signals.log_message.emit("停止状态轮询")

    def _update_connection_ui(self, connected):
        """
        统一更新所有与连接状态相关的界面元素

        参数:
            connected: True 表示已连接，False 表示未连接
        """
        if connected:
            # 已连接：绿色状态
            self.lbl_conn.setText("已连接")
            self.lbl_conn.setStyleSheet("color: #07C160; border: none; font-weight: bold;")
            self.status_labels["网络连接"].setText("已连接")
            self.status_labels["网络连接"].setStyleSheet(
                "color: #07C160; font-size: 16px; font-weight: bold; background:transparent;"
            )
            self.btn_connect.setText("断开")
            self.btn_connect.setStyleSheet(
                "QPushButton{background:#fa5151; color:#ffffff; border:none;"
                "border-radius:6px; padding:8px 16px; font-weight:bold; font-size:15px;}"
                "QPushButton:hover{background:#E04444;}"
            )
        else:
            # 未连接：红色状态
            self.lbl_conn.setText("未连接")
            self.lbl_conn.setStyleSheet("color: #fa5151; border: none; font-weight: bold;")
            self.status_labels["网络连接"].setText("断开")
            self.status_labels["网络连接"].setStyleSheet(
                "color: #fa5151; font-size: 16px; font-weight: bold;"
            )
            self.btn_connect.setText("连接设备")
            self.btn_connect.setStyleSheet(
                "QPushButton{background:#07C160; color:#ffffff; border:none;"
                "border-radius:6px; padding:8px 16px; font-weight:bold; font-size:15px;}"
                "QPushButton:hover{background:#06AD56;}"
            )

    # ============================================================
    #  状态更新显示（接收数据后更新界面）
    # ============================================================
    def _update_display(self, data):
        """
        接收到新的状态数据后，对比旧数据，只更新变化的部分。
        通过 WorkerSignals.status_update 信号在主线程中调用。

        参数:
            data: 字典，包含 state/speed/pot/sensor/mode
        """
        old = self.current_data
        self.current_data = data

        state  = data.get("state", 0)
        speed  = data.get("speed", 0)
        pot    = data.get("pot", 0)
        sensor = data.get("sensor", 0)
        mode   = data.get("mode", 0)

        # --- 电机状态变化 → 更新电机模式和舵机模式 ---
        if state != old.get("state"):
            motor_text, color = self.controller.get_state_text(state)
            self.status_labels["电机模式"].setText(motor_text)
            self.status_labels["电机模式"].setStyleSheet(
                f"color: {color}; font-size: 16px; font-weight: bold; background:transparent;"
            )
            dir_text = self.controller.get_direction_text(state)
            self.status_labels["舵机模式"].setText(dir_text)
            self.status_labels["舵机模式"].setStyleSheet(
                f"color: {color}; font-size: 16px; font-weight: bold; background:transparent;"
            )

        # --- 电位器变化 ---
        if pot != old.get("pot"):
            self.status_labels["电位器"].setText(f"{pot} %")

        # --- 速度变化 ---
        if speed != old.get("speed"):
            self.status_labels["舵机速度"].setText(f"{speed} / 13")
            # 远程模式下，滑块跟随 ESP32 的实际速度同步
            if self.speed_mode == 1:
                self.slider_updating = True
                self.speed_slider.setValue(speed)
                self.speed_entry.setText(str(speed))
                self.slider_updating = False

        # --- 传感器变化 ---
        if sensor != old.get("sensor"):
            sensor_text, sensor_color = self.controller.get_sensor_text(sensor)
            self.status_labels["光纤传感器"].setText(sensor_text)
            self.status_labels["光纤传感器"].setStyleSheet(
                f"color: {sensor_color}; font-size: 16px; font-weight: bold; background:transparent;"
            )

        # --- 速度模式变化 ---
        if mode != old.get("mode"):
            self.speed_mode = mode
            btn_text, status_text, mode_color = self.controller.get_speed_mode_text(mode)
            self.btn_mode.setText(btn_text)
            self.btn_mode.setStyleSheet(
                "QPushButton{background:#07C160; color:#ffffff;"
                "border:none; border-radius:6px; padding:12px;"
                "font-weight:bold; font-size:16px;}"
                "QPushButton:hover{background:#06AD56;}"
            )
            self.status_labels["速度模式"].setText(status_text)
            self.status_labels["速度模式"].setStyleSheet(
                f"color: {mode_color}; font-size: 16px; font-weight: bold; background:transparent;"
            )

    # ============================================================
    #  模式切换
    # ============================================================
    def _toggle_mode(self):
        """
        点击模式切换按钮：
        就地模式(0) → 发送 "mode remote" → 切到远程
        远程模式(1) → 发送 "mode local"  → 切到就地
        """
        if not self.client.is_connected():
            self.signals.log_message.emit("未连接，无法切换模式")
            return
        cmd = self.controller.get_mode_command(self.speed_mode)
        self._send_command(cmd)

    # ============================================================
    #  速度滑块 & 输入框
    # ============================================================
    def _on_slider_change(self, value):
        """
        滑块值变化时：
        1. 同步更新输入框显示
        2. 如果是远程模式，发送速度命令给 ESP32
        """
        if self.slider_updating:
            return  # 程序设值时不触发发送，避免循环
        self.speed_entry.setText(str(value))
        if self.speed_mode == 1:
            cmd = self.controller.get_speed_command(value)
            self._send_command(cmd)

    def _on_entry_change(self):
        """
        输入框按回车时：
        1. 校验并限制输入值到合法范围 (0~13)
        2. 同步更新滑块位置
        3. 如果是远程模式，发送速度命令
        """
        try:
            val = self.controller.clamp_speed(self.speed_entry.text())
            # 同步滑块（临时屏蔽滑块事件）
            self.slider_updating = True
            self.speed_slider.setValue(val)
            self.speed_entry.setText(str(val))
            self.slider_updating = False
            # 远程模式下发送命令
            if self.speed_mode == 1:
                cmd = self.controller.get_speed_command(val)
                self._send_command(cmd)
        except ValueError:
            # 输入非法字符，恢复为当前速度值
            self.speed_entry.setText(str(self.current_data.get("speed", 0)))

    # ============================================================
    #  命令发送（通用方法，所有按钮和指令都走这里）
    # ============================================================
    def _send_command(self, cmd):
        """
        在后台线程发送命令并接收响应。

        参数:
            cmd: 命令字符串，例如 "dj 1"、"speed 5"、"mode remote"
        """
        def do_send():
            self.signals.log_message.emit(f"→ {cmd}")
            response = self.client.send_cmd(cmd)
            if response is None:
             # 没有响应：可能是没连上，也可能是连接断了
                if not self.client.is_connected():
                    # 连接确实断了
                    self.signals.disconnected.emit()
                # 如果还连着但没回复，就不做任何处理（正常情况）
                return

            # 收到了响应
            self.signals.log_message.emit(f"← {response}")

            # 若是 status 查询，解析 JSON 并发射状态更新信号（供 UI 刷新）
            if cmd.strip() == 'status':
                try:
                    import json
                    data = json.loads(response)
                    self.signals.status_update.emit(data)
                except Exception:
                    pass

        threading.Thread(target=do_send, daemon=True).start()

    # ============================================================
    #  手动指令输入
    # ============================================================
    def _on_send_command(self):
        """底部指令输入框按回车或点发送按钮"""
        cmd = self.cmd_entry.text().strip()
        if not cmd:
            return
        if not self.client.is_connected():
            self.signals.log_message.emit("未连接，无法发送指令")
            return
        self._send_command(cmd)
        self.cmd_entry.clear()  # 清空输入框

    # ============================================================
    #  日志输出
    # ============================================================
    def _append_log(self, message):
        """
        向通信日志区域追加一条消息（带时间戳）。
        通过 WorkerSignals.log_message 信号在主线程中调用。
        """
        now = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{now}] {message}")
        # 自动滚动到底部，显示最新日志
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # ============================================================
    #  快捷键
    # ============================================================
    def keyPressEvent(self, event):
        """Esc 键关闭窗口"""
        if event.key() == Qt.Key_Escape:
            self.close()