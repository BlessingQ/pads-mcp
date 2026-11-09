"""Capture a window to PNG (works when the window is covered, not when minimized)."""
from __future__ import annotations

import ctypes
import io
import time

import win32con
import win32gui
import win32ui
from PIL import Image

PW_RENDERFULLCONTENT = 2


def capture(hwnd: int, max_width: int = 1600, crop_client: bool = True) -> bytes:
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.6)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    hdc = win32gui.GetWindowDC(hwnd)
    src = win32ui.CreateDCFromHandle(hdc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src, w, h)
    mem.SelectObject(bmp)
    try:
        ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), PW_RENDERFULLCONTENT)
        info = bmp.GetInfo()
        img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                               bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        mem.DeleteDC()
        src.DeleteDC()
        win32gui.ReleaseDC(hwnd, hdc)
    if crop_client:
        cl, ct = win32gui.ClientToScreen(hwnd, (0, 0))
        cr, cb = win32gui.GetClientRect(hwnd)[2:]
        img = img.crop((cl - left, ct - top, cl - left + cr, ct - top + cb))
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
