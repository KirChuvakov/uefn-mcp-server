"""Desktop control for the UEFN MCP server: see the Windows desktop and drive UEFN / Fortnite UI.

Several UEFN steps have no scripting surface: the crash-report dialog, the HUB (project browser)
screen, Launch Session / Push Changes hotkeys, modal dialogs, and the Fortnite client itself. These
tools let an agent look at the desktop and send real mouse / keyboard input, inside safety rails:

* **Allowlist.** Input goes only to windows of allowed processes: the UEFN editor, its crash
  reporter, the Epic Games Launcher and the Fortnite client. ``UEFN_DESKTOP_ALLOW`` replaces the
  list (comma-separated process names, ``*`` / ``?`` globs); a leading ``+`` extends it instead;
  ``*`` alone disables the check (dangerous: any window can then receive input).
* **Foreground check.** Every input call names or resolves its target window, brings it to the
  foreground, and re-checks right before each input batch that the foreground window belongs to an
  allowed process (and to the target's process). Otherwise it refuses with a clear error.
* **Kill switch.** When an acting call starts while the mouse cursor sits in the top-left corner of
  any monitor (within 5 px), the call is refused. Park the mouse there to stop a runaway agent. Drags
  and long typing re-check between steps. Points inside that zone are never clicked.
* **UIPI.** A target running at a higher integrity level (elevated) would silently drop injected
  input, so such targets are refused.
* **Audit log.** Every acting call (input, focus, close, launch) is logged as one JSON line to
  ``%TEMP%\\uefn-mcp\\desktop_control.log`` (rotating, 1 MB x 4; ``UEFN_DESKTOP_LOG`` overrides).
  Typed text is redacted when it looks like a password or goes to a sign-in window.
* ``UEFN_DESKTOP_DISABLE=1`` turns every acting call off.

Coordinates: importing this module makes the MCP server process per-monitor DPI aware (V2), so all
coordinates are PHYSICAL pixels of the virtual desktop: the space of GDI / ``mss`` captures,
GetWindowRect, SetCursorPos and GetCursorPos. Monitors left of or above the primary have negative
origins. ``desktop_screenshot`` downscales by default and returns (and stores next to the PNG) the
mapping ``screen = origin + floor((image + 0.5) * scale)``, so ``desktop_click(shot=...)`` takes
pixel coordinates read off the screenshot.

Pure ctypes on Windows, no third-party dependency (captures use GDI, PNGs are written with zlib).
Runs in the MCP server process: needs neither UEFN nor the listener.
"""

from __future__ import annotations

import asyncio
import ctypes
import fnmatch
import itertools
import json
import logging
import logging.handlers
import math
import os
import re
import struct
import sys
import tempfile
import time
import zlib
from typing import Any, Iterable, Optional, Sequence

IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_ALLOWED_PROCESSES = (
    "UnrealEditorFortnite-Win64-Shipping",  # UEFN editor, also its HUB / project browser
    "CrashReportClientEditor*",             # UEFN crash report dialog
    "EpicGamesLauncher",                    # Epic Games Launcher
    "FortniteClient-Win64-Shipping*",       # Fortnite client (incl. anti-cheat variants)
    "FortniteLauncher",                     # Fortnite bootstrap launcher
)
ALLOW_ENV = "UEFN_DESKTOP_ALLOW"
DISABLE_ENV = "UEFN_DESKTOP_DISABLE"
LOG_ENV = "UEFN_DESKTOP_LOG"
SHOTS_ENV = "UEFN_DESKTOP_SHOTS"

KILL_SWITCH_MARGIN = 5          # px from a monitor's top-left corner
SIGNATURE = 0x55454D43           # dwExtraInfo tag on injected input ("UEMC")
DEFAULT_MAX_LONG_EDGE = 1568     # screenshots the model sees without further resizing
DEFAULT_MAX_PIXELS = 1_190_000
MAX_TEXT_LEN = 4000
MAX_BURST_SECONDS = 120.0


class DesktopError(RuntimeError):
    """A desktop operation failed."""


class DesktopRefused(DesktopError):
    """A safety rail refused the operation."""


# ---------------------------------------------------------------------------
# Pure helpers (no Win32; unit-tested offline)
# ---------------------------------------------------------------------------

def normalize_process_name(name: str) -> str:
    """'C:\\x\\Foo.EXE' -> 'foo'; used for allowlist and window matching."""
    base = re.split(r"[\\/]", (name or "").strip().strip('"'))[-1].strip()
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base.lower()


def parse_allowlist(value: Optional[str]) -> tuple[list[str], bool]:
    """Parse UEFN_DESKTOP_ALLOW. Returns (patterns, check_disabled).

    None / empty -> the defaults; 'a,b' replaces them; '+a,b' extends them; '*' disables the check.
    """
    defaults = [normalize_process_name(p) for p in DEFAULT_ALLOWED_PROCESSES]
    if value is None or not value.strip():
        return defaults, False
    text = value.strip()
    extend = text.startswith("+")
    if extend:
        text = text[1:]
    items = [normalize_process_name(x) for x in text.split(",") if x.strip()]
    if "*" in items:
        return ["*"], True
    out: list[str] = []
    for p in (defaults if extend else []) + items:
        if p and p not in out:
            out.append(p)
    return out, False


def process_allowed(name: str, patterns: Sequence[str], disabled: bool = False) -> bool:
    if disabled:
        return True
    n = normalize_process_name(name)
    return bool(n) and any(fnmatch.fnmatchcase(n, p) for p in patterns)


def allowlist() -> tuple[list[str], bool]:
    return parse_allowlist(os.environ.get(ALLOW_ENV))


def is_allowed(name: str) -> bool:
    patterns, disabled = allowlist()
    return process_allowed(name, patterns, disabled)


def process_matches(name: str, pattern: str) -> bool:
    """Window filter: glob when the pattern has wildcards, else case-insensitive substring."""
    n, p = normalize_process_name(name), normalize_process_name(pattern)
    if not p:
        return True
    if any(c in p for c in "*?["):
        return fnmatch.fnmatchcase(n, p)
    return p in n


# -- key names ---------------------------------------------------------------

KEY_CODES: dict[str, int] = {}


def _key(names: str, vk: int) -> None:
    for n in names.split():
        KEY_CODES[n] = vk


_key("backspace back bksp", 0x08)
_key("tab", 0x09)
_key("clear", 0x0C)
_key("enter return", 0x0D)
_key("shift", 0x10)
_key("ctrl control", 0x11)
_key("alt", 0x12)
_key("pause break", 0x13)
_key("capslock", 0x14)
_key("esc escape", 0x1B)
_key("space spacebar", 0x20)
_key("pageup pgup", 0x21)
_key("pagedown pgdn", 0x22)
_key("end", 0x23)
_key("home", 0x24)
_key("left arrowleft", 0x25)
_key("up arrowup", 0x26)
_key("right arrowright", 0x27)
_key("down arrowdown", 0x28)
_key("printscreen prtsc", 0x2C)
_key("insert ins", 0x2D)
_key("delete del", 0x2E)
for _d in range(10):
    _key(str(_d), 0x30 + _d)
    _key(f"num{_d} numpad{_d}", 0x60 + _d)
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _key(_c, 0x41 + _i)
_key("win lwin windows", 0x5B)
_key("rwin", 0x5C)
_key("apps contextmenu", 0x5D)
_key("multiply", 0x6A)
_key("add", 0x6B)
_key("separator", 0x6C)
_key("subtract", 0x6D)
_key("decimal", 0x6E)
_key("divide", 0x6F)
for _i in range(1, 25):
    _key(f"f{_i}", 0x6F + _i)
_key("numlock", 0x90)
_key("scrolllock", 0x91)
_key("lshift", 0xA0)
_key("rshift", 0xA1)
_key("lctrl", 0xA2)
_key("rctrl", 0xA3)
_key("lalt", 0xA4)
_key("ralt", 0xA5)
_key("volumemute", 0xAD)
_key("volumedown", 0xAE)
_key("volumeup", 0xAF)
_key("nexttrack", 0xB0)
_key("prevtrack", 0xB1)
_key("mediastop", 0xB2)
_key("playpause", 0xB3)
_key("semicolon ;", 0xBA)
_key("equals equal = plus", 0xBB)   # the "=/+" key; numpad plus is "add"
_key("comma ,", 0xBC)
_key("minus -", 0xBD)
_key("period dot .", 0xBE)
_key("slash /", 0xBF)
_key("backquote grave tilde `", 0xC0)
_key("bracketleft [", 0xDB)
_key("backslash \\", 0xDC)
_key("bracketright ]", 0xDD)
_key("quote apostrophe '", 0xDE)

MODIFIER_VKS = frozenset({0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5})
EXTENDED_VKS = frozenset({
    0x03, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D,
    0x6F, 0x90, 0xA3, 0xA5, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3,
})


def parse_combo(combo: str) -> list[tuple[str, int]]:
    """'ctrl+shift+s' -> [('ctrl', 0x11), ('shift', 0x10), ('s', 0x53)]. Keys are pressed in this order.

    '+' separates keys; the '+' key itself is 'plus' (or a trailing '++': 'ctrl++').
    """
    text = (combo or "").strip().lower()
    if not text:
        raise ValueError("empty key combo")
    if text == "+":
        names = ["plus"]
    elif text.endswith("++"):
        names = [t.strip() for t in text[:-2].split("+")] + ["plus"]
    else:
        names = [t.strip() for t in text.split("+")]
    if any(not n for n in names):
        raise ValueError(f"malformed key combo {combo!r} (use 'plus' for the + key)")
    out: list[tuple[str, int]] = []
    for n in names:
        vk = KEY_CODES.get(n)
        if vk is None:
            raise ValueError(f"unknown key {n!r} in {combo!r}; known: {', '.join(sorted(KEY_CODES))}")
        if any(vk == v for _, v in out):
            raise ValueError(f"key {n!r} repeated in {combo!r}")
        out.append((n, vk))
    return out


MOUSE_BUTTONS = {
    # name: (down flag, up flag, mouseData)
    "left": (0x0002, 0x0004, 0),
    "right": (0x0008, 0x0010, 0),
    "middle": (0x0020, 0x0040, 0),
    "x1": (0x0080, 0x0100, 1),
    "x2": (0x0080, 0x0100, 2),
}
_BUTTON_ALIASES = {"l": "left", "r": "right", "m": "middle", "back": "x1", "forward": "x2"}


def parse_button(name: str) -> tuple[int, int, int]:
    key = _BUTTON_ALIASES.get((name or "left").strip().lower(), (name or "left").strip().lower())
    if key not in MOUSE_BUTTONS:
        raise ValueError(f"unknown mouse button {name!r}; use left, right, middle, x1, x2")
    return MOUSE_BUTTONS[key]


# -- geometry ----------------------------------------------------------------

def scale_from_dpi(dpi: int) -> float:
    return round((dpi or 96) / 96.0, 4)


def virtual_screen(monitors: Sequence[dict]) -> dict:
    if not monitors:
        return {"left": 0, "top": 0, "width": 0, "height": 0}
    left = min(m["left"] for m in monitors)
    top = min(m["top"] for m in monitors)
    right = max(m["left"] + m["width"] for m in monitors)
    bottom = max(m["top"] + m["height"] for m in monitors)
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


def monitor_for_point(x: float, y: float, monitors: Sequence[dict]) -> Optional[dict]:
    for m in monitors:
        if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
            return m
    return None


def kill_switch_hit(cursor: Sequence[float], monitors: Sequence[dict], margin: int = KILL_SWITCH_MARGIN) -> bool:
    """True when the cursor sits within `margin` px of the top-left corner of any monitor (or of the
    virtual desktop)."""
    x, y = cursor[0], cursor[1]
    corners = [(m["left"], m["top"]) for m in monitors]
    if monitors:
        vs = virtual_screen(monitors)
        corners.append((vs["left"], vs["top"]))
    return any(0 <= x - cx <= margin and 0 <= y - cy <= margin for cx, cy in corners)


def dip_to_physical(dx: float, dy: float, monitor: dict) -> tuple[int, int]:
    """Monitor-relative DIPs (96-dpi units) -> physical screen pixels."""
    s = monitor.get("scale") or 1.0
    return monitor["left"] + int(math.floor(dx * s)), monitor["top"] + int(math.floor(dy * s))


def physical_to_dip(x: float, y: float, monitor: dict) -> tuple[float, float]:
    s = monitor.get("scale") or 1.0
    return (x - monitor["left"]) / s, (y - monitor["top"]) / s


def plan_capture_size(width: int, height: int, max_long_edge: int = DEFAULT_MAX_LONG_EDGE,
                      max_pixels: int = DEFAULT_MAX_PIXELS, full_resolution: bool = False) -> tuple[int, int]:
    """Output size for a capture of width x height: downscale (never upscale) to fit both limits."""
    if full_resolution or width <= 0 or height <= 0:
        return max(width, 0), max(height, 0)
    s = 1.0
    if max_long_edge and max(width, height) > max_long_edge:
        s = min(s, max_long_edge / max(width, height))
    if max_pixels and width * height * s * s > max_pixels:
        s = min(s, math.sqrt(max_pixels / float(width * height)))
    return max(1, int(width * s)), max(1, int(height * s))


def make_mapping(left: int, top: int, width: int, height: int, out_w: int, out_h: int) -> dict:
    return {
        "origin_x": left, "origin_y": top,
        "source_width": width, "source_height": height,
        "image_width": out_w, "image_height": out_h,
        "scale_x": width / float(out_w) if out_w else 1.0,
        "scale_y": height / float(out_h) if out_h else 1.0,
        "formula": "screen = origin + floor((image + 0.5) * scale), per axis",
    }


def image_to_screen(px: float, py: float, mapping: dict) -> tuple[int, int]:
    """Pixel of a (possibly downscaled) capture -> the physical screen pixel under its center."""
    sx = int(math.floor((float(px) + 0.5) * mapping["scale_x"]))
    sy = int(math.floor((float(py) + 0.5) * mapping["scale_y"]))
    sx = min(max(sx, 0), int(mapping["source_width"]) - 1)
    sy = min(max(sy, 0), int(mapping["source_height"]) - 1)
    return int(mapping["origin_x"]) + sx, int(mapping["origin_y"]) + sy


def screen_to_image(sx: float, sy: float, mapping: dict) -> tuple[int, int]:
    px = int(math.floor((float(sx) - mapping["origin_x"] + 0.5) / mapping["scale_x"]))
    py = int(math.floor((float(sy) - mapping["origin_y"] + 0.5) / mapping["scale_y"]))
    return px, py


def normalized_absolute(x: int, y: int, vscreen: dict) -> tuple[int, int]:
    """Screen pixel -> SendInput MOUSEEVENTF_ABSOLUTE|VIRTUALDESK coordinates (0..65535), aimed at
    the pixel center so Windows' floor(n * size / 65536) lands on exactly (x, y)."""
    w, h = max(int(vscreen["width"]), 1), max(int(vscreen["height"]), 1)
    nx = ((int(x) - int(vscreen["left"])) * 2 + 1) * 65536 // (2 * w)
    ny = ((int(y) - int(vscreen["top"])) * 2 + 1) * 65536 // (2 * h)
    return min(max(nx, 0), 65535), min(max(ny, 0), 65535)


def rect_intersection(a: dict, b: dict) -> Optional[dict]:
    left, top = max(a["x"], b["x"]), max(a["y"], b["y"])
    right = min(a["x"] + a["width"], b["x"] + b["width"])
    bottom = min(a["y"] + a["height"], b["y"] + b["height"])
    if right <= left or bottom <= top:
        return None
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


# -- sensitive text ----------------------------------------------------------

_SENSITIVE_CONTEXT = re.compile(
    r"pass(?:word|wd|code|phrase)?\b|sign[\s_-]?in|log[\s_-]?in|credential|two[\s_-]?factor|\b2fa\b|"
    r"\botp\b|verification|secret|token|\bauth", re.IGNORECASE)


def looks_sensitive(text: str, window_title: str = "", class_name: str = "", process: str = "",
                    password_control: bool = False, forced: bool = False) -> bool:
    """Whether typed text must be redacted in the audit log."""
    if forced or password_control:
        return True
    if normalize_process_name(process) == "epicgameslauncher":
        return True
    if _SENSITIVE_CONTEXT.search(f"{window_title} {class_name}"):
        return True
    t = (text or "").strip()
    if len(t) >= 8 and not re.search(r"\s", t):
        classes = sum(bool(re.search(p, t)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
        if classes >= 3:
            return True
        if len(t) >= 20 and re.fullmatch(r"[A-Za-z0-9+/=_.\-]+", t):
            return True
    return False


# -- PNG ---------------------------------------------------------------------

def encode_png(width: int, height: int, rgb: bytes, level: int = 6) -> bytes:
    """Minimal RGB8 PNG encoder (filter 0 on every row)."""
    stride = width * 3
    if len(rgb) != stride * height:
        raise ValueError(f"rgb buffer is {len(rgb)} bytes, expected {stride * height}")
    raw = bytearray((stride + 1) * height)
    for y in range(height):
        o = y * (stride + 1)
        raw[o + 1:o + 1 + stride] = rgb[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), level)) + chunk(b"IEND", b""))


def bgra_to_rgb(bgra: bytes, width: int, height: int) -> bytes:
    n = width * height
    rgb = bytearray(n * 3)
    rgb[0::3] = bgra[2:n * 4:4]
    rgb[1::3] = bgra[1:n * 4:4]
    rgb[2::3] = bgra[0:n * 4:4]
    return bytes(rgb)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

_audit_logger: Optional[logging.Logger] = None


def log_path() -> str:
    return os.environ.get(LOG_ENV) or os.path.join(tempfile.gettempdir(), "uefn-mcp", "desktop_control.log")


def _logger() -> logging.Logger:
    global _audit_logger
    if _audit_logger is None:
        path = log_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        lg = logging.getLogger("uefn_mcp.desktop_audit")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if not lg.handlers:
            h = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(message)s"))
            lg.addHandler(h)
        _audit_logger = lg
    return _audit_logger


def audit(tool: str, outcome: str, **fields: Any) -> None:
    now = time.time()
    rec = {"time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}",
           "tool": tool, "outcome": outcome, "server_pid": os.getpid()}
    rec.update({k: v for k, v in fields.items() if v is not None})
    try:
        _logger().info(json.dumps(rec, ensure_ascii=False, default=str))
    except Exception:
        pass


class _ActionLog:
    """Context manager: one audit line per acting call (ok / refused / error)."""

    def __init__(self, tool: str):
        self.tool = tool
        self.fields: dict[str, Any] = {}
        self.t0 = time.perf_counter()

    def __enter__(self) -> "_ActionLog":
        return self

    def __exit__(self, et, ev, tb) -> bool:
        ms = int((time.perf_counter() - self.t0) * 1000)
        if et is None:
            audit(self.tool, "ok", ms=ms, **self.fields)
        elif issubclass(et, DesktopRefused):
            audit(self.tool, "refused", reason=str(ev), ms=ms, **self.fields)
        else:
            audit(self.tool, "error", reason=f"{et.__name__}: {ev}", ms=ms, **self.fields)
        return False


# ---------------------------------------------------------------------------
# Win32 layer
# ---------------------------------------------------------------------------

if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:  # pragma: no cover
        dwmapi = None
    try:
        shcore = ctypes.WinDLL("shcore")
    except OSError:  # pragma: no cover
        shcore = None

    ULONG_PTR = ctypes.c_size_t
    HRESULT = ctypes.c_long

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                    ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ULONG_PTR), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                         ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def _proto(dll, name, restype, *argtypes):
        fn = getattr(dll, name)
        fn.restype = restype
        fn.argtypes = list(argtypes)
        return fn

    H, B, D, I, U = wintypes.HWND, wintypes.BOOL, wintypes.DWORD, ctypes.c_int, wintypes.UINT
    _SendInput = _proto(user32, "SendInput", U, U, ctypes.POINTER(INPUT), I)
    _GetForegroundWindow = _proto(user32, "GetForegroundWindow", H)
    _SetForegroundWindow = _proto(user32, "SetForegroundWindow", B, H)
    _GetWindowThreadProcessId = _proto(user32, "GetWindowThreadProcessId", D, H, ctypes.POINTER(D))
    _GetWindowTextLengthW = _proto(user32, "GetWindowTextLengthW", I, H)
    _GetWindowTextW = _proto(user32, "GetWindowTextW", I, H, wintypes.LPWSTR, I)
    _GetClassNameW = _proto(user32, "GetClassNameW", I, H, wintypes.LPWSTR, I)
    _IsWindow = _proto(user32, "IsWindow", B, H)
    _IsWindowVisible = _proto(user32, "IsWindowVisible", B, H)
    _IsWindowEnabled = _proto(user32, "IsWindowEnabled", B, H)
    _IsIconic = _proto(user32, "IsIconic", B, H)
    _IsZoomed = _proto(user32, "IsZoomed", B, H)
    _GetWindowRect = _proto(user32, "GetWindowRect", B, H, ctypes.POINTER(wintypes.RECT))
    _GetClientRect = _proto(user32, "GetClientRect", B, H, ctypes.POINTER(wintypes.RECT))
    _ClientToScreen = _proto(user32, "ClientToScreen", B, H, ctypes.POINTER(wintypes.POINT))
    _EnumWindows = _proto(user32, "EnumWindows", B, WNDENUMPROC, wintypes.LPARAM)
    _GetWindow = _proto(user32, "GetWindow", H, H, U)
    _GetAncestor = _proto(user32, "GetAncestor", H, H, U)
    _WindowFromPoint = _proto(user32, "WindowFromPoint", H, wintypes.POINT)
    _GetCursorPos = _proto(user32, "GetCursorPos", B, ctypes.POINTER(wintypes.POINT))
    _SetCursorPos = _proto(user32, "SetCursorPos", B, I, I)
    _ShowWindow = _proto(user32, "ShowWindow", B, H, I)
    _BringWindowToTop = _proto(user32, "BringWindowToTop", B, H)
    _SetFocus = _proto(user32, "SetFocus", H, H)
    _AttachThreadInput = _proto(user32, "AttachThreadInput", B, D, D, B)
    _SwitchToThisWindow = _proto(user32, "SwitchToThisWindow", None, H, B)
    _PostMessageW = _proto(user32, "PostMessageW", B, H, U, wintypes.WPARAM, wintypes.LPARAM)
    _MonitorFromWindow = _proto(user32, "MonitorFromWindow", wintypes.HMONITOR, H, D)
    _EnumDisplayMonitors = _proto(user32, "EnumDisplayMonitors", B, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                  MONITORENUMPROC, wintypes.LPARAM)
    _GetMonitorInfoW = _proto(user32, "GetMonitorInfoW", B, wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW))
    _GetDC = _proto(user32, "GetDC", wintypes.HDC, H)
    _ReleaseDC = _proto(user32, "ReleaseDC", I, H, wintypes.HDC)
    _MapVirtualKeyW = _proto(user32, "MapVirtualKeyW", U, U, U)
    _GetGUIThreadInfo = _proto(user32, "GetGUIThreadInfo", B, D, ctypes.POINTER(GUITHREADINFO))
    _GetDoubleClickTime = _proto(user32, "GetDoubleClickTime", U)
    _PeekMessageW = _proto(user32, "PeekMessageW", B, ctypes.POINTER(wintypes.MSG), H, U, U, U)
    _GetWindowLongPtrW = _proto(user32, "GetWindowLongPtrW" if ctypes.sizeof(ctypes.c_void_p) == 8
                                else "GetWindowLongW", ctypes.c_ssize_t, H, I)

    _GetCurrentThreadId = _proto(kernel32, "GetCurrentThreadId", D)
    _GetCurrentProcessId = _proto(kernel32, "GetCurrentProcessId", D)
    _OpenProcess = _proto(kernel32, "OpenProcess", wintypes.HANDLE, D, B, D)
    _CloseHandle = _proto(kernel32, "CloseHandle", B, wintypes.HANDLE)
    _QueryFullProcessImageNameW = _proto(kernel32, "QueryFullProcessImageNameW", B, wintypes.HANDLE, D,
                                         wintypes.LPWSTR, ctypes.POINTER(D))
    _GetProcessTimes = _proto(kernel32, "GetProcessTimes", B, wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                              ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                              ctypes.POINTER(wintypes.FILETIME))
    _CreateToolhelp32Snapshot = _proto(kernel32, "CreateToolhelp32Snapshot", wintypes.HANDLE, D, D)
    _Process32FirstW = _proto(kernel32, "Process32FirstW", B, wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    _Process32NextW = _proto(kernel32, "Process32NextW", B, wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))

    _OpenProcessToken = _proto(advapi32, "OpenProcessToken", B, wintypes.HANDLE, D, ctypes.POINTER(wintypes.HANDLE))
    _GetTokenInformation = _proto(advapi32, "GetTokenInformation", B, wintypes.HANDLE, I, ctypes.c_void_p, D,
                                  ctypes.POINTER(D))
    _GetSidSubAuthorityCount = _proto(advapi32, "GetSidSubAuthorityCount", ctypes.POINTER(ctypes.c_ubyte),
                                      ctypes.c_void_p)
    _GetSidSubAuthority = _proto(advapi32, "GetSidSubAuthority", ctypes.POINTER(D), ctypes.c_void_p, D)

    _CreateCompatibleDC = _proto(gdi32, "CreateCompatibleDC", wintypes.HDC, wintypes.HDC)
    _CreateCompatibleBitmap = _proto(gdi32, "CreateCompatibleBitmap", wintypes.HBITMAP, wintypes.HDC, I, I)
    _SelectObject = _proto(gdi32, "SelectObject", wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
    _BitBlt = _proto(gdi32, "BitBlt", B, wintypes.HDC, I, I, I, I, wintypes.HDC, I, I, D)
    _StretchBlt = _proto(gdi32, "StretchBlt", B, wintypes.HDC, I, I, I, I, wintypes.HDC, I, I, I, I, D)
    _SetStretchBltMode = _proto(gdi32, "SetStretchBltMode", I, wintypes.HDC, I)
    _SetBrushOrgEx = _proto(gdi32, "SetBrushOrgEx", B, wintypes.HDC, I, I, ctypes.POINTER(wintypes.POINT))
    _GetDIBits = _proto(gdi32, "GetDIBits", I, wintypes.HDC, wintypes.HBITMAP, U, U, ctypes.c_void_p,
                        ctypes.POINTER(BITMAPINFO), U)
    _DeleteObject = _proto(gdi32, "DeleteObject", B, wintypes.HGDIOBJ)
    _DeleteDC = _proto(gdi32, "DeleteDC", B, wintypes.HDC)

    if dwmapi is not None:
        _DwmGetWindowAttribute = _proto(dwmapi, "DwmGetWindowAttribute", HRESULT, H, D, ctypes.c_void_p, D)
    if shcore is not None:
        _GetDpiForMonitor = _proto(shcore, "GetDpiForMonitor", HRESULT, wintypes.HMONITOR, I,
                                   ctypes.POINTER(U), ctypes.POINTER(U))

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0001, 0x0800, 0x1000
MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x4000, 0x8000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
WHEEL_DELTA = 120
VK_RETURN, VK_TAB, VK_MENU = 0x0D, 0x09, 0x12
SW_RESTORE = 9
WM_CLOSE = 0x0010
GW_OWNER, GW_HWNDPREV = 4, 3
GA_ROOT, GA_ROOTOWNER = 2, 3
MONITOR_DEFAULTTONEAREST = 2
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_EX_TOPMOST, WS_EX_TOOLWINDOW = 0x0008, 0x0080
ES_PASSWORD = 0x0020
SRCCOPY, CAPTUREBLT, HALFTONE = 0x00CC0020, 0x40000000, 4
PROCESS_QUERY_LIMITED_INFORMATION, TOKEN_QUERY, TOKEN_INTEGRITY_LEVEL = 0x1000, 0x0008, 25

_dpi_mode = "not-windows"


def _init_dpi_awareness() -> str:
    """Per-monitor DPI awareness V2 for this process (before any geometry call)."""
    try:
        fn = user32.SetProcessDpiAwarenessContext
        fn.restype = wintypes.BOOL
        fn.argtypes = [ctypes.c_void_p]
        if fn(ctypes.c_void_p(-4)):
            return "per_monitor_v2"
    except AttributeError:
        pass
    try:
        if shcore is not None and shcore.SetProcessDpiAwareness(2) == 0:
            return "per_monitor"
    except Exception:
        pass
    try:
        if user32.SetProcessDPIAware():
            return "system"
    except Exception:
        pass
    return "unchanged"


def dpi_awareness() -> str:
    """The effective awareness of this thread: unaware / system / per_monitor(_v2)."""
    if not IS_WINDOWS:
        return "not-windows"
    try:
        get_ctx = user32.GetThreadDpiAwarenessContext
        get_ctx.restype = ctypes.c_void_p
        get_aw = user32.GetAwarenessFromDpiAwarenessContext
        get_aw.restype = ctypes.c_int
        get_aw.argtypes = [ctypes.c_void_p]
        eq = user32.AreDpiAwarenessContextsEqual
        eq.restype = wintypes.BOOL
        eq.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        ctx = get_ctx()
        if eq(ctx, ctypes.c_void_p(-4)):
            return "per_monitor_v2"
        return {0: "unaware", 1: "system", 2: "per_monitor"}.get(get_aw(ctx), "unknown")
    except AttributeError:
        return _dpi_mode


if IS_WINDOWS:
    _dpi_mode = _init_dpi_awareness()


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise DesktopError("desktop control is Windows-only")


# -- processes ----------------------------------------------------------------

_PROC_CACHE: dict[int, tuple[float, str]] = {}


def process_image(pid: int) -> str:
    """Full image path of a process ('' when it cannot be opened)."""
    now = time.monotonic()
    hit = _PROC_CACHE.get(pid)
    if hit and now - hit[0] < 5.0:
        return hit[1]
    path = ""
    h = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if h:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if _QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                path = buf.value
        finally:
            _CloseHandle(h)
    _PROC_CACHE[pid] = (now, path)
    return path


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
    _require_windows()
    snap = _CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        raise DesktopError(f"CreateToolhelp32Snapshot failed ({ctypes.get_last_error()})")
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


def integrity_rid(pid: int) -> Optional[int]:
    """Mandatory integrity RID of a process (0x2000 medium, 0x3000 high, ...), None if unknown."""
    h = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        tok = wintypes.HANDLE()
        if not _OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            return None
        try:
            size = wintypes.DWORD(0)
            _GetTokenInformation(tok, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
            if not size.value:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if not _GetTokenInformation(tok, TOKEN_INTEGRITY_LEVEL, buf, size, ctypes.byref(size)):
                return None
            psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            count = _GetSidSubAuthorityCount(psid)[0]
            return int(_GetSidSubAuthority(psid, count - 1)[0])
        finally:
            _CloseHandle(tok)
    finally:
        _CloseHandle(h)


# -- monitors / cursor -----------------------------------------------------------

def list_monitors() -> list[dict]:
    """Monitors in EnumDisplayMonitors order (= mss.monitors[1:]), 1-based index, physical pixels."""
    _require_windows()
    mons: list[dict] = []

    def cb(hmon, hdc, lprc, lparam):
        try:
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            _GetMonitorInfoW(hmon, ctypes.byref(info))
            r, w = info.rcMonitor, info.rcWork
            dpi = 96
            if shcore is not None:
                dx, dy = wintypes.UINT(), wintypes.UINT()
                if _GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
                    dpi = int(dx.value)
            mons.append({
                "index": len(mons) + 1, "left": r.left, "top": r.top,
                "width": r.right - r.left, "height": r.bottom - r.top,
                "primary": bool(info.dwFlags & 1), "device": info.szDevice,
                "dpi": dpi, "scale": scale_from_dpi(dpi),
                "work_area": {"x": w.left, "y": w.top, "width": w.right - w.left, "height": w.bottom - w.top},
                "_handle": int(hmon or 0),
            })
        except Exception:
            pass
        return True

    proc = MONITORENUMPROC(cb)
    _EnumDisplayMonitors(None, None, proc, 0)
    return mons


def public_monitors(monitors: Sequence[dict]) -> list[dict]:
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in monitors]


def cursor_pos() -> tuple[int, int]:
    _require_windows()
    pt = wintypes.POINT()
    if not _GetCursorPos(ctypes.byref(pt)):
        raise DesktopError(f"GetCursorPos failed ({ctypes.get_last_error()})")
    return pt.x, pt.y


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


def _pid_tid(hwnd: int) -> tuple[int, int]:
    pid = wintypes.DWORD()
    tid = _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value), int(tid)


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


def window_info(hwnd: int, monitors: Optional[Sequence[dict]] = None, fg: Optional[int] = None) -> dict:
    hwnd = _hwnd(hwnd)
    pid, tid = _pid_tid(hwnd)
    image = process_image(pid)
    left, top, right, bottom = _frame_rect(hwnd)
    exstyle = _GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    info = {
        "hwnd": hwnd, "title": _window_text(hwnd), "class": _class_name(hwnd),
        "pid": pid, "tid": tid, "process": os.path.basename(image) if image else "",
        "rect": {"x": left, "y": top, "width": right - left, "height": bottom - top},
        "visible": bool(_IsWindowVisible(hwnd)), "minimized": bool(_IsIconic(hwnd)),
        "maximized": bool(_IsZoomed(hwnd)), "enabled": bool(_IsWindowEnabled(hwnd)),
        "cloaked": _cloaked(hwnd), "owner": _hwnd(_GetWindow(hwnd, GW_OWNER)),
        "topmost": bool(exstyle & WS_EX_TOPMOST), "tool_window": bool(exstyle & WS_EX_TOOLWINDOW),
    }
    if fg is not None:
        info["foreground"] = hwnd == fg
    if monitors:
        hmon = _hwnd(_MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST))
        info["monitor"] = next((m["index"] for m in monitors if m.get("_handle") == hmon), None)
    info["input_allowed"] = is_allowed(info["process"])
    return info


def enum_windows(include_hidden: bool = False, monitors: Optional[Sequence[dict]] = None) -> list[dict]:
    """Top-level windows in z-order (topmost first)."""
    _require_windows()
    hwnds: list[int] = []

    def cb(h, lparam):
        hwnds.append(_hwnd(h))
        return True

    proc = WNDENUMPROC(cb)
    _EnumWindows(proc, 0)
    fg = _hwnd(_GetForegroundWindow())
    out = []
    for z, h in enumerate(hwnds):
        if not include_hidden and not _IsWindowVisible(h):
            continue
        try:
            info = window_info(h, monitors, fg)
        except Exception:
            continue
        if not include_hidden and (info["cloaked"] or info["rect"]["width"] <= 0 or info["rect"]["height"] <= 0):
            continue
        info["z"] = z
        out.append(info)
    return out


def find_windows(process: str = "", title: str = "", class_name: str = "", include_hidden: bool = False,
                 monitors: Optional[Sequence[dict]] = None) -> list[dict]:
    t, c = (title or "").lower(), (class_name or "").lower()
    return [w for w in enum_windows(include_hidden, monitors)
            if (not process or process_matches(w["process"], process))
            and (not t or t in w["title"].lower())
            and (not c or c == w["class"].lower() or (any(ch in c for ch in "*?[") and fnmatch.fnmatchcase(w["class"].lower(), c)))]


def pick_main_window(windows: Sequence[dict]) -> Optional[dict]:
    """Best target among matches: foreground, visible, unowned (the app's main window, even when minimized, beats
    its floating panels), not a tool window, restored, largest, topmost in z-order."""
    if not windows:
        return None
    return sorted(windows, key=lambda w: (
        not w.get("foreground", False), not w["visible"], w["cloaked"], bool(w["owner"]), w["tool_window"],
        w["minimized"], -(w["rect"]["width"] * w["rect"]["height"]), w.get("z", 0)))[0]


def foreground_window(monitors: Optional[Sequence[dict]] = None) -> Optional[dict]:
    h = _hwnd(_GetForegroundWindow())
    return window_info(h, monitors, h) if h else None


def root_window_at(x: int, y: int, monitors: Optional[Sequence[dict]] = None) -> Optional[dict]:
    h = _hwnd(_WindowFromPoint(wintypes.POINT(int(x), int(y))))
    if not h:
        return None
    root = _hwnd(_GetAncestor(h, GA_ROOT)) or h
    return window_info(root, monitors, _hwnd(_GetForegroundWindow()))


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


def _owned_by(hwnd: int, owner: int) -> bool:
    h, seen = hwnd, 0
    while h and seen < 16:
        if h == owner:
            return True
        h, seen = _hwnd(_GetWindow(h, GW_OWNER)), seen + 1
    return False


def occluders(win: dict, limit: int = 5) -> list[dict]:
    """Visible top-level windows above `win` in z-order that overlap it (other processes only)."""
    out, h, steps = [], win["hwnd"], 0
    while len(out) < limit and steps < 500:
        h, steps = _hwnd(_GetWindow(h, GW_HWNDPREV)), steps + 1
        if not h:
            break
        if not _IsWindowVisible(h) or _IsIconic(h) or _cloaked(h):
            continue
        pid, _ = _pid_tid(h)
        if pid == win["pid"]:
            continue
        left, top, right, bottom = _frame_rect(h)
        inter = rect_intersection(win["rect"], {"x": left, "y": top, "width": right - left, "height": bottom - top})
        if inter:
            out.append({"hwnd": h, "process": os.path.basename(process_image(pid)), "title": _window_text(h)[:80],
                        "overlap": inter})
    return out


# -- focus -------------------------------------------------------------------------

def _ensure_queue() -> None:
    msg = wintypes.MSG()
    _PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # PM_NOREMOVE: creates this thread's message queue


def _fg_ok(target: dict) -> bool:
    """The foreground is the target, or shares its root owner (popup menus, owned dialogs)."""
    fg = _hwnd(_GetForegroundWindow())
    if not fg:
        return False
    hwnd = target["hwnd"]
    if fg == hwnd or _owned_by(fg, hwnd):
        return True
    root_t = _hwnd(_GetAncestor(hwnd, GA_ROOTOWNER)) or hwnd
    root_f = _hwnd(_GetAncestor(fg, GA_ROOTOWNER)) or fg
    if root_t == root_f:
        return True
    # A modal dialog of the same process disables the target and takes the foreground.
    pid, _ = _pid_tid(fg)
    return pid == target["pid"] and not _IsWindowEnabled(hwnd)


def _wait_fg(target: dict, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while True:
        if _fg_ok(target):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.03)


def _tap_alt() -> None:
    down = _key_input(VK_MENU, False)
    up = _key_input(VK_MENU, True)
    arr = (INPUT * 1)(down)
    _SendInput(1, arr, ctypes.sizeof(INPUT))
    arr = (INPUT * 1)(up)
    _SendInput(1, arr, ctypes.sizeof(INPUT))


def focus_window(target: dict, timeout: float = 3.0) -> dict:
    """Bring `target` to the foreground despite Windows' foreground lock.

    Order: restore if minimized; SetForegroundWindow; AttachThreadInput to the foreground and target
    threads + SetForegroundWindow/BringWindowToTop; SwitchToThisWindow; last resort the ALT tap
    (an injected ALT press makes this process the last input source, which lifts the lock).
    """
    hwnd = target["hwnd"]
    if not _IsWindow(hwnd):
        raise DesktopError(f"window {hwnd} no longer exists")
    steps: list[str] = []
    if _IsIconic(hwnd):
        _ShowWindow(hwnd, SW_RESTORE)
        steps.append("restore")
        time.sleep(0.15)
    if _fg_ok(target):
        return {"ok": True, "method": steps[-1] if steps else "already_foreground", "steps": steps}
    _ensure_queue()
    slice_ = max(0.25, timeout / 4.0)

    _SetForegroundWindow(hwnd)
    steps.append("set_foreground")
    if _wait_fg(target, slice_):
        return {"ok": True, "method": "set_foreground", "steps": steps}

    me = _GetCurrentThreadId()
    fg = _hwnd(_GetForegroundWindow())
    fg_tid = _pid_tid(fg)[1] if fg else 0
    attached = []
    for tid in {fg_tid, target["tid"]} - {0, me}:
        if _AttachThreadInput(me, tid, True):
            attached.append(tid)
    try:
        _BringWindowToTop(hwnd)
        _SetForegroundWindow(hwnd)
        _SetFocus(hwnd)
    finally:
        for tid in attached:
            _AttachThreadInput(me, tid, False)
    steps.append("attach_thread_input")
    if _wait_fg(target, slice_):
        return {"ok": True, "method": "attach_thread_input", "steps": steps}

    _SwitchToThisWindow(hwnd, True)
    steps.append("switch_to_this_window")
    if _wait_fg(target, slice_):
        return {"ok": True, "method": "switch_to_this_window", "steps": steps}

    _tap_alt()
    _SetForegroundWindow(hwnd)
    steps.append("alt_tap")
    ok = _wait_fg(target, slice_)
    return {"ok": ok, "method": "alt_tap" if ok else "failed", "steps": steps}


# -- input -------------------------------------------------------------------------

def _key_input(vk: int, up: bool) -> "INPUT":
    scan = _MapVirtualKeyW(vk, 0) & 0xFF
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=SIGNATURE)
    return inp


def _unicode_input(unit: int, up: bool) -> "INPUT":
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=0, wScan=unit, dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), time=0,
                        dwExtraInfo=SIGNATURE)
    return inp


def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> "INPUT":
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0, dwExtraInfo=SIGNATURE)
    return inp


def _send(inputs: Sequence["INPUT"]) -> None:
    if not inputs:
        return
    arr = (INPUT * len(inputs))(*inputs)
    sent = _SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise DesktopError(f"SendInput inserted {sent}/{len(inputs)} events (error {ctypes.get_last_error()}); "
                           "input may be blocked by a secure desktop (UAC, lock screen)")


def _move_cursor(x: int, y: int, monitors: Sequence[dict]) -> None:
    nx, ny = normalized_absolute(x, y, virtual_screen(monitors))
    _send([_mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny)])
    if cursor_pos() != (x, y):
        _SetCursorPos(int(x), int(y))
    got = cursor_pos()
    if abs(got[0] - x) > 1 or abs(got[1] - y) > 1:
        raise DesktopError(f"cursor could not be placed at ({x}, {y}); it is at {got} "
                           "(an app may be clipping the cursor, e.g. the Fortnite client in gameplay)")


# ---------------------------------------------------------------------------
# Operations (used by the MCP tools and by uefn_session)
# ---------------------------------------------------------------------------

_shots: dict[str, dict] = {}
_shot_counter = itertools.count(1)


def shots_dir() -> str:
    return os.environ.get(SHOTS_ENV) or os.path.join(tempfile.gettempdir(), "uefn-mcp", "shots")


def _cleanup_shots(folder: str, keep_seconds: float = 2 * 86400, max_files: int = 300) -> None:
    try:
        files = [os.path.join(folder, f) for f in os.listdir(folder)]
        if len(files) <= max_files:
            return
        cutoff = time.time() - keep_seconds
        for f in files:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
    except OSError:
        pass


def capture_rgb(left: int, top: int, width: int, height: int, out_w: int, out_h: int) -> bytes:
    """GDI capture of a physical screen rect, optionally downscaled (HALFTONE) to out_w x out_h."""
    _require_windows()
    screen = _GetDC(None)
    if not screen:
        raise DesktopError("GetDC failed (no interactive desktop?)")
    mem = _CreateCompatibleDC(screen)
    bmp = _CreateCompatibleBitmap(screen, out_w, out_h)
    old = _SelectObject(mem, bmp)
    try:
        if (out_w, out_h) == (width, height):
            ok = _BitBlt(mem, 0, 0, width, height, screen, left, top, SRCCOPY | CAPTUREBLT)
        else:
            _SetStretchBltMode(mem, HALFTONE)
            _SetBrushOrgEx(mem, 0, 0, None)
            ok = _StretchBlt(mem, 0, 0, out_w, out_h, screen, left, top, width, height, SRCCOPY | CAPTUREBLT)
        if not ok:
            raise DesktopError(f"screen copy failed ({ctypes.get_last_error()}); a secure desktop may be active")
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = out_w, -out_h
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount, bmi.bmiHeader.biCompression = 1, 32, 0
        buf = ctypes.create_string_buffer(out_w * out_h * 4)
        if _GetDIBits(mem, bmp, 0, out_h, buf, ctypes.byref(bmi), 0) != out_h:
            raise DesktopError("GetDIBits failed")
        return bgra_to_rgb(buf.raw, out_w, out_h)
    finally:
        _SelectObject(mem, old)
        _DeleteObject(bmp)
        _DeleteDC(mem)
        _ReleaseDC(None, screen)


def _write_file_atomic(path: str, data: bytes) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def screenshot(monitor: int = 1, process: str = "", title: str = "", class_name: str = "",
               region: Optional[Sequence[int]] = None, output_path: str = "",
               max_long_edge: int = DEFAULT_MAX_LONG_EDGE, max_pixels: int = DEFAULT_MAX_PIXELS,
               full_resolution: bool = False) -> dict:
    """Capture a region, a window or a monitor (0 = the whole virtual desktop) to PNG + mapping sidecar."""
    _require_windows()
    monitors = list_monitors()
    vs = virtual_screen(monitors)
    window, occl = None, []
    if region:
        if len(region) != 4:
            raise ValueError("region must be [x, y, width, height] in screen pixels")
        left, top, width, height = (int(v) for v in region)
        kind = "region"
    elif process or title or class_name:
        window = pick_main_window(find_windows(process, title, class_name, monitors=monitors))
        if not window:
            raise DesktopError(f"no visible window matches process={process!r} title={title!r} class={class_name!r}")
        if window["minimized"]:
            raise DesktopError(f"window '{window['title']}' is minimized; call desktop_focus_window first")
        r = window["rect"]
        if window["maximized"] and window.get("monitor"):
            # A maximized window's rect overhangs its monitor by the border; keep its monitor's work area.
            wa = monitors[window["monitor"] - 1]["work_area"]
            r = rect_intersection(r, wa) or r
        left, top, width, height = r["x"], r["y"], r["width"], r["height"]
        occl = occluders(window)
        kind = "window"
    elif monitor == 0:
        left, top, width, height = vs["left"], vs["top"], vs["width"], vs["height"]
        kind = "virtual_screen"
    else:
        if monitor < 1 or monitor > len(monitors):
            raise ValueError(f"monitor {monitor} does not exist; available: 0 (all) and 1..{len(monitors)}")
        m = monitors[monitor - 1]
        left, top, width, height = m["left"], m["top"], m["width"], m["height"]
        kind = "monitor"
    inter = rect_intersection({"x": left, "y": top, "width": width, "height": height},
                              {"x": vs["left"], "y": vs["top"], "width": vs["width"], "height": vs["height"]})
    if not inter:
        raise DesktopError(f"capture rect {left},{top} {width}x{height} is outside the desktop")
    left, top, width, height = inter["x"], inter["y"], inter["width"], inter["height"]
    out_w, out_h = plan_capture_size(width, height, max_long_edge, max_pixels, full_resolution)
    rgb = capture_rgb(left, top, width, height, out_w, out_h)
    shot_id = f"shot-{time.strftime('%Y%m%d-%H%M%S')}-{next(_shot_counter)}"
    folder = shots_dir()
    path = os.path.abspath(output_path) if output_path else os.path.join(folder, f"{shot_id}.png")
    _write_file_atomic(path, encode_png(out_w, out_h, rgb, 6 if out_w * out_h <= 2_500_000 else 3))
    mapping = make_mapping(left, top, width, height, out_w, out_h)
    record = {"shot_id": shot_id, "path": path, "kind": kind, "mapping": mapping, "time": time.time(),
              "monitor": (monitor_for_point(left, top, monitors) or {}).get("index"),
              "window": dict(brief(window), rect=window["rect"]) if window else None}
    _write_file_atomic(path + ".json", json.dumps(record, indent=2).encode("utf-8"))
    _shots[shot_id] = record
    if not output_path:
        _cleanup_shots(folder)
    cur = cursor_pos()
    result = {
        "path": path, "shot_id": shot_id, "width": out_w, "height": out_h, "kind": kind,
        "source": {"x": left, "y": top, "width": width, "height": height, "monitor": record["monitor"]},
        "mapping": mapping, "cursor": {"x": cur[0], "y": cur[1], "in_image": None},
        "dpi_awareness": dpi_awareness(),
    }
    cx, cy = screen_to_image(cur[0], cur[1], mapping)
    if 0 <= cx < out_w and 0 <= cy < out_h:
        result["cursor"]["in_image"] = {"x": cx, "y": cy}
    if window:
        result["window"] = record["window"]
        if occl:
            result["occluded_by"] = occl
    return result


def load_shot(shot: str) -> dict:
    """A capture record by shot_id, PNG path or sidecar path."""
    if shot in _shots:
        return _shots[shot]
    for candidate in (shot, shot + ".json", os.path.join(shots_dir(), shot + ".png.json")):
        if candidate.lower().endswith(".json") and os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8") as fh:
                rec = json.load(fh)
            _shots[rec.get("shot_id", shot)] = rec
            return rec
    raise DesktopError(f"unknown screenshot {shot!r}: pass the shot_id or the PNG path returned by desktop_screenshot")


def preflight(act: _ActionLog) -> dict:
    _require_windows()
    if os.environ.get(DISABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        raise DesktopRefused(f"desktop input is disabled ({DISABLE_ENV})")
    monitors = list_monitors()
    cur = cursor_pos()
    act.fields["cursor"] = cur
    if kill_switch_hit(cur, monitors):
        raise DesktopRefused(
            f"kill switch: the mouse cursor is at {cur}, in the top-left corner of a monitor. Move it out of "
            "that corner to allow desktop input again.")
    return {"monitors": monitors, "cursor": cur}


def _check_integrity(target: dict) -> None:
    mine = integrity_rid(_GetCurrentProcessId())
    theirs = integrity_rid(target["pid"])
    if mine is not None and theirs is not None and theirs > mine:
        raise DesktopRefused(
            f"'{target['process']}' runs at a higher integrity level (elevated); Windows would silently drop "
            "injected input (UIPI). Run it non-elevated, or run Claude Code elevated.")


def resolve_target(act: _ActionLog, monitors: Sequence[dict], process: str = "", title: str = "",
                   class_name: str = "", point: Optional[tuple[int, int]] = None,
                   shot: Optional[dict] = None) -> dict:
    """Target window: explicit filter > the screenshot's window > the window at the point > foreground."""
    target = None
    if process or title or class_name:
        target = pick_main_window(find_windows(process, title, class_name, monitors=monitors))
        if not target:
            raise DesktopRefused(f"no visible window matches process={process!r} title={title!r} "
                                 f"class={class_name!r}")
    elif shot and shot.get("window"):
        sw = shot["window"]
        if _IsWindow(sw["hwnd"]) and _pid_tid(sw["hwnd"])[0] == sw["pid"]:
            target = window_info(sw["hwnd"], monitors, _hwnd(_GetForegroundWindow()))
        else:
            target = pick_main_window([w for w in enum_windows(False, monitors) if w["pid"] == sw["pid"]])
            if not target:
                raise DesktopRefused(f"the screenshot's window ({sw['process']} '{sw['title']}') is gone")
    elif point is not None:
        target = root_window_at(point[0], point[1], monitors)
    else:
        target = foreground_window(monitors)
    if not target:
        raise DesktopRefused("no target window")
    act.fields["target"] = brief(target)
    if not is_allowed(target["process"]):
        raise DesktopRefused(
            f"target window '{target['title']}' belongs to '{target['process'] or 'unknown'}', which is not in the "
            f"desktop allowlist {allowlist()[0]} (set {ALLOW_ENV} to change it)")
    _check_integrity(target)
    return target


def bring_to_front(act: _ActionLog, target: dict, timeout: float = 3.0) -> dict:
    if _fg_ok(target) and not _IsIconic(target["hwnd"]):
        return {"ok": True, "method": "already_foreground", "steps": []}
    res = focus_window(target, timeout)
    act.fields["focus"] = res["method"]
    if not res["ok"]:
        fg = foreground_window()
        raise DesktopRefused(
            f"could not bring '{target['title']}' ({target['process']}) to the foreground; the foreground is "
            f"'{(fg or {}).get('title', '')}' ({(fg or {}).get('process', '')}). Windows focus-stealing protection "
            "can block this while another app has input; click the target once, or retry.")
    return res


def verify_foreground(target: dict) -> dict:
    """Right before sending input: the foreground window must be allowed and belong to the target."""
    fg = foreground_window()
    if not fg or not is_allowed(fg["process"]):
        raise DesktopRefused(
            f"refusing input: the foreground window is '{(fg or {}).get('title', '')}' "
            f"({(fg or {}).get('process', 'none')}), not an allowed process")
    if fg["pid"] != target["pid"]:
        raise DesktopRefused(
            f"refusing input: the foreground switched to '{fg['title']}' ({fg['process']}) instead of "
            f"'{target['title']}' ({target['process']})")
    return fg


def _verify_point(target: dict, x: int, y: int, monitors: Sequence[dict]) -> None:
    if not monitor_for_point(x, y, monitors):
        raise DesktopRefused(f"point ({x}, {y}) is not on any monitor")
    if kill_switch_hit((x, y), monitors):
        raise DesktopRefused(f"point ({x}, {y}) is inside the kill-switch corner; it is never clicked")
    w = root_window_at(x, y, monitors)
    if not w or not is_allowed(w["process"]):
        raise DesktopRefused(
            f"point ({x}, {y}) is covered by '{(w or {}).get('title', '')}' ({(w or {}).get('process', 'nothing')}), "
            "not an allowed window")
    if w["pid"] != target["pid"]:
        raise DesktopRefused(
            f"point ({x}, {y}) shows '{w['title']}' ({w['process']}), not the target '{target['title']}' "
            f"({target['process']})")


def resolve_point(x: float, y: float, monitors: Sequence[dict], shot: Optional[dict] = None,
                  relative_to: str = "screen", monitor: int = 1, target: Optional[dict] = None) -> tuple[int, int, dict]:
    """Caller coordinates -> physical screen pixel. Returns (x, y, info)."""
    info: dict[str, Any] = {"input": [x, y]}
    rel = (relative_to or "screen").lower()
    if shot:
        sx, sy = image_to_screen(x, y, shot["mapping"])
        info["space"] = "shot"
        sw = shot.get("window")
        if sw and target and target["hwnd"] == sw["hwnd"]:
            dx = target["rect"]["x"] - sw["rect"]["x"]
            dy = target["rect"]["y"] - sw["rect"]["y"]
            if dx or dy:
                sx, sy = sx + dx, sy + dy
                info["window_moved_by"] = [dx, dy]
        return sx, sy, info
    info["space"] = rel
    if rel == "screen":
        return int(round(x)), int(round(y)), info
    if rel in ("monitor", "monitor_dip"):
        if monitor < 1 or monitor > len(monitors):
            raise ValueError(f"monitor {monitor} does not exist (1..{len(monitors)})")
        m = monitors[monitor - 1]
        if rel == "monitor_dip":
            px, py = dip_to_physical(x, y, m)
            return px, py, info
        return m["left"] + int(round(x)), m["top"] + int(round(y)), info
    if rel in ("window", "client"):
        if not target:
            raise ValueError(f"relative_to='{rel}' needs a target window (process / title)")
        if rel == "window":
            return target["rect"]["x"] + int(round(x)), target["rect"]["y"] + int(round(y)), info
        pt = wintypes.POINT(int(round(x)), int(round(y)))
        _ClientToScreen(target["hwnd"], ctypes.byref(pt))
        return pt.x, pt.y, info
    raise ValueError("relative_to must be screen, monitor, monitor_dip, window or client")


EDITOR_PROCESS = "unrealeditorfortnite-win64-shipping"


def _is_editor_top_window(w: dict) -> bool:
    return normalize_process_name(w["process"]) == EDITOR_PROCESS and w["class"] == "UnrealWindow" and not w["owner"]


def _mods(modifiers: str) -> list[tuple[str, int]]:
    if not modifiers or not modifiers.strip():
        return []
    keys = parse_combo(modifiers)
    bad = [n for n, vk in keys if vk not in MODIFIER_VKS]
    if bad:
        raise ValueError(f"not modifier keys: {bad}")
    return keys


def op_focus(process: str = "", title: str = "", class_name: str = "", timeout: float = 3.0) -> dict:
    with _ActionLog("desktop_focus_window") as act:
        env = preflight(act)
        target = resolve_target(act, env["monitors"], process, title, class_name)
        res = bring_to_front(act, target, timeout)
        fg = verify_foreground(target)
        return {"ok": True, "method": res["method"], "steps": res["steps"], "target": brief(target),
                "foreground": brief(fg), "rect": window_info(target["hwnd"])["rect"]}


def _prepare_pointer(act: _ActionLog, tool_x: float, tool_y: float, shot: str, relative_to: str, monitor: int,
                     process: str, title: str) -> tuple[dict, dict, int, int, dict]:
    env = preflight(act)
    monitors = env["monitors"]
    rec = load_shot(shot) if shot else None
    pre = None
    if (relative_to or "screen").lower() in ("window", "client") or (rec and rec.get("window")) or process or title:
        pre = resolve_target(act, monitors, process, title, point=None, shot=rec)
    x, y, info = resolve_point(tool_x, tool_y, monitors, rec, relative_to, monitor, pre)
    target = pre or resolve_target(act, monitors, point=(x, y))
    act.fields["point"] = [x, y]
    return env, target, x, y, info


def op_click(x: float, y: float, shot: str = "", relative_to: str = "screen", monitor: int = 1,
             process: str = "", title: str = "", button: str = "left", double: bool = False,
             modifiers: str = "", hold_ms: int = 0) -> dict:
    down, up, data = parse_button(button)
    mods = _mods(modifiers)
    with _ActionLog("desktop_click") as act:
        act.fields.update(button=button, double=double, modifiers=modifiers or None)
        env, target, sx, sy, info = _prepare_pointer(act, x, y, shot, relative_to, monitor, process, title)
        bring_to_front(act, target)
        _verify_point(target, sx, sy, env["monitors"])
        _move_cursor(sx, sy, env["monitors"])
        time.sleep(0.04)
        verify_foreground(target)
        _verify_point(target, sx, sy, env["monitors"])
        pressed: list[int] = []
        try:
            for _, vk in mods:
                _send([_key_input(vk, False)])
                pressed.append(vk)
            gap = min(0.06, _GetDoubleClickTime() / 4000.0)
            for i in range(2 if double else 1):
                _send([_mouse_input(down, data=data)])
                if hold_ms > 0:
                    time.sleep(min(hold_ms, 5000) / 1000.0)
                _send([_mouse_input(up, data=data)])
                if double and i == 0:
                    time.sleep(gap)
        finally:
            for vk in reversed(pressed):
                _send([_key_input(vk, True)])
        return {"ok": True, "screen": {"x": sx, "y": sy}, "coords": info, "button": button, "double": double,
                "target": brief(target)}


def op_move(x: float, y: float, shot: str = "", relative_to: str = "screen", monitor: int = 1,
            process: str = "", title: str = "") -> dict:
    with _ActionLog("desktop_move") as act:
        env, target, sx, sy, info = _prepare_pointer(act, x, y, shot, relative_to, monitor, process, title)
        bring_to_front(act, target)
        _verify_point(target, sx, sy, env["monitors"])
        verify_foreground(target)
        _move_cursor(sx, sy, env["monitors"])
        return {"ok": True, "screen": {"x": sx, "y": sy}, "coords": info, "target": brief(target)}


def op_drag(x1: float, y1: float, x2: float, y2: float, shot: str = "", relative_to: str = "screen",
            monitor: int = 1, process: str = "", title: str = "", button: str = "left",
            duration_ms: int = 400) -> dict:
    down, up, data = parse_button(button)
    with _ActionLog("desktop_drag") as act:
        act.fields["button"] = button
        env, target, sx1, sy1, info1 = _prepare_pointer(act, x1, y1, shot, relative_to, monitor, process, title)
        rec = load_shot(shot) if shot else None
        sx2, sy2, _ = resolve_point(x2, y2, env["monitors"], rec, relative_to, monitor, target)
        act.fields["to"] = [sx2, sy2]
        bring_to_front(act, target)
        _verify_point(target, sx1, sy1, env["monitors"])
        _verify_point(target, sx2, sy2, env["monitors"])
        _move_cursor(sx1, sy1, env["monitors"])
        time.sleep(0.05)
        verify_foreground(target)
        duration = min(max(duration_ms, 50), 10000) / 1000.0
        steps = max(8, int(duration / 0.015))
        vs = virtual_screen(env["monitors"])
        _send([_mouse_input(down, data=data)])
        released = False
        try:
            time.sleep(0.08)
            prev = (sx1, sy1)
            for i in range(1, steps + 1):
                px = round(sx1 + (sx2 - sx1) * i / steps)
                py = round(sy1 + (sy2 - sy1) * i / steps)
                nx, ny = normalized_absolute(px, py, vs)
                _send([_mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny)])
                time.sleep(duration / steps)
                cur = cursor_pos()
                # The cursor may lag one step behind; anything farther means a human moved the mouse.
                tol = 8 + max(abs(px - prev[0]), abs(py - prev[1]))
                if abs(cur[0] - px) > tol or abs(cur[1] - py) > tol:
                    raise DesktopRefused(f"drag aborted: the mouse was moved by someone else (at {cur})")
                if kill_switch_hit(cur, env["monitors"]):
                    raise DesktopRefused("drag aborted: kill switch")
                prev = (px, py)
            _move_cursor(sx2, sy2, env["monitors"])
            time.sleep(0.05)
            verify_foreground(target)
            _send([_mouse_input(up, data=data)])
            released = True
        finally:
            if not released:
                _send([_mouse_input(up, data=data)])
        return {"ok": True, "from": {"x": sx1, "y": sy1}, "to": {"x": sx2, "y": sy2}, "coords": info1,
                "target": brief(target)}


def op_scroll(x: float, y: float, clicks: int, horizontal: bool = False, shot: str = "",
              relative_to: str = "screen", monitor: int = 1, process: str = "", title: str = "") -> dict:
    clicks = int(clicks)
    if not clicks or abs(clicks) > 50:
        raise ValueError("clicks must be between -50 and 50 and not 0 (positive = up / right)")
    with _ActionLog("desktop_scroll") as act:
        act.fields.update(clicks=clicks, horizontal=horizontal)
        env, target, sx, sy, info = _prepare_pointer(act, x, y, shot, relative_to, monitor, process, title)
        bring_to_front(act, target)
        _verify_point(target, sx, sy, env["monitors"])
        _move_cursor(sx, sy, env["monitors"])
        time.sleep(0.04)
        flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
        step = WHEEL_DELTA if clicks > 0 else -WHEEL_DELTA
        for _ in range(abs(clicks)):
            verify_foreground(target)
            _send([_mouse_input(flag, data=step)])
            time.sleep(0.025)
        return {"ok": True, "screen": {"x": sx, "y": sy}, "clicks": clicks, "horizontal": horizontal,
                "coords": info, "target": brief(target)}


def _focused_is_password(target: dict) -> bool:
    try:
        gti = GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not _GetGUIThreadInfo(target["tid"], ctypes.byref(gti)) or not gti.hwndFocus:
            return False
        style = _GetWindowLongPtrW(gti.hwndFocus, GWL_STYLE)
        return "edit" in _class_name(_hwnd(gti.hwndFocus)).lower() and bool(style & ES_PASSWORD)
    except Exception:
        return False


def op_type(text: str, process: str = "", title: str = "", interval_ms: int = 8, sensitive: bool = False) -> dict:
    if text is None or text == "":
        raise ValueError("text is empty")
    if len(text) > MAX_TEXT_LEN:
        raise ValueError(f"text is longer than {MAX_TEXT_LEN} characters; split it")
    with _ActionLog("desktop_type") as act:
        act.fields["text_len"] = len(text)
        env = preflight(act)
        target = resolve_target(act, env["monitors"], process, title)
        bring_to_front(act, target)
        verify_foreground(target)
        redact = looks_sensitive(text, target["title"], target["class"], target["process"],
                                 _focused_is_password(target), sensitive)
        act.fields["text"] = f"<redacted {len(text)} chars>" if redact else text[:200]
        units = 0
        chars = text.replace("\r\n", "\n")
        for i, ch in enumerate(chars):
            if i % 16 == 0:
                if kill_switch_hit(cursor_pos(), env["monitors"]):
                    raise DesktopRefused(f"typing aborted by the kill switch after {i} characters")
                verify_foreground(target)
            if ch == "\n":
                events = [_key_input(VK_RETURN, False), _key_input(VK_RETURN, True)]
            elif ch == "\t":
                events = [_key_input(VK_TAB, False), _key_input(VK_TAB, True)]
            else:
                data = ch.encode("utf-16-le")
                codes = [int.from_bytes(data[j:j + 2], "little") for j in range(0, len(data), 2)]
                events = [_unicode_input(c, False) for c in codes] + [_unicode_input(c, True) for c in codes]
            _send(events)
            units += 1
            if interval_ms > 0:
                time.sleep(min(interval_ms, 500) / 1000.0)
        return {"ok": True, "typed_chars": units, "redacted_in_log": redact, "target": brief(target)}


def op_key(keys: str, process: str = "", title: str = "", repeat: int = 1, interval_ms: int = 60,
           hold_ms: int = 30, allow_editor_close: bool = False) -> dict:
    combo = parse_combo(keys)
    repeat = max(1, min(int(repeat), 50))
    with _ActionLog("desktop_key") as act:
        act.fields.update(keys=keys, repeat=repeat)
        env = preflight(act)
        target = resolve_target(act, env["monitors"], process, title)
        vks = {vk for _, vk in combo}
        if (0x73 in vks and vks & {0x12, 0xA4, 0xA5} and normalize_process_name(target["process"]) == EDITOR_PROCESS
                and not allow_editor_close):
            raise DesktopRefused("alt+f4 on UEFN closes the editor window (the main one quits the editor); pass "
                                 "allow_editor_close=true only when the owner asked for it")
        bring_to_front(act, target)
        for r in range(repeat):
            if r and kill_switch_hit(cursor_pos(), env["monitors"]):
                raise DesktopRefused(f"key repeat aborted by the kill switch after {r} presses")
            verify_foreground(target)
            pressed: list[int] = []
            try:
                for _, vk in combo:
                    _send([_key_input(vk, False)])
                    pressed.append(vk)
                    if vk in MODIFIER_VKS:
                        time.sleep(0.01)
                if hold_ms > 0:
                    time.sleep(min(hold_ms, 5000) / 1000.0)
            finally:
                for vk in reversed(pressed):
                    _send([_key_input(vk, True)])
            if r + 1 < repeat and interval_ms > 0:
                time.sleep(min(interval_ms, 5000) / 1000.0)
        return {"ok": True, "keys": [n for n, _ in combo], "repeat": repeat, "target": brief(target)}


def op_close(process: str = "", title: str = "", class_name: str = "", all_matches: bool = False,
             wait_sec: float = 5.0, allow_editor_main: bool = False) -> dict:
    if not (process or title or class_name):
        raise ValueError("name the window to close: process and/or title and/or class_name")
    with _ActionLog("desktop_close_window") as act:
        env = preflight(act)
        wins = find_windows(process, title, class_name, monitors=env["monitors"])
        act.fields["matches"] = len(wins)
        if not wins:
            return {"ok": True, "closed": [], "note": "no visible window matches; nothing to close"}
        bad = [w for w in wins if not is_allowed(w["process"])]
        if bad:
            raise DesktopRefused(f"refusing to close windows of non-allowed processes: {[brief(w) for w in bad]}")
        if len(wins) > 1 and not all_matches:
            raise DesktopRefused(f"{len(wins)} windows match; narrow the filter or pass all_matches=true: "
                                 f"{[brief(w) for w in wins]}")
        for w in wins:
            if _is_editor_top_window(w) and not allow_editor_main:
                raise DesktopRefused("this is a top-level UEFN editor window (the main window or a floating tab): "
                                     "closing the main one quits the editor. Pass allow_editor_main=true only when "
                                     "the owner asked for it.")
        act.fields["target"] = [brief(w) for w in wins]
        for w in wins:
            _PostMessageW(w["hwnd"], WM_CLOSE, 0, 0)
        end = time.monotonic() + max(0.0, min(wait_sec, 60.0))
        pending = list(wins)
        while pending and time.monotonic() < end:
            time.sleep(0.2)
            pending = [w for w in pending if _IsWindow(w["hwnd"]) and _IsWindowVisible(w["hwnd"])]
        return {"ok": not pending, "closed": [brief(w) for w in wins if w not in pending],
                "still_open": [brief(w) for w in pending],
                "note": "still open: the app may be showing a confirmation; take a screenshot" if pending else ""}


async def op_wait_for_window(process: str = "", title: str = "", class_name: str = "", gone: bool = False,
                             timeout_sec: float = 60.0, interval_sec: float = 0.5) -> dict:
    _require_windows()
    if not (process or title or class_name):
        raise ValueError("give process and/or title and/or class_name")
    t0 = time.monotonic()
    end = t0 + max(0.0, min(timeout_sec, 1800.0))
    while True:
        wins = find_windows(process, title, class_name)
        if (wins and not gone) or (not wins and gone):
            return {"ok": True, "waited_sec": round(time.monotonic() - t0, 2), "gone": gone,
                    "windows": [dict(brief(w), rect=w["rect"], minimized=w["minimized"]) for w in wins[:10]]}
        if time.monotonic() >= end:
            return {"ok": False, "timeout": True, "waited_sec": round(time.monotonic() - t0, 2), "gone": gone,
                    "windows": [brief(w) for w in wins[:10]]}
        await asyncio.sleep(max(0.1, min(interval_sec, 10.0)))


def list_windows_report(process: str = "", title: str = "", class_name: str = "", include_hidden: bool = False,
                        limit: int = 60) -> dict:
    _require_windows()
    monitors = list_monitors()
    wins = find_windows(process, title, class_name, include_hidden, monitors)
    cur = cursor_pos()
    fg = foreground_window(monitors)
    patterns, disabled = allowlist()
    return {
        "count": len(wins), "windows": wins[:max(1, limit)], "truncated": max(0, len(wins) - limit),
        "foreground": brief(fg), "cursor": {"x": cur[0], "y": cur[1]},
        "kill_switch_active": kill_switch_hit(cur, monitors),
        "monitors": public_monitors(monitors), "virtual_screen": virtual_screen(monitors),
        "dpi_awareness": dpi_awareness(), "allowlist": patterns, "allowlist_disabled": disabled,
    }


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def register(mcp) -> None:
    """Attach the desktop_* tools to a FastMCP instance."""

    @mcp.tool()
    def desktop_list_windows(process: str = "", title: str = "", class_name: str = "",
                             include_hidden: bool = False, limit: int = 60) -> str:
        """List top-level windows (z-order, topmost first) with process, pid, class, rect in physical screen
        pixels, monitor, visible/minimized/foreground flags and whether desktop input may target them.
        Also returns the monitors (EnumDisplayMonitors order = desktop_screenshot's monitor index), the
        virtual desktop, the cursor, the kill-switch state and the input allowlist. Read-only.

        Args:
            process: Filter: process name, substring or glob (e.g. 'UnrealEditorFortnite', 'Fortnite*').
            title: Filter: case-insensitive title substring.
            class_name: Filter: window class (exact or glob), e.g. 'UnrealWindow'.
            include_hidden: Include invisible and cloaked windows.
            limit: Max windows returned.
        """
        return _dump(list_windows_report(process, title, class_name, include_hidden, limit))

    @mcp.tool()
    async def desktop_screenshot(monitor: int = 1, process: str = "", title: str = "", class_name: str = "",
                                 region: Optional[list[int]] = None, output_path: str = "",
                                 max_long_edge: int = DEFAULT_MAX_LONG_EDGE, max_pixels: int = DEFAULT_MAX_PIXELS,
                                 full_resolution: bool = False, count: int = 1, interval_sec: float = 1.0) -> str:
        """Capture the screen to PNG (GDI, no UEFN needed) and return its path plus the pixel mapping.

        Source, first match wins: `region` [x, y, width, height] in screen pixels; a window (`process` /
        `title` / `class_name`, captured as it appears on screen, so bring it to the front first); else
        `monitor` (1 = first monitor, 0 = all monitors). Read the PNG with the Read tool. Images are
        downscaled to at most `max_long_edge` px and `max_pixels` unless `full_resolution` (use a small
        `region` to zoom into details). Pass the returned `path` (or `shot_id`) as `shot` to
        desktop_click / desktop_move / desktop_drag / desktop_scroll to use image pixels directly
        (mapping: screen = origin + floor((image + 0.5) * scale)). `count` > 1 takes a burst every
        `interval_sec` (max 120 s), e.g. to record a Fortnite session at fixed moments. Read-only.
        """
        count = max(1, min(int(count), 100))
        if count > 1 and count * interval_sec > MAX_BURST_SECONDS:
            raise ValueError(f"burst longer than {MAX_BURST_SECONDS:.0f} s; lower count or interval_sec")
        shots = []
        for i in range(count):
            path = output_path
            if output_path and count > 1:
                root, ext = os.path.splitext(output_path)
                path = f"{root}_{i + 1:03d}{ext or '.png'}"
            shots.append(screenshot(monitor, process, title, class_name, region, path, max_long_edge, max_pixels,
                                    full_resolution))
            if i + 1 < count:
                await asyncio.sleep(max(0.05, interval_sec))
        return _dump(shots[0] if count == 1 else {"count": count, "shots": shots})

    @mcp.tool()
    def desktop_focus_window(process: str = "", title: str = "", class_name: str = "",
                             timeout_sec: float = 3.0) -> str:
        """Bring a window of an allowed process to the foreground (restores it if minimized) and verify it.

        Works around the Windows foreground lock: SetForegroundWindow, then AttachThreadInput, then
        SwitchToThisWindow, then an ALT tap. With no filter it acts on the current foreground window.
        Safety rails apply (allowlist, kill switch, audit log).
        """
        return _dump(op_focus(process, title, class_name, timeout_sec))

    @mcp.tool()
    def desktop_click(x: float, y: float, shot: str = "", relative_to: str = "screen", monitor: int = 1,
                      process: str = "", title: str = "", button: str = "left", double: bool = False,
                      modifiers: str = "", hold_ms: int = 0) -> str:
        """Click at a point of an allowed window with real mouse input (SendInput).

        Coordinates: with `shot` (a desktop_screenshot path or shot_id) x/y are pixels of that image;
        otherwise `relative_to` = screen (physical virtual-desktop pixels, may be negative), monitor
        (relative to monitor N's top-left), monitor_dip (monitor-relative 96-dpi units), window or
        client (relative to the target window). The target window (`process` / `title`, else the
        screenshot's window, else the window under the point) is focused first; the click is refused
        unless the foreground and the window under the point belong to an allowed process.

        Args:
            button: left | right | middle | x1 | x2.
            double: Double-click.
            modifiers: Held during the click, e.g. 'ctrl' or 'ctrl+shift'.
            hold_ms: Time between button down and up.
        """
        return _dump(op_click(x, y, shot, relative_to, monitor, process, title, button, double, modifiers, hold_ms))

    @mcp.tool()
    def desktop_move(x: float, y: float, shot: str = "", relative_to: str = "screen", monitor: int = 1,
                     process: str = "", title: str = "") -> str:
        """Move the mouse to a point (hover) over an allowed window. Same coordinates and rails as
        desktop_click."""
        return _dump(op_move(x, y, shot, relative_to, monitor, process, title))

    @mcp.tool()
    def desktop_drag(x1: float, y1: float, x2: float, y2: float, shot: str = "", relative_to: str = "screen",
                     monitor: int = 1, process: str = "", title: str = "", button: str = "left",
                     duration_ms: int = 400) -> str:
        """Press at (x1, y1), move in small steps to (x2, y2) over `duration_ms`, release. Both points must
        lie on the target window. Aborts (and releases the button) if the user moves the mouse or the
        kill switch triggers. Same coordinates and rails as desktop_click."""
        return _dump(op_drag(x1, y1, x2, y2, shot, relative_to, monitor, process, title, button, duration_ms))

    @mcp.tool()
    def desktop_scroll(x: float, y: float, clicks: int, horizontal: bool = False, shot: str = "",
                       relative_to: str = "screen", monitor: int = 1, process: str = "", title: str = "") -> str:
        """Scroll the mouse wheel over a point: `clicks` notches, positive = up (or right with
        `horizontal`), -50..50. Same coordinates and rails as desktop_click."""
        return _dump(op_scroll(x, y, clicks, horizontal, shot, relative_to, monitor, process, title))

    @mcp.tool()
    def desktop_type(text: str, process: str = "", title: str = "", interval_ms: int = 8,
                     sensitive: bool = False) -> str:
        """Type Unicode text into the focused control of an allowed window (KEYEVENTF_UNICODE, independent
        of the keyboard layout; newline = Enter, tab = Tab). The target (`process` / `title`, else the
        foreground window) is focused and re-checked every 16 characters. Pass sensitive=true for
        secrets: the audit log then records only the length (it also redacts password-like text and
        sign-in windows automatically). Max 4000 characters per call."""
        return _dump(op_type(text, process, title, interval_ms, sensitive))

    @mcp.tool()
    def desktop_key(keys: str, process: str = "", title: str = "", repeat: int = 1, interval_ms: int = 60,
                    hold_ms: int = 30, allow_editor_close: bool = False) -> str:
        """Press a key or combo in an allowed window: 'f5', 'ctrl+s', 'alt+f4', 'enter', 'esc',
        'ctrl+shift+p', 'ctrl+plus'. Names: a-z, 0-9, f1-f24, enter, esc, tab, space, backspace, delete,
        insert, home, end, pageup, pagedown, up/down/left/right, ctrl/shift/alt/win (+ l/r variants),
        num0-num9, add, subtract, multiply, divide, decimal, capslock, printscreen, apps, and
        punctuation (minus, equals/plus, comma, period, slash, backslash, semicolon, quote, backquote,
        bracketleft, bracketright). Keys go down in the given order and come up in reverse. The target
        (`process` / `title`, else the foreground window) is focused first; rails apply. alt+f4 on a
        UEFN editor window is refused unless allow_editor_close=true (owner's request only)."""
        return _dump(op_key(keys, process, title, repeat, interval_ms, hold_ms, allow_editor_close))

    @mcp.tool()
    def desktop_close_window(process: str = "", title: str = "", class_name: str = "", all_matches: bool = False,
                             wait_sec: float = 5.0, allow_editor_main: bool = False) -> str:
        """Close a window gracefully by posting WM_CLOSE (like its X button), e.g. the UEFN crash report
        dialog: desktop_close_window(process='CrashReportClientEditor'). Only allowed processes; refuses
        when several windows match unless all_matches=true. The UEFN editor's main window is refused
        unless allow_editor_main=true (closing it quits the editor). Waits up to `wait_sec` and reports
        what is still open."""
        return _dump(op_close(process, title, class_name, all_matches, wait_sec, allow_editor_main))

    @mcp.tool()
    async def desktop_wait_for_window(process: str = "", title: str = "", class_name: str = "",
                                      gone: bool = False, timeout_sec: float = 60.0,
                                      interval_sec: float = 0.5) -> str:
        """Wait until a visible window matching process / title / class appears (or, with gone=true,
        disappears). Returns ok=false with timeout=true when the time runs out. Read-only."""
        return _dump(await op_wait_for_window(process, title, class_name, gone, timeout_sec, interval_sec))
