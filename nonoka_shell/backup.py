# -*- coding: utf-8 -*-
"""数据导入 / 导出：把用户数据目录（数据库、配置、插件状态、日志）打包为 ZIP，
从 ZIP 恢复（覆盖同名文件，不删除既有其它文件）。
"""
import os
import zipfile

from .utils import get_data_dir
from .logger import get_logger

_log = get_logger("backup")

_TOP = "NonokaLab"  # ZIP 内顶层目录，便于安全解压


def export_data(dest_path):
    data_dir = get_data_dir()
    try:
        with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as z:
            if os.path.isdir(data_dir):
                for root, _dirs, files in os.walk(data_dir):
                    for f in files:
                        fp = os.path.join(root, f)
                        rel = os.path.relpath(fp, data_dir)
                        z.write(fp, os.path.join(_TOP, rel))
        return {"ok": True, "path": dest_path}
    except Exception as e:
        _log.warning("导出失败: %s", e)
        return {"ok": False, "error": str(e)}


def import_data(src_path):
    data_dir = get_data_dir()
    try:
        with zipfile.ZipFile(src_path, "r") as z:
            for member in z.namelist():
                parts = member.split("/", 1)
                if len(parts) < 2 or parts[0] != _TOP:
                    continue
                dest = os.path.join(data_dir, parts[1])
                if member.endswith("/"):
                    os.makedirs(dest, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with z.open(member) as src, open(dest, "wb") as out:
                        out.write(src.read())
        return {"ok": True}
    except Exception as e:
        _log.warning("导入失败: %s", e)
        return {"ok": False, "error": str(e)}
