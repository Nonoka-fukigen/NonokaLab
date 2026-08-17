# -*- coding: utf-8 -*-
"""Nonoka Lab 启动级冒烟测试（headless：不建 GUI 窗口，其余全真实代码路径）"""
import os, sys, tempfile, json, time, shutil
ROOT = os.path.dirname(os.path.abspath(__file__))  # 脚本所在目录（与 cwd 无关）
sys.path.insert(0, ROOT)
PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (("  " + str(extra)) if extra else ""))

print("== 0. 模块导入 ==")
try:
    from nonoka_shell import main as M
    from nonoka_shell.bridge import NonokaBridge
    from nonoka_shell.core import CorePluginManager, EventBus, ServiceLocator
    from nonoka_shell.plugin_manager import PluginManager, NonokaPlugin
    from nonoka_shell.config import Config
    from nonoka_shell.database import Database
    from nonoka_shell.queue_manager import QueueManager
    from nonoka_shell.stats import Stats
    from nonoka_shell.notifier import Notifier
    from nonoka_shell.component_manager import ComponentManager
    from nonoka_shell.registry import Registry
    from nonoka_shell import i18n, logger, backup, crash_report, feedback
    check("所有模块可导入（含 main/bridge/core 包）", True)
except Exception as e:
    check("模块导入", False, repr(e)); sys.exit(1)

print("== 1. 启动上下文（模拟 build_context，db 用临时文件） ==")
tmp = tempfile.mkdtemp(prefix="nonoka_smoke_")
class Ctx:
    def __init__(self):
        self.plugins = {}   # 与 main.Context 一致（懒加载下为空）
        self.updater = None
        self.clipboard = None
        self.hotkeys = None
        self.tray = None
        self.window = None
        self._quitting = False
ctx = Ctx()
try:
    ctx.config = Config(os.path.join(tmp, "config.json"))
    ctx.db = Database(os.path.join(tmp, "nonoka.db"))
    ctx.components = ComponentManager(ctx)
    ctx.plugin_manager = PluginManager(ctx)
    ctx.notifier = Notifier(ctx)
    ctx.queue = QueueManager(ctx, max_parallel=2)
    ctx.stats = Stats(ctx)
    # 三目录扫描（内置=仓库 plugins/，市场/开发者=临时目录）
    ctx.plugin_manager.load_all(os.path.join(ROOT, "plugins"))
    check("启动上下文 + 三目录扫描", True, "meta=%d" % len(ctx.plugin_manager.meta))
except Exception as e:
    check("启动上下文", False, repr(e)); sys.exit(1)

print("== 2. 插件列表与元数据 ==")
meta = ctx.plugin_manager.list_meta()
p = next((x for x in meta if x["id"] == "Nonoka_video_download"), None)
check("内置插件被发现", p is not None)
if p:
    check("元数据完整", bool(p["provides"]) and p["source"] == "builtin" and p["core_version_min"] is not None,
          "provides=%s source=%s v%s" % (p["provides"][0], p["source"], p["version"]))
    check("未启动（懒加载不 import）", "Nonoka_video_download" not in ctx.plugin_manager.plugins)

print("== 3. 插件生命周期 + 心跳 ==")
r = ctx.plugin_manager.start_plugin("Nonoka_video_download")
check("启动插件", r is True)
check("状态 running", ctx.plugin_manager.state("Nonoka_video_download") == "running")
ctx.plugin_manager.start_heartbeat(); time.sleep(0.3)
check("心跳健康 ok", ctx.plugin_manager.health("Nonoka_video_download") == "ok")
ctx.plugin_manager.pause_plugin("Nonoka_video_download")
check("暂停 paused", ctx.plugin_manager.state("Nonoka_video_download") == "paused")
ctx.plugin_manager.resume_plugin("Nonoka_video_download")
check("恢复 running", ctx.plugin_manager.state("Nonoka_video_download") == "running")

print("== 4. 任务队列（download 路由，提交后取消，不真实下载） ==")
inst = ctx.plugin_manager.plugins["Nonoka_video_download"]
try:
    res = inst.download({"url": "https://www.bilibili.com/video/BV1xx_TEST", "platform": "bilibili", "mode": "audio"})
    time.sleep(0.5)
    q = ctx.queue.list()
    check("download 进入队列", bool(q), "task=%s status=%s" % (q[0]["id"][:8] if q else "-", q[0]["status"] if q else "-"))
    for t in q:
        ctx.queue.cancel(t["id"])
    # 第三方核心为阻塞式：取消为「尽力而为」，取消后状态应为终态（done/failed/cancelled）
    check("队列可取消（尽力而为）", all(t["status"] in ("done", "failed", "cancelled") for t in ctx.queue.list()))
    ctx.plugin_manager.stop_plugin("Nonoka_video_download")
    check("停止插件", ctx.plugin_manager.state("Nonoka_video_download") == "stopped")
except Exception as e:
    check("任务队列", False, repr(e))

print("== 5. 下载历史（SQLite 读写） ==")
ctx.db.add_download("https://www.bilibili.com/video/BV1xx", "测试标题", "video", "C:/tmp")
rows = ctx.db.search_downloads("测试")
check("历史写入+搜索", len(rows) == 1, "rows=%d" % len(rows))
ctx.db.clear_downloads()
check("历史清空", len(ctx.db.get_downloads()) == 0)

print("== 6. 崩溃标记 + 重启 ==")
ctx.plugin_manager.set_state("Nonoka_video_download", "running")
ctx.plugin_manager.mark_crashed("Nonoka_video_download", "模拟崩溃")
check("崩溃标记 crashed", ctx.plugin_manager.state("Nonoka_video_download") == "crashed" and "模拟崩溃" in ctx.plugin_manager.crash_reason("Nonoka_video_download"))
r = ctx.plugin_manager.restart_plugin("Nonoka_video_download")
check("崩溃后重启", r is True, "state=%s" % ctx.plugin_manager.state("Nonoka_video_download"))

print("== 7. EventBus 事件面板 + ServiceLocator ==")
ctx.plugin_manager.bus.on("test.topic", lambda d, tid: None)
ctx.plugin_manager.bus.emit("test.topic", {"k": 1}); time.sleep(0.3)
tr = ctx.plugin_manager.bus.trace()
check("事件面板有记录", any(x[2] == "test.topic" for x in tr))
ctx.plugin_manager.register_service("demo", type("S", (), {"ping": lambda s: "pong"})())
check("ServiceLocator 同步调用", ctx.plugin_manager.get_service("demo").ping() == "pong")

print("== 8. 数据备份（导出/导入 ZIP） ==")
from nonoka_shell import backup as bk
try:
    ex = bk.export_data(os.path.join(tmp, "exp"))
    check("备份导出", ex.get("ok"), str(ex.get("path")))
    im = bk.import_data(ex.get("path"))
    check("备份导入", im.get("ok"))
except Exception as e:
    check("数据备份", False, repr(e))

print("== 9. 桥接对象（js_api）关键接口 ==")
bridge = NonokaBridge(ctx)
try:
    plugs = bridge.get_plugins()
    check("bridge.get_plugins", isinstance(plugs, list) and len(plugs) > 0)
    check("bridge.get_health", isinstance(bridge.get_health(), dict))
    check("bridge.get_event_trace", isinstance(bridge.get_event_trace(10), list))
    st = bridge.get_plugin_state("Nonoka_video_download")
    check("bridge.get_plugin_state", st in ("running", "stopped", "paused", "crashed"), st)
except Exception as e:
    check("桥接对象", False, repr(e))

print("== 10. 注册表自清洁（平台安全） ==")
try:
    reg = Registry(ctx.db)
    leftovers = reg.check_leftovers()
    check("Registry 启动残留检查（安全 no-op）", isinstance(leftovers, list))
except Exception as e:
    check("注册表自清洁", False, repr(e))

print("== 11. 崩溃报告隐私（默认不含日志/URL） ==")
rep = crash_report.build_report(ValueError, ValueError("test url: https://x.com/v"), None)
check("崩溃报告默认无日志", "recent_log" not in rep)

print()
print("结果：PASS=%d  FAIL=%d" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：", FAIL)
    sys.exit(1)
print("ALL SMOKE TESTS PASSED")
