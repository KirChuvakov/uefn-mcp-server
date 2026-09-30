"""UEFN session helpers for the MCP server: find and launch the editor, open a project (HUB aware),
report readiness from the editor log and the ports, and read / set the "Load on Startup" preference.

Facts this module relies on (verified 2026-09-30, UEFN 42.20):

* The editor log is %LOCALAPPDATA%\\UnrealEditorFortnite\\Saved\\Logs\\UnrealEditorFortnite.log: UTF-8
  with BOM, a new file per editor run, first line "Log file open, MM/DD/YY HH:MM:SS" (local time).
  The file's creation time is useless (NTFS tunnelling keeps the previous file's). Lines carry UTC
  timestamps "[YYYY.MM.DD-HH.MM.SS:mmm]".
* The editor window title is "Unreal Editor for Fortnite" (class UnrealWindow) with or without a
  project, so readiness comes from log markers (see MARKERS): "Engine is initialized", "Searching
  for projects under" (HUB / project browser populated), "Selected Project (OnMouseClick)" (a HUB
  tile was clicked), "Opening project '<path>'", "Successfully opened project '<path>'", "Python
  enabled via ...ForceEnablePythonAtRuntime", "[MCP] Auto-started on port <p>", "Started the
  ModelContextProtocol server on port <p>" (Epic's Toolsets MCP). The Verse workflow socket 1962 is
  already up on the HUB, so it does not mean "project open".
* "Load on Startup" (Editor Preferences > Loading & Saving; also a selector on the HUB screen) is
  ValkyrieLoadAtStartupMostRecentProject = HomeScreen ("Home Panel") | LastProject ("Most Recent
  Project") in [/Script/ValkyrieEditor.ValkyrieEditorConfig] of
  %LOCALAPPDATA%\\UnrealEditorFortnite\\Saved\\Config\\WindowsEditor\\EditorPerProjectUserSettings.ini,
  next to LastProjectFileName and EnablePythonLocallyPerProject=((<projectId>, True), ...). UEFN
  rewrites that file on exit, so it is only edited while UEFN is closed.
* UEFN's Slate UI exposes no UI Automation tree (the window is a bare UnrealWindow with 0
  children): picking a HUB tile needs a screenshot and a click.
* The Epic launcher starts UEFN as app "Fortnite_Studio" (launcher manifest *.item); the launcher URI
  com.epicgames.launcher://apps/<ns>%3A<item>%3A<app>?action=launch&silent=true passes the sign-in
  arguments. Starting the editor exe directly also works when UEFN has a cached sign-in.

CLI (used by setup.ps1): python uefn_session.py status [--project P] | find-install |
set-load-on-startup HomeScreen|LastProject [--dry-run]
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from typing import Any, Optional

import desktop_control as dc

EDITOR_PROCESS = "UnrealEditorFortnite-Win64-Shipping"
EDITOR_EXE = EDITOR_PROCESS + ".exe"
CRASH_PROCESS = "CrashReportClientEditor*"
EDITOR_REL = os.path.join("FortniteGame", "Binaries", "Win64", EDITOR_EXE)
HOOK_REL = os.path.join("Engine", "Plugins", "Experimental", "Toolsets", "EditorToolset", "Content", "Python",
                        "init_unreal.py")
HOOK_MARKER = "UEFN-MCP autostart hook"
UEFN_APP_NAME = "Fortnite_Studio"

VALKYRIE_SECTION = "/Script/ValkyrieEditor.ValkyrieEditorConfig"
LOAD_KEY = "ValkyrieLoadAtStartupMostRecentProject"
LAST_PROJECT_KEY = "LastProjectFileName"
PYTHON_KEY = "EnablePythonLocallyPerProject"
PROJECTS_DIR_KEY = "LastCreatedProjectLocation"
LOAD_VALUES = {"homescreen": "HomeScreen", "home": "HomeScreen", "hub": "HomeScreen", "homepanel": "HomeScreen",
               "home_panel": "HomeScreen", "lastproject": "LastProject", "last": "LastProject",
               "mostrecentproject": "LastProject", "most_recent_project": "LastProject"}

LISTENER_PORTS = range(8765, 8771)
WORKFLOW_PORT = 1962
TOOLSET_PORT = 8000
HUB_GRACE_SEC = 30.0


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def saved_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.environ.get("UEFN_SAVED_DIR") or os.path.join(base, "UnrealEditorFortnite", "Saved")


def log_file() -> str:
    return os.path.join(saved_dir(), "Logs", "UnrealEditorFortnite.log")


def settings_file() -> str:
    return os.path.join(saved_dir(), "Config", "WindowsEditor", "EditorPerProjectUserSettings.ini")


def documents_dir() -> str:
    """The user's Documents folder (Known Folder API, so OneDrive redirection is honoured)."""
    if dc.IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                            ("d", ctypes.c_ubyte * 8)]

            fid = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            out = ctypes.c_wchar_p()
            shell32 = ctypes.WinDLL("shell32")
            ole32 = ctypes.WinDLL("ole32")
            if shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(out)) == 0 and out.value:
                path = out.value
                ole32.CoTaskMemFree(out)
                return path
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path or ""))


# ---------------------------------------------------------------------------
# INI text (pure, unit-tested)
# ---------------------------------------------------------------------------

def decode_text(data: bytes) -> tuple[str, str, bytes]:
    """bytes -> (text, codec, bom); latin-1 keeps any non-UTF-8 bytes intact."""
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le"), "utf-16-le", b"\xff\xfe"
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), "utf-8", b"\xef\xbb\xbf"
    try:
        return data.decode("utf-8"), "utf-8", b""
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1", b""


def encode_text(text: str, codec: str, bom: bytes) -> bytes:
    return bom + text.encode(codec)


def _section_bounds(lines: list[str], section: str) -> Optional[tuple[int, int]]:
    header = f"[{section}]".lower()
    start = None
    for i, raw in enumerate(lines):
        s = raw.strip().lower()
        if start is None:
            if s == header:
                start = i
        elif s.startswith("[") and s.endswith("]"):
            return start, i
    return (start, len(lines)) if start is not None else None


def ini_get(text: str, section: str, key: str) -> Optional[str]:
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    bounds = _section_bounds(lines, section)
    if not bounds:
        return None
    for ln in lines[bounds[0] + 1:bounds[1]]:
        k, sep, v = ln.partition("=")
        if sep and k.strip().lower() == key.lower():
            return v
    return None


def ini_set(text: str, section: str, key: str, value: str) -> tuple[str, Optional[str]]:
    """Set key=value inside [section], keeping every other byte and the line endings. Returns
    (new_text, old_value or None)."""
    lines = text.split("\n")
    stripped = [ln.rstrip("\r") for ln in lines]
    crlf = "\r\n" in text or not text
    eol = "\r" if crlf else ""
    bounds = _section_bounds(stripped, section)
    if bounds is None:
        sep = "" if (not text or text.endswith("\n")) else "\n"
        return text + sep + f"[{section}]{eol}\n{key}={value}{eol}\n", None
    for i in range(bounds[0] + 1, bounds[1]):
        k, sep, v = stripped[i].partition("=")
        if sep and k.strip().lower() == key.lower():
            lines[i] = f"{k}={value}" + lines[i][len(stripped[i]):]
            return "\n".join(lines), v
    lines.insert(bounds[0] + 1, f"{key}={value}{eol}")
    return "\n".join(lines), None


_PY_ENTRY = re.compile(r"\(\s*([0-9A-Fa-f-]{36})\s*,\s*(True|False)\s*\)")


def parse_python_enabled(value: Optional[str]) -> dict[str, bool]:
    """EnablePythonLocallyPerProject=((<guid>, True),...) -> {guid_lower: bool}."""
    return {g.lower(): v == "True" for g, v in _PY_ENTRY.findall(value or "")}


def normalize_load_value(value: str) -> str:
    key = re.sub(r"[\s-]", "", (value or "")).lower()
    if key not in LOAD_VALUES:
        raise ValueError("value must be HomeScreen (show the HUB / Home Panel) or LastProject (open the most "
                         "recent project)")
    return LOAD_VALUES[key]


# ---------------------------------------------------------------------------
# Editor log scanning (pure, unit-tested)
# ---------------------------------------------------------------------------

_TS = re.compile(r"^\[(\d{4})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2}):(\d{3})\]")
_HEADER = re.compile(r"Log file open, (\d{2})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})")
MARKERS: list[tuple[str, re.Pattern]] = [
    ("engine_init", re.compile(r"LogInit: Display: Engine is initialized")),
    ("browser_ready", re.compile(r"LogValkyrie: Searching for projects under")),
    ("tile_clicked", re.compile(r"LogValkyrieProjectBrowser: Selected Project \(OnMouseClick\)")),
    ("opening", re.compile(r"LogValkyrie: Opening project '(?P<path>[^']+)'")),
    ("opened", re.compile(r"LogValkyrie: Display: Successfully opened project '(?P<path>[^']+)'")),
    ("open_end", re.compile(r"OpenProject_End - Begin \(bSuccess=(?P<ok>\d), bCanceled=(?P<canceled>\d)\)")),
    ("python_off", re.compile(r"LogPython: Python disabled via CVar")),
    ("python_on", re.compile(r"Python enabled via IPythonScriptPlugin::ForceEnablePythonAtRuntime")),
    ("mcp_started", re.compile(r"\[MCP\] Auto-started on port (?P<port>\d+)")),
    ("mcp_listener", re.compile(r"\[MCP\] Listener started on http://127\.0\.0\.1:(?P<port>\d+)")),
    ("mcp_error", re.compile(r"\[MCP\] (?P<msg>(?:Autostart hook failed|Auto-start failed|Import finished but "
                             r"listener is not bound).*)")),
    ("toolset_mcp", re.compile(r"Started the ModelContextProtocol server on port (?P<port>\d+)")),
    ("verse_server", re.compile(r"Verse message server is ready for client connections on IP: [\d.]+:(?P<port>\d+)")),
    ("fatal", re.compile(r"=== Critical error: ===|Fatal error!|Unhandled Exception: EXCEPTION_")),
]
_ANY_MARKER = re.compile("|".join(f"(?:{re.sub(r'[(][?]P<[a-z_]+>', '(?:', rx.pattern)})" for _, rx in MARKERS))


def parse_log_time(line: str) -> Optional[_dt.datetime]:
    m = _TS.match(line)
    if not m:
        return None
    y, mo, d, h, mi, s, ms = (int(x) for x in m.groups())
    try:
        return _dt.datetime(y, mo, d, h, mi, s, ms * 1000, tzinfo=_dt.timezone.utc)
    except ValueError:
        return None


class LogScan:
    """Latest occurrence of every marker in one editor log (fed line by line)."""

    def __init__(self) -> None:
        self.header_epoch: Optional[float] = None
        self.lines = 0
        self.events: dict[str, dict] = {}
        self.mcp_errors: list[dict] = []

    def feed(self, line: str) -> None:
        self.lines += 1
        if self.lines <= 3 and self.header_epoch is None:
            h = _HEADER.search(line)
            if h:
                mo, d, y, hh, mi, ss = (int(x) for x in h.groups())
                try:
                    self.header_epoch = time.mktime((2000 + y, mo, d, hh, mi, ss, 0, 0, -1))
                except (OverflowError, ValueError):
                    pass
        if not _ANY_MARKER.search(line):
            return
        ts = parse_log_time(line)
        for name, rx in MARKERS:
            m = rx.search(line)
            if m:
                rec = {"line": self.lines, "time": ts.isoformat() if ts else None, "_ts": ts}
                rec.update({k: v for k, v in m.groupdict().items() if v is not None})
                self.events[name] = rec
                if name == "mcp_error":
                    self.mcp_errors = (self.mcp_errors + [rec])[-5:]

    def feed_text(self, text: str) -> "LogScan":
        for ln in text.lstrip("﻿").splitlines():
            self.feed(ln)
        return self

    def event(self, name: str) -> Optional[dict]:
        return self.events.get(name)

    def after(self, name: str, ref: Optional[dict]) -> Optional[dict]:
        """The marker if it happened after `ref` (or at all when ref is None)."""
        ev = self.events.get(name)
        if ev and (ref is None or ev["line"] > ref["line"]):
            return ev
        return None

    def project(self) -> dict:
        """{'state': none | opening | open | open_failed, 'path': ...} from the latest project events."""
        opening, opened, end = self.event("opening"), self.event("opened"), self.event("open_end")
        if opening and (not opened or opened["line"] < opening["line"]):
            if end and end["line"] > opening["line"] and end.get("ok") == "0":
                return {"state": "open_failed", "path": opening["path"], "since": opening["time"]}
            return {"state": "opening", "path": opening["path"], "since": opening["time"]}
        if opened:
            return {"state": "open", "path": opened["path"], "since": opened["time"]}
        return {"state": "none", "path": None, "since": None}

    def public(self) -> dict:
        return {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in self.events.items()}


class _LogTail:
    """Incremental reader of the editor log; restarts when the file is replaced (new editor run)."""

    def __init__(self, path: str):
        self.path = path
        self._reset(b"")

    def _reset(self, sig: bytes) -> None:
        self.sig = sig
        self.offset = 0
        self.partial = b""
        self.scan = LogScan()

    def update(self) -> LogScan:
        try:
            with open(self.path, "rb") as fh:
                sig = fh.read(64)
                size = os.fstat(fh.fileno()).st_size
                if sig != self.sig or size < self.offset:
                    self._reset(sig)
                fh.seek(self.offset)
                data = fh.read()
        except OSError:
            self._reset(b"")
            return self.scan
        self.offset += len(data)
        data = self.partial + data
        parts = data.split(b"\n")
        self.partial = parts.pop()
        for raw in parts:
            self.scan.feed(raw.decode("utf-8", "replace").rstrip("\r").lstrip("﻿"))
        return self.scan


_tails: dict[str, _LogTail] = {}


def scan_log(path: Optional[str] = None) -> LogScan:
    path = path or log_file()
    tail = _tails.get(path)
    if tail is None:
        tail = _tails[path] = _LogTail(path)
    return tail.update()


# ---------------------------------------------------------------------------
# Install, launcher, hook
# ---------------------------------------------------------------------------

def _program_data() -> str:
    return os.environ.get("ProgramData") or os.path.join(os.environ.get("SystemDrive", "C:") + os.sep, "ProgramData")


def epic_manifests() -> list[dict]:
    out = []
    for path in glob.glob(os.path.join(_program_data(), "Epic", "EpicGamesLauncher", "Data", "Manifests", "*.item")):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            pass
    return out


def launcher_installed() -> list[dict]:
    path = os.path.join(_program_data(), "Epic", "UnrealEngineLauncher", "LauncherInstalled.dat")
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return list(json.load(fh).get("InstallationList") or [])
    except (OSError, ValueError, AttributeError):
        return []


def launcher_uri_from_manifest(item: dict) -> Optional[str]:
    ns, cat, app = item.get("CatalogNamespace"), item.get("CatalogItemId"), item.get("AppName")
    if not (ns and cat and app):
        return None
    return f"com.epicgames.launcher://apps/{ns}%3A{cat}%3A{app}?action=launch&silent=true"


def _launcher_registered() -> bool:
    if not dc.IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "com.epicgames.launcher"):
            return True
    except OSError:
        return False


def find_install(manifests: Optional[list[dict]] = None, installed: Optional[list[dict]] = None,
                 running_exe: Optional[str] = None) -> dict:
    """Locate the UEFN editor exe and the launcher URI without machine-specific paths."""
    manifests = epic_manifests() if manifests is None else manifests
    installed = launcher_installed() if installed is None else installed
    if running_exe is None and dc.IS_WINDOWS:
        procs = dc.list_processes(EDITOR_PROCESS)
        running_exe = dc.process_image(procs[0]["pid"]) if procs else ""
    candidates: list[tuple[str, str]] = []
    if os.environ.get("UEFN_EDITOR_EXE"):
        candidates.append(("UEFN_EDITOR_EXE", os.environ["UEFN_EDITOR_EXE"]))
    if running_exe:
        candidates.append(("running editor", running_exe))
    uri = None
    for item in manifests:
        exe_rel = item.get("LaunchExecutable") or ""
        if item.get("AppName") == UEFN_APP_NAME or exe_rel.replace("\\", "/").lower().endswith(EDITOR_EXE.lower()):
            if item.get("InstallLocation") and exe_rel:
                candidates.append(("Epic launcher manifest", os.path.join(item["InstallLocation"], exe_rel)))
            uri = uri or launcher_uri_from_manifest(item)
    for entry in installed:
        if entry.get("AppName") in (UEFN_APP_NAME, "Fortnite") and entry.get("InstallLocation"):
            candidates.append(("LauncherInstalled.dat", os.path.join(entry["InstallLocation"], EDITOR_REL)))
    if os.environ.get("UEFN_FORTNITE_DIR"):
        candidates.append(("UEFN_FORTNITE_DIR", os.path.join(os.environ["UEFN_FORTNITE_DIR"], EDITOR_REL)))
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(("default install folder", os.path.join(program_files, "Epic Games", "Fortnite", EDITOR_REL)))
    exe, source = None, None
    for src, path in candidates:
        if path and os.path.isfile(path):
            exe, source = os.path.normpath(path), src
            break
    fortnite_dir = None
    if exe:
        fortnite_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(exe))))
    return {"exe": exe, "exe_source": source, "fortnite_dir": fortnite_dir, "launcher_uri": uri,
            "launcher_registered": _launcher_registered(),
            "checked": [f"{src}: {path}" for src, path in candidates]}


def hook_status(fortnite_dir: Optional[str]) -> dict:
    if not fortnite_dir:
        return {"installed": False, "reason": "Fortnite install not found"}
    path = os.path.join(fortnite_dir, HOOK_REL)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {"installed": False, "path": path, "reason": "Epic init_unreal.py not found"}
    if HOOK_MARKER not in text:
        return {"installed": False, "path": path,
                "reason": "hook missing (a Fortnite update overwrites the file): run ensure_mcp_hook.ps1 or setup.ps1"}
    m = re.search(r"run_path\(r[\"']([^\"']+)[\"']\)", text)
    target = m.group(1) if m else None
    ok = bool(target and os.path.isfile(target))
    return {"installed": ok, "path": path, "target": target,
            "reason": None if ok else f"hook points at a missing file: {target}"}


# ---------------------------------------------------------------------------
# Settings file
# ---------------------------------------------------------------------------

def read_settings(path: Optional[str] = None) -> dict:
    path = path or settings_file()
    try:
        with open(path, "rb") as fh:
            text, _, _ = decode_text(fh.read())
    except OSError:
        return {"path": path, "exists": False}
    load = ini_get(text, VALKYRIE_SECTION, LOAD_KEY)
    return {
        "path": path, "exists": True,
        "load_on_startup": load,
        "load_on_startup_meaning": {"HomeScreen": "HUB (Home Panel)", "LastProject": "Most Recent Project"}.get(
            (load or "").strip(), "unknown (UEFN default)"),
        "last_project": ini_get(text, VALKYRIE_SECTION, LAST_PROJECT_KEY),
        "projects_dir": ini_get(text, VALKYRIE_SECTION, PROJECTS_DIR_KEY),
        "python_enabled": parse_python_enabled(ini_get(text, VALKYRIE_SECTION, PYTHON_KEY)),
    }


def editor_running() -> bool:
    return bool(dc.IS_WINDOWS and dc.list_processes(EDITOR_PROCESS))


def write_settings(changes: dict[str, str], dry_run: bool = False, path: Optional[str] = None,
                   allow_running: bool = False) -> dict:
    """Edit keys of [ValkyrieEditorConfig] in EditorPerProjectUserSettings.ini (UEFN must be closed)."""
    path = path or settings_file()
    running = (not allow_running) and editor_running()
    if running and not dry_run:
        raise RuntimeError("UEFN is running and rewrites EditorPerProjectUserSettings.ini on exit, which would undo "
                           "this change. Close UEFN first, or change it in the editor (Editor Preferences > Loading "
                           "& Saving > Load on Startup, or the selector on the HUB screen).")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        data = b""
    text, codec, bom = decode_text(data)
    if encode_text(text, codec, bom) != data:
        raise RuntimeError(f"cannot round-trip the encoding of {path}; change the setting in the editor instead")
    new, report = text, {}
    for key, value in changes.items():
        new, old = ini_set(new, VALKYRIE_SECTION, key, value)
        report[key] = {"old": old, "new": value}
    changed = new != text
    result = {"path": path, "changes": report, "changed": changed, "dry_run": dry_run}
    if running:
        result["warning"] = "UEFN is running: a real write is refused until it is closed"
    if dry_run or not changed:
        return result
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if data:
        backup = f"{path}.mcp-bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
        result["backup"] = backup
        for old_backup in sorted(glob.glob(glob.escape(path) + ".mcp-bak-*"))[:-3]:
            try:
                os.remove(old_backup)
            except OSError:
                pass
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "wb") as fh:
        fh.write(encode_text(new, codec, bom))
    os.replace(tmp, path)
    return result


def set_load_on_startup(value: str, dry_run: bool = False) -> dict:
    return write_settings({LOAD_KEY: normalize_load_value(value)}, dry_run)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def read_project_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    exp = ((data.get("dataSets") or {}).get("experimental") or {})
    return {
        "path": os.path.normpath(os.path.abspath(path)),
        "name": os.path.splitext(os.path.basename(path))[0],
        "title": data.get("title") or os.path.splitext(os.path.basename(path))[0],
        "project_id": ((data.get("bindings") or {}).get("projectId") or "").lower() or None,
        "python_for_project": ((exp.get("pythonExperimental") or {}).get("bEnablePythonForProject")),
        "toolsets_for_project": ((exp.get("toolsets") or {}).get("bEnableToolsetsForProject")),
    }


def _log_project_paths(limit_files: int = 12) -> list[str]:
    logs = sorted(glob.glob(os.path.join(saved_dir(), "Logs", "UnrealEditorFortnite*.log")),
                  key=lambda p: os.path.getmtime(p), reverse=True)[:limit_files]
    seen: list[str] = []
    rx = re.compile(rb"Opening project '([^']+\.uefnproject)'")
    for path in logs:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        for m in rx.finditer(data):
            p = m.group(1).decode("utf-8", "replace")
            if p not in seen:
                seen.append(p)
    return seen


def resolve_project(project: str = "") -> dict:
    """A .uefnproject path, a project folder or a project name -> project info."""
    settings = read_settings()
    spec = (project or os.environ.get("UEFN_PROJECT") or settings.get("last_project") or "").strip().strip('"')
    if not spec:
        raise ValueError("no project given (pass a .uefnproject path, a project folder or a project name)")
    spec = os.path.expandvars(os.path.expanduser(spec))
    if os.path.isfile(spec) and spec.lower().endswith(".uefnproject"):
        return read_project_file(spec)
    if os.path.isdir(spec):
        hits = sorted(glob.glob(os.path.join(glob.escape(spec), "*.uefnproject")))
        if hits:
            return read_project_file(hits[0])
        raise ValueError(f"no .uefnproject in {spec}")
    name = os.path.splitext(os.path.basename(spec.replace("\\", "/").rstrip("/")))[0]
    roots = [r for r in (settings.get("projects_dir"), os.path.join(documents_dir(), "Fortnite Projects")) if r]
    candidates = [os.path.join(r, name, name + ".uefnproject") for r in roots]
    if settings.get("last_project"):
        candidates.append(settings["last_project"])
    for c in candidates:
        if os.path.isfile(c) and os.path.splitext(os.path.basename(c))[0].lower() == name.lower():
            return read_project_file(c)
    for r in roots:  # folder name or title match
        for c in glob.glob(os.path.join(glob.escape(r), "*", "*.uefnproject")):
            try:
                info = read_project_file(c)
            except (OSError, ValueError):
                continue
            if name.lower() in (info["name"].lower(), (info["title"] or "").lower(),
                                os.path.basename(os.path.dirname(c)).lower()):
                return info
    for c in _log_project_paths():
        if os.path.splitext(os.path.basename(c))[0].lower() == name.lower() and os.path.isfile(c):
            return read_project_file(c)
    raise ValueError(f"project {project!r} not found (looked in {roots} and the editor logs); pass the "
                     ".uefnproject path")


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

def tcp_open(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_listener(timeout: float = 2.0) -> dict:
    """{'port': answering port or None, 'busy': ports that accept TCP but did not answer in time}.

    A listener whose editor thread is inside a long command accepts connections but answers late,
    so 'busy' is not 'dead': wait for the command to finish before diagnosing.
    """
    busy = []
    for port in LISTENER_PORTS:
        if not tcp_open(port, 0.3):
            continue
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=timeout) as resp:
                if json.loads(resp.read().decode("utf-8", "replace")).get("status") == "ok":
                    return {"port": port, "busy": busy}
        except Exception:
            busy.append(port)
    return {"port": None, "busy": busy}


def listener_port(timeout: float = 2.0) -> Optional[int]:
    return probe_listener(timeout)["port"]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def classify(*, editor_pids: list[int], crash_dialog: bool, log_current: bool, project_state: dict,
             engine_ready_at: Optional[_dt.datetime], now: _dt.datetime, load_setting: Optional[str],
             wanted_path: Optional[str], hub_grace_sec: float = HUB_GRACE_SEC) -> str:
    """Pure state decision (unit-tested)."""
    if crash_dialog:
        return "crash_dialog"
    if not editor_pids:
        return "not_running"
    if not log_current:
        return "starting"
    ps = project_state.get("state")
    if ps == "opening":
        return "opening"
    if ps == "open_failed":
        return "open_failed"
    if ps == "open":
        if wanted_path and _norm(project_state.get("path")) != _norm(wanted_path):
            return "other_project_open"
        return "project_open"
    if engine_ready_at is None:
        return "starting"
    grace = 3.0 if (load_setting or "").strip() == "HomeScreen" else hub_grace_sec
    return "hub" if (now - engine_ready_at).total_seconds() >= grace else "starting"


def editor_state(project: Optional[dict] = None, probe_ports: bool = True,
                 hub_grace_sec: float = HUB_GRACE_SEC) -> dict:
    """Snapshot: processes, windows, crash dialog, log markers, ports, settings, hook; state + hints."""
    dc._require_windows()
    procs = dc.list_processes(EDITOR_PROCESS)
    pid = procs[0]["pid"] if procs else None
    started = dc.process_start_time(pid) if pid else None
    monitors = dc.list_monitors()
    crash = dc.find_windows(process=CRASH_PROCESS, monitors=monitors)
    ed_windows = [w for w in dc.find_windows(process=EDITOR_PROCESS, monitors=monitors) if w["class"] == "UnrealWindow"]
    main = dc.pick_main_window([w for w in ed_windows if not w["owner"]] or ed_windows)
    dialogs = [dc.brief(w) for w in ed_windows if main and w["hwnd"] != main["hwnd"]]
    scan = scan_log()
    log_current = bool(pid and scan.header_epoch and started and scan.header_epoch >= started - 120)
    ps = scan.project() if log_current else {"state": "none", "path": None, "since": None}
    ready_ev = scan.event("browser_ready") or scan.event("engine_init")
    settings = read_settings()
    now = _dt.datetime.now(_dt.timezone.utc)
    state = classify(editor_pids=[p["pid"] for p in procs], crash_dialog=bool(crash), log_current=log_current,
                     project_state=ps, engine_ready_at=ready_ev.get("_ts") if (ready_ev and log_current) else None,
                     now=now, load_setting=settings.get("load_on_startup"),
                     wanted_path=project["path"] if project else None, hub_grace_sec=hub_grace_sec)
    opening = scan.event("opening") if log_current else None
    out: dict[str, Any] = {
        "state": state,
        "project": project,
        "open_project": ps,
        "editor": {"pids": [p["pid"] for p in procs], "started": time.strftime("%Y-%m-%d %H:%M:%S",
                   time.localtime(started)) if started else None,
                   "main_window": dict(dc.brief(main), rect=main["rect"], minimized=main["minimized"],
                                       responding=not dc.is_hung(main["hwnd"])) if main else None,
                   "other_windows": dialogs},
        "crash_dialog": [dc.brief(w) for w in crash],
        "log": {"path": log_file(), "current_run": log_current,
                "python_enabled": bool(scan.after("python_on", opening)) if log_current else None,
                "mcp_autostart": (scan.after("mcp_started", opening) or {}).get("port") if log_current else None,
                "mcp_errors": [e["msg"] for e in scan.mcp_errors if not opening or e["line"] > opening["line"]]
                if log_current else [],
                "toolset_mcp_port": (scan.after("toolset_mcp", opening) or {}).get("port") if log_current else None,
                "fatal": (scan.event("fatal") or {}).get("time")},
        "settings": {k: v for k, v in settings.items() if k != "python_enabled"},
    }
    if project and project.get("project_id"):
        out["settings"]["python_enabled_for_project"] = settings.get("python_enabled", {}).get(project["project_id"])
    if probe_ports:
        probe = probe_listener()
        out["ports"] = {"listener": probe["port"], "listener_busy": probe["busy"],
                        "verse_workflow_1962": tcp_open(WORKFLOW_PORT), "toolset_8000": tcp_open(TOOLSET_PORT)}
    out["hints"] = hints_for(out)
    return out


def hints_for(st: dict) -> list[str]:
    s, hints = st["state"], []
    log = st.get("log", {})
    ports = st.get("ports") or {}
    title = (st.get("project") or {}).get("title") or "the project"
    if s == "crash_dialog":
        hints.append("The UEFN crash reporter is showing: desktop_close_window(process='CrashReportClientEditor'), "
                     "then uefn_launch_project.")
    elif s == "not_running":
        hints.append("UEFN is not running: uefn_launch_project(project=...) starts it.")
    elif s == "starting":
        hints.append("UEFN is starting (about 30-60 s to the HUB or to the project load); call again.")
    elif s == "hub":
        hints.append(f"HUB screen: uefn_launch_project returns a screenshot; click the tile of '{title}', then "
                     "Launch; then call uefn_launch_project(project=..., launch=False) to wait.")
    elif s == "opening":
        hints.append("The project is loading (typically 30-90 s).")
        if st["editor"].get("other_windows"):
            hints.append("UEFN shows extra windows (dialog?): desktop_screenshot(process='UnrealEditorFortnite') "
                         "and answer it if the load waits for input.")
    elif s == "open_failed":
        hints.append("The project failed to open: read the editor log (get_editor_log / uefn_logs) around "
                     "'OpenProject_'.")
    elif s == "other_project_open":
        hints.append("Another project is open. Never kill a healthy editor: switch through File > Open Project "
                     "(HUB) with desktop tools, or ask the owner.")
    if s == "project_open" and not ports.get("listener"):
        py_enabled = log.get("python_enabled")
        responding = ((st.get("editor") or {}).get("main_window") or {}).get("responding", True)
        if ports.get("listener_busy"):
            hints.append(f"The listener on {ports['listener_busy'][0]} accepts connections but did not answer in "
                         "time: a long command is running in the editor" + ("" if responding else
                         " and the editor window is not responding (hung?)") + ". Wait and re-check before "
                         "restarting anything; another agent may be driving the editor.")
        elif py_enabled is False:
            hints.append("Python is off for this project, so the listener cannot start: unreal-mcp "
                         "ValkyriePythonToolset.EnablePythonInUEFN (check IsPythonEnabledInUEFN), or Project "
                         "Settings > Python Editor Script Plugin, then reopen the project.")
        elif log.get("mcp_errors"):
            hints.append(f"Listener autostart failed: {log['mcp_errors'][-1]} (check UEFN_MCP_PATH and the hook "
                         "target; rerun ensure_mcp_hook.ps1).")
        elif py_enabled and not log.get("mcp_autostart"):
            hints.append("Python is on but the hook did not run: run ensure_mcp_hook.ps1 (or setup.ps1) and reopen "
                         "the project, or start it now: Tools > Execute Python Script > uefn_listener.py.")
        elif log.get("mcp_autostart"):
            hints.append(f"The listener autostarted on {log['mcp_autostart']} but does not answer: wait 30 s "
                         "(watchdog), then see the restart-uefn skill (zombie socket).")
        else:
            hints.append("Waiting for Python / the listener (about 20-40 s after the project opens).")
    if s == "project_open" and ports.get("listener"):
        hints.append(f"Ready: listener on {ports['listener']}.")
    return hints


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

def launch_editor(project_path: Optional[str], via: str = "auto", install: Optional[dict] = None) -> dict:
    inst = install or find_install()
    mode = (via or "auto").lower()
    if mode == "auto":
        mode = "launcher" if inst.get("launcher_uri") and inst.get("launcher_registered") else "exe"
    if mode == "launcher":
        if not inst.get("launcher_uri"):
            raise RuntimeError("no Epic launcher manifest for Unreal Editor for Fortnite; use launch_via='exe'")
        os.startfile(inst["launcher_uri"])  # the launcher starts UEFN with the sign-in arguments
        return {"via": "launcher", "uri": inst["launcher_uri"]}
    if mode != "exe":
        raise ValueError("launch_via must be auto, launcher or exe")
    exe = inst.get("exe")
    if not exe:
        raise RuntimeError("UEFN editor exe not found; set UEFN_EDITOR_EXE or UEFN_FORTNITE_DIR. Checked: "
                           + "; ".join(inst.get("checked", [])))
    args = [exe] + ([project_path] if project_path else [])
    base = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    for flags in (base | 0x01000000, base):  # try CREATE_BREAKAWAY_FROM_JOB first
        try:
            proc = subprocess.Popen(args, cwd=os.path.dirname(exe), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
                                    close_fds=True)
            return {"via": "exe", "exe": exe, "pid": proc.pid, "args": args[1:]}
        except OSError:
            continue
    raise RuntimeError(f"could not start {exe}")


def _hub_capture(focus: bool) -> dict:
    wins = [w for w in dc.find_windows(process=EDITOR_PROCESS) if w["class"] == "UnrealWindow"]
    main = dc.pick_main_window([w for w in wins if not w["owner"]] or wins)
    if not main:
        raise dc.DesktopError("no UEFN window found")
    focused = None
    if focus and (main["minimized"] or dc.occluders(main) or not main.get("foreground")):
        focused = dc.op_focus(process=EDITOR_PROCESS, class_name="UnrealWindow")
        time.sleep(0.4)
    shot = dc.screenshot(process=EDITOR_PROCESS, class_name="UnrealWindow")
    return {"screenshot": shot, "focused": focused}


def _result(status: str, st: Optional[dict], t0: float, **extra: Any) -> dict:
    out = {"status": status, "elapsed_sec": round(time.monotonic() - t0, 1)}
    out.update(extra)
    if st is not None:
        out["state"] = st
    return out


async def launch_project(project: str = "", launch: bool = True, launch_via: str = "auto",
                         retarget_last_project: bool = True, enable_load_last_project: bool = False,
                         wait_sec: float = 180.0, hub_grace_sec: float = HUB_GRACE_SEC, focus_hub: bool = True,
                         wait_for_listener: bool = True, listener_wait_sec: float = 90.0) -> dict:
    """Start UEFN if needed, get the project open (HUB aware) and wait for the listener."""
    dc._require_windows()
    t0 = time.monotonic()
    proj = resolve_project(project)
    actions: list[Any] = []
    st = editor_state(proj, probe_ports=False, hub_grace_sec=hub_grace_sec)
    if st["state"] == "crash_dialog":
        return _result("crash_dialog", st, t0, next_steps=st["hints"])
    if st["state"] == "not_running":
        if not launch:
            return _result("not_running", st, t0, next_steps=st["hints"])
        with dc._ActionLog("uefn_launch_project") as act:
            act.fields["project"] = proj["path"]
            dc.preflight(act)
            changes: dict[str, str] = {}
            settings = read_settings()
            load = (settings.get("load_on_startup") or "").strip()
            if enable_load_last_project and load != "LastProject":
                changes[LOAD_KEY] = "LastProject"
                load = "LastProject"
            if (retarget_last_project and load == "LastProject"
                    and _norm(settings.get("last_project") or "") != _norm(proj["path"])):
                changes[LAST_PROJECT_KEY] = proj["path"].replace("\\", "/")
            if changes:
                actions.append({"settings": write_settings(changes)})
            launched = launch_editor(proj["path"], launch_via)
            act.fields["launch"] = launched.get("via")
            actions.append({"launch": launched})
    deadline = t0 + max(10.0, min(wait_sec, 1800.0))
    while True:
        st = editor_state(proj, probe_ports=False, hub_grace_sec=hub_grace_sec)
        s = st["state"]
        if s in ("crash_dialog", "open_failed", "other_project_open"):
            return _result(s, st, t0, actions=actions, next_steps=st["hints"])
        if s == "project_open":
            break
        if s == "hub":
            cap = _hub_capture(focus_hub)
            return _result("hub", st, t0, actions=actions, screenshot=cap["screenshot"], focused=cap["focused"],
                           next_steps=[
                               f"Read {cap['screenshot']['path']} and find the tile titled '{proj['title']}' "
                               "(Recent Projects / My Projects). No UI Automation: UEFN's Slate UI exposes none.",
                               f"desktop_click(x, y, shot='{cap['screenshot']['path']}') on the tile; take a new "
                               "desktop_screenshot(process='UnrealEditorFortnite') and click the Launch button (or "
                               "double-click the tile). Do not change the 'Load on Startup' selector unless the owner "
                               "asked.",
                               f"Then call uefn_launch_project(project='{proj['path']}', launch=False) to wait for "
                               "the project and the listener."])
        if time.monotonic() >= deadline:
            extra: dict[str, Any] = {"actions": actions, "next_steps": st["hints"] + [
                "Timed out; call uefn_launch_project again (with launch=False) to keep waiting."]}
            if s == "not_running" and actions:
                extra["next_steps"].append("Nothing started: check the Epic Games Launcher (sign-in, update) or "
                                           "retry with launch_via='exe'.")
            if st["editor"].get("main_window") and not st["editor"]["main_window"]["minimized"]:
                try:
                    extra["screenshot"] = dc.screenshot(process=EDITOR_PROCESS, class_name="UnrealWindow")
                except Exception:
                    pass
            return _result("timeout", st, t0, **extra)
        await asyncio.sleep(2.0)
    port = None
    if wait_for_listener:
        lw_deadline = time.monotonic() + max(0.0, min(listener_wait_sec, 600.0))
        while True:
            port = listener_port()
            if port or time.monotonic() >= lw_deadline:
                break
            await asyncio.sleep(2.0)
    st = editor_state(proj, probe_ports=True, hub_grace_sec=hub_grace_sec)
    ready = bool(st["ports"].get("listener"))
    status = "ready" if ready else ("project_open" if not wait_for_listener else "project_open_no_listener")
    return _result(status, st, t0, actions=actions, next_steps=st["hints"])


def python_readiness(project: dict, settings: dict) -> dict:
    """Is Python (hence the listener) able to start for this project?"""
    local = (settings.get("python_enabled") or {}).get(project.get("project_id") or "")
    return {"project": project["path"], "title": project["title"],
            "enable_python_locally": local,              # Project Settings > Python Editor Script Plugin
            "project_python_flag": project.get("python_for_project"),
            "project_toolsets_flag": project.get("toolsets_for_project"),
            "ok": bool(local)}


def status_report(project: str = "", probe_ports: bool = True) -> dict:
    proj = resolve_project(project) if project else None
    st = editor_state(proj, probe_ports=probe_ports)
    inst = find_install()
    st["install"] = {k: v for k, v in inst.items() if k != "checked"}
    st["hook"] = hook_status(inst.get("fortnite_dir"))
    settings = read_settings()
    target = proj
    if target is None and settings.get("last_project") and os.path.isfile(settings["last_project"]):
        try:
            target = read_project_file(settings["last_project"])
        except (OSError, ValueError):
            target = None
    if target:
        st["python_readiness"] = python_readiness(target, settings)
    return st


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def register(mcp) -> None:
    """Attach the uefn_* session tools to a FastMCP instance."""

    @mcp.tool()
    def uefn_status(project: str = "", probe_ports: bool = True) -> str:
        """Read-only snapshot of the local UEFN session, no listener needed: state (not_running | starting
        | hub | opening | project_open | other_project_open | open_failed | crash_dialog), the open
        project (from the editor log; the window title never names it), editor windows and whether they
        respond, the crash dialog, Python-on / listener-autostart / Toolsets markers for the current
        project, ports (listener 8765-8770, Verse 1962, Toolsets 8000), the 'Load on Startup' setting,
        the autostart hook, the install / launcher, and next-step hints.

        Args:
            project: Optional .uefnproject path, project folder or name, to compare with the open one.
            probe_ports: Also probe the ports (fast, read-only).
        """
        return _dump(status_report(project, probe_ports))

    @mcp.tool()
    async def uefn_launch_project(project: str = "", launch: bool = True, launch_via: str = "auto",
                                  retarget_last_project: bool = True, enable_load_last_project: bool = False,
                                  wait_sec: float = 180.0, hub_grace_sec: float = HUB_GRACE_SEC,
                                  focus_hub: bool = True, wait_for_listener: bool = True,
                                  listener_wait_sec: float = 90.0) -> str:
        """Open a UEFN project end to end and wait until it is usable.

        1. Resolves `project` (.uefnproject path, folder or name; default UEFN_PROJECT or the last one).
        2. If UEFN is not running and `launch`: starts it through the Epic launcher URI (sign-in args) or
           the editor exe (`launch_via` auto | launcher | exe). When 'Load on Startup' is already Most
           Recent Project, `retarget_last_project` points LastProjectFileName at this project first (UEFN
           closed only). `enable_load_last_project=true` also switches 'Load on Startup' to Most Recent
           Project: an explicit opt-in, never done otherwise.
        3. Waits for the project load (editor log) or the HUB screen. On the HUB it returns
           status='hub' with a screenshot of the UEFN window and its mapping: click the project's tile
           and Launch with desktop_click(shot=...), then call again with launch=False.
        4. Waits for the listener (8765-8770) and reports ready, or why not (Python off, hook missing).
        Other results: crash_dialog, other_project_open, open_failed, timeout (call again). Never
        closes or kills an editor. Acting parts obey the desktop safety rails (kill switch, audit log).
        """
        return _dump(await launch_project(project, launch, launch_via, retarget_last_project,
                                          enable_load_last_project, wait_sec, hub_grace_sec, focus_hub,
                                          wait_for_listener, listener_wait_sec))

    @mcp.tool()
    def uefn_set_load_on_startup(value: str, dry_run: bool = False) -> str:
        """Set UEFN's 'Load on Startup' preference: HomeScreen (the HUB / Home Panel) or LastProject (Most
        Recent Project, so a relaunch reopens the project by itself). Only on the owner's explicit
        request. UEFN must be closed (it rewrites the file on exit); edits
        EditorPerProjectUserSettings.ini ([/Script/ValkyrieEditor.ValkyrieEditorConfig]
        ValkyrieLoadAtStartupMostRecentProject) with a backup. dry_run shows the change only."""
        with dc._ActionLog("uefn_set_load_on_startup") as act:
            act.fields["value"] = value
            return _dump(set_load_on_startup(value, dry_run))


# ---------------------------------------------------------------------------
# CLI (setup.ps1)
# ---------------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="uefn_session.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_status = sub.add_parser("status", help="UEFN session snapshot (read-only)")
    p_status.add_argument("--project", default="")
    p_status.add_argument("--no-ports", action="store_true")
    sub.add_parser("find-install", help="where the editor exe / launcher entry is")
    p_set = sub.add_parser("set-load-on-startup", help="HomeScreen | LastProject (UEFN must be closed)")
    p_set.add_argument("value")
    p_set.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "status":
            out = status_report(args.project, not args.no_ports)
        elif args.cmd == "find-install":
            out = find_install()
        else:
            out = set_load_on_startup(args.value, args.dry_run)
    except Exception as e:  # printed for the calling script
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1
    print(_dump(out))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
