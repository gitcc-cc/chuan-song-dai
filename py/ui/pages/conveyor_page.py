"""
传送带控制页面
==============
从原 ESP32MainWindow 迁移而来，功能零改动，仅页面化。
"""
from datetime import datetime
import threading

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QSlider, QLineEdit, QTextEdit, QFrame, QGroupBox, QMessageBox, QSplitter
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QColor, QPalette

from core.esp32_client import ESP32Client
from core.controller import Controller
from ui.pages.base_page import BasePage
from styles import STYLE


# 状态轮询间隔（毫秒）
POLL_INTERVAL = 2000


class WorkerSignals(QObject):
    """Qt 信号集合：后台线程 -> 主线程"""
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    connect_failed = pyqtSignal(str)
    status_update = pyqtSignal(dict)
    log_message = pyqtSignal(str)


class ConveyorPage(BasePage):
    """传送带控制页面"""

    def __init__(self, parent=None):
        super().__init__(parent)

        # 通信层 & 逻辑层
        self.client = ESP32Client()
        self.controller = Controller()
        self.signals = WorkerSignals()

        # 当前缓存数据
        self.current_data = {
            "state": 0, "speed": 0, "pot": 0, "sensor": 0, "mode": 0
        }

        # 速度模式：0=就地(电位器)，1=远程(滑块)
        self.speed_mode = 0
        self.slider_updating = False

        # 页面标题
        self.setWindowTitle("传送带控制")
        self.setStyleSheet(STYLE)

        # 创建界面
        self._create_ui()

        # 绑定信号
        self.signals.connected.connect(self._on_connected)
        self.signals.disconnected.connect(self._on_disconnected)
        self.signals.connect_failed.connect(self._on_connect_failed)
        self.signals.status_update.connect(self._update_display)
        self.signals.log_message.connect(self._append_log)

    # ============ 创建界面 ============
    def _create_ui(self):
        """创建完整界面布局"""
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # 顶栏
        header = self._create_header()
        main_layout.addWidget(header)

        # 中间区域：左右分割
        horizontal_splitter = QSplitter(Qt.Orientation.Horizontal)
        horizontal_splitter.setHandleWidth(6)
        horizontal_splitter.setStyleSheet("""
            QSplitter::handle { background-color: #e0e0e0; }
            QSplitter::handle:hover { background-color: #07C160; }
        """)

        # 左侧：系统状态
        status_group = self._create_status_panel()
        horizontal_splitter.addWidget(status_group)

        # 右侧：控制面板
        right_widget = self._create_control_panel()
        horizontal_splitter.addWidget(right_widget)

        horizontal_splitter.setSizes([400, 400])
        main_layout.addWidget(horizontal_splitter, stretch=1)

        # 底部：指令输入 + 日志
        bottom_widget = self._create_bottom_panel()
        vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        vertical_splitter.setHandleWidth(6)
        vertical_splitter.setStyleSheet("""
            QSplitter::handle { background-color: #e0e0e0; height: 3px; }
            QSplitter::handle:hover { background-color: #07C160; }
        """)
        vertical_splitter.addWidget(horizontal_splitter)
        vertical_splitter.addWidget(bottom_widget)
        vertical_splitter.setSizes([500, 200])
        main_layout.addWidget(vertical_splitter, stretch=1)

    def _create_header(self):
        """创建顶栏"""
        header = QFrame()
        header.setFixedHeight(65)
        header.setStyleSheet(
            "background-color: #ffffff; border-bottom: 1px solid #e0e0e0;"
        )
        layout = QHBoxLayout(header)
        layout.setContentsMargins(15, 8, 15, 8)

        # 标题
        title = QLabel("ESP32 控制中心")
        title.setFont(QFont("Microsoft YaHei", 18, QFont.Weight.Bold))
        title.setStyleSheet("color: #333333; border: none;")
        layout.addWidget(title)
        layout.addSpacing(30)

        # IP 输入
        from core.esp32_client import DEFAULT_BROKER, DEFAULT_PORT
        ip_label = QLabel("IP:")
        ip_label.setStyleSheet("color: #333333; font-size: 15px; font-weight: bold;")
        layout.addWidget(ip_label)

        self.broker_entry = QLineEdit(DEFAULT_BROKER)
        self.broker_entry.setFixedWidth(160)
        self.broker_entry.setStyleSheet(self._input_style())
        layout.addWidget(self.broker_entry)

        # 端口输入
        port_label = QLabel("端口:")
        port_label.setStyleSheet("color: #333333; font-size: 15px; font-weight: bold;")
        layout.addWidget(port_label)

        self.port_entry = QLineEdit(str(DEFAULT_PORT))
        self.port_entry.setFixedWidth(80)
        self.port_entry.setStyleSheet(self._input_style())
        layout.addWidget(self.port_entry)

        # 连接按钮
        self.btn_connect = QPushButton("连接设备")
        self.btn_connect.setFixedWidth(100)
        self.btn_connect.setStyleSheet(self._connect_btn_style(False))
        self.btn_connect.clicked.connect(self._on_connect_clicked)
        layout.addWidget(self.btn_connect)

        layout.addStretch()

        # 时钟
        self.lbl_clock = QLabel("00:00:00")
        self.lbl_clock.setFont(QFont("Microsoft YaHei", 15))
        self.lbl_clock.setStyleSheet("color: #999999;")
        layout.addWidget(self.lbl_clock)
        layout.addSpacing(15)

        # 连接状态
        self.lbl_conn = QLabel("未连接")
        self.lbl_conn.setFont(QFont("Microsoft YaHei", 15))
        self.lbl_conn.setStyleSheet("color: #fa5151; font-weight: bold;")
        layout.addWidget(self.lbl_conn)

        # 时钟定时器
        self.clock_timer = QTimer()
        self.clock_timer.timeout.connect(self._update_clock)
        self.clock_timer.start(1000)
        self._update_clock()

        return header

    def _input_style(self):
        return ("QLineEdit{background:#f5f5f5; border:1px solid #e0e0e0; "
                "border-radius:6px; padding:6px 10px; font-size:15px;}"
                "QLineEdit:focus{border-color:#07C160;}")

    def _connect_btn_style(self, connected: bool):
        if connected:
            return ("QPushButton{background:#fa5151; color:#fff; border:none;"
                    "border-radius:6px; padding:8px 16px; font-weight:bold; font-size:15px;}"
                    "QPushButton:hover{background:#E04444;}")
        return ("QPushButton{background:#07C160; color:#fff; border:none;"
                "border-radius:6px; padding:8px 16px; font-weight:bold; font-size:15px;}"
                "QPushButton:hover{background:#06AD56;}")

    def _create_status_panel(self):
        """创建系统状态面板"""
        group = QGroupBox("系统状态")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(8, 10, 8, 8)
        layout.setSpacing(0)

        status_items = [
            (0, "电机模式", "停止",   "#333333"),
            (1, "舵机模式", "—",      "#333333"),
            (2, "电位器",   "0 %",    "#333333"),
            (3, "舵机速度", "0 / 13", "#333333"),
            (4, "光纤传感器", "未检测", "#333333"),
            (5, "速度模式", "就地",    "#07C160"),
            (6, "网络连接", "断开",    "#fa5151"),
        ]

        self.status_labels = {}

        for i, (row, name, default, color) in enumerate(status_items):
            row_widget = QWidget()
            bg = "#f8f8f8" if i % 2 == 0 else "#ffffff"
            row_widget.setObjectName(f"row{i}")
            row_widget.setStyleSheet(f"QWidget#row{i}{{background:{bg}; border:none;}}")

            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(12, 0, 12, 0)
            row_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

            lbl_name = QLabel(name)
            lbl_name.setStyleSheet(
                "color: #333333; font-size: 16px; font-weight: bold; background:transparent;"
            )
            lbl_name.setFixedWidth(110)
            lbl_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row_layout.addWidget(lbl_name)

            lbl_value = QLabel(default)
            lbl_value.setStyleSheet(
                f"color: {color}; font-size: 16px; font-weight: bold; background:transparent;"
            )
            lbl_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row_layout.addWidget(lbl_value, stretch=1)

            self.status_labels[name] = lbl_value
            layout.addWidget(row_widget, stretch=1)

        return group

    def _create_control_panel(self):
        """创建右侧控制面板"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 速度控制
        speed_group = QGroupBox("速度控制")
        speed_layout = QVBoxLayout(speed_group)
        speed_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        slider_layout = QHBoxLayout()
        slider_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(0, 13)
        self.speed_slider.setValue(0)
        self.speed_slider.valueChanged.connect(self._on_slider_change)
        self.speed_slider.setMinimumHeight(30)
        slider_layout.addWidget(self.speed_slider, stretch=1)

        self.speed_entry = QLineEdit("0")
        self.speed_entry.setFixedWidth(55)
        self.speed_entry.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.speed_entry.returnPressed.connect(self._on_entry_change)
        slider_layout.addWidget(self.speed_entry)

        speed_layout.addLayout(slider_layout)
        layout.addWidget(speed_group, stretch=0)

        # 设备控制
        ctrl_group = QGroupBox("设备控制")
        ctrl_grid = QGridLayout(ctrl_group)

        buttons = [
            (0, 0, "正转",   "#576B95", "#fff", "#4A5D84", "dj 1"),
            (0, 1, "反转",   "#576B95", "#fff", "#4A5D84", "dj 2"),
            (1, 0, "停止",   "#fa5151", "#fff", "#E04444", "dj 0"),
            (1, 1, "自动",   "#FFC300", "#333", "#E6B000", "dj 3"),
            (2, 0, "解抱闸", "#E67E22", "#fff", "#D35400", "en 0"),
            (2, 1, "上抱闸", "#E67E22", "#fff", "#D35400", "en 1"),
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

        # 模式切换按钮
        self.btn_mode = QPushButton("就地模式")
        self.btn_mode.setStyleSheet(
            "QPushButton{background:#07C160; color:#fff;"
            "border:none; border-radius:6px; padding:12px;"
            "font-weight:bold; font-size:16px;}"
            "QPushButton:hover{background:#06AD56;}"
        )
        self.btn_mode.setMinimumHeight(52)
        self.btn_mode.clicked.connect(self._toggle_mode)
        ctrl_grid.addWidget(self.btn_mode, 3, 0, 1, 2)

        layout.addWidget(ctrl_group, stretch=3)
        return widget

    def _create_bottom_panel(self):
        """创建底部指令输入 + 日志"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 指令输入
        cmd_frame = QFrame()
        cmd_frame.setStyleSheet(
            "background-color: #fff; border: 1px solid #e0e0e0; border-radius: 8px;"
        )
        cmd_layout = QHBoxLayout(cmd_frame)
        cmd_layout.setContentsMargins(10, 10, 10, 10)
        cmd_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        prompt = QLabel(">")
        prompt.setStyleSheet("color: #07C160; font-size: 18px; font-weight: bold;")
        cmd_layout.addWidget(prompt)

        self.cmd_entry = QLineEdit()
        self.cmd_entry.setPlaceholderText("输入指令，回车发送...")
        self.cmd_entry.returnPressed.connect(self._on_send_command)
        cmd_layout.addWidget(self.cmd_entry, stretch=1)

        btn_send = QPushButton("发送")
        btn_send.setFixedWidth(80)
        btn_send.setStyleSheet(
            "QPushButton{background:#07C160; color:#fff; border:none;"
            "border-radius:6px; padding:10px; font-weight:bold; font-size:15px;}"
            "QPushButton:hover{background:#06AD56;}"
        )
        btn_send.clicked.connect(self._on_send_command)
        cmd_layout.addWidget(btn_send)

        layout.addWidget(cmd_frame)

        # 日志
        log_group = QGroupBox("通信日志")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Microsoft YaHei", 13))
        log_layout.addWidget(self.log_text)
        layout.addWidget(log_group, stretch=1)

        return widget

    # ============ 基础功能 ============
    def _update_clock(self):
        now = datetime.now().strftime("%H:%M:%S")
        self.lbl_clock.setText(now)

    # ============ 连接逻辑 ============
    def _on_connect_clicked(self):
        if self.client.is_connected():
            self.client.disconnect()
            self._update_connection_ui(False)
            self.signals.log_message.emit("手动断开连接")
            return

        broker = self.broker_entry.text().strip()
        port_str = self.port_entry.text().strip()
        if not broker:
            QMessageBox.warning(self, "提示", "请输入Broker地址")
            return

        self.btn_connect.setEnabled(False)
        self.btn_connect.setText("连接中...")

        def do_connect():
            try:
                self.signals.log_message.emit(f"正在连接 {broker}:{port_str} ...")
                result = self.client.connect(
                    broker=broker,
                    port=int(port_str) if port_str else None
                )
                if result:
                    self.signals.connected.emit()
                else:
                    self.signals.connect_failed.emit(f"{broker}:{port_str}")
            except Exception:
                self.signals.connect_failed.emit(f"{broker}:{port_str}")

        threading.Thread(target=do_connect, daemon=True).start()

    def _on_connected(self):
        self._update_connection_ui(True)
        self.btn_connect.setEnabled(True)
        self.signals.log_message.emit("Connected!")

        # 启动轮询
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(lambda: self._send_command('status'))
        self.poll_timer.start(POLL_INTERVAL)
        self.signals.log_message.emit(f"启动状态轮询 ({POLL_INTERVAL}ms间隔)")

    def _on_connect_failed(self, addr):
        if hasattr(self, 'poll_timer'):
            self.poll_timer.stop()
            self.poll_timer.deleteLater()
            del self.poll_timer

        self.btn_connect.setText("连接设备")
        self.btn_connect.setStyleSheet(self._connect_btn_style(False))
        self.btn_connect.setEnabled(True)

        reply = QMessageBox.question(
            self, "连接失败",
            f"无法连接到 {addr}\n\n是否重试？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._on_connect_clicked()
        else:
            self.signals.log_message.emit(f"连接失败，已取消：{addr}")

    def _on_disconnected(self):
        self._update_connection_ui(False)
        self.btn_connect.setEnabled(True)
        self.signals.log_message.emit("连接断开")

        if hasattr(self, 'poll_timer'):
            self.poll_timer.stop()
            self.poll_timer.deleteLater()
            del self.poll_timer
            self.signals.log_message.emit("停止状态轮询")

    def _update_connection_ui(self, connected):
        if connected:
            self.lbl_conn.setText("已连接")
            self.lbl_conn.setStyleSheet("color: #07C160; font-weight: bold;")
            self.status_labels["网络连接"].setText("已连接")
            self.status_labels["网络连接"].setStyleSheet(
                "color: #07C160; font-size: 16px; font-weight: bold;"
            )
            self.btn_connect.setText("断开")
            self.btn_connect.setStyleSheet(self._connect_btn_style(True))
        else:
            self.lbl_conn.setText("未连接")
            self.lbl_conn.setStyleSheet("color: #fa5151; font-weight: bold;")
            self.status_labels["网络连接"].setText("断开")
            self.status_labels["网络连接"].setStyleSheet(
                "color: #fa5151; font-size: 16px; font-weight: bold;"
            )
            self.btn_connect.setText("连接设备")
            self.btn_connect.setStyleSheet(self._connect_btn_style(False))

    # ============ 状态更新显示 ============
    def _update_display(self, data):
        old = self.current_data
        self.current_data = data

        state  = data.get("state", 0)
        speed  = data.get("speed", 0)
        pot    = data.get("pot", 0)
        sensor = data.get("sensor", 0)
        mode   = data.get("mode", 0)

        if state != old.get("state"):
            motor_text, color = self.controller.get_state_text(state)
            self.status_labels["电机模式"].setText(motor_text)
            self.status_labels["电机模式"].setStyleSheet(
                f"color: {color}; font-size: 16px; font-weight: bold;"
            )
            dir_text = self.controller.get_direction_text(state)
            self.status_labels["舵机模式"].setText(dir_text)
            self.status_labels["舵机模式"].setStyleSheet(
                f"color: {color}; font-size: 16px; font-weight: bold;"
            )

        if pot != old.get("pot"):
            self.status_labels["电位器"].setText(f"{pot} %")

        if speed != old.get("speed"):
            self.status_labels["舵机速度"].setText(f"{speed} / 13")
            if self.speed_mode == 1:
                self.slider_updating = True
                self.speed_slider.setValue(speed)
                self.speed_entry.setText(str(speed))
                self.slider_updating = False

        if sensor != old.get("sensor"):
            sensor_text, sensor_color = self.controller.get_sensor_text(sensor)
            self.status_labels["光纤传感器"].setText(sensor_text)
            self.status_labels["光纤传感器"].setStyleSheet(
                f"color: {sensor_color}; font-size: 16px; font-weight: bold;"
            )

        if mode != old.get("mode"):
            self.speed_mode = mode
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
        if not self.client.is_connected():
            self.signals.log_message.emit("未连接，无法切换模式")
            return
        cmd = self.controller.get_mode_command(self.speed_mode)
        self._send_command(cmd)

    def _on_slider_change(self, value):
        if self.slider_updating:
            return
        self.speed_entry.setText(str(value))
        if self.speed_mode == 1:
            cmd = self.controller.get_speed_command(value)
            self._send_command(cmd)

    def _on_entry_change(self):
        try:
            val = self.controller.clamp_speed(self.speed_entry.text())
            self.slider_updating = True
            self.speed_slider.setValue(val)
            self.speed_entry.setText(str(val))
            self.slider_updating = False
            if self.speed_mode == 1:
                cmd = self.controller.get_speed_command(val)
                self._send_command(cmd)
        except ValueError:
            self.speed_entry.setText(str(self.current_data.get("speed", 0)))

    def _send_command(self, cmd):
        def do_send():
            self.signals.log_message.emit(f"→ {cmd}")
            response = self.client.send_cmd(cmd)
            if response is None:
                if not self.client.is_connected():
                    self.signals.disconnected.emit()
                return
            self.signals.log_message.emit(f"← {response}")
            if cmd.strip() == 'status':
                try:
                    import json
                    data = json.loads(response)
                    self.signals.status_update.emit(data)
                except Exception:
                    pass
        threading.Thread(target=do_send, daemon=True).start()

    def _on_send_command(self):
        cmd = self.cmd_entry.text().strip()
        if not cmd:
            return
        if not self.client.is_connected():
            self.signals.log_message.emit("未连接，无法发送指令")
            return
        self._send_command(cmd)
        self.cmd_entry.clear()

    def _append_log(self, message):
        now = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{now}] {message}")
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # ============ BasePage 生命周期 ============
    def on_show(self):
        """页面显示：启动时钟"""
        self.clock_timer.start(1000)

    def on_hide(self):
        """页面隐藏：停止轮询、断开连接"""
        self.clock_timer.stop()
        if hasattr(self, 'poll_timer'):
            self.poll_timer.stop()
            self.poll_timer.deleteLater()
            del self.poll_timer
        if self.client.is_connected():
            self.client.disconnect()

    def save_state(self):
        """保存状态"""
        pass