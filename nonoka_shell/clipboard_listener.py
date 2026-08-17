# -*- coding: utf-8 -*-
"""剪贴板监听：默认关闭，开启后后台线程轮询剪贴板，检测 B站 / 抖音链接时
通过回调（转发为前端通知）提示用户。用户手动开启，程序退出时停止。

检测正则涵盖：b23.tv / bilibili.com / douyin.com / v.douyin.com。
读取剪贴板优先用 Windows API（避免 Tk 闪烁），失败回退 tkinter。
"""
import re
import threading
import time

from .logger import get_logger

_log = get_logger("clipboard")

_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:b23\.tv|bilibili\.com|douyin\.com|v\.douyin\.com)[^\s]*",
    re.I)


class ClipboardListener:
    def __init__(self, ctx, on_detect=None):
        self.ctx = ctx
        self.on_detect = on_detect  # callable(url)
        self._running = False
        self._thread = None
        self._last = ""

    def _read(self):
        try:
            import win32clipboard
            import win32con
            win32clipboard.OpenClipboard()
            try:
                data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            return data or ""
        except Exception:
            pass
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            try:
                return root.clipboard_get() or ""
            finally:
                root.destroy()
        except Exception:
            return ""

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                text = self._read()
                if text and text != self._last:
                    self._last = text
                    m = _URL_RE.search(text)
                    if m and callable(self.on_detect):
                        try:
                            self.on_detect(m.group(1))
                        except Exception:
                            pass
            except Exception:
                pass
            time.sleep(1.0)
