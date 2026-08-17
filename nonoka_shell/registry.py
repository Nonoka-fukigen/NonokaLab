# -*- coding: utf-8 -*-
r"""注册表自清洁（Windows HKCU only，无需管理员权限）。

原则：
- 所有注册表操作集中在 Registry 类，软件自己知道写了哪些键（记录到 DB）
- 只使用 HKCU，不碰 HKLM
- 卸载 / 退出时删除所有自己创建的键
- 启动时检查旧版本残留，提示用户清理
- 开机自启键：HKCU\Software\Microsoft\Windows\CurrentVersion\Run\NonokaLab
"""
import os
import sys

from .logger import get_logger

_log = get_logger("registry")

RUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "NonokaLab"


def _winreg():
    if sys.platform != "win32":
        return None
    try:
        import winreg
        return winreg
    except Exception:
        return None


class Registry:
    """集中管理本软件写入的注册表键，并记录到 DB 以便自清洁。"""

    def __init__(self, db=None):
        self.db = db

    # ---------------- 读写 ----------------
    def set_key(self, path, name, value, value_type="REG_SZ", hive="HKCU"):
        wr = _winreg()
        if not wr:
            return False
        try:
            root = wr.HKEY_CURRENT_USER if hive == "HKCU" else wr.HKEY_LOCAL_MACHINE
            key = wr.CreateKeyEx(root, path, 0, wr.KEY_SET_VALUE)
            wr.SetValueEx(key, name, 0, getattr(wr, value_type, wr.REG_SZ), value)
            wr.CloseKey(key)
            if self.db is not None:
                self.db.record_registry_key(hive, path, name, str(value))
            _log.info("registry write %s\\%s\\%s", hive, path, name)
            return True
        except Exception as e:
            _log.warning("registry write failed %s\\%s\\%s: %s", hive, path, name, e)
            return False

    def get_key(self, path, name, default=None, hive="HKCU"):
        wr = _winreg()
        if not wr:
            return default
        try:
            root = wr.HKEY_CURRENT_USER if hive == "HKCU" else wr.HKEY_LOCAL_MACHINE
            key = wr.OpenKey(root, path)
            try:
                val, _ = wr.QueryValueEx(key, name)
                return val
            finally:
                wr.CloseKey(key)
        except Exception:
            return default

    def delete_key(self, path, name, hive="HKCU"):
        wr = _winreg()
        ok = False
        if wr:
            try:
                root = wr.HKEY_CURRENT_USER if hive == "HKCU" else wr.HKEY_LOCAL_MACHINE
                key = wr.OpenKey(root, path, 0, wr.KEY_SET_VALUE)
                wr.DeleteValue(key, name)
                wr.CloseKey(key)
                ok = True
            except FileNotFoundError:
                ok = True  # 本来就不存在，视为已清理
            except Exception as e:
                _log.warning("registry delete failed %s: %s", name, e)
        if self.db is not None:
            self.db.remove_registry_key(hive, path, name)
        return ok

    # ---------------- 自清洁 ----------------
    def created_keys(self):
        """DB 中记录的本软件写过的键。"""
        if self.db is None:
            return []
        return self.db.get_registry_keys()

    def check_leftovers(self):
        """比对 DB 记录与实际注册表：返回残留（DB 有记录但注册表已存在不同值 / 丢失）。"""
        leftovers = []
        for rec in self.created_keys():
            val = self.get_key(rec["path"], rec["name"],
                               default="__MISSING__", hive=rec.get("hive") or "HKCU")
            if val == "__MISSING__":
                leftovers.append({"hive": rec["hive"], "path": rec["path"],
                                  "name": rec["name"], "issue": "missing",
                                  "expected": rec["value"]})
            elif val != rec["value"]:
                leftovers.append({"hive": rec["hive"], "path": rec["path"],
                                  "name": rec["name"], "issue": "changed",
                                  "expected": rec["value"], "actual": val})
        return leftovers

    def cleanup(self):
        """删除所有 DB 记录的自建键（卸载 / 退出清理）。返回删除数量。"""
        n = 0
        for rec in self.created_keys():
            if self.delete_key(rec["path"], rec["name"], hive=rec.get("hive") or "HKCU"):
                n += 1
        return n

    # ---------------- 开机自启（便捷封装） ----------------
    def set_autostart(self, enable):
        if sys.platform != "win32":
            return False, "当前平台不支持开机自启"
        if enable:
            exe = os.path.abspath(sys.executable) if getattr(sys, "frozen", False) else None
            if not exe:
                return False, "开发模式下无法设置自启，请打包为安装程序后使用"
            ok = self.set_key(RUN_PATH, AUTOSTART_NAME, '"%s"' % exe)
            return (ok, "" if ok else "写入注册表失败")
        self.delete_key(RUN_PATH, AUTOSTART_NAME)
        return True, ""

    def is_autostart(self):
        return self.get_key(RUN_PATH, AUTOSTART_NAME) is not None
