# -*- coding: utf-8 -*-
"""本地构建脚本：用 PyInstaller 将 Nonoka Lab 打包为单文件 exe。

用法：
    python build.py              # 生成 dist/NonokaLab.exe（windowed）
    python build.py --console    # 保留控制台（调试用）
    python build.py --name X     # 自定义产物名

前置：pip install -r requirements.txt pyinstaller
产物：dist/NonokaLab.exe （安装版由 GitHub Actions + Inno Setup 生成）
"""
import os
import sys

# 强制 Python 以 UTF-8 处理 stdin/stdout，避免 CI（cp1252 代码页）下打印中文崩溃。
# 相比直接重包 sys.stdout，PYTHONIOENCODING 更可靠，且在本地 / Actions 均生效。
if sys.version_info >= (3, 7):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import PyInstaller.__main__ as pyi

REPO = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(REPO, "run_app.py")

FRONTEND = os.path.join(REPO, "frontend")
PLUGINS_DIR = os.path.join(REPO, "plugins")


def main():
    console = "--console" in sys.argv
    name = "NonokaLab"
    if "--name" in sys.argv:
        i = sys.argv.index("--name")
        if len(sys.argv) > i + 1:
            name = sys.argv[i + 1]

    datas = [
        (FRONTEND, "frontend"),
        (PLUGINS_DIR, "plugins"),
    ]
    hidden = [
        "webview", "webview.platforms.winforms",
        "requests", "PIL", "cv2", "numpy", "qrcode", "playwright",
        "playwright.sync_api",
    ]

    opts = [
        ENTRY,
        "--name", name,
        "--onefile",
        "--windowed" if not console else "--console",
        "--noconfirm",
        "--clean",
        "--paths", REPO,
    ]
    for src, dst in datas:
        if os.path.isdir(src):
            opts += ["--add-data", f"{src}{os.pathsep}{dst}"]
    for h in hidden:
        opts += ["--hidden-import", h]

    # 调试：打印最终参数
    print("[build.py] PyInstaller 参数：")
    print("  " + " ".join(opts))

    pyi.run(opts)


if __name__ == "__main__":
    main()
