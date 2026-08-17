# -*- coding: utf-8 -*-
"""`python -m nonoka_shell.core [--test]` 命令行入口。"""
import sys

from . import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
