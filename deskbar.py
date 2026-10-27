from __future__ import annotations

import ctypes
from ctypes import wintypes
import io
import os
import queue
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import winreg

from PIL import Image, ImageDraw, ImageFont, ImageTk
import pystray

from accent import app_accent, NEUTRAL
from media_engine import ExternalSessions, MediaSnapshot
from win32_layer import LayeredSurface
from external_volume import get_app_volume, set_app_volume

WIDTH, HEIGHT = 521, 524
PANEL = (12, 14, 18)
VOLUME_BLUE = (74, 145, 255)
REFERENCE_COVER = Path(__file__).parent / "assets" / "reference_cover.png"
REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_NAME = "DeskBar"


def _blend(a: tuple[int, int, int], b: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(round(x * amount + y * (1 - amount)) for x, y in zip(a, b))


def _spring_step(value: float, velocity: float, target: float, dt: float,
                 stiffness: float = 245.0, damping: float = 20.0) -> tuple[float, float]:
    """Stable, lightly under-damped spring step for short jelly-like UI motion."""
    remaining = min(.06, max(0.0, dt))
    while remaining > 0.0:
        step = min(remaining, 1.0 / 120.0)
        acceleration = (target - value) * stiffness - velocity * damping
        velocity += acceleration * step
        value += velocity * step
        remaining -= step
    if abs(target - value) < .0007 and abs(velocity) < .008:
        return target, 0.0
    return value, velocity


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
                  r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf"]
    for item in candidates:
        try:
            return ImageFont.truetype(item, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _rounded_cover(raw: bytes | None, fallback: Image.Image) -> Image.Image:
    try:
        source = Image.open(io.BytesIO(raw)).convert("RGB") if raw else fallback.copy()
    except Exception:
        source = fallback.copy()
    side = min(source.size)
    left, top = (source.width - side) // 2, (source.height - side) // 2
    source = source.crop((left, top, left + side, top + side)).resize((208, 208), Image.Resampling.LANCZOS)
    mask = Image.new("L", (208, 208), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, 207, 207), radius=27, fill=255)
    image = Image.new("RGBA", (208, 208), (0, 0, 0, 0))
    image.paste(source, (0, 0), mask)
    return image


class DiagnosticSurface:
    """Non-layered preview surface so Windows UI automation can inspect test runs."""
    def __init__(self, root: tk.Tk, hwnd: int, width: int, height: int) -> None:
        self.root = root
        self.host_hwnd = ctypes.WinDLL("user32", use_last_error=True).GetAncestor(hwnd, 2) or hwnd
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        self.user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self.user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        self.user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user.SetWindowPos.restype = wintypes.BOOL
        style = self.user.GetWindowLongPtrW(self.host_hwnd, -20)
        self.user.SetWindowLongPtrW(self.host_hwnd, -20, (style & ~0x80) | 0x40000)
        self.user.SetWindowPos(self.host_hwnd, None, 0, 0, 0, 0, 0x27)
        self.canvas = tk.Canvas(root, width=width, height=height, bd=0,
                                highlightthickness=0, bg="#010203")
        self.canvas.place(x=0, y=0, width=width, height=height)
        self.item = self.canvas.create_image(0, 0, anchor="nw")
        self.image = None

    def present(self, image: Image.Image, _x: int, _y: int) -> None:
        self.image = ImageTk.PhotoImage(image.convert("RGB"), master=self.canvas)
        self.canvas.itemconfigure(self.item, image=self.image)

    def close(self) -> None:
        try:
            self.canvas.destroy()
        except tk.TclError:
            pass


class DeskBar:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("DeskBar")
        self.root.geometry(f"{WIDTH}x{HEIGHT}+150+100")
        self.root.resizable(False, False)
        self.root.configure(bg="#010203")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", False)
        self.root.update_idletasks()
        if "--ui-test" in sys.argv:
            self.renderer = DiagnosticSurface(self.root, self.root.winfo_id(), WIDTH, HEIGHT)
        else:
            self.renderer = LayeredSurface(self.root.winfo_id(), WIDTH, HEIGHT,
                                            self.root.winfo_x(), self.root.winfo_y())
        self.fallback_cover = Image.open(REFERENCE_COVER).convert("RGB")
        self.cover_data: bytes | None = None
        self.external = ExternalSessions()
        self.external_state = MediaSnapshot()
        self.accent = NEUTRAL
        self._accent_from = self.accent
        self._accent_target = self.accent
        self._accent_started = 0.0
        self._playback_animation = 0.0
        self._last_playing_visual = False
        self._press: str | None = None
        self._hover: str | None = None
        self._button_scale = {name: 1.0 for name in ("previous", "toggle", "next")}
        self._button_velocity = {name: 0.0 for name in ("previous", "toggle", "next")}
        self._button_press_until = {name: 0.0 for name in ("previous", "toggle", "next")}
        self._hover_amount = {name: 0.0 for name in ("previous", "toggle", "next")}
        self._hover_velocity = {name: 0.0 for name in ("previous", "toggle", "next")}
        self._button_base_center = {"previous": (143.0, 399.0),
                                    "toggle": (253.0, 391.0),
                                    "next": (363.0, 399.0)}
        self._button_center = {name: tuple(center) for name, center in self._button_base_center.items()}
        self._button_center_velocity = {name: (0.0, 0.0) for name in self._button_base_center}
        self._hover_volume = False
        self._dragging_volume = False
        self._dragging_seek = False
        self._volume = 0.5
        self._volume_visual = self._volume
        self._volume_velocity = 0.0
        self._last_frame_perf = time.perf_counter()
        self._last_toggle_at = 0.0
        self._seek_position = 0.0
        self._seek_duration = 0.0
        self._seek_available = False
        self._accent_future = None
        self._external_volume_future = None
        self._volume_set_future = None
        self._pending_external_volume: float | None = None
        self._external_volume_supported = False
        self._next_volume_check = 0.0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="DeskBar-tags")
        self._last_app_id = ""
        self._status_message = ""
        self._status_until = 0.0
        self._configure_redraw_after = None
        self._closing = False
        self.tray = self._create_tray()
        self.tray.run_detached()
        self._bind_events()
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.after(100, self._poll)
        self._schedule_frame()

    def _create_tray(self) -> pystray.Icon:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((2, 2, 62, 62), fill=(54, 130, 103, 255))
        draw.rounded_rectangle((20, 17, 28, 47), radius=4, fill="white")
        draw.rounded_rectangle((36, 17, 44, 47), radius=4, fill="white")
        menu = pystray.Menu(
            pystray.MenuItem("Открыть", lambda *_: self._tray_call(self.show), default=True),
            pystray.MenuItem("Запуск вместе с Windows", lambda *_: self._tray_call(self.toggle_startup), checked=lambda _: self.startup_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda *_: self._tray_call(self.quit)),
        )
        return pystray.Icon("DeskBar", image, "DeskBar", menu)

    def _tray_call(self, callback) -> None:
        try:
            self.root.after(0, callback)
        except tk.TclError:
            pass

    def _bind_events(self) -> None:
        self.root.bind("<Configure>", self._on_configure)
        self.root.bind("<Motion>", self._on_motion)
        self.root.bind("<Leave>", self._on_leave)
        self.root.bind("<ButtonPress-1>", self._on_press)
        self.root.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<B1-Motion>", self._on_drag)
        self.root.bind("<Button-3>", self._popup_menu)
        self.root.bind("<Map>", lambda _e: self._schedule_frame())
        self.root.bind("<Left>", lambda _e: self.previous())
        self.root.bind("<Right>", lambda _e: self.next())
        self.root.bind("<space>", lambda _e: self.toggle())
        self.root.bind("<Escape>", lambda _e: self.hide())
        self.root.bind("<Alt-F4>", lambda _e: self.hide())

    def _on_configure(self, _event) -> None:
        # Avoid repainting on every intermediate position while Windows moves
        # the window. The child surface follows its parent; redraw once movement settles.
        if self._configure_redraw_after is not None:
            try:
                self.root.after_cancel(self._configure_redraw_after)
            except tk.TclError:
                pass
        self._configure_redraw_after = self.root.after(90, self._configured_redraw)

    def _configured_redraw(self) -> None:
        self._configure_redraw_after = None
        self._schedule_frame()

    def _popup_menu(self, event) -> None:
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="Скрыть в трей", command=self.hide)
        menu.add_separator()
        menu.add_command(label="Выход", command=self.quit)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_motion(self, event) -> None:
        if self._dragging_volume or self._dragging_seek:
            return
        new = self._hit_control(event.x, event.y)
        on_volume = 84 <= event.x <= 420 and 464 <= event.y <= 510
        if new != self._hover or on_volume != self._hover_volume:
            self._hover = new
            self._hover_volume = on_volume
            self._schedule_frame()

    def _on_leave(self, _event) -> None:
        self._hover = None
        self._hover_volume = False
        self._schedule_frame()

    def _on_press(self, event) -> None:
        if self._hit_control(event.x, event.y) in {"previous", "toggle", "next"}:
            key = self._hit_control(event.x, event.y)
            self._press = key
            self._button_press_until[key] = time.perf_counter() + .065
            if key == "previous":
                self.previous()
            elif key == "toggle":
                self.toggle()
            else:
                self.next()
            self._schedule_frame()
            return
        if self._seek_available and 306 <= event.y <= 323 and 94 <= event.x <= 427:
            self._dragging_seek = True
            self._seek_to_x(event.x)
            return
        if 84 <= event.x <= 420 and 464 <= event.y <= 510:
            if self._is_external_active() and self._external_volume_supported:
                self._dragging_volume = True
                self._set_volume_from_x(event.x)
            else:
                self._message("Нет доступной громкости медиасеанса Windows")
            return
        # Use the native caption-move loop instead of changing Tk geometry on
        # every pointer event; that avoids jitter and stale layered-window frames.
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.ReleaseCapture.argtypes = []
        user32.ReleaseCapture.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        if user32.ReleaseCapture():
            user32.PostMessageW(self.renderer.host_hwnd, 0x00A1, 2, 0)  # WM_NCLBUTTONDOWN, HTCAPTION
        self._schedule_frame()

    def _on_release(self, _event) -> None:
        self._press = None
        self._dragging_volume = False
        self._dragging_seek = False
        self._schedule_frame()

    def _on_drag(self, event) -> None:
        if self._dragging_volume:
            self._set_volume_from_x(event.x)
        elif self._dragging_seek:
            self._seek_to_x(event.x)

    def _hit_control(self, x: int, y: int) -> str | None:
        if (106 - 36 <= x <= 180 and 362 <= y <= 436):
            return "previous"
        if 187 <= x <= 319 and 326 <= y <= 454:
            return "toggle"
        if 326 <= x <= 400 and 362 <= y <= 436:
            return "next"
        return None

    def _set_volume_from_x(self, x: int) -> None:
        self._volume = min(1.0, max(0.0, (x - 84) / 336))
        try:
            if not self._is_external_active() or not self._external_volume_supported:
                self._message("Нет доступной громкости медиасеанса Windows")
                return
            if self._volume_set_future and not self._volume_set_future.done():
                self._pending_external_volume = self._volume
            else:
                self._volume_set_future = self._executor.submit(set_app_volume, self._last_app_id, self._volume)
        except Exception as exc:
            self._message(str(exc))
        self._schedule_frame()

    def _seek_to_x(self, x: int) -> None:
        if self._seek_duration <= 0:
            return
        self._seek_position = min(1.0, max(0.0, (x - 97) / 330)) * self._seek_duration
        try:
            self.external.command(f"seek:{self._seek_position}")
        except Exception:
            pass
        self._schedule_frame()

    def _message(self, text: str) -> None:
        self._status_message = text
        self._status_until = self.root.tk.call("clock", "milliseconds") / 1000 + 2.5

    def show(self, *_args) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        try:
            self.root.update_idletasks()
        except tk.TclError:
            pass
        self._on_configure(None)
        self._schedule_frame()

    def hide(self, *_args) -> None:
        self.root.withdraw()

    def startup_enabled(self) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, REG_NAME)
            return True
        except OSError:
            return False

    def toggle_startup(self, *_args) -> None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
                if self.startup_enabled():
                    winreg.DeleteValue(key, REG_NAME)
                else:
                    if getattr(sys, "frozen", False):
                        command = f'"{sys.executable}"'
                    else:
                        command = f'"{sys.executable}" "{Path(__file__).resolve()}"'
                    winreg.SetValueEx(key, REG_NAME, 0, winreg.REG_SZ, command)
        except OSError as exc:
            self._message(f"Не удалось изменить автозапуск: {exc}")

    def previous(self) -> None:
        if self._is_external_active():
            self.external.command("previous")
        else:
            self._message("Нет активного медиасеанса Windows")

    def next(self) -> None:
        if self._is_external_active():
            self.external.command("next")
        else:
            self._message("Нет активного медиасеанса Windows")

    def toggle(self) -> None:
        now = time.perf_counter()
        if now - self._last_toggle_at < .28:
            return
        self._last_toggle_at = now
        try:
            if self._is_external_active():
                self.external.command("toggle")
            else:
                self._message("Нет активного медиасеанса Windows")
        except Exception as exc:
            self._message(str(exc))
        self._schedule_frame()

    def _is_external_active(self) -> bool:
        return (self.external_state.status in {"playing", "paused"}
                and bool(self.external_state.title or self.external_state.artist))

    def _poll(self) -> None:
        changed = False
        while True:
            try:
                snapshot = self.external.updates.get_nowait()
            except queue.Empty:
                break
            if snapshot is not None:
                if snapshot.app_id != self._last_app_id:
                    self._last_app_id = snapshot.app_id
                    self._accent_from = self.accent
                    self._accent_started = self.root.tk.call("clock", "milliseconds") / 1000
                    self._accent_future = self._executor.submit(app_accent, snapshot.app_id) if snapshot.app_id else None
                    self._external_volume_supported = False
                    self._volume = 0.5
                    self._external_volume_future = self._executor.submit(get_app_volume, snapshot.app_id) if snapshot.app_id else None
                    self._next_volume_check = time.perf_counter() + 2.0
                if snapshot.title != self.external_state.title or snapshot.app_id != self.external_state.app_id:
                    self.cover_data = snapshot.art
                elif snapshot.art:
                    self.cover_data = snapshot.art
                self.external_state = snapshot
                changed = True
        while True:
            try:
                command, succeeded = self.external.command_results.get_nowait()
            except queue.Empty:
                break
            if not succeeded:
                self._message("Медиаплеер не принял эту команду")
        if self._accent_future and self._accent_future.done():
            try:
                self._accent_target = self._accent_future.result()
            except Exception:
                self._accent_target = NEUTRAL
            self._accent_started = self.root.tk.call("clock", "milliseconds") / 1000
            self._accent_future = None
            changed = True
        if self._external_volume_future and self._external_volume_future.done():
            try:
                current_volume = self._external_volume_future.result()
                self._external_volume_supported = current_volume is not None
                if current_volume is not None and not self._dragging_volume:
                    self._volume = current_volume
            except Exception:
                self._external_volume_supported = False
            self._external_volume_future = None
            self._next_volume_check = time.perf_counter() + 2.0
            changed = True
        if self._volume_set_future and self._volume_set_future.done():
            try:
                if not self._volume_set_future.result():
                    self._external_volume_supported = False
                    self._message("Громкость этого медиасеанса недоступна")
            except Exception:
                self._external_volume_supported = False
                self._message("Громкость этого медиасеанса недоступна")
            self._volume_set_future = None
            if self._pending_external_volume is not None and self._external_volume_supported:
                pending = self._pending_external_volume
                self._pending_external_volume = None
                self._volume_set_future = self._executor.submit(set_app_volume, self._last_app_id, pending)
        if self._is_external_active():
            self._seek_position = self.external_state.position
            self._seek_duration = self.external_state.duration
            self._seek_available = self._seek_duration > 0
            if not self._external_volume_future and time.perf_counter() >= self._next_volume_check:
                self._external_volume_future = self._executor.submit(get_app_volume, self.external_state.app_id)
                self._next_volume_check = time.perf_counter() + 2.0
        else:
            self._seek_available = False
            self._seek_position = 0.0
            self._seek_duration = 0.0
        if changed:
            self._schedule_frame()
        self.root.after(1000, self._poll)

    def _schedule_frame(self) -> None:
        if not self._closing:
            self.root.after_idle(self._draw)

    def _current_text(self) -> tuple[str, str]:
        if self._status_message and self.root.tk.call("clock", "milliseconds") / 1000 < self._status_until:
            return self._status_message, ""
        if self._is_external_active():
            return self.external_state.title or "Медиа без названия", self.external_state.artist
        return "Ожидание музыки", ""

    def _playing(self) -> bool:
        return self.external_state.status == "playing"

    def _cover_bytes(self) -> bytes | None:
        return self.external_state.art or self.cover_data

    def _draw(self) -> None:
        if not self.root.winfo_exists() or not self.root.winfo_viewable():
            return
        frame_perf = time.perf_counter()
        frame_dt = min(.06, max(.001, frame_perf - self._last_frame_perf))
        self._last_frame_perf = frame_perf
        now = self.root.tk.call("clock", "milliseconds") / 1000
        t = min(1.0, max(0.0, (now - self._accent_started) / .55))
        ease = t * t * (3 - 2 * t)
        self.accent = tuple(round(a + (b - a) * ease) for a, b in zip(self._accent_from, self._accent_target))
        is_playing = self._playing()
        if is_playing != self._last_playing_visual:
            self._last_playing_visual = is_playing
            self._playback_animation = now
        playback_pulse = max(0.0, 1.0 - (now - self._playback_animation) / .24) if self._playback_animation else 0.0
        frame = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        d = ImageDraw.Draw(frame)
        # A flat per-pixel-alpha panel keeps the wallpaper visible without any
        # backdrop effects or extra windows that could lag behind while moving.
        d.rounded_rectangle((34, 28, 471, 504), radius=57, fill=(*PANEL, 64), outline=(202, 210, 221, 205), width=2)
        d.rounded_rectangle((36, 30, 469, 502), radius=55, outline=(143, 151, 164, 120), width=1)
        title, artist = self._current_text()
        max_width = 390
        title_font = _font(24 if artist else 27, True)
        while d.textbbox((0, 0), title, font=title_font)[2] > max_width and title_font.size > 15:
            title_font = _font(title_font.size - 1, True)
        if d.textbbox((0, 0), title, font=title_font)[2] > max_width:
            while len(title) > 1 and d.textbbox((0, 0), title + "…", font=title_font)[2] > max_width:
                title = title[:-1]
            title = title.rstrip() + "…"
        d.text((WIDTH // 2, 53 if artist else 63), title, font=title_font,
               fill=(247, 251, 248), anchor="mm")
        if artist:
            artist_font = _font(12)
            artist_text = artist
            while len(artist_text) > 1 and d.textbbox((0, 0), artist_text + "…", font=artist_font)[2] > max_width:
                artist_text = artist_text[:-1]
            if artist_text != artist:
                artist_text = artist_text.rstrip() + "…"
            d.text((WIDTH // 2, 74), artist_text, font=artist_font,
                   fill=(232, 238, 235), anchor="mm")

        cover_source = self._cover_bytes()
        if not hasattr(self, "_cover_source") or cover_source != self._cover_source:
            if hasattr(self, "_cover_render"):
                self._cover_transition_old = self._cover_render.copy()
                self._cover_transition_started = now
            self._cover_source = cover_source
            self._cover_render = _rounded_cover(cover_source, self.fallback_cover)
        cover_t = min(1.0, max(0.0, (now - getattr(self, "_cover_transition_started", 0.0)) / .36))
        cover_ease = cover_t * cover_t * (3 - 2 * cover_t)
        if hasattr(self, "_cover_transition_old") and cover_t < 1.0:
            cover = Image.blend(self._cover_transition_old, self._cover_render, cover_ease)
        else:
            cover = self._cover_render
            self._cover_transition_old = None
        frame.alpha_composite(cover, (149, 84))
        d = ImageDraw.Draw(frame)

        if self._seek_available and self._seek_duration > 0:
            x0, x1, y = 97, 427, 315
            d.rounded_rectangle((x0, y - 1, x1, y + 1), radius=2, fill=(189, 220, 207, 120))
            px = x0 + int((x1 - x0) * min(1, self._seek_position / self._seek_duration))
            d.rounded_rectangle((x0, y - 1, max(x0 + 1, px), y + 1), radius=2, fill=(*self.accent, 235))
            d.ellipse((px - 4, y - 4, px + 4, y + 4), fill=(*self.accent, 245))

        spring_active = False
        button_names = ("previous", "toggle", "next")
        for index, name in enumerate(button_names):
            hover_target = 1.0 if self._hover == name else 0.0
            self._hover_amount[name], self._hover_velocity[name] = _spring_step(
                self._hover_amount[name], self._hover_velocity[name], hover_target, frame_dt,
                stiffness=210.0, damping=22.0)
            neighbor_hover = max((self._hover_amount[other]
                                  for other in button_names if other != name), default=0.0)
            scale_target = 1.0 + .23 * self._hover_amount[name] + .045 * neighbor_hover
            if self._press == name or frame_perf < self._button_press_until[name]:
                scale_target = .93
            elif name == "toggle":
                scale_target += .016 * playback_pulse
            self._button_scale[name], self._button_velocity[name] = _spring_step(
                self._button_scale[name], self._button_velocity[name], scale_target, frame_dt)
            base_x, base_y = self._button_base_center[name]
            target_x, target_y = base_x, base_y - 7.0 * self._hover_amount[name]
            for other_index, other in enumerate(button_names):
                if other == name:
                    continue
                distance = abs(index - other_index)
                influence = self._hover_amount[other] * (1.0 - .22 * (distance - 1))
                target_x += (1.0 if index > other_index else -1.0) * 10.0 * influence
                target_y -= 2.0 * influence
            center_x, center_y = self._button_center[name]
            velocity_x, velocity_y = self._button_center_velocity[name]
            center_x, velocity_x = _spring_step(center_x, velocity_x, target_x, frame_dt,
                                                 stiffness=210.0, damping=22.0)
            center_y, velocity_y = _spring_step(center_y, velocity_y, target_y, frame_dt,
                                                 stiffness=210.0, damping=22.0)
            self._button_center[name] = (center_x, center_y)
            self._button_center_velocity[name] = (velocity_x, velocity_y)
            spring_active |= (abs(self._button_scale[name] - scale_target) > .001
                              or abs(self._button_velocity[name]) > .01
                              or abs(self._hover_amount[name] - hover_target) > .003
                              or abs(self._hover_velocity[name]) > .01
                              or abs(center_x - target_x) > .02 or abs(center_y - target_y) > .02
                              or abs(velocity_x) > .01 or abs(velocity_y) > .01)
        self._draw_button(frame, "previous", tuple(round(v) for v in self._button_center["previous"]), 28,
                          self._hover_amount["previous"], self._button_scale["previous"])
        self._draw_button(frame, "toggle", tuple(round(v) for v in self._button_center["toggle"]), 56,
                          self._hover_amount["toggle"], self._button_scale["toggle"])
        self._draw_button(frame, "next", tuple(round(v) for v in self._button_center["next"]), 28,
                          self._hover_amount["next"], self._button_scale["next"])

        # The groove is a blended glass surface; the thumb is the dynamic accent.
        d = ImageDraw.Draw(frame)
        x0, x1, y = 84, 419, 484
        d.rounded_rectangle((x0, y - 9, x1, y + 9), radius=10, fill=(57, 61, 69, 165), outline=(177, 184, 195, 205), width=2)
        d.rounded_rectangle((x0 + 3, y - 6, x1 - 3, y + 6), radius=7, fill=(30, 33, 39, 190))
        if self._dragging_volume:
            self._volume_visual = self._volume
            self._volume_velocity = 0.0
        else:
            self._volume_visual, self._volume_velocity = _spring_step(
                self._volume_visual, self._volume_velocity, self._volume, frame_dt,
                stiffness=210.0, damping=22.0)
        thumb_x = round(x0 + self._volume_visual * (x1 - x0))
        enabled = self._is_external_active() and self._external_volume_supported
        thumb_fill = VOLUME_BLUE if enabled else (91, 98, 110)
        if enabled:
            d.rounded_rectangle((x0 + 3, y - 3, max(x0 + 3, thumb_x), y + 3), radius=3,
                                fill=(*VOLUME_BLUE, 84))
        if self._hover_volume or self._dragging_volume:
            d.rounded_rectangle((thumb_x - 29, y - 17, thumb_x + 29, y + 17), radius=17,
                outline=(*VOLUME_BLUE, 135 if not self._dragging_volume else 190), width=2)
        d.rounded_rectangle((thumb_x - 25, y - 14, thumb_x + 25, y + 14), radius=14,
                            fill=(*thumb_fill, 232), outline=(204, 215, 232, 235), width=2)
        # Speaker glyph
        speaker = (12, 20, 32) if enabled else (44, 48, 55)
        d.polygon([(thumb_x - 9, y - 4), (thumb_x - 5, y - 4), (thumb_x, y - 8),
                   (thumb_x, y + 8), (thumb_x - 5, y + 4), (thumb_x - 9, y + 4)], fill=speaker)
        d.arc((thumb_x - 2, y - 6, thumb_x + 6, y + 6), -65, 65, fill=speaker, width=1)
        d.arc((thumb_x - 4, y - 9, thumb_x + 9, y + 9), -65, 65, fill=speaker, width=1)

        self.renderer.present(frame, self.root.winfo_x(), self.root.winfo_y())
        spring_active |= abs(self._volume - self._volume_visual) > .001 or abs(self._volume_velocity) > .01
        if t < 1 or spring_active or playback_pulse > 0 or cover_t < 1:
            self.root.after(20, self._draw)

    def _draw_button(self, frame: Image.Image, name: str, center: tuple[int, int], radius: int,
                     hover_amount: float, scale: float) -> None:
        # A small spring overshoot makes mouse feedback feel soft and elastic.
        cx, cy = center
        r = radius * scale
        draw = ImageDraw.Draw(frame)
        if hover_amount > 0.01:
            draw.ellipse((round(cx - r - 2), round(cy - r - 2), round(cx + r + 2), round(cy + r + 2)),
                         outline=(*self.accent, round(30 + 80 * hover_amount)), width=2)
        color = (229, 231, 229) if name != "toggle" else (221, 223, 221)
        if hover_amount > 0:
            color = tuple(min(255, c + round(8 * hover_amount)) for c in color)
        draw.ellipse((round(cx - r), round(cy - r), round(cx + r), round(cy + r)), fill=(*color, 240))
        sx = scale
        if name == "toggle":
            if self._playing():
                bar_h, bar_w, gap = round(67 * sx), round(25 * sx), round(12 * sx)
                draw.rounded_rectangle((cx - gap // 2 - bar_w, cy - bar_h // 2,
                                        cx - gap // 2, cy + bar_h // 2), radius=9, fill=(0, 0, 0, 255))
                draw.rounded_rectangle((cx + gap // 2, cy - bar_h // 2,
                                        cx + gap // 2 + bar_w, cy + bar_h // 2), radius=9, fill=(0, 0, 0, 255))
            else:
                draw.polygon([(cx - 14, cy - 22), (cx - 14, cy + 22), (cx + 24, cy)], fill=(0, 0, 0, 255))
        elif name == "previous":
            w, h = round(30 * sx), round(30 * sx)
            draw.polygon([(cx - 1, cy - h // 2), (cx - w // 2, cy), (cx - 1, cy + h // 2)], fill=(0, 0, 0, 255))
            draw.polygon([(cx + w // 2, cy - h // 2), (cx, cy), (cx + w // 2, cy + h // 2)], fill=(0, 0, 0, 255))
            draw.rounded_rectangle((cx - w // 2 - 2, cy - h // 2, cx - w // 2 + 3, cy + h // 2), radius=2, fill=(0, 0, 0, 255))
        else:
            w, h = round(30 * sx), round(30 * sx)
            draw.polygon([(cx + 1, cy - h // 2), (cx + w // 2, cy), (cx + 1, cy + h // 2)], fill=(0, 0, 0, 255))
            draw.polygon([(cx - w // 2, cy - h // 2), (cx, cy), (cx - w // 2, cy + h // 2)], fill=(0, 0, 0, 255))
            draw.rounded_rectangle((cx + w // 2 - 1, cy - h // 2, cx + w // 2 + 4, cy + h // 2), radius=2, fill=(0, 0, 0, 255))

    def quit(self, *_args) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            self.tray.stop()
        except Exception:
            pass
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.renderer.close()
        self.root.destroy()


def acquire_single_instance() -> int | None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    mutex = kernel.CreateMutexW(None, True, "Local\\DeskBar.SingleInstance.7E09A1D4")
    if not mutex:
        return None
    if ctypes.get_last_error() == 183:
        existing = ctypes.WinDLL("user32").FindWindowW(None, "DeskBar")
        if existing:
            ctypes.WinDLL("user32").ShowWindow(existing, 9)
            ctypes.WinDLL("user32").SetForegroundWindow(existing)
        kernel.CloseHandle(mutex)
        return None
    return mutex


def self_test() -> int:
    """Check GSMTC routing and periodic Windows media-session refresh."""
    import asyncio
    import tempfile
    from types import SimpleNamespace
    import winrt.windows.media.control as gsm
    result_path = Path(tempfile.gettempdir()) / "DeskBar-selftest.txt"
    try:
        class FakeSession:
            def __init__(self, state):
                self.info = SimpleNamespace(
                    playback_status=state,
                    controls=SimpleNamespace(is_pause_enabled=True, is_play_enabled=True,
                                             is_play_pause_toggle_enabled=True),
                )
                self.pause_calls = self.play_calls = self.toggle_calls = 0
            def get_playback_info(self): return self.info
            async def try_pause_async(self):
                self.pause_calls += 1
                return False
            async def try_play_async(self):
                self.play_calls += 1
                return True
            async def try_toggle_play_pause_async(self):
                self.toggle_calls += 1
                return True

        probe = ExternalSessions.__new__(ExternalSessions)
        probe.command_results = queue.SimpleQueue()
        fake = FakeSession(gsm.GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING)
        probe._session = fake
        asyncio.run(probe._command("toggle"))
        pause_safe = fake.pause_calls == 1 and fake.toggle_calls == 0 and not probe.command_results.get()[1]
        fake = FakeSession(gsm.GlobalSystemMediaTransportControlsSessionPlaybackStatus.PAUSED)
        probe._session = fake
        asyncio.run(probe._command("toggle"))
        resume_ok = fake.play_calls == 1 and fake.toggle_calls == 0 and probe.command_results.get()[1]
        fake = FakeSession(gsm.GlobalSystemMediaTransportControlsSessionPlaybackStatus.STOPPED)
        probe._session = fake
        asyncio.run(probe._command("toggle"))
        stopped_safe = fake.play_calls == 0 and fake.toggle_calls == 0 and not probe.command_results.get()[1]

        external = ExternalSessions()
        media_ready = external.ready.wait(3)
        media_ok = media_ready and external.available
        delta = 0.0
        periodic_ok = False
        if media_ok:
            deadline = time.perf_counter() + 3.2
            previous_at = None
            while time.perf_counter() < deadline and not periodic_ok:
                try:
                    snapshot = external.updates.get(timeout=max(.01, deadline - time.perf_counter()))
                except queue.Empty:
                    break
                now = time.perf_counter()
                if snapshot is None:
                    continue
                if previous_at is not None:
                    delta = now - previous_at
                    periodic_ok = .75 <= delta <= 1.6
                previous_at = now
        routing_ok = pause_safe and resume_ok and stopped_safe
        success = routing_ok and media_ok and periodic_ok
        status = (f"{'PASS' if success else 'FAIL'}: Windows GSMTC available={media_ok}; "
                  f"periodic one-second refresh={'PASS' if periodic_ok else 'FAIL'} "
                  f"(interval={delta:.2f}s); "
                  f"safe play/pause routing={'PASS' if routing_ok else 'FAIL'}\n")
        result_path.write_text(status, encoding="utf-8")
        print(status.strip())
        return 0 if success else 1
    except Exception as exc:
        print(f"GSMTC self-check failed: {exc}")
        result_path.write_text(f"FAIL: {exc}\n", encoding="utf-8")
        return 1


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    mutex = acquire_single_instance()
    if mutex is None:
        return 0
    root = tk.Tk()
    try:
        DeskBar(root)
        root.mainloop()
    finally:
        ctypes.WinDLL("kernel32").ReleaseMutex(mutex)
        ctypes.WinDLL("kernel32").CloseHandle(mutex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

