# -*- coding: utf-8 -*-
"""任务队列管理：统一调度插件的后台任务（如下载）。

特性：
  - 排队 / 运行 / 完成 / 失败 / 取消 / 暂停 状态
  - 并行数限制（默认 2），通过 set_max_parallel 调整
  - 手动排序（上移 / 下移，仅影响排队中任务）
  - 暂停 / 恢复 / 取消 单个任务
  - 插件通过 submit(plugin_id, kind, title, payload, runner) 提交；
    runner(task) 在线程中执行，可调用 task.set_progress / 检查 task.is_cancelled()

注意：第三方下载核心为单请求阻塞式，运行中的任务无法真正中断，因此取消 / 暂停
为「尽力而为」——标记状态并阻止新任务启动，已在 README 说明。
"""
import threading
import time
import uuid

from .logger import get_logger

_log = get_logger("queue")


class Task:
    def __init__(self, tid, plugin_id, kind, title, payload):
        self.id = tid
        self.plugin_id = plugin_id
        self.kind = kind
        self.title = title
        self.payload = payload
        self.status = "queued"   # queued|running|done|failed|cancelled|paused
        self.percent = 0
        self.message = ""
        self.error = ""
        self.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._runner = None

    def set_progress(self, percent, message=""):
        with self._lock:
            self.percent = percent
            if message:
                self.message = message

    def cancel(self):
        self.cancel_event.set()

    def is_cancelled(self):
        return self.cancel_event.is_set()


class QueueManager:
    def __init__(self, ctx, max_parallel=2):
        self.ctx = ctx
        self._tasks = {}
        self._order = []
        self._lock = threading.Lock()
        self._max = max(1, int(max_parallel or 1))
        self._paused = False
        self._running = 0

    # ---------------- 提交 / 调度 ----------------
    def submit(self, plugin_id, kind, title, payload, runner):
        tid = uuid.uuid4().hex
        t = Task(tid, plugin_id, kind, title, payload)
        t._runner = runner
        with self._lock:
            self._tasks[tid] = t
            self._order.append(tid)
        self._pump()
        return t

    def _pump(self):
        with self._lock:
            if self._paused:
                return
            while self._running < self._max:
                nxt = None
                for tid in self._order:
                    t = self._tasks.get(tid)
                    if t and t.status == "queued":
                        nxt = t
                        break
                if not nxt:
                    break
                nxt.status = "running"
                self._running += 1
                threading.Thread(target=self._run, args=(nxt,), daemon=True).start()

    def _run(self, task):
        try:
            if task.is_cancelled():
                task.status = "cancelled"
                return
            task._runner(task)
            if task.status == "running":
                task.status = "cancelled" if task.is_cancelled() else "done"
        except Exception as e:
            _log.exception("任务 %s 执行异常", task.id[:8])
            task.status = "failed"
            task.error = str(e)
        finally:
            with self._lock:
                self._running -= 1
            self._pump()

    # ---------------- 控制 ----------------
    def cancel(self, task_id):
        t = self._tasks.get(task_id)
        if not t:
            return False
        t.cancel()
        if t.status == "queued":
            t.status = "cancelled"
        return True

    def pause(self):
        with self._lock:
            self._paused = True
            for tid in self._order:
                t = self._tasks.get(tid)
                if t and t.status == "running":
                    t.status = "paused"

    def resume(self):
        with self._lock:
            self._paused = False
            for tid in self._order:
                t = self._tasks.get(tid)
                if t and t.status == "paused":
                    t.status = "running"
        self._pump()

    def set_max_parallel(self, n):
        with self._lock:
            self._max = max(1, int(n or 1))
        self._pump()

    def move(self, task_id, direction):
        """排序：direction = 'up' | 'down'（仅影响排队中任务的位置）。"""
        with self._lock:
            if task_id in self._order:
                i = self._order.index(task_id)
                j = i - 1 if direction == "up" else i + 1
                if 0 <= j < len(self._order):
                    self._order[i], self._order[j] = self._order[j], self._order[i]
                    return True
        return False

    def list(self):
        with self._lock:
            return [self._snapshot(self._tasks[tid]) for tid in self._order
                    if self._tasks.get(tid)]

    def get(self, task_id):
        t = self._tasks.get(task_id)
        return self._snapshot(t) if t else None

    def tasks_for(self, plugin_id):
        return [s for s in self.list() if s["plugin_id"] == plugin_id]

    def _snapshot(self, t):
        return {
            "id": t.id, "plugin_id": t.plugin_id, "kind": t.kind,
            "title": t.title, "status": t.status, "percent": t.percent,
            "message": t.message, "error": t.error, "created_at": t.created_at,
        }
