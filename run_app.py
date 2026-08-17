# -*- coding: utf-8 -*-
"""Nonoka Lab 统一入口（仓库根目录）。

PyInstaller / 命令行均以此为入口：保证 nonoka_shell 以包形式被正确导入。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from nonoka_shell.main import main

if __name__ == "__main__":
    main()
