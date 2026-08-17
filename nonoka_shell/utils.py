# -*- coding: utf-8 -*-
"""通用工具函数。"""
import os
import subprocess
import sys
import threading

from .brand import DATA_DIR_NAME


def get_data_dir():
    """返回数据目录：用户文档/NonokaLab（自动创建）。"""
    base = os.path.join(os.path.expanduser("~"), "Documents", DATA_DIR_NAME)
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        # 回退：程序同级
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "..", DATA_DIR_NAME)
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            pass
    return os.path.normpath(base)


def get_components_dir():
    from .brand import COMPONENTS_DIR
    d = os.path.join(get_data_dir(), COMPONENTS_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def open_folder(path):
    """在系统资源管理器中打开指定文件夹"""
    if not path or not os.path.exists(path):
        return False
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=True)
        else:
            subprocess.run(["xdg-open", path], check=True)
        return True
    except Exception:
        return False


def pick_folder(initial_dir=None):
    """
    弹出系统原生文件夹选择对话框。
    通过 subprocess 调用独立的 folderpicker.py 脚本，避免 pywebview 线程限制。
    返回：用户选择的文件夹路径，取消时返回 None。
    """
    # 定位 folderpicker.py 的路径
    current_dir = os.path.dirname(os.path.abspath(__file__))
    picker_script = os.path.join(current_dir, "folderpicker.py")

    if not os.path.exists(picker_script):
        # 如果找不到，降级到 tkinter
        return _pick_folder_tkinter(initial_dir)

    try:
        cmd = [sys.executable, picker_script]
        if initial_dir:
            cmd.append(initial_dir)

        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        else:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)

        stdout, _ = proc.communicate(timeout=120)
        out = stdout.decode("utf-8", errors="ignore").strip()

        if out and os.path.isdir(out):
            return out
        return None

    except Exception:
        return _pick_folder_tkinter(initial_dir)


def _pick_folder_tkinter(initial_dir=None):
    """纯 tkinter 回退方案"""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass

        # 保留既有修复：已 withdraw 的根窗口 -topmost 不会传导到 askdirectory 创建的子
        # 对话框，导致对话框在 pywebview 全屏窗口后面弹出。这里轮询把对话框 Toplevel
        # 顶置+聚焦；askdirectory 内部运行 Tk 事件循环，after 回调可在此间执行。
        def _raise_dialog():
            try:
                for w in root.winfo_children():
                    if isinstance(w, tk.Toplevel):
                        try:
                            w.attributes("-topmost", True)
                            w.lift()
                            w.focus_force()
                        except Exception:
                            pass
            except Exception:
                pass
            try:
                root.after(150, _raise_dialog)
            except Exception:
                pass

        try:
            root.update_idletasks()
            root.after(80, _raise_dialog)
            folder = filedialog.askdirectory(
                parent=root,
                title="选择保存文件夹",
                initialdir=initial_dir or "",
                mustexist=True
            )
        finally:
            try:
                root.destroy()
            except Exception:
                pass
        return folder if folder else None
    except Exception:
        return None


def pick_file(title="选择文件", exts=None):
    """弹出文件选择对话框，返回所选文件路径（取消返回空字符串）。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        ftyp = [("备份文件", "*.zip")] if exts is None else exts
        path = filedialog.askopenfilename(title=title or "选择文件", filetypes=ftyp)
        root.destroy()
        return path or ""
    except Exception:
        return ""


def format_bytes(n):
    try:
        n = int(n)
    except Exception:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    if i == 0:
        return f"{n} B"
    return f"{n:.1f} {units[i]}"


def run_in_thread(fn, *args, **kwargs):
    """在守护线程里执行 fn，避免阻塞 UI。"""
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    t.start()
    return t


def safe_json(obj):
    """把不可序列化对象尽量转成基本类型。"""
    import json
    try:
        json.dumps(obj)
        return obj
    except Exception:
        return str(obj)
