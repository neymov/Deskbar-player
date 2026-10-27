"""Windows external media sessions and local MCI playback for DeskBar."""
from __future__ import annotations

import asyncio
import queue
import threading
from dataclasses import dataclass

import winrt.windows.media.control as gsm
from winrt.windows.storage.streams import DataReader


@dataclass(slots=True)
class MediaSnapshot:
    title: str = ""
    artist: str = ""
    app_id: str = ""
    status: str = "stopped"
    art: bytes | None = None
    position: float = 0.0
    duration: float = 0.0
    has_previous: bool = False
    has_next: bool = False
    can_play: bool = False
    can_pause: bool = False
    can_toggle: bool = False


class ExternalSessions:
    """Observe the Windows Now Playing session and publish one-second snapshots."""

    def __init__(self) -> None:
        self.updates: queue.SimpleQueue[MediaSnapshot | None] = queue.SimpleQueue()
        self.command_results: queue.SimpleQueue[tuple[str, bool]] = queue.SimpleQueue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._refresh_lock: asyncio.Lock | None = None
        self._session = None
        self._manager = None
        self.ready = threading.Event()
        self.available = False
        self.error = ""
        self._events: list[tuple[object, str, int]] = []
        self._art_key: tuple[str, str, str] | None = None
        self._cached_art: bytes | None = None
        self._thread = threading.Thread(target=self._run, name="DeskBar-GSMTC", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            self._manager = await gsm.GlobalSystemMediaTransportControlsSessionManager.request_async()
            self.available = True
            self.ready.set()
            self._manager.add_current_session_changed(self._on_sessions_changed)
            self._manager.add_sessions_changed(self._on_sessions_changed)
            while True:
                started = self._loop.time()
                await self._refresh()
                await asyncio.sleep(max(0.05, 1.0 - (self._loop.time() - started)))
        except Exception as exc:
            self.error = str(exc)
            self.ready.set()
            self.updates.put(None)

    def _on_sessions_changed(self, *_args) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._refresh(force_art=True)))

    def _schedule_refresh(self, *_args) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._refresh(force_art=True)))

    async def _refresh(self, force_art: bool = False) -> None:
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        async with self._refresh_lock:
            await self._refresh_once(force_art)

    async def _refresh_once(self, force_art: bool = False) -> None:
        try:
            sessions = list(self._manager.get_sessions()) if self._manager else []
            if not sessions:
                self._bind_session(None)
                self.updates.put(MediaSnapshot())
                return
            playing = []
            current = self._manager.get_current_session() if self._manager else None
            for session in sessions:
                try:
                    info = session.get_playback_info()
                    label = gsm.GlobalSystemMediaTransportControlsSessionPlaybackStatus(
                        info.playback_status).name.lower()
                    if label == "playing":
                        playing.append((session, info))
                except Exception:
                    continue

            # Prefer Windows' own current Now Playing session, then any other
            # currently playing session. A stale paused session is considered
            # only when nothing is playing.
            current_state = ""
            if current is not None:
                try:
                    current_info = current.get_playback_info()
                    current_state = gsm.GlobalSystemMediaTransportControlsSessionPlaybackStatus(
                        current_info.playback_status).name.lower()
                except Exception:
                    current_info = None
            if current is not None and current_state == "playing":
                candidates = [(current, current_info)] + [item for item in playing if item[0] is not current]
            elif playing:
                candidates = playing
            elif current is not None and current_state == "paused":
                candidates = [(current, current_info)]
            else:
                candidates = []

            selected = None
            props = None
            for candidate, info in candidates:
                try:
                    candidate_props = await candidate.try_get_media_properties_async()
                    if (candidate_props.title or "").strip() or (candidate_props.artist or "").strip():
                        selected, props = candidate, candidate_props
                        break
                except Exception:
                    continue
            if selected is None:
                self._bind_session(None)
                self.updates.put(MediaSnapshot())
                return
            session = selected
            self._bind_session(session)
            info = session.get_playback_info()
            title = (props.title or "").strip()
            artist = (props.artist or "").strip()
            playback_status = gsm.GlobalSystemMediaTransportControlsSessionPlaybackStatus(info.playback_status).name.lower()
            status = "playing" if "playing" in playback_status else "paused" if "paused" in playback_status else "stopped"
            app_id = session.source_app_user_model_id or ""
            art_key = (app_id, title, artist)
            if force_art or art_key != self._art_key:
                art = None
                if props.thumbnail:
                    try:
                        stream = await props.thumbnail.open_read_async()
                        size = min(int(stream.size), 6 * 1024 * 1024)
                        if size:
                            reader = DataReader(stream.get_input_stream_at(0))
                            await reader.load_async(size)
                            chunk = bytearray(size)
                            reader.read_bytes(chunk)
                            reader.close()
                            art = bytes(chunk)
                    except Exception:
                        art = None
                self._art_key, self._cached_art = art_key, art
            else:
                art = self._cached_art
            timeline = session.get_timeline_properties()
            pos = max(0.0, timeline.position.total_seconds())
            end = max(0.0, timeline.end_time.total_seconds())
            controls = info.controls
            self.updates.put(MediaSnapshot(
                title=title, artist=artist,
                app_id=app_id, status=status,
                art=art, position=pos, duration=max(0.0, end),
                has_previous=bool(controls.is_previous_enabled),
                has_next=bool(controls.is_next_enabled),
                can_play=bool(controls.is_play_enabled),
                can_pause=bool(controls.is_pause_enabled),
                can_toggle=bool(controls.is_play_pause_toggle_enabled),
            ))
        except Exception:
            self.updates.put(MediaSnapshot())

    def _bind_session(self, session) -> None:
        if session is self._session:
            return
        for obj, kind, token in self._events:
            try:
                getattr(obj, f"remove_{kind}")(token)
            except Exception:
                pass
        self._events.clear()
        self._session = session
        if session is not None:
            try:
                self._events.append((session, "playback_info_changed", session.add_playback_info_changed(self._schedule_refresh)))
                self._events.append((session, "media_properties_changed", session.add_media_properties_changed(self._schedule_refresh)))
                self._events.append((session, "timeline_properties_changed", session.add_timeline_properties_changed(self._schedule_refresh)))
            except Exception:
                pass

    def command(self, command: str) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._command(command), self._loop)

    async def _command(self, command: str) -> None:
        session = self._session
        if not session:
            self.command_results.put((command, False))
            return
        try:
            result = False
            if command == "toggle":
                info = session.get_playback_info()
                state = gsm.GlobalSystemMediaTransportControlsSessionPlaybackStatus(
                    info.playback_status).name
                if state == "PLAYING":
                    if info.controls.is_pause_enabled:
                        result = await session.try_pause_async()
                    elif info.controls.is_play_pause_toggle_enabled:
                        result = await session.try_toggle_play_pause_async()
                elif state == "PAUSED":
                    if info.controls.is_play_enabled:
                        result = await session.try_play_async()
                    elif info.controls.is_play_pause_toggle_enabled:
                        result = await session.try_toggle_play_pause_async()
            elif command == "previous":
                result = await session.try_skip_previous_async()
            elif command == "next":
                result = await session.try_skip_next_async()
            elif command == "stop":
                result = await session.try_stop_async()
            elif command.startswith("seek:"):
                result = await session.try_change_playback_position_async(int(float(command[5:]) * 10_000_000))
            self.command_results.put((command, bool(result)))
        except Exception:
            self.command_results.put((command, False))


