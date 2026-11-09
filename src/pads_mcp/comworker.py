"""Single-threaded COM executor + modal dialog watchdog for PADS applications.

PADS automation servers are apartment-threaded: every COM object must be used
from the thread that created it, and a modal dialog inside PADS blocks the call
until someone dismisses it. All COM work therefore runs on one dedicated STA
thread, and a separate watchdog thread auto-answers known harmless dialogs and
records any others so the MCP client can see (and answer) them.
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import queue
import threading
import time

import pythoncom
import pywintypes
import win32api
import win32con
import win32gui
import win32process

PADS_EXES = {"powerlogic.exe": "logic", "powerpcb.exe": "layout", "blazerouter.exe": "router"}

# dialog title -> button caption to click automatically
AUTO_ANSWERS = {
    "Font Replacement": "OK",
}


class ComWorker:
    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="pads-com", daemon=True)
        self._thread.start()

    def _loop(self):
        pythoncom.CoInitialize()
        while True:
            fut, fn, args, kw = self._q.get()
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(*args, **kw))
            except BaseException as e:  # noqa: BLE001 - forwarded to caller
                fut.set_exception(e)

    def submit(self, fn, *args, **kw) -> cf.Future:
        fut: cf.Future = cf.Future()
        self._q.put((fut, fn, args, kw))
        return fut

    def busy(self) -> bool:
        return not self._q.empty()


def com_error_text(e: BaseException) -> str:
    if isinstance(e, pywintypes.com_error):
        hr, msg, exc, _ = e.args
        detail = ""
        if exc and len(exc) > 2 and exc[2]:
            detail = f": {exc[2]}"
        return f"COM error 0x{hr & 0xFFFFFFFF:08X} {msg}{detail}"
    return f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------- windows
_exe_cache: dict[int, str] = {}


def exe_of_pid(pid: int) -> str:
    if pid in _exe_cache:
        return _exe_cache[pid]
    name = ""
    try:
        h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        try:
            name = os.path.basename(win32process.GetModuleFileNameEx(h, 0)).lower()
        finally:
            win32api.CloseHandle(h)
    except pywintypes.error:
        pass
    _exe_cache[pid] = name
    return name


def pads_windows():
    """Yield (hwnd, app, title, class) for visible top-level PADS windows."""
    out = []

    def cb(h, _):
        if not win32gui.IsWindowVisible(h):
            return True
        _, pid = win32process.GetWindowThreadProcessId(h)
        app = PADS_EXES.get(exe_of_pid(pid))
        if app:
            out.append((h, app, win32gui.GetWindowText(h), win32gui.GetClassName(h)))
        return True

    win32gui.EnumWindows(cb, None)
    return out


def main_window(app: str) -> int | None:
    title_key = {"logic": "PADS Logic", "layout": "PADS Layout", "router": "PADS Router"}[app]
    for h, a, title, cls in pads_windows():
        if a == app and cls != "#32770" and title_key in title:
            return h
    return None


def dialog_info(h: int) -> dict:
    texts, buttons = [], []

    def cb(c, _):
        t = win32gui.GetWindowText(c)
        cls = win32gui.GetClassName(c)
        if t:
            (buttons if cls == "Button" else texts).append(t.replace("&", ""))
        return True

    try:
        win32gui.EnumChildWindows(h, cb, None)
    except pywintypes.error:
        pass
    return {"hwnd": h, "title": win32gui.GetWindowText(h), "texts": texts[:20], "buttons": buttons}


def click_button(h: int, caption: str) -> bool:
    found = []

    def cb(c, _):
        if win32gui.GetClassName(c) == "Button" and win32gui.GetWindowText(c).replace("&", "").lower() == caption.lower():
            found.append(c)
        return True

    win32gui.EnumChildWindows(h, cb, None)
    if found:
        win32gui.PostMessage(found[0], win32con.BM_CLICK, 0, 0)
        return True
    return False


class DialogWatcher:
    """Polls for modal dialogs of PADS processes."""

    def __init__(self, interval: float = 0.4):
        self.interval = interval
        self.history: list[dict] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, name="pads-dialogs", daemon=True).start()

    def current(self) -> list[dict]:
        out = []
        for h, app, title, cls in pads_windows():
            if cls == "#32770":
                d = dialog_info(h)
                d["app"] = app
                out.append(d)
        return out

    def _loop(self):
        seen: set[int] = set()
        while True:
            try:
                for d in self.current():
                    want = AUTO_ANSWERS.get(d["title"])
                    if want and click_button(d["hwnd"], want):
                        if d["hwnd"] not in seen:
                            with self._lock:
                                self.history.append({**d, "auto_clicked": want, "time": time.strftime("%H:%M:%S")})
                            seen.add(d["hwnd"])
            except Exception:  # noqa: BLE001 - never let the watchdog die
                pass
            time.sleep(self.interval)

    def recent_auto(self, since: float = 0) -> list[dict]:
        with self._lock:
            return list(self.history[-20:])
