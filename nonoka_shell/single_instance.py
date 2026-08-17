# -*- coding: utf-8 -*-
"""单实例：通过 Windows 命名互斥量（Named Mutex）保证同一时间只运行一个 Nonoka Lab。

跨平台回退：非 Windows 用文件锁模拟。返回是否成功取得实例锁。
"""
import os
import sys

from .brand import APP_ID
from .logger import get_logger

_log = get_logger("single_instance")

_mutex = None          # 句柄，持有即代表本进程是唯一实例
_flock = None


def acquire():
    """尝试取得单实例锁。返回 True 表示本进程成为唯一实例。"""
    global _mutex, _flock
    if sys.platform == "win32":
        return _acquire_win()
    return _acquire_posix()


def _acquire_win():
    global _mutex
    import ctypes
    import ctypes.wintypes
    # 全局命名空间互斥量，名称需加 Global\\ 前缀以覆盖远程桌面会话
    name = "Global\\" + APP_ID.replace(".", "_") + "_single_instance"
    kernel32 = ctypes.windll.kernel32
    _mutex = kernel32.CreateMutexW(None, 0, name)
    err = ctypes.GetLastError()
    if err == 183:  # ERROR_ALREADY_EXISTS
        _mutex = None
        _log.warning("已有 Nonoka Lab 实例在运行，退出本进程。")
        return False
    return True


def _acquire_posix():
    global _flock
    lock_path = os.path.join(os.path.expanduser("~"), ".nonoka_lab.lock")
    try:
        import fcntl
        _flock = open(lock_path, "w")
        fcntl.flock(_flock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except Exception:
        _flock = None
        _log.warning("已有 Nonoka Lab 实例在运行（文件锁），退出本进程。")
        return False


def release():
    global _mutex, _flock
    if _mutex is not None and sys.platform == "win32":
        try:
            ctypes.windll.kernel32.ReleaseMutex(_mutex)
        except Exception:
            pass
        _mutex = None
    if _flock is not None:
        try:
            _flock.close()
        except Exception:
            pass
        _flock = None


def focus_existing():
    """尽力把已运行的实例窗口提到前台（仅 Windows）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.user32.FindWindowW(None, "Nonoka Lab")
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
