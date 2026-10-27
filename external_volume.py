"""Per-application audio-session volume (never the Windows master volume)."""
from __future__ import annotations

import ctypes
import re
import uuid
from ctypes import wintypes
from pathlib import Path


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value: str) -> "GUID":
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


CLSID_MMDEVICE_ENUMERATOR = GUID.parse("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDEVICE_ENUMERATOR = GUID.parse("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAUDIOSESSION_MANAGER2 = GUID.parse("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
IID_IAUDIOSESSION_CONTROL2 = GUID.parse("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
IID_ISIMPLE_AUDIO_VOLUME = GUID.parse("87CE5498-68D6-44E5-9215-6DA47EF883D8")
CLSCTX_ALL = 0x17
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def _matching_process_ids(app_id: str) -> set[int]:
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
    target = app_id.casefold()
    is_path = "\\" in target or (len(target) > 2 and target[1:3] == ":\\")
    executable = Path(app_id).stem.casefold() if is_path or target.endswith(".exe") else ""
    ignored = {"microsoft", "windows", "application", "desktop", "runtime", "host", "exe"}
    tokens = [s for s in re.split(r"[^a-z0-9]+", target.split("!")[0])
              if len(s) >= 4 and s not in ignored]
    snapshot = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return set()
    result: set[int] = set()
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            return result
        while True:
            pid = int(entry.th32ProcessID)
            process = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if process:
                try:
                    size = wintypes.UINT(0)
                    get_aumid = kernel.GetApplicationUserModelId
                    get_aumid.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
                    get_aumid.restype = wintypes.LONG
                    code = get_aumid(process, ctypes.byref(size), None)
                    exact = False
                    if size.value:
                        aumid = ctypes.create_unicode_buffer(size.value)
                        exact = get_aumid(process, ctypes.byref(size), aumid) == 0 and aumid.value.casefold() == target
                    stem = Path(entry.szExeFile).stem.casefold()
                    matched_name = stem == executable if executable else any(token in stem for token in tokens)
                    if exact or matched_name:
                        result.add(pid)
                except Exception:
                    stem = Path(entry.szExeFile).stem.casefold()
                    if (stem == executable if executable else any(token in stem for token in tokens)):
                        result.add(pid)
                finally:
                    kernel.CloseHandle(process)
            if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel.CloseHandle(snapshot)
    return result


def _method(pointer, index: int, result_type, *argument_types):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *argument_types)(table[index])
    return function


def _release(pointer) -> None:
    if pointer and pointer.value:
        _method(pointer, 2, wintypes.ULONG)(pointer)
        pointer.value = None


def _query(pointer, iid: GUID):
    out = ctypes.c_void_p()
    hr = _method(pointer, 0, wintypes.LONG, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
        pointer, ctypes.byref(iid), ctypes.byref(out))
    return out if hr >= 0 else None


def _iterate_audio_sessions(app_id: str):
    """Yield (ISimpleAudioVolume*, interface refs to release) for matching PIDs."""
    pids = _matching_process_ids(app_id)
    if not pids:
        return [], [], False, None, {}
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = wintypes.LONG
    ole.CoUninitialize.argtypes = []
    initialized = ole.CoInitializeEx(None, 0) >= 0
    refs: list[ctypes.c_void_p] = []
    volumes: list[ctypes.c_void_p] = []
    found_pids: dict[int, int] = {}
    try:
        enumerator = ctypes.c_void_p()
        hr = ole.CoCreateInstance(ctypes.byref(CLSID_MMDEVICE_ENUMERATOR), None, CLSCTX_ALL,
                                  ctypes.byref(IID_IMMDEVICE_ENUMERATOR), ctypes.byref(enumerator))
        if hr < 0:
            return volumes, refs, initialized, ole, found_pids
        refs.append(enumerator)
        device = ctypes.c_void_p()
        hr = _method(enumerator, 4, wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
                     ctypes.POINTER(ctypes.c_void_p))(enumerator, 0, 1, ctypes.byref(device))
        if hr < 0:
            return volumes, refs, initialized, ole, found_pids
        refs.append(device)
        manager = ctypes.c_void_p()
        hr = _method(device, 3, wintypes.LONG, ctypes.POINTER(GUID), wintypes.DWORD,
                     ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
            device, ctypes.byref(IID_IAUDIOSESSION_MANAGER2), CLSCTX_ALL, None, ctypes.byref(manager))
        if hr < 0:
            return volumes, refs, initialized, ole, found_pids
        refs.append(manager)
        sessions = ctypes.c_void_p()
        hr = _method(manager, 5, wintypes.LONG, ctypes.POINTER(ctypes.c_void_p))(
            manager, ctypes.byref(sessions))
        if hr < 0:
            return volumes, refs, initialized, ole, found_pids
        refs.append(sessions)
        count = wintypes.INT()
        hr = _method(sessions, 3, wintypes.LONG, ctypes.POINTER(wintypes.INT))(
            sessions, ctypes.byref(count))
        if hr < 0:
            return volumes, refs, initialized, ole, found_pids
        for index in range(count.value):
            control = ctypes.c_void_p()
            hr = _method(sessions, 4, wintypes.LONG, wintypes.INT, ctypes.POINTER(ctypes.c_void_p))(
                sessions, index, ctypes.byref(control))
            if hr < 0 or not control:
                continue
            refs.append(control)
            control2 = _query(control, IID_IAUDIOSESSION_CONTROL2)
            if not control2:
                continue
            refs.append(control2)
            pid = wintypes.DWORD()
            hr = _method(control2, 14, wintypes.LONG, ctypes.POINTER(wintypes.DWORD))(
                control2, ctypes.byref(pid))
            if hr < 0 or pid.value not in pids:
                continue
            found_pids[int(pid.value)] = -1
            state = wintypes.INT()
            hr = _method(control2, 3, wintypes.LONG, ctypes.POINTER(wintypes.INT))(
                control2, ctypes.byref(state))
            found_pids[int(pid.value)] = state.value if hr >= 0 else hr
            # Inactive sessions can still retain a user volume slider while paused.
            # Expired sessions no longer represent a controllable stream.
            if hr >= 0 and state.value == 2:
                continue
            volume = _query(control, IID_ISIMPLE_AUDIO_VOLUME)
            if volume:
                volumes.append(volume)
        return volumes, refs, initialized, ole, found_pids
    except Exception:
        for pointer in reversed(refs):
            _release(pointer)
        if initialized:
            ole.CoUninitialize()
        return [], [], False, ole, {}


def get_app_volume(app_id: str) -> float | None:
    """Return the average active session level for a source app, or None if unavailable."""
    result = _iterate_audio_sessions(app_id)
    if len(result) != 5:
        return None
    volumes, refs, initialized, ole, _found_pids = result
    try:
        levels = []
        for volume in volumes:
            level = ctypes.c_float()
            hr = _method(volume, 4, wintypes.LONG, ctypes.POINTER(ctypes.c_float))(
                volume, ctypes.byref(level))
            if hr >= 0:
                levels.append(max(0.0, min(1.0, level.value)))
        return sum(levels) / len(levels) if levels else None
    finally:
        for pointer in reversed(volumes + refs):
            _release(pointer)
        if initialized:
            ole.CoUninitialize()


def set_app_volume(app_id: str, level: float) -> bool:
    """Set the active source app's Core Audio session(s), not the system master volume."""
    result = _iterate_audio_sessions(app_id)
    if len(result) != 5:
        return False
    volumes, refs, initialized, ole, _found_pids = result
    try:
        if not volumes:
            return False
        value = ctypes.c_float(max(0.0, min(1.0, float(level))))
        succeeded = 0
        for volume in volumes:
            hr = _method(volume, 3, wintypes.LONG, ctypes.c_float, ctypes.POINTER(GUID))(
                volume, value, None)
            if hr >= 0:
                succeeded += 1
        return succeeded > 0
    finally:
        for pointer in reversed(volumes + refs):
            _release(pointer)
        if initialized:
            ole.CoUninitialize()

