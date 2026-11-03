"""
mqtt_client.py - 主控 MQTT 通信封装库

本文件封装了与 主控 通过 MQTT 协议通信的所有功能。

主要职责：
1. 连接/断开 MQTT Broker（消息代理服务器）
2. 订阅指定主题，接收 主控 发来的状态消息
3. 向 主控 发送控制命令（传送带、舵机等）
4. 支持产线控制模式的双向通信（命令 + 检测结果）
5. 提供线程安全的状态管理

使用场景：
- 传送带控制页面：连接 broker，发送电机控制命令，接收状态反馈
- 产线控制页面：接收产线指令，发送检测结果

依赖：
- paho-mqtt: MQTT 客户端库
- json: 消息序列化/反序列化
- threading: 多线程同步

作者：自动生成
日期：2026-09-22
"""

import paho.mqtt.client as mqtt
import json
import threading
import time


class MqttClient:
    """
    主控 MQTT 通信客户端类

    该类封装了与主控通信的所有 MQTT 操作。
    支持两种工作模式：
    1. 传送带控制模式（topic_cmd/topic_sta）：发送电机命令，接收状态
    2. 产线控制模式（topic_cmd2/topic_sta2）：接收产线指令，发送检测结果

    属性：
        broker (str): MQTT 服务器地址
        port (int): MQTT 服务器端口，默认 1883
        connected (bool): 当前连接状态，True 表示已连接
    """

    def __init__(self, broker='127.0.0.1', port=1883):
        """
        初始化 主控 客户端实例

        参数：
            broker (str): MQTT 服务器地址，默认 '127.0.0.1'（本机）
            port (int): MQTT 服务器端口，默认 1883（MQTT 标准端口）

        说明：
            初始化时会创建 MQTT 客户端对象，设置回调函数，
            但不会立即连接服务器，需要调用 connect() 方法。
        """
        # 保存连接参数
        self.broker = broker
        self.port = port

        # MQTT 主题配置（topic 是消息的"地址"，类似频道号）
        # cmd = command（命令），sta = status（状态）
        self.topic_cmd = 'ht/cmd/1'      # 传送带控制命令：GUI → 主控
        self.topic_sta = 'ht/status/1'   # 传送带状态：主控 → GUI
        self.topic_cmd2 = 'ht/cmd/2'     # 产线控制指令：主控 → GUI
        self.topic_sta2 = 'ht/status/2'  # 产线检测结果：GUI → 主控

        # 缓存 主控 最后一次发送的状态（JSON 格式）
        self._last_status = None

        # 线程锁：保证多线程环境下读写 _last_status 的安全
        # 为什么需要锁？因为 MQTT 回调和主线程可能同时访问 _last_status
        self._lock = threading.Lock()

        # 产线指令回调函数：当收到产线指令时调用
        # 回调函数签名：callback(payload_str)
        self._prod_cmd_callback = None

        # 创建 MQTT 客户端对象
        # VERSION2 表示使用 paho-mqtt 2.x 版本的回调 API
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

        # 绑定三个核心回调函数
        self.client.on_connect = self._on_connect      # 连接成功时触发
        self.client.on_disconnect = self._on_disconnect  # 连接断开时触发
        self.client.on_message = self._on_message        # 收到消息时触发

        # 连接状态标志，初始为未连接
        self.connected = False

        # 线程事件对象：用于同步等待连接结果
        # 调用 event.set() 会唤醒所有等待的线程
        # 调用 event.wait(timeout) 会阻塞等待，直到被唤醒或超时
        self._event = threading.Event()

    def connect(self, timeout=5):
        """
        连接到 MQTT Broker（消息代理服务器）

        参数：
            timeout (int): 连接超时时间（秒），默认 5 秒

        返回：
            bool: True 表示连接成功，False 表示连接失败或超时

        工作流程：
        1. 停止之前的网络循环（如果有的话）
        2. 重置连接状态
        3. 设置重连延迟（0-1秒随机）
        4. 尝试连接服务器
        5. 启动网络循环线程
        6. 等待连接成功或超时

        注意：
            - 该方法会阻塞当前线程，直到连接成功或超时
            - 连接失败后需要手动再次调用 connect() 重试
        """
        try:
            # 停止之前的网络循环线程（防止重复启动）
            self.client.loop_stop()

            # 重置同步事件，准备等待新的连接结果
            self._event.clear()

            # 重置连接状态为未连接
            self.connected = False

            # 设置重连延迟：min_delay=0, max_delay=1 秒
            # 这样如果连接失败，重试间隔会是 0-1 秒的随机值
            self.client.reconnect_delay_set(0, 1)

            # 打印连接信息，方便调试
            print(f"[MQTT] 正在连接 {self.broker}:{self.port} ...")

            # 调用 paho-mqtt 的 connect 方法
            # 参数 60 是 keepalive 时间（秒），超过这个时间没有通信会认为连接断开
            self.client.connect(self.broker, self.port, 60)

            # 启动网络循环线程（在后台运行，处理所有 MQTT 通信）
            # loop_start() 会创建一个守护线程，自动处理消息收发
            self.client.loop_start()

            # 等待连接结果：最多等待 timeout 秒
            # _event.wait() 会阻塞，直到 _on_connect 中调用 _event.set()
            result = self._event.wait(timeout=timeout)

            if result:
                # 连接成功（_on_connect 回调已被触发）
                print(f"[MQTT] 连接成功: {self.broker}:{self.port}")
            else:
                # 连接超时：超过 timeout 秒仍未收到连接确认
                print(f"[MQTT] 连接超时 ({timeout}s): {self.broker}:{self.port}")
                # 超时后停止网络循环，避免资源浪费
                self.client.loop_stop()

            return result

        except Exception as e:
            # 捕获所有异常（如网络错误、服务器不可达等）
            print(f"[MQTT] 连接异常: {e}")
            return False

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        """
        MQTT 连接成功回调函数（由 paho-mqtt 在连接成功后自动调用）

        参数：
            client: MQTT 客户端实例
            userdata: 用户自定义数据（本例未使用）
            flags: 连接标志
            reason_code: 连接结果码，reason_code.is_failure 为 False 表示成功
            properties: 连接属性

        动作：
        1. 标记连接状态为已连接
        2. 唤醒等待连接的线程（connect() 方法中的 _event.wait()）
        3. 订阅需要监听的主题
        """
        # 检查连接是否成功（reason_code 为 0 表示成功）
        if not reason_code.is_failure:
            # 标记连接成功
            self.connected = True

            # 唤醒所有等待的线程（主要是 connect() 方法）
            self._event.set()

            # 订阅主题：只有订阅了某个主题，才能收到该主题的消息
            # topic_sta: 传送带状态（主控 → GUI）
            # topic_cmd2: 产线控制指令（主控 → GUI）
            client.subscribe(self.topic_sta)
            client.subscribe(self.topic_cmd2)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        """
        MQTT 连接断开回调函数（由 paho-mqtt 在连接断开后自动调用）

        参数：
            client: MQTT 客户端实例
            userdata: 用户自定义数据
            disconnect_flags: 断开标志
            reason_code: 断开原因码
            properties: 连接属性

        动作：
            标记连接状态为未连接

        注意：
            本例没有实现自动重连，断开后需要手动调用 connect() 重连
        """
        # 标记连接已断开
        self.connected = False

    def _on_message(self, client, userdata, msg):
        """
        收到 MQTT 消息时的回调函数（由 paho-mqtt 在收到消息后自动调用）

        参数：
            client: MQTT 客户端实例
            userdata: 用户自定义数据
            msg: 消息对象，包含：
                - msg.topic: 消息主题（从哪个频道收到的）
                - msg.payload: 消息内容（字节数组）

        处理逻辑：
        1. 如果是传送带状态消息 → 解析 JSON 并缓存
        2. 如果是产线控制指令 → 调用注册的回调函数

        注意：
            该函数在 MQTT 网络线程中执行，不是主线程！
            所以回调中不要做耗时操作，否则会影响其他消息处理
        """
        # 根据消息主题分发处理
        if msg.topic == self.topic_sta:
            # 处理传送带状态消息
            try:
                # 使用线程锁保护 _last_status 的读写
                with self._lock:
                    # 解析 JSON 格式的状态数据
                    # 例如：{"state": 1, "speed": 5, "en": 1}
                    self._last_status = json.loads(msg.payload.decode())
            except:
                # JSON 解析失败时静默忽略（可能收到空消息或格式错误）
                pass

        elif msg.topic == self.topic_cmd2:
            # 处理产线控制指令
            # 指令格式示例："xj1 1 1"（槽位1 检测开 扫码开）
            payload = msg.payload.decode().strip()

            # 如果设置了回调函数，就调用它
            if self._prod_cmd_callback:
                try:
                    self._prod_cmd_callback(payload)
                except Exception as e:
                    # 回调函数执行出错，打印错误信息
                    print(f"[MQTT] 产线指令处理错误: {e}")

    def disconnect(self):
        """
        断开与 MQTT Broker 的连接

        参数：无
        返回：无

        工作流程：
        1. 标记连接状态为未连接
        2. 重置同步事件
        3. 停止网络循环线程
        4. 断开与服务器的连接

        注意：
            调用此方法后，如果需要重新通信，必须再次调用 connect()
        """
        # 标记连接已断开
        self.connected = False

        # 重置同步事件
        self._event.clear()

        try:
            # 停止网络循环线程（停止在后台收发消息）
            self.client.loop_stop()

            # 如果当前确实连接着，才发送断开请求
            if self.client.is_connected():
                self.client.disconnect()
        except:
            # 忽略任何断开时的异常（如网络已经断开）
            pass

    def send(self, cmd, timeout=3):
        """
        向 主控 发送控制命令

        参数：
            cmd (str): 要发送的命令字符串
            timeout (int): 等待响应的超时时间（秒），仅对 'status' 命令有效

        返回：
            str: 命令执行结果
                - 'ok': 普通命令发送成功（无响应）
                - JSON 字符串: 'status' 命令的返回值
                - None: 发送失败（未连接或超时）

        支持的命令：
            - 'status': 查询 主控 状态
            - 'dj 0': 停止电机
            - 'dj 1': 正转
            - 'dj 2': 反转
            - 'dj 3': 自动模式
            - 'speed N': 设置速度（0-13）
            - 'mode local/remote': 切换本地/远程控制
            - 'en 0/1': 解锁/锁定
            - 'hello': 测试连接

        注意：
            - 发送前会检查连接状态，未连接则直接返回 None
            - 只有 'status' 命令会等待响应，其他命令发送后直接返回 'ok'
        """
        # 检查是否已连接，未连接则无法发送
        if not self.connected:
            return None

        # 发布消息到传送带控制主题
        # publish() 是非阻塞的，消息会在后台发送
        self.client.publish(self.topic_cmd, cmd)

        # 如果是查询状态的命令，需要等待 主控 回复
        if cmd == 'status':
            # 记录发送前的状态快照，用于检测是否收到新状态
            old = self._last_status

            # 计算超时截止时间
            deadline = time.time() + timeout

            # 循环等待，直到收到新状态或超时
            while time.time() < deadline:
                with self._lock:
                    # 检查状态是否已更新（且不为 None）
                    if self._last_status and self._last_status != old:
                        # 收到新状态，返回 JSON 字符串
                        return json.dumps(self._last_status)

                # 短暂休眠，避免 CPU 空转
                # 100 毫秒的间隔足够响应 MQTT 通信
                time.sleep(0.1)

        # 普通命令（非 status）直接返回 'ok'
        # 或者 status 命令超时后也返回 'ok'
        return "ok"

    def status(self):
        """
        查询 主控 当前状态（便捷方法）

        参数：无
        返回：
            dict: 状态字典，例如 {"state": 1, "speed": 5, "en": 1}
                  - state: 电机状态（0=停止, 1=正转, 2=反转, 3=自动）
                  - speed: 速度值（0-13）
                  - en: 使能状态（0=解锁, 1=锁定）
            None: 查询失败或超时

        说明：
            这是 send('status') 的便捷封装，自动将 JSON 字符串转为字典
        """
        # 调用 send 方法发送 'status' 命令，等待响应
        result = self.send('status')

        if result:
            try:
                # 将 JSON 字符串解析为 Python 字典
                return json.loads(result)
            except:
                # JSON 解析失败，返回 None
                return None

        # 发送失败或超时
        return None

    def is_connected(self):
        """
        检查当前连接状态

        参数：无
        返回：
            bool: True 表示已连接且连接有效，False 表示未连接或连接已断开

        说明：
            同时检查 self.connected 标志和底层 client 的连接状态
            这样更可靠，因为 client 的状态可能在后台发生变化
        """
        # 两个条件都满足才认为真正连接
        return self.connected and self.client.is_connected()

    def set_prod_cmd_callback(self, callback):
        """
        设置产线指令回调函数

        参数：
            callback (callable): 回调函数，签名如下：
                def callback(payload_str: str) -> None
                - payload_str: 主控 发来的指令字符串，如 "xj1 1 1"

        返回：无

        说明：
            当收到 主控 发来的产线控制指令时，会调用此回调函数。
            常见指令格式："<槽位> <检测> <扫码>"
            例如："xj1 1 1" 表示槽位1 开启检测和扫码

        使用示例：
            def handle_cmd(payload):
                print(f"收到指令: {payload}")

            client.set_prod_cmd_callback(handle_cmd)
        """
        self._prod_cmd_callback = callback

    def send_prod_result(self, slot_id: int, detections: list):
        """
        发送产线检测结果到 主控

        参数：
            slot_id (int): 槽位编号（1、2 或 3）
            detections (list): 检测结果列表，格式为 [
                {
                    "class": "类别名称",
                    "confidence": 置信度,
                    "depth": 深度值（毫米）或 null
                },
                ...
            ]

        返回：无

        说明：
            检测结果会以 JSON 格式发送到 topic_sta2 主题，
            主控 收到后会根据结果控制舵机分拣。

        注意：
            - 未连接时直接返回，不发送
            - ensure_ascii=False 确保中文字符正常传输
        """
        # 未连接时无法发送
        if not self.connected:
            return

        # 构造 JSON 格式的检测结果
        payload = json.dumps({
            "slot_id": slot_id,
            "detections": detections
        }, ensure_ascii=False)

        # 发布到产线检测结果主题
        self.client.publish(self.topic_sta2, payload)

    # ========== 快捷命令方法 ==========
    # 这些方法都是对 send() 的简单封装，提供更友好的调用接口
    # 返回值与 send() 相同：'ok' 表示成功，None 表示失败

    def forward(self):
        """
        控制传送带正转

        参数：无
        返回：str 或 None（同 send()）

        说明：
            发送命令 'dj 1'，主控 收到后电机正转
            正转方向：物体从上料端流向分拣端
        """
        return self.send('dj 1')

    def reverse(self):
        """
        控制传送带反转

        参数：无
        返回：str 或 None（同 send()）

        说明：
            发送命令 'dj 2'，主控 收到后电机反转
            反转方向：物体从分拣端流向上料端
        """
        return self.send('dj 2')

    def stop(self):
        """
        停止传送带

        参数：无
        返回：str 或 None（同 send()）

        说明：
            发送命令 'dj 0'，主控 收到后停止电机
        """
        return self.send('dj 0')

    def auto(self):
        """
        启动传送带自动模式

        参数：无
        返回：str 或 None（同 send()）

        说明：
            发送命令 'dj 3'，主控 切换到自动模式
            自动模式下，光纤传感器检测到物体时自动停止
        """
        return self.send('dj 3')

    def speed(self, v):
        """
        设置传送带速度

        参数：
            v (int/float): 目标速度值（0-13）
                - 0: 最低速度
                - 13: 最高速度
                - 超出范围的值会被自动截断到 0-13

        返回：str 或 None（同 send()）

        说明：
            发送命令 'speed N'，主控 收到后调整电机速度
            速度值会被自动限制在 0-13 范围内
        """
        # 确保速度值在 0-13 范围内
        # max(0, ...) 确保不小于 0
        # min(13, ...) 确保不大于 13
        # int(v) 转换为整数
        return self.send(f'speed {max(0,min(13,int(v)))}')

    def mode_local(self):
        """
        切换到本地控制模式

        参数：无
        返回：str 或 None（同 send()）

        说明：
            发送命令 'mode local'
            本地模式下，传送带由板上按钮和电位器控制
        """
        return self.send('mode local')

    def mode_remote(self):
        """
        切换到远程控制模式

        参数：无
        返回：str 或 None（同 send()）

        说明：
            发送命令 'mode remote'
            远程模式下，传送带由 MQTT 命令控制
        """
        return self.send('mode remote')

    def unlock(self):
        """
        解锁传送带（允许控制）

        参数：无
        返回：str 或 None（同 send()）

        说明：
            发送命令 'en 0'
            解锁后才能通过命令控制传送带
        """
        return self.send('en 0')

    def lock(self):
        """
        锁定传送带（禁止控制）

        参数：无
        返回：str 或 None（同 send()）

        说明：
            发送命令 'en 1'
            锁定后所有控制命令无效
        """
        return self.send('en 1')

    def hello(self):
        """
        测试连接（发送心跳包）

        参数：无
        返回：str 或 None（同 send()）

        说明：
            发送命令 'hello'，用于测试 主控 是否正常响应
            如果返回 'ok'，说明通信正常
        """
        return self.send('hello')
