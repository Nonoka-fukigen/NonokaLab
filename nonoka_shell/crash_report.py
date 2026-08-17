# -*- coding: utf-8 -*-
"""崩溃报告：全局异常捕获时，在写入日志之外额外生成结构化崩溃报告。

报告位置：用户文档/NonokaLab/logs/crash_report.json
内容：错误信息、堆栈、软件版本、操作系统。
隐私：默认**不包含**下载 URL / 视频标题 / 日志正文；仅当用户勾选「包含详细日志」
时才在提交时附加最近日志（见 build_issue_body include_logs）。
不上传任何数据；是否提交由用户在前端弹窗中决定（跳转 GitHub Issues）。
"""
import datetime
import json
import os
import platform
import traceback

from .utils import get_data_dir
from .logger import get_logger
from .brand import VERSION

_log = get_logger("crash")


def recent_log_lines(n=80):
    path = os.path.join(get_data_dir(), "logs", "nonoka.log")
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def build_report(exc_type, exc, tb, include_logs=False):
    report = {
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "version": VERSION,
        "os": platform.platform(),
        "python": platform.python_version(),
        "error": str(exc),
        "type": exc_type.__name__ if exc_type else "",
        "traceback": "".join(traceback.format_exception(exc_type, exc, tb)) if tb else "",
    }
    if include_logs:
        report["recent_log"] = recent_log_lines()
    return report


def write(exc_type, exc, tb, include_logs=False):
    """写入崩溃报告文件（默认不含日志/URL，隐私优先），返回 (path, report)。"""
    report = build_report(exc_type, exc, tb, include_logs=include_logs)
    path = os.path.join(get_data_dir(), "logs", "crash_report.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log.warning("崩溃报告写入失败: %s", e)
        path = None
    return path, report


def build_issue_body(report, include_logs=False):
    """生成 GitHub Issues 正文；仅当 include_logs=True 时附加最近日志（用户主动勾选）。"""
    body = (
        "## 崩溃报告\n"
        f"- 版本：{report.get('version')}\n"
        f"- 系统：{report.get('os')}\n"
        f"- Python：{report.get('python')}\n\n"
        "### 错误信息\n"
        f"{report.get('type')}: {report.get('error')}\n\n"
        "### 堆栈\n"
        f"```\n{report.get('traceback')}\n```"
    )
    if include_logs:
        body += "\n\n### 最近日志\n```\n%s\n```" % recent_log_lines()
    return body


def read_last():
    """读取上一次保存的崩溃报告（用于“提交报告”弹窗）。"""
    path = os.path.join(get_data_dir(), "logs", "crash_report.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
