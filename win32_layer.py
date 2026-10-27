"""Per-pixel-alpha Windows surface used to let wallpaper show through DeskBar."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from PIL import Image, ImageChops


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class LayeredSurface:
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    ULW_ALPHA = 0x00000002
    AC_SRC_OVER = 0
    AC_SRC_ALPHA = 1
    BI_RGB = 0
    DIB_RGB_COLORS = 0
    GA_ROOT = 2
    LWA_COLORKEY = 0x00000001
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOZORDER = 0x0004
    SWP_FRAMECHANGED = 0x0020

    def __init__(self, hwnd: int, width: int, height: int, x: int, y: int,
                 expose_in_taskbar: bool = False) -> None:
        self.hwnd, self.width, self.height = hwnd, width, height
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user.GetAncestor.restype = wintypes.HWND
        self.user.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF,
            wintypes.BYTE, wintypes.DWORD]
        self.user.SetLayeredWindowAttributes.restype = wintypes.BOOL
        self.user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user.SetWindowPos.restype = wintypes.BOOL
        self.surface_hwnd = hwnd
        self.host_hwnd = self.user.GetAncestor(hwnd, self.GA_ROOT) or hwnd
        self.user.GetDC.argtypes = [wintypes.HWND]
        self.user.GetDC.restype = wintypes.HDC
        self.user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        self.user.ReleaseDC.restype = ctypes.c_int
        self.user.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC,
            ctypes.POINTER(POINT), ctypes.POINTER(SIZE), wintypes.HDC,
            ctypes.POINTER(POINT), wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
        self.user.UpdateLayeredWindow.restype = wintypes.BOOL
        self.gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self.gdi.CreateCompatibleDC.restype = wintypes.HDC
        self.gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO),
            wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
        self.gdi.CreateDIBSection.restype = wintypes.HBITMAP
        self.gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        self.gdi.SelectObject.restype = wintypes.HGDIOBJ
        self.gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        self.gdi.DeleteDC.argtypes = [wintypes.HDC]
        # Tk creates a client child HWND under a separate top-level wrapper. The
        # child supplies per-pixel alpha; the wrapper's color key lets fully
        # transparent child pixels reveal the desktop instead of a black box.
        self._set_layered_style(self.surface_hwnd)
        if self.host_hwnd != self.surface_hwnd:
            self._set_layered_style(self.host_hwnd)
            if expose_in_taskbar:
                self._set_taskbar_style(self.host_hwnd)
            color_key = 0x030201  # RGB(1, 2, 3), matching the Tk host background.
            if not self.user.SetLayeredWindowAttributes(self.host_hwnd, color_key, 255,
                    self.LWA_COLORKEY):
                raise ctypes.WinError(ctypes.get_last_error())
        self.screen_dc = self.user.GetDC(None)
        self.memory_dc = self.gdi.CreateCompatibleDC(self.screen_dc)
        info = BITMAPINFO()
        info.bmiHeader = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height,
            1, 32, self.BI_RGB, width * height * 4, 0, 0, 0, 0)
        self.bits = ctypes.c_void_p()
        self.bitmap = self.gdi.CreateDIBSection(self.screen_dc, ctypes.byref(info),
            self.DIB_RGB_COLORS, ctypes.byref(self.bits), None, 0)
        if not self.bitmap or not self.bits:
            raise ctypes.WinError(ctypes.get_last_error())
        self.old_bitmap = self.gdi.SelectObject(self.memory_dc, self.bitmap)
        # UpdateLayeredWindow takes child-window positions in parent-client
        # coordinates. This Tk surface is a child filling the whole host.
        self.dest = POINT(0, 0) if self.host_hwnd != self.surface_hwnd else POINT(x, y)
        self.size = SIZE(width, height)
        self.source = POINT(0, 0)
        self.blend = BLENDFUNCTION(self.AC_SRC_OVER, 0, 255, self.AC_SRC_ALPHA)

    def _set_layered_style(self, hwnd: int) -> None:
        get_style = self.user.GetWindowLongPtrW
        set_style = self.user.SetWindowLongPtrW
        get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        get_style.restype = ctypes.c_ssize_t
        set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_style.restype = ctypes.c_ssize_t
        style = get_style(hwnd, self.GWL_EXSTYLE)
        if not (style & self.WS_EX_LAYERED):
            ctypes.set_last_error(0)
            set_style(hwnd, self.GWL_EXSTYLE, style | self.WS_EX_LAYERED)
            if ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.user.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                    self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOZORDER | self.SWP_FRAMECHANGED):
                raise ctypes.WinError(ctypes.get_last_error())

    def _set_taskbar_style(self, hwnd: int) -> None:
        get_style = self.user.GetWindowLongPtrW
        set_style = self.user.SetWindowLongPtrW
        get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        get_style.restype = ctypes.c_ssize_t
        set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_style.restype = ctypes.c_ssize_t
        style = get_style(hwnd, self.GWL_EXSTYLE)
        ctypes.set_last_error(0)
        set_style(hwnd, self.GWL_EXSTYLE, (style & ~0x00000080) | 0x00040000)
        if ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.user.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOZORDER | self.SWP_FRAMECHANGED):
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _premultiplied_bgra(image: Image.Image) -> bytes:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        red, green, blue, _ = rgba.split()
        red = ImageChops.multiply(red, alpha)
        green = ImageChops.multiply(green, alpha)
        blue = ImageChops.multiply(blue, alpha)
        return Image.merge("RGBA", (blue, green, red, alpha)).tobytes()

    def present(self, image: Image.Image, x: int, y: int) -> None:
        if image.size != (self.width, self.height):
            raise ValueError("Layered surface dimensions changed")
        data = self._premultiplied_bgra(image)
        ctypes.memmove(self.bits, data, len(data))
        if self.host_hwnd == self.surface_hwnd:
            self.dest.x, self.dest.y = x, y
        if not self.user.UpdateLayeredWindow(self.surface_hwnd, self.screen_dc, ctypes.byref(self.dest),
                ctypes.byref(self.size), self.memory_dc, ctypes.byref(self.source),
                0, ctypes.byref(self.blend), self.ULW_ALPHA):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if getattr(self, "memory_dc", None):
            self.gdi.SelectObject(self.memory_dc, self.old_bitmap)
            self.gdi.DeleteObject(self.bitmap)
            self.gdi.DeleteDC(self.memory_dc)
            self.memory_dc = None
        if getattr(self, "screen_dc", None):
            self.user.ReleaseDC(None, self.screen_dc)
            self.screen_dc = None

