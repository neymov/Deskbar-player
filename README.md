# DeskBar

Compact Windows Now Playing controller inspired by the supplied 521 × 523 reference image. The application uses a per-pixel-alpha glass window rendered with Pillow, Win32 single-instance/tray/startup integration, and Windows Global System Media Transport Controls (GSMTC). The window corners are transparent and the glass card lets the desktop wallpaper show through.
![Описание фото](images/player.png)

## Features

- Compact matte-black translucent glass surface (75% transparent) with rounded album artwork, a fixed blue volume thumb, and spring-based controls with dock-style hover magnification. No backdrop blur is used.
- Reads title, artist, playback status, timeline, and available album art from Windows' current media session (the same session Windows identifies for its Now Playing controls). The session is refreshed once per second and also on Windows session/track events. It prefers Windows' current playing session, then another playing session, then its current paused session.
- Sends previous, play/pause, next, stop, and seek commands to the selected external session where Windows and the source app expose them.
- Shows metadata and artwork published by Spotify, browsers, and other compatible Windows media sessions. It does not open local tracks or provide a music picker.
- Real Windows notification-area icon, single-instance activation, close-to-tray, Open/Exit actions, and reversible per-user startup toggle.
- The volume slider uses Windows Core Audio's per-application audio-session volume when it can match the selected media source to a shared-mode audio session. It never changes Windows master volume and is disabled when Windows does not expose a matching audio session.

## Known limits

- External metadata and commands depend on the source app publishing a GSMTC session and supporting the requested command. Browser tabs and SoundCloud behave according to their current Windows media-session integration; this cannot be guaranteed for every app or tab.
- The dynamic glow uses the icon from a process whose AppUserModelID matches the selected GSMTC app. If Windows does not expose a matching process/icon, DeskBar falls back to a stable neutral gray. The volume thumb stays blue as requested.
- Per-app external volume depends on a matching Windows audio session and is unavailable for apps that expose no session, use exclusive-mode audio, or cannot be matched to the GSMTC source. The slider is disabled in that case.
- Spotify, SoundCloud in a browser, and other sources appear only if they publish a Windows GSMTC session and provide metadata. GSMTC support and available fields differ by application and browser.
- The sample cover in `assets/reference_cover.png` is cropped from the user-supplied design reference and is displayed only when the selected Windows session does not provide album artwork.
- The progress scrubber appears only when the active source reports a seekable timeline. External seek can be rejected by the source app.
- The reference has no title bar. `Alt+F4` or `Escape` hides DeskBar to the tray; use the tray menu's Exit command to quit.

## Requirements

- Windows 10/11 x64.
- Python 3.12+ for source runs (this project was packaged here with Python 3.15 x64).
- Dependencies in `requirements.txt`; the build script installs them into `.venv` when present.

## Run from source

```powershell
py -3.15 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe deskbar.py
```

`Space` toggles playback, Left/Right sends previous/next to the Windows session, and Escape hides the window.

If the player window is hidden, DeskBar remains in the notification area: click its pause icon to reopen it, or choose **Открыть** from the icon's menu. Choose **Выход** to stop DeskBar completely.

DeskBar follows Windows' current media session automatically; there is no local file picker or source-selection menu.

## Build a Release EXE

```powershell
.\build.ps1
```

The script creates a single-file, 64-bit Windows executable at `publish\DeskBar.exe`. PyInstaller bundles the Python runtime and Python dependencies; the EXE uses Windows' built-in Win32, Core Audio, and GSMTC APIs. No Visual Studio C++ compiler is required.

Build intermediates go to the sibling `work\DeskBar-build` folder by default. Pass `-BuildPath <folder>` to `build.ps1` to choose another location.

To check GSMTC access, the one-second metadata refresh, and safe pause/resume routing:

```powershell
.\.venv\Scripts\python.exe deskbar.py --self-test
```

## Third-party components

- Pillow — image rendering; HPND license.
- pystray — Windows notification icon; LGPL-3.0.
- PyWinRT projections — Windows Runtime APIs; MIT.
- PyInstaller — executable packaging; GPL-2.0-or-later with its bootloader exception.

The project source is MIT licensed. See the upstream packages for their exact notices and redistribution terms.

