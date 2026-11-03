# AGENTS.md — 6.30传送带 (ESP32 传送带控制 + Ubuntu 深度相机)

## 结构
一个工作区，两个仓库：
- **11/** — PlatformIO 固件 (ESP32-S3, Arduino)
- **py/** — Python PyQt6 上位机 GUI (ESP32 控制中心 + HP60C 深度相机)

## 11/ — 固件（历史架构，已停止使用）

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
cd py && ./run.sh
# 或直接运行（需要手动 source ROS2 环境）：
cd py && source /opt/ros/humble/setup.bash && source .venv/bin/activate && python3 main.py
```

`run.sh` 自动完成：
1. 源 ROS2 Humble 环境
2. 激活 `.venv` 虚拟环境
3. **启动前清场**：`pkill` 残留相机进程 + `ros2 daemon stop`（清话题缓存）
4. 启动 `ros2 launch ascamera hp60c.launch.py`（HP60C 深度相机节点，假设相机已插好）
5. 启动 `main.py`
6. 退出时自动关闭相机节点

**无自动重连/watchdog**：运行中拔插深度相机后，点产线页槽位1参数面板的「重启相机」按钮手动重启（约 11 秒，`RestartCameraThread`）。
**`respawn=False`**：相机节点崩溃不会自动拉起，需手动重启或重跑 `./run.sh`。
**相机节点日志**：`/tmp/hp60c_camera.log`（`run.sh` 里重定向）。

### 依赖
```bash
pip install -r requirements.txt
# Ubuntu ROS2 依赖（系统 apt 包）：
sudo apt install -y $(grep -v '^#' ros-requirements.txt | tr '\n' ' ')
```

### 核心架构
一个工作区，三个页面：
- **传送带控制** (`conveyor_page.py`) — MQTT 连接入口 + 电机控制
- **相机识别** (`camera_page.py`) — 单摄像头 YOLO + 二维码
- **产线控制** (`production_page.py`) — 3路摄像头共享模型 + MQTT 指令控制 + 1号槽位深度相机

通信层文件：`core/mqtt_client.py`（原 `core/esp32_lib.py`）、`core/mqtt_shared.py`（原 `core/esp32_client.py`）。类名已从 `ESP32` 改为 `MqttClient`，函数从 `get_shared_esp32()` 改为 `get_shared_mqtt()`。

模块间连接方式：
- **信号槽** — QThread→GUI、Manager→GUI、MQTT→GUI 都用 Qt 信号
- **全局单例** — MQTT 连接 (`get_shared_mqtt()`)、配置 (`get_config()`)、生产配置 (`get_production_config()`)
- **回调函数** — MQTT 消息回调 (`set_prod_cmd_callback`)
- **页面独立** — 页面之间不互相引用，通过 `AppShell` 的 `QStackedWidget` 切换

### 注意：用的是 PyQt6（不是 PyQt5）
所有 import 都是 `PyQt6`，不要降级。枚举写法：`Qt.AlignmentFlag.AlignCenter`，`QFrame.Shape.VLine`。

### MQTT 连接（全局唯一）
- `core/mqtt_shared.py` 提供 `get_shared_mqtt()` / `connect_shared()` / `disconnect_shared()`
- 全局只有一个 `MqttClient` 实例，所有页面共用
- **现在 broker 和上位机运行在同一台机器**，地址为 `127.0.0.1`
- ESP32 是历史架构——跨设备部署时才用手机热点组局域网
- **传送带页面 `on_hide` 不能断开连接**，连接是全局的
- `MqttClient.connect()` 每次连接前必须 `_event.clear()` + `self.connected = False`，否则状态不准

### MQTT 主题
| 主题 | 方向 | 用途 |
|------|------|------|
| `ht/cmd/1` | 上位机→传送带 | 传送带控制命令 |
| `ht/status/1` | 传送带→上位机 | 传送带状态 |
| `ht/cmd/2` | 设备→上位机 | 产线控制指令 |
| `ht/status/2` | 上位机→设备 | 产线检测结果（JSON：`slot_id` + `detections`） |
| `ht/cmd/3` | 上位机→仓储 | 仓储控制（**规划中，GUI 未实现**） |
| `ht/status/3` | 仓储→上位机 | 仓储回报（cc 码）（**规划中**） |

协议手稿：根目录 `通讯协议.txt`（用户手写，方向最权威）。

### 产线控制指令格式
发送到 `ht/cmd/2`：`<槽位> <检测> <扫码>`
- 槽位：`xj1` / `xj2` / `xj3` / `all`
- 检测：`1`=开，`0`=关
- 扫码：`1`=开，`0`=关
- 示例：`xj1 1 1` = 槽位1 检测开+扫码开
- 手动测试：`mosquitto_pub -t "ht/cmd/2" -m "xj1 1 1"`

### MQTT Broker 部署（本机 mosquitto）
- broker 跑在**上位机本机**：`/etc/mosquitto/conf.d/default.conf` 已配 `listener 1883 0.0.0.0` + `allow_anonymous true`——局域网内任何设备可匿名读/写全部主题，**仅限内网调试环境**
- `main.py` 启动时 `print_broker_info()`（core/mqtt_shared.py）打印**动态探测的本机局域网 IP**（`local_ip()` 每次调用重新探测，不发任何网络包）
- IP 随 DHCP 漂移是常态：别人连不上先核对启动横幅上的地址，端口 1883、用户名/密码留空

### 深度相机（1号槽位 HP60C）
- **ROS2 话题**：RGB `/ascamera_hp60c/camera_publisher/rgb0/image`，深度 `/ascamera_hp60c/camera_publisher/depth0/image_raw`
- **深度格式**：uint16，单位毫米，最大范围约 1.5m
- **深度相机节点**：`run.sh` 启动一次，进程组方式管理，退出时自动关闭；**无 watchdog/自动重连**，拔插后用「重启相机」按钮
- **槽位切换逻辑**：
  - 勾选「深度相机 (HP60C)」→ 自动停止普通摄像头 → 启动 DepthWorker → 预览 RGB + 深度值
  - 取消勾选 → 停止 DepthWorker
  - 选"请选择摄像头" → 停止该槽位所有预览
  - 下拉列表在深度模式下禁用（深度相机不需要手动选摄像头）
- **深度 Worker** (`vision/depth_worker.py`)：QThread + rclpy.spin()，ROS2 订阅在后台线程
- **深度值读取**：检测框中心点像素值，0 或越界返回 -1（显示 N/A）；标签居中白底时会撞上**中心深度盲区**，建议读"框内有效像素中位数"
- **硬件实测（重要）**：
  - HP60C **仅支持 USB 2.0**——设备描述符 `bcdUSB=2.00`、VID:PID `3482:6723`（Angstrong/奥松）。换 3.0 线/口没用，这是出厂声明上限；实测 `fps=25` 实际 RGB ≈4~8fps，改 `fps=10` 反而 1.1fps（已回退 25，结论记录在 launch 文件注释）
  - 厂商 SDK（`ascamera_ros2_ws/src/ascamera/libs`，32 个 `AS_SDK_*` 接口）**无补光灯/曝光/增益接口**——"软件开灯"不存在，补光只能外接可见光灯
  - **深度中心盲区**：白色标签红外反射，标签中心深度大片为 0（画面正中心 21×21 全 0 实测）——不管代码怎么改都救不了，靠取框内中位数或侧装相机规避
  - 条形码**模块宽 ~6px** 是近距临界（实测 5.94px 可解但贴线）——扫不出时优先怀疑距离/光照/帧率，其次才是解码参数；`--interval 1 --slice 600` 是低帧率下的推荐参数
  - USB 带宽诊断口诀：`lsusb -t` 看协商速率；HP60C 预期就是 `480M`，出现在 10000M/20000M 总线上也不会变快

### 扫码 v1.2 渐进式搜索（生产默认启用）
`vision/qr_decoder.py`（`__version__="1.2"`），三机制缺省全开（`_DEFAULT_SLICE_MS=600`，qr_decoder.py:102；生产 manager `qr_slice_ms` 缺省同步 600，显式 0 才回退）：
- **时间片**：单次 `decode()` 最长 600ms，到点休眠不断，下一帧（`_decode_progressive`）接着搜——预览不冻结
- **搜索游标**：4 维状态记录"搜到哪儿了"（Q→M→S→阶段），36 个原子阶段（`_WARP_STAGES=32` 梯形流水线 + `_BIN_STAGES=4` 二值档，见 `_warp_stage`/`_bin_stage`）；跨帧永远续搜、已经扫过的整齐跳过
- **帧指纹门**（`_update_gate`，阈值 4.0）：新帧 32×32 灰度平均差 >4.0 才清空进度重搜；停带时重复帧只交代办进度、不重复烧预算
- **运行时开关**：`set_slice(0)` / 配置显式 `qr_slice_ms: 0` = 回退 v1.1（单次烧满 `rectify_budget_ms=5000`，预览会冻）；`set_slice(600)` 恢复
- **quick-skip**：静止画面跳过重复主链（直读/估角），只推进 rectify 游标——上下一次出结果的关键

### 保留的 v1.1 基础机制（仍在 v1.2 链条内）
- **估角链路**：直读 → minAreaRect 估角转 1~2 次 → 上次成功角 → 90° 单点兜底 → 放弃等下一帧（1D 转 180°/-90° 等价，故兜底只有 90°）
- **`_filter_codes()` 防假阳性**（qr_decoder.py:386，实测数字）：面积 <64 或短边 <4px 丢；1D 码制（CODE128/I25/…）短边/长边 >0.70（近方形）丢——旋转条码经 Otsu 后 ZBar 常吐假 I25
- **minAreaRect 估角**：Otsu → MORPH_CLOSE 21×21 → 外轮廓 → `area * min(aspect,4)` 评分取主轴角归一化 (-90,90]，附加 ±90 垂直候选。实测非 15 倍数角 11/11、双垂直码 7/7
- **ROI 失败后整帧也走完整估角链**（不只是直读）——YOLO 框裁不全时整帧才是唯一机会
- 耗时参考：正放 ~3ms，歪 35~40° 估角命中 ~35ms，无码坏帧 ~35~73ms（11 角盲试时代 250~480ms）
- YOLO 模型输入固定 640 letterbox，坐标映射回原帧；MQTT `ht/status/2` 的 `center` 是**绝对像素**（主控按分辨率标定对接时，改分辨率需同步；可改归一化）

### 测试工具脚本（用完即删）
- **铁律：测试脚本与考卷（含历史 tests/ 目录）用完即删，长期资产只留在正规程序里**。19 帧回归考卷与 9/14、5.6× 这些结论已随目录删除，仅作历史参考不可复验
- `scan_test.py`：普通相机扫码链调试，和产线同一条 `QRDecoder`。`--cam/--size/--interval/--slice/--fp-thresh/--budget`；**`--slice 0` 是旧行为**（`--budget` 此时生效），不传=跟随生产默认 600；存帧目录 `scan_test_out/`
- `depth_scan_test.py`：深度相机（ROS2）扫码链调试，同参数 + `--no-depth/--model/--conf`；依赖先 source ROS 环境再进 .venv 用
- 合成实验数据（已随考卷删除，仅作记录）：**同一批难帧 v1.1 需要 22~55 秒，v1.2 相同已解**；考卷好帧率不变、可靠类型毛刺不反弹

### 摄像头枚举（Ubuntu）
- **优先用 `/sys/class/video4linux/video*/name`** 读设备名（不受设备占用影响）
- 回退 `v4l2-ctl --list-devices`（ROS2 节点占用设备时会报错但仍输出部分结果）
- 再回退 OpenCV 索引探测
- **同名设备去重**：每个物理摄像头有 2 个 video 节点（采集 + 元数据），只保留第一个
- `v4l2-ctl` 输出中的错误行（"Cannot open..."）已过滤

### 摄像头生命周期（产线控制）
- **进入页面** → 自动打开已保存摄像头并预览（不检测）；深度模式则启动 DepthWorker
- **离开页面** → 关闭所有摄像头
- **按钮/MQTT** → 只控制检测开关，不控制摄像头
- `set_params()` 必须 `blockSignals(True)` 防止 `setCurrentIndex` 触发 `camera_changed` 信号自动启动

### 产线管理器 (`vision/production_manager.py`)
- 模型单例：一个 YOLO 实例，三个 Worker 共享
- 1号槽位支持 `start_slot_depth()` 启动深度相机模式
- `_depth_mode` 数组跟踪每个槽位是否为深度模式
- `stop_slot()` 同时清理普通 Worker 和深度 Worker
- `restart_camera_node()` / `RestartCameraThread`：手动重启相机 ROS2 节点（UI「重启相机」按钮）

## 跨仓库关键问题

### MQTT Broker IP 不匹配（历史问题）
- ESP32 固件：硬编码 `10.156.130.56`（myMqtt.h:14）
- Python GUI：默认 `127.0.0.1`（core/mqtt_shared.py 的 `DEFAULT_BROKER`）
- **历史问题**：ESP32 已停止使用，现在 broker 和上位机同机运行（`127.0.0.1`），不再需要手动配置。

### WiFi 密码硬编码（历史问题）
- ESP32：`myMqtt.h:12-13`（`"14"` / `"111111111"`）——**ESP32 固件已不用**
- Python：运行时通过 `MqttClient(broker, port)` 构造函数传入

## 未来架构
- **主控**（地瓜派等单板计算机）：负责 IO 控制和整体调度
- **上位机**：只做显示和调试
- 两者通过 MQTT 通信，跑两套独立程序
- 主控负责调用相机控制
