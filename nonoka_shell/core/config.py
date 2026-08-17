# -*- coding: utf-8 -*-
"""配置管理（Core 基础实现）：读写 JSON 配置文件。

业务层的完整配置（含默认值合并、插件配置、组件缓存）见 nonoka_shell/config.py；
本模块是 Core 包自包含的最小实现，供无窗口 / 命令行场景使用。
"""
import json
import os
import threading


class Config:
    """JSON 配置文件的读写（线程安全）。"""

    def __init__(self, path):
        self._path = path
        self._lock = threading.RLock()
        self._data = {}
        self.load()

    def load(self):
        with self._lock:
            try:
                if os.path.isfile(self._path):
                    with open(self._path, "r", encoding="utf-8") as f:
                        self._data = json.load(f) or {}
            except Exception:
                self._data = {}
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

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key, value, save=True):
        with self._lock:
            self._data[key] = value
            if save:
                self.save()

    def raw(self):
        with self._lock:
            return dict(self._data)
