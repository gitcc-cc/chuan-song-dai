"""
ESP32 通信层
===========
全局唯一 MQTT 连接，所有页面共用。
"""
from core.esp32_lib import ESP32

DEFAULT_BROKER = "127.0.0.1"
DEFAULT_PORT = 1883

# 全局唯一实例
_shared: ESP32 = None


def get_shared_esp32() -> ESP32:
    global _shared
    if _shared is None:
        _shared = ESP32()
    return _shared


def connect_shared(broker: str = DEFAULT_BROKER, port: int = DEFAULT_PORT) -> bool:
    """全局唯一连接入口"""
    esp = get_shared_esp32()
    if esp.is_connected():
        return True
    esp.broker = broker
    esp.port = port
    return esp.connect(timeout=5)


def disconnect_shared():
    esp = get_shared_esp32()
    esp.disconnect()
