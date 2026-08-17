# -*- coding: utf-8 -*-
"""更新检查：比对本地版本与远程（GitHub Releases latest）。

UPDATE_API 为空时直接返回「无需检查」。
- check()：返回 {update_available, current, latest, url, download_url, notes, error}
- check_async(cb)：后台检查，结果回传回调
- download_update(cb)：后台下载安装包到 用户文档/NonokaLab/updates/，进度通过 cb 回传
  注意：软件无法自我覆盖，下载完成后需用户手动运行安装包。
所有网络异常均静默处理（不弹错、不阻塞）。
"""
import json
import os
import threading

import requests

from .brand import VERSION, UPDATE_API, REPO_URL
from .utils import get_data_dir
from .logger import get_logger

_log = get_logger("updater")

# 安装包命名规则：NonokaLab_Setup_vX.X.X.exe
_SETUP_GLOB = ("NonokaLab_Setup", ".exe")


def _parse_version(v):
    """把版本字符串解析为可比较的 (major, minor, patch, build) 元组。

    兼容多种来源：'v1.2.3'、'NonokaLab_Setup_v1.2.3.exe'、
    'release-2.0.1-beta'、'1.0' 等。先提取第一段形如 X.Y.Z[.N] 的数字串，
    再按 '.' 转整数；不足 4 段补 0，多余段截断。
    """
    import re
    s = (v or "").strip()
    m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?", s)
    if not m:
        return (0, 0, 0, 0)
    parts = [int(g) if g is not None else 0 for g in m.groups()]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def _pick_asset(assets):
    """优先选择匹配安装包命名规则的资产，其次任意 .exe。"""
    if not assets:
        return None
    for a in assets:
        name = (a.get("name") or "").lower()
        if name.startswith(_SETUP_GLOB[0].lower()) and name.endswith(".exe"):
            return a.get("browser_download_url")
    for a in assets:
        name = (a.get("name") or "").lower()
        if name.endswith(".exe"):
            return a.get("browser_download_url")
    return None


class Updater:
    def __init__(self, ctx):
        self.ctx = ctx

    def check(self):
        """返回 {update_available, current, latest, url, download_url, notes, error}。"""
        if not UPDATE_API:
            return {"update_available": False, "current": VERSION,
                    "latest": VERSION, "url": REPO_URL, "download_url": None,
                    "notes": "", "error": "未配置更新源"}
        try:
            resp = requests.get(UPDATE_API, timeout=12,
                                headers={"Accept": "application/vnd.github+json"})
            if resp.status_code != 200:
                return {"update_available": False, "current": VERSION,
                        "latest": VERSION, "url": REPO_URL, "download_url": None,
                        "notes": "", "error": f"HTTP {resp.status_code}"}
            data = resp.json()
            latest = (data.get("tag_name") or "").lstrip("vV") or VERSION
            url = data.get("html_url") or REPO_URL
            notes = data.get("body") or ""
            cur = _parse_version(VERSION)
            new = _parse_version(latest)
            avail = new > cur
            dl = _pick_asset(data.get("assets") or [])
            return {"update_available": avail, "current": VERSION,
                    "latest": latest, "url": url, "download_url": dl,
                    "notes": notes, "error": ""}
        except Exception as e:
            _log.warning("更新检查失败: %s", e)
            return {"update_available": False, "current": VERSION,
                    "latest": VERSION, "url": REPO_URL, "download_url": None,
                    "notes": "", "error": str(e)}

    def check_async(self, callback):
        """异步检查，结果通过 callback(result) 回传。"""
        def run():
            try:
                callback(self.check())
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def download_update(self, callback):
        """后台下载安装包到 updates/ 目录。cb 收到：
        {progress:true, fetched,total} | {done:true, ok, path, error}。
        """
        def run():
            info = self.check()
            if not info.get("update_available"):
                callback({"ok": False, "error": "no_update"})
                return
            url = info.get("download_url")
            if not url:
                callback({"ok": False, "error": "no_asset"})
                return
            dest = os.path.join(get_data_dir(), "updates")
            try:
                os.makedirs(dest, exist_ok=True)
            except Exception:
                pass
            fname = url.split("?")[0].rstrip("/").split("/")[-1] or "NonokaLab_Setup.exe"
            path = os.path.join(dest, fname)
            try:
                with requests.get(url, stream=True, timeout=30) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("content-length", 0) or 0)
                    done = 0
                    with open(path, "wb") as f:
                        for chunk in r.iter_content(8192):
                            if chunk:
                                f.write(chunk)
                                done += len(chunk)
                                callback({"ok": True, "progress": True,
                                          "fetched": done, "total": total})
                callback({"ok": True, "done": True, "path": path})
            except Exception as e:
                _log.warning("安装包下载失败: %s", e)
                callback({"ok": False, "error": str(e)})
        threading.Thread(target=run, daemon=True).start()
