"""List visible top-level windows (and their child control texts) of a process."""
import sys
import win32gui, win32process

pid = int(sys.argv[1])

def children(h):
    out = []
    def cb(c, _):
        t = win32gui.GetWindowText(c)
        cls = win32gui.GetClassName(c)
        if t:
            out.append(f"      [{cls}] {t[:200]!r}")
        return True
    try:
        win32gui.EnumChildWindows(h, cb, None)
    except Exception:
        pass
    return out

def top(h, _):
    if not win32gui.IsWindowVisible(h):
        return True
    _, p = win32process.GetWindowThreadProcessId(h)
    if p == pid:
        print(f"hwnd={h} [{win32gui.GetClassName(h)}] {win32gui.GetWindowText(h)!r}")
        if win32gui.GetClassName(h) == "#32770" or len(sys.argv) > 2:
            print("\n".join(children(h)[:40]))
    return True

win32gui.EnumWindows(top, None)
