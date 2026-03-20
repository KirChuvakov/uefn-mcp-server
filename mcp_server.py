"""MCP Server for UEFN Editor.

External process that bridges Claude Code (stdio) to the UEFN HTTP listener.
Requires: pip install mcp

Usage:
    python mcp_server.py
    python mcp_server.py --port 8765

Claude Code config (~/.claude/settings.json or project .mcp.json):
    {
      "mcpServers": {
        "uefn": {
          "command": "python",
          "args": ["/path/to/mcp_server.py"]
        }
      }
    }
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_PORT = int(os.environ.get("UEFN_MCP_PORT", "8765"))
MAX_PORT = 8770
REQUEST_TIMEOUT = 30.0

_discovered_port: Optional[int] = None

# ---------------------------------------------------------------------------
# Port discovery
# ---------------------------------------------------------------------------


def _discover_port() -> int:
    """Find the listener by scanning the port range.

    Tries the last known port first, then scans DEFAULT_PORT..MAX_PORT.
    Caches the result so subsequent calls are instant.
    """
    global _discovered_port

    # Fast path: already discovered and still alive
    if _discovered_port is not None:
        if _ping_port(_discovered_port):
            return _discovered_port
        _discovered_port = None

    # Scan the range
    for port in range(DEFAULT_PORT, MAX_PORT + 1):
        if _ping_port(port):
            _discovered_port = port
            return port

    raise ConnectionError(
        f"UEFN listener not found on ports {DEFAULT_PORT}-{MAX_PORT}. "
        "Start it in the UEFN editor console: py \"path/to/uefn_listener.py\""
    )


def _ping_port(port: int) -> bool:
    """Quick check if a listener responds on the given port."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            body = json.loads(resp.read().decode())
            return body.get("status") == "ok"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def _send_command(command: str, params: Optional[dict] = None, timeout: float = REQUEST_TIMEOUT) -> dict:
    """Send a command to the UEFN listener and return the result.

    Auto-discovers the listener port by scanning the range.

    Raises:
        ConnectionError: Listener is not running.
        RuntimeError: Command failed on the UEFN side.
        TimeoutError: Command timed out.
    """
    global _discovered_port

    port = _discover_port()
    url = f"http://127.0.0.1:{port}"

    payload = json.dumps({"command": command, "params": params or {}}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        # Port may have changed — invalidate cache and retry once
        if _discovered_port is not None:
            _discovered_port = None
            return _send_command(command, params, timeout)
        raise ConnectionError(
            "UEFN listener is not running. "
            "Start it in the UEFN editor console: py \"path/to/uefn_listener.py\""
        ) from e
    except Exception as e:
        if "timed out" in str(e).lower():
            raise TimeoutError(f"Command '{command}' timed out after {timeout}s") from e
        raise

    if not body.get("success", False):
        error_msg = body.get("error", "Unknown error")
        tb = body.get("traceback", "")
        raise RuntimeError(f"UEFN command '{command}' failed: {error_msg}\n{tb}".strip())

    return body.get("result", {})


def _check_connection() -> str:
    """Quick connection check, returns status message."""
    try:
        port = _discover_port()
        return f"Connected to UEFN on port {port}"
    except ConnectionError:
        return "NOT CONNECTED - UEFN listener is not running"
    except Exception as e:
        return f"Connection error: {e}"


# ---------------------------------------------------------------------------
# Heartbeat — periodic ping so the listener knows we're alive
# ---------------------------------------------------------------------------

_HEARTBEAT_INTERVAL = 10.0


def _heartbeat_loop() -> None:
    """Ping the listener periodically."""
    time.sleep(3.0)  # wait for listener to be ready
    while True:
        try:
            port = _discover_port()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}",
                method="GET",
            )
            urllib.request.urlopen(req, timeout=2.0)
        except Exception:
            pass
        time.sleep(_HEARTBEAT_INTERVAL)


threading.Thread(target=_heartbeat_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "uefn-mcp",
    instructions=(
        "MCP server for controlling UEFN (Unreal Editor for Fortnite). "
        "Provides tools to manage actors, assets, levels, and viewport in the UEFN editor. "
        "The 'execute_python' tool is the most powerful — it runs arbitrary Python code "
        "inside the editor with full access to the `unreal` module. "
        "Use structured tools for common operations and execute_python for everything else.\n\n"
        "IMPORTANT: When creating tkinter UI windows via execute_python, NEVER call tk.Tk(). "
        "Use `root = get_tk_root()` to get the shared root, then `tk.Toplevel(root)` for windows. "
        "Multiple tk.Tk() instances will crash the editor."
    ),
)


# -- System tools ------------------------------------------------------------


@mcp.tool()
def ping() -> str:
    """Check if the UEFN editor listener is running and responsive."""
    result = _send_command("ping")
    return json.dumps(result, indent=2)


@mcp.tool()
def execute_python(code: str) -> str:
    """Execute arbitrary Python code inside the UEFN editor.

    The code runs on the main editor thread with full access to the `unreal` module.
    Pre-populated variables: unreal, actor_sub, asset_sub, level_sub, tk, get_tk_root.
    Assign to `result` variable to return a value. Use print() for stdout output.

    IMPORTANT — tkinter windows:
        Use get_tk_root() to get the shared tk.Tk() root, then create windows with
        tk.Toplevel(root). NEVER create a new tk.Tk() — multiple Tk instances crash
        the editor. The root is shared across all scripts in the process.

    Examples:
        # Get world name
        result = unreal.EditorLevelLibrary.get_editor_world().get_name()

        # List all static mesh actors
        actors = actor_sub.get_all_level_actors()
        result = [a.get_actor_label() for a in actors if a.get_class().get_name() == 'StaticMeshActor']

        # Create a material
        mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            'M_Test', '/Game/Materials', unreal.Material, unreal.MaterialFactoryNew()
        )
        result = str(mat.get_path_name())

        # Create a tkinter window (ALWAYS use Toplevel, never tk.Tk!)
        import threading
        def show_window():
            root = get_tk_root()
            win = tk.Toplevel(root)
            win.title("My Tool")
            win.attributes("-topmost", True)
            tk.Label(win, text="Hello from UEFN").pack(padx=20, pady=20)
            root.mainloop()
        threading.Thread(target=show_window, daemon=True).start()
        result = "Window opened"
    """
    result = _send_command("execute_python", {"code": code})
    parts = []
    if result.get("stdout"):
        parts.append(f"stdout:\n{result['stdout']}")
    if result.get("stderr"):
        parts.append(f"stderr:\n{result['stderr']}")
    if result.get("result") is not None:
        parts.append(f"result: {json.dumps(result['result'], indent=2)}")
    return "\n".join(parts) if parts else "(no output)"


@mcp.tool()
def get_log(last_n: int = 50) -> str:
    """Get recent MCP listener log entries from the UEFN editor."""
    result = _send_command("get_log", {"last_n": last_n})
    return "\n".join(result.get("lines", []))


@mcp.tool()
def shutdown() -> str:
    """Gracefully stop the UEFN listener, freeing the port.

    The listener will finish the current request, then shut down.
    After this call the listener must be restarted from the UEFN console.
    """
    result = _send_command("shutdown", timeout=5.0)
    return json.dumps(result, indent=2)


# -- Actor tools -------------------------------------------------------------


@mcp.tool()
def get_all_actors(class_filter: str = "") -> str:
    """List all actors in the current level.

    Args:
        class_filter: Optional class name to filter by (e.g. 'StaticMeshActor', 'PointLight').
    """
    result = _send_command("get_all_actors", {"class_filter": class_filter})
    return json.dumps(result, indent=2)


@mcp.tool()
def get_selected_actors() -> str:
    """Get currently selected actors in the UEFN viewport."""
    result = _send_command("get_selected_actors")
    return json.dumps(result, indent=2)


@mcp.tool()
def spawn_actor(
    asset_path: str = "",
    actor_class: str = "",
    location: Optional[list[float]] = None,
    rotation: Optional[list[float]] = None,
) -> str:
    """Spawn an actor in the current level.

    Provide either asset_path OR actor_class (not both).

    Args:
        asset_path: Asset path to spawn from (e.g. '/Engine/BasicShapes/Cube').
        actor_class: Unreal class name (e.g. 'PointLight', 'CameraActor').
        location: [x, y, z] coordinates. Defaults to origin.
        rotation: [pitch, yaw, roll] in degrees. Defaults to zero.
    """
    params: dict[str, Any] = {}
    if asset_path:
        params["asset_path"] = asset_path
    if actor_class:
        params["actor_class"] = actor_class
    if location is not None:
        params["location"] = location
    if rotation is not None:
        params["rotation"] = rotation
    result = _send_command("spawn_actor", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def delete_actors(actor_paths: list[str]) -> str:
    """Delete actors from the current level by path or label.

    Args:
        actor_paths: List of actor path names or labels to delete.
    """
    result = _send_command("delete_actors", {"actor_paths": actor_paths})
    return json.dumps(result, indent=2)


@mcp.tool()
def set_actor_transform(
    actor_path: str,
    location: Optional[list[float]] = None,
    rotation: Optional[list[float]] = None,
    scale: Optional[list[float]] = None,
) -> str:
    """Set an actor's transform (location, rotation, and/or scale).

    Args:
        actor_path: Actor path name or label.
        location: [x, y, z] world coordinates.
        rotation: [pitch, yaw, roll] in degrees.
        scale: [x, y, z] scale factors.
    """
    params: dict[str, Any] = {"actor_path": actor_path}
    if location is not None:
        params["location"] = location
    if rotation is not None:
        params["rotation"] = rotation
    if scale is not None:
        params["scale"] = scale
    result = _send_command("set_actor_transform", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def get_actor_properties(actor_path: str, properties: list[str]) -> str:
    """Read specific properties from an actor.

    Note: UEFN uses Fort*-prefixed actor classes (e.g. FortStaticMeshActor instead of
    StaticMeshActor). Some standard UE5 property names may not exist on Fort* actors.
    Properties that fail to read will return an error string instead of a value.

    Args:
        actor_path: Actor path name or label.
        properties: List of property names to read (e.g. ['static_mesh_component', 'mobility']).
    """
    result = _send_command("get_actor_properties", {"actor_path": actor_path, "properties": properties})
    return json.dumps(result, indent=2)


@mcp.tool()
def set_actor_properties(actor_path: str, properties: dict[str, Any]) -> str:
    """Set properties on an actor via set_editor_property().

    Note: UEFN uses Fort*-prefixed actor classes (e.g. FortStaticMeshActor instead of
    StaticMeshActor). Not all properties are writable — some are read-only or don't exist
    on Fort* actors. For methods like set_actor_hidden_in_game(), use execute_python instead.
    Each property reports 'ok' or an error individually.

    Args:
        actor_path: Actor path name or label.
        properties: Dict of property names to values (e.g. {'cast_shadow': False}).
    """
    result = _send_command("set_actor_properties", {"actor_path": actor_path, "properties": properties})
    return json.dumps(result, indent=2)


@mcp.tool()
def select_actors(actor_paths: list[str], add_to_selection: bool = False) -> str:
    """Select actors in the UEFN viewport.

    Args:
        actor_paths: List of actor path names or labels to select.
        add_to_selection: If True, add to current selection instead of replacing.
    """
    result = _send_command("select_actors", {"actor_paths": actor_paths, "add_to_selection": add_to_selection})
    return json.dumps(result, indent=2)


@mcp.tool()
def focus_selected() -> str:
    """Move the viewport camera to focus on the currently selected actors (like pressing F)."""
    result = _send_command("focus_selected")
    return json.dumps(result, indent=2)



@mcp.tool()
def get_editor_log(last_n: int = 100, filter_str: str = "") -> str:
    """Read recent lines from the Unreal Editor Output Log.

    Args:
        last_n: Number of recent lines to return.
        filter_str: Optional filter — only lines containing this string (case-insensitive).
    """
    result = _send_command("get_editor_log", {"last_n": last_n, "filter_str": filter_str})
    lines = result.get("lines", [])
    if result.get("error"):
        return f"Error: {result['error']}"
    return "\n".join(lines)


# -- Asset tools -------------------------------------------------------------


@mcp.tool()
def list_assets(directory: str = "/Game/", recursive: bool = True, class_filter: str = "") -> str:
    """List assets in a directory.

    Args:
        directory: Content directory path (e.g. '/Game/', '/Game/Materials/').
        recursive: Include subdirectories.
        class_filter: Optional class name filter (e.g. 'Material', 'StaticMesh').
    """
    result = _send_command("list_assets", {"directory": directory, "recursive": recursive, "class_filter": class_filter})
    return json.dumps(result, indent=2)


@mcp.tool()
def get_asset_info(asset_path: str) -> str:
    """Get detailed info about an asset.

    Args:
        asset_path: Full asset path (e.g. '/Game/Materials/M_Base').
    """
    result = _send_command("get_asset_info", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def get_selected_assets() -> str:
    """Get assets currently selected in the Content Browser."""
    result = _send_command("get_selected_assets")
    return json.dumps(result, indent=2)


@mcp.tool()
def rename_asset(old_path: str, new_path: str) -> str:
    """Rename or move an asset.

    Args:
        old_path: Current asset path.
        new_path: New asset path.
    """
    result = _send_command("rename_asset", {"old_path": old_path, "new_path": new_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def delete_asset(asset_path: str) -> str:
    """Delete an asset.

    Args:
        asset_path: Asset path to delete.
    """
    result = _send_command("delete_asset", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def duplicate_asset(source_path: str, dest_path: str) -> str:
    """Duplicate an asset to a new path.

    Args:
        source_path: Source asset path.
        dest_path: Destination asset path.
    """
    result = _send_command("duplicate_asset", {"source_path": source_path, "dest_path": dest_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def does_asset_exist(asset_path: str) -> str:
    """Check if an asset exists at the given path.

    Args:
        asset_path: Asset path to check.
    """
    result = _send_command("does_asset_exist", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def save_asset(asset_path: str) -> str:
    """Save a modified asset.

    Args:
        asset_path: Asset path to save.
    """
    result = _send_command("save_asset", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def search_assets(class_name: str = "", directory: str = "/Game/", recursive: bool = True) -> str:
    """Search for assets using the Asset Registry.

    Args:
        class_name: Filter by class name (e.g. 'Material', 'Texture2D').
        directory: Directory to search in.
        recursive: Include subdirectories.
    """
    result = _send_command("search_assets", {"class_name": class_name, "directory": directory, "recursive": recursive})
    return json.dumps(result, indent=2)


# -- Project tools -----------------------------------------------------------


@mcp.tool()
def get_project_info() -> str:
    """Get the UEFN project name and content root path.

    Use the returned content_root as the base path for asset operations
    (e.g. list_assets, search_assets, create assets via execute_python).
    In UEFN the content root is '/{ProjectName}/', NOT '/Game/'.
    """
    result = _send_command("get_project_info")
    return json.dumps(result, indent=2)


# -- Level tools -------------------------------------------------------------


@mcp.tool()
def save_current_level() -> str:
    """Save the current level."""
    result = _send_command("save_current_level")
    return json.dumps(result, indent=2)


@mcp.tool()
def get_level_info() -> str:
    """Get info about the current level (name, actor count)."""
    result = _send_command("get_level_info")
    return json.dumps(result, indent=2)


# -- Viewport tools ----------------------------------------------------------


@mcp.tool()
def get_viewport_camera() -> str:
    """Get the current viewport camera position and rotation."""
    result = _send_command("get_viewport_camera")
    return json.dumps(result, indent=2)


@mcp.tool()
def set_viewport_camera(
    location: Optional[list[float]] = None,
    rotation: Optional[list[float]] = None,
) -> str:
    """Move the viewport camera to a position.

    Args:
        location: [x, y, z] world coordinates.
        rotation: [pitch, yaw, roll] in degrees.
    """
    params: dict[str, Any] = {}
    if location is not None:
        params["location"] = location
    if rotation is not None:
        params["rotation"] = rotation
    result = _send_command("set_viewport_camera", params)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Allow --port override (skips auto-discovery, uses fixed port)
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--port" and i < len(sys.argv) - 1:
            _discovered_port = int(sys.argv[i + 1])

    mcp.run()
