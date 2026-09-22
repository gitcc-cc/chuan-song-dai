"""
ESP32 通信层 - 全局 MQTT 连接单例管理
=====================================
这个文件负责管理与 ESP32 的 MQTT 连接，确保整个应用程序只有一个连接实例。

为什么需要单例？
- MQTT 连接是全局共享的资源
- 所有页面（传送带、相机、产线）都需要通过同一个连接与 ESP32 通信
- 避免重复连接导致资源浪费和状态混乱

如何使用：
- 调用 get_shared_esp32() 获取全局唯一的 ESP32 实例
- 调用 connect_shared() 建立连接
- 调用 disconnect_shared() 断开连接

MQTT 主题：
- ht/cmd/1: 传送带控制命令
- ht/status/1: 传送带状态
- ht/cmd/2: 产线控制指令
- ht/status/2: 产线检测结果
"""
from core.esp32_lib import ESP32

# 默认 MQTT Broker 地址和端口
# 注意：ESP32 固件中硬编码的 Broker 地址是 10.156.130.56
# 这里默认是 127.0.0.1（本地），实际使用时需要手动配置为 ESP32 的 Broker 地址
# 如果地址不匹配，命令将无法到达 ESP32
DEFAULT_BROKER = "127.0.0.1"
DEFAULT_PORT = 1883

# 全局唯一的 ESP32 连接实例（单例模式）
# 类型注解：ESP32 或 None，初始值为 None
# 下划线前缀表示这是一个模块内部变量，不应该被外部直接访问
_shared: ESP32 = None


def get_shared_esp32() -> ESP32:
    """
    获取全局唯一的 ESP32 连接实例（单例模式）
    
    这个函数确保整个应用程序只有一个 ESP32 实例。
    第一次调用时创建实例，之后每次调用都返回同一个实例。
    
    返回值：
        ESP32: 全局唯一的 ESP32 连接实例
    
    使用示例：
        esp = get_shared_esp32()
        esp.connect()
    """
    global _shared  # 声明 _shared 为全局变量，这样函数内才能修改它
    
    # 如果实例尚未创建，则创建一个
    if _shared is None:
        _shared = ESP32()
    
    return _shared


def connect_shared(broker: str = DEFAULT_BROKER, port: int = DEFAULT_PORT) -> bool:
    """
    全局唯一连接入口 - 建立与 ESP32 的 MQTT 连接
    
    这是外部代码调用连接的主要函数。它会检查是否已连接，
    如果没有连接则建立新连接。
    
    参数：
        broker (str): MQTT Broker 的 IP 地址，默认是 "127.0.0.1"
        port (int): MQTT Broker 的端口号，默认是 1883
    
    返回值：
        bool: 连接成功返回 True，失败返回 False
    
    使用示例：
        success = connect_shared("192.168.1.100", 1883)
        if success:
            print("连接成功！")
    
    注意事项：
        - 如果已经连接，会直接返回 True，不会重复连接
        - 连接超时时间为 5 秒
        - ESP32 固件中的 Broker 地址必须与此处一致
    """
    # 获取全局唯一的 ESP32 实例
    esp = get_shared_esp32()
    
    # 如果已经连接，直接返回 True
    if esp.is_connected():
        return True
    
    # 设置 Broker 地址和端口
    esp.broker = broker
    esp.port = port
    
    # 建立连接，超时时间 5 秒
    return esp.connect(timeout=5)


def disconnect_shared():
    """
    断开全局 MQTT 连接
    
    这个函数会断开与 ESP32 的 MQTT 连接。
    通常在应用程序退出时调用。
    
    注意事项：
        - 断开连接后，可以通过 connect_shared() 重新连接
        - 这个函数不会删除 ESP32 实例，只是断开连接
    
    使用示例：
        disconnect_shared()
        print("连接已断开")
    """
    # 获取全局唯一的 ESP32 实例
    esp = get_shared_esp32()
    
    # 断开连接
    esp.disconnect()
