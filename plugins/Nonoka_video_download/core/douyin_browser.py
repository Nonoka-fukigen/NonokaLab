# -*- coding: utf-8 -*-
"""
抖音浏览器解析器（兜底方案）。

部分抖音链接（如 jingxuan?modal_id=）服务端直连会被风控拦截、页面不内嵌 RENDER_DATA。
此时用本机已安装的 Chrome/Edge（Playwright channel）真实渲染页面，从视频元素 /
网络请求中拿到真实可下载的视频直链、封面与标题。

依赖：pip install playwright  （无需额外下载浏览器，复用系统 Chrome/Edge）
"""

import os
import re
import sys
import time

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36)")


def _launch_in_context(p):
    """在已打开的 sync_playwright() 上下文里启动浏览器，优先 Chrome 其次 Edge。"""
    last_err = None
    for channel in ("chrome", "msedge"):
        try:
            return p.chromium.launch(
                channel=channel, headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                      "--disable-dev-shm-usage"])
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"未找到系统 Chrome / Edge，浏览器解析不可用：{last_err}")


def fetch_video_info_browser(url):
    """用浏览器渲染页面，返回 dict：title, cover, video_url, audio_url, video_id, err。"""
    if sync_playwright is None:
        return {"err": "未安装 Playwright，无法使用浏览器解析（pip install playwright）。"}

    video_id = None
    m = (re.search(r"[?&]modal_id=(\d+)", url) or re.search(r"/video/(\d+)", url)
         or re.search(r"item_id=(\d+)", url))
    if m:
        video_id = m.group(1)

    try:
        with sync_playwright() as p:
            browser = _launch_in_context(p)
            captured = []

            def on_response(resp):
                u = resp.url
                if any(k in u for k in ("douyinvod", "bytecdn", "amemv", "snssdk")) and \
                   (".mp4" in u or ".m3u8" in u or "video" in u or "play" in u):
                    captured.append(u)
                ct = resp.headers.get("content-type", "")
                if "json" in ct and ("aweme" in u or "play" in u or "video" in u):
                    try:
                        s = __import__("json").dumps(resp.json(), ensure_ascii=False)
                        for mm in re.finditer(
                                r'https?://[^\s"\'\\]+?(?:douyinvod\.com|bytecdn\.com|snssdk\.com|amemv\.com)[^\s"\'\\]*', s):
                            captured.append(mm.group(1))
                    except Exception:
                        pass

            ctx = browser.new_context(user_agent=UA, locale="zh-CN",
                                      viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            page.on("response", on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector("video", timeout=30000)
            # 强制静音自动播放，触发真实流地址的网络请求（headless 下常需显式 play）
            try:
                page.evaluate("""() => {
                    const v = document.querySelector('video');
                    if (v) {
                        try { v.muted = true; } catch (e) {}
                        try { v.setAttribute('muted',''); } catch (e) {}
                        const p = v.play();
                        if (p && p.catch) p.catch(function(){});
                    }
                }""")
            except Exception:
                pass
            page.wait_for_timeout(6000)

            # 轮询视频当前地址，最多 ~15s，直到拿到真实直链
            deadline = time.time() + 15
            current_src = ""
            while time.time() < deadline:
                current_src = page.evaluate("""() => {
                    const v = document.querySelector('video');
                    if (!v) return '';
                    if (v.currentSrc) return v.currentSrc;
                    if (v.src) return v.src;
                    const s = v.querySelector('source');
                    return s ? (s.src || '') : '';
                }""")
                if current_src and ("douyinvod" in current_src or "bytecdn" in current_src
                                    or "amemv" in current_src or current_src.lower().endswith(".mp4")):
                    break
                page.wait_for_timeout(1000)

            title = page.evaluate("""() => {
                for (const s of ['[data-e2e=video-title]','.video-info-title','h1','.title']) {
                    const e=document.querySelector(s); if(e&&e.innerText.trim()) return e.innerText.trim();
                }
                return document.title;
            }""")
            cover = page.evaluate("""() => {
                const v=document.querySelector('video'); let c=v&&v.poster||'';
                const og=document.querySelector('meta[property=\\"og:image\\"]'); if(!c&&og) c=og.getAttribute('content')||'';
                return c;
            }""")
            current_src = page.evaluate("""() => {
                const v=document.querySelector('video');
                return (v&&(v.currentSrc||v.src))||'';
            }""")
            browser.close()
    except Exception as e:
        return {"err": f"浏览器解析失败: {e}"}

    # 从抓包里挑真实视频直链：排除占位，优先带 hvc1/mp4 的 douyinvod
    real = [u for u in captured if "uuu_" not in u]
    video_url = current_src if ("douyinvod" in current_src or "bytecdn" in current_src) else ""
    audio_url = ""

    for u in real:
        if "media-video" in u or "/video/tos/" in u:
            video_url = u
            break
    for u in real:
        if "media-audio" in u or "/audio/" in u:
            audio_url = u
            break
    if not video_url:
        for u in real:
            if u.lower().endswith(".mp4") or "douyinvod" in u or "bytecdn" in u:
                video_url = u
                break

    if not video_url:
        return {"err": "浏览器已渲染页面，但未捕获到视频直链（可能需登录或内容受限）。"}

    return {
        "title": title or "douyin_video",
        "author": "",
        "cover": cover or None,
        "video_url": video_url,
        "audio_url": audio_url or None,
        "video_id": video_id,
        "err": None,
    }


if __name__ == "__main__":
    import json
    test = sys.argv[1] if len(sys.argv) > 1 else "https://www.douyin.com/jingxuan?modal_id=7672693399065005331"
    print(json.dumps(fetch_video_info_browser(test), ensure_ascii=False, indent=2))
