# -*- coding: utf-8 -*-
"""全局错误捕获：未捕获异常记录到日志，并尽量通过 WebView 弹出错误提示。"""
import sys
import traceback

from .logger import get_logger

_log = get_logger("error_handler")


def install(window=None, on_crash=None):
    """安装 sys.excepthook。window 为 pywebview 窗口对象（可稍后通过 set_window 设置）。
    on_crash(exc_type, exc, tb)：可选回调，用于生成崩溃报告 / 弹出上报提示。
    """
    state = {"window": window}

    def hook(exc_type, exc, tb):
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        _log.error("未捕获异常:\n" + msg)
        if callable(on_crash):
            try:
                on_crash(exc_type, exc, tb)
            except Exception:
                pass
        _notify(state["window"], "程序发生错误", msg)

    sys.excepthook = hook

    def set_window(w):
        state["window"] = w

    return set_window


def _notify(window, title, detail):
    if window is None:
        return
    try:
        import json
        snippet = (
            "window.NonokaShell && window.NonokaShell.showError && "
            f"window.NonokaShell.showError({json.dumps(title)}, {json.dumps(detail)});"
        )
        window.evaluate_js(snippet)
    except Exception:
        pass
