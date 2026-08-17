# -*- coding: utf-8 -*-
"""Nonoka_video_download 插件：接口层（净化版）。

职责仅限：
  - 插件元数据（id / name / icon / description）
  - 生命周期方法（on_load / on_start / on_stop / on_pause / on_resume / on_unload）
  - 暴露给前端的 RPC 方法（api_methods）

所有业务逻辑（下载 / 解析 / 封面 / 扫码 / ffmpeg / 网络 / 文件）均在 executor.py 中，
本文件不包含任何 subprocess / os.system / requests 调用。
"""
import json
import os
import threading
import time
import uuid

from executor import Executor
from nonoka_shell.plugin_manager import NonokaPlugin
from nonoka_shell.utils import run_in_thread
from nonoka_shell.logger import get_logger

_log = get_logger("Nonoka_video_download")


class Plugin(NonokaPlugin):
    id = "Nonoka_video_download"
    name = "视频下载"
    icon = "video"
    description = "下载 B站 / 抖音 的视频、音频与封面，支持清晰度增强"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._executor = Executor(self._deliver, self.id)
        self._qr_token = 0
        self._qr_lock = threading.Lock()

    # ----------------------- 声明 -----------------------
    def api_methods(self):
        return ["parse", "download", "cover", "get_status", "pick_folder",
                "get_config", "set_config", "qr_generate", "open_folder"]

    def frontend_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "frontend", "index.html")

    # ----------------------- 事件推送 -----------------------
    def _deliver(self, event_name, data):
        """把 executor 的事件（如 "Nonoka_video_download.progress"）推回 UI。"""
        typ = event_name.rsplit(".", 1)[-1] if "." in event_name else event_name
        if self.ctx.window is None:
            return
        try:
            evt = dict(data or {})
            evt["type"] = typ
            snippet = (
                "window.NonokaShell && window.NonokaShell.deliverToPlugin(%s, %s);"
                % (json.dumps(self.id), json.dumps(evt, ensure_ascii=False))
            )
            self.ctx.window.evaluate_js(snippet)
        except Exception as e:
            _log.debug("emit 失败: %s", e)

    # ----------------------- 生命周期 -----------------------
    def on_load(self):
        try:
            self.ctx.components.ensure_on_path("ffmpeg")
        except Exception:
            pass

    def on_start(self):
        _log.info("插件 %s 已启动（运行中）", self.id)

    def on_stop(self):
        _log.info("插件 %s 已停止", self.id)

    def on_pause(self):
        _log.info("插件 %s 已暂停", self.id)

    def on_resume(self):
        _log.info("插件 %s 已继续", self.id)

    def on_unload(self):
        # 终止扫码轮询线程
        with self._qr_lock:
            self._qr_token += 1

    # ----------------------- 解析 -----------------------
    def parse(self, payload):
        self._executor.parse(payload or {})
        return {"status": "ok", "data": {}}

    # ----------------------- 下载（进入任务队列） -----------------------
    def download(self, payload):
        payload = payload or {}
        pid, kind = self.id, "download"
        url = (payload.get("url") or "").strip()
        title = (url.split("?")[0][-48:] or kind)

        def on_complete(ok, ctx):
            if ok and self.ctx.db is not None:
                try:
                    self.ctx.db.add_download(
                        ctx.get("url"), ctx.get("title"),
                        ctx.get("type") or "unknown", ctx.get("folder") or "")
                except Exception as e:
                    _log.warning("写下载历史失败: %s", e)

        def runner(task):
            self._executor.download(payload, task=task, on_complete=on_complete)

        if self.ctx.queue is not None:
            t = self.ctx.queue.submit(pid, kind, title, payload, runner)
            return {"status": "ok", "data": {"task_id": t.id}}
        run_in_thread(runner, None)
        return {"status": "ok", "data": {}}

    # ----------------------- 封面（进入任务队列） -----------------------
    def cover(self, payload):
        payload = payload or {}
        pid, kind = self.id, "cover"
        url = (payload.get("url") or "").strip()
        title = (url.split("?")[0][-48:] or kind)

        def on_complete(ok, ctx):
            if ok and self.ctx.db is not None:
                try:
                    self.ctx.db.add_download(
                        ctx.get("url"), ctx.get("title"),
                        ctx.get("type") or "unknown", ctx.get("folder") or "")
                except Exception as e:
                    _log.warning("写下载历史失败: %s", e)

        def runner(task):
            self._executor.cover(payload, task=task, on_complete=on_complete)

        if self.ctx.queue is not None:
            t = self.ctx.queue.submit(pid, kind, title, payload, runner)
            return {"status": "ok", "data": {"task_id": t.id}}
        run_in_thread(runner, None)
        return {"status": "ok", "data": {}}

    # ----------------------- 状态 / 文件夹 / 配置 -----------------------
    def get_status(self):
        return {"status": "ok", "data": self._executor.get_status()}

    def pick_folder(self, initial_dir=None):
        """弹出系统文件夹选择框。

        优先用 pywebview 原生 create_file_dialog（WebView2 系统对话框，主窗口线程，
        最可靠）；失败回退到 shell 层 utils.pick_folder（子进程 SHBrowseForFolder）。
        返回路径字符串或 None。
        """
        # 1. pywebview 原生对话框（推荐）
        try:
            if self.ctx.window is not None:
                import webview
                result = self.ctx.window.create_file_dialog(
                    webview.FOLDER_DIALOG,
                    directory=initial_dir or "",
                    allow_multiple=False)
                if result:
                    if isinstance(result, (list, tuple)):
                        return result[0] if result else None
                    return result
        except Exception as e:
            _log.debug("pywebview 文件夹对话框失败，回退子进程: %s", e)
        # 2. 回退：shell 层 utils.pick_folder（folderpicker.py 原生 + tkinter）
        try:
            from nonoka_shell.utils import pick_folder as _pick
            return _pick(initial_dir or None)
        except Exception as e:
            _log.debug("pick_folder 回退失败: %s", e)
            return None

    def open_folder(self, path):
        """在系统资源管理器中打开文件夹（返回布尔值）"""
        import sys
        if not path or not os.path.exists(path):
            return False
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.run(["open", path], check=True)
            else:
                import subprocess
                subprocess.run(["xdg-open", path], check=True)
            return True
        except Exception:
            return False

    def get_config(self):
        return {"status": "ok", "data": self.ctx.config.get_plugin(self.id)}

    def set_config(self, subkey, value):
        self.ctx.config.set_plugin(self.id, subkey, value)
        return {"status": "ok", "data": True}

    # ----------------------- B站 扫码登录（后端轮询 + 事件推送） -----------------------
    def qr_generate(self):
        try:
            res = self._executor.qr_generate()
            if res.get("error"):
                return {"status": "ok", "data": {"error": res["error"]}}
            with self._qr_lock:
                self._qr_token += 1
                token = self._qr_token
            run_in_thread(self._qr_poll_loop, res["key"], token)
            return {"status": "ok", "data": {"key": res["key"], "image": res["image"]}}
        except Exception as e:
            return {"status": "ok", "data": {"error": str(e)}}

    def _qr_poll_loop(self, key, token):
        while True:
            with self._qr_lock:
                if token != self._qr_token:
                    return  # 已被新二维码取代
            res = self._executor.qr_poll(key)
            if res.get("code") == 0:
                self._deliver(self.id + ".qr_success", {"cookie": res.get("cookie", "")})
                return
            if res.get("code") == 86038:
                self._deliver(self.id + ".qr_expired", {})
                return
            if res.get("error"):
                self._deliver(self.id + ".qr_error", {"message": res["error"]})
                return
            time.sleep(2)