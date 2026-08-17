# -*- coding: utf-8 -*-
"""Nonoka Shell 主入口 —— 「后台优先」启动模式（窗口懒创建）。

架构：一切皆插件。Core 只提供规则；插件管理器 / 设置页 / 系统托盘 / 组件管理器
均为内置插件（随 Core 分发），视频下载等为用户插件（手动启停）。

启动流程（后台优先）：
  双击 exe → Core 启动 + 创建系统托盘图标（不创建窗口）
  用户点击托盘图标 → 懒创建 pywebview 窗口 → 加载壳子前端
  用户关闭窗口 → 拦截 closing → hide()（销毁 WebView2 渲染进程，插件继续后台运行）
  用户再次点击托盘图标 → 窗口重新显示
  托盘右键「退出」 → 停止所有插件 → 清理资源 → 退出进程

其他职责：
  - 解析命令行（含抖音浏览器兜底 --browser-parse 模式）
  - 初始化日志 / 单实例 / 数据目录 / 配置 / 数据库 / 多语言
  - 扫描插件（懒加载：仅读元数据）→ 恢复上次运行的插件 → 启动心跳监控
  - 启动子系统：托盘、全局快捷键、剪贴板监听（按配置）
  - 启动后台任务：壳子更新检查、插件更新检查、首次引导
  - 注册表自清洁：启动时检查旧版本残留
  - 全局错误捕获（崩溃报告 + 上报弹窗）
  - 退出时停止所有插件、停止心跳、清理
"""
import os
import sys

# 仓库根目录（nonoka_lab）加入 sys.path，确保 nonoka_shell / plugins 可作为包导入
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from .brand import (  # noqa: E402
    WINDOW_TITLE, WINDOW_WIDTH, WINDOW_HEIGHT, WINDOW_MIN_WIDTH,
    WINDOW_MIN_HEIGHT, VERSION, DATA_DIR_NAME,
)
from .logger import get_logger, get_log_path  # noqa: E402
from .config import Config  # noqa: E402
from .component_manager import ComponentManager  # noqa: E402
from .plugin_manager import PluginManager  # noqa: E402
from .updater import Updater  # noqa: E402
from .database import Database  # noqa: E402
from .notifier import Notifier  # noqa: E402
from .queue_manager import QueueManager  # noqa: E402
from .stats import Stats  # noqa: E402
from .clipboard_listener import ClipboardListener  # noqa: E402
from .hotkeys import HotkeyManager  # noqa: E402
from .tray import Tray  # noqa: E402
from .registry import Registry  # noqa: E402
from . import i18n  # noqa: E402
from . import crash_report  # noqa: E402
from .bridge import NonokaBridge  # noqa: E402
from . import single_instance  # noqa: E402
from . import error_handler  # noqa: E402
from . import autostart  # noqa: E402

_log = get_logger("main")


class Context:
    """贯穿全局的上下文对象，传递给各管理器与插件。"""

    # 标记为不可序列化：pywebview 生成 JS 桥时会递归遍历 js_api 对象的属性图，
    # 若深入 ctx -> window -> native(WinForms) 会触发 AccessibilityObject 无限递归
    # （RecursionError / 启动卡死）。此标记让 get_functions 直接跳过本对象。
    _serializable = False

    def __init__(self):
        self.config = None
        self.components = None
        self.db = None
        self.plugins = {}
        self.plugin_manager = None
        self.updater = None
        self.notifier = None
        self.queue = None
        self.stats = None
        self.clipboard = None
        self.hotkeys = None
        self.tray = None
        self.window = None
        self._quitting = False


def _detect_locale():
    """根据系统语言自动选择（首次启动）。"""
    try:
        import locale as _loc
        sys_loc = (_loc.getdefaultlocale()[0] or "")
    except Exception:
        sys_loc = ""
    sys_loc = (sys_loc or "").lower()
    return "en" if sys_loc.startswith(("en", "us", "en_", "english")) else "zh"


def _locate_plugin_core(pid):
    """按插件 id 动态定位其 core 目录（内置 / 市场 / 开发者目录都查）。
    插件未被安装（如已被卸载）时返回 None，避免硬编码路径导致的崩溃。"""
    roots = [
        os.path.join(_ROOT, "plugins"),
        os.path.join(os.path.expanduser("~"), "Documents", DATA_DIR_NAME, "plugins"),
        os.path.join(os.path.expanduser("~"), "Documents", DATA_DIR_NAME, "dev_plugins"),
    ]
    for root in roots:
        core = os.path.join(root, pid, "core")
        if os.path.isfile(os.path.join(core, "douyin_browser.py")):
            return core
    return None


def _handle_browser_parse(url):
    """抖音浏览器兜底解析模式：作为独立子进程被 douyin_downloader 调用。"""
    import json
    core = _locate_plugin_core("Nonoka_video_download")
    if not core:
        sys.stdout.write(json.dumps({"err": "视频下载插件未安装，无法进行浏览器解析"},
                                    ensure_ascii=False))
        sys.stdout.flush()
        sys.exit(0)
    if core not in sys.path:
        sys.path.insert(0, core)
    try:
        import douyin_browser
        out = douyin_browser.fetch_video_info_browser(url)
    except Exception as e:
        out = {"err": f"浏览器解析失败: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()
    sys.exit(0)


def build_context():
    ctx = Context()
    ctx.config = Config()
    ctx.db = Database()
    ctx.components = ComponentManager(ctx)
    ctx.updater = Updater(ctx)
    ctx.plugin_manager = PluginManager(ctx)
    ctx.notifier = Notifier(ctx)
    ctx.queue = QueueManager(ctx, max_parallel=ctx.config.get("parallel", 2))
    ctx.stats = Stats(ctx)
    plugins_root = os.path.join(_ROOT, "plugins")
    # 懒加载：只扫描元数据 + 拓扑排序 + 环检测，不 import 插件
    ctx.plugin_manager.load_all(plugins_root)
    # 启动时确保已安装组件在 PATH 上（ffmpeg 等）
    for meta in ctx.components.list_all():
        if meta.get("installed"):
            ctx.components.ensure_on_path(meta["id"])
    return ctx


def _crash_callback(bridge, exc_type, exc, tb):
    try:
        path, rep = crash_report.write(exc_type, exc, tb)
        bridge._emit_shell("onCrash", {
            "path": path,
            "error": rep.get("error"),
            "type": rep.get("type"),
        })
    except Exception:
        pass


def _safe_js(window, snippet):
    try:
        if window is not None:
            window.evaluate_js(snippet)
    except Exception:
        pass


def _ensure_window(ctx, bridge):
    """窗口懒创建：首次点击托盘时创建，之后只 show。"""
    if ctx.window is not None:
        try:
            ctx.window.show()
        except Exception:
            pass
        return ctx.window
    import webview
    # 用 pathlib.as_uri() 生成标准 file:/// URL：
    # 1) 反斜杠统一为 `/`；2) 路径中的中文等非 ASCII 字符做百分号编码，
    # 避免 WebView2 在 file:// 下解析 iframe 内相对子资源（page.js/cupertino.css）时失败，
    # 导致设置页/关于页空白。
    from pathlib import Path as _Path
    frontend_url = _Path(os.path.join(_ROOT, "frontend", "index.html")).resolve().as_uri()
    window = webview.create_window(
        title=WINDOW_TITLE,
        url=frontend_url,
        js_api=bridge,
        width=ctx.config.get("window", {}).get("width", WINDOW_WIDTH),
        height=ctx.config.get("window", {}).get("height", WINDOW_HEIGHT),
        min_size=(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT),
        background_color="#ffffff",
    )
    bridge.set_window(window)
    ctx.window = window
    ctx.window_ready = False   # WebView2 加载完成前 evaluate_js 会触发跨线程 COM 异常

    # 监听窗口加载完成 → 标记 ready 并主动拉取初始状态（避免后台线程在 ready 前 evaluate_js）
    def _on_loaded():
        ctx.window_ready = True
        _log.info("窗口加载完成（WebView2 ready）")
        # ready 后主动推送一次初始主题与插件列表
        try:
            bridge._emit_shell("onTheme", {"mode": ctx.config.get("theme", "auto")})
            bridge._emit_shell("onPluginsChanged", ctx.plugin_manager.list_meta())
        except Exception:
            pass

    if hasattr(window, "events"):
        for ev_name in ("loaded", "load"):
            ev = getattr(window.events, ev_name, None)
            if ev is not None:
                try:
                    ev += _on_loaded
                except Exception:
                    pass

    # 关闭窗口行为：默认退出；close_action=minimize 时最小化到托盘。
    # 首次点 X：原生 MessageBoxW 三选一（最小化/退出/取消），记下选择 + close_action。
    def _on_closing():
        if ctx._quitting:
            return True
        # 首次询问
        if not ctx.config.get("_closing_asked", False):
            try:
                import ctypes
                IDYES = 6
                IDNO = 7
                MB_YESNOCANCEL = 0x3
                r = ctypes.windll.user32.MessageBoxW(
                    None,
                    "关闭 Nonoka Lab 时希望如何处理？\n\n[是] 最小化到托盘（程序继续后台运行）\n[否] 直接退出程序\n[取消] 不关闭",
                    "Nonoka Lab", MB_YESNOCANCEL)
                if r == 0:  # 用户关掉了弹窗
                    return False
                if r == IDYES:
                    ctx.config.set("close_action", "minimize")
                elif r == IDNO:
                    ctx.config.set("close_action", "quit")
                else:  # 取消
                    return False
                ctx.config.set("_closing_asked", True)
                ctx.config.save()
            except Exception:
                ctx.config.set("_closing_asked", True)  # 跳过弹窗避免重复
        if ctx.config.get("close_action", "quit") == "minimize":
            try:
                window.hide()
            except Exception:
                pass
            return False
        return True  # 默认退出

    if hasattr(window, "events") and hasattr(window.events, "closing"):
        try:
            window.events.closing += _on_closing
        except Exception:
            pass
    return window


def _open_devtools(ctx, bridge):
    """F12：打开主窗口 WebView2 开发者工具（DevTools 窗口）。

    生产环境 webview.start() 未开 debug，AreDevToolsEnabled 默认 False，因此运行时
    先把该开关置 True，再弹出 DevTools。CoreWebView2 只能在 UI 线程访问，而 F12
    热键回调运行在 pynput 的后台线程，故统一交给 bridge.open_debug_tools()（内部用
    WinForms Invoke 切回 UI 线程）处理，避免 "CoreWebView2 can only be accessed
    from the UI thread" 异常导致静默失败。
    """
    try:
        if ctx.window is None:
            _ensure_window(ctx, bridge)
        r = bridge.open_debug_tools()
        if not r or not r.get("ok"):
            _log.warning("F12：打开开发者工具未成功: %s", (r or {}).get("error", "?"))
    except Exception as e:
        _log.warning("打开开发者工具失败: %s", e)


def _quit(ctx, bridge):
    """彻底退出（仅托盘菜单触发）：停插件 → 清理 → 退出进程。"""
    ctx._quitting = True
    try:
        ctx.plugin_manager.stop_heartbeat()
    except Exception:
        pass
    try:
        ctx.plugin_manager.stop_all()
    except Exception:
        pass
    for mgr in ("clipboard", "hotkeys", "tray"):
        try:
            getattr(ctx, mgr, None).stop()
        except Exception:
            pass
    try:
        if ctx.window is not None:
            ctx.window.destroy()
    except Exception:
        pass
    try:
        import webview
        webview.stop()
    except Exception:
        pass


def main(argv=None):
    argv = argv if argv is not None else sys.argv

    # ---- 抖音浏览器兜底子进程模式 ----
    if "--browser-parse" in argv:
        i = argv.index("--browser-parse")
        url = argv[i + 1] if len(argv) > i + 1 else ""
        _handle_browser_parse(url)
        return

    if "--version" in argv:
        print(f"{WINDOW_TITLE} {VERSION}")
        return

    # ---- 单实例 ----
    no_single = "--no-single-instance" in argv
    if not no_single:
        if not single_instance.acquire():
            single_instance.focus_existing()
            return

    # ---- 日志 ----
    _log.info("启动 %s %s", WINDOW_TITLE, VERSION)
    _log.info("日志路径: %s", get_log_path())

    # ---- 抑制 pywebview 内部日志刷屏 ----
    # 后台线程（剪贴板/托盘/心跳/updater）跨线程触发 evaluate_js 时，
    # pywebview 内部会访问 WebView2 native 属性，触发 COM 异常和递归 bug。
    # 抑制到 CRITICAL 后只输出致命错误，窗口与功能不受影响。
    import logging as _logging
    _logging.getLogger("pywebview").setLevel(_logging.CRITICAL)
    _logging.getLogger("clr").setLevel(_logging.CRITICAL)

    # ---- 上下文 ----
    ctx = build_context()
    bridge = NonokaBridge(ctx)

    # 插件状态变化时推送前端
    ctx.plugin_manager.on_change = (
        lambda: bridge._emit_shell("onPluginsChanged", ctx.plugin_manager.list_meta()))
    ctx.plugin_manager.on_state = (
        lambda pid, st: bridge._emit_shell("onPluginState", {"id": pid, "state": st}))

    # ---- 全局错误捕获（含崩溃报告 / 上报弹窗）----
    error_handler.install(
        on_crash=lambda et, ex, tb: _crash_callback(bridge, et, ex, tb))

    # ---- 语言（首次按系统，之后按配置）----
    loc = ctx.config.get("locale") or "auto"
    if not loc or loc == "auto":
        loc = _detect_locale()
    i18n.set_locale(loc)

    # ---- DEBUG 日志仅开发者模式 ----
    try:
        from .logger import set_debug
        set_debug(bool(ctx.config.get("dev_mode", False)))
    except Exception:
        pass

    # ---- 插件线程崩溃监控（未捕获异常 → 标记 crashed + 通知）----
    try:
        ctx.plugin_manager.install_thread_hook()
    except Exception as e:
        _log.error("安装线程崩溃监控失败: %s", e)

    # ---- Core 启动即恢复上次运行的插件（按「恢复上次运行的插件」开关，默认关）+ 心跳 ----
    try:
        ctx.plugin_manager.restore_running()
    except Exception as e:
        _log.error("恢复插件运行状态失败: %s", e)
    ctx.plugin_manager.start_heartbeat()

    # ---- 注册表自清洁：启动检查旧版本残留 ----
    try:
        autostart.set_db(ctx.db)
        leftovers = Registry(ctx.db).check_leftovers()
        if leftovers:
            _log.warning("检测到注册表残留 %d 项（可在卸载时自动清理）", len(leftovers))
    except Exception:
        pass

    # ---- 子系统：剪贴板监听（按配置）----
    ctx.clipboard = ClipboardListener(
        ctx, on_detect=lambda url: bridge.on_clipboard_detect(url))
    if ctx.config.get("clipboard", False):
        try:
            ctx.clipboard.start()
        except Exception:
            pass

    # ---- 子系统：全局快捷键（窗口懒创建下也能唤起）----
    ctx.hotkeys = HotkeyManager(ctx)
    ctx.hotkeys.on_show = lambda: _ensure_window(ctx, bridge)
    ctx.hotkeys.on_f12 = lambda: _open_devtools(ctx, bridge)
    try:
        ctx.hotkeys.start(ctx.config.get("hotkey", "Ctrl+Shift+N"))
    except Exception:
        pass

    # ---- 子系统：系统托盘（后台能力，窗口仍正常显示）----
    ctx.tray = Tray(
        ctx,
        on_show=lambda: _ensure_window(ctx, bridge),
        on_settings=lambda: (_ensure_window(ctx, bridge),
                            bridge._emit_shell("onSettingsRequested", None)),
        on_stop_all=lambda: bridge.stop_all_plugins(),
        on_quit=lambda: _quit(ctx, bridge),
    )
    tray_ok = False
    try:
        tray_ok = ctx.tray.start()
    except Exception as e:
        _log.warning("托盘启动失败: %s", e)
    # 启动反馈（终端可见，避免「没反应」的误解）
    if tray_ok:
        print("Nonoka Lab 已启动：窗口即将显示；关闭窗口最小化到托盘（右键托盘可退出）。", flush=True)
    else:
        print("Nonoka Lab 已启动（托盘不可用），窗口即将显示。", flush=True)

    # ---- 开机自启：同步实际状态到配置 ----
    try:
        ctx.config.set("autostart", autostart.is_enabled())
    except Exception:
        pass

    # ---- 启动后后台任务 ----
    ctx.updater.check_async(
        lambda info: bridge._emit_shell("onUpdateEvent", dict(info, startup=True)))
    ctx.plugin_manager.check_updates_async(
        lambda metas: bridge._emit_shell("onPluginsChanged", metas))
    if not bridge.get_welcome_done():
        bridge._emit_shell("onWelcome", {
            "show": True,
            "ffmpeg_missing": not ctx.components.get_status("ffmpeg").get("installed"),
        })
    # 推送初始主题（窗口创建后由前端自行拉取，这里仅记录）
    _log.info("主题模式: %s", ctx.config.get("theme", "auto"))

    # ---- 控制台实时推送：新日志经 bridge 推给前端 onConsoleLog ----
    # worker 线程本身已批量聚合（≤100 条 / 批），订阅回调直接转发即可，无需额外节流。
    from . import logger as _logger

    def _on_console_new(records):
        try:
            bridge._emit_shell("onConsoleLog", records)
        except Exception:
            pass

    _logger.subscribe_new(_on_console_new)

    # ---- 主循环：启动即创建并显示主窗口（GUI 直接弹出到桌面）----
    _ensure_window(ctx, bridge)
    import webview
    try:
        # private_mode=True：WebView2 使用全新临时数据目录（无历史/无缓存），
        # 确保 file:// 加载到最新的 HTML/CSS，规避 WebView2 对本地资源的缓存导致
        # 前端改动不生效的问题。配置/日志仍存于磁盘 config.json，不受影响。
        webview.start(private_mode=True)
    except Exception as e:
        _log.exception("主循环异常: %s", e)
    finally:
        _quit(ctx, bridge)
        try:
            ctx.plugin_manager.unload_all()
        except Exception:
            pass
        try:
            ctx.plugin_manager.stop_heartbeat()
        except Exception:
            pass
        single_instance.release()
        if ctx.db is not None:
            ctx.db.close()
        _log.info("%s 已退出", WINDOW_TITLE)


if __name__ == "__main__":
    main()
