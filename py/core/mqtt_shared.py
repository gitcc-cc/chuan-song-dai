"""
主控通信层 - 全局 MQTT 连接单例管理
=====================================
这个文件负责管理与主控的 MQTT 连接，确保整个应用程序只有一个连接实例。

为什么需要单例？
- MQTT 连接是全局共享的资源
- 所有页面（传送带、相机、产线）都需要通过同一个连接与主控通信
- 避免重复连接导致资源浪费和状态混乱

如何使用：
- 调用 get_shared_mqtt() 获取全局唯一的 MqttClient 实例
- 调用 connect_shared() 建立连接
- 调用 disconnect_shared() 断开连接

MQTT 主题：
- ht/cmd/1: 传送带控制命令
- ht/status/1: 传送带状态
- ht/cmd/2: 产线控制指令
- ht/status/2: 产线检测结果
"""
from core.mqtt_client import MqttClient

# 默认 MQTT Broker 地址和端口
# 注意：ESP32 固件（历史架构）中硬编码的 Broker 地址是 10.156.130.56
# 这里默认是 127.0.0.1（本地），实际使用时需要手动配置为主控的 Broker 地址
# 如果地址不匹配，命令将无法到达主控
DEFAULT_BROKER = "127.0.0.1"
DEFAULT_PORT = 1883


def local_ip() -> str:
    """
    读取本机当前局域网 IP（每次调用都重新读取，IP 变化后自动反映）。

    实现：建一个 UDP socket 连向一个不会被路由的地址，只为让内核
    选出口网卡——不发送任何数据，拔了网线/换网络也能立刻拿到新 IP。

    返回：局域网 IP 字符串；取不到时回退解析 `hostname -I`。
    """
    import socket
    # 不发包的取出口 IP 技巧
    for probe in ("10.255.255.255", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.2)
            s.connect((probe, 1))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
    # 兜底：hostname -I 的第一个非回环 IPv4
    try:
        import subprocess
        out = subprocess.run(["hostname", "-I"], capture_output=True,
                             text=True, timeout=1).stdout.split()
        for ip in out:
            if ":" not in ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return "127.0.0.1"


def print_broker_info():
    """启动时打印 broker 连接信息——别人的上位机/主控照着填即可连进来。"""
    ip = local_ip()
    print("=" * 52)
    print(" MQTT Broker 连接信息（供其他设备连接本机）")
    print(f"   本机局域网地址: {ip}:{DEFAULT_PORT}")
    print(f"   本机回环地址  : {DEFAULT_BROKER}:{DEFAULT_PORT}")
    print(f"   用户名/密码   : 无（mosquitto 允许匿名）")
    print("   话题（方向: 数据格式）:")
    print("     传送带 ht/cmd/1    上位机 → 传送带")
    print("     传送带 ht/status/1 传送带 → 上位机")
    print("     相机   ht/cmd/2    设备 → 上位机    xj1 1 1（相机1：检测开，扫码开）")
    print("     相机   ht/status/2 上位机 → 设备")
    print("     仓储   ht/cmd/3    上位机 → 仓储")
    print("     仓储   ht/status/3 仓储 → 上位机    cc 码")
    print("=" * 52)

# 全局唯一的 MqttClient 实例（单例模式）
# 类型注解：MqttClient 或 None，初始值为 None
# 下划线前缀表示这是一个模块内部变量，不应该被外部直接访问
_shared: MqttClient = None


def get_shared_mqtt() -> MqttClient:
    """
    获取全局唯一的 MqttClient 实例（单例模式）
    
    这个函数确保整个应用程序只有一个 MqttClient 实例。
    第一次调用时创建实例，之后每次调用都返回同一个实例。
    
    返回值：
        MqttClient: 全局唯一的 MqttClient 实例
    
    使用示例：
        mqtt = get_shared_mqtt()
        mqtt.connect()
    """
    global _shared  # 声明 _shared 为全局变量，这样函数内才能修改它
    
    # 如果实例尚未创建，则创建一个
    if _shared is None:
        _shared = MqttClient()
    
    return _shared


def connect_shared(broker: str = DEFAULT_BROKER, port: int = DEFAULT_PORT) -> bool:
    """
    全局唯一连接入口 - 建立与主控的 MQTT 连接
    
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
        - ESP32 固件（历史架构）中的 Broker 地址必须与此处一致
    """
    # 获取全局唯一的 MqttClient 实例
    mqtt = get_shared_mqtt()
    
    # 如果已经连接，直接返回 True
    if mqtt.is_connected():
        return True
    
    # 设置 Broker 地址和端口
    mqtt.broker = broker
    mqtt.port = port
    
    # 建立连接，超时时间 5 秒
    return mqtt.connect(timeout=5)


def disconnect_shared():
    """
    断开全局 MQTT 连接
    
    这个函数会断开与主控的 MQTT 连接。
    通常在应用程序退出时调用。
    
    注意事项：
        - 断开连接后，可以通过 connect_shared() 重新连接
        - 这个函数不会删除 MqttClient 实例，只是断开连接
    
    使用示例：
        disconnect_shared()
        print("连接已断开")
    """
    # 获取全局唯一的 MqttClient 实例
    mqtt = get_shared_mqtt()
    
    # 断开连接
    mqtt.disconnect()
