# -*- coding: utf-8 -*-
"""配置管理：读取/写入 用户文档/NonokaLab/config.json。

配置以 JSON 字典存储，支持嵌套读取。所有读写均带文件锁，避免多实例冲突。
"""
import json
import os
import threading

from .brand import DATA_DIR_NAME, CONFIG_FILE
from .utils import get_data_dir

_DEFAULTS = {
    "last_folder": "",
    "window": {"width": 1100, "height": 740, "maximized": False},
    "plugins": {
        "Nonoka_video_download": {
            "platform": "bilibili",
            "mode": "both",
            "quality": "1080p",
            "bilibili_cookie": "",
            "douyin": {},
            "scale": 2,
        }
    },
    "components": {},          # 组件状态缓存：id -> {"path":..., "version":...}
    "update": {"last_check": 0, "skip_version": ""},
    # ---- 新增设置项 ----
    "theme": "auto",           # light | dark | auto
    "notifications": True,     # 桌面通知开关
    # close_action: 关闭窗口时行为。默认 quit（首次点 X 会询问并记住）。
    # 选项 minimize = 最小化到托盘 / quit = 直接退出软件
    "close_action": "quit",
    "_closing_asked": False,   # 首次 X 是否已询问过（不再重复弹窗）
    "clipboard": False,        # 剪贴板监听（默认关）
    "autostart": False,        # 开机自启（默认关）
    "hotkey": "Ctrl+Shift+N",  # 全局快捷键
    "parallel": 2,             # 任务队列并行数
    "dev_mode": False,         # 开发者模式（默认关）
    "restore_running": False,  # 恢复上次运行的插件（默认关，普通设置可改）
    "auto_restart_crashed": False,  # 插件崩溃后自动重启（默认关）
    "proxy": {"type": "none", "host": "", "port": "", "user": "", "pass": ""},
    "market_url": "",          # 插件市场清单覆盖地址（留空用 brand.MARKET_URL）
}


class Config:
    def __init__(self, path=None):
        self._lock = threading.RLock()
        self._path = path or os.path.join(get_data_dir(), CONFIG_FILE)
        self._data = {}
        self.load()

    # ---------- 加载 / 保存 ----------
    def load(self):
        with self._lock:
            try:
                if os.path.isfile(self._path):
                    with open(self._path, "r", encoding="utf-8") as f:
                        self._data = json.load(f) or {}
            except Exception:
                self._data = {}
            # 合并默认，补全缺失键
            self._data = _deep_merge(dict(_DEFAULTS), self._data)
            self._ensure_dir()

    def save(self):
        with self._lock:
            self._ensure_dir()
            tmp = self._path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self._path)
            except Exception:
                pass

    def _ensure_dir(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
        except Exception:
            pass

    # ---------- 读写接口 ----------
    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key, value, save=True):
        with self._lock:
            self._data[key] = value
            if save:
                self.save()

    def get_plugin(self, plugin_id):
        with self._lock:
            return dict(self._data.setdefault("plugins", {}).get(plugin_id, {}))

    def reset_plugin(self, plugin_id, save=True):
        """恢复插件配置为默认（删除该插件的自定义配置）。"""
        with self._lock:
            self._data.setdefault("plugins", {}).pop(plugin_id, None)
            if save:
                self.save()

    def set_plugin(self, plugin_id, subkey, value, save=True):
        with self._lock:
            pl = self._data.setdefault("plugins", {}).setdefault(plugin_id, {})
            pl[subkey] = value
            if save:
                self.save()

    def get_component(self, cid):
        with self._lock:
            return dict(self._data.get("components", {}).get(cid, {}))

    def set_component(self, cid, info, save=True):
        with self._lock:
            self._data.setdefault("components", {})[cid] = info
            if save:
                self.save()

    def raw(self):
        with self._lock:
            return dict(self._data)


def _deep_merge(base, override):
    """递归合并：以 base 为骨架，override 覆盖同键。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
