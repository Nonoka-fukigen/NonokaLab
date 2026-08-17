# -*- coding: utf-8 -*-
"""SQLite 数据库管理（标准库 sqlite3，无需额外依赖）。

数据库文件：用户文档/NonokaLab/data/nonoka.db
表：
  - download_history(id, url, title, download_type, save_path, created_at)
  - plugin_status(plugin_id PK, enabled, version, last_check, last_update, running_status)
  - settings(key PK, value)   # JSON 字符串，配置项优先从此读取，config.json 兜底
"""
import datetime
import json
import os
import sqlite3
import threading

from .utils import get_data_dir
from .logger import get_logger

_log = get_logger("db")


class Database:
    def __init__(self, path=None):
        self.path = path or os.path.join(get_data_dir(), "data", "nonoka.db")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        with self._lock:
            c = self._conn
            c.execute(
                "CREATE TABLE IF NOT EXISTS download_history ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, title TEXT, "
                "download_type TEXT, save_path TEXT, created_at TEXT)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS plugin_status ("
                "plugin_id TEXT PRIMARY KEY, enabled INTEGER, version TEXT, "
                "last_check TEXT, last_update TEXT)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS settings ("
                "key TEXT PRIMARY KEY, value TEXT)"
            )
            # 注册表自清洁：记录本软件写入过的所有键
            c.execute(
                "CREATE TABLE IF NOT EXISTS registry_keys ("
                "hive TEXT, path TEXT, name TEXT, value TEXT, created_at TEXT, "
                "PRIMARY KEY (hive, path, name))"
            )
            # 兼容旧库：补齐 running_status 列
            try:
                c.execute("ALTER TABLE plugin_status ADD COLUMN running_status TEXT")
            except Exception:
                pass
            # 兼容旧库：补齐 config_version 列（配置迁移用）
            try:
                c.execute("ALTER TABLE plugin_status ADD COLUMN config_version INTEGER")
            except Exception:
                pass
            c.commit()

    # ----------------------- download_history -----------------------
    def add_download(self, url, title, download_type, save_path):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._conn.execute(
                "INSERT INTO download_history (url, title, download_type, save_path, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (url or "", title or "", download_type or "", save_path or "", ts),
            )
            self._conn.commit()

    def get_downloads(self, limit=100):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM download_history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_download(self, did):
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM download_history WHERE id=?", (did,)
            ).fetchone()
        return dict(r) if r else None

    def search_downloads(self, q, limit=200):
        q = (q or "").strip()
        with self._lock:
            if q:
                like = "%" + q + "%"
                rows = self._conn.execute(
                    "SELECT * FROM download_history WHERE title LIKE ? OR url LIKE ? "
                    "ORDER BY id DESC LIMIT ?", (like, like, limit)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM download_history ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def delete_download(self, did):
        with self._lock:
            self._conn.execute("DELETE FROM download_history WHERE id=?", (did,))
            self._conn.commit()
        return True

    def clear_downloads(self):
        with self._lock:
            self._conn.execute("DELETE FROM download_history")
            self._conn.commit()

    # ----------------------- plugin_status -----------------------
    def upsert_plugin(self, pid, enabled=None, version=None, last_check=None,
                      last_update=None, running=None, config_version=None):
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM plugin_status WHERE plugin_id=?", (pid,)
            ).fetchone()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if cur:
                fields = {}
                if enabled is not None:
                    fields["enabled"] = 1 if enabled else 0
                if version is not None:
                    fields["version"] = version
                if last_check is not None:
                    fields["last_check"] = last_check
                if last_update is not None:
                    fields["last_update"] = last_update
                if running is not None:
                    fields["running_status"] = running
                if config_version is not None:
                    fields["config_version"] = int(config_version)
                if not fields:
                    return
                sql = "UPDATE plugin_status SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE plugin_id=?"
                self._conn.execute(sql, list(fields.values()) + [pid])
            else:
                self._conn.execute(
                    "INSERT INTO plugin_status "
                    "(plugin_id, enabled, version, last_check, last_update, running_status, config_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (pid, 1 if (enabled if enabled is not None else True) else 0,
                     version or "", last_check or now, last_update or "",
                     running or "stopped", int(config_version if config_version is not None else 1)),
                )
            self._conn.commit()

    def set_plugin_running(self, pid, status):
        self.upsert_plugin(pid, running=status)

    def get_plugin(self, pid):
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM plugin_status WHERE plugin_id=?", (pid,)
            ).fetchone()
        return dict(r) if r else None

    def get_plugins(self):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM plugin_status").fetchall()
        return [dict(r) for r in rows]

    def set_plugin_enabled(self, pid, enabled):
        self.upsert_plugin(pid, enabled=enabled)

    # ----------------------- settings -----------------------
    def set_setting(self, key, value):
        blob = json.dumps(value, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, blob),
            )
            self._conn.commit()

    def get_setting(self, key, default=None):
        with self._lock:
            r = self._conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        if not r:
            return default
        try:
            return json.loads(r["value"])
        except Exception:
            return default

    # ----------------------- registry_keys（注册表自清洁） -----------------------
    def record_registry_key(self, hive, path, name, value=""):
        with self._lock:
            self._conn.execute(
                "INSERT INTO registry_keys (hive, path, name, value, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(hive, path, name) DO UPDATE SET value=excluded.value",
                (hive, path, name, value or "",
                 datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            self._conn.commit()

    def get_registry_keys(self):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM registry_keys").fetchall()
        return [dict(r) for r in rows]

    def remove_registry_key(self, hive, path, name):
        with self._lock:
            self._conn.execute(
                "DELETE FROM registry_keys WHERE hive=? AND path=? AND name=?",
                (hive, path, name))
            self._conn.commit()

    def clear_registry_keys(self):
        with self._lock:
            self._conn.execute("DELETE FROM registry_keys")
            self._conn.commit()

    # ----------------------- 统计 -----------------------
    def stats(self):
        with self._lock:
            dh = self._conn.execute("SELECT COUNT(*) AS c FROM download_history").fetchone()["c"]
            pl = self._conn.execute("SELECT COUNT(*) AS c FROM plugin_status").fetchone()["c"]
        return {"downloads": dh, "plugins": pl, "path": self.path}

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
