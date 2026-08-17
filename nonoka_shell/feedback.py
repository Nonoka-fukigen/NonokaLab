# -*- coding: utf-8 -*-
"""用户反馈：不搭建服务器，直接跳转 GitHub Issues 创建页面并预填内容。

标题：【用户反馈】+ 简短描述
内容：问题描述、软件版本、操作系统信息、日志内容（可选）
"""
import platform
import webbrowser
from urllib.parse import urlencode

from .brand import REPO_URL, VERSION
from .logger import get_logger

_log = get_logger("feedback")


def build_issue_url(title, body):
    base = REPO_URL.rstrip("/") + "/issues/new"
    return base + "?" + urlencode({"title": title, "body": body})


def build_body(description, include_log=False, log_text=""):
    """默认不含日志（隐私：下载记录与链接永不自动上传）；用户勾选才包含。"""
    body = "### 描述\n" + (description or "(空)") + "\n\n"
    body += "### 环境\n"
    body += f"- 版本：{VERSION}\n"
    body += f"- 系统：{platform.platform()}\n"
    if include_log and log_text:
        body += f"\n### 日志\n```\n{log_text}\n```"
    return body


def open_feedback(summary, description, include_log=False, log_text=""):
    """打开浏览器跳转到 GitHub Issues 创建页面，返回目标 URL。"""
    title = "【用户反馈】" + (summary or (description or "反馈"))[:40]
    body = build_body(description, include_log=include_log, log_text=log_text)
    url = build_issue_url(title, body)
    try:
        webbrowser.open(url)
    except Exception as e:
        _log.warning("打开反馈页失败: %s", e)
    return url


def open_crash_issue(body):
    """崩溃上报：直接打开带预填内容的 Issues 页面。"""
    url = build_issue_url("【崩溃报告】自动上报", body)
    try:
        webbrowser.open(url)
    except Exception as e:
        _log.warning("打开崩溃上报页失败: %s", e)
    return url
