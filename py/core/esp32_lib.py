# esp32_lib.py - MQTT 通信库
import paho.mqtt.client as mqtt
import json
import threading
import time


class ESP32:
    def __init__(self, broker='127.0.0.1', port=1883):
        self.broker = broker
        self.port = port
        self.topic_cmd = 'ht/cmd/1'
        self.topic_sta = 'ht/status/1'
        self.topic_cmd2 = 'ht/cmd/2'
        self.topic_sta2 = 'ht/status/2'
        self._last_status = None
        self._lock = threading.Lock()
        self._prod_cmd_callback = None  # 产线指令回调

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.connected = False
        self._event = threading.Event()

    def connect(self, timeout=5):
        try:
            self.client.loop_stop()
            self._event.clear()
            self.connected = False
            self.client.reconnect_delay_set(0, 1)
            print(f"[MQTT] 正在连接 {self.broker}:{self.port} ...")
            self.client.connect(self.broker, self.port, 60)
            self.client.loop_start()
            result = self._event.wait(timeout=timeout)
            if result:
                print(f"[MQTT] 连接成功: {self.broker}:{self.port}")
            else:
                print(f"[MQTT] 连接超时 ({timeout}s): {self.broker}:{self.port}")
                self.client.loop_stop()
            return result
        except Exception as e:
            print(f"[MQTT] 连接异常: {e}")
            return False

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if not reason_code.is_failure:
            self.connected = True
            self._event.set()
            client.subscribe(self.topic_sta)
            client.subscribe(self.topic_cmd2)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        if msg.topic == self.topic_sta:
            try:
                with self._lock:
                    self._last_status = json.loads(msg.payload.decode())
            except:
                pass
        elif msg.topic == self.topic_cmd2:
            payload = msg.payload.decode().strip()
            if self._prod_cmd_callback:
                try:
                    self._prod_cmd_callback(payload)
                except Exception as e:
                    print(f"[MQTT] 产线指令处理错误: {e}")

    def disconnect(self):
        self.connected = False
        self._event.clear()
        try:
            self.client.loop_stop()
            if self.client.is_connected():
                self.client.disconnect()
        except:
            pass

    def send(self, cmd, timeout=3):
        if not self.connected:
            return None
        self.client.publish(self.topic_cmd, cmd)
        if cmd == 'status':
            old = self._last_status
            deadline = time.time() + timeout
            while time.time() < deadline:
                with self._lock:
                    if self._last_status and self._last_status != old:
                        return json.dumps(self._last_status)
                time.sleep(0.1)
        return "ok"

    def status(self):
        result = self.send('status')
        if result:
            try:
                return json.loads(result)
            except:
                return None
        return None

    def is_connected(self):
        return self.connected and self.client.is_connected()

    def set_prod_cmd_callback(self, callback):
        """设置产线指令回调：callback(payload_str)"""
        self._prod_cmd_callback = callback

    def send_prod_result(self, slot_id: int, detections: list):
        """发送产线检测结果到 ht/status/2"""
        if not self.connected:
            return
        payload = json.dumps({
            "slot_id": slot_id,
            "detections": detections
        }, ensure_ascii=False)
        self.client.publish(self.topic_sta2, payload)

    # 快捷方法
    def forward(self): return self.send('dj 1')
    def reverse(self): return self.send('dj 2')
    def stop(self): return self.send('dj 0')
    def auto(self): return self.send('dj 3')
    def speed(self, v): return self.send(f'speed {max(0,min(13,int(v)))}')
    def mode_local(self): return self.send('mode local')
    def mode_remote(self): return self.send('mode remote')
    def unlock(self): return self.send('en 0')
    def lock(self): return self.send('en 1')
    def hello(self): return self.send('hello')
