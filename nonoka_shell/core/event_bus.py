# -*- coding: utf-8 -*-
"""事件总线（异步事件通知）。

- 插件状态变化（启动/停止/崩溃）、下载完成、组件安装完成等事件均通过 EventBus 通知
- 订阅方收到事件后更新 UI 或执行后续动作
- 每次 emit 携带 trace_id（8 位十六进制），支持链路追踪
- 事件记录保存在 trace 缓冲区，供「事件追踪面板」（开发者模式）实时查看
- 插件间需要同步返回结果的调用请走 ServiceLocator（见 service_locator.py）
"""
import logging
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

log = logging.getLogger("nonoka.bus")


class EventBus:
    """异步事件总线：订阅/发布模型，emit 在独立线程池中分发。"""

    def __init__(self, workers=4, trace_limit=500):
        self._subs = defaultdict(list)
        self._trace = []           # [(ts, trace_id, topic, data)]
        self._trace_limit = trace_limit
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers),
                                        thread_name_prefix="nonoka.bus")

    # ---------------- 订阅 / 发布 ----------------
    def on(self, topic, callback):
        """订阅事件。callback(data, trace_id)。"""
        with self._lock:
            self._subs[topic].append(callback)

    def off(self, topic, callback):
        with self._lock:
            try:
                self._subs[topic].remove(callback)
            except ValueError:
                pass

    def emit(self, topic, data=None, trace_id=None):
        """异步发布：事件投递到线程池执行，不阻塞调用方。"""
        trace_id = trace_id or uuid4().hex[:8]
        with self._lock:
            self._trace.append((time.time(), trace_id, topic, data))
            if len(self._trace) > self._trace_limit:
                del self._trace[:len(self._trace) - self._trace_limit]
            callbacks = list(self._subs.get(topic, []))
        for cb in callbacks:
            self._pool.submit(self._safe, cb, data, trace_id)

    def emit_sync(self, topic, data=None, trace_id=None):
        """同步发布：订阅者就地执行（用于关键路径，顺序保证）。"""
        trace_id = trace_id or uuid4().hex[:8]
        with self._lock:
            self._trace.append((time.time(), trace_id, topic, data))
            if len(self._trace) > self._trace_limit:
                del self._trace[:len(self._trace) - self._trace_limit]
            callbacks = list(self._subs.get(topic, []))
        for cb in callbacks:
            self._safe(cb, data, trace_id)

    @staticmethod
    def _safe(cb, data, trace_id):
        try:
            cb(data, trace_id)
        except Exception as e:
            log.error("bus[%s] handler error: %s", trace_id, e)

    # ---------------- 事件追踪面板 ----------------
    def trace(self, limit=200):
        """返回最近事件记录 [(ts, trace_id, topic, data)]，供开发者事件面板。"""
        with self._lock:
            return list(self._trace[-limit:])

    def clear_trace(self):
        with self._lock:
            self._trace.clear()

    def shutdown(self):
        try:
            self._pool.shutdown(wait=False)
        except Exception:
            pass
