"""Read-only Windows process and window queries for the UEFN session tools (pure ctypes).

uefn_status / uefn_launch_project use these to see whether the UEFN editor runs, whether its
windows respond and whether the crash reporter is showing. Nothing here sends mouse or keyboard
input or moves windows. The one acting call is terminate_process(), used only by
uefn_launch_project(close_crash_reporter=True) on an orphaned crash reporter.
"""

from __future__ import annotations

import ctypes
import fnmatch
import os
import re
import sys
import time
from typing import Optional, Sequence

IS_WINDOWS = sys.platform == "win32"


class WinError(RuntimeError):
    """A Windows query failed (or this is not Windows)."""


def normalize_process_name(name: str) -> str:
    """'C:\\x\\Foo.EXE' -> 'foo'."""
    base = re.split(r"[\\/]", (name or "").strip().strip('"'))[-1].strip()
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base.lower()


def process_matches(name: str, pattern: str) -> bool:
    """Glob when the pattern has wildcards, else case-insensitive substring."""
    n, p = normalize_process_name(name), normalize_process_name(pattern)
    if not p:
        return True
    if any(c in p for c in "*?["):
        return fnmatch.fnmatchcase(n, p)
    return p in n


if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:  # pragma: no cover
        dwmapi = None

    ULONG_PTR = ctypes.c_size_t

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ULONG_PTR), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _proto(dll, name, restype, *argtypes):
        fn = getattr(dll, name)
        fn.restype = restype
        fn.argtypes = list(argtypes)
        return fn

    H, B, D, I, U = wintypes.HWND, wintypes.BOOL, wintypes.DWORD, ctypes.c_int, wintypes.UINT
    _GetForegroundWindow = _proto(user32, "GetForegroundWindow", H)
    _GetWindowThreadProcessId = _proto(user32, "GetWindowThreadProcessId", D, H, ctypes.POINTER(D))
    _GetWindowTextLengthW = _proto(user32, "GetWindowTextLengthW", I, H)
    _GetWindowTextW = _proto(user32, "GetWindowTextW", I, H, wintypes.LPWSTR, I)
    _GetClassNameW = _proto(user32, "GetClassNameW", I, H, wintypes.LPWSTR, I)
    _IsWindowVisible = _proto(user32, "IsWindowVisible", B, H)
    _IsIconic = _proto(user32, "IsIconic", B, H)
    _GetWindowRect = _proto(user32, "GetWindowRect", B, H, ctypes.POINTER(wintypes.RECT))
    _EnumWindows = _proto(user32, "EnumWindows", B, WNDENUMPROC, wintypes.LPARAM)
    _GetWindow = _proto(user32, "GetWindow", H, H, U)

    _OpenProcess = _proto(kernel32, "OpenProcess", wintypes.HANDLE, D, B, D)
    _CloseHandle = _proto(kernel32, "CloseHandle", B, wintypes.HANDLE)
    _TerminateProcess = _proto(kernel32, "TerminateProcess", B, wintypes.HANDLE, U)
    _QueryFullProcessImageNameW = _proto(kernel32, "QueryFullProcessImageNameW", B, wintypes.HANDLE, D,
                                         wintypes.LPWSTR, ctypes.POINTER(D))
    _GetProcessTimes = _proto(kernel32, "GetProcessTimes", B, wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                              ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                              ctypes.POINTER(wintypes.FILETIME))
    _CreateToolhelp32Snapshot = _proto(kernel32, "CreateToolhelp32Snapshot", wintypes.HANDLE, D, D)
    _Process32FirstW = _proto(kernel32, "Process32FirstW", B, wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    _Process32NextW = _proto(kernel32, "Process32NextW", B, wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))

    if dwmapi is not None:
        _DwmGetWindowAttribute = _proto(dwmapi, "DwmGetWindowAttribute", ctypes.c_long, H, D, ctypes.c_void_p, D)

GW_OWNER = 4
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
PROCESS_TERMINATE, PROCESS_QUERY_LIMITED_INFORMATION = 0x0001, 0x1000


def require_windows() -> None:
    if not IS_WINDOWS:
        raise WinError("the UEFN session tools are Windows-only")


# -- processes ----------------------------------------------------------------

def process_image(pid: int) -> str:
    """Full image path of a process ('' when it cannot be opened)."""
    h = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        return buf.value if _QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        _CloseHandle(h)


def process_start_time(pid: int) -> Optional[float]:
    """Process creation time as a Unix timestamp."""
    h = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not _GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        ticks = (c.dwHighDateTime << 32) | c.dwLowDateTime
        return (ticks - 116444736000000000) / 1e7
    finally:
        _CloseHandle(h)


def list_processes(name_pattern: str = "") -> list[dict]:
    """Running processes (pid, parent, exe name) filtered like process_matches()."""
    require_windows()
    snap = _CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        raise WinError(f"CreateToolhelp32Snapshot failed ({ctypes.get_last_error()})")
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile
            if not name_pattern or process_matches(name, name_pattern):
                out.append({"pid": int(entry.th32ProcessID), "parent_pid": int(entry.th32ParentProcessID),
                            "name": name})
            ok = _Process32NextW(snap, ctypes.byref(entry))
    finally:
        _CloseHandle(snap)
    return out


def terminate_process(pid: int) -> bool:
    """Stop one process by pid (used only for an orphaned UEFN crash reporter)."""
    require_windows()
    h = _OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(_TerminateProcess(h, 1))
    finally:
        _CloseHandle(h)


# -- windows ---------------------------------------------------------------------

def _hwnd(h) -> int:
    return int(h or 0)


def _window_text(hwnd: int) -> str:
    n = _GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(max(n, 0) + 2)
    _GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _GetClassNameW(hwnd, buf, 256)
    return buf.value


def _frame_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    if dwmapi is not None and _DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r),
                                                    ctypes.sizeof(r)) == 0 and r.right > r.left:
        return r.left, r.top, r.right, r.bottom
    _GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _cloaked(hwnd: int) -> bool:
    if dwmapi is None:
        return False
    v = wintypes.DWORD()
    return _DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v)) == 0 and bool(v.value)


def window_info(hwnd: int, fg: int = 0) -> dict:
    hwnd = _hwnd(hwnd)
    pid = wintypes.DWORD()
    _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    image = process_image(int(pid.value))
    left, top, right, bottom = _frame_rect(hwnd)
    return {
        "hwnd": hwnd, "title": _window_text(hwnd), "class": _class_name(hwnd),
        "pid": int(pid.value), "process": os.path.basename(image) if image else "",
        "rect": {"x": left, "y": top, "width": right - left, "height": bottom - top},
        "visible": bool(_IsWindowVisible(hwnd)), "minimized": bool(_IsIconic(hwnd)),
        "cloaked": _cloaked(hwnd), "owner": _hwnd(_GetWindow(hwnd, GW_OWNER)), "foreground": hwnd == fg,
    }


def find_windows(process: str = "") -> list[dict]:
    """Visible top-level windows in z-order (topmost first), filtered by process name."""
    require_windows()
    hwnds: list[int] = []

    def cb(h, lparam):
        hwnds.append(_hwnd(h))
        return True

    _EnumWindows(WNDENUMPROC(cb), 0)
    fg = _hwnd(_GetForegroundWindow())
    out = []
    for z, h in enumerate(hwnds):
        if not _IsWindowVisible(h):
            continue
        try:
            info = window_info(h, fg)
        except Exception:
            continue
        if info["cloaked"] or info["rect"]["width"] <= 0 or info["rect"]["height"] <= 0:
            continue
        if process and not process_matches(info["process"], process):
            continue
        info["z"] = z
        out.append(info)
    return out


def pick_main_window(windows: Sequence[dict]) -> Optional[dict]:
    """The app's main window among matches: foreground, unowned, restored, largest, topmost."""
    if not windows:
        return None
    return sorted(windows, key=lambda w: (
        not w.get("foreground", False), bool(w["owner"]), w["minimized"],
        -(w["rect"]["width"] * w["rect"]["height"]), w.get("z", 0)))[0]


def is_hung(hwnd: int) -> bool:
    """Windows' 'Not Responding' test: the window's thread has not pumped messages for ~5 s."""
    try:
        fn = user32.IsHungAppWindow
        fn.restype, fn.argtypes = wintypes.BOOL, [wintypes.HWND]
        return bool(fn(hwnd))
    except Exception:
        return False


def brief(w: Optional[dict]) -> Optional[dict]:
    if not w:
        return None
    return {"hwnd": w["hwnd"], "pid": w["pid"], "process": w["process"], "title": w["title"][:120],
            "class": w["class"]}


def wait_gone(pids: Sequence[int], timeout: float = 5.0) -> bool:
    """True once none of `pids` runs any more."""
    deadline = time.monotonic() + timeout
    while True:
        alive = {p["pid"] for p in list_processes()} & set(pids)
        if not alive:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
