# -*- coding: utf-8 -*-
"""独立进程的文件夹选择器（由 nonoka_shell.utils._pick_folder_subprocess 调用）。

在 pywebview 的 js_api 线程里直接创建对话框不可靠：该线程属于 .NET 线程池（MTA），
且对话框容易被应用窗口盖住。因此用 subprocess 派生一个独立进程来弹对话框，
结果通过 stdout（utf-8）回传给父进程。

本进程优先使用原生 SHBrowseForFolder（shell32 导出函数，非 COM，几乎必然可用），
其对话框是系统原生模态窗口，天然前置；若失败再回退到 tkinter 方案（含轮询强置顶）。

用法：
    python folderpicker.py [initial_dir]
退出码 0 且 stdout 为所选文件夹路径；取消时 stdout 为空。
"""
import os
import sys


class _ForegroundRaiser:
    """后台线程：在对话框显示期间，把本进程的所有顶层窗口强制置顶+拉到前台。

    避免对话框被 pywebview 全屏/常驻窗口盖住而「看不到」。集成环境里
    SetForegroundWindow 常受限，但 SetWindowPos(HWND_TOPMOST) 不受此限制，
    足以让对话框浮在其它非置顶窗口之上。
    """

    def __init__(self):
        self._stop = False
        self._thread = None

    def start(self):
        try:
            import threading
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        except Exception:
            pass

    def stop(self):
        self._stop = True

    def _run(self):
        import ctypes
        import time
        from ctypes import wintypes
        try:
            user32 = ctypes.windll.user32
        except Exception:
            return
        this_pid = os.getpid()
        HWND_TOPMOST = -1
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        while not self._stop:
            try:
                hwnds = []

                def _cb(hwnd, lparam):
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value == this_pid and user32.IsWindowVisible(hwnd):
                        hwnds.append(hwnd)
                    return True

                CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                user32.EnumWindows(CB(_cb), 0)
                for h in hwnds:
                    try:
                        user32.SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0,
                                            SWP_NOMOVE | SWP_NOSIZE)
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.12)


def _native_pick(title, initial_dir):
    """用 SHBrowseForFolderW 弹原生文件夹选择框，返回路径或空串。"""
    import ctypes
    from ctypes import wintypes, Structure, byref, c_void_p, c_wchar_p

    # SHBrowseForFolder 内部会 CoCreateInstance 系统对话框组件，
    # 必须先在本（主）线程初始化 COM，否则会抛 REGDB_E_CLASSNOTREG 而失败。
    try:
        ctypes.windll.ole32.CoInitialize(None)
    except Exception:
        pass

    BIF_RETURNONLYFSDIRS = 0x0001
    BIF_NEWDIALOGSTYLE = 0x0040
    BIF_EDITBOX = 0x0010
    BFFM_INITIALIZED = 1
    BFFM_SETSELECTION = 0x467  # WM_USER + 103

    class BROWSEINFO(Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", c_void_p),
            ("pszDisplayName", c_void_p),
            ("lpszTitle", c_wchar_p),
            ("ulFlags", ctypes.c_ulong),
            ("lpfn", c_void_p),
            ("lParam", c_void_p),
            ("iImage", ctypes.c_int),
        ]

    CB_FUNC = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, ctypes.c_uint,
                                 ctypes.c_long, ctypes.c_long)
    initial = initial_dir or ""

    @CB_FUNC
    def _cb(hwnd, uMsg, lParam, lpData):
        if uMsg == BFFM_INITIALIZED and initial:
            try:
                ptr = ctypes.c_wchar_p(initial)
                ctypes.windll.user32.SendMessageW(hwnd, BFFM_SETSELECTION, 1, ptr)
            except Exception:
                pass
        return 0

    shell32 = ctypes.windll.shell32
    shell32.SHBrowseForFolder.restype = c_void_p
    shell32.SHBrowseForFolder.argtypes = [ctypes.POINTER(BROWSEINFO)]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    shell32.SHGetPathFromIDListW.argtypes = [c_void_p, wintypes.LPWSTR]

    display = ctypes.create_unicode_buffer(260)
    bi = BROWSEINFO()
    bi.hwndOwner = ctypes.windll.user32.GetActiveWindow()
    bi.pidlRoot = None
    bi.pszDisplayName = ctypes.cast(display, c_void_p)
    bi.lpszTitle = title
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_EDITBOX
    bi.lpfn = ctypes.cast(_cb, c_void_p)
    bi.lParam = None
    bi.iImage = 0

    pidl = shell32.SHBrowseForFolder(byref(bi))
    if not pidl:
        return ""
    try:
        path_buf = ctypes.create_unicode_buffer(260)
        if shell32.SHGetPathFromIDListW(pidl, path_buf):
            return path_buf.value
        return ""
    finally:
        try:
            ctypes.windll.ole32.CoTaskMemFree(pidl)
        except Exception:
            pass


def _tk_pick(title, initial_dir):
    """tkinter 回退方案：轮询把 askdirectory 对话框强置顶+聚焦，确保浮在前面。"""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

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
            root.attributes("-topmost", True)
        except Exception:
            pass
        try:
            root.after(120, _raise_dialog)
        except Exception:
            pass

    try:
        root.update_idletasks()
        root.after(80, _raise_dialog)
        folder = filedialog.askdirectory(parent=root, mustexist=True,
                                         title=title,
                                         initialdir=initial_dir or "")
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    return folder or ""


def main(argv):
    title = "选择保存文件夹"
    initial_dir = argv[1] if len(argv) > 1 else ""
    folder = ""
    # 无论原生还是 tkinter，都启动强置顶轮询：把本进程所有顶层窗口（含对话框）
    # 拉到最前，避免被 pywebview 全屏/常驻窗口盖住而「看不到」。
    raiser = _ForegroundRaiser()
    raiser.start()
    try:
        if sys.platform == "win32":
            try:
                folder = _native_pick(title, initial_dir)
            except Exception:
                folder = ""
        if not folder:
            try:
                folder = _tk_pick(title, initial_dir)
            except Exception:
                folder = ""
    finally:
        raiser.stop()

    # 结果以 utf-8 字节写回 stdout，规避 Windows 控制台默认 GBK 编码
    out = (folder or "").replace("\r", " ").replace("\n", " ")
    try:
        sys.stdout.buffer.write((out + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
    except Exception:
        pass


if __name__ == "__main__":
    main(sys.argv)