#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B 站视频下载器 (Bilibili Downloader) —— 图形界面版

运行方式：
  - 直接运行  -> 弹出图形界面（粘贴链接、选文件夹、选模式下载）
  - 命令行    -> python bilibili_downloader.py -u <BV号或链接> -m audio|video

下载模式（图形界面可三选一）：
  - video : 仅下载视频（无声，单独的视频流）
  - audio : 仅下载音频（.m4a，无需 ffmpeg）
  - both  : 视频 + 音频合并为 .mp4（需要 ffmpeg）

依赖：requests（已内置说明）；合并/重封装需要系统安装 ffmpeg（https://ffmpeg.org）
"""

import argparse
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    sys.stderr.write("缺少 requests 库，请先运行: pip install requests\n")
    raise

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

MODE_LABELS = {
    "video": "仅视频",
    "audio": "仅音频",
    "both":  "视频+音频",
}


# ---------------------------------------------------------------------------
# 核心网络 / 解析
# ---------------------------------------------------------------------------
def build_session(cookie=None):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": REFERER})
    if cookie:
        s.headers.update({"Cookie": cookie})
    return s


def ensure_buvid(session):
    """确保 session 持有 buvid3 cookie；B站 QR 登录等接口需要它。"""
    try:
        if not session.cookies.get("buvid3"):
            session.get("https://www.bilibili.com/", timeout=10)
    except Exception:
        pass


def parse_bvid(raw):
    if raw.startswith("BV") and re.fullmatch(r"BV[0-9A-Za-z]+", raw):
        return raw
    m = re.search(r"(BV[0-9A-Za-z]+)", raw)
    if not m:
        raise ValueError("无法从输入中解析出 BV 号，请检查链接或 BV 号是否正确。")
    return m.group(1)


def get_mixin_key(img_key, sub_key):
    raw = img_key + sub_key
    return "".join(raw[i] for i in WBI_MIXIN_KEY_TABLE)[:32]


def get_wbi_keys(session):
    """未登录时 nav 返回 code=-101，但 data.wbi_img 仍然有效，故不要求 code==0。"""
    resp = session.get("https://api.bilibili.com/x/web-interface/nav",
                       timeout=15).json()
    wbi = (resp.get("data") or {}).get("wbi_img")
    if not wbi:
        raise RuntimeError(f"获取 WBI 密钥失败: {resp.get('message')}")
    img_key = wbi["img_url"].rsplit("/", 1)[1].split(".")[0]
    sub_key = wbi["sub_url"].rsplit("/", 1)[1].split(".")[0]
    return img_key, sub_key


def sign_params(params, img_key, sub_key):
    mixin_key = get_mixin_key(img_key, sub_key)
    params = dict(params)
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    query = urlencode(params)
    params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return params


def get_view(session, bvid):
    # 先确保持有 buvid 相关 cookie：部分 IP/时段下 B 站 view 接口对无 cookie 的
    # 请求会返回 -400（请求错误），补齐 buvid 后重试一次可规避。
    ensure_buvid(session)
    last = None
    for attempt in range(2):
        try:
            resp = session.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid}, timeout=15).json()
            if resp.get("code") != 0:
                last = RuntimeError(
                    f"获取视频信息失败: {resp.get('message')} (code={resp.get('code')})")
                # -400 往往是临时风控/缺少 cookie，重试一次
                if resp.get("code") == -400 and attempt == 0:
                    ensure_buvid(session)
                    continue
                raise last
            return resp["data"]
        except (RuntimeError, ValueError) as e:
            raise e
        except Exception as e:
            last = e
    raise last


def pick_cid(view, page):
    if "pages" in view and len(view["pages"]) > 1:
        if page < 0 or page >= len(view["pages"]):
            raise ValueError(f"分 P 序号越界，该视频共有 {len(view['pages'])} 个分 P。")
        return view["pages"][page]["cid"]
    return view["cid"]


def get_playurl(session, bvid, cid, qn):
    img_key, sub_key = get_wbi_keys(session)
    params = {
        "bvid": bvid,
        "cid": cid,
        "qn": qn,
        "fnval": 16,   # 16 = 返回 DASH 格式
        "fourk": 1,
        "platform": "pc",
    }
    params = sign_params(params, img_key, sub_key)
    resp = session.get(
        "https://api.bilibili.com/x/player/playurl",
        params=params, timeout=15).json()
    if resp.get("code") != 0:
        raise RuntimeError(f"获取播放地址失败: {resp.get('message')} (code={resp.get('code')})")
    return resp["data"]


def choose_best(streams, key="id"):
    return max(streams, key=lambda s: s.get(key, 0))


def sanitize_filename(name):
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", name)
    return name.strip().strip(".")[:120] or "bilibili"


def have_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _system_proxies():
    """返回适用于 requests 的 proxies 字典。

    优先使用环境变量 HTTP(S)_PROXY；若没有，则在 Windows 上读取
    「Internet 设置」里的系统代理（很多 VPN 的「系统代理 / 全局模式」
    会写到这里）。这样即使 Python 进程没有直接继承代理环境变量，
    也能走用户的 VPN 出去，避免 ffmpeg 等外网下载失败。
    """
    proxies = {}
    env_http = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    env_https = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if env_http or env_https:
        proxies = {
            "http": (env_http or env_https),
            "https": (env_https or env_http),
        }
        return proxies
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
                if enabled:
                    server, _ = winreg.QueryValueEx(key, "ProxyServer")
                    override, _ = winreg.QueryValueEx(key, "ProxyOverride")
                    ov = {o.strip().lower().rstrip("/") for o in (override or "").split(";") if o.strip()}
                    if "=" in server:
                        for part in server.split(";"):
                            if "=" in part:
                                proto, addr = part.split("=", 1)
                                proto = proto.strip().lower()
                                if proto in ("http", "https"):
                                    proxies[proto] = ("http://" + addr.strip()) if not addr.startswith("http") else addr.strip()
                    else:
                        addr = server.strip()
                        if not addr.startswith("http"):
                            addr = "http://" + addr
                        proxies = {"http": addr, "https": addr}
        except Exception:
            pass
    return proxies


def find_ffmpeg_binary():
    """返回 ffmpeg 可执行文件路径。优先检查程序同级目录下的 ffmpeg.exe。"""
    candidates = []
    # 持久化目录（PyInstaller 单文件 exe 运行时即 exe 所在目录）
    candidates.append(os.path.join(get_program_dir(), "ffmpeg.exe"))
    here = os.path.dirname(os.path.abspath(__file__))
    if here != get_program_dir():
        candidates.append(os.path.join(here, "ffmpeg.exe"))
    if sys.platform == "win32":
        candidates.append(os.path.join(os.getcwd(), "ffmpeg.exe"))
    # 系统 PATH
    from shutil import which
    w = which("ffmpeg")
    if w:
        candidates.append(w)
    for p in candidates:
        if os.path.isfile(p):
            try:
                subprocess.run([p, "-version"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=True)
                return p
            except Exception:
                pass
    return None


def ffmpeg_cmd():
    """返回可调用的 ffmpeg 命令列表（包含路径）。"""
    binary = find_ffmpeg_binary()
    return [binary or "ffmpeg"]


def get_program_dir():
    """返回软件持久化目录。PyInstaller 单文件 exe 运行时，__file__ 指向临时目录，
    因此以 exe 所在目录为准，避免重启后文件丢失。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# ffmpeg 下载源：按顺序尝试，优先能直接返回二进制安装包的镜像
FFMPEG_URLS = [
    "https://ghfast.top/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://mirror.ghproxy.com/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    "https://ghproxy.com/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
]


def _ffmpeg_session():
    """专用于下载 ffmpeg 的会话：干净的浏览器 UA（不带 B 站 Referer），
    并自动套用系统代理 / 环境变量代理，确保走用户 VPN 联网。"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    proxies = _system_proxies()
    if proxies:
        s.proxies.update(proxies)
    return s


def _fetch_ffmpeg_zip(url, zip_path, log, progress):
    """下载单个 ffmpeg zip：支持断点续传与多次重试，抗网络抖动；
    若源返回网页则抛错以触发换源。"""
    s = _ffmpeg_session()
    MAX_RETRY = 5
    for attempt in range(1, MAX_RETRY + 1):
        try:
            resume = os.path.getsize(zip_path) if os.path.isfile(zip_path) else 0
            req_headers = {}
            if resume:
                req_headers["Range"] = f"bytes={resume}-"
            with s.get(url, stream=True, timeout=(30, None), allow_redirects=True, headers=req_headers) as r:
                ctype = (r.headers.get("Content-Type") or "").lower()
                if "text/html" in ctype:
                    raise RuntimeError("该下载源返回了网页而非安装包（可能被代理拦截或需浏览器跳转），换源重试")
                if resume and r.status_code == 200:
                    # 服务器不支持断点续传，从头开始
                    resume = 0
                total = (int(r.headers.get("Content-Length", 0)) or 0) + resume
                with open(zip_path, "ab" if resume else "wb") as f:
                    if resume:
                        f.seek(resume)
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        f.write(chunk)
                        resume += len(chunk)
                        progress("下载 ffmpeg", resume, total)
            # 校验确实是 zip（部分镜像会返回 HTML 落地页而不是二进制）
            with open(zip_path, "rb") as f:
                head = f.read(4)
            if head[:2] != b"PK":
                raise RuntimeError("下载到的文件不是有效的 ffmpeg 压缩包（内容校验失败），换源重试")
            return True
        except Exception as e:
            log(f"  （第 {attempt} 次下载中断: {e}）")
            time.sleep(1)
    raise RuntimeError("多次重试后仍未能完整下载 ffmpeg 安装包")


def download_ffmpeg_windows(out_dir, log=lambda *a, **k: None,
                            progress=lambda *a, **k: None):
    """为 Windows 下载静态 ffmpeg 到 out_dir。返回是否成功。"""
    import zipfile
    import tempfile

    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(tempfile.gettempdir(), "ffmpeg_download.zip")
    tmp_extract = os.path.join(tempfile.gettempdir(), "ffmpeg_extract")

    # 提示当前联网方式（帮助用户判断 VPN 是否生效）
    try:
        px = _system_proxies()
        if px:
            log(f"检测到系统代理，将通过代理联网下载: {px.get('https') or px.get('http')}")
        else:
            log("未检测到系统代理，将直连下载（请确保 VPN 已开「全局模式」或系统可直连外网）。")
    except Exception:
        pass

    # 清理旧残留
    for p in (zip_path, tmp_extract):
        try:
            if os.path.isfile(p):
                os.remove(p)
            elif os.path.isdir(p):
                shutil.rmtree(p)
        except Exception:
            pass

    last_err = "未尝试任何下载源"
    for url in FFMPEG_URLS:
        label = url.split("/")[-2] if ("ghproxy" in url or "ghfast" in url) else url.split("/")[-3]
        log(f"尝试下载 ffmpeg: {label}")
        # 每个镜像从干净文件开始（续传只在同一个镜像的重试内进行）
        try:
            if os.path.isfile(zip_path):
                os.remove(zip_path)
        except Exception:
            pass
        try:
            _fetch_ffmpeg_zip(url, zip_path, log, progress)
            break
        except Exception as e:
            last_err = str(e)
            log(f"该源失败: {last_err}")
            continue
    else:
        log(f"所有 ffmpeg 下载源均失败，最后错误: {last_err}")
        log("建议：确认 VPN 已开启「全局模式」后重试；或手动从 https://www.gyan.dev/ffmpeg/builds/ 下载 ffmpeg-release-essentials.zip，解压后将 ffmpeg.exe 放到本程序同级目录。")
        return False

    try:
        log("解压 ffmpeg…")
        progress("解压 ffmpeg", 1, 1)
        with zipfile.ZipFile(zip_path, "r") as z:
            if os.path.isdir(tmp_extract):
                shutil.rmtree(tmp_extract)
            z.extractall(tmp_extract)

        exe_src = None
        for root, dirs, files in os.walk(tmp_extract):
            for f in files:
                if f.lower() == "ffmpeg.exe":
                    exe_src = os.path.join(root, f)
                    break
            if exe_src:
                break

        if not exe_src or not os.path.isfile(exe_src):
            raise RuntimeError("解压后未找到 ffmpeg.exe")

        out_exe = os.path.join(out_dir, "ffmpeg.exe")
        shutil.copy2(exe_src, out_exe)
        log(f"ffmpeg 已保存到: {out_exe}")
        return True
    except Exception as e:
        log(f"ffmpeg 解压失败: {e}")
        return False
    finally:
        for p in (zip_path, tmp_extract):
            try:
                if os.path.isfile(p):
                    os.remove(p)
                elif os.path.isdir(p):
                    shutil.rmtree(p)
            except Exception:
                pass


def merge_with_ffmpeg(video_path, audio_path, out_path):
    subprocess.run(ffmpeg_cmd() + ["-y", "-i", video_path, "-i", audio_path,
                    "-c", "copy", "-movflags", "+faststart", out_path],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def remux_ffmpeg(src_path, out_path):
    subprocess.run(ffmpeg_cmd() + ["-y", "-i", src_path, "-c", "copy", out_path],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# 可直接 -c copy 封装的容器集合（其余需要重新编码）
AUDIO_COPY_SAFE = {".m4a", ".aac", ".mp4"}
VIDEO_COPY_SAFE = {".mp4", ".mkv", ".mov", ".flv"}


def reencode_ffmpeg(src_path, out_path, kind):
    """对非 copy-safe 后缀做重新编码（例如音频转 .mp3）。"""
    base = ffmpeg_cmd()
    if kind == "audio":
        cmd = base + ["-y", "-i", src_path, "-c:a", "libmp3lame", "-b:a", "320k", out_path]
    else:
        cmd = base + ["-y", "-i", src_path, "-c:v", "libx264", "-c:a", "aac",
               "-preset", "fast", out_path]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def resolve_ext(mode, user_ext):
    """根据用户选择解析最终文件后缀。"""
    if user_ext:
        e = user_ext.strip().split()[0]   # 去掉 " (默认)" 之类后缀说明
        if not e.startswith("."):
            e = "." + e
        return e.lower()
    return {"audio": ".m4a", "video": ".mp4", "both": ".mp4"}.get(mode, ".mp4")


def stream_download(url, path, session, on_progress=None):
    """流式下载单条流，on_progress(name, fetched, total) 回调进度。"""
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


def save_single_stream(url, final_path, session, label, on_progress, kind, ext):
    """保存单条流（视频或音频）。有 ffmpeg 就封装/重编码，否则直接重命名。"""
    tmp = final_path + ".tmp.m4s"
    stream_download(url, tmp, session,
                   on_progress=lambda f, t: on_progress(label, f, t))
    copy_safe = (ext in AUDIO_COPY_SAFE) if kind == "audio" else (ext in VIDEO_COPY_SAFE)
    if have_ffmpeg():
        try:
            if copy_safe:
                remux_ffmpeg(tmp, final_path)
            else:
                reencode_ffmpeg(tmp, final_path, kind)
            os.remove(tmp)
            return
        except subprocess.CalledProcessError:
            pass
    # 无 ffmpeg 或处理失败 -> 直接当作成品（B 站流多为 M4A/MP4 容器）
    if os.path.exists(final_path):
        os.remove(final_path)
    os.rename(tmp, final_path)


# ---------------------------------------------------------------------------
# 扫码登录（二维码）
# ---------------------------------------------------------------------------
def qr_generate(session):
    """生成登录二维码，返回 (qrcode_key, qr_url)。"""
    headers = {"User-Agent": UA, "Referer": "https://passport.bilibili.com/"}
    r = session.get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
        headers=headers, timeout=15)
    data = r.json().get("data", {})
    if not data.get("qrcode_key"):
        raise RuntimeError("生成二维码失败，请重试。")
    return data["qrcode_key"], data["url"]


def qr_poll(session, qrcode_key):
    """轮询扫码状态。返回 (status_code, response)。

    status_code: 0=成功, 86101=未扫描, 86090=已扫描待确认, 86038=已过期
    成功时 response.cookies 中已包含 SESSDATA 等登录态。
    """
    headers = {"User-Agent": UA, "Referer": "https://passport.bilibili.com/"}
    r = session.get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
        params={"qrcode_key": qrcode_key},
        headers=headers, timeout=15)
    j = r.json()
    return j.get("data", {}).get("code", -1), r


# ---------------------------------------------------------------------------
# 统一下载入口（CLI 与 GUI 共用）
# ---------------------------------------------------------------------------
def do_download(url, mode, out_dir, qn, cookie=None, page=0, hooks=None,
                filename=None, ext=None):
    """执行一次下载。

    hooks: dict，含 'log'(msg), 'progress'(label, fetched, total),
           'done'(success:bool, message:str), 'title'(title_str),
           'bytes'(total_fetched:int)。不传则静默。
    filename: 自定义文件名（留空则用 标题__BV号）。
    ext:      自定义后缀，如 '.mp3'；缺省按模式取默认容器。
    """
    hooks = hooks or {}
    log = hooks.get("log", lambda *a, **k: None)
    progress = hooks.get("progress", lambda *a, **k: None)
    done = hooks.get("done", lambda *a, **k: None)
    on_title = hooks.get("title", lambda *a, **k: None)
    on_bytes = hooks.get("bytes", lambda *a, **k: None)
    downloaded_bytes = 0

    try:
        bvid = parse_bvid(url)
        log(f"解析 BV 号: {bvid}")
        session = build_session(cookie)
        view = get_view(session, bvid)
        title = view.get("title", bvid)
        on_title(title)
        cid = pick_cid(view, page)
        log(f"视频标题: {title}")

        data = get_playurl(session, bvid, cid, qn)
        dash = data.get("dash")
        if not dash:
            raise RuntimeError("该视频未返回 DASH 流（可能是受限内容），暂不支持下载。")

        os.makedirs(out_dir, exist_ok=True)
        if filename and filename.strip():
            base_name = sanitize_filename(filename.strip())
        else:
            base_name = f"{sanitize_filename(title)}__{bvid}"
        ext = resolve_ext(mode, ext)
        audio_streams = dash.get("audio") or []
        video_streams = dash.get("video") or []
        if not audio_streams and mode != "video":
            raise RuntimeError("未找到可用音频流。")
        if not video_streams and mode != "audio":
            raise RuntimeError("未找到可用视频流。")

        # 选流
        audio_best = choose_best(audio_streams, key="bandwidth") if audio_streams else None
        video_best = choose_best(video_streams, key="id") if video_streams else None
        audio_url = (audio_best.get("baseUrl") or audio_best.get("backupUrl", [None])[0]) if audio_best else None
        video_url = (video_best.get("baseUrl") or video_best.get("backupUrl", [None])[0]) if video_best else None

        if mode == "audio":
            log(f"选择音频: {audio_best.get('id')} / {audio_best.get('bandwidth') // 1000}kbps")
            log(f"输出: {base_name}{ext}（{ext}）")
            out_path = os.path.join(out_dir, base_name + ext)
            save_single_stream(audio_url, out_path, session, "下载音频", progress, "audio", ext)
            if not have_ffmpeg():
                log(f"提示: 未检测到 ffmpeg，已直接保存为 {ext}（如播放异常请安装 ffmpeg）。")

        elif mode == "video":
            log(f"选择视频: {video_best.get('id')} / "
                f"{video_best.get('width')}x{video_best.get('height')}")
            log(f"输出: {base_name}{ext}（{ext}）")
            out_path = os.path.join(out_dir, base_name + ext)
            save_single_stream(video_url, out_path, session, "下载视频", progress, "video", ext)
            if not have_ffmpeg():
                log(f"提示: 未检测到 ffmpeg，已直接保存为 {ext}（无声，如需音轨请选“视频+音频”）。")

        else:  # both
            if not have_ffmpeg():
                raise RuntimeError("合并模式需要 ffmpeg，请先安装并加入 PATH（https://ffmpeg.org）。")
            log(f"选择视频: {video_best.get('id')} / "
                f"{video_best.get('width')}x{video_best.get('height')}")
            log(f"选择音频: {audio_best.get('id')} / {audio_best.get('bandwidth') // 1000}kbps")
            v_tmp = os.path.join(out_dir, base_name + ".video.m4s")
            a_tmp = os.path.join(out_dir, base_name + ".audio.m4s")
            out_path = os.path.join(out_dir, base_name + ext)
            try:
                stream_download(video_url, v_tmp, session,
                                on_progress=lambda f, t: progress("下载视频", f, t))
                stream_download(audio_url, a_tmp, session,
                                on_progress=lambda f, t: progress("下载音频", f, t))
                log("正在合并音视频（ffmpeg）…")
                progress("合并中", 1, 1)
                merge_with_ffmpeg(v_tmp, a_tmp, out_path)
            finally:
                for f in (v_tmp, a_tmp):
                    if os.path.exists(f):
                        os.remove(f)

        log(f"✅ 完成 -> {out_path}")
        done(True, out_path)

    except Exception as e:
        log(f"❌ 错误: {e}")
        done(False, str(e))


# ---------------------------------------------------------------------------
# 封面下载
# ---------------------------------------------------------------------------
def get_cover_url(session, bvid):
    """获取视频封面 URL（最大尺寸原图）。"""
    view = get_view(session, bvid)
    pic = view.get("pic")
    if not pic:
        raise RuntimeError("该视频未返回封面地址。")
    # 去掉可能存在的 @xxx 后缀，拿到原图
    pic = pic.split("@")[0]
    return pic, view.get("title", bvid)


def _enhance_with_opencv(img_bytes, scale, log=lambda *a, **k: None):
    """使用 OpenCV 放大并锐化图片。返回 bytes。"""
    import cv2
    import numpy as np
    arr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("OpenCV 无法解码图片")
    h, w = img.shape[:2]
    if scale > 1:
        # 使用 Lanczos 插值放大
        new_w, new_h = int(w * scale), int(h * scale)
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
        # 轻微细节增强
        try:
            img = cv2.detailEnhance(img, sigma_s=10, sigma_r=0.15)
        except Exception:
            pass
        # 轻微锐化
        kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
        img = cv2.filter2D(img, -1, kernel)
    _, buf = cv2.imencode(".png", img)
    return buf.tobytes()


def _enhance_with_pil(img_bytes, scale, log=lambda *a, **k: None):
    """使用 PIL 超采样 + 锐化增强图片。返回 RGB 图像对象。"""
    from PIL import Image, ImageFilter, ImageEnhance
    img = Image.open(io.BytesIO(img_bytes))
    # 转换常见模式为 RGB（处理 RGBA/P/LA）
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    if scale > 1:
        w, h = img.size
        # 超采样：先放大再缩放回目标，减少锯齿
        big = img.resize((w * scale * 2, h * scale * 2), Image.LANCZOS)
        target = big.resize((w * scale, h * scale), Image.LANCZOS)
        # 锐化
        target = target.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
        # 对比度
        target = ImageEnhance.Contrast(target).enhance(1.1)
        img = target
    return img


def download_cover(url, out_dir, filename=None, ext=".jpg", scale=2,
                   cookie=None, hooks=None):
    """下载并处理 B 站视频封面。

    hooks: 与 do_download 相同，含 log/progress/done/title/bytes。
    scale: 1=原图, 2=2x, 4=4x。
    ext: .jpg / .png / .webp。
    """
    hooks = hooks or {}
    log = hooks.get("log", lambda *a, **k: None)
    progress = hooks.get("progress", lambda *a, **k: None)
    done = hooks.get("done", lambda *a, **k: None)
    on_title = hooks.get("title", lambda *a, **k: None)
    on_bytes = hooks.get("bytes", lambda *a, **k: None)
    downloaded_bytes = 0

    try:
        bvid = parse_bvid(url)
        log(f"解析 BV 号: {bvid}")
        session = build_session(cookie)
        pic_url, title = get_cover_url(session, bvid)
        on_title(title)
        log(f"封面地址: {pic_url}")

        os.makedirs(out_dir, exist_ok=True)
        if filename and filename.strip():
            base_name = sanitize_filename(filename.strip())
        else:
            base_name = f"{sanitize_filename(title)}__{bvid}_cover"
        out_path = os.path.join(out_dir, base_name + ext)

        # 下载封面
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
        log(f"封面原图大小: {len(img_bytes)/1024:.1f} KB")

        # 清晰度增强
        if scale > 1:
            log(f"正在使用 {scale}x 清晰度增强…")
            progress("增强封面", 1, 1)
            try:
                # 优先尝试 OpenCV
                enhanced_bytes = _enhance_with_opencv(img_bytes, scale, log)
                # OpenCV 输出的是 PNG，需要按目标格式保存
                from PIL import Image
                tmp = Image.open(io.BytesIO(enhanced_bytes))
                tmp = tmp.convert("RGB")
                tmp.save(out_path, quality=95, optimize=True)
                log("使用 OpenCV 完成增强")
            except Exception as e:
                log(f"OpenCV 不可用或失败 ({e})，回退到 PIL…")
                from PIL import Image
                img = _enhance_with_pil(img_bytes, scale, log)
                img.save(out_path, quality=95, optimize=True)
                log("使用 PIL 完成增强")
        else:
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

        log(f"✅ 完成 -> {out_path}")
        done(True, out_path)

    except Exception as e:
        log(f"❌ 错误: {e}")
        done(False, str(e))


# ---------------------------------------------------------------------------
# 命令行模式
# ---------------------------------------------------------------------------
def run_cli():
    parser = argparse.ArgumentParser(
        description="B 站视频下载器：支持仅下音频 / 仅下视频 / 音视频合并。")
    parser.add_argument("-u", "--url", default=None,
                        help="视频 BV 号或完整链接")
    parser.add_argument("-m", "--mode", choices=["audio", "video", "both"], default="both",
                        help="audio=仅音频；video=仅视频；both=音视频合并（默认 both）")
    parser.add_argument("-q", "--quality", default="1080p",
                        choices=list(QN_BY_NAME.keys()), help="清晰度上限提示（默认 1080p）")
    parser.add_argument("-p", "--page", type=int, default=0, help="多 P 视频的分 P 序号，从 0 开始")
    parser.add_argument("-o", "--output", default="downloads", help="输出目录（默认 ./downloads）")
    parser.add_argument("-n", "--name", default=None, help="自定义文件名（缺省用 标题__BV号）")
    parser.add_argument("-s", "--suffix", default=None, help="自定义后缀，如 .mp3 / .mkv")
    parser.add_argument("--cookie", default=None,
                        help="登录 Cookie，例如 'SESSDATA=xxxx; bili_jct=yyyy'")
    args = parser.parse_args()

    if not args.url:
        parser.error("命令行模式需要 -u/--url；直接运行本文件可打开图形界面。")

    def cli_progress(label, fetched, total):
        if total:
            pct = fetched * 100 // total
            sys.stdout.write(f"\r{label}: {pct}%  {fetched // 1048576}MB/{total // 1048576}MB")
            sys.stdout.flush()
        else:
            sys.stdout.write(f"\r{label}: 已下载 {fetched // 1048576}MB")
            sys.stdout.flush()

    def cli_done(ok, msg):
        if ok:
            print(f"\n完成: {msg}")
        else:
            print(f"\n失败: {msg}")
            sys.exit(1)

    do_download(args.url, args.mode, args.output,
                QN_BY_NAME.get(args.quality, 80),
                cookie=args.cookie, page=args.page,
                filename=args.name, ext=args.suffix,
                hooks={"log": print, "progress": cli_progress, "done": cli_done})


# ---------------------------------------------------------------------------
# 图形界面：扫码登录窗口 / Cookie 说明窗口
# ---------------------------------------------------------------------------
def build_qr_login_window(parent, cookie_var):
    """弹出二维码登录窗口，扫码成功后自动把 Cookie 填入 cookie_var。"""
    import tkinter as tk
    from tkinter import messagebox

    ACCENT  = "#fb7299"
    HOVER   = "#fc8bab"
    CARD    = "#ffffff"
    TEXT    = "#222222"
    SUB     = "#8a8f99"
    BORDER  = "#e6e8eb"
    try:
        FONT_B = ("Microsoft YaHei", 10, "bold")
        FONT   = ("Microsoft YaHei", 10)
    except Exception:
        FONT = FONT_B = ("", 10)

    win = tk.Toplevel(parent)
    win.title("扫码登录 B 站")
    win.configure(bg=CARD)
    win.minsize(320, 420)
    win.transient(parent)
    win.grab_set()
    try:
        win.attributes("-topmost", True)
    except Exception:
        pass

    tk.Label(win, text="📱 哔哩哔哩扫码登录", bg=CARD, fg=TEXT,
             font=("Microsoft YaHei", 14, "bold")).pack(pady=(18, 4))
    tk.Label(win, text="用「哔哩哔哩」App 扫下方二维码", bg=CARD, fg=SUB,
             font=FONT).pack(pady=(0, 10))

    qr_label = tk.Label(win, bg=CARD, width=240, height=240)
    qr_label.pack(pady=6)

    status_var = tk.StringVar(value="正在生成二维码…")
    status_label = tk.Label(win, textvariable=status_var, bg=CARD, fg=ACCENT,
                           font=FONT_B, wraplength=280, justify="center")
    status_label.pack(pady=(6, 10))

    btn_row = tk.Frame(win, bg=CARD)
    btn_row.pack(pady=(0, 14))
    btn_refresh = tk.Button(btn_row, text="🔄 刷新二维码", font=FONT_B,
                            bg="white", fg=ACCENT, bd=1, relief="solid",
                            highlightbackground=BORDER, cursor="hand2", padx=12, pady=6)
    btn_refresh.pack(side="left", padx=6)
    btn_cancel = tk.Button(btn_row, text="取消", font=FONT_B,
                           bg="white", fg=SUB, bd=1, relief="solid",
                           highlightbackground=BORDER, cursor="hand2", padx=12, pady=6)
    btn_cancel.pack(side="left", padx=6)

    stop = threading.Event()
    session = build_session()
    state = {"key": None, "url": None}

    def make_qr_image(url_str):
        try:
            import qrcode
            from PIL import Image, ImageTk
        except ImportError:
            raise RuntimeError("缺少 qrcode/pillow 库，请运行: pip install qrcode pillow")
        img = qrcode.make(url_str).resize((240, 240))
        return ImageTk.PhotoImage(img)

    def regenerate():
        try:
            key, url = qr_generate(session)
        except Exception as e:
            status_var.set(f"生成失败: {e}")
            return
        state["key"], state["url"] = key, url
        try:
            photo = make_qr_image(url)
        except Exception as e:
            status_var.set(str(e))
            return
        qr_label.configure(image=photo)
        qr_label.image = photo
        status_var.set("请使用哔哩哔哩 App 扫码")

    def poll():
        while not stop.is_set():
            try:
                code, r = qr_poll(session, state["key"])
            except Exception as e:
                parent.after(0, lambda: status_var.set(f"轮询出错: {e}"))
                return
            if code == 0:
                cookies = r.cookies.get_dict()
                cs = "; ".join(f"{k}={v}" for k, v in cookies.items())
                parent.after(0, lambda c=cs: cookie_var.set(c))
                parent.after(0, lambda: status_var.set("✅ 登录成功！已自动填入 Cookie"))
                parent.after(900, win.destroy)
                return
            elif code == 86090:
                parent.after(0, lambda: status_var.set("已扫描，请在手机上点击「确认登录」"))
            elif code == 86038:
                parent.after(0, lambda: status_var.set("二维码已过期，正在刷新…"))
                regenerate()
            else:  # 86101 未扫描
                parent.after(0, lambda: status_var.set("请使用哔哩哔哩 App 扫码"))
            if stop.wait(2):
                return

    def on_close():
        stop.set()
        win.destroy()

    btn_cancel.configure(command=on_close)
    btn_refresh.configure(command=lambda: (regenerate(), status_var.set("已刷新，请重新扫码")))
    win.protocol("WM_DELETE_WINDOW", on_close)

    regenerate()
    threading.Thread(target=poll, daemon=True).start()


def build_cookie_help_window(parent):
    """弹出 Cookie 教程窗口。"""
    import tkinter as tk
    from tkinter import scrolledtext

    ACCENT = "#fb7299"
    CARD   = "#ffffff"
    TEXT   = "#222222"
    SUB    = "#8a8f99"
    BORDER = "#e6e8eb"
    try:
        FONT = ("Microsoft YaHei", 10)
    except Exception:
        FONT = ("", 10)

    win = tk.Toplevel(parent)
    win.title("Cookie 说明 / 获取教程")
    win.configure(bg=CARD)
    win.minsize(420, 480)
    win.transient(parent)
    win.grab_set()

    tk.Label(win, text="🍪 什么是登录 Cookie？怎么获取？", bg=CARD, fg=TEXT,
             font=("Microsoft YaHei", 13, "bold"), wraplength=380,
             justify="left", anchor="w").pack(pady=(14, 6), padx=16, anchor="w")

    text = scrolledtext.ScrolledText(win, font=FONT, bg="#fbfbfc", fg=TEXT,
                                     relief="flat", bd=0, wrap="word",
                                     highlightthickness=1, highlightbackground=BORDER)
    text.pack(fill="both", expand=True, padx=16, pady=8)
    text.insert("end", COOKIE_HELP_TEXT)
    text.configure(state="disabled")

    tk.Button(win, text="我知道了", command=win.destroy,
              bg=ACCENT, fg="white", font=("Microsoft YaHei", 10, "bold"),
              bd=0, relief="flat", cursor="hand2", padx=18, pady=6
              ).pack(pady=(0, 14))


# Cookie 教程正文
COOKIE_HELP_TEXT = """\
【Cookie 是什么？】
Cookie 是你的浏览器登录 B 站后保存的一串身份凭证。下载器带上它，就能以“已登录”的身份获取更高清晰度（1080P+、4K）和会员视频。

其中最关键的是 SESSDATA 这一项。

【推荐方式①：扫码登录（最简单）】
直接点主界面的「📱 扫码登录」按钮，用哔哩哔哩 App 扫码并确认，程序会自动把登录 Cookie 填好，无需任何手动操作。

【方式②：手动复制 Cookie】
1) 电脑浏览器打开 https://www.bilibili.com 并登录你的账号。
2) 按 F12 打开“开发者工具” → 切到「应用程序 / Application」标签
   （Chrome 在 Application → 左侧 Storage → Cookies → https://www.bilibili.com）。
3) 找到 SESSDATA，双击复制它的值（一长串字母数字）。
4) 在主界面 Cookie 框里粘贴：  SESSDATA=你复制的值
   如有 bili_jct 也可一并加上，用分号隔开，例如：
   SESSDATA=xxxx; bili_jct=yyyy

【注意事项】
• Cookie 等于你的登录态，等同于账号密码，请勿发给他人或在公共电脑长期保存。
• 复制到的 SESSDATA 显示为星号（*）仅本地掩码，不会外传。
• 登录态会过期，失效后重新扫码或重新复制即可。
• 不填 Cookie 也能下载，但清晰度会被限制为游客最高（通常 480P）。
"""


# ---------------------------------------------------------------------------
# 图形界面主程序
# ---------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, scrolledtext, messagebox

    # —— 主题配色（B 站粉）——
    BG      = "#f4f5f7"
    CARD    = "#ffffff"
    ACCENT  = "#fb7299"
    ACCENT2 = "#00aeec"
    TEXT    = "#222222"
    SUB     = "#8a8f99"
    BORDER  = "#e6e8eb"
    HOVER   = "#fc8bab"

    try:
        FONT     = ("Microsoft YaHei", 10)
        FONT_B   = ("Microsoft YaHei", 10, "bold")
        FONT_T   = ("Microsoft YaHei", 20, "bold")
    except Exception:
        FONT = FONT_B = FONT_T = ("", 10)

    root = tk.Tk()
    root.title("B 站视频下载器")
    root.configure(bg=BG)
    root.minsize(560, 660)

    # —— 顶部标题条 ——
    header = tk.Frame(root, bg=ACCENT, height=78)
    header.pack(fill="x")
    header.pack_propagate(False)
    tk.Label(header, text="📥 B 站视频下载器", bg=ACCENT, fg="white",
             font=FONT_T).pack(side="left", padx=22, pady=14)
    tk.Label(header, text="粘贴链接 · 选择模式 · 一键下载", bg=ACCENT, fg="#ffe3ec",
             font=FONT).pack(side="left", padx=6, pady=20)

    # —— 主体卡片 ——
    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True, padx=22, pady=18)

    def card(parent, **kw):
        f = tk.Frame(parent, bg=CARD, bd=1, relief="solid",
                     highlightbackground=BORDER, highlightthickness=1)
        f.pack(fill="x", pady=8, **kw)
        return f

    def field_label(parent, text):
        tk.Label(parent, text=text, bg=CARD, fg=TEXT, font=FONT_B,
                 anchor="w").pack(anchor="w", padx=14, pady=(12, 4))

    # 链接输入
    c1 = card(body)
    field_label(c1, "视频链接 / BV 号")
    url_var = tk.StringVar()
    url_entry = tk.Entry(c1, textvariable=url_var, font=FONT, bd=0,
                         relief="flat", bg="#f7f8fa", fg=TEXT,
                         insertbackground=ACCENT)
    url_entry.pack(fill="x", padx=14, pady=(0, 12), ipady=8)
    url_entry.configure(highlightthickness=1, highlightbackground=BORDER,
                        highlightcolor=ACCENT)

    # 保存位置
    c2 = card(body)
    field_label(c2, "保存位置")
    row2 = tk.Frame(c2, bg=CARD)
    row2.pack(fill="x", padx=14, pady=(0, 12))
    folder_var = tk.StringVar(value=os.path.join(os.getcwd(), "downloads"))
    folder_entry = tk.Entry(row2, textvariable=folder_var, font=FONT, bd=0,
                            relief="flat", bg="#f7f8fa", fg=SUB,
                            state="readonly",
                            highlightthickness=1, highlightbackground=BORDER,
                            highlightcolor=ACCENT)
    folder_entry.pack(side="left", fill="x", expand=True, ipady=8)

    def choose_folder():
        d = filedialog.askdirectory(title="选择保存文件夹")
        if d:
            folder_var.set(d)

    btn_browse = tk.Button(row2, text="选择文件夹", command=choose_folder,
                           bg="white", fg=ACCENT, font=FONT_B, bd=1,
                           relief="solid", highlightbackground=BORDER,
                           activebackground="#fff0f5", cursor="hand2",
                           padx=12, pady=6)
    btn_browse.pack(side="right", padx=(8, 0))

    # 文件名（自定义）
    c_fn = card(body)
    field_label(c_fn, "保存文件名（留空则用 标题__BV号）")
    filename_var = tk.StringVar()
    fn_entry = tk.Entry(c_fn, textvariable=filename_var, font=FONT, bd=0,
                        relief="flat", bg="#f7f8fa", fg=TEXT,
                        insertbackground=ACCENT,
                        highlightthickness=1, highlightbackground=BORDER,
                        highlightcolor=ACCENT)
    fn_entry.pack(fill="x", padx=14, pady=(0, 12), ipady=8)

    # 文件后缀（自定义，随模式切换可选值）
    c_ext = card(body)
    field_label(c_ext, "文件后缀（格式）")
    ext_var = tk.StringVar(value=".m4a (默认)")
    ext_combo = ttk.Combobox(c_ext, textvariable=ext_var, state="readonly",
                             font=FONT, width=14)
    ext_combo.pack(fill="x", padx=14, pady=(0, 12), ipady=4)

    def refresh_ext_for_mode(m):
        opts = {
            "audio": [".m4a (默认)", ".mp3", ".aac"],
            "video": [".mp4 (默认)", ".mkv", ".mov"],
            "both":  [".mp4 (默认)", ".mkv", ".mov"],
        }[m]
        ext_combo["values"] = opts
        ext_combo.set(opts[0])

    # 模式选择（三段式）
    c3 = card(body)
    field_label(c3, "下载模式")
    mode_var = tk.StringVar(value="both")
    seg = tk.Frame(c3, bg=CARD)
    seg.pack(fill="x", padx=14, pady=(0, 12))

    seg_buttons = {}

    def select_mode(m):
        mode_var.set(m)
        refresh_ext_for_mode(m)
        for k, b in seg_buttons.items():
            if k == m:
                b.configure(bg=ACCENT, fg="white", relief="solid")
            else:
                b.configure(bg="white", fg=SUB, relief="solid")

    for i, (m, lbl) in enumerate(MODE_LABELS.items()):
        b = tk.Button(seg, text=lbl, font=FONT_B, bd=1, relief="solid",
                      highlightbackground=BORDER, cursor="hand2",
                      command=lambda mm=m: select_mode(mm))
        b.pack(side="left", fill="x", expand=True, padx=(0 if i == 0 else 6), ipady=8)
        seg_buttons[m] = b
    select_mode("both")

    # 清晰度
    c4 = card(body)
    field_label(c4, "清晰度上限（提示值，最终以可用最高为准）")
    qrow = tk.Frame(c4, bg=CARD)
    qrow.pack(fill="x", padx=14, pady=(0, 12))
    q_var = tk.StringVar(value="1080p")
    q_combo = ttk.Combobox(qrow, textvariable=q_var,
                           values=list(QN_BY_NAME.keys()),
                           state="readonly", font=FONT, width=12)
    q_combo.pack(side="left", ipady=4)
    tk.Label(qrow, text="  未登录默认最高 480p，登录 Cookie 可解锁更高清",
             bg=CARD, fg=SUB, font=FONT).pack(side="left", padx=8)

    # Cookie（可选）
    c5 = card(body)
    field_label(c5, "登录 Cookie（可选，用于解锁高清/会员内容）")
    cookie_var = tk.StringVar()
    cookie_entry = tk.Entry(c5, textvariable=cookie_var, font=FONT, bd=0,
                            relief="flat", bg="#f7f8fa", fg=TEXT, show="*",
                            highlightthickness=1, highlightbackground=BORDER,
                            highlightcolor=ACCENT)
    cookie_entry.pack(fill="x", padx=14, pady=(0, 8), ipady=8)

    cookie_btn_row = tk.Frame(c5, bg=CARD)
    cookie_btn_row.pack(fill="x", padx=14, pady=(0, 12))

    def open_qr_login():
        build_qr_login_window(root, cookie_var)

    def show_cookie_help():
        build_cookie_help_window(root)

    btn_qr = tk.Button(cookie_btn_row, text="📱 扫码登录", command=open_qr_login,
                       bg=ACCENT, fg="white", font=FONT_B, bd=0, relief="flat",
                       cursor="hand2", padx=14, pady=6)
    btn_qr.pack(side="left", padx=(0, 8))
    btn_qr.bind("<Enter>", lambda e: btn_qr.configure(bg=HOVER))
    btn_qr.bind("<Leave>", lambda e: btn_qr.configure(bg=ACCENT))

    btn_help = tk.Button(cookie_btn_row, text="❓ Cookie 说明", command=show_cookie_help,
                         bg="white", fg=ACCENT2, font=FONT_B, bd=1, relief="solid",
                         highlightbackground=BORDER, cursor="hand2", padx=14, pady=6)
    btn_help.pack(side="left")

    # 下载按钮
    def start_download():
        u = url_var.get().strip()
        if not u:
            messagebox.showwarning("提示", "请先粘贴视频链接或 BV 号。")
            return
        out_dir = folder_var.get().strip() or os.path.join(os.getcwd(), "downloads")
        btn_start.configure(state="disabled", text="下载中…")
        log_text.delete("1.0", "end")
        log_line_var.set("准备中…")
        progress_bar["value"] = 0

        def worker():
            do_download(
                u, mode_var.get(), out_dir,
                QN_BY_NAME.get(q_var.get(), 80),
                cookie=cookie_var.get().strip() or None,
                filename=filename_var.get().strip() or None,
                ext=ext_var.get().strip() or None,
                hooks={
                    "log": lambda m: root.after(0, lambda mm=m: append_log(mm)),
                    "progress": lambda lab, f, t: root.after(
                        0, lambda: set_progress(lab, f, t)),
                    "done": lambda ok, msg: root.after(
                        0, lambda: on_done(ok, msg)),
                })

        threading.Thread(target=worker, daemon=True).start()

    btn_start = tk.Button(root, text="⬇  开始下载", command=start_download,
                          bg=ACCENT, fg="white", font=("Microsoft YaHei", 13, "bold"),
                          bd=0, relief="flat", cursor="hand2", pady=12)
    btn_start.pack(fill="x", padx=22, pady=(0, 6))
    btn_start.bind("<Enter>", lambda e: btn_start.configure(bg=HOVER))
    btn_start.bind("<Leave>", lambda e: btn_start.configure(bg=ACCENT))

    # 进度 + 状态
    progress_bar = ttk.Progressbar(root, orient="horizontal", length=100,
                                   mode="determinate")
    progress_bar.pack(fill="x", padx=22, pady=(4, 2))
    progress_bar["maximum"] = 100

    log_line_var = tk.StringVar(value="等待下载…")
    status_label = tk.Label(root, textvariable=log_line_var, bg=BG, fg=SUB,
                            font=FONT, anchor="w")
    status_label.pack(fill="x", padx=22, pady=(0, 6))

    # 日志框
    log_text = scrolledtext.ScrolledText(root, height=8, font=("Consolas", 9),
                                        bg="#1e1e24", fg="#d6d6d6",
                                        relief="flat", bd=0,
                                        highlightthickness=1,
                                        highlightbackground=BORDER)
    log_text.pack(fill="both", expand=True, padx=22, pady=(0, 10))
    log_text.insert("end", "日志区域\n")
    log_text.configure(state="disabled")

    def append_log(msg):
        log_text.configure(state="normal")
        log_text.insert("end", msg + "\n")
        log_text.see("end")
        log_text.configure(state="disabled")

    def set_progress(label, fetched, total):
        if total:
            pct = fetched * 100 // total
            progress_bar["value"] = pct
            log_line_var.set(f"{label}: {pct}%  "
                             f"{fetched // 1048576}MB / {total // 1048576}MB")
        else:
            progress_bar["mode"] = "indeterminate"
            progress_bar.start(20)
            log_line_var.set(f"{label}: 下载中 {fetched // 1048576}MB")

    def on_done(ok, msg):
        progress_bar.stop()
        progress_bar["mode"] = "determinate"
        btn_start.configure(state="normal", text="⬇  开始下载")
        if ok:
            log_line_var.set("✅ 下载完成！")
            messagebox.showinfo("完成", f"已保存到：\n{msg}")
        else:
            log_line_var.set("❌ 下载失败")
            messagebox.showerror("失败", msg)

    # 打开文件夹按钮
    def open_folder():
        d = folder_var.get().strip()
        if d and os.path.isdir(d):
            try:
                os.startfile(d)
            except Exception:
                pass

    btn_open = tk.Button(root, text="📁 打开保存文件夹", command=open_folder,
                         bg="white", fg=ACCENT2, font=FONT_B, bd=1,
                         relief="solid", highlightbackground=BORDER,
                         cursor="hand2", pady=6)
    btn_open.pack(padx=22, pady=(0, 14), anchor="e")

    root.mainloop()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # 无任何参数时启动图形界面；带 -u 等参数时走命令行
    if len(sys.argv) == 1:
        try:
            run_gui()
        except ImportError:
            sys.stderr.write("当前 Python 缺少 tkinter，无法启动图形界面。\n"
                             "请使用标准 Python（如官网安装版）运行，或用 -u 走命令行模式。\n")
            sys.exit(1)
    else:
        # 带 --web 时启动本地 Web 界面（网页与 Python 联动）
        if "--web" in sys.argv:
            import web_app
            web_app.run()
            sys.exit(0)
        run_cli()
