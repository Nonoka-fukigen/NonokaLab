# -*- coding: utf-8 -*-
"""系统托盘：左键显示主窗口；右键菜单：显示主窗口 / 停止全部插件 / 退出。

关闭窗口默认最小化到托盘（不退出），由 main 在窗口 closing 事件中处理；
完全退出时由 main 调用 stop_all_plugins 再退出。

使用 pystray（跨平台），缺失时静默降级（无托盘，不影响其它功能）。
"""
import os
import threading

from .logger import get_logger

_log = get_logger("tray")


class Tray:
    def __init__(self, ctx, on_show=None, on_settings=None, on_stop_all=None, on_quit=None):
        self.ctx = ctx
        self.on_show = on_show
        self.on_settings = on_settings
        self.on_stop_all = on_stop_all
        self.on_quit = on_quit
        self._icon = None
        self._thread = None

    def available(self):
        try:
            import pystray  # noqa
            return True
        except Exception:
            return False

    def start(self):
        if self._icon or not self.available():
            return False
        try:
            import pystray
            from PIL import Image
        except Exception as e:
            _log.warning("未安装 pystray/PIL，托盘不可用: %s", e)
            return False
        # 菜单项：显示主窗口（默认项 = 双击/回车触发）+ 设置 + 停止全部插件 + 退出
        # Windows 托盘单击左键默认弹菜单；双击默认项 / 直接点击该项 触发弹窗。
        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", lambda: self._safe(self.on_show), default=True),
            pystray.MenuItem("设置...", lambda: self._safe(self.on_settings)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("停止全部插件", lambda: self._safe(self.on_stop_all)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self._safe(self.on_quit)),
        )
        self._icon = pystray.Icon("NonokaLab", self._make_image(), "Nonoka Lab", menu)
        # 左键单击托盘图标直接触发弹窗（依赖 pystray 版本/后端；最坏情况会先弹菜单）
        self._icon.on_click = lambda icon, event: self._safe(self.on_show)
        # run_detached 在 Windows 上更稳定：独立消息循环，避免与 pywebview 主循环冲突导致图标被移除。
        if hasattr(self._icon, "run_detached"):
            self._icon.run_detached()
        else:
            self._thread = threading.Thread(target=self._icon.run, daemon=True)
            self._thread.start()
        return True

    def _make_image(self):
        from PIL import Image
        return Image.new("RGB", (64, 64), (255, 187, 204))

    def _safe(self, cb):
        try:
            if callable(cb):
                cb()
        except Exception:
            pass

    def stop(self):
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
