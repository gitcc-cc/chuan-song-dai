"""
业务逻辑层
==========
负责所有业务判断和数据处理，包括：
- 电机状态映射（数字 → 文字）
- 舵机方向映射
- 传感器状态判断
- 速度值校验和限制
- 模式切换命令生成

这一层不关心界面怎么显示，也不关心数据怎么传输。
修改业务规则时，只需要改这个文件。
"""


class Controller:
    """
    业务控制器
    集中管理所有状态映射和业务判断逻辑。
    MainWindow 遇到"该怎么判断"的问题时，问 Controller。
    """

    # ============ 电机状态映射表 ============
    # 主控返回的数字 → (显示文字, 颜色)
    STATE_MAP = {
        0: ("停止", "#999999"),
        1: ("正转", "#07C160"),
        2: ("反转", "#576B95"),
        3: ("自动", "#FFC300"),
    }

    # ============ 舵机方向映射表 ============
    # 主控返回的数字 → 显示文字
    DIRECTION_MAP = {
        0: "—",
        1: "顺时针",
        2: "逆时针",
        3: "传感器",
    }

    # ============ 速度范围限制 ============
    SPEED_MIN = 0
    SPEED_MAX = 13

    def get_state_text(self, state):
        """
        根据电机状态数字，返回显示文字和颜色

        参数:
            state: 主控返回的状态数字 (0/1/2/3)

        返回:
            (文字, 颜色) 的元组，例如 ("正转", "#07C160")
            未知状态返回 ("未知", "#ffffff")
        """
        return self.STATE_MAP.get(state, ("未知", "#ffffff"))

    def get_direction_text(self, state):
        """
        根据状态数字，返回舵机方向描述

        参数:
            state: 主控返回的状态数字

        返回:
            方向描述文字，例如 "顺时针"、"逆时针"
        """
        return self.DIRECTION_MAP.get(state, "—")

    def get_sensor_text(self, sensor):
        """
        根据传感器数字，返回显示文字和颜色

        参数:
            sensor: 传感器值 (0 = 检测到, 非0 = 未检测)

        返回:
            (文字, 颜色) 的元组
        """
        if sensor == 0:
            return ("检测到！", "#fa5151")
        else:
            return ("未检测", "#333333")

    def clamp_speed(self, value):
        """
        将速度值限制在合法范围内 (0~13)

        参数:
            value: 用户输入的速度值

        返回:
            限制后的整数值
        """
        return max(self.SPEED_MIN, min(self.SPEED_MAX, int(value)))

    def get_speed_mode_text(self, mode):
        """
        根据模式数字，返回速度模式的显示信息

        参数:
            mode: 0 = 就地模式，1 = 远程模式

        返回:
            (按钮文字, 状态栏文字, 颜色) 的元组
        """
        if mode == 0:
            return ("就地模式", "就地", "#07C160")
        else:
            return ("远程模式", "远程", "#576B95")

    def get_mode_command(self, current_mode):
        """
        根据当前模式，生成切换模式的命令

        参数:
            current_mode: 当前模式 (0 = 就地，1 = 远程)

        返回:
            要发送的命令字符串
        """
        if current_mode == 0:
            return "mode remote"  # 当前是就地 → 切到远程
        else:
            return "mode local"   # 当前是远程 → 切到就地

    def get_speed_command(self, speed_value):
        """
        根据速度值，生成设置速度的命令

        参数:
            speed_value: 速度值 (已校验)

        返回:
            要发送的命令字符串，例如 "speed 5"
        """
        return f"speed {speed_value}"