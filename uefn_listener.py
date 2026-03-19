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
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Optional, Tuple

import unreal

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

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

_server: Optional[HTTPServer] = None
_server_thread: Optional[threading.Thread] = None
_tick_handle: Optional[object] = None
_bound_port: int = 0

_command_queue: queue.Queue = queue.Queue()
_responses: Dict[str, dict] = {}
_responses_lock = threading.Lock()
_request_counter = 0

_log_ring: List[str] = []

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
    result = {
        "name": actor.get_name(),
        "label": actor.get_actor_label(),
        "class": actor.get_class().get_name(),
        "path": actor.get_path_name(),
        "location": _serialize(actor.get_actor_location()),
        "rotation": _serialize(actor.get_actor_rotation()),
        "scale": _serialize(actor.get_actor_scale3d()),
    }
    return result


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
        "python_version": sys.version,
        "port": _bound_port,
        "timestamp": time.time(),
        "commands": list(_HANDLERS.keys()),
    }


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
    asset_reg = unreal.AssetRegistryHelpers.get_asset_registry()
    ar_filter = unreal.ARFilter()
    if directory:
        ar_filter.package_paths = [directory]
    ar_filter.recursive_paths = recursive
    if class_name:
        try:
            ar_filter.class_names = [class_name]
        except Exception:
            pass
    results = asset_reg.get_assets(ar_filter)
    return {"assets": [_serialize(a) for a in results], "count": len(results)}


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

    def do_GET(self) -> None:
        """Health check and tool manifest."""
        body = json.dumps({
            "status": "ok",
            "port": _bound_port,
            "commands": list(_HANDLERS.keys()),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        """Execute a command."""
        global _request_counter

        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            self._error(400, f"Invalid JSON: {e}")
            return

        command = body.get("command", "")
        params = body.get("params", {})
        if not command:
            self._error(400, "Missing 'command' field")
            return

        _request_counter += 1
        req_id = f"req_{_request_counter}_{time.time_ns()}"

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
            self._error(504, f"Command '{command}' timed out after {HTTP_TIMEOUT_SEC}s")
            return

        resp_body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def _error(self, code: int, message: str) -> None:
        body = json.dumps({"success": False, "error": message}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        """Suppress default stderr logging."""
        pass


# ---------------------------------------------------------------------------
# Tick callback (main thread)
# ---------------------------------------------------------------------------


def _tick_handler(delta_time: float) -> None:
    """Process queued commands on the main thread."""
    processed = 0
    while not _command_queue.empty() and processed < TICK_BATCH_LIMIT:
        try:
            req_id, command, params = _command_queue.get_nowait()
        except queue.Empty:
            break
        try:
            result = _dispatch(command, params)
            response = {"success": True, "result": result}
        except Exception as e:
            _log(f"Command '{command}' failed: {e}", "error")
            response = {"success": False, "error": str(e), "traceback": traceback.format_exc()}
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


def start_listener(port: int = 0) -> int:
    """Start the MCP listener. Returns the bound port.

    Args:
        port: Port to bind to. 0 = auto-detect free port.
    """
    global _server, _server_thread, _tick_handle, _bound_port

    if _server is not None:
        _log(f"Listener already running on port {_bound_port}", "warning")
        return _bound_port

    if port == 0:
        port = _find_free_port()

    _server = HTTPServer(("127.0.0.1", port), _MCPHandler)
    _bound_port = port

    _server_thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _server_thread.start()

    _tick_handle = unreal.register_slate_post_tick_callback(_tick_handler)

    _log(f"Listener started on http://127.0.0.1:{port}")
    _log(f"Registered {len(_HANDLERS)} command handlers")
    return port


def stop_listener() -> None:
    """Stop the MCP listener."""
    global _server, _server_thread, _tick_handle, _bound_port

    if _server is None:
        _log("Listener is not running", "warning")
        return

    if _tick_handle is not None:
        unreal.unregister_slate_post_tick_callback(_tick_handle)
        _tick_handle = None

    _server.shutdown()
    if _server_thread is not None:
        _server_thread.join(timeout=3.0)

    _server = None
    _server_thread = None
    _log(f"Listener stopped (was on port {_bound_port})")
    _bound_port = 0


def restart_listener(port: int = 0) -> int:
    """Restart the MCP listener."""
    stop_listener()
    time.sleep(0.5)
    return start_listener(port)


# ---------------------------------------------------------------------------
# Auto-start when script is executed directly
# ---------------------------------------------------------------------------

start_listener()
