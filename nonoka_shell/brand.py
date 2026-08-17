# -*- coding: utf-8 -*-
"""Nonoka Lab —— 品牌与全局常量（集中管理，便于统一换肤）。"""

APP_NAME = "Nonoka Lab"
APP_ID = "com.nonoka.lab"
PUBLISHER = "Nonoka"
VERSION = "1.0.1"  # 正式发布前统一为 1.0.1

# 主题色（全局唯一强调色）
THEME_PRIMARY = "#ffbbcc"
THEME_PRIMARY_DARK = "#f7a8c0"
THEME_PRIMARY_SOFT = "#ffe6ee"

# 窗口
WINDOW_TITLE = "Nonoka Lab"
WINDOW_WIDTH = 1100
WINDOW_HEIGHT = 740
WINDOW_MIN_WIDTH = 920
WINDOW_MIN_HEIGHT = 620

# 数据目录（位于 用户文档/NonokaLab）
DATA_DIR_NAME = "NonokaLab"
CONFIG_FILE = "config.json"
LOG_FILE = "nonoka.log"
LOG_DIR = "logs"
COMPONENTS_DIR = "components"

# 更新 / 仓库
REPO_URL = "https://github.com/Nonoka-fukigen/NonokaLab"
# 留空则不做在线更新检查；可填 GitHub Releases 的 latest API
UPDATE_API = "https://api.github.com/repos/Nonoka-fukigen/NonokaLab/releases/latest"
# 插件市场清单（远程 JSON，列出可用插件元数据）；可在 config 中覆盖
MARKET_URL = "https://raw.githubusercontent.com/Nonoka-fukigen/NonokaLab/main/market/plugins.json"
