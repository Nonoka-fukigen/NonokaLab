# -*- coding: utf-8 -*-
"""桌面通知（跨平台，Windows 优先）。

优先 plyer；缺失时回退 win10toast；再缺失则静默。
所有通知不阻塞主流程；是否启用由配置 notifications 控制（默认开启）。
"""
from .logger import get_logger

_log = get_logger("notifier")


class Notifier:
    def __init__(self, ctx=None):
        self.ctx = ctx
        self._plyer = None
        self._toast = None

    def _enabled(self):
        try:
            if self.ctx and self.ctx.config:
                return bool(self.ctx.config.get("notifications", True))
        except Exception:
            pass
        return True

    def notify(self, title, body="", force=False):
        """弹出桌面通知；force=True 时无视开关。返回是否成功。"""
        if not force and not self._enabled():
            return False
        try:
            if self._notify_plyer(title, body):
                return True
            return self._notify_toast(title, body)
        except Exception as e:
            _log.debug("通知失败: %s", e)
            return False

    def _notify_plyer(self, title, body):
        try:
            if self._plyer is None:
                from plyer import notification as _n
                self._plyer = _n
            if self._plyer is False:
                return False
            self._plyer.notify(title=title, message=body or "",
                               app_name="Nonoka Lab", timeout=4)
            return True
        except Exception:
            self._plyer = False
            return False

    def _notify_toast(self, title, body):
        try:
            if self._toast is None:
                import win10toast
                self._toast = win10toast.ToastNotifier()
            if self._toast is False:
                return False
            self._toast.show_toast(title, body or "", duration=4, threaded=True)
            return True
        except Exception:
            self._toast = False
            return False
