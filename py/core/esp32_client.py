"""
ESP32 通信层
============
负责所有与 ESP32 的网络通信，包括：
- 建立/断开连接
- 发送命令
- 查询连接状态

这一层不关心命令的含义，只负责"发出去"和"收回来"。
修改通信协议或底层库时，只需要改这个文件。
"""
from core.esp32_lib import ESP32

# ============ 默认配置 ============
DEFAULT_BROKER = "127.0.0.1"
DEFAULT_PORT = 1883


class ESP32Client:
    """
    ESP32 网络客户端
    封装了底层 esp32_lib 的连接和收发操作。
    MainWindow 通过这个类与 ESP32 通信，不需要直接操作底层库。
    """

    def __init__(self):
        """初始化客户端，创建底层通信对象"""
        self.client = ESP32()
        self.connected = False

    def connect(self, broker=None, port=None):
        """
        连接到 ESP32（通过 Broker 地址）

        参数:
            broker: Broker 的 IP 地址，默认使用 DEFAULT_BROKER
            port: 端口号，默认使用 DEFAULT_PORT

        返回:
            True 连接成功，False 连接失败
        """
        # 如果已连接，先断开再重连
        if self.client.is_connected():
            self.client.disconnect()

        # 用指定地址创建新的底层连接对象
        self.client = ESP32(broker=broker or DEFAULT_BROKER)
        result = self.client.connect(timeout=5)
        self.connected = result
        return result

    def disconnect(self):
        """断开与 ESP32 的连接"""
        self.client.disconnect()
        self.connected = False

    def send_cmd(self, cmd):
        """
        向 ESP32 发送一条命令

        参数:
            cmd: 命令字符串，例如 "dj 1"、"speed 5"

        返回:
            ESP32 返回的响应字符串，未连接时返回 None
        """
        if not self.connected:
            return None
        return self.client.send(cmd)

    def is_connected(self):
        """
        查询当前是否处于连接状态

        返回:
            True 已连接，False 未连接
        """
        return self.connected