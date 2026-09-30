"""MCP HTTP Listener for UEFN Editor.

Runs an HTTP server on a background thread inside the UEFN editor.
All unreal.* API calls are dispatched to the main thread via tick callback.

Usage (in UEFN editor console):
    py "path/to/uefn_listener.py"

Or auto-start via init_unreal.py.
"""

import atexit
import io
import json
import math
import os
import queue
import re
import socket
import sys
import threading
import time
import traceback
import tkinter as tk
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Optional

import unreal

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# 0.3.3: rotations take named axes; static-mesh tools use crash-safe reads only (0.5.0 safety fixes).
PROTOCOL_VERSION = "0.3.3"
VERSION_SUFFIX = "by Romasno"

try:
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _SCRIPT_DIR = os.getcwd()
ICON_PATH = os.path.join(_SCRIPT_DIR, "icon.png")
DEFAULT_PORT = 8765
MAX_PORT = 8770
TICK_BATCH_LIMIT = 5
HTTP_TIMEOUT_SEC = 30.0
POLL_INTERVAL_SEC = 0.02
STALE_CLEANUP_SEC = 60.0
LOG_RING_SIZE = 200
ERROR_LOG_SIZE = 50
TASK_LOG_SIZE = 200

# ---------------------------------------------------------------------------
# Optional system-tray support (pystray + Pillow).
# These are vendored into ./vendor next to this file (installed with the same
# UEFN Python so the Pillow C-extension ABI matches). If they are missing the
# listener still works — it just falls back to "hide window" with no tray icon.
# ---------------------------------------------------------------------------

_VENDOR_DIR = os.path.join(_SCRIPT_DIR, "vendor")
if os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

try:
    import pystray  # type: ignore
    from PIL import Image as PIL_Image  # type: ignore
    _TRAY_AVAILABLE = True
except Exception as _tray_import_err:  # pragma: no cover - import guard
    pystray = None  # type: ignore
    PIL_Image = None  # type: ignore
    _TRAY_AVAILABLE = False

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Shared state — stored on `unreal` module so re-runs of the script
# share the same objects (queues, metrics, tick handle, etc.).
# ---------------------------------------------------------------------------

def _init_shared_state() -> None:
    """Initialise shared state on the ``unreal`` module (once)."""
    defaults: Dict[str, Any] = {
        "_mcp_server": None,
        "_mcp_server_thread": None,
        "_mcp_tick_handle": None,
        "_mcp_bound_port": 0,
        "_mcp_command_queue": queue.Queue(),
        "_mcp_main_queue": queue.Queue(),
        "_mcp_responses": {},
        "_mcp_responses_lock": threading.Lock(),
        "_mcp_request_counter": 0,
        "_mcp_log_ring": [],
        "_mcp_error_log": [],
        "_mcp_task_log": [],
        "_mcp_current_task": {"command": None, "start_time": 0.0},
        "_mcp_metrics": {
            "started_at": 0.0,
            "total_requests": 0,
            "total_errors": 0,
            "last_request_at": 0.0,
            "last_command": "",
            "last_error": "",
            "last_client_ping": 0.0,
            "response_times_ms": [],
        },
        "_mcp_status_window": None,
        "_mcp_tray": None,
    }
    for attr, default in defaults.items():
        if not hasattr(unreal, attr):
            setattr(unreal, attr, default)

_init_shared_state()

# Convenience aliases for mutable containers — safe because dicts/queues
# are modified in-place, so the alias always points to the shared object.
_command_queue: queue.Queue = unreal._mcp_command_queue
_main_queue: queue.Queue = unreal._mcp_main_queue
_responses: Dict[str, dict] = unreal._mcp_responses
_responses_lock: threading.Lock = unreal._mcp_responses_lock
_log_ring: List[str] = unreal._mcp_log_ring
_error_log: List[Dict[str, Any]] = unreal._mcp_error_log
_task_log: List[Dict[str, Any]] = unreal._mcp_task_log
_current_task: Dict[str, Any] = unreal._mcp_current_task
_metrics: Dict[str, Any] = unreal._mcp_metrics

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log(msg: str, level: str = "info") -> None:
    """Log to UE Output Log and internal ring buffer."""
    entry = f"[MCP] {msg}"
    _log_ring.append(entry)
    if len(_log_ring) > LOG_RING_SIZE:
        _log_ring.pop(0)
    if level == "error":
        unreal.log_error(entry)
    elif level == "warning":
        unreal.log_warning(entry)
    else:
        unreal.log(entry)


# ---------------------------------------------------------------------------
# Main-thread helpers
# ---------------------------------------------------------------------------


def _run_on_main_thread(fn: Callable[[], Any]) -> None:
    """Schedule *fn* to execute on the UE main thread (next tick)."""
    _main_queue.put(fn)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _serialize(obj: Any) -> Any:
    """Convert unreal objects to JSON-serializable types."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _serialize(v) for k, v in obj.items()}
    if isinstance(obj, unreal.Vector):
        return {"x": obj.x, "y": obj.y, "z": obj.z}
    if isinstance(obj, unreal.Rotator):
        return {"pitch": obj.pitch, "yaw": obj.yaw, "roll": obj.roll}
    if isinstance(obj, unreal.Vector2D):
        return {"x": obj.x, "y": obj.y}
    if isinstance(obj, unreal.LinearColor):
        return {"r": obj.r, "g": obj.g, "b": obj.b, "a": obj.a}
    if isinstance(obj, unreal.Color):
        return {"r": obj.r, "g": obj.g, "b": obj.b, "a": obj.a}
    if isinstance(obj, unreal.Transform):
        return {
            "location": _serialize(obj.translation),
            "rotation": _serialize(obj.rotation.rotator()),
            "scale": _serialize(obj.scale3d),
        }
    if isinstance(obj, unreal.AssetData):
        return {
            "asset_name": str(obj.asset_name),
            "asset_class": str(obj.asset_class_path.asset_name) if hasattr(obj, "asset_class_path") else str(getattr(obj, "asset_class", "")),
            "package_name": str(obj.package_name),
            "package_path": str(obj.package_path),
            "object_path": str(obj.get_export_text_name()) if hasattr(obj, "get_export_text_name") else str(obj.object_path) if hasattr(obj, "object_path") else "",
        }
    # Generic unreal.Object
    if hasattr(obj, "get_path_name"):
        return str(obj.get_path_name())
    if hasattr(obj, "get_name"):
        return str(obj.get_name())
    # Enum
    if hasattr(obj, "__class__") and hasattr(obj.__class__, "__qualname__"):
        cls_name = obj.__class__.__qualname__
        if "." in cls_name or cls_name[0].isupper():
            return str(obj)
    try:
        return str(obj)
    except Exception:
        return repr(obj)


def _serialize_actor(actor: unreal.Actor) -> dict:
    """Serialize an actor to a dict with common properties."""
    return {
        "name": actor.get_name(),
        "label": actor.get_actor_label(),
        "class": actor.get_class().get_name(),
        "path": actor.get_path_name(),
        "location": _serialize(actor.get_actor_location()),
        "rotation": _serialize(actor.get_actor_rotation()),
        "scale": _serialize(actor.get_actor_scale3d()),
    }


# ---------------------------------------------------------------------------
# Rotation convention
# ---------------------------------------------------------------------------
# The Python constructor is unreal.Rotator(roll=0.0, pitch=0.0, yaw=0.0): positional arguments
# are (roll, pitch, yaw) (UE Python stub; the struct's native make function is
# MakeRotator(Roll, Pitch, Yaw)). Before 0.5.0 the tools documented rotation lists as
# [pitch, yaw, roll] but passed them positionally, so UEFN applied them as [roll, pitch, yaw]
# without any error. Every rotation parameter now takes named axes and every Rotator is built
# with keywords (tests/test_rotation_offline.py checks both).

ROTATION_AXES = ("pitch", "yaw", "roll")


def _finite_number(value: Any, label: str) -> float:
    """A finite int/float (bool and numeric strings are refused)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}: expected a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label}: expected a finite number, got {value!r}")
    return number


def _parse_rotation(value: Any, name: str = "rotation") -> Dict[str, float]:
    """Normalize a rotation parameter to {"pitch", "yaw", "roll"} in degrees.

    Accepted: a dict with any of the keys pitch / yaw / roll (missing axes are 0), or a list of
    three EQUAL numbers such as [0, 0, 0] (the only lists both readings agree on). Any other
    list is refused as ambiguous: the tools documented [pitch, yaw, roll] but applied it as
    [roll, pitch, yaw] until 0.5.0.
    """
    if isinstance(value, dict):
        unknown = sorted(str(k) for k in value if k not in ROTATION_AXES)
        if unknown:
            raise ValueError(f"{name}: unknown key(s) {unknown}; use pitch, yaw, roll (degrees)")
        return {axis: _finite_number(value.get(axis, 0.0), f"{name}.{axis}") for axis in ROTATION_AXES}
    if isinstance(value, (list, tuple)):
        if len(value) != 3:
            raise ValueError(f'{name}: expected named axes {{"pitch", "yaw", "roll"}}, got a list of {len(value)}')
        a, b, c = (_finite_number(v, f"{name}[{i}]") for i, v in enumerate(value))
        if a == b == c:
            return {"pitch": a, "yaw": a, "roll": a}
        raise ValueError(
            f"{name}: the list {list(value)} is ambiguous. Rotation lists were documented as [pitch, yaw, roll] "
            "but applied as [roll, pitch, yaw] before 0.5.0. Pass named axes: "
            f'{{"pitch": {a:g}, "yaw": {b:g}, "roll": {c:g}}} for [pitch, yaw, roll], or '
            f'{{"pitch": {b:g}, "yaw": {c:g}, "roll": {a:g}}} for what the old tools did.')
    raise ValueError(f'{name}: expected named axes {{"pitch", "yaw", "roll"}} in degrees, got {type(value).__name__}')


def _make_rotator(value: Any, name: str = "rotation") -> "unreal.Rotator":
    """unreal.Rotator from a rotation parameter, always built with keywords."""
    r = _parse_rotation(value, name)
    return unreal.Rotator(roll=r["roll"], pitch=r["pitch"], yaw=r["yaw"])


def _float_list(value: Any, size: int, name: str) -> List[float]:
    """A list of `size` finite numbers (a position, a size, a tiling)."""
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError(f"{name}: expected a list of {size} numbers, got {value!r}")
    return [_finite_number(v, f"{name}[{i}]") for i, v in enumerate(value)]


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

_HANDLERS: Dict[str, Callable] = {}


def _register(name: str):
    """Decorator to register a command handler."""
    def decorator(fn: Callable):
        _HANDLERS[name] = fn
        return fn
    return decorator


def _dispatch(command: str, params: dict) -> dict:
    """Dispatch a command to its handler. Runs on main thread."""
    handler = _HANDLERS.get(command)
    if handler is None:
        raise ValueError(f"Unknown command: {command}. Available: {list(_HANDLERS.keys())}")
    return handler(**params)


def _describe_command(command: str, params: dict) -> str:
    """Human-friendly label for task log / status window.

    For execute_python extracts a '# DESC: ...' comment if present, otherwise
    the first non-empty non-comment line. Other commands use their name.
    """
    if command != "execute_python":
        return command
    code = params.get("code", "") or ""
    lines = code.splitlines()
    for ln in lines[:8]:
        s = ln.strip()
        if s.lower().startswith("# desc:"):
            return f"py: {s[7:].strip()[:80]}"
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#"):
            trimmed = s[:80] + ("\u2026" if len(s) > 80 else "")
            return f"py: {trimmed}"
    return "execute_python"


# -- System ------------------------------------------------------------------


@_register("ping")
def _cmd_ping() -> dict:
    return {
        "status": "ok",
        "version": PROTOCOL_VERSION,
        "python_version": sys.version,
        "port": unreal._mcp_bound_port,
        "timestamp": time.time(),
        "commands": list(_HANDLERS.keys()),
    }


@_register("status")
def _cmd_status() -> dict:
    """Full listener status with metrics."""
    uptime = time.time() - _metrics["started_at"] if _metrics["started_at"] > 0 else 0.0
    times = _metrics["response_times_ms"]
    avg_ms = sum(times) / len(times) if times else 0.0
    return {
        "running": unreal._mcp_server is not None,
        "version": PROTOCOL_VERSION,
        "port": unreal._mcp_bound_port,
        "uptime_sec": round(uptime, 1),
        "total_requests": _metrics["total_requests"],
        "total_errors": _metrics["total_errors"],
        "avg_response_ms": round(avg_ms, 2),
        "last_request_at": _metrics["last_request_at"],
        "last_command": _metrics["last_command"],
        "last_error": _metrics["last_error"],
        "queue_size": _command_queue.qsize(),
        "commands": list(_HANDLERS.keys()),
    }


@_register("shutdown")
def _cmd_shutdown() -> dict:
    """Schedule listener shutdown after current request completes.

    Uses a short timer on a daemon thread to avoid deadlock — the HTTP
    handler that is processing this very request must finish first.
    """
    def _deferred_stop() -> None:
        time.sleep(0.5)
        _run_on_main_thread(stop_listener)

    threading.Thread(target=_deferred_stop, daemon=True).start()
    _log("Shutdown scheduled in 0.5s")
    return {"status": "shutting_down", "port": unreal._mcp_bound_port}


@_register("get_log")
def _cmd_get_log(last_n: int = 50) -> dict:
    return {"lines": _log_ring[-last_n:]}


@_register("execute_python")
def _cmd_execute_python(code: str) -> dict:
    """Execute arbitrary Python code on the main thread.

    Assign to `result` to return a value. Use print() for stdout.
    Pre-populated globals: unreal, actor_sub, asset_sub, level_sub.
    """
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr

    exec_globals: Dict[str, Any] = {
        "__builtins__": __builtins__,
        "unreal": unreal,
        "tk": tk,
        "get_tk_root": _get_tk_root,
        "result": None,
    }
    # Pre-populate subsystems (best-effort)
    for attr, cls_name in [
        ("actor_sub", "EditorActorSubsystem"),
        ("asset_sub", "EditorAssetSubsystem"),
        ("level_sub", "LevelEditorSubsystem"),
    ]:
        try:
            cls = getattr(unreal, cls_name)
            exec_globals[attr] = unreal.get_editor_subsystem(cls)
        except Exception:
            pass

    try:
        sys.stdout, sys.stderr = stdout_buf, stderr_buf
        exec(code, exec_globals)
    except Exception:
        traceback.print_exc(file=stderr_buf)
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr

    return {
        "result": _serialize(exec_globals.get("result")),
        "stdout": stdout_buf.getvalue(),
        "stderr": stderr_buf.getvalue(),
    }


# -- Actors ------------------------------------------------------------------


@_register("get_all_actors")
def _cmd_get_all_actors(class_filter: str = "") -> dict:
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = actor_sub.get_all_level_actors()
    if class_filter:
        actors = [a for a in actors if a.get_class().get_name() == class_filter]
    return {"actors": [_serialize_actor(a) for a in actors], "count": len(actors)}


@_register("get_selected_actors")
def _cmd_get_selected_actors() -> dict:
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = actor_sub.get_selected_level_actors()
    return {"actors": [_serialize_actor(a) for a in actors], "count": len(actors)}


@_register("spawn_actor")
def _cmd_spawn_actor(
    asset_path: str = "",
    actor_class: str = "",
    location: Optional[List[float]] = None,
    rotation: Any = None,
) -> dict:
    """rotation: named axes {"pitch", "yaw", "roll"} in degrees (see _parse_rotation)."""
    loc = unreal.Vector(*location) if location else unreal.Vector(0, 0, 0)
    rot = _make_rotator(rotation) if rotation is not None else unreal.Rotator(roll=0.0, pitch=0.0, yaw=0.0)

    if asset_path:
        asset = unreal.EditorAssetLibrary.load_asset(asset_path)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_path}")
        actor = unreal.EditorLevelLibrary.spawn_actor_from_object(asset, loc, rot)
    elif actor_class:
        cls = getattr(unreal, actor_class, None)
        if cls is None:
            raise ValueError(f"Class not found: {actor_class}")
        actor = unreal.EditorLevelLibrary.spawn_actor_from_class(cls, loc, rot)
    else:
        raise ValueError("Provide either asset_path or actor_class")

    if actor is None:
        raise RuntimeError("Failed to spawn actor")
    return {"actor": _serialize_actor(actor)}


@_register("delete_actors")
def _cmd_delete_actors(actor_paths: List[str]) -> dict:
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    all_actors = actor_sub.get_all_level_actors()
    deleted = []
    for path in actor_paths:
        for actor in all_actors:
            if actor.get_path_name() == path or actor.get_actor_label() == path:
                actor_sub.destroy_actor(actor)
                deleted.append(path)
                break
    return {"deleted": deleted, "count": len(deleted)}


@_register("set_actor_transform")
def _cmd_set_actor_transform(
    actor_path: str,
    location: Optional[List[float]] = None,
    rotation: Any = None,
    scale: Optional[List[float]] = None,
) -> dict:
    """rotation: named axes {"pitch", "yaw", "roll"} in degrees (see _parse_rotation)."""
    rot = _make_rotator(rotation) if rotation is not None else None  # validate before touching the actor
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    all_actors = actor_sub.get_all_level_actors()
    target = None
    for a in all_actors:
        if a.get_path_name() == actor_path or a.get_actor_label() == actor_path:
            target = a
            break
    if target is None:
        raise ValueError(f"Actor not found: {actor_path}")

    if location is not None:
        target.set_actor_location(unreal.Vector(*location), False, False)
    if rot is not None:
        target.set_actor_rotation(rot, False)
    if scale is not None:
        target.set_actor_scale3d(unreal.Vector(*scale))
    return {"actor": _serialize_actor(target)}


@_register("get_actor_properties")
def _cmd_get_actor_properties(actor_path: str, properties: List[str]) -> dict:
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    all_actors = actor_sub.get_all_level_actors()
    target = None
    for a in all_actors:
        if a.get_path_name() == actor_path or a.get_actor_label() == actor_path:
            target = a
            break
    if target is None:
        raise ValueError(f"Actor not found: {actor_path}")

    result = {}
    for prop in properties:
        try:
            result[prop] = _serialize(target.get_editor_property(prop))
        except Exception as e:
            result[prop] = f"<error: {e}>"
    return {"actor_path": actor_path, "properties": result}


@_register("set_actor_properties")
def _cmd_set_actor_properties(actor_path: str, properties: Dict[str, Any]) -> dict:
    """Set properties on an actor via set_editor_property."""
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    all_actors = actor_sub.get_all_level_actors()
    target = None
    for a in all_actors:
        if a.get_path_name() == actor_path or a.get_actor_label() == actor_path:
            target = a
            break
    if target is None:
        raise ValueError(f"Actor not found: {actor_path}")

    set_results = {}
    for prop, value in properties.items():
        try:
            target.set_editor_property(prop, value)
            set_results[prop] = "ok"
        except Exception as e:
            set_results[prop] = f"<error: {e}>"
    return {"actor_path": actor_path, "properties": set_results}


@_register("select_actors")
def _cmd_select_actors(actor_paths: List[str], add_to_selection: bool = False) -> dict:
    """Select actors in the viewport by path or label."""
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    all_actors = actor_sub.get_all_level_actors()

    to_select = []
    found = []
    for path in actor_paths:
        for a in all_actors:
            if a.get_path_name() == path or a.get_actor_label() == path:
                to_select.append(a)
                found.append(path)
                break

    if add_to_selection:
        current = actor_sub.get_selected_level_actors()
        to_select = list(current) + to_select

    actor_sub.set_selected_level_actors(to_select)
    return {"selected": found, "count": len(found)}


@_register("focus_selected")
def _cmd_focus_selected() -> dict:
    """Move viewport camera to focus on selected actors (like pressing F)."""
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    selected = actor_sub.get_selected_level_actors()
    if not selected:
        raise ValueError("No actors selected")

    # Calculate bounding center of selected actors
    xs, ys, zs = [], [], []
    for a in selected:
        loc = a.get_actor_location()
        xs.append(loc.x)
        ys.append(loc.y)
        zs.append(loc.z)

    center_x = sum(xs) / len(xs)
    center_y = sum(ys) / len(ys)
    center_z = sum(zs) / len(zs)

    # Pull camera back from center
    spread = max(
        max(xs) - min(xs),
        max(ys) - min(ys),
        max(zs) - min(zs),
        200.0,
    )
    cam_dist = spread * 1.5
    cam_loc = unreal.Vector(center_x - cam_dist * 0.5, center_y - cam_dist * 0.5, center_z + cam_dist * 0.5)
    # Look from (-x, -y, +z) back at the center: yaw 45, pitch -35 (keywords: positional is roll, pitch, yaw).
    cam_rot = unreal.Rotator(roll=0.0, pitch=-35.0, yaw=45.0)

    unreal.EditorLevelLibrary.set_level_viewport_camera_info(cam_loc, cam_rot)
    return {
        "center": {"x": center_x, "y": center_y, "z": center_z},
        "camera": _serialize(cam_loc),
        "rotation": _serialize(cam_rot),
        "actors_count": len(selected),
    }




@_register("get_editor_log")
def _cmd_get_editor_log(last_n: int = 100, filter_str: str = "") -> dict:
    """Read recent lines from the UE Output Log file."""
    log_path = unreal.Paths.project_log_dir()
    log_file = None
    try:
        import os
        log_dir = str(log_path)
        # Find the most recent .log file
        log_files = [f for f in os.listdir(log_dir) if f.endswith(".log")]
        if log_files:
            log_files.sort(key=lambda f: os.path.getmtime(os.path.join(log_dir, f)), reverse=True)
            log_file = os.path.join(log_dir, log_files[0])
    except Exception:
        pass

    if not log_file:
        return {"lines": [], "error": "Log file not found"}

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        lines = all_lines[-last_n:]
        if filter_str:
            lines = [l for l in lines if filter_str.lower() in l.lower()]
        return {"lines": [l.rstrip() for l in lines], "count": len(lines), "file": log_file}
    except Exception as e:
        return {"lines": [], "error": str(e)}


# -- Assets -----------------------------------------------------------------


@_register("list_assets")
def _cmd_list_assets(directory: str = "/Game/", recursive: bool = True, class_filter: str = "") -> dict:
    assets = unreal.EditorAssetLibrary.list_assets(directory, recursive=recursive)
    if class_filter:
        filtered = []
        for asset_path in assets:
            data = unreal.EditorAssetLibrary.find_asset_data(asset_path)
            if data is not None:
                cls = str(data.asset_class_path.asset_name) if hasattr(data, "asset_class_path") else str(getattr(data, "asset_class", ""))
                if cls == class_filter:
                    filtered.append(str(asset_path))
        assets = filtered
    else:
        assets = [str(a) for a in assets]
    return {"assets": assets, "count": len(assets)}


@_register("get_asset_info")
def _cmd_get_asset_info(asset_path: str) -> dict:
    data = unreal.EditorAssetLibrary.find_asset_data(asset_path)
    if data is None:
        raise ValueError(f"Asset not found: {asset_path}")
    return {"asset": _serialize(data)}


@_register("get_selected_assets")
def _cmd_get_selected_assets() -> dict:
    selected = unreal.EditorUtilityLibrary.get_selected_assets()
    return {
        "assets": [_serialize(a) for a in selected],
        "count": len(selected),
    }


@_register("rename_asset")
def _cmd_rename_asset(old_path: str, new_path: str) -> dict:
    success = unreal.EditorAssetLibrary.rename_asset(old_path, new_path)
    return {"success": success, "old_path": old_path, "new_path": new_path}


@_register("delete_asset")
def _cmd_delete_asset(asset_path: str) -> dict:
    success = unreal.EditorAssetLibrary.delete_asset(asset_path)
    return {"success": success, "asset_path": asset_path}


@_register("duplicate_asset")
def _cmd_duplicate_asset(source_path: str, dest_path: str) -> dict:
    result = unreal.EditorAssetLibrary.duplicate_asset(source_path, dest_path)
    return {"success": result is not None, "source": source_path, "dest": dest_path}


@_register("does_asset_exist")
def _cmd_does_asset_exist(asset_path: str) -> dict:
    exists = unreal.EditorAssetLibrary.does_asset_exist(asset_path)
    return {"exists": exists, "asset_path": asset_path}


@_register("save_asset")
def _cmd_save_asset(asset_path: str) -> dict:
    success = unreal.EditorAssetLibrary.save_asset(asset_path)
    return {"success": success, "asset_path": asset_path}


@_register("search_assets")
def _cmd_search_assets(class_name: str = "", directory: str = "/Game/", recursive: bool = True) -> dict:
    # UEFN doesn't allow setting ARFilter properties on instances.
    # Fall back to list_assets + filter by class.
    assets = unreal.EditorAssetLibrary.list_assets(directory, recursive=recursive)
    results = []
    for asset_path in assets:
        data = unreal.EditorAssetLibrary.find_asset_data(str(asset_path))
        if data is None:
            continue
        if class_name:
            cls = str(data.asset_class_path.asset_name) if hasattr(data, "asset_class_path") else str(getattr(data, "asset_class", ""))
            if cls != class_name:
                continue
        results.append(_serialize(data))
    return {"assets": results, "count": len(results)}


# -- Project -----------------------------------------------------------------


@_register("get_project_info")
def _cmd_get_project_info() -> dict:
    """Get project name and content root path."""
    world = unreal.EditorLevelLibrary.get_editor_world()
    project_name = ""
    content_root = ""
    if world:
        # World path is like /ProjectName/LevelName.LevelName
        parts = world.get_path_name().split("/")
        if len(parts) >= 2:
            project_name = parts[1]
            content_root = f"/{project_name}/"
    return {
        "project_name": project_name,
        "content_root": content_root,
        "project_dir": str(unreal.Paths.project_dir()),
    }


# -- Level -------------------------------------------------------------------


@_register("save_current_level")
def _cmd_save_current_level() -> dict:
    success = unreal.EditorLevelLibrary.save_current_level()
    return {"success": success}


@_register("get_level_info")
def _cmd_get_level_info() -> dict:
    world = unreal.EditorLevelLibrary.get_editor_world()
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = actor_sub.get_all_level_actors()
    return {
        "world_name": world.get_name() if world else "None",
        "actor_count": len(actors),
    }


# -- Viewport ----------------------------------------------------------------


@_register("get_viewport_camera")
def _cmd_get_viewport_camera() -> dict:
    loc, rot = unreal.EditorLevelLibrary.get_level_viewport_camera_info()
    return {"location": _serialize(loc), "rotation": _serialize(rot)}


@_register("set_viewport_camera")
def _cmd_set_viewport_camera(
    location: Optional[List[float]] = None,
    rotation: Any = None,
) -> dict:
    """rotation: named axes {"pitch", "yaw", "roll"} in degrees (see _parse_rotation)."""
    new_rot = _make_rotator(rotation) if rotation is not None else None
    cur_loc, cur_rot = unreal.EditorLevelLibrary.get_level_viewport_camera_info()
    loc = unreal.Vector(*location) if location else cur_loc
    rot = new_rot if new_rot is not None else cur_rot
    unreal.EditorLevelLibrary.set_level_viewport_camera_info(loc, rot)
    return {"location": _serialize(loc), "rotation": _serialize(rot)}


# -- Material tools ----------------------------------------------------------

_MATERIAL_DOMAIN_MAP = {
    "surface": "MD_SURFACE",
    "deferred_decal": "MD_DEFERRED_DECAL",
    "light_function": "MD_LIGHT_FUNCTION",
    "volume": "MD_VOLUME",
    "post_process": "MD_POST_PROCESS",
    "user_interface": "MD_UI",
    "virtual_texture": "MD_RUNTIME_VIRTUAL_TEXTURE",
}

_MATERIAL_BLEND_MODE_MAP = {
    "opaque": "BLEND_OPAQUE",
    "masked": "BLEND_MASKED",
    "translucent": "BLEND_TRANSLUCENT",
    "additive": "BLEND_ADDITIVE",
    "modulate": "BLEND_MODULATE",
    "alphacomposite": "BLEND_ALPHACOMPOSITE",
    "alphaholdout": "BLEND_ALPHAHOLDOUT",
}

_MATERIAL_PROPERTY_MAP = {
    "base_color": "MP_BASE_COLOR",
    "metallic": "MP_METALLIC",
    "specular": "MP_SPECULAR",
    "roughness": "MP_ROUGHNESS",
    "anisotropy": "MP_ANISOTROPY",
    "emissive_color": "MP_EMISSIVE_COLOR",
    "opacity": "MP_OPACITY",
    "opacity_mask": "MP_OPACITY_MASK",
    "normal": "MP_NORMAL",
    "tangent": "MP_TANGENT",
    "world_position_offset": "MP_WORLD_POSITION_OFFSET",
    "subsurface_color": "MP_SUBSURFACE_COLOR",
    "ambient_occlusion": "MP_AMBIENT_OCCLUSION",
    "refraction": "MP_REFRACTION",
    "pixel_depth_offset": "MP_PIXEL_DEPTH_OFFSET",
}


def _split_asset_path(asset_path: str):
    parts = asset_path.rsplit("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid asset path: {asset_path!r} (expected '/Path/To/AssetName')")
    return parts[1], parts[0]


def _resolve_material(material_path: str):
    mat = unreal.EditorAssetLibrary.load_asset(material_path)
    if mat is None:
        raise ValueError(f"Material not found: {material_path}")
    if not isinstance(mat, unreal.Material):
        raise ValueError(f"Asset is not a Material: {material_path} (got {type(mat).__name__})")
    return mat


def _resolve_material_instance(instance_path: str):
    mi = unreal.EditorAssetLibrary.load_asset(instance_path)
    if mi is None:
        raise ValueError(f"Material Instance not found: {instance_path}")
    if not isinstance(mi, unreal.MaterialInstanceConstant):
        raise ValueError(f"Asset is not a MaterialInstanceConstant: {instance_path} (got {type(mi).__name__})")
    return mi


def _find_material_expression(mat, node_name: str):
    exprs = unreal.MaterialEditingLibrary.get_material_expressions(mat)
    for e in exprs:
        if e.get_name() == node_name:
            return e
    raise ValueError(f"Expression '{node_name}' not found in {mat.get_path_name()}")


def _resolve_material_expression_class(name: str):
    full = name if name.startswith("MaterialExpression") else f"MaterialExpression{name}"
    cls = getattr(unreal, full, None)
    if cls is None:
        raise ValueError(f"Unknown material expression class: {name!r}")
    return cls


@_register("material_create")
def _cmd_material_create(
    asset_path: str,
    domain: str = "surface",
    blend_mode: str = "opaque",
    two_sided: bool = False,
) -> dict:
    asset_name, package_path = _split_asset_path(asset_path)
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        raise ValueError(f"Asset already exists: {asset_path}")

    domain_key = _MATERIAL_DOMAIN_MAP.get(domain.lower())
    if domain_key is None:
        raise ValueError(f"Unknown domain {domain!r}. Valid: {list(_MATERIAL_DOMAIN_MAP.keys())}")

    blend_key = _MATERIAL_BLEND_MODE_MAP.get(blend_mode.lower())
    if blend_key is None:
        raise ValueError(f"Unknown blend_mode {blend_mode!r}. Valid: {list(_MATERIAL_BLEND_MODE_MAP.keys())}")

    tools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = tools.create_asset(asset_name, package_path, unreal.Material, unreal.MaterialFactoryNew())
    if mat is None:
        raise RuntimeError(f"Failed to create material: {asset_path}")

    mat.set_editor_property("material_domain", getattr(unreal.MaterialDomain, domain_key))
    mat.set_editor_property("blend_mode", getattr(unreal.BlendMode, blend_key))
    mat.set_editor_property("two_sided", two_sided)
    unreal.MaterialEditingLibrary.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(asset_path)
    return {
        "material_path": mat.get_path_name(),
        "domain": domain,
        "blend_mode": blend_mode,
        "two_sided": two_sided,
    }


@_register("material_create_instance")
def _cmd_material_create_instance(parent_path: str, asset_path: str) -> dict:
    asset_name, package_path = _split_asset_path(asset_path)
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        raise ValueError(f"Asset already exists: {asset_path}")

    parent = unreal.EditorAssetLibrary.load_asset(parent_path)
    if parent is None or not isinstance(parent, unreal.MaterialInterface):
        raise ValueError(f"Parent is not a MaterialInterface: {parent_path}")

    factory = unreal.MaterialInstanceConstantFactoryNew()
    factory.set_editor_property("initial_parent", parent)
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    mi = tools.create_asset(asset_name, package_path, unreal.MaterialInstanceConstant, factory)
    if mi is None:
        raise RuntimeError(f"Failed to create material instance: {asset_path}")

    unreal.EditorAssetLibrary.save_asset(asset_path)
    return {"instance_path": mi.get_path_name(), "parent_path": parent.get_path_name()}


@_register("material_add_expression")
def _cmd_material_add_expression(
    material_path: str,
    expression_class: str,
    x: int = 0,
    y: int = 0,
) -> dict:
    mat = _resolve_material(material_path)
    cls = _resolve_material_expression_class(expression_class)
    expr = unreal.MaterialEditingLibrary.create_material_expression(mat, cls, x, y)
    if expr is None:
        raise RuntimeError(f"Failed to create expression {expression_class}")
    return {
        "node_name": expr.get_name(),
        "class": expr.get_class().get_name(),
        "material_path": mat.get_path_name(),
    }


@_register("material_set_expression_property")
def _cmd_material_set_expression_property(
    material_path: str,
    node_name: str,
    property_name: str,
    value: Any,
) -> dict:
    mat = _resolve_material(material_path)
    expr = _find_material_expression(mat, node_name)

    if isinstance(value, list) and len(value) in (3, 4) and all(isinstance(v, (int, float)) for v in value):
        rgba = [float(v) for v in value] + [1.0] * (4 - len(value))
        expr.set_editor_property(property_name, unreal.LinearColor(*rgba))
        out_value: Any = rgba
    elif isinstance(value, str) and value.startswith("/") and unreal.EditorAssetLibrary.does_asset_exist(value):
        asset = unreal.EditorAssetLibrary.load_asset(value)
        expr.set_editor_property(property_name, asset)
        out_value = value
    else:
        expr.set_editor_property(property_name, value)
        out_value = value

    return {"node": node_name, "property": property_name, "value": out_value}


@_register("material_connect_expressions")
def _cmd_material_connect_expressions(
    material_path: str,
    from_node: str,
    from_output: str,
    to_node: str,
    to_input: str,
) -> dict:
    mat = _resolve_material(material_path)
    from_expr = _find_material_expression(mat, from_node)
    to_expr = _find_material_expression(mat, to_node)
    ok = unreal.MaterialEditingLibrary.connect_material_expressions(from_expr, from_output, to_expr, to_input)
    if not ok:
        raise RuntimeError(f"Failed to connect {from_node}.{from_output} -> {to_node}.{to_input}")
    unreal.MaterialEditingLibrary.recompile_material(mat)
    return {"connected": True, "from": f"{from_node}.{from_output}", "to": f"{to_node}.{to_input}"}


@_register("material_connect_property")
def _cmd_material_connect_property(
    material_path: str,
    from_node: str,
    from_output: str,
    material_property: str,
) -> dict:
    mat = _resolve_material(material_path)
    from_expr = _find_material_expression(mat, from_node)
    prop_key = _MATERIAL_PROPERTY_MAP.get(material_property.lower())
    if prop_key is None:
        raise ValueError(f"Unknown property {material_property!r}. Valid: {list(_MATERIAL_PROPERTY_MAP.keys())}")
    prop = getattr(unreal.MaterialProperty, prop_key)
    ok = unreal.MaterialEditingLibrary.connect_material_property(from_expr, from_output, prop)
    if not ok:
        raise RuntimeError(f"Failed to connect {from_node}.{from_output} -> {material_property}")
    unreal.MaterialEditingLibrary.recompile_material(mat)
    return {"connected": True, "from": f"{from_node}.{from_output}", "to": material_property}


@_register("material_list_expressions")
def _cmd_material_list_expressions(material_path: str) -> dict:
    mat = _resolve_material(material_path)
    exprs = unreal.MaterialEditingLibrary.get_material_expressions(mat)
    out = []
    for e in exprs:
        info = {"name": e.get_name(), "class": e.get_class().get_name()}
        try:
            pname = e.get_editor_property("parameter_name")
            if pname:
                info["parameter_name"] = str(pname)
        except Exception:
            pass
        out.append(info)
    return {"material_path": mat.get_path_name(), "expressions": out, "count": len(out)}


@_register("material_recompile")
def _cmd_material_recompile(material_path: str) -> dict:
    mat = _resolve_material(material_path)
    unreal.MaterialEditingLibrary.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(material_path)
    return {"recompiled": True, "material_path": mat.get_path_name()}


@_register("material_set_scalar_param")
def _cmd_material_set_scalar_param(instance_path: str, param_name: str, value: float) -> dict:
    mi = _resolve_material_instance(instance_path)
    unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(mi, param_name, float(value))
    unreal.EditorAssetLibrary.save_asset(instance_path)
    return {"instance_path": instance_path, "param": param_name, "value": float(value)}


@_register("material_set_vector_param")
def _cmd_material_set_vector_param(
    instance_path: str,
    param_name: str,
    r: float,
    g: float,
    b: float,
    a: float = 1.0,
) -> dict:
    mi = _resolve_material_instance(instance_path)
    color = unreal.LinearColor(float(r), float(g), float(b), float(a))
    unreal.MaterialEditingLibrary.set_material_instance_vector_parameter_value(mi, param_name, color)
    unreal.EditorAssetLibrary.save_asset(instance_path)
    return {"instance_path": instance_path, "param": param_name, "value": [r, g, b, a]}


@_register("material_set_texture_param")
def _cmd_material_set_texture_param(instance_path: str, param_name: str, texture_path: str) -> dict:
    mi = _resolve_material_instance(instance_path)
    tex = unreal.EditorAssetLibrary.load_asset(texture_path)
    if tex is None or not isinstance(tex, unreal.Texture):
        raise ValueError(f"Asset is not a Texture: {texture_path}")
    unreal.MaterialEditingLibrary.set_material_instance_texture_parameter_value(mi, param_name, tex)
    unreal.EditorAssetLibrary.save_asset(instance_path)
    return {"instance_path": instance_path, "param": param_name, "texture": texture_path}


@_register("material_set_static_switch_param")
def _cmd_material_set_static_switch_param(instance_path: str, param_name: str, value: bool) -> dict:
    mi = _resolve_material_instance(instance_path)
    unreal.MaterialEditingLibrary.set_material_instance_static_switch_parameter_value(mi, param_name, bool(value))
    unreal.EditorAssetLibrary.save_asset(instance_path)
    return {"instance_path": instance_path, "param": param_name, "value": bool(value)}


# -- Niagara tools -----------------------------------------------------------


def _find_actor(path_or_label: str):
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in actor_sub.get_all_level_actors():
        if a.get_path_name() == path_or_label or a.get_actor_label() == path_or_label:
            return a
    raise ValueError(f"Actor not found: {path_or_label}")


def _find_niagara_component(actor) -> "unreal.NiagaraComponent":
    if isinstance(actor, unreal.NiagaraActor):
        comp = actor.get_editor_property("niagara_component")
        if comp is not None:
            return comp
    comp = actor.get_component_by_class(unreal.NiagaraComponent)
    if comp is None:
        raise ValueError(f"Actor has no NiagaraComponent: {actor.get_path_name()}")
    return comp


def _resolve_niagara_system(system_path: str) -> "unreal.NiagaraSystem":
    asset = unreal.EditorAssetLibrary.load_asset(system_path)
    if asset is None:
        raise ValueError(f"NiagaraSystem not found: {system_path}")
    if not isinstance(asset, unreal.NiagaraSystem):
        raise ValueError(f"Asset is not a NiagaraSystem: {system_path} (got {type(asset).__name__})")
    return asset


@_register("niagara_place_actor")
def _cmd_niagara_place_actor(
    system_path: str,
    location: Optional[List[float]] = None,
    rotation: Any = None,
    label: str = "",
) -> dict:
    """rotation: named axes {"pitch", "yaw", "roll"} in degrees (see _parse_rotation)."""
    rot = _make_rotator(rotation) if rotation is not None else unreal.Rotator(roll=0.0, pitch=0.0, yaw=0.0)
    system = _resolve_niagara_system(system_path)
    loc = unreal.Vector(*location) if location else unreal.Vector(0, 0, 0)
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actor = actor_sub.spawn_actor_from_class(unreal.NiagaraActor, loc, rot)
    if actor is None:
        raise RuntimeError(f"Failed to spawn NiagaraActor")
    comp = actor.get_editor_property("niagara_component")
    comp.set_asset(system, True)
    if label:
        actor.set_actor_label(label)
    return {
        "actor_path": actor.get_path_name(),
        "label": actor.get_actor_label(),
        "system_path": system.get_path_name(),
        "location": _serialize(loc),
        "rotation": _serialize(actor.get_actor_rotation()),
    }


@_register("niagara_set_system_asset")
def _cmd_niagara_set_system_asset(actor_path: str, system_path: str) -> dict:
    actor = _find_actor(actor_path)
    comp = _find_niagara_component(actor)
    system = _resolve_niagara_system(system_path)
    comp.set_asset(system, True)
    return {"actor_path": actor.get_path_name(), "system_path": system.get_path_name()}


@_register("niagara_activate")
def _cmd_niagara_activate(actor_path: str, reset: bool = False) -> dict:
    actor = _find_actor(actor_path)
    comp = _find_niagara_component(actor)
    comp.activate(reset)
    return {"actor_path": actor.get_path_name(), "activated": True, "reset": reset}


@_register("niagara_deactivate")
def _cmd_niagara_deactivate(actor_path: str) -> dict:
    actor = _find_actor(actor_path)
    comp = _find_niagara_component(actor)
    comp.deactivate()
    return {"actor_path": actor.get_path_name(), "deactivated": True}


@_register("niagara_reset")
def _cmd_niagara_reset(actor_path: str) -> dict:
    actor = _find_actor(actor_path)
    comp = _find_niagara_component(actor)
    comp.reset_system()
    return {"actor_path": actor.get_path_name(), "reset": True}


@_register("niagara_set_float_param")
def _cmd_niagara_set_float_param(actor_path: str, param_name: str, value: float) -> dict:
    actor = _find_actor(actor_path)
    comp = _find_niagara_component(actor)
    comp.set_niagara_variable_float(param_name, float(value))
    return {"actor_path": actor.get_path_name(), "param": param_name, "value": float(value)}


@_register("niagara_set_int_param")
def _cmd_niagara_set_int_param(actor_path: str, param_name: str, value: int) -> dict:
    actor = _find_actor(actor_path)
    comp = _find_niagara_component(actor)
    comp.set_niagara_variable_int(param_name, int(value))
    return {"actor_path": actor.get_path_name(), "param": param_name, "value": int(value)}


@_register("niagara_set_bool_param")
def _cmd_niagara_set_bool_param(actor_path: str, param_name: str, value: bool) -> dict:
    actor = _find_actor(actor_path)
    comp = _find_niagara_component(actor)
    comp.set_niagara_variable_bool(param_name, bool(value))
    return {"actor_path": actor.get_path_name(), "param": param_name, "value": bool(value)}


@_register("niagara_set_vec3_param")
def _cmd_niagara_set_vec3_param(
    actor_path: str,
    param_name: str,
    x: float,
    y: float,
    z: float,
) -> dict:
    actor = _find_actor(actor_path)
    comp = _find_niagara_component(actor)
    comp.set_niagara_variable_vec3(param_name, unreal.Vector(float(x), float(y), float(z)))
    return {"actor_path": actor.get_path_name(), "param": param_name, "value": [x, y, z]}


@_register("niagara_set_color_param")
def _cmd_niagara_set_color_param(
    actor_path: str,
    param_name: str,
    r: float,
    g: float,
    b: float,
    a: float = 1.0,
) -> dict:
    actor = _find_actor(actor_path)
    comp = _find_niagara_component(actor)
    comp.set_niagara_variable_linear_color(param_name, unreal.LinearColor(float(r), float(g), float(b), float(a)))
    return {"actor_path": actor.get_path_name(), "param": param_name, "value": [r, g, b, a]}


@_register("niagara_set_texture_param")
def _cmd_niagara_set_texture_param(actor_path: str, param_name: str, texture_path: str) -> dict:
    actor = _find_actor(actor_path)
    comp = _find_niagara_component(actor)
    tex = unreal.EditorAssetLibrary.load_asset(texture_path)
    if tex is None or not isinstance(tex, unreal.Texture):
        raise ValueError(f"Asset is not a Texture: {texture_path}")
    comp.set_variable_texture(param_name, tex)
    return {"actor_path": actor.get_path_name(), "param": param_name, "texture": texture_path}


# -- Animation tools ---------------------------------------------------------


def _resolve_anim_sequence_base(anim_path: str):
    asset = unreal.EditorAssetLibrary.load_asset(anim_path)
    if asset is None:
        raise ValueError(f"Animation asset not found: {anim_path}")
    if not isinstance(asset, unreal.AnimSequenceBase):
        raise ValueError(f"Asset is not an AnimSequence/Montage: {anim_path} (got {type(asset).__name__})")
    return asset


def _resolve_notify_class(class_name: str, expect_state: bool = False):
    cls = getattr(unreal, class_name, None)
    if cls is None:
        raise ValueError(f"Unknown notify class: {class_name!r}")
    base = unreal.AnimNotifyState if expect_state else unreal.AnimNotify
    if not issubclass(cls, base):
        raise ValueError(f"{class_name} is not a subclass of {base.__name__}")
    return cls


@_register("anim_get_info")
def _cmd_anim_get_info(anim_path: str) -> dict:
    anim = _resolve_anim_sequence_base(anim_path)
    skel = anim.get_editor_property("skeleton")
    return {
        "path": anim.get_path_name(),
        "class": anim.get_class().get_name(),
        "length_sec": anim.get_play_length(),
        "skeleton": skel.get_path_name() if skel else None,
    }


@_register("anim_list_notify_tracks")
def _cmd_anim_list_notify_tracks(anim_path: str) -> dict:
    anim = _resolve_anim_sequence_base(anim_path)
    names = unreal.AnimationLibrary.get_animation_notify_track_names(anim)
    return {"tracks": [str(n) for n in names], "count": len(names)}


@_register("anim_add_notify_track")
def _cmd_anim_add_notify_track(
    anim_path: str,
    track_name: str,
    color: Optional[List[float]] = None,
) -> dict:
    anim = _resolve_anim_sequence_base(anim_path)
    rgba = color if color else [1.0, 1.0, 1.0, 1.0]
    rgba = [float(v) for v in rgba] + [1.0] * (4 - len(rgba))
    unreal.AnimationLibrary.add_animation_notify_track(anim, track_name, unreal.LinearColor(*rgba[:4]))
    unreal.EditorAssetLibrary.save_asset(anim_path)
    return {"path": anim_path, "track": track_name}


@_register("anim_remove_all_notify_tracks")
def _cmd_anim_remove_all_notify_tracks(anim_path: str) -> dict:
    anim = _resolve_anim_sequence_base(anim_path)
    unreal.AnimationLibrary.remove_all_animation_notify_tracks(anim)
    unreal.EditorAssetLibrary.save_asset(anim_path)
    return {"path": anim_path, "removed": True}


@_register("anim_list_notifies")
def _cmd_anim_list_notifies(anim_path: str) -> dict:
    anim = _resolve_anim_sequence_base(anim_path)
    events = unreal.AnimationLibrary.get_animation_notify_events(anim)
    out = []
    for e in events:
        notify_obj = e.get_editor_property("notify")
        notify_state = e.get_editor_property("notify_state_class")
        entry = {
            "name": str(e.get_editor_property("notify_name")),
            "time": float(unreal.AnimationLibrary.get_anim_notify_event_trigger_time(e)),
            "duration": float(unreal.AnimationLibrary.get_anim_notify_event_duration(e)),
            "track_index": int(e.get_editor_property("track_index")),
        }
        if notify_obj is not None:
            entry["notify_class"] = notify_obj.get_class().get_name()
        if notify_state is not None:
            entry["notify_state_class"] = notify_state.get_name()
        out.append(entry)
    return {"path": anim.get_path_name(), "notifies": out, "count": len(out)}


@_register("anim_add_notify")
def _cmd_anim_add_notify(
    anim_path: str,
    track_name: str,
    time: float,
    notify_class: str,
) -> dict:
    anim = _resolve_anim_sequence_base(anim_path)
    cls = _resolve_notify_class(notify_class, expect_state=False)
    notify = unreal.AnimationLibrary.add_animation_notify_event(anim, track_name, float(time), cls)
    unreal.EditorAssetLibrary.save_asset(anim_path)
    return {
        "path": anim_path,
        "track": track_name,
        "time": float(time),
        "notify_class": notify_class,
        "created": notify is not None,
    }


@_register("anim_add_notify_state")
def _cmd_anim_add_notify_state(
    anim_path: str,
    track_name: str,
    time: float,
    duration: float,
    notify_state_class: str,
) -> dict:
    anim = _resolve_anim_sequence_base(anim_path)
    cls = _resolve_notify_class(notify_state_class, expect_state=True)
    notify = unreal.AnimationLibrary.add_animation_notify_state_event(
        anim, track_name, float(time), float(duration), cls,
    )
    unreal.EditorAssetLibrary.save_asset(anim_path)
    return {
        "path": anim_path,
        "track": track_name,
        "time": float(time),
        "duration": float(duration),
        "notify_state_class": notify_state_class,
        "created": notify is not None,
    }


@_register("anim_add_float_curve")
def _cmd_anim_add_float_curve(anim_path: str, curve_name: str) -> dict:
    anim = _resolve_anim_sequence_base(anim_path)
    unreal.AnimationLibrary.add_curve(
        anim, curve_name, unreal.RawCurveTrackTypes.RCT_FLOAT, False,
    )
    unreal.EditorAssetLibrary.save_asset(anim_path)
    return {"path": anim_path, "curve": curve_name}


@_register("anim_add_float_curve_key")
def _cmd_anim_add_float_curve_key(
    anim_path: str,
    curve_name: str,
    time: float,
    value: float,
) -> dict:
    anim = _resolve_anim_sequence_base(anim_path)
    unreal.AnimationLibrary.add_float_curve_key(anim, curve_name, float(time), float(value))
    unreal.EditorAssetLibrary.save_asset(anim_path)
    return {"path": anim_path, "curve": curve_name, "time": float(time), "value": float(value)}


@_register("anim_create_montage")
def _cmd_anim_create_montage(source_animation_path: str, asset_path: str) -> dict:
    asset_name, package_path = _split_asset_path(asset_path)
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        raise ValueError(f"Asset already exists: {asset_path}")

    src = unreal.EditorAssetLibrary.load_asset(source_animation_path)
    if src is None or not isinstance(src, unreal.AnimSequence):
        raise ValueError(f"Source is not an AnimSequence: {source_animation_path}")
    skel = src.get_editor_property("skeleton")
    if skel is None:
        raise ValueError(f"Source AnimSequence has no skeleton: {source_animation_path}")

    factory = unreal.AnimMontageFactory()
    factory.set_editor_property("target_skeleton", skel)
    factory.set_editor_property("source_animation", src)
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    montage = tools.create_asset(asset_name, package_path, unreal.AnimMontage, factory)
    if montage is None:
        raise RuntimeError(f"Failed to create montage: {asset_path}")
    unreal.EditorAssetLibrary.save_asset(asset_path)
    return {
        "montage_path": montage.get_path_name(),
        "source": src.get_path_name(),
        "skeleton": skel.get_path_name(),
    }


# -- Static Mesh tools ------------------------------------------------------
#
# UEFN 42.20 (2026-09-30) died with EXCEPTION_ACCESS_VIOLATION (reading 0x18 in the Engine DLL) on
# ONE read-only probe of a project mesh that called the StaticMeshEditorSubsystem metadata getters
# get_lod_count / get_number_verts / get_number_materials / get_simple_collision_count /
# get_collision_complexity / get_convex_collision_count / get_lod_screen_sizes / get_nanite_settings /
# has_vertex_colors / get_num_uv_channels, StaticMesh.get_num_triangles / get_num_sections and
# BodySetup.agg_geom.export_text(). Which one crashed is unknown (likeliest has_vertex_colors: it
# dereferences a source-model mesh description without a null check), so these tools call none of
# them. Mesh facts come from reads that ran on every mesh of a project without trouble: asset-registry
# tags, the static_materials and nanite_settings properties and get_bounding_box(). Everything else
# is reported as STATICMESH_UNSAFE. tests/test_staticmesh_safety_offline.py fails if one of those
# getters is called anywhere in this file.

STATICMESH_UNSAFE = "not available safely in UEFN 42.20"

# Asset-registry tags of a StaticMesh (UStaticMesh::GetAssetRegistryTags; values are text).
_STATIC_MESH_TAGS = (
    "Triangles", "Vertices", "UVChannels", "Materials", "LODs", "MinLOD", "CollisionPrims",
    "SectionsWithCollision", "DefaultCollision", "CollisionComplexity", "LODGroup", "ApproxSize",
    "NaniteEnabled", "HasNaniteData", "NaniteTriangles", "NaniteVertices", "NaniteFallbackTriangles",
)

_COLLISION_SHAPE_MAP = {
    "box": "BOX",
    "sphere": "SPHERE",
    "capsule": "CAPSULE",
    "ndop10_x": "NDOP10_X",
    "ndop10_y": "NDOP10_Y",
    "ndop10_z": "NDOP10_Z",
    "ndop18": "NDOP18",
    "ndop26": "NDOP26",
}

_UV_GEN_MAP = {"planar", "box", "cylindrical"}


def _resolve_static_mesh(asset_path: str):
    sm = unreal.EditorAssetLibrary.load_asset(asset_path)
    if sm is None:
        raise ValueError(f"Static mesh not found: {asset_path}")
    if not isinstance(sm, unreal.StaticMesh):
        raise ValueError(f"Asset is not a StaticMesh: {asset_path} (got {type(sm).__name__})")
    return sm


def _tag_text(value: Any) -> Optional[str]:
    """An asset-registry tag value as text; None when the tag is absent.

    UEFN 42.20 returns a plain string (None or "None" when absent); other builds return a
    (found, value) pair.
    """
    if isinstance(value, tuple):
        flags = [v for v in value if isinstance(v, bool)]
        texts = [v for v in value if not isinstance(v, bool)]
        value = texts[0] if texts and (not flags or flags[0]) else None
    if value is None:
        return None
    text = str(value)
    return None if text in ("", "None") else text


def _tag_int(tags: Dict[str, str], name: str) -> Optional[int]:
    text = tags.get(name)
    if text is None:
        return None
    try:
        return int(float(text.replace(",", "").strip()))
    except ValueError:
        return None


def _tag_bool(tags: Dict[str, str], name: str) -> Optional[bool]:
    text = (tags.get(name) or "").strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return None


def _registry_tags(asset_path: str, names) -> Dict[str, str]:
    """The named asset-registry tags of an asset (only the ones present), as text."""
    data = unreal.EditorAssetLibrary.find_asset_data(asset_path)
    if data is None:
        return {}
    try:
        if not data.is_valid():
            return {}
    except Exception:
        pass
    out: Dict[str, str] = {}
    for name in names:
        try:
            raw = unreal.AssetRegistryHelpers.get_tag_value(data, name)
        except Exception:
            try:
                raw = data.get_tag_value(name)
            except Exception:
                raw = None
        text = _tag_text(raw)
        if text is not None:
            out[name] = text
    return out


def _static_mesh_slots(sm) -> List[Dict[str, Any]]:
    """Material slots from the static_materials property."""
    slots = []
    for i, item in enumerate(sm.get_editor_property("static_materials") or []):
        mat = item.get_editor_property("material_interface")
        slots.append({
            "index": i,
            "slot": str(item.get_editor_property("material_slot_name")),
            "material": mat.get_path_name() if mat is not None else None,
        })
    return slots


def _static_mesh_bounds(sm) -> Dict[str, Any]:
    """Local-space bounds (cm, bounds extensions included) from StaticMesh.get_bounding_box()."""
    box = sm.get_bounding_box()
    lo, hi = box.min, box.max
    return {
        "min": {"x": lo.x, "y": lo.y, "z": lo.z},
        "max": {"x": hi.x, "y": hi.y, "z": hi.z},
        "size": {"x": hi.x - lo.x, "y": hi.y - lo.y, "z": hi.z - lo.z},
        "center": {"x": (lo.x + hi.x) / 2.0, "y": (lo.y + hi.y) / 2.0, "z": (lo.z + hi.z) / 2.0},
    }


def _static_mesh_nanite(sm) -> Dict[str, Any]:
    """Nanite settings from the reflected nanite_settings property (a plain property copy)."""
    settings = sm.get_editor_property("nanite_settings")
    return {
        "enabled": bool(settings.get_editor_property("enabled")),
        "fallback_percent_triangles": float(settings.get_editor_property("fallback_percent_triangles")),
    }


def _read_or_note(errors: Dict[str, str], key: str, fn: Callable, *args: Any) -> Any:
    """Run one read; a Python-level failure goes to `errors` instead of failing the whole tool."""
    try:
        return fn(*args)
    except Exception as e:
        errors[key] = f"{type(e).__name__}: {e}"
        return None


def _staticmesh_info(path: str, tags: Dict[str, str], slots: Optional[List[dict]], bounds: Optional[dict],
                     nanite: Optional[dict]) -> dict:
    """The staticmesh_get_info result, built from the crash-safe reads only."""
    shared = STATICMESH_UNSAFE + " (collision_prims counts simple and convex shapes together)"
    return {
        "path": path,
        "triangles_lod0": _tag_int(tags, "Triangles"),
        "verts_lod0": _tag_int(tags, "Vertices"),
        "uv_channels_lod0": _tag_int(tags, "UVChannels"),
        "lod_count": _tag_int(tags, "LODs"),
        "material_slots": len(slots) if slots is not None else _tag_int(tags, "Materials"),
        "materials": slots,
        "collision_prims": _tag_int(tags, "CollisionPrims"),
        "collision_complexity": tags.get("CollisionComplexity"),
        "bounds": bounds,
        "nanite_enabled": nanite["enabled"] if nanite else _tag_bool(tags, "NaniteEnabled"),
        "nanite_fallback_percent": nanite["fallback_percent_triangles"] if nanite else None,
        "has_nanite_data": _tag_bool(tags, "HasNaniteData"),
        "simple_collision_count": shared,
        "convex_collision_count": shared,
        "has_vertex_colors": STATICMESH_UNSAFE,
        "lod_screen_sizes": STATICMESH_UNSAFE,
        "registry": tags,
        "source": ("asset-registry tags, static_materials, get_bounding_box(), nanite_settings; the "
                   "StaticMeshEditorSubsystem getters are not called (they crashed UEFN 42.20)"),
    }


@_register("staticmesh_get_info")
def _cmd_staticmesh_get_info(asset_path: str) -> dict:
    """Static mesh facts from crash-safe reads only (see the section comment)."""
    sm = _resolve_static_mesh(asset_path)
    path = sm.get_path_name()
    errors: Dict[str, str] = {}
    tags = _read_or_note(errors, "registry", _registry_tags, path, _STATIC_MESH_TAGS) or {}
    slots = _read_or_note(errors, "materials", _static_mesh_slots, sm)
    bounds = _read_or_note(errors, "bounds", _static_mesh_bounds, sm)
    nanite = _read_or_note(errors, "nanite", _static_mesh_nanite, sm)
    info = _staticmesh_info(path, tags, slots, bounds, nanite)
    if errors:
        info["read_errors"] = errors
    return info


@_register("staticmesh_enable_nanite")
def _cmd_staticmesh_enable_nanite(
    asset_path: str,
    enabled: bool = True,
    fallback_percent_triangles: float = 1.0,
) -> dict:
    sm = _resolve_static_mesh(asset_path)
    sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    # Start from the mesh's own settings (the reflected property, not the subsystem getter, which is
    # on the UEFN 42.20 crash list) so every other Nanite setting is kept.
    settings = sm.get_editor_property("nanite_settings")
    settings.set_editor_property("enabled", bool(enabled))
    settings.set_editor_property("fallback_percent_triangles", float(fallback_percent_triangles))
    sub.set_nanite_settings(sm, settings, apply_changes=True)
    saved = unreal.EditorAssetLibrary.save_asset(asset_path)
    errors: Dict[str, str] = {}
    result = {
        "path": asset_path,
        "nanite_enabled": bool(enabled),
        "fallback_percent_triangles": float(fallback_percent_triangles),
        "saved": bool(saved),
        "nanite_settings_after": _read_or_note(errors, "nanite_settings_after", _static_mesh_nanite, sm),
    }
    if errors:
        result["read_errors"] = errors
    return result


@_register("staticmesh_set_lods")
def _cmd_staticmesh_set_lods(
    asset_path: str,
    percent_triangles: List[float],
    screen_sizes: Optional[List[float]] = None,
    auto_compute_screen_size: bool = True,
) -> dict:
    sm = _resolve_static_mesh(asset_path)
    sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)

    opts = unreal.EditorScriptingMeshReductionOptions()
    opts.set_editor_property("auto_compute_lod_screen_size", bool(auto_compute_screen_size))

    settings = []
    for i, pct in enumerate(percent_triangles):
        s = unreal.EditorScriptingMeshReductionSettings()
        s.set_editor_property("percent_triangles", float(pct))
        if screen_sizes and i < len(screen_sizes):
            s.set_editor_property("screen_size", float(screen_sizes[i]))
        settings.append(s)
    opts.set_editor_property("reduction_settings", settings)

    num = sub.set_lods(sm, opts)
    unreal.EditorAssetLibrary.save_asset(asset_path)
    return {"path": asset_path, "lod_count": int(num), "percent_triangles": percent_triangles}


@_register("staticmesh_remove_lods")
def _cmd_staticmesh_remove_lods(asset_path: str) -> dict:
    sm = _resolve_static_mesh(asset_path)
    sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    ok = sub.remove_lods(sm)
    saved = unreal.EditorAssetLibrary.save_asset(asset_path)
    return {
        "path": asset_path,
        "removed": bool(ok),
        "saved": bool(saved),
        "lod_count": STATICMESH_UNSAFE + "; read it in a separate call with staticmesh_get_info (tag LODs)",
    }


@_register("staticmesh_add_collision")
def _cmd_staticmesh_add_collision(asset_path: str, shape: str = "box") -> dict:
    sm = _resolve_static_mesh(asset_path)
    sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    shape_key = _COLLISION_SHAPE_MAP.get(shape.lower())
    if shape_key is None:
        raise ValueError(f"Unknown shape {shape!r}. Valid: {list(_COLLISION_SHAPE_MAP.keys())}")
    shape_enum = getattr(unreal.ScriptingCollisionShapeType, shape_key)
    count = sub.add_simple_collisions(sm, shape_enum)
    unreal.EditorAssetLibrary.save_asset(asset_path)
    return {"path": asset_path, "shape": shape, "collision_count": int(count)}


@_register("staticmesh_remove_collisions")
def _cmd_staticmesh_remove_collisions(asset_path: str) -> dict:
    sm = _resolve_static_mesh(asset_path)
    sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    ok = sub.remove_collisions(sm)
    saved = unreal.EditorAssetLibrary.save_asset(asset_path)
    return {
        "path": asset_path,
        "removed": bool(ok),
        "saved": bool(saved),
        "simple_collision_count": (STATICMESH_UNSAFE + "; read it in a separate call with staticmesh_get_info "
                                   "(collision_prims)"),
    }


@_register("staticmesh_generate_uv")
def _cmd_staticmesh_generate_uv(
    asset_path: str,
    uv_type: str = "planar",
    lod_index: int = 0,
    uv_channel_index: int = 1,
    position: Optional[List[float]] = None,
    orientation: Any = None,
    tiling: Optional[List[float]] = None,
    size: Optional[List[float]] = None,
) -> dict:
    """Project UVs with a gizmo: generate_{planar,cylindrical}_uv_channel(mesh, lod, channel,
    position: Vector, orientation: Rotator, tiling: Vector2D) and generate_box_uv_channel(...,
    size: Vector). position defaults to the bounds center, size to the bounds size (get_bounding_box)."""
    t = str(uv_type).lower()
    if t not in _UV_GEN_MAP:
        raise ValueError(f"uv_type must be one of {sorted(_UV_GEN_MAP)}, got {uv_type!r}")
    if t == "box" and tiling is not None:
        raise ValueError("tiling applies to planar / cylindrical projections; a box projection takes size")
    if t != "box" and size is not None:
        raise ValueError("size applies to the box projection only; planar / cylindrical take tiling")
    # Validate every argument before touching the mesh.
    if orientation is not None:
        rot = _make_rotator(orientation, "orientation")
    else:
        rot = unreal.Rotator(roll=0.0, pitch=0.0, yaw=0.0)
    pos = _float_list(position, 3, "position") if position is not None else None
    box_size = _float_list(size, 3, "size") if size is not None else None
    uv_tiling = _float_list(tiling, 2, "tiling") if tiling is not None else [1.0, 1.0]

    sm = _resolve_static_mesh(asset_path)
    sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    if pos is None or (t == "box" and box_size is None):
        bounds = _static_mesh_bounds(sm)
        if pos is None:
            pos = [bounds["center"][k] for k in ("x", "y", "z")]
        if t == "box" and box_size is None:
            box_size = [bounds["size"][k] for k in ("x", "y", "z")]
    if t == "box":
        ok = sub.generate_box_uv_channel(sm, lod_index, uv_channel_index, unreal.Vector(*pos), rot,
                                         unreal.Vector(*box_size))
        extra = {"size": box_size}
    else:
        fn = sub.generate_planar_uv_channel if t == "planar" else sub.generate_cylindrical_uv_channel
        ok = fn(sm, lod_index, uv_channel_index, unreal.Vector(*pos), rot, unreal.Vector2D(*uv_tiling))
        extra = {"tiling": uv_tiling}
    saved = unreal.EditorAssetLibrary.save_asset(asset_path) if ok else False
    result = {"path": asset_path, "uv_type": t, "channel": uv_channel_index, "lod": lod_index, "ok": bool(ok),
              "saved": bool(saved), "position": pos, "orientation": _serialize(rot)}
    result.update(extra)
    return result


# -- Asset Pipeline tools ---------------------------------------------------


@_register("asset_batch_rename")
def _cmd_asset_batch_rename(renames: List[Dict[str, str]]) -> dict:
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    rename_data = []
    for r in renames:
        old_path = r["old_path"]
        new_path = r["new_path"]
        asset = unreal.EditorAssetLibrary.load_asset(old_path)
        if asset is None:
            raise ValueError(f"Asset not found: {old_path}")
        new_name_parts = new_path.rsplit("/", 1)
        if len(new_name_parts) != 2:
            raise ValueError(f"Invalid new_path: {new_path}")
        ad = unreal.AssetRenameData()
        ad.set_editor_property("asset", asset)
        ad.set_editor_property("new_package_path", new_name_parts[0])
        ad.set_editor_property("new_name", new_name_parts[1])
        rename_data.append(ad)
    tools.rename_assets(rename_data)
    return {"renamed_count": len(rename_data), "renames": renames}


@_register("asset_set_metadata")
def _cmd_asset_set_metadata(asset_path: str, tag: str, value: str) -> dict:
    asset = unreal.EditorAssetLibrary.load_asset(asset_path)
    if asset is None:
        raise ValueError(f"Asset not found: {asset_path}")
    unreal.EditorAssetLibrary.set_metadata_tag(asset, tag, value)
    unreal.EditorAssetLibrary.save_asset(asset_path)
    return {"path": asset_path, "tag": tag, "value": value}


@_register("asset_get_metadata")
def _cmd_asset_get_metadata(asset_path: str) -> dict:
    asset = unreal.EditorAssetLibrary.load_asset(asset_path)
    if asset is None:
        raise ValueError(f"Asset not found: {asset_path}")
    raw = unreal.EditorAssetLibrary.get_metadata_tag_values(asset)
    tags = {str(k): str(v) for k, v in (raw or {}).items()}
    return {"path": asset_path, "tags": tags, "count": len(tags)}


@_register("asset_remove_metadata")
def _cmd_asset_remove_metadata(asset_path: str, tag: str) -> dict:
    asset = unreal.EditorAssetLibrary.load_asset(asset_path)
    if asset is None:
        raise ValueError(f"Asset not found: {asset_path}")
    unreal.EditorAssetLibrary.remove_metadata_tag(asset, tag)
    unreal.EditorAssetLibrary.save_asset(asset_path)
    return {"path": asset_path, "tag": tag, "removed": True}


@_register("asset_find_referencers")
def _cmd_asset_find_referencers(asset_path: str) -> dict:
    refs = unreal.EditorAssetLibrary.find_package_referencers_for_asset(asset_path)
    refs_list = [str(r) for r in refs]
    return {"path": asset_path, "referencers": refs_list, "count": len(refs_list)}


@_register("asset_find_dependencies")
def _cmd_asset_find_dependencies(asset_path: str) -> dict:
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    pkg = asset_path.split(".")[0] if "." in asset_path else asset_path
    deps = registry.get_dependencies(pkg)
    deps_list = [str(d) for d in (deps or [])]
    return {"path": asset_path, "dependencies": deps_list, "count": len(deps_list)}


@_register("asset_find_unused")
def _cmd_asset_find_unused(directory: str = "/Game/", class_filter: str = "") -> dict:
    paths = unreal.EditorAssetLibrary.list_assets(directory, recursive=True, include_folder=False)
    unused = []
    for p in paths:
        if class_filter:
            data = unreal.EditorAssetLibrary.find_asset_data(p)
            if data is None:
                continue
            cls_name = str(data.asset_class_path.asset_name) if hasattr(data, "asset_class_path") else str(getattr(data, "asset_class", ""))
            if cls_name != class_filter:
                continue
        refs = unreal.EditorAssetLibrary.find_package_referencers_for_asset(p)
        if not refs:
            unused.append(p)
    return {"directory": directory, "class_filter": class_filter, "unused": unused, "count": len(unused)}


# -- DataTable tools --------------------------------------------------------


def _resolve_data_table(asset_path: str):
    dt = unreal.EditorAssetLibrary.load_asset(asset_path)
    if dt is None:
        raise ValueError(f"DataTable not found: {asset_path}")
    if not isinstance(dt, unreal.DataTable):
        raise ValueError(f"Asset is not a DataTable: {asset_path} (got {type(dt).__name__})")
    return dt


@_register("datatable_info")
def _cmd_datatable_info(asset_path: str) -> dict:
    dt = _resolve_data_table(asset_path)
    struct = dt.get_row_struct()
    return {
        "path": dt.get_path_name(),
        "row_struct": struct.get_path_name() if struct else None,
        "row_names": [str(n) for n in dt.get_row_names()],
        "column_names": [str(c) for c in dt.get_column_names()],
        "row_count": len(dt.get_row_names()),
    }


@_register("datatable_export_json")
def _cmd_datatable_export_json(asset_path: str) -> dict:
    dt = _resolve_data_table(asset_path)
    return {"path": dt.get_path_name(), "json": dt.export_to_json_string()}


@_register("datatable_export_csv")
def _cmd_datatable_export_csv(asset_path: str) -> dict:
    dt = _resolve_data_table(asset_path)
    return {"path": dt.get_path_name(), "csv": dt.export_to_csv_string()}


@_register("datatable_import_json")
def _cmd_datatable_import_json(asset_path: str, json_string: str) -> dict:
    dt = _resolve_data_table(asset_path)
    ok = dt.fill_from_json_string(json_string)
    unreal.EditorAssetLibrary.save_asset(asset_path)
    return {"path": asset_path, "ok": bool(ok), "row_count": len(dt.get_row_names())}


@_register("datatable_import_csv")
def _cmd_datatable_import_csv(asset_path: str, csv_string: str) -> dict:
    dt = _resolve_data_table(asset_path)
    ok = dt.fill_from_csv_string(csv_string)
    unreal.EditorAssetLibrary.save_asset(asset_path)
    return {"path": asset_path, "ok": bool(ok), "row_count": len(dt.get_row_names())}


@_register("datatable_get_row")
def _cmd_datatable_get_row(asset_path: str, row_name: str) -> dict:
    dt = _resolve_data_table(asset_path)
    full = dt.export_to_json_string()
    import json as _json
    rows = _json.loads(full)
    if isinstance(rows, list):
        for entry in rows:
            if str(entry.get("Name")) == row_name or str(entry.get("---")) == row_name:
                return {"path": asset_path, "row_name": row_name, "row": entry}
    elif isinstance(rows, dict):
        if row_name in rows:
            return {"path": asset_path, "row_name": row_name, "row": rows[row_name]}
    raise ValueError(f"Row {row_name!r} not found in {asset_path}")


# -- Validation tools -------------------------------------------------------


def _build_validator_settings(capture_logs: bool = True, load_assets: bool = True):
    s = unreal.ValidateAssetsSettings()
    s.set_editor_property("capture_logs_during_validation", capture_logs)
    s.set_editor_property("load_assets_for_validation", load_assets)
    s.set_editor_property("show_if_no_failures", False)
    return s


def _run_validation(asset_data_list) -> dict:
    sub = unreal.get_editor_subsystem(unreal.EditorValidatorSubsystem)
    settings = _build_validator_settings()
    num_failed, results = sub.validate_assets_with_settings(asset_data_list, settings)
    out = {
        "asset_count": len(list(asset_data_list)) if hasattr(asset_data_list, "__iter__") else 0,
        "num_failed": int(num_failed),
    }
    try:
        out["num_invalid"] = int(results.get_editor_property("num_invalid"))
        out["num_valid"] = int(results.get_editor_property("num_valid"))
        out["num_warnings"] = int(results.get_editor_property("num_warnings"))
    except Exception:
        pass
    return out


@_register("validate_asset")
def _cmd_validate_asset(asset_path: str) -> dict:
    data = unreal.EditorAssetLibrary.find_asset_data(asset_path)
    if data is None:
        raise ValueError(f"Asset not found: {asset_path}")
    out = _run_validation([data])
    out["path"] = asset_path
    return out


@_register("validate_folder")
def _cmd_validate_folder(directory: str = "/Game/", recursive: bool = True) -> dict:
    paths = unreal.EditorAssetLibrary.list_assets(directory, recursive=recursive, include_folder=False)
    datas = []
    for p in paths:
        d = unreal.EditorAssetLibrary.find_asset_data(p)
        if d is not None:
            datas.append(d)
    out = _run_validation(datas)
    out["directory"] = directory
    out["scanned"] = len(datas)
    return out


@_register("validate_selected")
def _cmd_validate_selected() -> dict:
    selected = unreal.EditorUtilityLibrary.get_selected_asset_data()
    out = _run_validation(list(selected))
    out["selected_count"] = len(list(selected))
    return out


# -- Screenshot -------------------------------------------------------------


@_register("screenshot_start")
def _cmd_screenshot_start(
    width: int = 1920,
    height: int = 1080,
    force_game_view: bool = False,
) -> dict:
    """Fire a viewport screenshot. Non-blocking — returns expected path.

    The external MCP process is responsible for polling the filesystem for
    the file to appear (the editor render thread writes asynchronously).
    """
    tmp_name = f"_mcp_{int(time.time() * 1000)}.png"
    unreal.AutomationLibrary.take_high_res_screenshot(
        int(width), int(height), tmp_name, force_game_view=bool(force_game_view),
    )
    saved_dir = os.path.join(unreal.Paths.project_saved_dir(), "Screenshots", "WindowsEditor")
    return {
        "expected_path": os.path.join(saved_dir, tmp_name),
        "tmp_name": tmp_name,
        "width": int(width),
        "height": int(height),
    }


@_register("anim_create_blendspace")
def _cmd_anim_create_blendspace(
    skeleton_path: str,
    asset_path: str,
    blendspace_type: str = "2D",
) -> dict:
    asset_name, package_path = _split_asset_path(asset_path)
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        raise ValueError(f"Asset already exists: {asset_path}")

    skel = unreal.EditorAssetLibrary.load_asset(skeleton_path)
    if skel is None or not isinstance(skel, unreal.Skeleton):
        raise ValueError(f"Asset is not a Skeleton: {skeleton_path}")

    t = blendspace_type.upper()
    if t == "1D":
        asset_class = unreal.BlendSpace1D
    elif t == "2D":
        asset_class = unreal.BlendSpace
    else:
        raise ValueError(f"blendspace_type must be '1D' or '2D', got {blendspace_type!r}")

    factory = unreal.BlendSpaceFactoryNew()
    factory.set_editor_property("target_skeleton", skel)
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    bs = tools.create_asset(asset_name, package_path, asset_class, factory)
    if bs is None:
        raise RuntimeError(f"Failed to create blendspace: {asset_path}")
    unreal.EditorAssetLibrary.save_asset(asset_path)
    return {
        "blendspace_path": bs.get_path_name(),
        "type": t,
        "skeleton": skel.get_path_name(),
    }


# -- Device tools (Verse @editable) ------------------------------------------


def _find_actor(identifier: str) -> Optional[unreal.Actor]:
    """Find an actor by path, label, or name. Returns None if not found."""
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in actor_sub.get_all_level_actors():
        if (
            a.get_path_name() == identifier
            or a.get_actor_label() == identifier
            or a.get_name() == identifier
        ):
            return a
    return None


# Built-in actor properties we should hide from Verse @editable listing.
_BUILTIN_ACTOR_PROPS = {
    "root_component", "tags", "actor_guid", "default_updated_component",
    "primary_actor_tick", "hidden", "net_driver_name", "replicates",
    "instigator", "owner", "auto_destroy_when_finished", "can_be_damaged",
    "always_relevant", "only_relevant_to_owner", "net_load_on_client",
    "net_use_owner_relevancy", "net_dormancy", "replicating_movement",
    "input_priority", "input", "spawn_collision_handling_method",
    "initial_life_span", "custom_time_dilation", "bring_to_front_on_selected",
    "actor_label", "folder_path", "layers", "pivot_offset", "data_layers",
    "content_bundle_guid", "external_data_layer_asset", "is_spatially_loaded",
    "instance_guid", "is_editor_only_actor", "rayhaven_tags",
}


def _list_verse_editables(actor: unreal.Actor) -> List[Dict[str, Any]]:
    """Enumerate Verse @editable properties on an actor.

    Uses dir() on the actor class and filters out built-in UActor properties.
    Each entry: {name, current_value, class}.
    """
    results: List[Dict[str, Any]] = []
    seen = set()
    for name in sorted(dir(actor)):
        if (
            name.startswith("_")
            or name in _BUILTIN_ACTOR_PROPS
            or name in seen
        ):
            continue
        if callable(getattr(type(actor), name, None)):
            continue
        seen.add(name)
        try:
            val = actor.get_editor_property(name)
        except Exception:
            continue
        if _is_unreal_array(val):
            inner = _sniff_array_inner_type(val)
            value_type = f"array:{inner}"
            current_value = [_serialize(v) for v in val]
        else:
            value_type = type(val).__name__
            current_value = _serialize(val)
        results.append({
            "name": name,
            "current_value": current_value,
            "value_type": value_type,
        })
    return results


def _is_unreal_array(obj: Any) -> bool:
    """True if obj is an unreal.Array (TArray)."""
    array_cls = getattr(unreal, "Array", None)
    if array_cls is not None and isinstance(obj, array_cls):
        return True
    return type(obj).__name__ == "Array" and type(obj).__module__.startswith("unreal")


def _sniff_array_inner_type(arr: Any) -> str:
    """Infer the inner type of a (possibly empty) unreal.Array.

    Uses the first element's type when populated. Returns "unknown" otherwise.
    Result is a value_type string usable in `array:INNER` hints.
    """
    try:
        first = next(iter(arr))
    except (StopIteration, TypeError):
        return "unknown"
    if isinstance(first, bool):
        return "bool"
    if isinstance(first, int):
        return "int"
    if isinstance(first, float):
        return "float"
    if isinstance(first, str):
        return "string"
    if isinstance(first, unreal.Vector):
        return "vector"
    if isinstance(first, unreal.Rotator):
        return "rotator"
    if first is None or isinstance(first, unreal.Actor):
        return "actor"
    return "unknown"


def _coerce_editable_value(
    actor: unreal.Actor, field: str, value: Any, value_type: str
) -> Any:
    """Coerce a raw JSON value into the type expected by the UProperty.

    value_type hints:
        "int", "float", "bool", "string" — primitive coercion
        "actor"                           — resolve via _find_actor(value)
        "vector"                          — [x,y,z] or {x,y,z} → unreal.Vector
        "rotator"                         — {pitch,yaw,roll} → unreal.Rotator (keywords;
                                            lists only as [a,a,a], see _parse_rotation)
        "array:INNER"                     — list of INNER (e.g. "array:actor",
                                            "array:int"). Empty list allowed.
        "auto" (default)                  — infer from current field value
    """
    if value_type == "auto":
        try:
            current = actor.get_editor_property(field)
        except Exception:
            current = None
        if isinstance(current, bool):
            value_type = "bool"
        elif isinstance(current, int):
            value_type = "int"
        elif isinstance(current, float):
            value_type = "float"
        elif isinstance(current, str):
            value_type = "string"
        elif isinstance(current, unreal.Vector):
            value_type = "vector"
        elif isinstance(current, unreal.Rotator):
            value_type = "rotator"
        elif _is_unreal_array(current):
            inner = _sniff_array_inner_type(current)
            if inner == "unknown":
                raise ValueError(
                    f"Cannot auto-infer element type for empty array field {field!r}. "
                    f"Pass value_type='array:INNER' explicitly (e.g. 'array:actor')."
                )
            value_type = f"array:{inner}"
        elif current is None or isinstance(current, unreal.Actor):
            value_type = "actor"
        else:
            value_type = "string"

    if value_type.startswith("array:"):
        inner = value_type.split(":", 1)[1]
        if not inner:
            raise ValueError("array value_type requires inner type, e.g. 'array:actor'")
        if not isinstance(value, (list, tuple)):
            raise ValueError(
                f"array value must be a list, got {type(value).__name__}"
            )
        return [_coerce_editable_value(actor, field, item, inner) for item in value]

    if value_type == "int":
        return int(value)
    if value_type == "float":
        return float(value)
    if value_type == "bool":
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    if value_type == "string":
        return str(value)
    if value_type == "vector":
        if isinstance(value, dict):
            return unreal.Vector(float(value["x"]), float(value["y"]), float(value["z"]))
        return unreal.Vector(float(value[0]), float(value[1]), float(value[2]))
    if value_type == "rotator":
        return _make_rotator(value, f"{field} (rotator)")
    if value_type == "actor":
        if value is None or value == "":
            return None
        resolved = _find_actor(str(value))
        if resolved is None:
            raise ValueError(f"Referenced actor not found: {value!r}")
        return resolved
    raise ValueError(f"Unknown value_type: {value_type!r}")


@_register("device_list_editables")
def _cmd_device_list_editables(actor_path: str) -> dict:
    """List all @editable (UProperty) fields on a creative_device actor.

    Returns the actor's identity plus a list of {name, current_value, value_type}
    entries — everything exposed in the Details panel that isn't a built-in
    Actor property. Use this before device_set_editable to discover field names.
    """
    actor = _find_actor(actor_path)
    if actor is None:
        raise ValueError(f"Actor not found: {actor_path}")
    return {
        "actor": _serialize_actor(actor),
        "editables": _list_verse_editables(actor),
    }


@_register("device_set_editable")
def _cmd_device_set_editable(
    actor_path: str,
    field: str,
    value: Any,
    value_type: str = "auto",
) -> dict:
    """Set a single Verse @editable field on a creative_device actor.

    Coerces ``value`` into the type UEFN expects. ``value_type`` hints the
    coercion: 'int' | 'float' | 'bool' | 'string' | 'actor' | 'vector' |
    'rotator' | 'auto'. For 'actor', ``value`` is the label/path of another
    actor in the level and is resolved to an object reference before assignment.
    """
    actor = _find_actor(actor_path)
    if actor is None:
        raise ValueError(f"Actor not found: {actor_path}")

    coerced = _coerce_editable_value(actor, field, value, value_type)
    try:
        actor.set_editor_property(field, coerced)
    except Exception as e:
        raise RuntimeError(
            f"set_editor_property({field!r}) failed on {actor.get_actor_label()}: {e}"
        ) from e

    try:
        new_val = actor.get_editor_property(field)
    except Exception:
        new_val = coerced

    return {
        "actor": actor.get_actor_label(),
        "actor_path": actor.get_path_name(),
        "field": field,
        "value_type": value_type,
        "new_value": _serialize(new_val),
    }


@_register("device_set_editables_bulk")
def _cmd_device_set_editables_bulk(
    actor_path: str,
    fields: List[Dict[str, Any]],
) -> dict:
    """Set multiple Verse @editable fields on one actor in a single call.

    ``fields`` is a list of {name, value, value_type?} entries. Per-field
    failures are captured and reported in ``results`` — the call does NOT abort
    on first error, letting the agent see which fields succeeded.
    """
    actor = _find_actor(actor_path)
    if actor is None:
        raise ValueError(f"Actor not found: {actor_path}")

    results: List[Dict[str, Any]] = []
    for entry in fields:
        name = entry.get("name") or entry.get("field")
        if not name:
            results.append({"field": None, "ok": False, "error": "missing 'name'"})
            continue
        value = entry.get("value")
        value_type = entry.get("value_type", "auto")
        try:
            coerced = _coerce_editable_value(actor, name, value, value_type)
            actor.set_editor_property(name, coerced)
            try:
                new_val = actor.get_editor_property(name)
            except Exception:
                new_val = coerced
            results.append({
                "field": name,
                "ok": True,
                "value_type": value_type,
                "new_value": _serialize(new_val),
            })
        except Exception as e:
            results.append({"field": name, "ok": False, "error": str(e)})

    ok_count = sum(1 for r in results if r["ok"])
    return {
        "actor": actor.get_actor_label(),
        "actor_path": actor.get_path_name(),
        "ok_count": ok_count,
        "fail_count": len(results) - ok_count,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Playtest (Play-In-Editor)
# ---------------------------------------------------------------------------


def _level_editor_subsystem():
    return unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)


@_register("playtest_start")
def _cmd_playtest_start() -> dict:
    """Start a Play-In-Editor session (equivalent to the UEFN Play button)."""
    sub = _level_editor_subsystem()
    if sub.is_in_play_in_editor():
        return {"status": "already_running"}
    sub.editor_request_begin_play()
    return {"status": "started"}


@_register("playtest_stop")
def _cmd_playtest_stop() -> dict:
    """End the current Play-In-Editor session."""
    sub = _level_editor_subsystem()
    if not sub.is_in_play_in_editor():
        return {"status": "not_running"}
    sub.editor_request_end_play()
    return {"status": "stopping"}


@_register("playtest_status")
def _cmd_playtest_status() -> dict:
    """Report whether a PIE session is currently running."""
    sub = _level_editor_subsystem()
    return {"in_pie": bool(sub.is_in_play_in_editor())}


# ---------------------------------------------------------------------------
# Mesh scatter
# ---------------------------------------------------------------------------


@_register("mesh_scatter")
def _cmd_mesh_scatter(
    static_mesh_path: str,
    min_xyz: List[float],
    max_xyz: List[float],
    count: int = 100,
    seed: int = 0,
    scale_min: float = 1.0,
    scale_max: float = 1.0,
    yaw_random: bool = True,
    pitch_random: bool = False,
    clearance_radius: float = 0.0,
    folder_path: str = "",
    max_attempts: int = 0,
    material_path: str = "",
    collision_profile: str = "",
) -> dict:
    """Scatter StaticMeshActor instances randomly inside an axis-aligned box.

    Deterministic when ``seed`` is non-zero. ``clearance_radius`` enforces a
    minimum XY distance between placed instances via brute-force rejection —
    practical up to a few hundred points. For ground projection, use
    execute_python with a dedicated line trace.

    Returns {placed, attempted, skipped_clearance, actors: [{label, path}]}.
    """
    import random

    mesh = unreal.EditorAssetLibrary.load_asset(static_mesh_path)
    if mesh is None:
        raise ValueError(f"static_mesh_path not found: {static_mesh_path}")
    if not isinstance(mesh, unreal.StaticMesh):
        raise ValueError(f"Asset is not a StaticMesh: {static_mesh_path}")
    if len(min_xyz) != 3 or len(max_xyz) != 3:
        raise ValueError("min_xyz and max_xyz must be 3-element lists")

    mat_override = None
    if material_path:
        mat_override = unreal.EditorAssetLibrary.load_asset(material_path)
        if mat_override is None:
            raise ValueError(f"material_path not found: {material_path}")

    rng = random.Random(seed) if seed else random.Random()
    placed = []
    placed_xy: List = []
    skipped_clearance = 0
    attempts = 0
    cap = max_attempts if max_attempts > 0 else max(count * 10, 50)
    c2 = clearance_radius * clearance_radius

    while len(placed) < count and attempts < cap:
        attempts += 1
        x = rng.uniform(min_xyz[0], max_xyz[0])
        y = rng.uniform(min_xyz[1], max_xyz[1])
        z = rng.uniform(min_xyz[2], max_xyz[2])

        if clearance_radius > 0.0:
            conflict = False
            for px, py in placed_xy:
                dx = x - px
                dy = y - py
                if dx * dx + dy * dy < c2:
                    conflict = True
                    break
            if conflict:
                skipped_clearance += 1
                continue

        yaw = rng.uniform(0.0, 360.0) if yaw_random else 0.0
        pitch = rng.uniform(-15.0, 15.0) if pitch_random else 0.0
        loc = unreal.Vector(x, y, z)
        rot = unreal.Rotator(pitch=pitch, yaw=yaw, roll=0.0)
        actor = unreal.EditorLevelLibrary.spawn_actor_from_object(mesh, loc, rot)
        if actor is None:
            continue
        s = rng.uniform(scale_min, scale_max)
        actor.set_actor_scale3d(unreal.Vector(s, s, s))
        if folder_path:
            actor.set_folder_path(folder_path)
        smc = actor.get_component_by_class(unreal.StaticMeshComponent)
        if smc is not None:
            if mat_override is not None:
                smc.set_material(0, mat_override)
            if collision_profile:
                smc.set_collision_profile_name(collision_profile)
        placed.append({
            "label": actor.get_actor_label(),
            "path": actor.get_path_name(),
        })
        placed_xy.append((x, y))

    return {
        "placed": len(placed),
        "attempted": attempts,
        "skipped_clearance": skipped_clearance,
        "actors": placed,
    }


# ---------------------------------------------------------------------------
# Verse introspection (regex-based .verse file parsing)
# ---------------------------------------------------------------------------


_VERSE_CLASS_RE = re.compile(
    r'^(?P<indent>[ \t]*)(?P<name>[A-Za-z_][\w]*)\s*(?:<[^>]+>)?\s*:=\s*class\s*(?:<[^>]+>)?\s*\(\s*(?P<parents>[^)]*)\s*\)',
    re.MULTILINE,
)
_VERSE_EDITABLE_RE = re.compile(
    r'@editable(?:\s*\([^)]*\))?\s*\n\s*(?P<name>[A-Za-z_][\w]*)\s*:\s*(?P<type>[^=\n#]+?)(?:\s*=\s*[^\n#]*)?\s*(?:#.*)?$',
    re.MULTILINE,
)


def _verse_project_root() -> str:
    """Return absolute path of the UEFN project's plugin content dir.

    UEFN Creative projects are mounted as a single plugin (e.g.
    ``/di_template/``). ``unreal.Paths.project_dir()`` returns Fortnite's
    install — NOT the user's project — so we resolve the plugin content
    dir from the current world's mount point via PluginBlueprintLibrary.
    """
    world = unreal.EditorLevelLibrary.get_editor_world()
    if world is None:
        raise RuntimeError("No editor world loaded")
    parts = world.get_path_name().split("/")
    if len(parts) < 2 or not parts[1]:
        raise RuntimeError(f"Unexpected world path: {world.get_path_name()}")
    plugin_name = parts[1]
    content_dir = unreal.PluginBlueprintLibrary.get_plugin_content_dir(plugin_name)
    if not content_dir:
        raise RuntimeError(f"Plugin content dir not found for '{plugin_name}'")
    return os.path.abspath(content_dir)


def _verse_scan_files() -> List[str]:
    """Return absolute paths of all .verse files under the UEFN project."""
    root = _verse_project_root()
    skip_segs = (os.sep + "Intermediate" + os.sep,
                 os.sep + "Saved" + os.sep,
                 os.sep + "Binaries" + os.sep)
    out: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        low = dirpath + os.sep
        if any(seg in low for seg in skip_segs):
            continue
        for fn in filenames:
            if fn.endswith(".verse"):
                out.append(os.path.join(dirpath, fn))
    return out


def _verse_read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except UnicodeDecodeError:
        with open(path, "r", encoding="latin-1") as fh:
            return fh.read()
    except Exception:
        return ""


def _verse_split_parents(parents: str) -> List[str]:
    out: List[str] = []
    depth = 0
    buf: List[str] = []
    for ch in parents:
        if ch == "<":
            depth += 1
            buf.append(ch)
        elif ch == ">":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            name = "".join(buf).strip()
            if name:
                out.append(name)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return [re.sub(r"<[^>]+>", "", n).strip() for n in out]


def _verse_rel(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except Exception:
        return path


def _verse_collect_classes(src: str) -> List[dict]:
    """Return all class declarations in source with position + parents."""
    out: List[dict] = []
    for m in _VERSE_CLASS_RE.finditer(src):
        parents = _verse_split_parents(m.group("parents") or "")
        out.append({
            "name": m.group("name"),
            "start": m.start(),
            "line": src.count("\n", 0, m.start()) + 1,
            "parents": parents,
        })
    return out


@_register("verse_list_services")
def _cmd_verse_list_services() -> dict:
    """List Verse classes implementing ``i_service``.

    Returns {count, services: [{name, rel_path, line, parents,
    is_initializable, is_player_listener, is_character_listener}]}.
    """
    root = _verse_project_root()
    services = []
    for path in _verse_scan_files():
        src = _verse_read(path)
        if not src or "i_service" not in src:
            continue
        for cls in _verse_collect_classes(src):
            parents = cls["parents"]
            if "i_service" not in parents:
                continue
            services.append({
                "name": cls["name"],
                "rel_path": _verse_rel(path, root),
                "line": cls["line"],
                "parents": parents,
                "is_initializable": "i_initializable" in parents,
                "is_player_listener": "i_player_listener" in parents,
                "is_character_listener": "i_character_listener" in parents,
            })
    services.sort(key=lambda s: s["name"])
    return {"count": len(services), "services": services}


@_register("verse_list_editables")
def _cmd_verse_list_editables(class_filter: str = "") -> dict:
    """List @editable fields grouped by enclosing class.

    Args:
        class_filter: Case-insensitive substring match on class name.
            Empty = include all classes that contain @editable fields.

    Returns {class_count, field_count, by_class: {name: {rel_path, line,
    parents, fields: [{name, type, line}]}}}.
    """
    root = _verse_project_root()
    filt = class_filter.lower()
    by_class: Dict[str, dict] = {}
    for path in _verse_scan_files():
        src = _verse_read(path)
        if not src or "@editable" not in src:
            continue
        classes = _verse_collect_classes(src)
        if not classes:
            continue
        for em in _VERSE_EDITABLE_RE.finditer(src):
            owner = None
            for c in classes:
                if c["start"] < em.start():
                    owner = c
                else:
                    break
            if owner is None:
                continue
            if filt and filt not in owner["name"].lower():
                continue
            entry = by_class.setdefault(owner["name"], {
                "rel_path": _verse_rel(path, root),
                "line": owner["line"],
                "parents": owner["parents"],
                "fields": [],
            })
            entry["fields"].append({
                "name": em.group("name"),
                "type": em.group("type").strip(),
                "line": src.count("\n", 0, em.start()) + 1,
            })
    total = sum(len(v["fields"]) for v in by_class.values())
    return {
        "class_count": len(by_class),
        "field_count": total,
        "by_class": by_class,
    }


@_register("verse_service_graph")
def _cmd_verse_service_graph(installer_filename: str = "_service_installer.verse") -> dict:
    """Parse the composition-root file and extract the DI graph.

    Expects the project convention: each service is constructed via a
    ``Name := class_name:`` archetype block with indented
    ``Field := Source`` lines. Heuristic parser — brittle if the file
    diverges from the convention.

    Args:
        installer_filename: Basename to search for. First match wins.

    Returns {path, count, services: [{name, class, line, deps:
    [{field, source}]}]}.
    """
    root = _verse_project_root()
    target = None
    for path in _verse_scan_files():
        if os.path.basename(path) == installer_filename:
            target = path
            break
    if target is None:
        raise ValueError(f"Installer file not found: {installer_filename}")
    src = _verse_read(target)
    svc_decl = re.compile(
        r'^(?P<indent>[ \t]*)(?P<name>[A-Za-z_][\w]*)\s*:=\s*(?P<cls>[A-Za-z_][\w]*)\s*:\s*$',
        re.MULTILINE,
    )
    field_decl = re.compile(
        r'^(?P<indent>[ \t]+)(?P<field>[A-Za-z_][\w]*)\s*:=\s*(?P<src>[^\n#]+?)\s*(?:#.*)?$',
        re.MULTILINE,
    )
    reserved = {"class", "module", "enum", "struct", "interface", "option"}
    decls = list(svc_decl.finditer(src))
    services = []
    for i, m in enumerate(decls):
        cls_name = m.group("cls")
        if cls_name in reserved:
            continue  # declaration like `foo := class:` — not a service archetype
        header_indent_len = len(m.group("indent").expandtabs(4))
        block_start = m.end()
        block_end = decls[i + 1].start() if i + 1 < len(decls) else len(src)
        deps = []
        for fm in field_decl.finditer(src, block_start, block_end):
            if len(fm.group("indent").expandtabs(4)) <= header_indent_len:
                break
            deps.append({
                "field": fm.group("field"),
                "source": fm.group("src").strip().rstrip(","),
            })
        services.append({
            "name": m.group("name"),
            "class": cls_name,
            "line": src.count("\n", 0, m.start()) + 1,
            "deps": deps,
        })
    return {
        "path": _verse_rel(target, root),
        "count": len(services),
        "services": services,
    }


@_register("verse_find_resource_usage")
def _cmd_verse_find_resource_usage(
    enum_name: str = "resource",
    enum_filename: str = "_resource_type.verse",
    max_sites_per_variant: int = 20,
) -> dict:
    """Find usages of each variant of a Verse enum across all .verse files.

    Default scans the 'resource' enum in '_resource_type.verse' — the
    project convention for money/crystal/etc. tokens.

    Args:
        enum_name: Enum type name (default 'resource').
        enum_filename: Basename of the file containing the enum.
        max_sites_per_variant: Cap per-variant site list to keep result size
            sane. Total count is always accurate.

    Returns {enum, variants, by_variant: {name: {count, sites: [...]}}}.
    """
    root = _verse_project_root()
    enum_path = None
    for path in _verse_scan_files():
        if os.path.basename(path) == enum_filename:
            enum_path = path
            break
    if enum_path is None:
        raise ValueError(f"Enum file not found: {enum_filename}")
    enum_src = _verse_read(enum_path)
    enum_decl = re.compile(
        rf'^\s*{re.escape(enum_name)}\s*(?:<[^>]+>)?\s*:=\s*enum\s*:\s*$',
        re.MULTILINE,
    )
    em = enum_decl.search(enum_src)
    if em is None:
        raise ValueError(f"Enum '{enum_name}' not found in {enum_filename}")
    variants: List[str] = []
    saw_any = False
    for line in enum_src[em.end():].splitlines():
        s = line.strip()
        if not s:
            if saw_any:
                break
            continue
        if s.startswith("#"):
            continue
        mm = re.match(r'^([A-Za-z_][\w]*)', s)
        if not mm:
            break
        variants.append(mm.group(1))
        saw_any = True
    by_variant: Dict[str, dict] = {v: {"count": 0, "sites": []} for v in variants}
    word_re = {v: re.compile(rf'\b{re.escape(v)}\b') for v in variants}
    for path in _verse_scan_files():
        if path == enum_path:
            continue
        src = _verse_read(path)
        if not src or not any(v in src for v in variants):
            continue
        for lineno, line in enumerate(src.splitlines(), start=1):
            for v in variants:
                if word_re[v].search(line):
                    entry = by_variant[v]
                    entry["count"] += 1
                    if len(entry["sites"]) < max_sites_per_variant:
                        entry["sites"].append({
                            "rel_path": _verse_rel(path, root),
                            "line": lineno,
                            "text": line.strip()[:200],
                        })
    return {
        "enum": enum_name,
        "variants": variants,
        "by_variant": by_variant,
    }


@_register("verse_check_editable_coverage")
def _cmd_verse_check_editable_coverage(
    config_class: str = "world_accessor_device",
) -> dict:
    """Source-side audit of a config class's @editable fields.

    UEFN's ScriptDevice bindings block reading Verse @editable values
    from Python, so this tool performs a static analysis instead:

    1. Parse the config class in .verse sources, extract @editable fields.
    2. For each field, count references across the rest of the project
       (e.g. ``World.NotificationServiceConfig``).
    3. Flag fields with zero references as potentially unused.

    Useful for spotting forgotten config slots after a refactor.

    Args:
        config_class: Verse class name (default world_accessor_device).

    Returns {config_class, rel_path, total, unused_count,
    fields: [{name, type, ref_count, unused, sites: [...]}]}.
    """
    editables_all = _cmd_verse_list_editables(class_filter=config_class)["by_class"]
    editables = editables_all.get(config_class)
    if editables is None:
        # prefer exact match when substring produced extras
        for k, v in editables_all.items():
            if k == config_class:
                editables = v
                break
    if editables is None:
        raise ValueError(f"Config class not found in .verse sources: {config_class}")

    root = _verse_project_root()
    config_path = os.path.join(root, editables["rel_path"].replace("/", os.sep))
    field_names = [f["name"] for f in editables["fields"]]
    field_res = {n: re.compile(rf'\b{re.escape(n)}\b') for n in field_names}
    counts: Dict[str, int] = {n: 0 for n in field_names}
    sites: Dict[str, List[dict]] = {n: [] for n in field_names}

    for path in _verse_scan_files():
        if os.path.abspath(path) == os.path.abspath(config_path):
            continue  # skip self — the declaration file
        src = _verse_read(path)
        if not src:
            continue
        if not any(n in src for n in field_names):
            continue
        for lineno, line in enumerate(src.splitlines(), start=1):
            for n in field_names:
                if field_res[n].search(line):
                    counts[n] += 1
                    if len(sites[n]) < 10:
                        sites[n].append({
                            "rel_path": _verse_rel(path, root),
                            "line": lineno,
                            "text": line.strip()[:200],
                        })

    fields_out = []
    for fld in editables["fields"]:
        n = fld["name"]
        ref_count = counts[n]
        fields_out.append({
            "name": n,
            "type": fld["type"],
            "line": fld["line"],
            "ref_count": ref_count,
            "unused": ref_count == 0,
            "sites": sites[n],
        })
    unused = sum(1 for f in fields_out if f["unused"])
    return {
        "config_class": config_class,
        "rel_path": editables["rel_path"],
        "total": len(fields_out),
        "unused_count": unused,
        "fields": fields_out,
    }


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------


class _MCPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for MCP commands."""

    def _send_json(self, code: int, body: bytes) -> None:
        """Send a JSON response, silently ignoring broken connections."""
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass  # client disconnected (e.g. heartbeat timeout) — safe to ignore

    def do_GET(self) -> None:
        """Health check and tool manifest."""
        _metrics["last_client_ping"] = time.time()
        body = json.dumps({
            "status": "ok",
            "version": PROTOCOL_VERSION,
            "port": unreal._mcp_bound_port,
            "commands": list(_HANDLERS.keys()),
        }).encode()
        self._send_json(200, body)

    def do_POST(self) -> None:
        """Execute a command."""
        _metrics["last_client_ping"] = time.time()
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            self._send_json(400, json.dumps({"success": False, "error": f"Invalid JSON: {e}"}).encode())
            return

        command = body.get("command", "")
        params = body.get("params", {})
        if not command:
            self._send_json(400, json.dumps({"success": False, "error": "Missing 'command' field"}).encode())
            return

        unreal._mcp_request_counter += 1
        req_id = f"req_{unreal._mcp_request_counter}_{time.time_ns()}"

        _command_queue.put((req_id, command, params))

        # Poll for result
        deadline = time.time() + HTTP_TIMEOUT_SEC
        while time.time() < deadline:
            with _responses_lock:
                if req_id in _responses:
                    result = _responses.pop(req_id)
                    break
            time.sleep(POLL_INTERVAL_SEC)
        else:
            self._send_json(504, json.dumps({"success": False, "error": f"Command '{command}' timed out"}).encode())
            return

        self._send_json(200, json.dumps(result).encode())

    def log_message(self, fmt: str, *args: Any) -> None:
        """Suppress default stderr logging."""
        pass


# ---------------------------------------------------------------------------
# Tick callback (main thread)
# ---------------------------------------------------------------------------


_watchdog_state = {"last_check": 0.0}
WATCHDOG_INTERVAL_SEC = 30.0


def _watchdog_check() -> None:
    """Self-heal: verify the HTTP server thread is alive AND the port actually
    accepts connections; rebind if not (the old port may be held by a zombie
    socket, in which case _find_free_port moves to the next one)."""
    if unreal._mcp_server is None:
        return
    now = time.monotonic()
    if now - _watchdog_state["last_check"] < WATCHDOG_INTERVAL_SEC:
        return
    _watchdog_state["last_check"] = now

    thread = unreal._mcp_server_thread
    broken = thread is None or not thread.is_alive()
    if not broken:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        try:
            probe.connect(("127.0.0.1", unreal._mcp_bound_port))
        except OSError:
            broken = True
        finally:
            probe.close()
    if not broken:
        return

    _log("Watchdog: listener unreachable — rebinding", "warning")
    # Do NOT go through stop_listener(): shutdown() can block the main thread
    # if the serve loop is wedged. Close the socket and rebind fresh.
    try:
        unreal._mcp_server.server_close()
    except Exception:
        pass
    unreal._mcp_server = None
    unreal._mcp_server_thread = None
    unreal._mcp_bound_port = 0
    try:
        start_listener(show_status=False)
    except Exception as e:
        _log(f"Watchdog: restart failed: {e}", "error")


def _tick_handler(delta_time: float) -> None:
    """Process queued commands and main-thread tasks."""
    _watchdog_check()

    # Drain general-purpose main-thread queue
    while not _main_queue.empty():
        try:
            fn = _main_queue.get_nowait()
            fn()
        except queue.Empty:
            break
        except Exception as e:
            _log(f"Main-thread task error: {e}", "error")

    # Process MCP commands
    processed = 0
    while not _command_queue.empty() and processed < TICK_BATCH_LIMIT:
        try:
            req_id, command, params = _command_queue.get_nowait()
        except queue.Empty:
            break

        t0 = time.time()
        display = _describe_command(command, params)
        _current_task["command"] = display
        _current_task["start_time"] = t0
        try:
            result = _dispatch(command, params)
            response = {"success": True, "result": result}
        except Exception as e:
            tb = traceback.format_exc()
            _log(f"Command '{display}' failed: {e}", "error")
            response = {"success": False, "error": str(e), "traceback": tb}
            _metrics["total_errors"] += 1
            _metrics["last_error"] = str(e)
            _error_log.append({
                "time": time.time(),
                "command": display,
                "error": str(e),
                "traceback": tb,
            })
            if len(_error_log) > ERROR_LOG_SIZE:
                _error_log.pop(0)
        finally:
            _current_task["command"] = None
            _current_task["start_time"] = 0.0

        elapsed_ms = (time.time() - t0) * 1000
        _metrics["total_requests"] += 1
        _metrics["last_request_at"] = time.time()
        _metrics["last_command"] = display
        _metrics["response_times_ms"].append(elapsed_ms)
        if len(_metrics["response_times_ms"]) > 100:
            _metrics["response_times_ms"].pop(0)

        _task_log.append({
            "time": time.time(),
            "command": display,
            "success": response.get("success", False),
            "duration_ms": elapsed_ms,
            "error": response.get("error", ""),
        })
        if len(_task_log) > TASK_LOG_SIZE:
            _task_log.pop(0)

        with _responses_lock:
            _responses[req_id] = response
        processed += 1

    # Clean up stale responses
    now = time.time()
    with _responses_lock:
        stale = [k for k in _responses if float(k.split("_")[2]) / 1e9 < now - STALE_CLEANUP_SEC]
        for k in stale:
            del _responses[k]


# ---------------------------------------------------------------------------
# Shared tkinter root — one per process, all windows are Toplevel
# ---------------------------------------------------------------------------


def _get_tk_root() -> tk.Tk:
    """Return a tk.Tk root, reusing an pre-existing one if possible.

    Must be called from the tkinter thread only.
    All visible windows should use tk.Toplevel(root).
    """
    # Check if someone already created a Tk root in this process
    if hasattr(unreal, "_mcp_tk_root") and unreal._mcp_tk_root is not None:
        try:
            unreal._mcp_tk_root.winfo_exists()
            return unreal._mcp_tk_root
        except Exception:
            unreal._mcp_tk_root = None

    # Try to find an existing Tk instance (created by another script)
    try:
        existing = tk._default_root  # noqa: SLF001 — tkinter internal
        if existing is not None and existing.winfo_exists():
            unreal._mcp_tk_root = existing
            return existing
    except Exception:
        pass

    # No root exists — create a hidden one
    root = tk.Tk()
    root.withdraw()
    unreal._mcp_tk_root = root
    return root


# ---------------------------------------------------------------------------
# Status window (tkinter)
# ---------------------------------------------------------------------------


class MCPStatusWindow:
    """Compact floating status window for the MCP listener."""

    BG = "#1e1e1e"
    BG_SECTION = "#252525"
    FG = "#cccccc"
    FG_DIM = "#777777"
    GREEN = "#4ec94e"
    RED = "#e74c4c"
    YELLOW = "#e0c050"
    FONT = ("Segoe UI", 9)
    FONT_BOLD = ("Segoe UI", 10, "bold")
    FONT_BIG = ("Segoe UI", 12)
    UPDATE_MS = 200
    JUST_RAN_WINDOW_SEC = 2.0

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._window: Optional[tk.Toplevel] = None
        self._labels: Dict[str, tk.Label] = {}
        self._listener_dot: Optional[tk.Label] = None
        self._listener_text: Optional[tk.Label] = None
        self._client_dot: Optional[tk.Label] = None
        self._client_text: Optional[tk.Label] = None
        self._btn_toggle: Optional[tk.Button] = None
        self._btn_errors: Optional[tk.Button] = None
        self._btn_tasks: Optional[tk.Button] = None
        self._now_dot: Optional[tk.Label] = None
        self._now_text: Optional[tk.Label] = None
        self._port_var: Optional[tk.StringVar] = None
        self._port_entry: Optional[tk.Entry] = None
        self._errors_window: Optional[tk.Toplevel] = None
        self._errors_text: Optional[tk.Text] = None
        self._errors_shown_count: int = -1
        self._tasks_window: Optional[tk.Toplevel] = None
        self._tasks_text: Optional[tk.Text] = None
        self._tasks_shown_count: int = -1

    def start(self) -> None:
        """Open the status window in a background thread."""
        if self._thread and self._thread.is_alive() and self._window is not None:
            # Window already exists — it may have been hidden via _on_close.
            # Schedule the re-show on the tkinter thread to be safe.
            try:
                self._window.after(0, self.show)
            except Exception:
                try:
                    self.show()
                except Exception:
                    pass
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        root = _get_tk_root()
        self._create_window()
        root.mainloop()

    def _create_window(self) -> None:
        """Build the Toplevel status window. Safe to call multiple times."""
        root = getattr(unreal, "_mcp_tk_root", None)
        if root is None:
            return

        window = tk.Toplevel(root)
        self._window = window
        self._labels = {}
        window.title("MCP for UEFN")
        if os.path.isfile(ICON_PATH):
            try:
                self._icon_img = tk.PhotoImage(file=ICON_PATH)
                window.iconphoto(True, self._icon_img)
            except Exception:
                self._icon_img = None
        window.attributes("-topmost", True)
        window.configure(bg=self.BG)
        window.resizable(True, True)

        # -- Title --
        title_frame = tk.Frame(window, bg=self.BG)
        title_frame.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(title_frame, text="UEFN MCP Listener", font=self.FONT_BIG, fg=self.FG, bg=self.BG).pack(side="left")
        tk.Label(title_frame, text=f"v{PROTOCOL_VERSION} {VERSION_SUFFIX}", font=self.FONT, fg=self.FG_DIM, bg=self.BG).pack(side="right")

        # -- Status rows --
        hdr = tk.Frame(window, bg=self.BG)
        hdr.pack(fill="x", padx=12, pady=(4, 2))

        row1 = tk.Frame(hdr, bg=self.BG)
        row1.pack(fill="x")
        self._listener_dot = tk.Label(row1, text="\u25cf", font=self.FONT, fg=self.GREEN, bg=self.BG)
        self._listener_dot.pack(side="left")
        self._listener_text = tk.Label(row1, text="Listener: Running", font=self.FONT_BOLD, fg=self.FG, bg=self.BG)
        self._listener_text.pack(side="left", padx=(4, 0))

        row2 = tk.Frame(hdr, bg=self.BG)
        row2.pack(fill="x", pady=(2, 0))
        self._client_dot = tk.Label(row2, text="\u25cf", font=self.FONT, fg=self.FG_DIM, bg=self.BG)
        self._client_dot.pack(side="left")
        self._client_text = tk.Label(row2, text="MCP Server: Connecting...", font=self.FONT, fg=self.FG_DIM, bg=self.BG)
        self._client_text.pack(side="left", padx=(4, 0))

        row3 = tk.Frame(hdr, bg=self.BG)
        row3.pack(fill="x", pady=(2, 0))
        self._now_dot = tk.Label(row3, text="\u25cb", font=self.FONT, fg=self.FG_DIM, bg=self.BG)
        self._now_dot.pack(side="left")
        self._now_text = tk.Label(row3, text="Idle", font=self.FONT, fg=self.FG_DIM, bg=self.BG, anchor="w")
        self._now_text.pack(side="left", padx=(4, 0), fill="x", expand=True)

        tk.Frame(window, bg="#333333", height=1).pack(fill="x", padx=12, pady=4)

        info = tk.Frame(window, bg=self.BG)
        info.pack(fill="x", padx=12, pady=2)
        info.columnconfigure(1, weight=1)

        tk.Label(info, text="Port", font=self.FONT, fg=self.FG_DIM, bg=self.BG, anchor="w").grid(
            row=0, column=0, sticky="w", pady=1
        )
        self._port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self._port_entry = tk.Entry(
            info, textvariable=self._port_var, font=self.FONT, width=7,
            bg="#333333", fg=self.FG, insertbackground=self.FG,
            disabledbackground=self.BG, disabledforeground=self.FG,
            relief="flat", justify="right", state="disabled",
        )
        self._port_entry.grid(row=0, column=1, sticky="e", padx=(10, 0), pady=1)

        rows = [
            ("Uptime", "uptime"),
            ("Requests", "requests"),
            ("Errors", "errors"),
            ("Last cmd", "last_cmd"),
            ("Avg time", "avg_time"),
        ]
        for i, (label_text, key) in enumerate(rows, start=1):
            tk.Label(info, text=label_text, font=self.FONT, fg=self.FG_DIM, bg=self.BG, anchor="w").grid(
                row=i, column=0, sticky="w", pady=1
            )
            lbl = tk.Label(info, text="\u2014", font=self.FONT, fg=self.FG, bg=self.BG, anchor="e")
            lbl.grid(row=i, column=1, sticky="e", padx=(10, 0), pady=1)
            self._labels[key] = lbl

        tk.Frame(window, bg="#333333", height=1).pack(fill="x", padx=12, pady=4)

        btn_frame = tk.Frame(window, bg=self.BG)
        btn_frame.pack(fill="x", padx=12, pady=(2, 8))

        btn_cfg = dict(bg="#3c3c3c", fg=self.FG, activebackground="#4a4a4a", activeforeground=self.FG,
                       relief="flat", font=self.FONT, padx=12, pady=2, cursor="hand2")

        self._btn_toggle = tk.Button(btn_frame, text="Stop", command=self._on_toggle, **btn_cfg)
        self._btn_toggle.pack(side="left")

        tk.Button(btn_frame, text="Restart", command=self._on_restart, **btn_cfg).pack(side="left", padx=(6, 0))

        self._btn_errors = tk.Button(btn_frame, text="Errors", command=self._on_errors, **btn_cfg)
        self._btn_errors.pack(side="right")

        self._btn_tasks = tk.Button(btn_frame, text="Tasks", command=self._on_tasks, **btn_cfg)
        self._btn_tasks.pack(side="right", padx=(0, 6))

        self._update()
        window.protocol("WM_DELETE_WINDOW", self._on_close)

        window.update_idletasks()
        req_w = max(window.winfo_reqwidth(), 320)
        req_h = max(window.winfo_reqheight(), 300)
        window.geometry(f"{req_w}x{req_h}")
        window.minsize(req_w, req_h)

    def _update(self) -> None:
        if not self._window:
            return

        running = unreal._mcp_server is not None

        # Listener status
        if self._listener_dot:
            self._listener_dot.configure(fg=self.GREEN if running else self.RED)
        if self._listener_text:
            self._listener_text.configure(text="Listener: Running" if running else "Listener: Stopped")
        if self._btn_toggle:
            self._btn_toggle.configure(text="Stop" if running else "Start")

        # MCP Server heartbeat status
        last_ping = _metrics.get("last_client_ping", 0.0)
        if last_ping > 0:
            ago = int(time.time() - last_ping)
            if ago < 15:
                client_color = self.GREEN
                client_text = "MCP Server: Connected"
                client_fg = self.FG
            else:
                if ago < 60:
                    ago_str = f"{ago}s ago"
                elif ago < 3600:
                    ago_str = f"{ago // 60}m ago"
                else:
                    ago_str = f"{ago // 3600}h ago"
                client_color = self.FG_DIM
                client_text = f"MCP Server: Lost {ago_str}"
                client_fg = self.FG_DIM
        elif running:
            client_color = self.YELLOW
            client_text = "MCP Server: Connecting..."
            client_fg = self.FG_DIM
        else:
            client_color = self.FG_DIM
            client_text = "MCP Server: Not connected"
            client_fg = self.FG_DIM

        if self._client_dot:
            self._client_dot.configure(fg=client_color)
        if self._client_text:
            self._client_text.configure(text=client_text, fg=client_fg)

        # Now-executing row
        cur_cmd = _current_task.get("command")
        cur_start = _current_task.get("start_time") or 0.0
        q_size = _command_queue.qsize()
        if self._now_dot and self._now_text:
            if cur_cmd:
                elapsed = time.time() - cur_start
                if elapsed < 60:
                    elapsed_str = f"{elapsed:.2f}s" if elapsed < 10 else f"{elapsed:.1f}s"
                else:
                    elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
                queued_str = f"  (+{q_size} queued)" if q_size > 0 else ""
                self._now_dot.configure(text="\u25cf", fg=self.YELLOW)
                self._now_text.configure(text=f"Now: {cur_cmd}  {elapsed_str}{queued_str}", fg=self.FG)
            elif q_size > 0:
                self._now_dot.configure(text="\u25cb", fg=self.YELLOW)
                self._now_text.configure(text=f"Queued: {q_size}", fg=self.FG_DIM)
            elif _task_log:
                last = _task_log[-1]
                age = time.time() - last["time"]
                if age <= self.JUST_RAN_WINDOW_SEC:
                    ok = last["success"]
                    color = self.GREEN if ok else self.RED
                    dur = last["duration_ms"]
                    self._now_dot.configure(text="\u25cf", fg=color)
                    self._now_text.configure(
                        text=f"Just ran: {last['command']}  {dur:.1f} ms",
                        fg=self.FG,
                    )
                else:
                    self._now_dot.configure(text="\u25cb", fg=self.FG_DIM)
                    self._now_text.configure(text="Idle", fg=self.FG_DIM)
            else:
                self._now_dot.configure(text="\u25cb", fg=self.FG_DIM)
                self._now_text.configure(text="Idle", fg=self.FG_DIM)

        # Port entry: editable when stopped, locked when running
        if self._port_entry:
            if running:
                self._port_entry.configure(state="disabled")
                self._port_var.set(str(unreal._mcp_bound_port))
            else:
                self._port_entry.configure(state="normal")

        # Uptime
        if running and _metrics["started_at"] > 0:
            uptime = int(time.time() - _metrics["started_at"])
            h, rem = divmod(uptime, 3600)
            m, s = divmod(rem, 60)
            self._labels["uptime"].configure(text=f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s")
        else:
            self._labels["uptime"].configure(text="\u2014")

        # Requests
        self._labels["requests"].configure(text=str(_metrics["total_requests"]))

        # Errors
        errs = _metrics["total_errors"]
        self._labels["errors"].configure(text=str(errs), fg=self.RED if errs > 0 else self.FG)

        # Last command
        last = _metrics["last_command"]
        if last and _metrics["last_request_at"] > 0:
            ago = int(time.time() - _metrics["last_request_at"])
            if ago < 60:
                ago_str = f"{ago}s ago"
            elif ago < 3600:
                ago_str = f"{ago // 60}m ago"
            else:
                ago_str = f"{ago // 3600}h ago"
            self._labels["last_cmd"].configure(text=f"{last} ({ago_str})")
        else:
            self._labels["last_cmd"].configure(text="\u2014")

        # Avg response time
        times = _metrics["response_times_ms"]
        if times:
            avg = sum(times) / len(times)
            self._labels["avg_time"].configure(text=f"{avg:.1f} ms")
        else:
            self._labels["avg_time"].configure(text="\u2014")

        # Errors button — highlight red if there are any, show count
        if self._btn_errors:
            count = len(_error_log)
            if count > 0:
                self._btn_errors.configure(text=f"Errors ({count})", fg=self.RED)
            else:
                self._btn_errors.configure(text="Errors", fg=self.FG)

        # Tasks button — show total request count
        if self._btn_tasks:
            total = _metrics.get("total_requests", 0)
            self._btn_tasks.configure(text=f"Tasks ({total})" if total else "Tasks")

        # Live-refresh open history windows if new entries arrived
        if self._errors_window is not None and len(_error_log) != self._errors_shown_count:
            self._refresh_errors_text()
        if self._tasks_window is not None and len(_task_log) != self._tasks_shown_count:
            self._refresh_tasks_text()

        self._window.after(self.UPDATE_MS, self._update)

    def _on_toggle(self) -> None:
        if unreal._mcp_server is not None:
            _run_on_main_thread(stop_listener)
        else:
            # Read port from entry (0 = auto-detect)
            try:
                port = int(self._port_var.get())
            except (ValueError, TypeError):
                port = 0
            _run_on_main_thread(lambda: start_listener(port=port, show_status=False))

    def _on_restart(self) -> None:
        _run_on_main_thread(restart_listener)

    def _on_errors(self) -> None:
        """Open (or focus) the errors window."""
        if self._errors_window is not None:
            try:
                self._errors_window.lift()
                self._errors_window.focus_force()
                return
            except Exception:
                self._errors_window = None

        if self._window is None:
            return

        win = tk.Toplevel(self._window)
        self._errors_window = win
        win.title("UEFN MCP — Errors")
        win.geometry("720x420")
        win.minsize(400, 200)
        win.configure(bg=self.BG)

        toolbar = tk.Frame(win, bg=self.BG)
        toolbar.pack(fill="x", padx=8, pady=(8, 4))
        btn_cfg = dict(bg="#3c3c3c", fg=self.FG, activebackground="#4a4a4a",
                       activeforeground=self.FG, relief="flat", font=self.FONT,
                       padx=10, pady=2, cursor="hand2")
        tk.Button(toolbar, text="Clear", command=self._on_errors_clear, **btn_cfg).pack(side="left")
        tk.Button(toolbar, text="Copy all", command=self._on_errors_copy, **btn_cfg).pack(side="left", padx=(6, 0))
        tk.Button(toolbar, text="Close", command=self._on_errors_close, **btn_cfg).pack(side="right")

        body = tk.Frame(win, bg=self.BG)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        scrollbar = tk.Scrollbar(body, orient="vertical")
        scrollbar.pack(side="right", fill="y")

        text = tk.Text(
            body, wrap="word", font=("Consolas", 9),
            bg="#1a1a1a", fg=self.FG, insertbackground=self.FG,
            relief="flat", yscrollcommand=scrollbar.set,
        )
        text.tag_configure("header", foreground=self.RED, font=("Consolas", 9, "bold"))
        text.tag_configure("meta", foreground=self.FG_DIM)
        text.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=text.yview)

        self._errors_text = text
        self._refresh_errors_text()
        win.protocol("WM_DELETE_WINDOW", self._on_errors_close)

    def _refresh_errors_text(self) -> None:
        txt = self._errors_text
        if txt is None:
            return
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        if not _error_log:
            txt.insert("end", "No errors captured yet.\n", ("meta",))
        else:
            for i, entry in enumerate(_error_log, 1):
                ts = time.strftime("%H:%M:%S", time.localtime(entry["time"]))
                txt.insert("end", f"[{i}] {ts}  {entry['command']}\n", ("header",))
                txt.insert("end", f"{entry['error']}\n", ())
                tb = entry.get("traceback", "").rstrip()
                if tb:
                    txt.insert("end", f"{tb}\n", ("meta",))
                txt.insert("end", "\n")
        txt.configure(state="disabled")
        txt.see("end")
        self._errors_shown_count = len(_error_log)

    def _on_errors_clear(self) -> None:
        _error_log.clear()
        self._refresh_errors_text()

    def _on_errors_copy(self) -> None:
        if self._errors_text is None or self._errors_window is None:
            return
        try:
            content = self._errors_text.get("1.0", "end-1c")
            self._errors_window.clipboard_clear()
            self._errors_window.clipboard_append(content)
        except Exception:
            pass

    def _on_errors_close(self) -> None:
        if self._errors_window is not None:
            try:
                self._errors_window.destroy()
            except Exception:
                pass
            self._errors_window = None
            self._errors_text = None
            self._errors_shown_count = -1

    def _on_tasks(self) -> None:
        """Open (or focus) the tasks history window."""
        if self._tasks_window is not None:
            try:
                self._tasks_window.lift()
                self._tasks_window.focus_force()
                return
            except Exception:
                self._tasks_window = None

        if self._window is None:
            return

        win = tk.Toplevel(self._window)
        self._tasks_window = win
        win.title("UEFN MCP — Task History")
        win.geometry("720x420")
        win.minsize(400, 200)
        win.configure(bg=self.BG)

        toolbar = tk.Frame(win, bg=self.BG)
        toolbar.pack(fill="x", padx=8, pady=(8, 4))
        btn_cfg = dict(bg="#3c3c3c", fg=self.FG, activebackground="#4a4a4a",
                       activeforeground=self.FG, relief="flat", font=self.FONT,
                       padx=10, pady=2, cursor="hand2")
        tk.Button(toolbar, text="Clear", command=self._on_tasks_clear, **btn_cfg).pack(side="left")
        tk.Button(toolbar, text="Copy all", command=self._on_tasks_copy, **btn_cfg).pack(side="left", padx=(6, 0))
        self._tasks_summary = tk.Label(
            toolbar, text="", font=self.FONT, fg=self.FG_DIM, bg=self.BG,
        )
        self._tasks_summary.pack(side="left", padx=(12, 0))
        tk.Button(toolbar, text="Close", command=self._on_tasks_close, **btn_cfg).pack(side="right")

        body = tk.Frame(win, bg=self.BG)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        scrollbar = tk.Scrollbar(body, orient="vertical")
        scrollbar.pack(side="right", fill="y")

        text = tk.Text(
            body, wrap="none", font=("Consolas", 9),
            bg="#1a1a1a", fg=self.FG, insertbackground=self.FG,
            relief="flat", yscrollcommand=scrollbar.set,
        )
        text.tag_configure("ok", foreground=self.GREEN)
        text.tag_configure("fail", foreground=self.RED)
        text.tag_configure("slow", foreground=self.YELLOW)
        text.tag_configure("meta", foreground=self.FG_DIM)
        text.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=text.yview)

        self._tasks_text = text
        self._refresh_tasks_text()
        win.protocol("WM_DELETE_WINDOW", self._on_tasks_close)

    def _refresh_tasks_text(self) -> None:
        txt = self._tasks_text
        if txt is None:
            return
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        if not _task_log:
            txt.insert("end", "No tasks executed yet.\n", ("meta",))
        else:
            ok_count = sum(1 for e in _task_log if e["success"])
            fail_count = len(_task_log) - ok_count
            total_ms = sum(e["duration_ms"] for e in _task_log)
            avg_ms = total_ms / len(_task_log) if _task_log else 0.0
            if getattr(self, "_tasks_summary", None) is not None:
                self._tasks_summary.configure(
                    text=f"{len(_task_log)} shown  •  {ok_count} ok  •  {fail_count} failed  •  avg {avg_ms:.1f} ms"
                )
            for entry in reversed(_task_log):
                ts = time.strftime("%H:%M:%S", time.localtime(entry["time"]))
                status = "OK  " if entry["success"] else "FAIL"
                tag = "ok" if entry["success"] else "fail"
                dur = entry["duration_ms"]
                dur_tag = "slow" if dur >= 500 else "meta"
                txt.insert("end", f"{ts}  ", ("meta",))
                txt.insert("end", f"{status}  ", (tag,))
                txt.insert("end", f"{dur:>7.1f} ms  ", (dur_tag,))
                txt.insert("end", f"{entry['command']}\n", ())
                if not entry["success"] and entry.get("error"):
                    txt.insert("end", f"             {entry['error']}\n", ("fail",))
        txt.configure(state="disabled")
        self._tasks_shown_count = len(_task_log)

    def _on_tasks_clear(self) -> None:
        _task_log.clear()
        self._refresh_tasks_text()

    def _on_tasks_copy(self) -> None:
        if self._tasks_text is None or self._tasks_window is None:
            return
        try:
            content = self._tasks_text.get("1.0", "end-1c")
            self._tasks_window.clipboard_clear()
            self._tasks_window.clipboard_append(content)
        except Exception:
            pass

    def _on_tasks_close(self) -> None:
        if self._tasks_window is not None:
            try:
                self._tasks_window.destroy()
            except Exception:
                pass
            self._tasks_window = None
            self._tasks_text = None
            self._tasks_shown_count = -1

    def _on_close(self) -> None:
        """Hide the status window to the background instead of closing it.

        The listener keeps running; the window is only withdrawn. Re-open it by
        calling start_listener() / status_window.start() again (which deiconifies
        and re-focuses the existing window).
        """
        # Close the child windows (errors/tasks) so we don't leak them, but
        # keep the main window alive — just hidden.
        self._on_errors_close()
        self._on_tasks_close()
        if self._window:
            try:
                self._window.withdraw()
            except Exception:
                pass

    def show(self) -> None:
        """Re-show the status window after it was hidden via _on_close."""
        if self._window is None:
            return
        try:
            self._window.deiconify()
            self._window.lift()
            self._window.focus_force()
        except Exception:
            pass

    def hide(self) -> None:
        """Withdraw the status window (same as pressing the window's X)."""
        if self._window is None:
            return
        try:
            self._window.withdraw()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# System-tray icon (pystray)
# ---------------------------------------------------------------------------


def _show_status_window() -> None:
    """Show the status window, (re)creating it if it was fully closed.

    Safe to call from any thread — window mutation is marshalled onto the
    tkinter thread by MCPStatusWindow.start()/show().
    """
    win = unreal._mcp_status_window
    if win is not None and win.is_alive() and getattr(win, "_window", None) is not None:
        win.start()  # reuses + deiconifies the existing window
    else:
        unreal._mcp_status_window = MCPStatusWindow()
        unreal._mcp_status_window.start()


class MCPTrayIcon:
    """Real Windows system-tray icon for the MCP listener.

    Runs the pystray event loop on its own daemon thread. Menu actions fire on
    that pystray thread, so anything touching tkinter is marshalled via
    ``window.after(...)`` and anything touching the UE API via
    ``_run_on_main_thread(...)``.
    """

    def __init__(self) -> None:
        self._icon: Optional["pystray.Icon"] = None
        self._thread: Optional[threading.Thread] = None

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if not _TRAY_AVAILABLE:
            return
        if self.is_alive() and self._icon is not None:
            return
        try:
            image = PIL_Image.open(ICON_PATH)
        except Exception as e:
            _log(f"Tray icon image load failed: {e}", "warning")
            return

        menu = pystray.Menu(
            pystray.MenuItem("Show window", self._on_show, default=True),
            pystray.MenuItem("Hide window", self._on_hide),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart listener", self._on_restart),
            pystray.MenuItem(self._toggle_label, self._on_toggle),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit (stop & remove tray)", self._on_quit),
        )
        self._icon = pystray.Icon(
            "uefn_mcp",
            icon=image,
            title=self._title(),
            menu=menu,
        )
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        _log("Tray icon started")

    def _run(self) -> None:
        try:
            self._icon.run()
        except Exception as e:
            _log(f"Tray icon loop crashed: {e}", "warning")

    def stop(self) -> None:
        icon = self._icon
        self._icon = None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    # -- dynamic labels --------------------------------------------------
    def _title(self) -> str:
        if unreal._mcp_server is not None:
            return f"UEFN MCP — running (port {unreal._mcp_bound_port})"
        return "UEFN MCP — stopped"

    def _toggle_label(self, _item: Any) -> str:
        return "Stop listener" if unreal._mcp_server is not None else "Start listener"

    def refresh(self) -> None:
        """Update tray title + menu to reflect the current listener state."""
        if self._icon is None:
            return
        try:
            self._icon.title = self._title()
            self._icon.update_menu()
        except Exception:
            pass

    # -- menu actions (invoked on the pystray thread) --------------------
    def _on_show(self, _icon: Any = None, _item: Any = None) -> None:
        _show_status_window()

    def _on_hide(self, _icon: Any = None, _item: Any = None) -> None:
        win = unreal._mcp_status_window
        if win is not None and getattr(win, "_window", None) is not None:
            try:
                win._window.after(0, win.hide)
            except Exception:
                pass

    def _on_toggle(self, _icon: Any = None, _item: Any = None) -> None:
        if unreal._mcp_server is not None:
            _run_on_main_thread(stop_listener)
        else:
            _run_on_main_thread(lambda: start_listener(show_status=False))
        self.refresh()

    def _on_restart(self, _icon: Any = None, _item: Any = None) -> None:
        _run_on_main_thread(restart_listener)
        self.refresh()

    def _on_quit(self, _icon: Any = None, _item: Any = None) -> None:
        # Stop serving and remove the tray icon. The window (if any) is hidden,
        # not destroyed, so re-running start_listener() restores everything.
        _run_on_main_thread(stop_listener)
        win = unreal._mcp_status_window
        if win is not None and getattr(win, "_window", None) is not None:
            try:
                win._window.after(0, win.hide)
            except Exception:
                pass
        self.stop()


def _start_tray() -> None:
    """Create + start the tray icon (once), if pystray is available."""
    if not _TRAY_AVAILABLE:
        return
    tray = unreal._mcp_tray
    if tray is None:
        tray = MCPTrayIcon()
        unreal._mcp_tray = tray
    tray.start()


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Find a free port in the configured range.

    NOTE: deliberately NO SO_REUSEADDR — on Windows it lets bind() succeed on
    a port still held by a zombie socket (leaked when UEFN disables Python on
    project close/switch without the listener closing its socket), which made
    this probe report the dead port as free and left the new listener
    unreachable behind the zombie.
    """
    for port in range(DEFAULT_PORT, MAX_PORT + 1):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            continue
    raise RuntimeError(f"No free port in range {DEFAULT_PORT}-{MAX_PORT}")


class _ExclusiveHTTPServer(HTTPServer):
    # HTTPServer defaults allow_reuse_address=1; on Windows that binds over
    # zombie sockets leaked by a previous Python interpreter (project switch),
    # producing a listener that logs success but never receives connections.
    allow_reuse_address = False


def start_listener(port: int = 0, show_status: bool = True) -> int:
    """Start the MCP listener. Returns the bound port.

    Args:
        port: Port to bind to. 0 = auto-detect free port.
        show_status: Open the status window.
    """
    if unreal._mcp_server is not None:
        _log(f"Listener already running on port {unreal._mcp_bound_port}", "warning")
        if show_status:
            _show_status_window()
        _start_tray()
        return unreal._mcp_bound_port

    if port == 0:
        port = _find_free_port()

    unreal._mcp_server = _ExclusiveHTTPServer(("127.0.0.1", port), _MCPHandler)
    unreal._mcp_bound_port = port

    unreal._mcp_server_thread = threading.Thread(
        target=unreal._mcp_server.serve_forever, daemon=True,
    )
    unreal._mcp_server_thread.start()

    if unreal._mcp_tick_handle is None:
        unreal._mcp_tick_handle = unreal.register_slate_post_tick_callback(_tick_handler)

    _metrics["started_at"] = time.time()

    _log(f"Listener started on http://127.0.0.1:{port}")
    _log(f"Registered {len(_HANDLERS)} command handlers")

    if show_status:
        _show_status_window()

    # Real system-tray icon (persists for the editor session).
    _start_tray()
    if unreal._mcp_tray is not None:
        unreal._mcp_tray.refresh()

    return port


def stop_listener() -> None:
    """Stop the HTTP server. The tick callback stays alive for _main_queue."""
    if unreal._mcp_server is None:
        _log("Listener is not running", "warning")
        return

    unreal._mcp_server.shutdown()
    if unreal._mcp_server_thread is not None:
        unreal._mcp_server_thread.join(timeout=3.0)

    unreal._mcp_server = None
    unreal._mcp_server_thread = None
    _log(f"Listener stopped (was on port {unreal._mcp_bound_port})")
    unreal._mcp_bound_port = 0
    _metrics["started_at"] = 0.0
    _metrics["last_client_ping"] = 0.0

    if unreal._mcp_tray is not None:
        unreal._mcp_tray.refresh()


def cleanup() -> None:
    """Full cleanup: stop listener, remove tray icon AND unregister tick callback."""
    stop_listener()
    if unreal._mcp_tray is not None:
        try:
            unreal._mcp_tray.stop()
        except Exception:
            pass
        unreal._mcp_tray = None
    if unreal._mcp_tick_handle is not None:
        unreal.unregister_slate_post_tick_callback(unreal._mcp_tick_handle)
        unreal._mcp_tick_handle = None


def restart_listener(port: int = 0) -> int:
    """Restart the MCP listener."""
    stop_listener()
    time.sleep(0.5)
    return start_listener(port, show_status=False)


# ---------------------------------------------------------------------------
# Python-shutdown cleanup
# ---------------------------------------------------------------------------
# UEFN destroys the Python interpreter when the project is closed or switched
# (IPythonScriptPlugin::DisablePythonAtRuntime). The listening socket is a
# process-level OS handle: left open it survives the interpreter as a zombie
# that keeps the port bound and refuses every connection until the editor
# exits. Close it (and the tray icon) while the interpreter is still alive.


def _release_resources_at_python_shutdown() -> None:
    srv = getattr(unreal, "_mcp_server", None)
    if srv is not None:
        try:
            srv.server_close()
        except Exception:
            pass
        unreal._mcp_server = None
        unreal._mcp_server_thread = None
        unreal._mcp_bound_port = 0
    tray = getattr(unreal, "_mcp_tray", None)
    if tray is not None:
        try:
            tray.stop()
        except Exception:
            pass
        unreal._mcp_tray = None


atexit.register(_release_resources_at_python_shutdown)
if hasattr(unreal, "register_python_shutdown_callback"):
    try:
        unreal.register_python_shutdown_callback(_release_resources_at_python_shutdown)
    except Exception:
        pass  # atexit registration above is the fallback


# ---------------------------------------------------------------------------
# Auto-start when script is executed directly
# ---------------------------------------------------------------------------

try:
    # If a previous HTTP server exists, close its socket to free the port.
    if unreal._mcp_server is not None:
        _log("Previous listener detected — replacing")
        try:
            unreal._mcp_server.server_close()
        except Exception:
            pass
        unreal._mcp_server = None
        unreal._mcp_server_thread = None
        unreal._mcp_bound_port = 0

    # Unregister old tick handle so we don't get duplicates
    _old_tick = unreal._mcp_tick_handle
    if _old_tick is not None:
        unreal.unregister_slate_post_tick_callback(_old_tick)
        unreal._mcp_tick_handle = None

    # Remove any old tray icon so start_listener recreates one bound to the
    # freshly-loaded code (avoids a stale duplicate icon after a re-run).
    if unreal._mcp_tray is not None:
        try:
            unreal._mcp_tray.stop()
        except Exception:
            pass
        unreal._mcp_tray = None

    # NEVER touch the old tkinter window — two tk.Tk() crashes tcl.
    # If the old window is still alive, start_listener will reuse it.
    start_listener()
except Exception as _e:
    unreal.log_error(f"[MCP] Failed to start listener: {_e}")
    import traceback
    traceback.print_exc()
