# -*- coding: utf-8 -*-
"""通用依赖组件管理器。

负责「体积较大的下载型依赖」（如 ffmpeg，以及未来可能加入的模型文件）：
  - 多位置探测（用户数据目录 / 安装目录 / 系统 PATH）
  - 状态查询
  - 下载（带进度回调、断点续传、多源重试，直接复用既有逻辑）
  - 下载后自动把所在目录加入 PATH，使既有 find_ffmpeg_binary 等探测逻辑可用

设计原则：组件本身的下载实现「登记」进来，本管理器只做统一调度与状态维护，
不重写任何既有业务逻辑。
"""
import os
import sys

from .logger import get_logger
from .utils import get_components_dir, get_data_dir

_log = get_logger("component")


class ComponentManager:
    def __init__(self, ctx=None):
        self.ctx = ctx
        self._components = {}
        self._register_builtins()

    # ------------------------------------------------------------------
    def register(self, meta):
        """登记一个组件。

        meta 字段：
          id, name, desc, version, kind(默认 binary),
          size_estimate_mb, inno_component(默认 False),
          path_resolver() -> 路径或 None,
          downloader(out_dir, log, progress) -> bool   # 可选
        """
        self._components[meta["id"]] = meta

    def _register_builtins(self):
        self.register({
            "id": "ffmpeg",
            "name": "FFmpeg",
            "desc": "音视频合并所必需。未安装时，B站「视频+音频」模式与抖音合并将不可用。",
            "version": "latest",
            "kind": "binary",
            "size_estimate_mb": 90,
            "inno_component": True,
            "path_resolver": self._ffmpeg_path,
            "downloader": self._ffmpeg_download,
        })

    # ---------------------- ffmpeg 探测 / 下载 ----------------------
    def _ffmpeg_path(self):
        import shutil
        candidates = []
        candidates.append(os.path.join(get_components_dir(), "ffmpeg", "ffmpeg.exe"))
        if getattr(sys, "frozen", False):
            candidates.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "ffmpeg.exe"))
        candidates.append(os.path.join(os.getcwd(), "ffmpeg.exe"))
        w = shutil.which("ffmpeg")
        if w:
            candidates.append(w)
        for p in candidates:
            if p and os.path.isfile(p):
                return p
        return None

    @staticmethod
    def _ensure_core_on_path():
        core = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "Nonoka_video_download", "core"))
        if core not in sys.path:
            sys.path.insert(0, core)

    def _ffmpeg_download(self, out_dir, log, progress):
        # 复用既有 bilibili_downloader.download_ffmpeg_windows（不重写其逻辑）
        self._ensure_core_on_path()
        try:
            from bilibili_downloader import download_ffmpeg_windows
        except Exception:
            return False
        dest = os.path.join(out_dir, "ffmpeg")
        os.makedirs(dest, exist_ok=True)
        return download_ffmpeg_windows(dest, log=log, progress=progress)

    # ---------------------- 对外接口 ----------------------
    def get(self, cid):
        return self._components.get(cid)

    def list_all(self):
        return [self.get_status(cid) for cid in self._components]

    def get_status(self, cid):
        meta = self._components.get(cid)
        if not meta:
            return None
        path = meta.get("path_resolver")() if meta.get("path_resolver") else None
        return {
            "id": cid,
            "name": meta.get("name"),
            "desc": meta.get("desc"),
            "version": meta.get("version"),
            "kind": meta.get("kind", "binary"),
            "installed": bool(path),
            "path": path,
            "size_estimate_mb": meta.get("size_estimate_mb"),
            "has_downloader": bool(meta.get("downloader")),
            "inno_component": meta.get("inno_component", False),
        }

    def get_path(self, cid):
        meta = self._components.get(cid)
        if not meta:
            return None
        return meta.get("path_resolver")() if meta.get("path_resolver") else None

    # ---------------------- 规范命名别名（与需求一致） ----------------------
    def register_component(self, meta):
        """登记组件（规范命名，等价于 register）。"""
        self.register(meta)

    def detect_component(self, cid):
        """探测组件是否已安装，返回路径或 None（等价于 get_path）。"""
        return self.get_path(cid)

    def get_component_path(self, cid):
        """返回组件可执行/资源路径（等价于 get_path）。"""
        return self.get_path(cid)

    def download_component(self, cid, log_cb=None, progress_cb=None):
        """下载组件（等价于 download）。"""
        return self.download(cid, log_cb=log_cb, progress_cb=progress_cb)

    def get_component_status(self, cid):
        """返回组件状态（等价于 get_status）。"""
        return self.get_status(cid)

    def ensure_on_path(self, cid):
        """把已安装组件所在目录加入 PATH（供 find_ffmpeg_binary 等探测）。"""
        path = self.get_path(cid)
        if not path:
            return False
        d = os.path.dirname(path)
        if d and d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        return True

    def download(self, cid, log_cb=None, progress_cb=None):
        """下载组件。返回 {"ok":bool, "path":str|None, "error":str}。"""
        meta = self._components.get(cid)
        if not meta or not meta.get("downloader"):
            return {"ok": False, "error": "该组件未提供下载器"}
        out_dir = os.path.join(get_components_dir(), cid)
        os.makedirs(out_dir, exist_ok=True)

        def _log(m):
            _log.info("[%s] %s", cid, m)
            if log_cb:
                try:
                    log_cb(m)
                except Exception:
                    pass

        def _prog(label, fetched, total):
            if progress_cb:
                try:
                    progress_cb(label, fetched, total)
                except Exception:
                    pass

        try:
            ok = meta["downloader"](out_dir, _log, _prog)
        except Exception as e:
            _log.error("组件 %s 下载异常: %s", cid, e)
            return {"ok": False, "error": str(e)}

        if ok:
            self.ensure_on_path(cid)
            return {"ok": True, "path": self.get_path(cid)}
        return {"ok": False, "error": "下载失败（请检查网络 / VPN 后重试）"}
