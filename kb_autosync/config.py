"""配置加载：读取 config.json，解析绝对路径，提供全局配置单例。"""
import os
import json

# 工程根目录：kb_autosync/ （即本文件的上两级）
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)

DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")


class Config:
    def __init__(self, data: dict, base_dir: str):
        self._raw = data
        self.base_dir = base_dir
        # paths 中的相对路径以工程根为准
        paths = data.get("paths", {})
        self.db_path = self._abs(paths.get("db", "data/kb.db"))
        self.data_dir = self._abs(paths.get("data_dir", "data"))
        os.makedirs(self.data_dir, exist_ok=True)

    def _abs(self, p: str) -> str:
        return p if os.path.isabs(p) else os.path.join(self.base_dir, p)

    def get(self, *keys, default=None):
        cur = self._raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    @property
    def raw(self):
        return self._raw


_cfg = None


def load(config_path: str = DEFAULT_CONFIG_PATH) -> Config:
    global _cfg
    if _cfg is not None and config_path == DEFAULT_CONFIG_PATH:
        return _cfg
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    base = os.path.dirname(os.path.abspath(config_path))
    _cfg = Config(data, base)
    return _cfg


def get_config() -> Config:
    return _cfg or load()
