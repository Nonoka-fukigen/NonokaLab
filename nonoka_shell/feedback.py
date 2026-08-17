# -*- coding: utf-8 -*-
"""崩溃上报：不搭建服务器，直接跳转 GitHub Issues 创建页面并预填内容。

标题：【崩溃报告】自动上报
内容：崩溃报告正文
"""
import webbrowser
from urllib.parse import urlencode

from .brand import REPO_URL
from .logger import get_logger

_log = get_logger("feedback")


def build_issue_url(title, body):
    base = REPO_URL.rstrip("/") + "/issues/new"
    return base + "?" + urlencode({"title": title, "body": body})


def open_crash_issue(body):
    """崩溃上报：直接打开带预填内容的 Issues 页面。"""
    url = build_issue_url("【崩溃报告】自动上报", body)
    try:
        webbrowser.open(url)
    except Exception as e:
        _log.warning("打开崩溃上报页失败: %s", e)
    return url
