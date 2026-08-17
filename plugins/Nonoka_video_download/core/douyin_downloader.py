#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音视频下载器（网页版解析）。

支持：
  - 视频（无水印优先）
  - 仅音频
  - 视频封面

输入：抖音分享短链接、视频链接、或含链接的分享口令。
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
DOUYIN_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.douyin.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def _build_session():
    s = requests.Session()
    s.headers.update(DOUYIN_HEADERS)
    return s


def _extract_url_from_text(text):
    """从分享口令/文本中提取抖音 URL。"""
    m = re.search(r"(https?://(?:v\.douyin\.com|www\.douyin\.com|webcast\.amemv\.com)/[^\s\]\)]+)", text)
    if m:
        return m.group(1)
    # 兜底：提取任意 http 链接
    m = re.search(r"(https?://[^\s\]\)]+)", text)
    return m.group(1) if m else text.strip()


def _resolve_short_url(url):
    """如果是短链，跟随重定向拿到真实长链接。"""
    try:
        r = requests.head(url, headers=DOUYIN_HEADERS, allow_redirects=True, timeout=20)
        return r.url
    except Exception:
        return url


def _get_video_id(url):
    """从长链接提取 video_id 或 modal_id。"""
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


def _extract_render_data(html):
    """从抖音网页 HTML 中提取 RENDER_DATA JSON。"""
    # 新版页面
    m = re.search(r'<script id="RENDER_DATA" type="application/json">([^<]+)</script>', html)
    if m:
        raw = m.group(1)
        # 可能是 URL 编码的
        if raw.startswith("%"):
            raw = urllib.parse.unquote(raw)
        return json.loads(raw)
    # 旧版/备用
    m = re.search(r'<script[^>]*>window\._SSR_HYDRATED_DATA\s*=\s*([^<]+)</script>', html)
    if m:
        return json.loads(m.group(1))
    return None


def _walk_json(obj, key):
    """在嵌套 dict/list 中查找第一个匹配 key 的值。"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _walk_json(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _walk_json(item, key)
            if found is not None:
                return found
    return None


def _pick_url(url_list):
    """从 url_list 中挑一个看起来最可靠的。"""
    if not url_list:
        return None
    # 优先 http(s) 字符串
    for u in url_list:
        if isinstance(u, str) and u.startswith("http"):
            return u
    return url_list[0]


def _fetch_via_browser(url, video_id):
    """服务端解析失败时，用本机浏览器（系统 Chrome/Edge）真实渲染页面兜底解析。

    通过独立子进程（sys.executable --browser-parse）调用，使 Playwright 运行在
    自己的主线程里，规避同步 API 在 Web 服务工作线程中的死锁。

    关键点：子进程（Chrome/Playwright）会产生大量 stderr 输出，若用
    capture_output=True 同时捕获 stdout+stderr，64KB 管道缓冲区会被 stderr 写满，
    导致子进程阻塞、父进程永远收不到退出 -> 死锁。因此这里把 stderr 直接丢弃
    （DEVNULL），只保留 stdout（仅一行 JSON，体积小），并配套超时杀进程树。
    """
    import json
    import subprocess
    import sys

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
        # 只截取最后的 JSON 块，容忍子进程在 JSON 前打印的警告信息
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
        # 超时：杀掉整棵进程树（含 Chrome），避免僵尸进程占用资源
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
        return {"err": f"浏览器兜底解析失败: {e}"}


def fetch_video_info(url):
    """获取抖音视频信息。返回 dict：title, cover, video_url, audio_url, author, err。"""
    url = _extract_url_from_text(url)
    url = _resolve_short_url(url)
    video_id, _ = _get_video_id(url)
    if not video_id:
        return {"err": "无法从链接中解析出抖音视频 ID，请检查链接是否完整。"}

    session = _build_session()
    detail_url = f"https://www.douyin.com/video/{video_id}"
    try:
        r = session.get(detail_url, timeout=20)
        r.raise_for_status()
    except Exception as e:
        return _fetch_via_browser(url, video_id)

    render = _extract_render_data(r.text)
    if not render:
        return _fetch_via_browser(url, video_id)

    # 尝试多个常见路径
    item = None
    for path_key in ("app", "data", "initialState"):
        node = render.get(path_key) if isinstance(render, dict) else None
        if not node:
            continue
        item = _walk_json(node, "itemInfo") or _walk_json(node, "item_list") or _walk_json(node, "item")
        if isinstance(item, list) and item:
            item = item[0]
        if item and isinstance(item, dict):
            break

    if not item or not isinstance(item, dict):
        return _fetch_via_browser(url, video_id)

    title = item.get("desc") or item.get("share_info", {}).get("share_title") or "douyin_video"
    author = item.get("author", {}).get("nickname") or ""

    # 封面
    cover = None
    cover_obj = item.get("video", {}).get("cover") or item.get("cover")
    if isinstance(cover_obj, dict):
        cover = _pick_url(cover_obj.get("url_list") or cover_obj.get("url"))
    elif isinstance(cover_obj, str):
        cover = cover_obj

    # 视频地址（优先 play_addr，不带水印）
    video_url = None
    video_obj = item.get("video", {})
    play_addr = video_obj.get("play_addr") or video_obj.get("playAddr")
    if isinstance(play_addr, dict):
        video_url = _pick_url(play_addr.get("url_list") or play_addr.get("url"))
    if not video_url:
        download_addr = video_obj.get("download_addr") or video_obj.get("downloadAddr")
        if isinstance(download_addr, dict):
            video_url = _pick_url(download_addr.get("url_list") or download_addr.get("url"))

    # 音频地址
    audio_url = None
    music_obj = item.get("music", {})
    if isinstance(music_obj, dict):
        play_url = music_obj.get("play_url") or music_obj.get("playUrl")
        if isinstance(play_url, dict):
            audio_url = _pick_url(play_url.get("url_list") or play_url.get("url"))
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


def sanitize_filename(name):
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", name)
    return name.strip().strip(".")[:120] or "douyin"


def _stream_download(url, path, session, label, progress, on_bytes=None):
    headers = {"User-Agent": UA, "Referer": "https://www.douyin.com/"}
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


def do_douyin_download(url, mode, out_dir, filename=None, ext=None, hooks=None):
    """下载抖音视频/音频。

    mode: audio / video / both
    """
    hooks = hooks or {}
    log = hooks.get("log", lambda *a, **k: None)
    progress = hooks.get("progress", lambda *a, **k: None)
    done = hooks.get("done", lambda *a, **k: None)
    on_title = hooks.get("title", lambda *a, **k: None)
    on_bytes = hooks.get("bytes", lambda *a, **k: None)

    try:
        log("正在解析抖音链接…")
        info = fetch_video_info(url)
        if info.get("err"):
            raise RuntimeError(info["err"])

        title = info["title"]
        on_title(title)
        log(f"标题: {title}")
        if info.get("author"):
            log(f"作者: {info['author']}")

        os.makedirs(out_dir, exist_ok=True)
        base_name = sanitize_filename(filename.strip()) if filename and filename.strip() else sanitize_filename(title)
        base_name = base_name or f"douyin_{info['video_id']}"

        session = _build_session()

        if mode == "audio":
            if not info.get("audio_url"):
                raise RuntimeError("未找到该视频的音频地址。")
            ext = ext or ".mp3"
            out_path = os.path.join(out_dir, base_name + ext)
            log(f"开始下载音频 -> {out_path}")
            _stream_download(info["audio_url"], out_path, session, "下载音频", progress, on_bytes)
            log(f"✅ 完成 -> {out_path}")
            done(True, out_path)

        elif mode == "video":
            if not info.get("video_url"):
                raise RuntimeError("未找到该视频的视频地址，可能链接已失效或被限制。")
            ext = ext or ".mp4"
            out_path = os.path.join(out_dir, base_name + ext)
            log(f"开始下载视频 -> {out_path}")
            _stream_download(info["video_url"], out_path, session, "下载视频", progress, on_bytes)
            log(f"✅ 完成 -> {out_path}")
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
                _stream_download(info["video_url"], v_tmp, session, "下载视频", progress, on_bytes)
                log("开始下载音频流…")
                _stream_download(info["audio_url"], a_tmp, session, "下载音频", progress, on_bytes)

                # 尝试 ffmpeg 合并
                try:
                    from bilibili_downloader import have_ffmpeg, merge_with_ffmpeg
                    if have_ffmpeg():
                        log("正在合并音视频（ffmpeg）…")
                        progress("合并中", 1, 1)
                        merge_with_ffmpeg(v_tmp, a_tmp, out_path)
                    else:
                        # 无 ffmpeg：直接把视频流当最终文件（无声）
                        log("未检测到 ffmpeg，仅保存视频流（无声）。如需合并请下载 ffmpeg。")
                        if os.path.exists(out_path):
                            os.remove(out_path)
                        os.rename(v_tmp, out_path)
                        # 保留音频备用
                        a_bak = os.path.join(out_dir, base_name + ".audio.m4a")
                        if os.path.exists(a_bak):
                            os.remove(a_bak)
                        os.rename(a_tmp, a_bak)
                        log(f"音频已额外保存为: {a_bak}")
                except Exception as e:
                    log(f"合并失败: {e}，保留视频流")
                    if os.path.exists(out_path):
                        os.remove(out_path)
                    os.rename(v_tmp, out_path)
                log(f"✅ 完成 -> {out_path}")
                done(True, out_path)
            finally:
                for f in (v_tmp, a_tmp):
                    if os.path.exists(f):
                        try:
                            os.remove(f)
                        except Exception:
                            pass

    except Exception as e:
        log(f"❌ 错误: {e}")
        done(False, str(e))


def do_douyin_cover(url, out_dir, filename=None, ext=".jpg", scale=2, hooks=None):
    """下载抖音视频封面，支持清晰度增强。"""
    hooks = hooks or {}
    log = hooks.get("log", lambda *a, **k: None)
    progress = hooks.get("progress", lambda *a, **k: None)
    done = hooks.get("done", lambda *a, **k: None)
    on_title = hooks.get("title", lambda *a, **k: None)
    on_bytes = hooks.get("bytes", lambda *a, **k: None)

    try:
        log("正在解析抖音链接…")
        info = fetch_video_info(url)
        if info.get("err"):
            raise RuntimeError(info["err"])

        title = info["title"]
        on_title(title)
        cover_url = info.get("cover")
        if not cover_url:
            raise RuntimeError("未找到该视频的封面地址。")

        os.makedirs(out_dir, exist_ok=True)
        base_name = sanitize_filename(filename.strip()) if filename and filename.strip() else sanitize_filename(title) + "_cover"
        base_name = base_name or f"douyin_{info['video_id']}_cover"
        out_path = os.path.join(out_dir, base_name + ext)

        log("正在下载封面…")
        progress("下载封面", 0, 1)
        session = _build_session()
        r = session.get(cover_url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"}, timeout=30)
        r.raise_for_status()
        img_bytes = r.content
        on_bytes(len(img_bytes))
        progress("下载封面", 1, 1)

        if scale > 1:
            log(f"正在使用 {scale}x 清晰度增强…")
            progress("增强封面", 1, 1)
            try:
                from bilibili_downloader import _enhance_with_opencv
                from PIL import Image
                enhanced = _enhance_with_opencv(img_bytes, scale, log)
                tmp = Image.open(__import__("io").BytesIO(enhanced))
                tmp = tmp.convert("RGB")
                tmp.save(out_path, quality=95, optimize=True)
                log("使用 OpenCV 完成增强")
            except Exception as e:
                log(f"OpenCV 不可用或失败 ({e})，回退到 PIL…")
                from bilibili_downloader import _enhance_with_pil
                from PIL import Image
                img = _enhance_with_pil(img_bytes, scale, log)
                img.save(out_path, quality=95, optimize=True)
                log("使用 PIL 完成增强")
        else:
            from PIL import Image
            img = Image.open(__import__("io").BytesIO(img_bytes))
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


if __name__ == "__main__":
    # 简单命令行测试
    test_url = sys.argv[1] if len(sys.argv) > 1 else ""
    if not test_url:
        print("用法: python douyin_downloader.py <抖音链接>")
        sys.exit(1)
    info = fetch_video_info(test_url)
    print(json.dumps(info, ensure_ascii=False, indent=2))
