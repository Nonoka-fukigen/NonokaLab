# -*- coding: utf-8 -*-
"""Nonoka Core 包 —— 只提供规则，不含任何业务逻辑。

模块：
- event_bus.py       异步事件总线（事件通知 + trace 链路 + 事件追踪面板数据）
- plugin_base.py     插件基类（唯一契约）
- plugin_manager.py  Core 插件管理器（扫描/依赖图/兼容检查/懒加载/状态机/心跳）
- service_locator.py 同步服务定位器（插件间同步调用唯一通道）
- config.py          配置管理（JSON）

Core 逻辑不依赖窗口，可用命令行自检：
    python -m nonoka_shell.core --test
"""
import sys

from .event_bus import EventBus
from .plugin_base import Plugin
from .plugin_manager import CorePluginManager
from .service_locator import ServiceLocator
from .config import Config

CORE_VERSION = "1.0.0"  # 与 brand.VERSION 一致（正式发布前）

__all__ = ["EventBus", "Plugin", "CorePluginManager", "ServiceLocator",
           "Config", "CORE_VERSION"]


def _self_test():
    """无窗口自检：事件总线 / 依赖算法 / 服务定位器 / 兼容检查 / 优先级 / 崩溃状态。"""
    import os
    import tempfile
    import time

    ok = []

    # 1) 事件总线（异步）
    bus = EventBus(workers=2)
    got = []
    bus.on("ping", lambda d, tid: got.append((d, tid)))
    bus.emit("ping", {"v": 1})
    time.sleep(0.3)
    assert got and got[0][0] == {"v": 1} and len(got[0][1]) == 8, got
    assert bus.trace(), "事件追踪应为空? no"
    bus.shutdown()
    ok.append("EventBus 异步事件 + trace")

    # 2) 依赖算法
    assert CorePluginManager.toposort({"a": [], "b": ["a"], "c": ["b"]}) == ["a", "b", "c"]
    cyc = CorePluginManager.detect_cycle({"a": ["b"], "b": ["a"]})
    assert cyc and cyc[0] == cyc[-1], cyc
    ok.append("拓扑排序 + DFS 环检测")

    # 3) 服务定位器（同步调用）
    sl = ServiceLocator()
    sl.register("svc", type("S", (), {"add": lambda self, x, y: x + y})())
    assert sl.call("svc", "add", 2, 3) == 5
    assert sl.has("svc") and "svc" in sl.services()
    sl.unregister("svc")
    assert not sl.has("svc")
    ok.append("ServiceLocator 同步服务调用")

    # 4) core_version_min 兼容检查 + 三目录优先级
    mgr = CorePluginManager(core_version="1.2.0")
    tmp = tempfile.mkdtemp()
    for d in ("builtin/A", "market/A", "market/B", "dev/C"):
        os.makedirs(os.path.join(tmp, d), exist_ok=True)
    import json
    json.dump({"id": "A", "name": "A"}, open(os.path.join(tmp, "builtin/A", "plugin.json"), "w"))
    json.dump({"id": "A", "name": "A2", "core_version_min": "2.0.0"},
              open(os.path.join(tmp, "market/A", "plugin.json"), "w"))
    json.dump({"id": "B"}, open(os.path.join(tmp, "market/B", "plugin.json"), "w"))
    json.dump({"id": "C"}, open(os.path.join(tmp, "dev/C", "plugin.json"), "w"))
    mgr.scan(os.path.join(tmp, "builtin"), source="builtin")
    mgr.scan(os.path.join(tmp, "market"), source="market")
    mgr.scan(os.path.join(tmp, "dev"), source="dev")
    assert mgr.meta["A"]["source"] == "builtin", "优先级：内置应覆盖市场"
    assert mgr.meta["A"]["name"] == "A"
    assert mgr.meta["B"]["source"] == "market"
    assert mgr.meta["C"]["source"] == "dev"
    assert mgr.meta["B"]["incompatible"] is False
    assert mgr.meta["A"]["incompatible"] is False  # A 用内置版本（无 core_version_min）
    # 单独构造一个不兼容插件
    assert mgr._check_core_version("2.0.0")[0] is False
    ok.append("core_version_min + 三目录优先级（内置>市场>开发者）")

    # 5) 崩溃状态
    mgr.set_state("B", "running")
    mgr.mark_crashed("B", "boom")
    assert mgr.state("B") == "crashed"
    assert mgr.crash_reason("B") == "boom"
    ok.append("崩溃标记 crashed")

    print("CORE SELF-TEST PASSED (%d checks)" % len(ok))
    for o in ok:
        print("  -", o)
    return 0


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if "--test" in argv:
        return _self_test()
    print("Nonoka Core v%s" % CORE_VERSION)
    print("用法: python -m nonoka_shell.core --test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
