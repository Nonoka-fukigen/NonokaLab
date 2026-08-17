# -*- coding: utf-8 -*-
"""全局快捷键：默认 Ctrl+Shift+N 显示主窗口；可禁用或自定义（如 "Ctrl+Alt+S"）。

使用 pynput（跨平台），缺失则静默不启用（不影响其它功能）。
"""
import threading

from .logger import get_logger

_log = get_logger("hotkeys")

DEFAULT_HOTKEY = "<ctrl>+<shift>+n"


class HotkeyManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self._listener = None
        self._combo = None
        self.on_show = None   # 回调：显示 / 创建主窗口（窗口懒创建模式下由 main 注入）
        self.on_f12 = None    # 回调：F12 打开开发者工具（由 main 注入）

    @staticmethod
    def _combo_from_str(s):
        """把 "Ctrl+Shift+N" 之类转成 pynput 组合，如 "<ctrl>+<shift>+n"。"""
        if not s:
            return DEFAULT_HOTKEY
        keymap = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift",
                  "alt": "alt", "win": "cmd", "cmd": "cmd", "super": "cmd",
                  "meta": "cmd"}
        out = []
        for p in s.replace(" ", "").lower().split("+"):
            if p in keymap:
                out.append("<%s>" % keymap[p])
            elif len(p) == 1:
                # 单个字符是普通按键，直接保留字面量（如 "n"），
                # 不能包成 <n>——尖括号只用于特殊键名（如 <ctrl>、<f12>）
                out.append(p)
            else:
                out.append(p)
        return "+".join(out) if out else DEFAULT_HOTKEY

    def start(self, combo=None):
        try:
            from pynput import keyboard
        except Exception as e:
            _log.warning("未安装 pynput，全局快捷键不可用: %s", e)
            return False
        # 空组合视为禁用快捷键
        if not combo:
            self.stop()
            return False
        if self._listener:
            self.stop()
        cfg = None
        try:
            if self.ctx and self.ctx.config:
                cfg = self.ctx.config.get("hotkey")
        except Exception:
            pass
        self._combo = self._combo_from_str(combo or cfg or DEFAULT_HOTKEY)
        # 除显示窗口的组合外，额外注册 F12 打开开发者工具（始终监听该键）
        hotkeys_map = {self._combo: self._on_press, "<f12>": self._on_f12}
        try:
            self._listener = keyboard.GlobalHotKeys(hotkeys_map)
            self._listener.start()
            return True
        except Exception as e:
            _log.warning("全局快捷键启动失败: %s", e)
            self._listener = None
            return False

    def _on_press(self):
        try:
            if callable(self.on_show):
                self.on_show()
                return
            if self.ctx and self.ctx.window is not None:
                self.ctx.window.evaluate_js(
                    "window.NonokaShell && window.NonokaShell.showWindow && window.NonokaShell.showWindow();")
        except Exception:
            pass

    def _on_f12(self):
        """F12：打开开发者工具。优先走 main 注入的 on_f12 回调。"""
        try:
            if callable(self.on_f12):
                self.on_f12()
        except Exception:
            pass

    def stop(self):
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
