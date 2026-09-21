"""
core 包初始化
将核心模块暴露出来，方便外部导入。
"""
from core.esp32_client import get_shared_esp32, connect_shared, disconnect_shared
from core.controller import Controller