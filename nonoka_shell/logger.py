# -*- coding: utf-8 -*-
"""Nonoka 日志系统（简洁版，保留网络思想）。

- 级别：DEBUG / INFO / WARNING / ERROR；DEBUG 仅在开发者模式开启
- 全局单调序号 [NNNN]：类似 TCP sequence number，跳号即日志丢失
- 异步缓冲 + 流量控制：内存队列（上限 1000），满时丢弃 DEBUG（非关键日志）
- 路由：nonoka.log（Core 核心日志）、plugin_<id>.log（插件日志）、
  crash_report.json（崩溃报告，见 crash_report.py）
- 心跳：heartbeat() 记录插件最后心跳，Core 监控 30s 无心跳标记「可能卡死」
- 事件追踪：EventBus 每次 emit 带 trace_id，log_event() 落链路日志；实时面板读 EventBus.trace()
- 轮转：每文件 5MB，保留最近 3 个
"""
import atexit
import collections
import itertools
import logging
import logging.handlers
import os
import queue
import threading
import time

from .brand import LOG_DIR, LOG_FILE
from .utils import get_data_dir

_SEQ = itertools.count(1)
_QUEUE = queue.Queue(maxsize=1000)      # 异步缓冲（流量控制：满时丢 DEBUG）
_LAST_BEAT = {}                          # name -> 最后心跳时间戳
_HANDLERS = {}                           # path -> RotatingFileHandler
_STOP = threading.Event()
_LOGGERS = {}
_DEBUG_ENABLED = False                   # 开发者模式开启后才记录 DEBUG

# ---- 控制台（内存日志缓冲，供「控制台」页面实时展示） ----
_CONSOLE = collections.deque(maxlen=600)   # 最近 600 条（含 seq 的结构化记录）
_CONSOLE_LOCK = threading.Lock()
_SUBSCRIBERS = set()                       # 新日志回调集合（用于实时推送到前端）

# ---- 控制台推送节流（防止高并发日志刷爆前端 UI / 拖垮进程） ----
# 核心：logging worker 只负责把新记录写入缓冲与待推送队列，不 inline 调用订阅者；
# 由独立的推送线程按固定间隔合并待推送记录后统一回调，从而把对前端的
# evaluate_js 推送频率限制为 ~1次/秒，即使日志瞬时成千上万条也不会卡死。
_PENDING = []                              # 待推送给订阅者的新记录（合并批次）
_PENDING_LOCK = threading.Lock()
_PENDING_EVT = threading.Event()
_PUSH_INTERVAL = 0.4                       # 前端推送合并间隔（秒）

_ROTATE_BYTES = 5 * 1024 * 1024          # 5MB
_BACKUPS = 3


def set_debug(enabled):
    """开发者模式：开启后记录 DEBUG 级别日志。"""
    global _DEBUG_ENABLED
    _DEBUG_ENABLED = bool(enabled)


def _log_dir():
    d = os.path.join(get_data_dir(), LOG_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def _plugin_id(name):
    """从 logger 名提取插件 id（如 nonoka.Nonoka_video_download → Nonoka_video_download）。"""
    n = name.rsplit(".", 1)[-1]
    return n if n.startswith("Nonoka_") else None


def _targets(name):
    """日志路由：Core 日志 → nonoka.log；插件日志 → plugin_<id>.log。"""
    base = _log_dir()
    pid = _plugin_id(name)
    if pid:
        return [(os.path.join(base, "plugin_%s.log" % pid),)]
    return [(os.path.join(base, LOG_FILE),)]


def _file_handler(path):
    h = _HANDLERS.get(path)
    if h is None:
        h = logging.handlers.RotatingFileHandler(
            path, maxBytes=_ROTATE_BYTES, backupCount=_BACKUPS,
            encoding="utf-8", delay=True)
        h.setFormatter(logging.Formatter(
            "[%(seq)04d] %(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        _HANDLERS[path] = h
    return h


def _write(record):
    try:
        for targets in _targets(record.name):
            _file_handler(targets[0]).emit(record)
    except Exception:
        pass


def _collect_console(records):
    """把一批日志记录写入内存控制台缓冲，并放入待推送队列（由推送线程合并推送）。

    注意：这里不再 inline 调用订阅者，避免 logging 线程被前端推送阻塞拖慢。
    """
    if not records:
        return
    new = []
    for rec in records:
        try:
            msg = rec.getMessage()
        except Exception:
            msg = str(getattr(rec, "msg", ""))
        new.append({
            "seq": getattr(rec, "seq", 0),
            "t": int(rec.created * 1000),          # 毫秒时间戳
            "level": rec.levelname or "INFO",
            "name": rec.name or "",
            "msg": msg,
        })
    with _CONSOLE_LOCK:
        _CONSOLE.extend(new)
    with _PENDING_LOCK:
        _PENDING.extend(new)
    _PENDING_EVT.set()


def _push_worker():
    """控制台推送线程：按固定间隔把累积的待推送记录合并后回调订阅者。

    保证对订阅者（→ 前端 evaluate_js）的调用频率不超过 ~1/秒，无论日志多密集。
    """
    global _PENDING
    while not _STOP.is_set():
        _PENDING_EVT.wait(_PUSH_INTERVAL)   # 有新数据立即醒来，否则按间隔节流
        _PENDING_EVT.clear()
        with _PENDING_LOCK:
            pending = _PENDING
            _PENDING = []
        if not pending:
            continue
        for cb in list(_SUBSCRIBERS):
            try:
                cb(pending)
            except Exception:
                pass


def get_console(from_seq=0, limit=300):
    """控制台：返回 (records, last_seq)。from_seq>0 时只返回该 seq 之后的新记录（增量）。"""
    with _CONSOLE_LOCK:
        items = list(_CONSOLE)
    if from_seq:
        items = [r for r in items if r["seq"] > from_seq]
    last_seq = max((r["seq"] for r in items), default=from_seq)
    return items[-limit:], last_seq


def subscribe_new(cb):
    """订阅新日志回调（cb 以 records 列表为参数）。"""
    _SUBSCRIBERS.add(cb)


def unsubscribe_new(cb):
    _SUBSCRIBERS.discard(cb)


def _worker():
    while not _STOP.is_set():
        try:
            record = _QUEUE.get(timeout=0.5)
        except queue.Empty:
            continue
        batch = [record]
        try:
            while len(batch) < 100:
                batch.append(_QUEUE.get_nowait())
        except queue.Empty:
            pass
        for rec in batch:
            _write(rec)
        _collect_console(batch)
    while True:
        try:
            rec = _QUEUE.get_nowait()
        except queue.Empty:
            break
        _write(rec)
        try:
            _collect_console([rec])
        except Exception:
            pass


class _AsyncHandler(logging.Handler):
    """异步缓冲 Handler：入内存队列，worker 批量刷盘；满时丢弃 DEBUG。"""

    def emit(self, record):
        record.seq = next(_SEQ)
        if record.levelno <= logging.DEBUG and not _DEBUG_ENABLED:
            return  # DEBUG 仅在开发者模式
        try:
            _QUEUE.put_nowait(record)
        except queue.Full:
            if record.levelno > logging.DEBUG:
                try:
                    _QUEUE.get_nowait()
                    _QUEUE.put_nowait(record)
                except Exception:
                    pass


def _configure_root():
    root = logging.getLogger("nonoka")
    if getattr(root, "_nk_async", False):
        return
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.addHandler(_AsyncHandler())
    root.propagate = False
    root._nk_async = True
    threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_push_worker, daemon=True).start()
    atexit.register(_shutdown)


def _shutdown():
    _STOP.set()
    try:
        for h in _HANDLERS.values():
            h.flush()
            h.close()
    except Exception:
        pass


def get_logger(name="nonoka"):
    """兼容既有接口：返回标准 logging.Logger（异步写入）。"""
    _configure_root()
    if name in _LOGGERS:
        return _LOGGERS[name]
    lg = logging.getLogger("nonoka") if name == "nonoka" else logging.getLogger("nonoka." + name)
    lg.propagate = True
    _LOGGERS[name] = lg
    return lg


def get_log_path():
    return os.path.join(get_data_dir(), LOG_DIR, LOG_FILE)


def heartbeat(name):
    """插件心跳：记录最后心跳时间并写一条心跳日志（每 5 秒由管理器调用）。"""
    _LAST_BEAT[name] = time.time()
    try:
        get_logger(name).info("[HB] heartbeat")
    except Exception:
        pass


def heartbeat_age(name, now=None):
    ts = _LAST_BEAT.get(name)
    if ts is None:
        return None
    return (now or time.time()) - ts


def log_event(trace_id, topic, detail=""):
    """事件总线链路日志（搜索 trace_id 可见完整调用链）。"""
    try:
        get_logger("bus").info("[trace=%s] emit %s%s", trace_id, topic,
                               (" " + str(detail)) if detail else "")
    except Exception:
        pass
