"""
core 包初始化
将核心模块暴露出来，方便外部导入。
"""
from core.mqtt_shared import get_shared_mqtt, connect_shared, disconnect_shared
from core.controller import Controller