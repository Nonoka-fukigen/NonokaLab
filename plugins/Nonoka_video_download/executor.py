# -*- coding: utf-8 -*-
"""Nonoka_video_download 插件业务执行器（Executor）。

职责：
  - 承载全部业务逻辑：B站 / 抖音 的视频、音频、封面下载与解析；
  - 承载所有底层操作：subprocess / 网络请求(requests) / 文件读写 / ffmpeg 合并与转码；
  - 通过 emit 回调把进度 / 日志 / 完成事件推回 UI（事件名形如 "<plugin_id>.progress"）。

约束：
  - 长 ffmpeg 命令一律写在 scripts/*.bat 中，本模块只通过
    subprocess.run(["cmd", "/c", "<scripts>/merge.bat", ...]) 调用，严禁拼接长命令字符串。
  - plugin.py 只做接口转发，不包含任何 subprocess / requests / os 调用本处逻辑。
"""
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlencode

import requests

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REFERER = "https://www.bilibili.com"

# WBI 签名用的字符重排表（B 站官方固定顺序）
WBI_MIXIN_KEY_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# 清晰度代号（数值越大越清晰），作为请求提示
QN_BY_NAME = {
    "360p": 16,
    "480p": 32,
    "720p": 64,
    "1080p": 80,
    "1080p+": 116,
    "4k": 120,
}

# 可直接 -c copy 封装的容器集合（其余需要重新编码）
AUDIO_COPY_SAFE = {".m4a", ".aac", ".mp4"}
VIDEO_COPY_SAFE = {".mp4", ".mkv", ".mov", ".flv"}

DOUYIN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
DOUYIN_HEADERS = {
    "User-Agent": DOUYIN_UA,
    "Referer": "https://www.douyin.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# scripts 目录（长 shell 命令统一放这里）
_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")


def _script(name):
    return os.path.join(_SCRIPTS_DIR, name)


# ---------------------------------------------------------------------------
# 纯工具函数（无 I/O，保留为模块级便于复用）
# ---------------------------------------------------------------------------
def sanitize_filename(name, fallback="bilibili"):
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", name)
    return name.strip().strip(".")[:120] or fallback


def parse_bvid(raw):
    if raw.startswith("BV") and re.fullmatch(r"BV[0-9A-Za-z]+", raw):
        return raw
    m = re.search(r"(BV[0-9A-Za-z]+)", raw)
    if not m:
        raise ValueError("无法从输入中解析出 BV 号，请检查链接或 BV 号是否正确。")
    return m.group(1)


def detect_platform(raw):
    """自动识别输入所属平台：bilibili / douyin / None（无法识别）。

    覆盖：
      - B站：bilibili.com 链接、b23.tv 短链、BV 号
      - 抖音：douyin.com / v.douyin.com 链接、分享口令（含「抖音」/「复制打开抖音」）
    """
    s = (raw or "").strip()
    if not s:
        return None
    low = s.lower()
    if ("bilibili.com" in low or "b23.tv" in low
            or s.startswith("BV")
            or re.search(r"(?<![A-Za-z0-9])BV[0-9A-Za-z]{8,}", s)):
        return "bilibili"
    if ("douyin.com" in low or "iesdouyin" in low or "抖音" in s
            or "复制打开抖音" in s or "打开抖音" in s):
        return "douyin"
    return None


def get_mixin_key(img_key, sub_key):
    raw = img_key + sub_key
    return "".join(raw[i] for i in WBI_MIXIN_KEY_TABLE)[:32]


def get_program_dir():
    """返回软件持久化目录。PyInstaller 单文件 exe 运行时，__file__ 指向临时目录，
    因此以 exe 所在目录为准，避免重启后文件丢失。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Executor：全部业务逻辑
# ---------------------------------------------------------------------------
class Executor:
    def __init__(self, emit_fn, plugin_id):
        """emit_fn(event_name, data)：把事件推回 UI，event_name 形如 "<plugin_id>.progress"。"""
        self._emit = emit_fn
        self._plugin_id = plugin_id
        self._qr_sessions = {}
        self._qr_lock = threading.Lock()

    # ----------------------- 事件推送 -----------------------
    def _emit_type(self, typ, data=None):
        self._emit("{}.{}".format(self._plugin_id, typ), data or {})

    def _build_hooks(self, tid, ctx_map, task=None, on_complete=None):
        """构造下载/封面所需的 hooks（log/progress/done/title/bytes）。"""
        ctx_map = ctx_map or {}

        def log(m):
            self._emit_type("log", {"call_id": tid, "message": str(m)})

        def progress(label, fetched, total):
            pct = (fetched * 100 // total) if total else 0
            if task is not None:
                try:
                    task.set_progress(pct, label)
                except Exception:
                    pass
            self._emit_type("progress", {"call_id": tid, "label": label,
                                         "fetched": fetched, "total": total,
                                         "percent": pct})

        def done(ok, msg):
            self._emit_type("done", {"call_id": tid, "ok": bool(ok),
                                     "message": str(msg)})
            if on_complete is not None:
                try:
                    on_complete(bool(ok), ctx_map)
                except Exception:
                    pass

        def title(t):
            ctx_map["title"] = str(t)
            self._emit_type("title", {"call_id": tid, "title": str(t)})

        def bytes_n(n):
            self._emit_type("bytes", {"call_id": tid, "total": n})

        return {"log": log, "progress": progress, "done": done,
                "title": title, "bytes": bytes_n}

    # ----------------------- 状态 -----------------------
    def get_status(self):
        return {"ffmpeg": {"installed": self.have_ffmpeg()}}

    # ----------------------- ffmpeg -----------------------
    def have_ffmpeg(self):
        """是否检测到可用 ffmpeg。统一走 find_ffmpeg_binary（覆盖 PATH/候选/用户指定）。"""
        return bool(self.find_ffmpeg_binary())

    @staticmethod
    def _ffmpeg_candidates():
        """ffmpeg.exe 候选路径（覆盖常见安装位置：PATH/程序目录/组件目录/winget/scoop/choco/Program Files/用户指定）。"""
        from shutil import which
        candidates = []
        # 1. 用户在 config 中手动指定的路径（最高优先级）
        try:
            from nonoka_shell.utils import get_data_dir
            import json as _json
            cfg_path = os.path.join(get_data_dir(), "config.json")
            if os.path.isfile(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = _json.load(f) or {}
                comp = (cfg.get("components") or {}).get("ffmpeg") or {}
                if comp.get("path"):
                    candidates.append(comp["path"])
        except Exception:
            pass
        # 2. 组件目录（component_manager 安装位置）
        try:
            from nonoka_shell.utils import get_components_dir
            candidates.append(os.path.join(get_components_dir(), "ffmpeg", "ffmpeg.exe"))
            candidates.append(os.path.join(get_components_dir(), "ffmpeg", "bin", "ffmpeg.exe"))
        except Exception:
            pass
        # 3. 程序同级 / 插件目录 / cwd
        candidates.append(os.path.join(get_program_dir(), "ffmpeg.exe"))
        here = os.path.dirname(os.path.abspath(__file__))
        if here != get_program_dir():
            candidates.append(os.path.join(here, "ffmpeg.exe"))
        if sys.platform == "win32":
            candidates.append(os.path.join(os.getcwd(), "ffmpeg.exe"))
        # 4. 系统 PATH
        w = which("ffmpeg")
        if w:
            candidates.append(w)
        # 5. 常见安装位置（winget / scoop / choco / Program Files / 用户目录）
        if sys.platform == "win32":
            env = os.environ
            home = env.get("USERPROFILE", "")
            local = env.get("LOCALAPPDATA", "")
            pf = env.get("ProgramFiles", r"C:\Program Files")
            pf86 = env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
            extra = [
                r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\ffmpeg\ffmpeg.exe",
                os.path.join(pf, "ffmpeg", "bin", "ffmpeg.exe"),
                os.path.join(pf, "ffmpeg", "ffmpeg.exe"),
                os.path.join(pf86, "ffmpeg", "bin", "ffmpeg.exe"),
                os.path.join(home, "scoop", "apps", "ffmpeg", "current", "bin", "ffmpeg.exe"),
                os.path.join(home, "scoop", "shims", "ffmpeg.exe"),
                os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg_*", "ffmpeg-*-full_build", "bin", "ffmpeg.exe"),
                os.path.join(home, "AppData", "Local", "Programs", "ffmpeg", "bin", "ffmpeg.exe"),
            ]
            candidates.extend(extra)
        # 去重 + 展开通配符
        seen, final = set(), []
        for p in candidates:
            if not p or p in seen:
                continue
            seen.add(p)
            final.append(p)
        return final

    def find_ffmpeg_binary(self):
        """返回 ffmpeg 可执行文件路径（覆盖 PATH + 常见安装位置 + 用户指定）。"""
        import glob
        for p in self._ffmpeg_candidates():
            matches = glob.glob(p)
            if matches:
                for real in matches:
                    if os.path.isfile(real):
                        try:
                            subprocess.run([real, "-version"], stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, check=True, timeout=8)
                            return real
                        except Exception:
                            pass
            elif os.path.isfile(p):
                try:
                    subprocess.run([p, "-version"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, check=True, timeout=8)
                    return p
                except Exception:
                    pass
        return None

    def _ffmpeg_cmd(self):
        binary = self.find_ffmpeg_binary()
        return [binary or "ffmpeg"]

    def _run_script(self, name, args):
        """仅通过 cmd /c 调用 scripts/ 下的批处理脚本执行长命令，禁止拼长字符串。

        脚本第一个参数固定为 ffmpeg 完整路径（不依赖 PATH），其余参数即业务入参。
        失败时捕获 ffmpeg 真实 stderr，拼进异常信息，便于定位具体原因。
        """
        binary = self.find_ffmpeg_binary() or "ffmpeg"
        cmd = ["cmd", "/c", _script(name), binary] + [str(a) for a in args]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            tail = []
            if proc.stdout:
                tail.append("stdout: " + proc.stdout.strip()[-800:])
            if proc.stderr:
                tail.append("stderr: " + proc.stderr.strip()[-1200:])
            raise RuntimeError(f"脚本 {name} 执行失败 (exit {proc.returncode}): "
                               + " | ".join(tail) if tail else f"脚本 {name} 执行失败 (exit {proc.returncode})")

    def merge_with_ffmpeg(self, video_path, audio_path, out_path):
        self._run_script("merge.bat", [video_path, audio_path, out_path])

    def remux_ffmpeg(self, src_path, out_path):
        self._run_script("remux.bat", [src_path, out_path])

    def reencode_ffmpeg(self, src_path, out_path, kind):
        self._run_script("reencode.bat", [kind, src_path, out_path])

    # ----------------------- B站：网络 / 解析 -----------------------
    def build_session(self, cookie=None):
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Referer": REFERER})
        if cookie:
            s.headers.update({"Cookie": cookie})
        return s

    def ensure_buvid(self, session):
        """确保 session 持有 buvid3 cookie；B站 QR 登录等接口需要它。"""
        try:
            if not session.cookies.get("buvid3"):
                session.get("https://www.bilibili.com/", timeout=10)
        except Exception:
            pass

    def get_wbi_keys(self, session):
        """未登录时 nav 返回 code=-101，但 data.wbi_img 仍然有效，故不要求 code==0。"""
        resp = session.get("https://api.bilibili.com/x/web-interface/nav",
                           timeout=15).json()
        wbi = (resp.get("data") or {}).get("wbi_img")
        if not wbi:
            raise RuntimeError("获取 WBI 密钥失败: {}".format(resp.get("message")))
        img_key = wbi["img_url"].rsplit("/", 1)[1].split(".")[0]
        sub_key = wbi["sub_url"].rsplit("/", 1)[1].split(".")[0]
        return img_key, sub_key

    def sign_params(self, params, img_key, sub_key):
        mixin_key = get_mixin_key(img_key, sub_key)
        params = dict(params)
        params["wts"] = int(time.time())
        params = dict(sorted(params.items()))
        query = urlencode(params)
        params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
        return params

    def get_view(self, session, bvid):
        # 先确保持有 buvid 相关 cookie：部分 IP/时段下 B 站 view 接口对无 cookie 的
        # 请求会返回 -400（请求错误），补齐 buvid 后重试一次可规避。
        self.ensure_buvid(session)
        last = None
        for attempt in range(2):
            try:
                resp = session.get(
                    "https://api.bilibili.com/x/web-interface/view",
                    params={"bvid": bvid}, timeout=15).json()
                if resp.get("code") != 0:
                    last = RuntimeError(
                        "获取视频信息失败: {} (code={})".format(resp.get("message"), resp.get("code")))
                    if resp.get("code") == -400 and attempt == 0:
                        self.ensure_buvid(session)
                        continue
                    raise last
                return resp["data"]
            except (RuntimeError, ValueError) as e:
                raise e
            except Exception as e:
                last = e
        raise last

    def pick_cid(self, view, page):
        if "pages" in view and len(view["pages"]) > 1:
            if page < 0 or page >= len(view["pages"]):
                raise ValueError("分 P 序号越界，该视频共有 {} 个分 P。".format(len(view["pages"])))
            return view["pages"][page]["cid"]
        return view["cid"]

    def get_playurl(self, session, bvid, cid, qn):
        img_key, sub_key = self.get_wbi_keys(session)
        params = {
            "bvid": bvid,
            "cid": cid,
            "qn": qn,
            "fnval": 16,   # 16 = 返回 DASH 格式
            "fourk": 1,
            "platform": "pc",
        }
        params = self.sign_params(params, img_key, sub_key)
        resp = session.get(
            "https://api.bilibili.com/x/player/playurl",
            params=params, timeout=15).json()
        if resp.get("code") != 0:
            raise RuntimeError("获取播放地址失败: {} (code={})".format(resp.get("message"), resp.get("code")))
        return resp["data"]

    def choose_best(self, streams, key="id"):
        return max(streams, key=lambda s: s.get(key, 0))

    # ----------------------- B站：下载 -----------------------
    def stream_download(self, url, path, session, on_progress=None):
        """流式下载单条流，on_progress(fetched, total) 回调进度。"""
        headers = {"User-Agent": UA, "Referer": REFERER}
        with session.get(url, headers=headers, stream=True, timeout=30) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0))
            fetched = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    f.write(chunk)
                    fetched += len(chunk)
                    if on_progress:
                        on_progress(fetched, total)

    def resolve_ext(self, mode, user_ext):
        """根据用户选择解析最终文件后缀。"""
        if user_ext:
            e = user_ext.strip().split()[0]   # 去掉 " (默认)" 之类后缀说明
            if not e.startswith("."):
                e = "." + e
            return e.lower()
        return {"audio": ".m4a", "video": ".mp4", "both": ".mp4"}.get(mode, ".mp4")

    def save_single_stream(self, url, final_path, session, label, on_progress, kind, ext):
        """保存单条流（视频或音频）。有 ffmpeg 就封装/重编码，否则直接重命名。"""
        tmp = final_path + ".tmp.m4s"
        self.stream_download(url, tmp, session,
                             on_progress=lambda f, t: on_progress(label, f, t))
        copy_safe = (ext in AUDIO_COPY_SAFE) if kind == "audio" else (ext in VIDEO_COPY_SAFE)
        if self.have_ffmpeg():
            try:
                if copy_safe:
                    self.remux_ffmpeg(tmp, final_path)
                else:
                    self.reencode_ffmpeg(tmp, final_path, kind)
                os.remove(tmp)
                return
            except subprocess.CalledProcessError:
                pass
        # 无 ffmpeg 或处理失败 -> 直接当作成品（B 站流多为 M4A/MP4 容器）
        if os.path.exists(final_path):
            os.remove(final_path)
        os.rename(tmp, final_path)

    # ----------------------- B站：扫码登录 -----------------------
    def qr_generate(self):
        """生成登录二维码，返回 {"key", "image"(data-url)} 或 {"error"}。"""
        try:
            import qrcode
            from PIL import Image
        except ImportError:
            return {"error": "缺少 qrcode/pillow 库，请运行: pip install qrcode pillow"}
        session = self.build_session()
        self.ensure_buvid(session)
        try:
            headers = {"User-Agent": UA, "Referer": "https://passport.bilibili.com/"}
            r = session.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
                headers=headers, timeout=15)
            data = r.json().get("data", {})
            if not data.get("qrcode_key"):
                raise RuntimeError("生成二维码失败，请重试。")
        except Exception as e:
            return {"error": str(e)}
        key = data["qrcode_key"]
        qr_url = data["url"]
        with self._qr_lock:
            self._qr_sessions[key] = session
        try:
            img = qrcode.make(qr_url).resize((240, 240)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "PNG")
            data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
            return {"key": key, "image": data_url}
        except Exception as e:
            return {"error": str(e)}

    def qr_poll(self, key):
        """轮询扫码状态。返回 {"code", ...}；code=0 时带 cookie。"""
        try:
            with self._qr_lock:
                session = self._qr_sessions.get(key)
            if not session:
                return {"code": -1, "error": "二维码会话不存在，请刷新"}
            headers = {"User-Agent": UA, "Referer": "https://passport.bilibili.com/"}
            r = session.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": key},
                headers=headers, timeout=15)
            code = r.json().get("data", {}).get("code", -1)
            if code == 0:
                cookies = r.cookies.get_dict()
                cs = "; ".join("{}={}".format(k, v) for k, v in cookies.items())
                with self._qr_lock:
                    self._qr_sessions.pop(key, None)
                return {"code": code, "cookie": cs}
            return {"code": code}
        except Exception as e:
            return {"code": -1, "error": str(e)}

    # ----------------------- 解析（入口） -----------------------
    def parse(self, payload):
        tid = uuid.uuid4().hex
        url = (payload.get("url") or "").strip()
        platform = (payload.get("platform") or "").strip()
        self._emit_type("parse_start", {"call_id": tid})
        if not url:
            self._emit_type("parse_error", {"call_id": tid, "message": "请填写视频链接。"})
            return
        # 平台：用户选择 > 自动识别 > 默认 bilibili（不再因识别失败阻断）
        platform = platform or detect_platform(url) or "bilibili"
        try:
            if platform == "douyin":
                info = self.fetch_video_info(url)
                if info.get("err"):
                    self._emit_type("parse_error", {"call_id": tid, "message": info["err"]})
                    return
                self._emit_type("parsed", {"call_id": tid,
                                           "title": info.get("title"),
                                           "author": info.get("author"),
                                           "cover": info.get("cover")})
            else:
                bvid = parse_bvid(url)
                session = self.build_session(payload.get("cookie") or None)
                view = self.get_view(session, bvid)
                title = view.get("title", bvid)
                cover = view.get("pic")
                self._emit_type("parsed", {"call_id": tid,
                                           "title": title, "author": "",
                                           "cover": (cover.split("@")[0] if cover else None),
                                           "bvid": bvid})
        except Exception as e:
            self._emit_type("parse_error", {"call_id": tid, "message": str(e)})

    # ----------------------- 下载（入口） -----------------------
    def download(self, payload, task=None, on_complete=None):
        from nonoka_shell.utils import get_data_dir
        if task is not None and task.is_cancelled():
            return
        platform = (payload.get("platform") or "").strip()
        url = (payload.get("url") or "").strip()
        mode = (payload.get("mode") or "both")
        folder = (payload.get("folder") or "").strip() or os.path.join(get_data_dir(), "downloads")
        os.makedirs(folder, exist_ok=True)
        filename = payload.get("filename") or None
        ext = payload.get("suffix") or None
        tid = (task.id if task else uuid.uuid4().hex)
        hooks = self._build_hooks(tid, {"url": url, "type": mode, "folder": folder,
                                        "title": None}, task=task, on_complete=on_complete)
        if not url:
            hooks["done"](False, "请先粘贴视频链接。")
            return
        # 平台：用户选择 > 自动识别 > 默认 bilibili
        platform = platform or detect_platform(url) or "bilibili"
        try:
            if platform == "douyin":
                self._do_douyin_download(url, mode, folder, filename=filename,
                                         ext=ext, hooks=hooks)
            else:
                qn = QN_BY_NAME.get(payload.get("quality", "1080p"), 80)
                self._do_bilibili_download(url, mode, folder, qn,
                                           cookie=(payload.get("cookie") or None),
                                           filename=filename, ext=ext, hooks=hooks)
        except Exception as e:
            self._emit_type("done", {"call_id": tid, "ok": False, "message": str(e)})

    # ----------------------- 封面（入口） -----------------------
    def cover(self, payload, task=None, on_complete=None):
        from nonoka_shell.utils import get_data_dir
        if task is not None and task.is_cancelled():
            return
        platform = (payload.get("platform") or "").strip()
        url = (payload.get("url") or "").strip()
        folder = (payload.get("folder") or "").strip() or os.path.join(get_data_dir(), "downloads")
        os.makedirs(folder, exist_ok=True)
        filename = payload.get("filename") or None
        ext = payload.get("format") or ".jpg"
        scale = int(payload.get("scale", 2) or 2)
        tid = (task.id if task else uuid.uuid4().hex)
        hooks = self._build_hooks(tid, {"url": url, "type": "cover", "folder": folder,
                                        "title": None}, task=task, on_complete=on_complete)
        if not url:
            hooks["done"](False, "请先粘贴视频链接。")
            return
        platform = platform or detect_platform(url) or "bilibili"
        try:
            if platform == "douyin":
                self._do_douyin_cover(url, folder, filename=filename, ext=ext,
                                      scale=scale, hooks=hooks)
            else:
                self._do_bilibili_cover(url, folder, filename=filename, ext=ext,
                                        scale=scale,
                                        cookie=(payload.get("cookie") or None),
                                        hooks=hooks)
        except Exception as e:
            self._emit_type("done", {"call_id": tid, "ok": False, "message": str(e)})

    # ----------------------- B站：核心流程 -----------------------
    def _do_bilibili_download(self, url, mode, out_dir, qn, cookie=None, page=0,
                              filename=None, ext=None, hooks=None):
        hooks = hooks or {}
        log = hooks.get("log", lambda *a, **k: None)
        progress = hooks.get("progress", lambda *a, **k: None)
        done = hooks.get("done", lambda *a, **k: None)
        on_title = hooks.get("title", lambda *a, **k: None)
        try:
            bvid = parse_bvid(url)
            log("解析 BV 号: {}".format(bvid))
            session = self.build_session(cookie)
            view = self.get_view(session, bvid)
            title = view.get("title", bvid)
            on_title(title)
            cid = self.pick_cid(view, page)
            log("视频标题: {}".format(title))

            data = self.get_playurl(session, bvid, cid, qn)
            dash = data.get("dash")
            if not dash:
                raise RuntimeError("该视频未返回 DASH 流（可能是受限内容），暂不支持下载。")

            if filename and filename.strip():
                base_name = sanitize_filename(filename.strip())
            else:
                base_name = "{}__{}".format(sanitize_filename(title), bvid)
            ext = self.resolve_ext(mode, ext)
            audio_streams = dash.get("audio") or []
            video_streams = dash.get("video") or []
            if not audio_streams and mode != "video":
                raise RuntimeError("未找到可用音频流。")
            if not video_streams and mode != "audio":
                raise RuntimeError("未找到可用视频流。")

            audio_best = self.choose_best(audio_streams, key="bandwidth") if audio_streams else None
            video_best = self.choose_best(video_streams, key="id") if video_streams else None
            audio_url = (audio_best.get("baseUrl") or audio_best.get("backupUrl", [None])[0]) if audio_best else None
            video_url = (video_best.get("baseUrl") or video_best.get("backupUrl", [None])[0]) if video_best else None

            if mode == "audio":
                log("选择音频: {} / {}kbps".format(audio_best.get('id'), audio_best.get('bandwidth') // 1000))
                log("输出: {}{}（{}）".format(base_name, ext, ext))
                out_path = os.path.join(out_dir, base_name + ext)
                self.save_single_stream(audio_url, out_path, session, "下载音频", progress, "audio", ext)
                if not self.have_ffmpeg():
                    log("提示: 未检测到 ffmpeg，已直接保存为 {}（如播放异常请安装 ffmpeg）。".format(ext))

            elif mode == "video":
                log("选择视频: {} / {}x{}".format(video_best.get('id'),
                    video_best.get('width'), video_best.get('height')))
                log("输出: {}{}（{}）".format(base_name, ext, ext))
                out_path = os.path.join(out_dir, base_name + ext)
                self.save_single_stream(video_url, out_path, session, "下载视频", progress, "video", ext)
                if not self.have_ffmpeg():
                    log("提示: 未检测到 ffmpeg，已直接保存为 {}（无声，如需音轨请选“视频+音频”）。".format(ext))

            else:  # both
                if not self.have_ffmpeg():
                    raise RuntimeError("合并模式需要 ffmpeg，请先安装并加入 PATH（https://ffmpeg.org）。")
                log("选择视频: {} / {}x{}".format(video_best.get('id'),
                    video_best.get('width'), video_best.get('height')))
                log("选择音频: {} / {}kbps".format(audio_best.get('id'), audio_best.get('bandwidth') // 1000))
                v_tmp = os.path.join(out_dir, base_name + ".video.m4s")
                a_tmp = os.path.join(out_dir, base_name + ".audio.m4s")
                out_path = os.path.join(out_dir, base_name + ext)
                try:
                    self.stream_download(video_url, v_tmp, session,
                                         on_progress=lambda f, t: progress("下载视频", f, t))
                    self.stream_download(audio_url, a_tmp, session,
                                         on_progress=lambda f, t: progress("下载音频", f, t))
                    log("正在合并音视频（ffmpeg）…")
                    progress("合并中", 1, 1)
                    self.merge_with_ffmpeg(v_tmp, a_tmp, out_path)
                finally:
                    for f in (v_tmp, a_tmp):
                        if os.path.exists(f):
                            os.remove(f)

            log("✅ 完成 -> {}".format(out_path))
            done(True, out_path)

        except Exception as e:
            log("❌ 错误: {}".format(e))
            done(False, str(e))

    # ----------------------- B站：封面 -----------------------
    def get_cover_url(self, session, bvid):
        view = self.get_view(session, bvid)
        pic = view.get("pic")
        if not pic:
            raise RuntimeError("该视频未返回封面地址。")
        pic = pic.split("@")[0]
        return pic, view.get("title", bvid)

    def _enhance_with_opencv(self, img_bytes, scale, log=lambda *a, **k: None):
        import cv2
        import numpy as np
        arr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("OpenCV 无法解码图片")
        h, w = img.shape[:2]
        if scale > 1:
            new_w, new_h = int(w * scale), int(h * scale)
            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
            try:
                img = cv2.detailEnhance(img, sigma_s=10, sigma_r=0.15)
            except Exception:
                pass
            kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
            img = cv2.filter2D(img, -1, kernel)
        _, buf = cv2.imencode(".png", img)
        return buf.tobytes()

    def _enhance_with_pil(self, img_bytes, scale, log=lambda *a, **k: None):
        from PIL import Image, ImageFilter, ImageEnhance
        img = Image.open(io.BytesIO(img_bytes))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        if scale > 1:
            w, h = img.size
            big = img.resize((w * scale * 2, h * scale * 2), Image.LANCZOS)
            target = big.resize((w * scale, h * scale), Image.LANCZOS)
            target = target.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
            target = ImageEnhance.Contrast(target).enhance(1.1)
            img = target
        return img

    def _save_image_rgb(self, img_bytes, out_path):
        from PIL import Image
        img = Image.open(io.BytesIO(img_bytes))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.save(out_path, quality=95, optimize=True)

    def _do_bilibili_cover(self, url, out_dir, filename=None, ext=".jpg", scale=2,
                           cookie=None, hooks=None):
        hooks = hooks or {}
        log = hooks.get("log", lambda *a, **k: None)
        progress = hooks.get("progress", lambda *a, **k: None)
        done = hooks.get("done", lambda *a, **k: None)
        on_title = hooks.get("title", lambda *a, **k: None)
        on_bytes = hooks.get("bytes", lambda *a, **k: None)
        try:
            bvid = parse_bvid(url)
            log("解析 BV 号: {}".format(bvid))
            session = self.build_session(cookie)
            pic_url, title = self.get_cover_url(session, bvid)
            on_title(title)
            log("封面地址: {}".format(pic_url))

            if filename and filename.strip():
                base_name = sanitize_filename(filename.strip())
            else:
                base_name = "{}__{}_cover".format(sanitize_filename(title), bvid)
            out_path = os.path.join(out_dir, base_name + ext)

            log("正在下载封面…")
            progress("下载封面", 0, 1)
            headers = {"User-Agent": UA, "Referer": REFERER}
            with session.get(pic_url, headers=headers, stream=True, timeout=30) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0))
                buf = io.BytesIO()
                fetched = 0
                for chunk in r.iter_content(chunk_size=1024 * 128):
                    if not chunk:
                        continue
                    buf.write(chunk)
                    fetched += len(chunk)
                    on_bytes(fetched)
                    progress("下载封面", fetched, total or fetched)
            img_bytes = buf.getvalue()
            log("封面原图大小: {:.1f} KB".format(len(img_bytes) / 1024))

            if scale > 1:
                log("正在使用 {}x 清晰度增强…".format(scale))
                progress("增强封面", 1, 1)
                try:
                    enhanced_bytes = self._enhance_with_opencv(img_bytes, scale, log)
                    self._save_image_rgb(enhanced_bytes, out_path)
                    log("使用 OpenCV 完成增强")
                except Exception as e:
                    log("OpenCV 不可用或失败 ({})，回退到 PIL…".format(e))
                    img = self._enhance_with_pil(img_bytes, scale, log)
                    img.save(out_path, quality=95, optimize=True)
                    log("使用 PIL 完成增强")
            else:
                self._save_image_rgb(img_bytes, out_path)

            log("✅ 完成 -> {}".format(out_path))
            done(True, out_path)

        except Exception as e:
            log("❌ 错误: {}".format(e))
            done(False, str(e))

    # ----------------------- 抖音：解析 -----------------------
    def _build_douyin_session(self):
        s = requests.Session()
        s.headers.update(DOUYIN_HEADERS)
        return s

    def _extract_url_from_text(self, text):
        m = re.search(r"(https?://(?:v\.douyin\.com|www\.douyin\.com|webcast\.amemv\.com)/[^\s\]\)]+)", text)
        if m:
            return m.group(1)
        m = re.search(r"(https?://[^\s\]\)]+)", text)
        return m.group(1) if m else text.strip()

    def _resolve_short_url(self, url):
        try:
            r = requests.head(url, headers=DOUYIN_HEADERS, allow_redirects=True, timeout=20)
            return r.url
        except Exception:
            return url

    def _get_video_id(self, url):
        m = re.search(r"/video/(\d+)", url)
        if m:
            return m.group(1), "video"
        m = re.search(r"[?&]modal_id=(\d+)", url)
        if m:
            return m.group(1), "video"
        m = re.search(r"[?&]item_id=(\d+)", url)
        if m:
            return m.group(1), "video"
        return None, None

    def _extract_render_data(self, html):
        m = re.search(r'<script id="RENDER_DATA" type="application/json">([^<]+)</script>', html)
        if m:
            raw = m.group(1)
            if raw.startswith("%"):
                raw = urllib_unquote(raw)
            return json.loads(raw)
        m = re.search(r'<script[^>]*>window\._SSR_HYDRATED_DATA\s*=\s*([^<]+)</script>', html)
        if m:
            return json.loads(m.group(1))
        return None

    def _walk_json(self, obj, key):
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for v in obj.values():
                found = self._walk_json(v, key)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = self._walk_json(item, key)
                if found is not None:
                    return found
        return None

    def _pick_url(self, url_list):
        if not url_list:
            return None
        for u in url_list:
            if isinstance(u, str) and u.startswith("http"):
                return u
        return url_list[0]

    def _fetch_via_browser(self, url, video_id):
        """服务端解析失败时，用本机浏览器（系统 Chrome/Edge）真实渲染页面兜底解析。

        通过独立子进程调用 Playwright；stderr 直接丢弃（避免 64KB 管道缓冲死锁），
        只保留 stdout（一行 JSON），并配套超时杀进程树。
        """
        try:
            r = subprocess.run(
                [sys.executable, "--browser-parse", url],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=150,
                start_new_session=True,
            )
            out = (r.stdout or "").strip()
            if not out:
                return {"err": "浏览器解析子进程无输出（可能 Chrome/网络受限）。"}
            start = out.rfind("{")
            end = out.rfind("}") + 1
            if start < 0 or end <= start:
                return {"err": "浏览器解析输出非 JSON：" + out[-300:]}
            info = json.loads(out[start:end])
            if info.get("err"):
                return {"err": info["err"]}
            info["video_id"] = info.get("video_id") or video_id
            return info
        except subprocess.TimeoutExpired as e:
            try:
                pid = getattr(e, "pid", None)
                if pid:
                    if sys.platform == "win32":
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        import os as _os
                        _os.killpg(_os.getpgid(pid), 9)
            except Exception:
                pass
            return {"err": "浏览器解析超时（>150s），可能是网络或风控限制。"}
        except Exception as e:
            return {"err": "浏览器兜底解析失败: {}".format(e)}

    def fetch_video_info(self, url):
        """获取抖音视频信息。返回 dict：title, cover, video_url, audio_url, author, err。"""
        url = self._extract_url_from_text(url)
        url = self._resolve_short_url(url)
        video_id, _ = self._get_video_id(url)
        if not video_id:
            return {"err": "无法从链接中解析出抖音视频 ID，请检查链接是否完整。"}

        session = self._build_douyin_session()
        detail_url = "https://www.douyin.com/video/{}".format(video_id)
        try:
            r = session.get(detail_url, timeout=20)
            r.raise_for_status()
        except Exception:
            return self._fetch_via_browser(url, video_id)

        render = self._extract_render_data(r.text)
        if not render:
            return self._fetch_via_browser(url, video_id)

        item = None
        for path_key in ("app", "data", "initialState"):
            node = render.get(path_key) if isinstance(render, dict) else None
            if not node:
                continue
            item = self._walk_json(node, "itemInfo") or self._walk_json(node, "item_list") or self._walk_json(node, "item")
            if isinstance(item, list) and item:
                item = item[0]
            if item and isinstance(item, dict):
                break

        if not item or not isinstance(item, dict):
            return self._fetch_via_browser(url, video_id)

        title = item.get("desc") or item.get("share_info", {}).get("share_title") or "douyin_video"
        author = item.get("author", {}).get("nickname") or ""

        cover = None
        cover_obj = item.get("video", {}).get("cover") or item.get("cover")
        if isinstance(cover_obj, dict):
            cover = self._pick_url(cover_obj.get("url_list") or cover_obj.get("url"))
        elif isinstance(cover_obj, str):
            cover = cover_obj

        video_url = None
        video_obj = item.get("video", {})
        play_addr = video_obj.get("play_addr") or video_obj.get("playAddr")
        if isinstance(play_addr, dict):
            video_url = self._pick_url(play_addr.get("url_list") or play_addr.get("url"))
        if not video_url:
            download_addr = video_obj.get("download_addr") or video_obj.get("downloadAddr")
            if isinstance(download_addr, dict):
                video_url = self._pick_url(download_addr.get("url_list") or download_addr.get("url"))

        audio_url = None
        music_obj = item.get("music", {})
        if isinstance(music_obj, dict):
            play_url = music_obj.get("play_url") or music_obj.get("playUrl")
            if isinstance(play_url, dict):
                audio_url = self._pick_url(play_url.get("url_list") or play_url.get("url"))
            elif isinstance(play_url, str):
                audio_url = play_url

        return {
            "title": title,
            "author": author,
            "cover": cover,
            "video_url": video_url,
            "audio_url": audio_url,
            "video_id": video_id,
            "err": None,
        }

    # ----------------------- 抖音：下载 -----------------------
    def _douyin_stream_download(self, url, path, session, label, progress, on_bytes=None):
        headers = {"User-Agent": DOUYIN_UA, "Referer": "https://www.douyin.com/"}
        with session.get(url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0))
            fetched = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    f.write(chunk)
                    fetched += len(chunk)
                    progress(label, fetched, total)
                    if on_bytes:
                        on_bytes(fetched)

    def _do_douyin_download(self, url, mode, out_dir, filename=None, ext=None, hooks=None):
        hooks = hooks or {}
        log = hooks.get("log", lambda *a, **k: None)
        progress = hooks.get("progress", lambda *a, **k: None)
        done = hooks.get("done", lambda *a, **k: None)
        on_title = hooks.get("title", lambda *a, **k: None)
        on_bytes = hooks.get("bytes", lambda *a, **k: None)
        try:
            log("正在解析抖音链接…")
            info = self.fetch_video_info(url)
            if info.get("err"):
                raise RuntimeError(info["err"])

            title = info["title"]
            on_title(title)
            log("标题: {}".format(title))
            if info.get("author"):
                log("作者: {}".format(info["author"]))

            base_name = sanitize_filename(filename.strip(), fallback="douyin") if filename and filename.strip() else sanitize_filename(title, fallback="douyin")
            base_name = base_name or "douyin_{}".format(info["video_id"])

            session = self._build_douyin_session()

            if mode == "audio":
                if not info.get("audio_url"):
                    raise RuntimeError("未找到该视频的音频地址。")
                ext = ext or ".mp3"
                out_path = os.path.join(out_dir, base_name + ext)
                log("开始下载音频 -> {}".format(out_path))
                self._douyin_stream_download(info["audio_url"], out_path, session, "下载音频", progress, on_bytes)
                log("✅ 完成 -> {}".format(out_path))
                done(True, out_path)

            elif mode == "video":
                if not info.get("video_url"):
                    raise RuntimeError("未找到该视频的视频地址，可能链接已失效或被限制。")
                ext = ext or ".mp4"
                out_path = os.path.join(out_dir, base_name + ext)
                log("开始下载视频 -> {}".format(out_path))
                self._douyin_stream_download(info["video_url"], out_path, session, "下载视频", progress, on_bytes)
                log("✅ 完成 -> {}".format(out_path))
                done(True, out_path)

            else:  # both
                if not info.get("video_url") or not info.get("audio_url"):
                    raise RuntimeError("需要同时有视频和音频地址才能合并，请重试或换一条链接。")
                ext = ext or ".mp4"
                out_path = os.path.join(out_dir, base_name + ext)
                v_tmp = os.path.join(out_dir, base_name + ".video.tmp")
                a_tmp = os.path.join(out_dir, base_name + ".audio.tmp")
                try:
                    log("开始下载视频流…")
                    self._douyin_stream_download(info["video_url"], v_tmp, session, "下载视频", progress, on_bytes)
                    log("开始下载音频流…")
                    self._douyin_stream_download(info["audio_url"], a_tmp, session, "下载音频", progress, on_bytes)
                    try:
                        if self.have_ffmpeg():
                            log("正在合并音视频（ffmpeg）…")
                            progress("合并中", 1, 1)
                            self.merge_with_ffmpeg(v_tmp, a_tmp, out_path)
                        else:
                            log("未检测到 ffmpeg，仅保存视频流（无声）。如需合并请下载 ffmpeg。")
                            if os.path.exists(out_path):
                                os.remove(out_path)
                            os.rename(v_tmp, out_path)
                            a_bak = os.path.join(out_dir, base_name + ".audio.m4a")
                            if os.path.exists(a_bak):
                                os.remove(a_bak)
                            os.rename(a_tmp, a_bak)
                            log("音频已额外保存为: {}".format(a_bak))
                    except Exception as e:
                        log("合并失败: {}，保留视频流".format(e))
                        if os.path.exists(out_path):
                            os.remove(out_path)
                        os.rename(v_tmp, out_path)
                    log("✅ 完成 -> {}".format(out_path))
                    done(True, out_path)
                finally:
                    for f in (v_tmp, a_tmp):
                        if os.path.exists(f):
                            try:
                                os.remove(f)
                            except Exception:
                                pass

        except Exception as e:
            log("❌ 错误: {}".format(e))
            done(False, str(e))

    def _do_douyin_cover(self, url, out_dir, filename=None, ext=".jpg", scale=2, hooks=None):
        hooks = hooks or {}
        log = hooks.get("log", lambda *a, **k: None)
        progress = hooks.get("progress", lambda *a, **k: None)
        done = hooks.get("done", lambda *a, **k: None)
        on_title = hooks.get("title", lambda *a, **k: None)
        on_bytes = hooks.get("bytes", lambda *a, **k: None)
        try:
            log("正在解析抖音链接…")
            info = self.fetch_video_info(url)
            if info.get("err"):
                raise RuntimeError(info["err"])

            title = info["title"]
            on_title(title)
            cover_url = info.get("cover")
            if not cover_url:
                raise RuntimeError("未找到该视频的封面地址。")

            base_name = sanitize_filename(filename.strip(), fallback="douyin") if filename and filename.strip() else sanitize_filename(title, fallback="douyin") + "_cover"
            base_name = base_name or "douyin_{}_cover".format(info["video_id"])
            out_path = os.path.join(out_dir, base_name + ext)

            log("正在下载封面…")
            progress("下载封面", 0, 1)
            session = self._build_douyin_session()
            r = session.get(cover_url, headers={"User-Agent": DOUYIN_UA, "Referer": "https://www.douyin.com/"}, timeout=30)
            r.raise_for_status()
            img_bytes = r.content
            on_bytes(len(img_bytes))
            progress("下载封面", 1, 1)

            if scale > 1:
                log("正在使用 {}x 清晰度增强…".format(scale))
                progress("增强封面", 1, 1)
                try:
                    enhanced = self._enhance_with_opencv(img_bytes, scale, log)
                    self._save_image_rgb(enhanced, out_path)
                    log("使用 OpenCV 完成增强")
                except Exception as e:
                    log("OpenCV 不可用或失败 ({})，回退到 PIL…".format(e))
                    img = self._enhance_with_pil(img_bytes, scale, log)
                    img.save(out_path, quality=95, optimize=True)
                    log("使用 PIL 完成增强")
            else:
                self._save_image_rgb(img_bytes, out_path)

            log("✅ 完成 -> {}".format(out_path))
            done(True, out_path)

        except Exception as e:
            log("❌ 错误: {}".format(e))
            done(False, str(e))


# 供 _extract_render_data 使用（与原始实现一致）
def urllib_unquote(s):
    import urllib.parse
    return urllib.parse.unquote(s)