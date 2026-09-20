# AGENTS.md — 6.30传送带 (ESP32 传送带控制)

## 结构
一个工作区，两个仓库：
- **11/** — PlatformIO 固件 (ESP32-S3, Arduino)
- **py/** — Python PyQt6 上位机 GUI (ESP32 控制中心)

## 11/ — 固件

### 编译与烧录
```bash
cd 11
pio run                          # 编译 (默认环境 esp32s3-usb)
pio run -t upload                # USB 烧录
pio run -e esp32s3-ota -t upload # OTA 烧录 (需先改 IP)
pio device monitor               # 串口监视器 (115200 baud)
```

### USB 口注意事项
板子有**两个 USB 口**：OTG（原生 USB-C, CDC）和 TTL（UART0, GPIO43/44, CH340/CP2102）。`platformio.ini` **没有** `board_build.arduino.usb_mode` 配置——`Serial` 输出走 **TTL 口**。如果加回这两行，`Serial` 会切到 OTG 口。`pio run -t upload` 始终用 OTG 口烧录。

### OTA
- 主机名：`ESP32-S3`，密码：`1234`
- **烧录前必须把 `platformio.ini` 的 `upload_port` 改成 ESP32 当前 IP**
- `otaBegin()` 必须在 WiFi 连上之后调用；`otaLoop()` 在每次 `loop()` 中执行（受 `wifiReady` 控制）

### WiFi 重连状态机 (main.cpp:204-230)
- `wifi_flag`：0=空闲, 1=计时中, 2=已调用 WiFi.begin()
- 断开时：`wifiReady=false`, `netInitDone=false`, flag→1, 记录时间戳
- 5秒后：若 flag 仍为 1 → 调用 `WiFi.begin()` 一次，flag→2
- 连上后：`!netInitDone && mqttWiFiConnected()` 块执行 → `mqttConnectBroker()` + `otaBegin()`，flag 重置为 0
- **关键缺陷**：5秒重连失败后 `wifi_flag` 停在 2，永远不再重试——每个上电周期只有一次重连机会。如果后续要加重试逻辑，从这里改。

### MQTT (myMqtt.h)
- WiFi SSID：`14`，密码：`111111111`
- Broker：`10.156.130.56:1883`
- 主题：`ht/cmd/1`（命令），`ht/status/1`（状态）
- `mqttConnectBroker()` 阻塞最多 2 秒，开机只调用一次。Broker 断开后**不会自动重连**。
- 支持命令：`status`、`dj 0-3`、`speed 0-13`、`mode local/remote`、`en 0/1`、`hello`
- 舵机寄存器 46：0=停，1-13=反转，128-141=正转

### 引脚对应
| 引脚 | 功能 |
|------|------|
| 4 | 按钮（内部上拉，按下=LOW） |
| 7 | 电位器 ADC |
| 18/15 | 电机 A/B（A=1,B=0=正转；A=1,B=1=反转） |
| 5 | 光纤传感器（NPN：LOW=检测到物体） |
| 16/17 | 舵机串口 RX/TX（1Mbps） |
| 48 | 板载 NeoPixel RGB LED |

### 状态机
- 状态：0=停止, 1=正转, 2=反转, 3=自动
- `state 3` 自动模式：传感器 LOW 持续 0.5 秒 → 停止；HIGH → 正转
- 速度模式：0=就地（电位器），1=远程（MQTT `speed` 命令）

### 无测试 / 无 Lint / 无 CI
质量完全靠手动验证。没有测试框架、没有 linter、没有 CI。

## py/ — Python 上位机

### 运行
```bash
cd py && python main.py
```

### 依赖
```bash
pip install -r requirements.txt
# 额外 Windows 依赖：
pip install wmi
```

### 核心架构
一个工作区，三个页面：
- **传送带控制** (`conveyor_page.py`) — MQTT 连接入口 + 电机控制
- **相机识别** (`camera_page.py`) — 单摄像头 YOLO + 二维码
- **产线控制** (`production_page.py`) — 3路摄像头共享模型 + MQTT 指令控制

模块间连接方式：
- **信号槽** — QThread→GUI、Manager→GUI、MQTT→GUI 都用 Qt 信号
- **全局单例** — MQTT 连接 (`get_shared_esp32()`)、配置 (`get_config()`)、生产配置 (`get_production_config()`)
- **回调函数** — MQTT 消息回调 (`set_prod_cmd_callback`)
- **页面独立** — 页面之间不互相引用，通过 `AppShell` 的 `QStackedWidget` 切换

### 注意：用的是 PyQt6（不是 PyQt5）
所有 import 都是 `PyQt6`，不要降级。枚举写法：`Qt.AlignmentFlag.AlignCenter`，`QFrame.Shape.VLine`。

### MQTT 连接（全局唯一）
- `core/esp32_client.py` 提供 `get_shared_esp32()` / `connect_shared()` / `disconnect_shared()`
- 全局只有一个 ESP32 实例，所有页面共用
- **传送带页面 `on_hide` 不能断开连接**，连接是全局的
- `ESP32.connect()` 每次连接前必须 `_event.clear()` + `self.connected = False`，否则状态不准

### MQTT 主题
| 主题 | 方向 | 用途 |
|------|------|------|
| `ht/cmd/1` | GUI→ESP32 | 传送带控制命令 |
| `ht/status/1` | ESP32→GUI | 传送带状态 |
| `ht/cmd/2` | ESP32→GUI | 产线控制指令 |
| `ht/status/2` | GUI→ESP32 | 产线检测结果 |

### 产线控制指令格式
发送到 `ht/cmd/2`：`<槽位> <检测> <扫码>`
- 槽位：`xj1` / `xj2` / `xj3` / `all`
- 检测：`1`=开，`0`=关
- 扫码：`1`=开，`0`=关
- 示例：`xj1 1 1` = 槽位1 检测开+扫码开

### 摄像头生命周期（产线控制）
- **进入页面** → 自动打开已保存摄像头并预览（不检测）
- **离开页面** → 关闭所有摄像头
- **按钮/MQTT** → 只控制检测开关，不控制摄像头
- `set_params()` 必须 `blockSignals(True)` 防止 `setCurrentIndex` 触发 `camera_changed` 信号自动启动

### 摄像头检测
- Windows 摄像头枚举：`pyusbcamerainindex`，fallback 用 WMI 查 Win32_PnPEntity
- 摄像头打开用 `SlotCameraOpenThread` 异步，不阻塞 UI
- 三个槽位共享模型实例 (`ProductionManager` 管理模型单例)

## 跨仓库关键问题

### MQTT Broker IP 不匹配（最严重）
- ESP32 固件：硬编码 `10.156.130.56`（myMqtt.h:14）
- Python GUI：默认 `127.0.0.1`（esp32_lib.py:9）
- **必须一致，否则命令到不了 ESP32。** 用户需手动配置上位机的 Broker 地址。

### WiFi 密码硬编码
- ESP32：`myMqtt.h:12-13`（`"14"` / `"111111111"`）
- Python：运行时通过 `ESP32(broker, port)` 构造函数传入
