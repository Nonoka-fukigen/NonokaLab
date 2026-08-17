# -*- coding: utf-8 -*-
"""使用统计：本地统计，不上传任何数据。

统计项：下载总数、各类型下载次数、使用天数、插件启用数量。
"""
from .logger import get_logger

_log = get_logger("stats")


class Stats:
    def __init__(self, ctx):
        self.ctx = ctx

    def collect(self):
        out = {"downloads": 0, "by_type": {}, "days": 0, "plugins_enabled": 0}
        db = self.ctx.db
        if db is not None:
            try:
                rows = db.get_downloads(limit=100000)
                out["downloads"] = len(rows)
                by_type = {}
                days = set()
                for r in rows:
                    t = r.get("download_type") or "unknown"
                    by_type[t] = by_type.get(t, 0) + 1
                    ca = r.get("created_at")
                    if ca:
                        days.add(str(ca)[:10])
                out["by_type"] = by_type
                out["days"] = len(days)
            except Exception as e:
                _log.warning("统计下载失败: %s", e)
        try:
            if self.ctx.plugin_manager is not None:
                out["plugins_enabled"] = sum(
                    1 for p in self.ctx.plugin_manager.list_meta() if p.get("enabled"))
        except Exception:
            pass
        return out
