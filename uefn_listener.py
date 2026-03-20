"""MCP HTTP Listener for UEFN Editor.

Runs an HTTP server on a background thread inside the UEFN editor.
All unreal.* API calls are dispatched to the main thread via tick callback.

Usage (in UEFN editor console):
    py "path/to/uefn_listener.py"

Or auto-start via init_unreal.py.
"""

import io
import json
import queue
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

PROTOCOL_VERSION = "0.2.0"
DEFAULT_PORT = 8765
MAX_PORT = 8770
TICK_BATCH_LIMIT = 5
HTTP_TIMEOUT_SEC = 30.0
POLL_INTERVAL_SEC = 0.02
STALE_CLEANUP_SEC = 60.0
LOG_RING_SIZE = 200

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
    rotation: Optional[List[float]] = None,
) -> dict:
    loc = unreal.Vector(*location) if location else unreal.Vector(0, 0, 0)
    rot = unreal.Rotator(*rotation) if rotation else unreal.Rotator(0, 0, 0)

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
    rotation: Optional[List[float]] = None,
    scale: Optional[List[float]] = None,
) -> dict:
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
    if rotation is not None:
        target.set_actor_rotation(unreal.Rotator(*rotation), False)
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
    cam_rot = unreal.Rotator(-35, 45, 0)

    unreal.EditorLevelLibrary.set_level_viewport_camera_info(cam_loc, cam_rot)
    return {
        "center": {"x": center_x, "y": center_y, "z": center_z},
        "camera": _serialize(cam_loc),
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
    rotation: Optional[List[float]] = None,
) -> dict:
    cur_loc, cur_rot = unreal.EditorLevelLibrary.get_level_viewport_camera_info()
    loc = unreal.Vector(*location) if location else cur_loc
    rot = unreal.Rotator(*rotation) if rotation else cur_rot
    unreal.EditorLevelLibrary.set_level_viewport_camera_info(loc, rot)
    return {"location": _serialize(loc), "rotation": _serialize(rot)}


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


def _tick_handler(delta_time: float) -> None:
    """Process queued commands and main-thread tasks."""
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
        try:
            result = _dispatch(command, params)
            response = {"success": True, "result": result}
        except Exception as e:
            _log(f"Command '{command}' failed: {e}", "error")
            response = {"success": False, "error": str(e), "traceback": traceback.format_exc()}
            _metrics["total_errors"] += 1
            _metrics["last_error"] = str(e)

        elapsed_ms = (time.time() - t0) * 1000
        _metrics["total_requests"] += 1
        _metrics["last_request_at"] = time.time()
        _metrics["last_command"] = command
        _metrics["response_times_ms"].append(elapsed_ms)
        if len(_metrics["response_times_ms"]) > 100:
            _metrics["response_times_ms"].pop(0)

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
    UPDATE_MS = 1000

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._window: Optional[tk.Toplevel] = None
        self._labels: Dict[str, tk.Label] = {}
        self._listener_dot: Optional[tk.Label] = None
        self._listener_text: Optional[tk.Label] = None
        self._client_dot: Optional[tk.Label] = None
        self._client_text: Optional[tk.Label] = None
        self._btn_toggle: Optional[tk.Button] = None
        self._port_var: Optional[tk.StringVar] = None
        self._port_entry: Optional[tk.Entry] = None

    def start(self) -> None:
        """Open the status window in a background thread."""
        if self._thread and self._thread.is_alive() and self._window is not None:
            try:
                self._window.lift()
                self._window.focus_force()
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
        window.title("You can close this window")
        window.geometry("260x295")
        window.attributes("-topmost", True)
        window.configure(bg=self.BG)
        window.resizable(False, False)

        # -- Title --
        title_frame = tk.Frame(window, bg=self.BG)
        title_frame.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(title_frame, text="UEFN MCP Listener", font=self.FONT_BIG, fg=self.FG, bg=self.BG).pack(side="left")
        tk.Label(title_frame, text=f"v{PROTOCOL_VERSION}", font=self.FONT, fg=self.FG_DIM, bg=self.BG).pack(side="right")

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

        self._update()
        window.protocol("WM_DELETE_WINDOW", self._on_close)

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

    def _on_close(self) -> None:
        if self._window:
            self._window.destroy()
            self._window = None


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Find a free port in the configured range."""
    for port in range(DEFAULT_PORT, MAX_PORT + 1):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            continue
    raise RuntimeError(f"No free port in range {DEFAULT_PORT}-{MAX_PORT}")


def start_listener(port: int = 0, show_status: bool = True) -> int:
    """Start the MCP listener. Returns the bound port.

    Args:
        port: Port to bind to. 0 = auto-detect free port.
        show_status: Open the status window.
    """
    if unreal._mcp_server is not None:
        _log(f"Listener already running on port {unreal._mcp_bound_port}", "warning")
        if show_status and unreal._mcp_status_window:
            unreal._mcp_status_window.start()
        return unreal._mcp_bound_port

    if port == 0:
        port = _find_free_port()

    unreal._mcp_server = HTTPServer(("127.0.0.1", port), _MCPHandler)
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
        win = unreal._mcp_status_window
        # Reuse only if thread alive AND window visible
        if win is not None and win.is_alive() and getattr(win, "_window", None) is not None:
            win.start()
        else:
            # Create fresh window
            unreal._mcp_status_window = MCPStatusWindow()
            unreal._mcp_status_window.start()

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


def cleanup() -> None:
    """Full cleanup: stop listener AND unregister tick callback."""
    stop_listener()
    if unreal._mcp_tick_handle is not None:
        unreal.unregister_slate_post_tick_callback(unreal._mcp_tick_handle)
        unreal._mcp_tick_handle = None


def restart_listener(port: int = 0) -> int:
    """Restart the MCP listener."""
    stop_listener()
    time.sleep(0.5)
    return start_listener(port, show_status=False)


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

    # NEVER touch the old tkinter window — two tk.Tk() crashes tcl.
    # If the old window is still alive, start_listener will reuse it.
    start_listener()
except Exception as _e:
    unreal.log_error(f"[MCP] Failed to start listener: {_e}")
    import traceback
    traceback.print_exc()
