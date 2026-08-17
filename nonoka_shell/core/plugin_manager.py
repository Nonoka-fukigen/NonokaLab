# -*- coding: utf-8 -*-
"""Core 插件管理器：扫描、加载、依赖图管理、状态管理。

只提供机制（规则），不包含任何业务逻辑：
- 三目录扫描：内置（安装目录，只读）> 市场（文档/NonokaLab/plugins）> 开发者（dev_plugins）
- ID 冲突优先级：内置 > 市场 > 开发者（后扫描目录不覆盖已存在条目）
- 依赖图：拓扑排序 + DFS 环检测 + dependents 查询
- core_version_min 兼容检查：不符合标为「不兼容」，不加载，并给出原因
- 懒加载：不启动的插件不 import
- 状态机：stopped -> running -> (paused -> running) -> stopped；崩溃标记 crashed
- 心跳：heartbeat / health（🟢 正常 / 🟡 无响应 / 🔴 卡死）
"""
import importlib.util
import json
import logging
import re
import sys
import time
from pathlib import Path

log = logging.getLogger("nonoka.core")


class CorePluginManager:
    """Core 层的插件管理器（纯机制）。"""

    def __init__(self, core_version="1.0.0", bus=None, services=None):
        self.core_version = core_version
        # Core 能力组合：事件总线（异步）+ 服务定位器（同步）
        from .event_bus import EventBus
        from .service_locator import ServiceLocator
        self.bus = bus or EventBus()
        self.services = services or ServiceLocator()
        self.plugins = {}          # id -> 已加载实例（懒加载）
        self.meta = {}             # id -> plugin.json 元数据（含 source / incompatible）
        self.dirs = {}             # id -> 插件目录
        self.order = []            # 拓扑排序结果
        self.cycle = None          # 检测到的依赖环（若有）
        self.last_beat = {}        # id -> 最后心跳时间戳
        self._states = {}          # id -> running/stopped/paused/crashed
        self._crashed = {}         # id -> 崩溃原因

    # ---------------- 服务定位器 / 事件总线（同步/异步调用） ----------------
    def get_service(self, plugin_id):
        """同步获取其它插件注册的服务（禁止直接 import）。"""
        return self.services.get(plugin_id)

    def register_service(self, plugin_id, service):
        self.services.register(plugin_id, service)

    def unregister_service(self, plugin_id):
        self.services.unregister(plugin_id)

    def emit(self, topic, data=None):
        self.bus.emit(topic, data)

    # ---------------- 扫描 / 依赖 ----------------
    def scan(self, directory, source="market"):
        """扫描一个插件目录并累积元数据；source ∈ builtin|market|dev。

        优先级规则：已存在的插件 id 不被后扫描目录覆盖（内置 > 市场 > 开发者）。
        """
        if not directory or not Path(directory).is_dir():
            return self.order
        for path in sorted(Path(directory).glob("*/plugin.json")):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                log.error("scan %s: %s", path, e)
                continue
            pid = meta.get("id") or path.parent.name
            if pid in self.meta:
                log.info("插件 %s 已存在（优先级更高），跳过 %s", pid, source)
                continue
            for k in ("provides", "consumes", "permissions"):
                meta.setdefault(k, [])
            meta.setdefault("sha256", "")
            meta.setdefault("config_version", 1)
            meta.setdefault("builtin", source == "builtin")
            meta.setdefault("system", False)
            meta["source"] = source
            ok, reason = self._check_core_version(meta.get("core_version_min") or "")
            meta["incompatible"] = not ok
            meta["incompatible_reason"] = reason if not ok else ""
            self.meta[pid] = meta
            self.dirs[pid] = str(path.parent)
        self._rebuild_deps()
        return self.order

    def _rebuild_deps(self):
        deps = {pid: list(m.get("consumes", [])) for pid, m in self.meta.items()}
        self.order = self.toposort(deps)
        self.cycle = self.detect_cycle(deps)
        if self.cycle:
            log.error("检测到循环依赖: %s", " -> ".join(self.cycle))

    def _check_core_version(self, need):
        """core_version_min 兼容检查：返回 (ok, reason)。"""
        if not need:
            return True, ""
        if self._version_tuple(self.core_version) < self._version_tuple(need):
            return False, "需要 Core v%s 或更高（当前 v%s）" % (need, self.core_version)
        return True, ""

    @staticmethod
    def _version_tuple(v):
        m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(v or ""))
        if not m:
            return (0, 0, 0)
        return tuple(int(g) if g else 0 for g in m.groups())

    @staticmethod
    def toposort(deps):
        """DFS 拓扑排序：被依赖者先启动；遇环跳过该环（环由 detect_cycle 报告）。"""

        def visit(node, visited, visiting, result):
            if node in visited or node not in deps or node in visiting:
                return
            visiting.add(node)
            for d in deps.get(node, []):
                if d in deps:
                    visit(d, visited, visiting, result)
            visiting.discard(node)
            visited.add(node)
            result.append(node)

        visited, visiting, result = set(), set(), []
        for n in deps:
            visit(n, visited, visiting, result)
        return result

    @staticmethod
    def detect_cycle(deps):
        """DFS 环检测：返回环路径（如 A->B->A），无环返回 None。"""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in deps}
        stack = []

        def dfs(node):
            color[node] = GRAY
            stack.append(node)
            for d in deps.get(node, []):
                if d not in deps:
                    continue
                if color[d] == GRAY:
                    return stack[stack.index(d):] + [d]
                if color[d] == WHITE:
                    cycle = dfs(d)
                    if cycle:
                        return cycle
            stack.pop()
            color[node] = BLACK
            return None

        for n in deps:
            if color[n] == WHITE:
                cycle = dfs(n)
                if cycle:
                    return cycle
        return None

    def dependents(self, plugin_id):
        """哪些插件 consumes 了 plugin_id（卸载前依赖检查）。"""
        return [pid for pid, m in self.meta.items()
                if plugin_id in m.get("consumes", [])]

    # ---------------- 懒加载 / 生命周期 ----------------
    def is_compatible(self, pid):
        return not bool(self.meta.get(pid, {}).get("incompatible", False))

    def load(self, plugin_id):
        """按需加载插件模块（懒加载：不调用不 import）。"""
        if plugin_id in self.plugins:
            return self.plugins[plugin_id]
        if not self.is_compatible(plugin_id):
            return None
        d = self.dirs.get(plugin_id)
        if not d:
            return None
        name = "plugin_" + plugin_id.replace("-", "_")
        spec = importlib.util.spec_from_file_location(name, str(Path(d) / "plugin.py"))
        if not spec or not spec.loader:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        if d not in sys.path:
            sys.path.insert(0, d)
        spec.loader.exec_module(mod)
        cls = getattr(mod, "Plugin", None)
        inst = cls() if isinstance(cls, type) else None
        self.plugins[plugin_id] = inst
        return inst

    def activate(self, plugin_id):
        inst = self.load(plugin_id)
        if inst is not None:
            self.heartbeat(plugin_id)
            try:
                inst.activate(self)
            except Exception as e:
                self.mark_crashed(plugin_id, str(e))
        return inst

    def deactivate(self, plugin_id):
        inst = self.plugins.pop(plugin_id, None)
        if inst is not None:
            try:
                inst.deactivate()
            except Exception as e:
                log.error("deactivate %s: %s", plugin_id, e)

    def stop(self):
        for pid in reversed(self.order):
            self.deactivate(pid)

    # ---------------- 状态机（running/stopped/paused/crashed） ----------------
    def state(self, pid):
        return self._states.get(pid, "stopped")

    def set_state(self, pid, st):
        self._states[pid] = st
        if st != "crashed":
            self._crashed.pop(pid, None)

    def crash_reason(self, pid):
        return self._crashed.get(pid, "")

    def mark_crashed(self, pid, error):
        """插件崩溃：标记 crashed 并记录原因（可触发桌面通知与自动重启）。"""
        self._states[pid] = "crashed"
        self._crashed[pid] = str(error)[:500]
        log.error("插件 %s 已崩溃: %s", pid, error)

    # ---------------- 心跳监控 ----------------
    def heartbeat(self, plugin_id):
        self.last_beat[plugin_id] = time.time()

    def health(self, plugin_id, now=None):
        """🟢 正常(<30s) / 🟡 无响应(30-120s) / 🔴 卡死(>120s)。"""
        if plugin_id not in self.last_beat:
            return "dead"
        age = (now or time.time()) - self.last_beat[plugin_id]
        return "ok" if age < 30 else ("warn" if age < 120 else "dead")
