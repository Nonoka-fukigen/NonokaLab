# -*- coding: utf-8 -*-
"""插件管理器（业务层）：基于 Core 规则的插件生命周期管理。

设计理念：Plugin Freedom, User Sovereignty。
- 三目录扫描：内置（安装目录，只读）> 市场（文档/NonokaLab/plugins）> 开发者（dev_plugins）
- 懒加载：不启动不 import；core_version_min 不兼容插件不加载并给出原因
- 生命周期：stopped -> running -> (paused -> running) -> stopped；崩溃标记 crashed
- 状态持久化：plugin_status.running_status 记录；恢复开关（默认关）决定是否自动恢复
- 崩溃恢复：线程未捕获异常 / 心跳超时 → crashed + 桌面通知；可选自动重启
- 插件数据：统一存放 文档/NonokaLab/plugins_data/<plugin_id>/；卸载时询问是否删除
- 基础插件：不可卸载 / 禁用（只可查看与调整非致命参数）
"""
import datetime
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import threading
import traceback
import zipfile

import requests

from .brand import MARKET_URL, VERSION
from .core import CorePluginManager, Plugin as BasePlugin
from .logger import get_logger, heartbeat as hb_log
from .utils import get_data_dir
from .i18n import t

_log = get_logger("plugin")


def _as_file_uri(path):
    """将本地路径转成标准 file:/// URL（反斜杠归一 + 中文等非 ASCII 百分号编码），
    避免 WebView2 在 file:// 下加载含中文路径的子资源失败。"""
    try:
        from pathlib import Path
        return Path(os.path.abspath(path)).as_uri()
    except Exception:
        return "file://" + path


class NonokaPlugin(BasePlugin):
    """插件基类：兼容既有契约（id/name/icon/description + 生命周期钩子）。"""

    id = ""
    name = ""
    icon = "square"
    description = ""

    def __init__(self, ctx=None):
        super().__init__(getattr(ctx, "plugin_manager", None) if ctx is not None else None)
        self.ctx = ctx

    def api_methods(self):
        return []

    def frontend_path(self):
        return None

    def on_load(self):
        pass

    def on_unload(self):
        pass

    def on_start(self):
        pass

    def on_stop(self):
        pass

    def on_pause(self):
        pass

    def on_resume(self):
        pass


class PluginManager(CorePluginManager):
    """插件管理器：扫描 / 依赖 / 生命周期 / 市场 / 更新 / 心跳 / 崩溃恢复。"""

    def __init__(self, ctx):
        super().__init__(core_version=VERSION)
        self.ctx = ctx
        self._proxies = {}       # id -> js 代理（仅启用态）
        self.on_change = None    # callback() 刷新插件列表到前端
        self.on_state = None     # callback(pid, state) 推送状态变化
        self._hb_stop = threading.Event()
        self._hb_thread = None

    # ----------------------- 发现 / 扫描（懒加载 + 三目录优先级） -----------------------
    def _market_root(self):
        d = os.path.join(get_data_dir(), "plugins")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d

    def _dev_root(self):
        d = os.path.join(get_data_dir(), "dev_plugins")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d

    def _data_root(self):
        d = os.path.join(get_data_dir(), "plugins_data")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d

    def load_all(self, plugins_root, scan_dev=True):
        """扫描三目录（内置 > 市场 > 开发者），ID 冲突内置优先；不 import 插件。"""
        self.meta.clear()
        self.dirs.clear()
        self.order = []
        self.cycle = None
        if os.path.isdir(plugins_root):
            self.scan(plugins_root, source="builtin")
        self.scan(self._market_root(), source="market")
        if scan_dev:
            self.scan(self._dev_root(), source="dev")
        # 目录已移除的插件：卸载实例并清理状态
        for pid in list(self.plugins.keys()):
            if pid not in self.meta:
                inst = self.plugins.pop(pid, None)
                self._proxies.pop(pid, None)
                self._states.pop(pid, None)
                if inst is not None:
                    try:
                        inst.on_unload()
                    except Exception:
                        pass
        return self.order

    def _read_meta(self, d):
        meta = {}
        for fn in ("plugin.json", "manifest.json"):
            p = os.path.join(d, fn)
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        meta = json.load(f) or {}
                    break
                except Exception as e:
                    _log.warning("读取 %s 失败: %s", fn, e)
        for k in ("provides", "consumes", "permissions"):
            meta.setdefault(k, [])
        meta.setdefault("sha256", "")
        meta.setdefault("config_version", 1)
        meta.setdefault("builtin", False)
        meta.setdefault("system", False)
        meta.setdefault("dependencies", [])
        meta.setdefault("enabled", True)
        return meta

    def _ensure_loaded(self, pid):
        """懒加载：按需 import 并实例化插件。"""
        if pid in self.plugins:
            return self.plugins[pid]
        d = self.dirs.get(pid)
        if not d:
            return None
        plug_py = os.path.join(d, "plugin.py")
        if not os.path.isfile(plug_py):
            return None
        self._load_one(d, plug_py, dev=self.meta.get(pid, {}).get("source") == "dev")
        return self.plugins.get(pid)

    def _load_one(self, d, plug_py, reload=False, dev=False):
        import importlib  # 整个函数内统一可用
        meta = self._read_meta(d)
        meta["dev"] = bool(dev)
        meta["source"] = meta.get("source") or ("dev" if dev else "builtin")
        mod_name = "plugin_" + os.path.basename(os.path.normpath(d))
        if reload and mod_name in sys.modules:
            mod = sys.modules[mod_name]
            try:
                importlib.reload(mod)
            except Exception:
                spec = importlib.util.spec_from_file_location(mod_name, plug_py)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
        else:
            spec = importlib.util.spec_from_file_location(mod_name, plug_py)
            mod = importlib.util.module_from_spec(spec)
            if d not in sys.path:
                sys.path.insert(0, d)
            spec.loader.exec_module(mod)

        PluginCls = getattr(mod, "Plugin", None)
        if not PluginCls or not isinstance(PluginCls, type) or not issubclass(PluginCls, NonokaPlugin):
            _log.warning("插件 %s 未定义 Plugin 子类，跳过", os.path.basename(d))
            return
        inst = PluginCls(self.ctx)
        try:
            inst.on_load()
        except Exception as e:
            _log.error("插件 %s on_load 异常: %s", inst.id, e)

        pid = inst.id or meta.get("id") or os.path.basename(d)
        old = self.plugins.pop(pid, None)
        if old is not None:
            try:
                old.on_unload()
            except Exception:
                pass
        self.plugins[pid] = inst
        merged = dict(self.meta.get(pid, {}))
        merged.update(meta)
        merged["id"] = pid
        self.meta[pid] = merged
        self.dirs[pid] = d
        if pid not in self._states:
            self._states[pid] = "stopped"

        if self.ctx.db is not None:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.ctx.db.upsert_plugin(pid, version=meta.get("version") or "",
                                      last_check=now, running="stopped")
        enabled = self._is_enabled(pid, meta)
        if enabled:
            self._proxies[pid] = self._make_proxy(inst)
        else:
            self._proxies.pop(pid, None)
        self._maybe_migrate(pid, inst)
        return inst

    def _is_enabled(self, pid, meta):
        if self.ctx.db is not None:
            rec = self.ctx.db.get_plugin(pid)
            if rec is not None:
                return bool(rec.get("enabled", 1))
        return bool(meta.get("builtin", False)) or bool(meta.get("dev", False)) \
            or bool(meta.get("enabled", True))

    @staticmethod
    def _make_proxy(plugin):
        class _Proxy:
            pass
        for name in plugin.api_methods():
            fn = getattr(plugin, name, None)
            if callable(fn):
                setattr(_Proxy, name, fn)
        return _Proxy()

    def get(self, pid):
        return self.plugins.get(pid)

    def proxy(self, pid):
        return self._proxies.get(pid)

    def get_meta(self, pid):
        return self.meta.get(pid, {})

    def dir_of(self, pid):
        return self.dirs.get(pid)

    # ----------------------- 插件数据目录 -----------------------
    def plugin_data_dir(self, pid):
        return os.path.join(self._data_root(), pid)

    def has_plugin_data(self, pid):
        d = self.plugin_data_dir(pid)
        return os.path.isdir(d) and bool(os.listdir(d))

    def delete_plugin_data(self, pid):
        d = self.plugin_data_dir(pid)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
            return True
        return False

    # ----------------------- 配置迁移 -----------------------
    def _maybe_migrate(self, pid, inst):
        """config_version 变化时调用插件 migrate_config(old, new)。"""
        new_ver = int(self.meta.get(pid, {}).get("config_version", 1) or 1)
        old_ver = 1
        if self.ctx.db is not None:
            rec = self.ctx.db.get_plugin(pid)
            if rec is not None and rec.get("config_version") is not None:
                old_ver = int(rec["config_version"])
        if old_ver >= new_ver:
            if self.ctx.db is not None:
                self.ctx.db.upsert_plugin(pid, config_version=new_ver)
            return
        migrator = getattr(inst, "migrate_config", None)
        if callable(migrator):
            try:
                ok = migrator(old_ver, new_ver)
                if ok:
                    _log.info("插件 %s 配置已从 v%d 迁移到 v%d", pid, old_ver, new_ver)
                else:
                    _log.warning("插件 %s 迁移返回 False，保留旧配置", pid)
            except Exception as e:
                _log.warning("插件 %s 配置迁移异常: %s，保留旧配置", pid, e)
        else:
            _log.warning("插件 %s config_version v%d->v%d 但未实现 migrate_config，保留旧配置",
                         pid, old_ver, new_ver)
        if self.ctx.db is not None:
            self.ctx.db.upsert_plugin(pid, config_version=new_ver)

    # ----------------------- 启用 / 禁用（基础插件不可禁用） -----------------------
    def set_enabled(self, pid, enabled):
        m = self.meta.get(pid, {})
        if m.get("system") and not enabled:
            return False
        if self.ctx.db is not None:
            self.ctx.db.set_plugin_enabled(pid, enabled)
        inst = self._ensure_loaded(pid)
        if inst is None:
            return False
        if enabled:
            self._proxies[pid] = self._make_proxy(inst)
        else:
            self._proxies.pop(pid, None)
        self._notify()
        return True

    # ----------------------- 生命周期 -----------------------
    def _persist_running(self, pid, status):
        if self.ctx.db is not None:
            try:
                self.ctx.db.set_plugin_running(pid, status)
            except Exception:
                pass

    def _notify(self):
        if callable(self.on_change):
            try:
                self.on_change()
            except Exception:
                pass

    def _notify_state(self, pid):
        if callable(self.on_state):
            try:
                self.on_state(pid, self.state(pid))
            except Exception:
                pass

    def start_plugin(self, pid):
        if not self.is_compatible(pid):
            return False
        inst = self._ensure_loaded(pid)
        if inst is None:
            return False
        if pid not in self._proxies:
            self.set_enabled(pid, True)
        self.set_state(pid, "running")
        self._persist_running(pid, "running")
        self.heartbeat(pid)
        try:
            inst.on_start()
        except Exception as e:
            _log.error("插件 %s on_start 异常: %s", pid, e)
            self.mark_crashed(pid, str(e))
            return False
        self._notify()
        self._notify_state(pid)
        return True

    def stop_plugin(self, pid):
        inst = self.plugins.get(pid)
        if not inst:
            return False
        try:
            if self.ctx.queue is not None:
                for tk in self.ctx.queue.tasks_for(pid):
                    if tk["status"] in ("queued", "running", "paused"):
                        self.ctx.queue.cancel(tk["id"])
        except Exception:
            pass
        try:
            inst.on_stop()
        except Exception as e:
            _log.error("插件 %s on_stop 异常: %s", pid, e)
        self.set_state(pid, "stopped")
        self._persist_running(pid, "stopped")
        self._notify()
        self._notify_state(pid)
        return True

    def pause_plugin(self, pid):
        if self.state(pid) != "running":
            return False
        inst = self.plugins.get(pid)
        self.set_state(pid, "paused")
        self._persist_running(pid, "paused")
        try:
            if inst:
                inst.on_pause()
        except Exception:
            pass
        self._notify()
        self._notify_state(pid)
        return True

    def resume_plugin(self, pid):
        if self.state(pid) != "paused":
            return False
        inst = self.plugins.get(pid)
        self.set_state(pid, "running")
        self._persist_running(pid, "running")
        try:
            if inst:
                inst.on_resume()
        except Exception:
            pass
        self._notify()
        self._notify_state(pid)
        return True

    def stop_all(self):
        for pid in list(self._states.keys()):
            if self._states.get(pid) == "running":
                try:
                    self.stop_plugin(pid)
                except Exception:
                    pass

    def unload_all(self):
        for pid in list(self.plugins.keys()):
            inst = self.plugins.pop(pid, None)
            if inst is not None:
                try:
                    inst.on_unload()
                except Exception:
                    pass

    # ----------------------- 崩溃恢复 -----------------------
    def mark_crashed(self, pid, error):
        super().mark_crashed(pid, str(error))
        try:
            self._persist_running(pid, "crashed")
        except Exception:
            pass
        if self.ctx and self.ctx.notifier:
            try:
                name = self.meta.get(pid, {}).get("name") or pid
                self.ctx.notifier.notify(t("plugin_crashed"), name)
            except Exception:
                pass
        self._notify()
        self._notify_state(pid)

    def restart_plugin(self, pid):
        """崩溃 / 停止后重启。"""
        try:
            self.stop_plugin(pid)
        except Exception:
            pass
        return self.start_plugin(pid)

    def crash_reason(self, pid):
        return self._crashed.get(pid, "")

    def install_thread_hook(self):
        """监控插件线程未捕获异常 → 标记崩溃（threading.excepthook）。"""
        orig = threading.excepthook

        def hook(args):
            try:
                tb = "".join(traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback))
                for pid, d in self.dirs.items():
                    if d and d in tb:
                        self.mark_crashed(pid, "%s: %s" % (args.exc_type.__name__, args.exc_value))
                        break
            except Exception:
                pass
            try:
                orig(args)
            except Exception:
                pass

        threading.excepthook = hook

    # ----------------------- 状态恢复 / 心跳（含崩溃检测） -----------------------
    def restore_running(self, enabled=None):
        """按「恢复上次运行的插件」开关（默认关）恢复 running 插件。"""
        if self.ctx.db is None:
            return 0
        if enabled is None:
            try:
                enabled = bool(self.ctx.config.get("restore_running", False))
            except Exception:
                enabled = False
        if not enabled:
            return 0
        n = 0
        for rec in self.ctx.db.get_plugins():
            if rec.get("running_status") == "running":
                pid = rec.get("plugin_id")
                if pid in self.meta:
                    try:
                        if self.start_plugin(pid):
                            n += 1
                    except Exception as e:
                        _log.error("恢复插件 %s 失败: %s", pid, e)
        if n:
            _log.info("已恢复 %d 个插件为运行状态", n)
        return n

    def start_heartbeat(self):
        """每 5 秒为 running 插件写心跳日志；30s 无心跳标记崩溃（可选自动重启）。"""
        if self._hb_thread and self._hb_thread.is_alive():
            return
        self._hb_stop.clear()

        def run():
            while not self._hb_stop.wait(5.0):
                for pid, st in list(self._states.items()):
                    if st == "running":
                        self.heartbeat(pid)
                        try:
                            hb_log(pid)
                        except Exception:
                            pass
                        if self.health(pid) != "ok":
                            self._handle_crash(pid)

        self._hb_thread = threading.Thread(target=run, daemon=True)
        self._hb_thread.start()

    def _handle_crash(self, pid):
        if self._states.get(pid) != "running":
            return
        reason = self.crash_reason(pid) or "心跳超时（30 秒无响应）"
        self.mark_crashed(pid, reason)
        try:
            if self.ctx.config and self.ctx.config.get("auto_restart_crashed", False):
                _log.info("插件崩溃自动重启: %s", pid)
                threading.Timer(2.0, lambda: self.restart_plugin(pid)).start()
        except Exception:
            pass

    def stop_heartbeat(self):
        self._hb_stop.set()

    # ----------------------- 元数据导出（不依赖实例） -----------------------
    def list_meta(self):
        out = []
        for pid, m in self.meta.items():
            d = self.dirs.get(pid) or ""
            fp = os.path.join(d, "frontend", "index.html")
            has_fp = bool(d) and os.path.isfile(fp)
            st = self.state(pid)
            out.append({
                "id": pid,
                "name": m.get("name") or pid,
                "icon": m.get("icon") or "square",
                "description": m.get("description") or "",
                "frontend": _as_file_uri(fp) if has_fp else None,
                "version": m.get("version", ""),
                "author": m.get("author", ""),
                "repo": m.get("repo", ""),
                "provides": m.get("provides", []),
                "consumes": m.get("consumes", []),
                "permissions": m.get("permissions", []),
                "dependencies": m.get("dependencies", []),
                "builtin": bool(m.get("builtin", False)),
                "system": bool(m.get("system", False)),
                "dev": bool(m.get("dev", False)) or m.get("source") == "dev",
                "source": m.get("source", "market"),
                "enabled": pid in self._proxies,
                "running": st,
                "crashed": st == "crashed",
                "crash_reason": self.crash_reason(pid),
                "health": self.health(pid),
                "incompatible": bool(m.get("incompatible", False)),
                "incompatible_reason": m.get("incompatible_reason", ""),
                "core_version_min": m.get("core_version_min", ""),
                "cycle": bool(self.cycle and pid in self.cycle),
                "has_update": m.get("has_update", False),
                "latest_version": m.get("latest_version", ""),
                "config_version": m.get("config_version", 1),
            })
        return out

    # ----------------------- 开发者：热重载 / 重扫 -----------------------
    def reload_plugin(self, pid):
        d = self.dirs.get(pid)
        if not d:
            return False
        plug_py = os.path.join(d, "plugin.py")
        if not os.path.isfile(plug_py):
            return False
        try:
            self._load_one(d, plug_py, reload=True, dev=self.meta.get(pid, {}).get("source") == "dev")
            self._notify()
            return True
        except Exception as e:
            _log.error("热重载插件 %s 失败: %s", pid, e)
            return False

    def rescan(self, scan_dev=True):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins")
        self.load_all(root, scan_dev=scan_dev)
        self._notify()
        return True

    # ----------------------- 插件市场 -----------------------
    def get_market(self):
        url = MARKET_URL
        try:
            if self.ctx.config is not None:
                url = self.ctx.config.get("market_url") or MARKET_URL
        except Exception:
            pass
        try:
            resp = requests.get(url, timeout=12)
            if resp.status_code != 200:
                return []
            data = resp.json() or {}
            items = data.get("plugins", []) if isinstance(data, dict) else data
            installed = {p["id"] for p in self.list_meta()}
            for it in items:
                it["installed"] = it.get("id") in installed
            return items
        except Exception as e:
            _log.warning("插件市场拉取失败: %s", e)
            return []

    # ----------------------- 安装 / 卸载 / 更新 -----------------------
    def _resolve_zip_url(self, entry):
        dl = entry.get("download")
        if dl:
            return dl
        repo = entry.get("repo") or entry.get("id")
        if repo and "/" in repo:
            return f"https://github.com/{repo}/archive/refs/heads/main.zip"
        return None

    @staticmethod
    def _check_sha256(path, expect):
        if not expect:
            return True
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest().lower() == str(expect).lower()
        except Exception:
            return False

    def install_plugin(self, entry, callback):
        def run():
            try:
                url = self._resolve_zip_url(entry)
                if not url:
                    callback({"ok": False, "error": t("err_no_download")})
                    return
                pid = entry.get("id") or os.path.basename(url).split(".")[0]
                dest_zip = os.path.join(get_data_dir(), "updates", pid + ".zip")
                os.makedirs(os.path.dirname(dest_zip), exist_ok=True)
                callback({"type": "start", "id": pid})
                path = self._download(url, dest_zip, callback, pid)
                if not path:
                    return
                if not self._check_sha256(path, entry.get("sha256") or ""):
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    callback({"type": "done", "ok": False, "id": pid,
                              "error": t("err_sha_mismatch")})
                    return
                target = os.path.join(self._market_root(), pid)
                self._extract(path, target)
                plug_py = os.path.join(target, "plugin.py")
                if os.path.isfile(plug_py):
                    self._load_one(target, plug_py, reload=True)
                callback({"type": "done", "ok": True, "id": pid, "path": target})
            except Exception as e:
                _log.warning("安装插件失败 %s: %s", entry.get("id"), e)
                callback({"type": "done", "ok": False, "id": entry.get("id"), "error": str(e)})
        threading.Thread(target=run, daemon=True).start()

    def update_plugin(self, pid, callback):
        def run():
            m = self.meta.get(pid, {})
            repo = m.get("repo") or pid
            if not repo or "/" not in repo:
                callback({"type": "done", "ok": False, "id": pid, "error": t("err_no_repo")})
                return
            try:
                api = f"https://api.github.com/repos/{repo}/releases/latest"
                resp = requests.get(api, timeout=12,
                                    headers={"Accept": "application/vnd.github+json"})
                if resp.status_code != 200:
                    callback({"type": "done", "ok": False, "id": pid, "error": f"HTTP {resp.status_code}"})
                    return
                data = resp.json()
                tag = (data.get("tag_name") or "").lstrip("vV")
                dl = None
                for a in data.get("assets", []):
                    if (a.get("name") or "").endswith(".zip"):
                        dl = a.get("browser_download_url")
                        break
                if not dl:
                    dl = f"https://github.com/{repo}/archive/refs/tags/{tag}.zip" if tag else None
                if not dl:
                    callback({"type": "done", "ok": False, "id": pid, "error": t("err_no_asset")})
                    return
                callback({"type": "start", "id": pid})
                dest_zip = os.path.join(get_data_dir(), "updates", pid + ".zip")
                os.makedirs(os.path.dirname(dest_zip), exist_ok=True)
                path = self._download(dl, dest_zip, callback, pid)
                if not path:
                    return
                if not self._check_sha256(path, m.get("sha256") or ""):
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    callback({"type": "done", "ok": False, "id": pid,
                              "error": t("err_sha_mismatch")})
                    return
                target = self.dirs.get(pid) or os.path.join(self._market_root(), pid)
                if self._states.get(pid) == "running":
                    try:
                        self.stop_plugin(pid)
                    except Exception:
                        pass
                self._extract(path, target)
                plug_py = os.path.join(target, "plugin.py")
                if os.path.isfile(plug_py):
                    self._load_one(target, plug_py, reload=True)
                if self.ctx.db is not None:
                    self.ctx.db.upsert_plugin(pid, version=tag or m.get("version", ""),
                                              last_update=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                self.meta[pid]["has_update"] = False
                self.meta[pid]["latest_version"] = tag
                self._notify()
                callback({"type": "done", "ok": True, "id": pid, "version": tag})
            except Exception as e:
                _log.warning("更新插件失败 %s: %s", pid, e)
                callback({"type": "done", "ok": False, "id": pid, "error": str(e)})
        threading.Thread(target=run, daemon=True).start()

    def uninstall_plugin(self, pid, force=False, delete_data=False):
        m = self.meta.get(pid, {})
        # 仅「系统核心插件」不可卸载；普通内置插件（如视频下载）允许卸载
        if m.get("system"):
            return {"ok": False, "error": t("err_builtin_no_uninstall")}
        deps = self.dependents(pid)
        if deps and not force:
            return {"ok": False, "need_confirm": deps,
                    "error": t("uninstall_deps", deps=", ".join(deps))}
        if self._states.get(pid) == "running":
            try:
                self.stop_plugin(pid)
            except Exception:
                pass
        target = self.dirs.get(pid) or os.path.join(self._market_root(), pid)
        try:
            inst = self.plugins.pop(pid, None)
            if inst is not None:
                try:
                    inst.on_unload()
                except Exception:
                    pass
            self._proxies.pop(pid, None)
            self.meta.pop(pid, None)
            self.dirs.pop(pid, None)
            self._states.pop(pid, None)
            if os.path.isdir(target) and not m.get("system"):
                shutil.rmtree(target)
            has_data = self.has_plugin_data(pid)
            if delete_data:
                self.delete_plugin_data(pid)
                has_data = False
            return {"ok": True, "has_data": has_data}
        except Exception as e:
            _log.warning("卸载插件失败 %s: %s", pid, e)
            return {"ok": False, "error": str(e)}

    # ----------------------- 插件更新检查 -----------------------
    def check_updates_async(self, callback):
        def run():
            for pid, m in list(self.meta.items()):
                repo = m.get("repo")
                if not repo or "/" not in repo:
                    continue
                try:
                    api = f"https://api.github.com/repos/{repo}/releases/latest"
                    resp = requests.get(api, timeout=12,
                                        headers={"Accept": "application/vnd.github+json"})
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    latest = (data.get("tag_name") or "").lstrip("vV")
                    if self._newer(latest, m.get("version", "")):
                        self.meta[pid]["has_update"] = True
                        self.meta[pid]["latest_version"] = latest
                except Exception:
                    pass
            try:
                callback(self.list_meta())
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _newer(a, b):
        import re

        def pv(v):
            s = (v or "").strip()
            m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?", s)
            if not m:
                return (0, 0, 0, 0)
            parts = [int(g) if g is not None else 0 for g in m.groups()]
            while len(parts) < 4:
                parts.append(0)
            return tuple(parts[:4])

        return pv(a) > pv(b)

    # ----------------------- 工具 -----------------------
    @staticmethod
    def _download(url, dest_zip, callback, pid):
        try:
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0) or 0)
                done = 0
                with open(dest_zip, "wb") as f:
                    for chunk in r.iter_content(8192):
                        if chunk:
                            f.write(chunk)
                            done += len(chunk)
                            callback({"type": "progress", "id": pid,
                                      "fetched": done, "total": total,
                                      "percent": (done * 100 // total) if total else 0})
            return dest_zip
        except Exception as e:
            _log.warning("下载插件失败 %s: %s", pid, e)
            callback({"type": "done", "ok": False, "id": pid, "error": str(e)})
            return None

    @staticmethod
    def _extract(zip_path, target_dir):
        os.makedirs(target_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as z:
            for member in z.namelist():
                dest = os.path.normpath(os.path.join(target_dir, member))
                if not (dest == target_dir or dest.startswith(target_dir + os.sep)):
                    continue
                if member.endswith("/"):
                    os.makedirs(dest, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with z.open(member) as src, open(dest, "wb") as out:
                        out.write(src.read())
