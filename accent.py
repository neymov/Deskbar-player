"""Resolve a stable accent from the icon of the app owning a GSMTC session."""
from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from pathlib import Path

from PIL import Image

NEUTRAL = (149, 190, 170)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _session_process(app_id: str) -> str | None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snap = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(snap, ctypes.byref(entry)):
            return None
        wanted = app_id.casefold()
        tokens = [s for s in re.split(r"[^a-z0-9]+", wanted.split("!")[0]) if len(s) >= 4]
        fallback = None
        while True:
            process = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
            if process:
                try:
                    size = wintypes.UINT(0)
                    fn = kernel.GetApplicationUserModelId
                    fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
                    code = fn(process, ctypes.byref(size), None)
                    if size.value:
                        value = ctypes.create_unicode_buffer(size.value)
                        if fn(process, ctypes.byref(size), value) == 0 and value.value.casefold() == wanted:
                            length = wintypes.DWORD(32768)
                            exe = ctypes.create_unicode_buffer(length.value)
                            if kernel.QueryFullProcessImageNameW(process, 0, exe, ctypes.byref(length)):
                                return exe.value
                    process_name = entry.szExeFile.casefold()
                    if any(token in Path(process_name).stem for token in tokens):
                        length = wintypes.DWORD(32768)
                        exe = ctypes.create_unicode_buffer(length.value)
                        if kernel.QueryFullProcessImageNameW(process, 0, exe, ctypes.byref(length)):
                            fallback = fallback or exe.value
                except Exception:
                    pass
                finally:
                    kernel.CloseHandle(process)
            if not kernel.Process32NextW(snap, ctypes.byref(entry)):
                break
        return fallback
    finally:
        kernel.CloseHandle(snap)
    return None


def _load_exe_icon(path: str) -> Image.Image | None:
    shell, user, gdi = (ctypes.WinDLL("shell32", use_last_error=True),
                         ctypes.WinDLL("user32", use_last_error=True),
                         ctypes.WinDLL("gdi32", use_last_error=True))
    large, small = wintypes.HANDLE(), wintypes.HANDLE()
    shell.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int,
        ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), wintypes.UINT]
    shell.ExtractIconExW.restype = wintypes.UINT
    if shell.ExtractIconExW(path, 0, ctypes.byref(large), ctypes.byref(small), 1) == 0:
        return None
    icon = large.value or small.value
    if not icon:
        return None
    user.GetDC.argtypes = [wintypes.HWND]
    user.GetDC.restype = wintypes.HDC
    user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user.ReleaseDC.restype = ctypes.c_int
    user.DestroyIcon.argtypes = [wintypes.HANDLE]
    user.DestroyIcon.restype = wintypes.BOOL
    user.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HANDLE,
        ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
    user.DrawIconEx.restype = wintypes.BOOL
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi.CreateDIBSection.restype = wintypes.HBITMAP
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi.SelectObject.restype = wintypes.HGDIOBJ
    gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    dc = user.GetDC(None)
    memdc = gdi.CreateCompatibleDC(dc)
    info = BITMAPINFO()
    info.bmiHeader = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), 32, -32, 1, 32, 0, 4096, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    bitmap = gdi.CreateDIBSection(dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
    old = gdi.SelectObject(memdc, bitmap)
    try:
        user.DrawIconEx(memdc, 0, 0, icon, 32, 32, 0, None, 0x0003)
        raw = ctypes.string_at(bits, 32 * 32 * 4)
        return Image.frombytes("RGBA", (32, 32), raw, "raw", "BGRA")
    finally:
        gdi.SelectObject(memdc, old)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(memdc)
        user.ReleaseDC(None, dc)
        user.DestroyIcon(large.value) if large.value else None
        user.DestroyIcon(small.value) if small.value else None


def _dominant(image: Image.Image) -> tuple[int, int, int]:
    pixels = image.convert("RGBA").resize((28, 28)).getdata()
    bins: dict[tuple[int, int, int], float] = {}
    for r, g, b, a in pixels:
        if a < 80 or max(r, g, b) < 38 or min(r, g, b) > 240:
            continue
        high, low = max(r, g, b), min(r, g, b)
        saturation = (high - low) / max(high, 1)
        if saturation < .17:
            continue
        key = (round(r / 24) * 24, round(g / 24) * 24, round(b / 24) * 24)
        bins[key] = bins.get(key, 0) + .45 + saturation
    if not bins:
        return NEUTRAL
    color = max(bins.items(), key=lambda item: item[1])[0]
    return tuple(max(0, min(255, int(v))) for v in color)


def app_accent(app_id: str) -> tuple[int, int, int]:
    if not app_id:
        return NEUTRAL
    try:
        path = _session_process(app_id)
        if path and Path(path).is_file():
            icon = _load_exe_icon(path)
            if icon:
                return _dominant(icon)
    except Exception:
        pass
    return NEUTRAL

