# AGENTS.md — 6.30传送带 (ESP32 Conveyor Belt Control)

## 项目概览
双仓库结构：
- **11/** — PlatformIO 固件 (ESP32-S3, Arduino 框架)
- **py/** — Python PyQt5 上位机 GUI (ESP32 控制中心)

---

## 11/ — ESP32 固件 (PlatformIO)

### 关键命令
```bash
cd 11
pio run                    # 编译 (默认环境 esp32s3-usb)
pio run -e esp32s3-ota     # 编译 OTA 环境
pio run -e esp32s3-usb     # 编译 USB 环境
pio device monitor         # 串口监视器 (115200 baud)
pio run -t upload          # 烧录 (USB)
pio run -e esp32s3-ota -t upload  # OTA 烧录 (需配置 IP)
```

### 环境配置 (platformio.ini)
- **esp32s3-usb** (默认): USB 串口烧录, `upload_protocol = esptool`
- **esp32s3-ota**: OTA 无线烧录, `upload_port = 10.47.240.122`, `upload_flags = --auth=1234`

### 硬件接线 (main.cpp)
| 引脚 | 功能 |
|------|------|
| 4 | 按钮 (内部上拉, 按下=LOW) |
| 7 | 电位器 ADC |
| 18 | 传送带电机 A |
| 15 | 传送带电机 B |
| 5 | 光纤传感器 (NPN: LOW=检测到物体) |
| 16/17 | 舵机串口 RX/TX (Serial2, 1Mbps) |
| 48 | 板载 RGB LED (NeoPixel) |

### 通信协议 (MQTT)
- **Broker**: 本地局域网 (默认 127.0.0.1:1883)
- **命令主题**: `ht/cmd/1`
- **状态主题**: `ht/status/1`

### 支持命令
| 命令 | 说明 |
|------|------|
| `status` | 查询状态 (返回 JSON) |
| `dj 0/1/2/3` | 状态切换: 停止/正转/反转/自动 |
| `speed 0-13` | 远程调速 (仅远程模式生效) |
| `mode local/remote` | 切换就地/远程模式 |
| `en 0/1` | 解抱闸/上抱闸 |
| `hello` | 测试通信 |

### 状态 JSON 格式
```json
{"state":0-3,"speed":0-13,"pot":0-100,"sensor":0/1,"mode":0/1}
```

---

## py/ — Python 上位机 (PyQt5)

### 依赖
```bash
pip install PyQt5 paho-mqtt
```

### 运行
```bash
cd py
python main.py
```

### 架构分层
```
main.py           → 入口
ui/main_window.py → 界面层 (Qt, 只负责显示/交互)
core/controller.py→ 业务层 (状态映射, 校验, 命令生成)
core/esp32_client.py → 通信层 (连接/发送/接收封装)
esp32_lib.py      → 底层 MQTT (paho-mqtt, 线程安全)
styles.py         → QSS 样式
```

### 关键设计
- **三层分离**: 界面不含业务逻辑, 业务不含通信细节
- **线程安全**: 网络操作在后台线程, 通过 `WorkerSignals` (Qt 信号) 回主线程更新 UI
- **状态轮询**: `POLL_INTERVAL = 2000ms` 定时查询 ESP32 状态
- **防抖设计**: 滑块联动时 `slider_updating` 标志防止循环发送

### 默认连接配置
- Broker: `127.0.0.1`
- Port: `1883`

---

## 常见问题/注意事项

1. **固件烧录前**需确认 `platformio.ini` 中 OTA IP (`upload_port`) 是否为当前 ESP32 实际 IP
2. **Python 连接失败**通常是 Broker IP/端口错误, 或 ESP32 未连上 WiFi
3. **串口监视器**可查看 WiFi 连接过程、MQTT 连接状态、OTA 进度
4. **自动模式**下传感器逻辑: LOW=检测到物体→停, HIGH=无物体→转 (main.cpp:246-269)
5. **速度范围**: 0-13 (舵机寄存器 46: 0=停, 1-13=反转, 128-141=正转)