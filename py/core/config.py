"""
配置管理
========
统一管理 JSON 配置文件的读写，自动创建目录。
"""
import json
import os
from pathlib import Path


class ConfigManager:
    """配置管理器"""

    def __init__(self, config_dir: str = "./config", filename: str = "camera_config.json"):
        """
        参数:
            config_dir: 配置文件目录（相对或绝对路径）
            filename: 配置文件名
        """
        self.config_path = Path(config_dir) / filename
        # 确保目录存在
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        # 默认配置
        self._defaults = {
            "model_path": "",
            "camera_index": 0,
            "conf_threshold": 0.5,
            "iou_threshold": 0.45,
            "window_geometry": None,
            "sidebar_expanded": True,
        }

        self._config = self._load()

    def _load(self) -> dict:
        """加载配置，不存在则返回默认值"""
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 合并默认值（兼容旧版本配置文件）
                merged = self._defaults.copy()
                merged.update(data)
                return merged
            except (json.JSONDecodeError, OSError):
                pass
        return self._defaults.copy()

    def save(self) -> bool:
        """保存配置到文件"""
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self._config, f, ensure_ascii=False, indent=2)
            return True
        except OSError:
            return False

    def get(self, key: str, default=None):
        """获取配置值"""
        return self._config.get(key, default)

    def set(self, key: str, value) -> None:
        """设置配置值"""
        self._config[key] = value

    def update(self, data: dict) -> None:
        """批量更新"""
        self._config.update(data)

    def all(self) -> dict:
        """获取完整配置副本"""
        return self._config.copy()


# 全局单例
_config_instance: ConfigManager | None = None


def get_config(config_dir: str = "./config", filename: str = "camera_config.json") -> ConfigManager:
    """获取全局配置实例（单例模式）"""
    global _config_instance
    if _config_instance is None:
        _config_instance = ConfigManager(config_dir, filename)
    return _config_instance