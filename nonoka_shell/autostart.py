# -*- coding: utf-8 -*-
"""开机自启：通过 Windows 注册表 Run 键实现（默认关闭）。

所有注册表操作统一走 registry.Registry（集中管理 + DB 记录 + 自清洁，仅 HKCU）。
跨平台回退：Linux 写入 ~/.config/autostart 桌面项；macOS 静默返回失败。
开发模式（未打包）下无法定位 exe，set_enabled(True) 会返回提示。
"""
import os
import sys

from .logger import get_logger
from .registry import Registry, RUN_PATH, AUTOSTART_NAME

_log = get_logger("autostart")

APP_NAME = AUTOSTART_NAME


def _registry():
    # 由 main 注入 db 以便记录；无 db 时 Registry 仍可工作（不记录）
    return Registry(getattr(_registry, "db", None))


def set_db(db):
    _registry.db = db


def is_enabled():
    if sys.platform != "win32":
        return False
    return _registry().is_autostart()


def set_enabled(enable):
    """返回 (ok, msg)。"""
    return _registry().set_autostart(bool(enable))


def cleanup():
    """删除自启键（卸载清理）。"""
    return _registry().delete_key(RUN_PATH, APP_NAME)
