"""
配置管理模块
============
这个文件负责管理应用程序的所有配置信息。

主要功能：
1. 使用 JSON 文件存储配置（持久化保存，程序关闭后配置不会丢失）
2. 自动创建配置目录（如果目录不存在）
3. 提供简单的读取/写入接口
4. 支持默认配置（新安装时自动使用合理的默认值）
5. 全局单例模式（整个程序只有一个配置管理器实例）

配置文件位置：./config/camera_config.json
配置文件格式：JSON（人类可读的文本格式）

使用示例：
    config = get_config()           # 获取配置管理器
    model = config.get("model_path")  # 读取配置
    config.set("model_path", "yolov8.pt")  # 写入配置
    config.save()                   # 保存到文件
"""
import json
from pathlib import Path


class ConfigManager:
    """
    配置管理器类
    
    这个类负责管理一个 JSON 配置文件，包括：
    - 读取配置文件
    - 写入配置文件
    - 提供默认配置
    
    属性：
        config_path: 配置文件的完整路径（Path 对象）
        _defaults: 默认配置字典（当配置文件不存在或缺少某些键时使用）
        _config: 当前内存中的配置字典（实际使用的配置）
    """

    def __init__(self, config_dir: str = "./config", filename: str = "camera_config.json"):
        """
        初始化配置管理器
        
        参数:
            config_dir (str): 配置文件存放的目录路径
                - 可以是相对路径（如 "./config"）或绝对路径（如 "/home/user/config"）
                - 如果目录不存在，会自动创建
            filename (str): 配置文件名
                - 默认为 "camera_config.json"
                - 文件扩展名通常是 .json
            
        返回值: 无（构造函数）
        
        示例:
            manager = ConfigManager()  # 使用默认配置
            manager = ConfigManager("/tmp", "my_config.json")  # 自定义路径
        """
        # 将目录和文件名组合成完整的文件路径
        # Path("/config/camera_config.json")
        self.config_path = Path(config_dir) / filename
        
        # 确保配置文件所在的目录存在
        # parents=True: 如果父目录不存在，递归创建所有父目录
        # exist_ok=True: 如果目录已存在，不报错
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        # 默认配置字典
        # 当配置文件不存在或缺少某些键时，使用这些默认值
        # 这样可以确保程序始终有可用的配置
        self._defaults = {
            "model_path": "",           # YOLO 模型文件路径（空字符串表示未设置）
            "camera_index": 0,          # 摄像头索引号（0 通常是第一个摄像头）
            "conf_threshold": 0.5,      # 置信度阈值（0-1，越高越严格）
            "iou_threshold": 0.45,      # IoU 阈值（用于 NMS，控制重叠框的过滤）
            "window_geometry": None,    # 窗口位置和大小（None 表示使用默认位置）
            "sidebar_expanded": True,   # 侧边栏是否展开（True=展开，False=收起）
        }

        self._config = self._load()

    def _load(self) -> dict:
        """
        从 JSON 文件加载配置
        
        工作流程：
        1. 检查配置文件是否存在
        2. 如果存在，读取并解析 JSON 内容
        3. 将读取的配置与默认配置合并（确保所有键都存在）
        4. 如果文件不存在或读取失败，返回默认配置
        
        返回值:
            dict: 配置字典，包含所有配置项
            
        异常处理：
            - JSONDecodeError: 如果文件内容不是有效的 JSON
            - OSError: 如果文件读取失败（如权限问题）
            这些异常会被静默处理，返回默认配置
        """
        # 检查配置文件是否存在
        if self.config_path.exists():
            try:
                # 以 UTF-8 编码读取 JSON 文件
                # with 语句确保文件正确关闭
                with open(self.config_path, "r", encoding="utf-8") as f:
                    # json.load() 将 JSON 文本转换为 Python 字典
                    data = json.load(f)
                
                # 合并默认值（兼容旧版本配置文件）
                # 如果旧配置文件缺少新增的键，会自动使用默认值
                merged = self._defaults.copy()  # 复制默认配置
                merged.update(data)  # 用文件中的配置覆盖默认值
                return merged
            except (json.JSONDecodeError, OSError):
                # 如果文件损坏或无法读取，忽略错误，返回默认配置
                pass
        
        # 如果文件不存在，返回默认配置的副本
        # 使用 copy() 是为了防止外部修改影响内部默认值
        return self._defaults.copy()

    def save(self) -> bool:
        """
        将当前内存中的配置保存到 JSON 文件
        
        参数: 无
        
        返回值:
            bool: 保存是否成功
                - True: 保存成功
                - False: 保存失败（如权限问题、磁盘满等）
        
        注意：
            - 这个方法会覆盖整个配置文件
            - 保存后，下次启动时会读取到最新的配置
        """
        try:
            # 以写入模式打开文件（如果文件不存在会创建，如果存在会覆盖）
            with open(self.config_path, "w", encoding="utf-8") as f:
                # json.dump() 将 Python 字典转换为 JSON 文本并写入文件
                # ensure_ascii=False: 允许写入中文等非 ASCII 字符
                # indent=2: 使用 2 个空格缩进，使 JSON 更易读
                json.dump(self._config, f, ensure_ascii=False, indent=2)
            return True
        except OSError:
            # 如果写入失败（如权限问题），返回 False
            return False

    def get(self, key: str, default=None):
        """
        获取指定键的配置值
        
        参数:
            key (str): 配置项的名称（如 "model_path"）
            default: 当键不存在时返回的默认值（默认为 None）
        
        返回值:
            任意类型: 配置值的类型取决于配置项（可能是字符串、数字、布尔值等）
            如果键不存在，返回 default 参数的值
        
        示例:
            model = config.get("model_path")  # 返回 "" 或配置的值
            threshold = config.get("conf_threshold", 0.5)  # 返回 0.5 或配置的值
        """
        # dict.get() 方法：如果键存在，返回对应的值；如果不存在，返回 default
        return self._config.get(key, default)

    def set(self, key: str, value) -> None:
        """
        设置指定键的配置值
        
        参数:
            key (str): 配置项的名称（如 "model_path"）
            value: 要设置的值（可以是任意类型：字符串、数字、布尔值、列表等）
        
        返回值: 无
        
        注意：
            - 这个方法只修改内存中的配置
            - 要永久保存，需要调用 save() 方法
            - 如果键已存在，会覆盖原来的值
            - 如果键不存在，会创建新的配置项
        
        示例:
            config.set("model_path", "yolov8.pt")
            config.set("conf_threshold", 0.7)
            config.save()  # 保存到文件
        """
        # 直接通过字典键值对设置配置
        self._config[key] = value

    def update(self, data: dict) -> None:
        """
        批量更新多个配置项
        
        参数:
            data (dict): 包含多个配置项的字典
                - 键是配置项名称
                - 值是要设置的新值
        
        返回值: 无
        
        注意：
            - 这个方法只修改内存中的配置
            - 要永久保存，需要调用 save() 方法
            - 如果键已存在，会覆盖原来的值
            - 如果键不存在，会创建新的配置项
        
        示例:
            config.update({
                "model_path": "yolov8.pt",
                "conf_threshold": 0.7,
                "iou_threshold": 0.5
            })
            config.save()  # 保存到文件
        """
        # dict.update() 方法：用另一个字典的内容更新当前字典
        self._config.update(data)

    def all(self) -> dict:
        """
        获取完整的配置字典副本
        
        参数: 无
        
        返回值:
            dict: 包含所有配置项的字典副本
        
        注意：
            - 返回的是副本，修改副本不会影响原始配置
            - 如果需要修改配置，请使用 set() 或 update() 方法
        
        示例:
            all_config = config.all()
            print(all_config)  # 打印所有配置
        """
        # 返回配置字典的浅副本
        # 使用 copy() 是为了防止外部修改影响内部配置
        return self._config.copy()


# 全局单例字典（按文件名隔离）
# 这个字典存储所有配置管理器实例
# 键是 "目录/文件名" 的组合，值是对应的 ConfigManager 实例
# 这样可以确保每个配置文件只有一个管理器实例（单例模式）
_config_instances: dict = {}


def get_config(config_dir: str = "./config", filename: str = "camera_config.json") -> ConfigManager:
    """
    获取全局配置实例（单例模式）
    
    这个函数实现了单例模式，确保每个配置文件只有一个管理器实例。
    如果实例已经存在，直接返回；如果不存在，创建新实例并存储。
    
    参数:
        config_dir (str): 配置文件目录（默认为 "./config"）
        filename (str): 配置文件名（默认为 "camera_config.json"）
    
    返回值:
        ConfigManager: 配置管理器实例
        
    示例:
        # 获取默认配置管理器
        config = get_config()
        
        # 获取产线控制配置管理器
        prod_config = get_config("./config", "production_config.json")
    """
    # 创建唯一的键，用于标识不同的配置文件
    key = f"{config_dir}/{filename}"
    
    # 如果该配置文件的管理器实例不存在，则创建
    if key not in _config_instances:
        _config_instances[key] = ConfigManager(config_dir, filename)
    
    # 返回已存在的或新创建的实例
    return _config_instances[key]


def get_production_config(config_dir: str = "./config") -> ConfigManager:
    """
    获取产线控制配置实例
    
    这个函数是 get_config() 的快捷方式，专门用于获取产线控制的配置。
    产线控制使用独立的配置文件 "production_config.json"，与普通摄像头配置分离。
    
    参数:
        config_dir (str): 配置文件目录（默认为 "./config"）
    
    返回值:
        ConfigManager: 产线控制配置管理器实例
        
    示例:
        # 获取产线控制配置
        prod_config = get_production_config()
        prod_config.set("camera_index", 1)
        prod_config.save()
    """
    # 调用 get_config() 函数，使用产线控制专用的配置文件名
    return get_config(config_dir, "production_config.json")