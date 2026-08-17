# -*- coding: utf-8 -*-
"""多语言管理（后端）。

语言文件位于 frontend/locales/{zh,en}.json（键名统一）。
壳子与插件前端从语言文件读取文案；后端返回的错误信息也通过 t() 取对应语言。
当前语言持久化由调用方（bridge/config）负责，本模块仅负责加载与翻译。
"""
import json
import os

from .logger import get_logger

_log = get_logger("i18n")

LOCALES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "locales"
)
SUPPORTED = ["zh", "en"]
DEFAULT = "zh"

_cache = {}
_current = None


def _load(locale):
    if locale in _cache:
        return _cache[locale]
    path = os.path.join(LOCALES_DIR, locale + ".json")
    data = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception as e:
        _log.warning("加载语言文件失败 %s: %s", locale, e)
    _cache[locale] = data
    return data


def set_locale(locale):
    global _current
    if locale in SUPPORTED:
        _current = locale
    elif locale:
        # 未知语言回退默认，但保留用户选择以便前端提示
        _current = DEFAULT
    return _current or DEFAULT


def get_locale():
    return _current or DEFAULT


def is_supported(locale):
    return locale in SUPPORTED


def t(key, **kwargs):
    """翻译键；当前语言缺失时回退到默认语言，仍缺失则返回原键。"""
    data = _load(get_locale())
    s = data.get(key)
    if s is None:
        data = _load(DEFAULT)
        s = data.get(key, key)
    if kwargs:
        try:
            s = s.format(**kwargs)
        except Exception:
            pass
    return s


def all_locales():
    """返回 {locale: dict}，供前端一次性加载文案。"""
    return {l: _load(l) for l in SUPPORTED}


def locale_list():
    """返回可被前端渲染的语言清单。"""
    return [{"code": l, "name": _load(l).get("__lang_name", l)} for l in SUPPORTED]
