# -*- coding: utf-8 -*-
"""前端桥接对象（pywebview 的 js_api）。

挂载为 window.pywebview.api：
  - 系统方法（见各方法注释）
  - 插件代理：以插件 id 为属性，例如 window.pywebview.api.Nonoka_video_download.download(...)
Python -> 前端 的事件推送通过 window.evaluate_js 调用 shell 上注册的全局函数。
"""
import csv
import io
import json
import os
import uuid

from .brand import (
    APP_NAME, APP_ID, PUBLISHER, VERSION, THEME_PRIMARY, THEME_PRIMARY_DARK,
    THEME_PRIMARY_SOFT, REPO_URL,
)
from .logger import get_logger
from .utils import open_folder, pick_folder, pick_file, get_data_dir
from .utils import run_in_thread
from . import i18n
from . import feedback
from . import crash_report
from . import autostart
from . import backup

_log = get_logger("bridge")


class NonokaBridge:
    def __init__(self, ctx):
        self.ctx = ctx
        self.window = None
        for pid, inst in ctx.plugins.items():
            setattr(self, pid, ctx.plugin_manager.proxy(pid))

    # ----------------------- 窗口 / 上下文 -----------------------
    def set_window(self, window):
        self.window = window
        # 防止 pywebview 生成 JS 桥时递归遍历 window.native(WinForms) 属性图，
        # 触发 AccessibilityObject.Bounds 无限递归（RecursionError / 启动卡死）。
        try:
            window._serializable = False
        except Exception:
            pass

    def _emit_shell(self, fn, payload):
        if self.window is None:
            return
        # 等待 WebView2 完成加载后再注入（否则后台线程触发 evaluate_js 会跨线程访问
        # CoreWebView2Controller 触发 COM 异常 / pywebview native 属性递归 bug）。
        ready = bool(getattr(self.ctx, "window_ready", False))
        if not ready:
            return  # 静默丢弃，loaded 后由前端主动拉取或 on_loaded 重发
        try:
            snippet = (f"window.NonokaShell && window.NonokaShell.{fn} && "
                       f"window.NonokaShell.{fn}({json.dumps(payload, ensure_ascii=False)});")
            self.window.evaluate_js(snippet)
        except Exception as e:
            _log.debug("emit_shell 失败: %s", e)

    # ----------------------- 系统信息 -----------------------
    def get_brand(self):
        return {
            "app_name": APP_NAME, "app_id": APP_ID, "publisher": PUBLISHER,
            "version": VERSION, "theme_primary": THEME_PRIMARY,
            "theme_primary_dark": THEME_PRIMARY_DARK,
            "theme_primary_soft": THEME_PRIMARY_SOFT, "repo_url": REPO_URL,
        }

    def get_plugins(self):
        return self.ctx.plugin_manager.list_meta()

    def get_config(self):
        return self.ctx.config.raw()

    def set_config(self, key, value):
        self.ctx.config.set(key, value)
        return True

    def set_plugin_config(self, plugin_id, subkey, value):
        self.ctx.config.set_plugin(plugin_id, subkey, value)
        return True

    def set_plugin_enabled(self, plugin_id, enabled):
        return self.ctx.plugin_manager.set_enabled(plugin_id, bool(enabled))

    # ----------------------- 多语言 -----------------------
    def get_locales(self):
        return i18n.all_locales()

    def get_locale(self):
        return i18n.get_locale()

    def set_locale(self, locale):
        i18n.set_locale(locale)
        try:
            self.ctx.config.set("locale", locale)
        except Exception:
            pass
        return i18n.get_locale()

    def locale_list(self):
        return i18n.locale_list()

    # ----------------------- 通用设置项（细分读写） -----------------------
    def get_setting(self, key, default=None):
        return self.ctx.config.get(key, default)

    def set_setting(self, key, value):
        self.ctx.config.set(key, value)
        self._apply_setting(key, value)
        return self.ctx.config.get(key, value)

    def _apply_setting(self, key, value):
        try:
            if key == "theme":
                self._emit_shell("onTheme", {"mode": value})
            elif key == "clipboard":
                cl = getattr(self.ctx, "clipboard", None)
                if cl is not None:
                    if value:
                        cl.start()
                    else:
                        cl.stop()
            elif key == "autostart":
                ok, msg = autostart.set_enabled(bool(value))
                if not ok:
                    _log.warning("设置开机自启失败: %s", msg)
            elif key == "hotkey":
                hk = getattr(self.ctx, "hotkeys", None)
                if hk is not None:
                    hk.start(value)
            elif key == "parallel":
                q = getattr(self.ctx, "queue", None)
                if q is not None:
                    q.set_max_parallel(value)
            elif key == "dev_mode":
                self.ctx.plugin_manager.rescan(scan_dev=bool(value))
                self._emit_shell("onPluginsChanged", self.ctx.plugin_manager.list_meta())
        except Exception as e:
            _log.warning("应用设置 %s 失败: %s", key, e)

    def get_theme(self):
        return self.ctx.config.get("theme", "auto")

    def set_theme(self, mode):
        return self.set_setting("theme", mode)

    def get_proxy(self):
        return self.ctx.config.get("proxy", {"type": "none", "host": "", "port": "", "user": "", "pass": ""})

    def set_proxy(self, obj):
        return self.set_setting("proxy", obj or {"type": "none"})

    def test_proxy(self, obj):
        import requests
        obj = obj or {}
        ptype = (obj.get("type") or "none")
        if ptype == "none":
            return {"ok": True, "msg": "无代理，直连可用"}
        host = (obj.get("host") or "").strip()
        port = (obj.get("port") or "").strip()
        if not host or not port:
            return {"ok": False, "error": "请填写代理地址与端口"}
        auth = ""
        if obj.get("user"):
            auth = "%s:%s@" % (obj["user"], obj.get("pass", ""))
        if ptype == "http":
            proxies = {"http": "http://%s%s:%s" % (auth, host, port),
                       "https": "http://%s%s:%s" % (auth, host, port)}
        else:  # socks5
            proxies = {"http": "socks5://%s%s:%s" % (auth, host, port),
                       "https": "socks5://%s%s:%s" % (auth, host, port)}
        try:
            r = requests.get("https://api.github.com", proxies=proxies, timeout=8)
            return {"ok": r.status_code < 500, "status": r.status_code}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_notifications(self):
        return bool(self.ctx.config.get("notifications", True))

    def set_notifications(self, v):
        return self.set_setting("notifications", bool(v))

    def get_clipboard(self):
        return bool(self.ctx.config.get("clipboard", False))

    def set_clipboard(self, v):
        return self.set_setting("clipboard", bool(v))

    def get_autostart(self):
        return bool(autostart.is_enabled())

    def set_autostart(self, v):
        self.set_setting("autostart", bool(v))
        return bool(autostart.is_enabled())

    def get_hotkey(self):
        return self.ctx.config.get("hotkey", "Ctrl+Shift+N")

    def set_hotkey(self, v):
        return self.set_setting("hotkey", v or "Ctrl+Shift+N")

    def get_parallel(self):
        return int(self.ctx.config.get("parallel", 2) or 2)

    def set_parallel(self, n):
        return self.set_setting("parallel", int(n or 2))

    def get_minimize_tray(self):
        return bool(self.ctx.config.get("minimize_to_tray", True))

    def set_minimize_tray(self, v):
        return self.set_setting("minimize_to_tray", bool(v))

    def get_dev_mode(self):
        return bool(self.ctx.config.get("dev_mode", False))

    def set_dev_mode(self, v):
        from .logger import set_debug
        ok = self.set_setting("dev_mode", bool(v))
        set_debug(bool(v))  # DEBUG 日志仅在开发者模式开启
        return ok

    # ----------------------- 数据库 / 历史 -----------------------
    def get_download_history(self, limit=100):
        if self.ctx.db is None:
            return []
        return self.ctx.db.get_downloads(limit)

    def download_history_search(self, q):
        if self.ctx.db is None:
            return []
        return self.ctx.db.search_downloads(q or "")

    def delete_history(self, did):
        if self.ctx.db is not None:
            self.ctx.db.delete_download(did)
        return True

    def clear_download_history(self):
        if self.ctx.db is not None:
            self.ctx.db.clear_downloads()
        return True

    def redownload(self, did):
        if self.ctx.db is None:
            return {"ok": False, "error": "no_db"}
        row = self.ctx.db.get_download(did)
        if not row:
            return {"ok": False, "error": "not_found"}
        pid = "Nonoka_video_download"
        inst = self.ctx.plugin_manager.get(pid)
        if inst is None or self.ctx.plugin_manager.state(pid) != "running":
            return {"ok": False, "error": i18n.t("plugin_not_running")}
        url = row.get("url") or ""
        mode = row.get("download_type") or "both"
        folder = row.get("save_path") or ""
        platform = "douyin" if "douyin" in url.lower() else "bilibili"
        if mode not in ("video", "audio", "both", "cover"):
            mode = "both"
        return inst.download({"platform": platform, "url": url, "mode": mode, "folder": folder})

    def export_history_csv(self):
        if self.ctx.db is None:
            return {"ok": False, "error": "no_db"}
        rows = self.ctx.db.get_downloads(limit=100000)
        out_dir = os.path.join(get_data_dir(), "exports")
        os.makedirs(out_dir, exist_ok=True)
        ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(out_dir, "download_history_%s.csv" % ts)
        try:
            with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["id", "url", "title", "type", "save_path", "created_at"])
                for r in rows:
                    w.writerow([r.get("id"), r.get("url"), r.get("title"),
                               r.get("download_type"), r.get("save_path"), r.get("created_at")])
            return {"ok": True, "path": path, "count": len(rows)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_database_stats(self):
        if self.ctx.db is None:
            return {"downloads": 0, "plugins": 0, "path": ""}
        return self.ctx.db.stats()

    # ----------------------- 使用统计 -----------------------
    def get_stats(self):
        if self.ctx.stats is not None:
            return self.ctx.stats.collect()
        return {"downloads": 0, "by_type": {}, "days": 0, "plugins_enabled": 0}

    # ----------------------- 数据备份 / 恢复 -----------------------
    def export_data(self, dest_dir):
        dest_dir = (dest_dir or "").strip()
        if not dest_dir or not os.path.isdir(dest_dir):
            return {"ok": False, "error": "invalid_dir"}
        ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(dest_dir, "nonoka_backup_%s.zip" % ts)
        return backup.export_data(path)

    def import_data(self, path):
        if not path or not os.path.isfile(path):
            return {"ok": False, "error": "invalid_file"}
        res = backup.import_data(path)
        if res.get("ok"):
            try:
                self.ctx.config.load()
            except Exception:
                pass
            try:
                self.ctx.plugin_manager.rescan(scan_dev=bool(self.ctx.config.get("dev_mode", False)))
            except Exception:
                pass
        return res

    # ----------------------- 组件管理 -----------------------
    def get_components(self):
        return self.ctx.components.list_all()

    def get_component_status(self, cid):
        return self.ctx.components.get_status(cid)

    def install_component(self, cid):
        tid = uuid.uuid4().hex
        run_in_thread(self._install_worker, tid, cid)
        return {"task_id": tid}

    def _install_worker(self, tid, cid):
        def emit(event):
            event = dict(event)
            event["task_id"] = tid
            event["id"] = cid
            self._emit_shell("onComponentEvent", event)

        def log_cb(m):
            emit({"type": "log", "message": m})

        def prog_cb(label, fetched, total):
            pct = (fetched * 100 // total) if total else 0
            emit({"type": "progress", "label": label, "fetched": fetched,
                  "total": total, "percent": pct})

        emit({"type": "start"})
        res = self.ctx.components.download(cid, log_cb=log_cb, progress_cb=prog_cb)
        emit({"type": "done", "ok": res.get("ok"), "path": res.get("path"),
              "error": res.get("error")})
        run_in_thread(lambda: self._emit_shell("onComponentsChanged",
                                                self.ctx.components.list_all()))

    # ----------------------- 插件：市场 / 安装 / 卸载 / 更新 -----------------------
    def get_plugin_market(self):
        return self.ctx.plugin_manager.get_market()

    def install_plugin(self, entry):
        tid = uuid.uuid4().hex
        entry = entry or {}
        run_in_thread(self._plugin_market_worker, tid, entry, "install")
        return {"task_id": tid}

    def update_plugin(self, pid):
        tid = uuid.uuid4().hex
        run_in_thread(self._plugin_market_worker, tid, pid, "update")
        return {"task_id": tid}

    def _plugin_market_worker(self, tid, arg, kind):
        def cb(evt):
            evt = dict(evt)
            evt["task_id"] = tid
            evt["kind"] = kind
            self._emit_shell("onPluginEvent", evt)
        if kind == "install":
            self.ctx.plugin_manager.install_plugin(arg, cb)
        else:
            self.ctx.plugin_manager.update_plugin(arg, cb)
        run_in_thread(lambda: self._emit_shell("onPluginsChanged",
                                                self.ctx.plugin_manager.list_meta()))

    def uninstall_plugin(self, pid, force=False, delete_data=False):
        return self.ctx.plugin_manager.uninstall_plugin(
            pid, force=bool(force), delete_data=bool(delete_data))

    def delete_plugin_data(self, pid):
        """卸载时用户选择「是」→ 删除该插件全部数据（plugins_data/<id>）。"""
        return self.ctx.plugin_manager.delete_plugin_data(pid)

    def has_plugin_data(self, pid):
        return self.ctx.plugin_manager.has_plugin_data(pid)

    def get_event_trace(self, limit=200):
        """事件追踪面板（开发者模式）：返回 EventBus 最近事件记录。"""
        try:
            bus = self.ctx.plugin_manager.bus
            raw = bus.trace(limit=limit)
            out = []
            for ts, trace_id, topic, data in raw:
                out.append({
                    "time": ts, "trace_id": trace_id, "topic": topic,
                    "data": str(data)[:200],
                })
            return out
        except Exception:
            return []

    def get_console_logs(self, from_seq=0, limit=300):
        """控制台：返回 Python 日志记录（增量）。含实时订阅，新日志由 onConsoleLog 推送。"""
        from . import logger as _lg
        try:
            records, last_seq = _lg.get_console(from_seq=from_seq or 0, limit=limit)
            return {"records": records, "last_seq": last_seq}
        except Exception:
            return {"records": [], "last_seq": from_seq or 0}

    def open_devtools(self):
        """兼容别名：开发者工具以新命名 open_debug_tools 提供。"""
        return self.open_debug_tools()

    def open_debug_tools(self):
        """打开 WebView2 开发者工具（设置页「打开开发者工具」按钮 / F12 触发）。

        CoreWebView2 只能在 UI 线程访问；js_api / 热键回调都在后台线程执行，
        因此必须用 WebView2 控件的 Invoke 把访问调度回 UI 线程，否则抛
        "CoreWebView2 can only be accessed from the UI thread"。
        该入口对任意线程调用均安全（Invoke 内部切回 UI 线程）。
        """
        try:
            window = self.window
            if window is None:
                return {"ok": False, "error": "no_window"}
            native = getattr(window, "native", None)
            browser = getattr(native, "browser", None)
            wv = getattr(browser, "webview", None)
            if wv is None:
                return {"ok": False, "error": "no_webview"}
            from System import Action  # noqa: E402  (pythonnet，pywebview 已加载 clr)
            out = {}

            def _do():
                try:
                    core = wv.CoreWebView2  # 必须在 UI 线程访问
                    if core is None:
                        out["error"] = "webview_not_ready"
                        return
                    try:
                        core.Settings.AreDevToolsEnabled = True
                    except Exception:
                        pass
                    core.OpenDevToolsWindow()
                    out["ok"] = True
                except Exception as e:  # noqa: BLE001
                    out["error"] = str(e)

            try:
                wv.Invoke(Action(_do))
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": "invoke_failed: %s" % e}
            if out.get("ok"):
                _log.info("已打开开发者工具")
                return {"ok": True}
            return {"ok": False, "error": out.get("error", "unknown")}
        except Exception as e:  # noqa: BLE001
            _log.warning("打开开发者工具失败: %s", e)
            return {"ok": False, "error": str(e)}

    def restore_base_settings(self):
        """恢复基础插件参数为默认（开发者模式按钮，仅影响 system 插件配置）。"""
        try:
            pm = self.ctx.plugin_manager
            for pid, m in list(pm.meta.items()):
                if m.get("system"):
                    self.ctx.config.reset_plugin(pid)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_health(self):
        """各插件健康状态：🟢 ok / 🟡 warn / 🔴 dead（心跳 30s 判定）。"""
        out = {}
        for pid in self.ctx.plugin_manager._states:
            out[pid] = self.ctx.plugin_manager.health(pid)
        return out

    # ----------------------- 插件：生命周期 -----------------------
    def get_state(self, pid):
        return self.ctx.plugin_manager.state(pid)

    # 别名：插件前端统一调用 get_plugin_state；保留 get_state 兼容旧调用方。
    # 返回带崩溃原因的完整状态对象，供 Shell 统一状态同步使用。
    def get_plugin_state(self, pid):
        pm = self.ctx.plugin_manager
        return {
            "state": pm.state(pid),
            "crashed": pm.state(pid) == "crashed",
            "crash_reason": pm.crash_reason(pid) if hasattr(pm, "crash_reason") else "",
        }

    def invoke_plugin(self, pid, method, args=None):
        """顶层插件方法派发：`invoke_plugin(pid, method, args)`。

        pywebview 对「嵌套对象的 js_api 方法」暴露在部分版本不可靠（bridge 捕获到空
        代理 / 未把嵌套方法暴露成 JS 函数），导致 `api.<plugin>.<method>` 报 "no method"。
        该顶层方法绕过嵌套代理，按 pid+method 从 plugin_manager 拿实时代理并直接调用。
        """
        try:
            if not self.ctx.plugin_manager.is_compatible(pid):
                return {"ok": False, "error": "incompatible"}
            if self.ctx.plugin_manager.state(pid) != "running":
                return {"ok": False, "error": i18n.t("plugin_not_running")}
            proxy = self.ctx.plugin_manager.proxy(pid)
            fn = getattr(proxy, method, None) if proxy is not None else None
            if not callable(fn):
                return {"ok": False, "error": "no method: %s" % method}
            return fn(*(list(args or [])))
        except Exception as e:  # noqa: BLE001
            _log.warning("invoke_plugin %s.%s 异常: %s", pid, method, e)
            return {"ok": False, "error": str(e)}

    def start_plugin(self, pid):
        return self.ctx.plugin_manager.start_plugin(pid)

    def stop_plugin(self, pid):
        return self.ctx.plugin_manager.stop_plugin(pid)

    def pause_plugin(self, pid):
        return self.ctx.plugin_manager.pause_plugin(pid)

    def resume_plugin(self, pid):
        return self.ctx.plugin_manager.resume_plugin(pid)

    def restart_plugin(self, pid):
        """崩溃 / 停止后重启插件。"""
        return self.ctx.plugin_manager.restart_plugin(pid)

    def rescan_plugins(self):
        self.ctx.plugin_manager.rescan(scan_dev=bool(self.ctx.config.get("dev_mode", False)))
        return True

    def reload_plugin(self, pid):
        return self.ctx.plugin_manager.reload_plugin(pid)

    def get_plugin_log(self, pid, lines=200):
        path = os.path.join(get_data_dir(), "logs", "nonoka.log")
        try:
            with open(path, "r", encoding="utf-8") as f:
                all_lines = f.read().splitlines()
            kw = (pid or "").replace(".", "_")
            hit = [l for l in all_lines if kw in l]
            return "\n".join(hit[-lines:])
        except Exception:
            return ""

    # ----------------------- 任务队列 -----------------------
    def get_queue(self):
        if self.ctx.queue is not None:
            return self.ctx.queue.list()
        return []

    def queue_action(self, action, task_id=None):
        q = self.ctx.queue
        if q is None:
            return False
        if action == "cancel" and task_id:
            return q.cancel(task_id)
        if action == "move_up" and task_id:
            return q.move(task_id, "up")
        if action == "move_down" and task_id:
            return q.move(task_id, "down")
        if action == "pause":
            q.pause()
            return True
        if action == "resume":
            q.resume()
            return True
        return False

    # ----------------------- 剪贴板检测回传 -----------------------
    def on_clipboard_detect(self, url):
        self._emit_shell("onClipboardDetect", {"url": url})
        if self.ctx.notifier is not None:
            self.ctx.notifier.notify(i18n.t("clipboard_detected"), url)

    # ----------------------- 文件夹 / 外部 -----------------------
    def open_external_folder(self, path):
        return open_folder(path)

    def open_external_url(self, url):
        """用系统默认浏览器打开外部链接（WebView2 内 window.open 不可靠）。"""
        import webbrowser
        try:
            webbrowser.open(url)
            return {"ok": True, "url": url}
        except Exception as e:  # noqa: BLE001
            _log.warning("打开外部链接失败: %s", e)
            return {"ok": False, "url": url, "error": str(e)}

    def open_log_folder(self):
        d = os.path.join(get_data_dir(), "logs")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return open_folder(d)

    def pick_folder(self):
        return pick_folder()

    def pick_file(self):
        return pick_file()

    # ----------------------- 更新 -----------------------
    def check_update(self):
        return self.ctx.updater.check()

    def download_update(self):
        tid = uuid.uuid4().hex

        def cb(evt):
            evt = dict(evt)
            evt["task_id"] = tid
            self._emit_shell("onUpdateEvent", evt)

        self.ctx.updater.download_update(cb)
        return {"task_id": tid}

    # ----------------------- 桌面通知 -----------------------
    def notify(self, title, body=""):
        if self.ctx.notifier is not None:
            self.ctx.notifier.notify(title, body or "", force=True)
        return True

    # ----------------------- 崩溃上报 -----------------------
    def open_crash_issue(self, include_logs=False):
        rep = crash_report.read_last()
        if not rep:
            return {"url": None}
        body = crash_report.build_issue_body(rep, include_logs=bool(include_logs))
        url = feedback.open_crash_issue(body)
        return {"url": url}

    # ----------------------- 首次引导 -----------------------
    def get_welcome_done(self):
        try:
            return bool(self.ctx.config.get("welcome_done", False))
        except Exception:
            return False

    def set_welcome_done(self, done=True):
        try:
            self.ctx.config.set("welcome_done", bool(done))
        except Exception:
            pass
        return True

    # ----------------------- 窗口控制（供托盘菜单跨线程安全调用） -----------------------
    def request_show(self):
        try:
            if self.window is not None:
                self.window.show()
        except Exception:
            pass
        return True

    def stop_all_plugins(self):
        try:
            self.ctx.plugin_manager.stop_all()
        except Exception:
            pass
        return True

    def request_quit(self):
        try:
            self.ctx._quitting = True
        except Exception:
            pass
        for mgr in ("clipboard", "hotkeys", "tray"):
            try:
                getattr(self.ctx, mgr, None).stop()
            except Exception:
                pass
        try:
            self.ctx.plugin_manager.stop_all()
        except Exception:
            pass
        try:
            if self.window is not None:
                self.window.destroy()
        except Exception:
            pass
        return True

    # ----------------------- 日志 -----------------------
    def log(self, level, msg):
        lvl = getattr(_log, level, None)
        if callable(lvl):
            lvl(msg)
        else:
            _log.info(msg)
        return True
