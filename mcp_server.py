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
import shutil
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


# -- Material tools ----------------------------------------------------------


@mcp.tool()
def material_create(
    asset_path: str,
    domain: str = "surface",
    blend_mode: str = "opaque",
    two_sided: bool = False,
) -> str:
    """Create a new Material asset.

    Args:
        asset_path: Full asset path (e.g. '/PlusOneDigPerStep/Materials/M_Test').
        domain: surface | deferred_decal | light_function | volume | post_process | user_interface | virtual_texture.
        blend_mode: opaque | masked | translucent | additive | modulate | alphacomposite | alphaholdout.
        two_sided: Render both sides.
    """
    result = _send_command("material_create", {
        "asset_path": asset_path,
        "domain": domain,
        "blend_mode": blend_mode,
        "two_sided": two_sided,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def material_create_instance(parent_path: str, asset_path: str) -> str:
    """Create a MaterialInstanceConstant from a parent Material.

    Args:
        parent_path: Full path to the parent Material or Material Instance.
        asset_path: Full path for the new instance asset.
    """
    result = _send_command("material_create_instance", {
        "parent_path": parent_path,
        "asset_path": asset_path,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def material_add_expression(
    material_path: str,
    expression_class: str,
    x: int = 0,
    y: int = 0,
) -> str:
    """Add an expression node to a material. Returns the node_name to use in later calls.

    Args:
        material_path: Full path to the Material asset.
        expression_class: Class name (e.g. 'TextureSample', 'Constant3Vector', 'ScalarParameter',
            'VectorParameter', 'TextureSampleParameter2D', 'Multiply', 'Add', 'Lerp').
            'MaterialExpression' prefix optional.
        x: Graph X position.
        y: Graph Y position.
    """
    result = _send_command("material_add_expression", {
        "material_path": material_path,
        "expression_class": expression_class,
        "x": x,
        "y": y,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def material_set_expression_property(
    material_path: str,
    node_name: str,
    property_name: str,
    value: Any,
) -> str:
    """Set an editor property on a material expression node.

    Auto-detects value type:
    - [r, g, b] or [r, g, b, a] list -> LinearColor.
    - String starting with '/' pointing to an existing asset -> loads and assigns the asset.
    - Otherwise -> passed through (int, float, bool, string).

    Common uses:
    - Set 'texture' on a TextureSample.
    - Set 'parameter_name' on ScalarParameter/VectorParameter.
    - Set 'r'/'constant'/'default_value' literal values.

    Args:
        material_path: Full path to the Material asset.
        node_name: Node name returned by material_add_expression.
        property_name: Editor property name (snake_case).
        value: Value to assign.
    """
    result = _send_command("material_set_expression_property", {
        "material_path": material_path,
        "node_name": node_name,
        "property_name": property_name,
        "value": value,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def material_connect_expressions(
    material_path: str,
    from_node: str,
    from_output: str,
    to_node: str,
    to_input: str,
) -> str:
    """Connect an output pin of one expression to an input pin of another. Recompiles material.

    Args:
        material_path: Full path to the Material asset.
        from_node: Source node name.
        from_output: Output pin name (e.g. 'RGB', 'R', '' for default output).
        to_node: Destination node name.
        to_input: Input pin name (e.g. 'A', 'B', 'X', 'Y' depending on node type).
    """
    result = _send_command("material_connect_expressions", {
        "material_path": material_path,
        "from_node": from_node,
        "from_output": from_output,
        "to_node": to_node,
        "to_input": to_input,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def material_connect_property(
    material_path: str,
    from_node: str,
    from_output: str,
    material_property: str,
) -> str:
    """Connect an expression output to a material attribute (BaseColor, Metallic, etc). Recompiles material.

    Args:
        material_path: Full path to the Material asset.
        from_node: Source node name.
        from_output: Output pin name (e.g. 'RGB', '' for default).
        material_property: One of base_color | metallic | specular | roughness | anisotropy |
            emissive_color | opacity | opacity_mask | normal | tangent | world_position_offset |
            subsurface_color | ambient_occlusion | refraction | pixel_depth_offset.
    """
    result = _send_command("material_connect_property", {
        "material_path": material_path,
        "from_node": from_node,
        "from_output": from_output,
        "material_property": material_property,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def material_list_expressions(material_path: str) -> str:
    """List all expression nodes in a material with names and classes."""
    result = _send_command("material_list_expressions", {"material_path": material_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def material_recompile(material_path: str) -> str:
    """Force recompile of a material and save it."""
    result = _send_command("material_recompile", {"material_path": material_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def material_set_scalar_param(instance_path: str, param_name: str, value: float) -> str:
    """Set a scalar parameter value on a MaterialInstanceConstant."""
    result = _send_command("material_set_scalar_param", {
        "instance_path": instance_path,
        "param_name": param_name,
        "value": value,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def material_set_vector_param(
    instance_path: str,
    param_name: str,
    r: float,
    g: float,
    b: float,
    a: float = 1.0,
) -> str:
    """Set a vector (LinearColor) parameter value on a MaterialInstanceConstant."""
    result = _send_command("material_set_vector_param", {
        "instance_path": instance_path,
        "param_name": param_name,
        "r": r,
        "g": g,
        "b": b,
        "a": a,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def material_set_texture_param(instance_path: str, param_name: str, texture_path: str) -> str:
    """Set a texture parameter value on a MaterialInstanceConstant."""
    result = _send_command("material_set_texture_param", {
        "instance_path": instance_path,
        "param_name": param_name,
        "texture_path": texture_path,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def material_set_static_switch_param(instance_path: str, param_name: str, value: bool) -> str:
    """Set a static switch parameter on a MaterialInstanceConstant (triggers recompile)."""
    result = _send_command("material_set_static_switch_param", {
        "instance_path": instance_path,
        "param_name": param_name,
        "value": value,
    })
    return json.dumps(result, indent=2)


# -- Niagara tools -----------------------------------------------------------
#
# UEFN Niagara is runtime-controllable only: spawn systems, set parameters,
# activate/deactivate. Graph editing / creating NiagaraSystem assets from
# scratch is NOT supported in UEFN (NiagaraSystemFactory is stripped).
# Create systems manually in the UEFN editor, then drive them via these tools.


@mcp.tool()
def niagara_place_actor(
    system_path: str,
    location: Optional[list[float]] = None,
    rotation: Optional[list[float]] = None,
    label: str = "",
) -> str:
    """Spawn a NiagaraActor in the current level with the given NiagaraSystem asset.

    Args:
        system_path: Full path to an existing NiagaraSystem asset.
        location: [x, y, z] world coordinates. Defaults to origin.
        rotation: [pitch, yaw, roll] in degrees. Defaults to zero.
        label: Optional actor label.
    """
    params: dict[str, Any] = {"system_path": system_path}
    if location is not None:
        params["location"] = location
    if rotation is not None:
        params["rotation"] = rotation
    if label:
        params["label"] = label
    result = _send_command("niagara_place_actor", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def niagara_set_system_asset(actor_path: str, system_path: str) -> str:
    """Swap the NiagaraSystem asset on an actor's NiagaraComponent."""
    result = _send_command("niagara_set_system_asset", {
        "actor_path": actor_path,
        "system_path": system_path,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def niagara_activate(actor_path: str, reset: bool = False) -> str:
    """Activate the NiagaraComponent on an actor. If reset=True, restarts the simulation."""
    result = _send_command("niagara_activate", {"actor_path": actor_path, "reset": reset})
    return json.dumps(result, indent=2)


@mcp.tool()
def niagara_deactivate(actor_path: str) -> str:
    """Deactivate the NiagaraComponent on an actor."""
    result = _send_command("niagara_deactivate", {"actor_path": actor_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def niagara_reset(actor_path: str) -> str:
    """Reset the particle simulation on an actor's NiagaraComponent."""
    result = _send_command("niagara_reset", {"actor_path": actor_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def niagara_set_float_param(actor_path: str, param_name: str, value: float) -> str:
    """Set a float user parameter on a NiagaraComponent.

    Args:
        actor_path: Actor path or label.
        param_name: Variable name (e.g. 'User.SpawnRate' or 'SpawnRate' depending on exposure).
        value: Float value.
    """
    result = _send_command("niagara_set_float_param", {
        "actor_path": actor_path,
        "param_name": param_name,
        "value": value,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def niagara_set_int_param(actor_path: str, param_name: str, value: int) -> str:
    """Set an int user parameter on a NiagaraComponent."""
    result = _send_command("niagara_set_int_param", {
        "actor_path": actor_path,
        "param_name": param_name,
        "value": value,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def niagara_set_bool_param(actor_path: str, param_name: str, value: bool) -> str:
    """Set a bool user parameter on a NiagaraComponent."""
    result = _send_command("niagara_set_bool_param", {
        "actor_path": actor_path,
        "param_name": param_name,
        "value": value,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def niagara_set_vec3_param(actor_path: str, param_name: str, x: float, y: float, z: float) -> str:
    """Set a vec3 user parameter on a NiagaraComponent (position or direction)."""
    result = _send_command("niagara_set_vec3_param", {
        "actor_path": actor_path,
        "param_name": param_name,
        "x": x, "y": y, "z": z,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def niagara_set_color_param(
    actor_path: str,
    param_name: str,
    r: float,
    g: float,
    b: float,
    a: float = 1.0,
) -> str:
    """Set a LinearColor user parameter on a NiagaraComponent."""
    result = _send_command("niagara_set_color_param", {
        "actor_path": actor_path,
        "param_name": param_name,
        "r": r, "g": g, "b": b, "a": a,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def niagara_set_texture_param(actor_path: str, param_name: str, texture_path: str) -> str:
    """Set a Texture user parameter on a NiagaraComponent."""
    result = _send_command("niagara_set_texture_param", {
        "actor_path": actor_path,
        "param_name": param_name,
        "texture_path": texture_path,
    })
    return json.dumps(result, indent=2)


# -- Animation tools ---------------------------------------------------------
#
# UEFN animation Python: notifies, curves, montages, blend spaces. Creating
# AnimSequence assets from scratch is NOT supported (AnimSequenceFactoryNew is
# stripped) — import via FBX first, then drive via these tools.


@mcp.tool()
def anim_get_info(anim_path: str) -> str:
    """Get basic info about an AnimSequence or AnimMontage (length, class, skeleton)."""
    result = _send_command("anim_get_info", {"anim_path": anim_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def anim_list_notify_tracks(anim_path: str) -> str:
    """List notify track names on an AnimSequence/Montage."""
    result = _send_command("anim_list_notify_tracks", {"anim_path": anim_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def anim_add_notify_track(
    anim_path: str,
    track_name: str,
    color: Optional[list[float]] = None,
) -> str:
    """Add a new notify track to an AnimSequence/Montage.

    Args:
        anim_path: Full path to the animation asset.
        track_name: New track name.
        color: Optional [r, g, b, a] track color (0..1). Defaults to white.
    """
    params: dict[str, Any] = {"anim_path": anim_path, "track_name": track_name}
    if color is not None:
        params["color"] = color
    result = _send_command("anim_add_notify_track", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def anim_remove_all_notify_tracks(anim_path: str) -> str:
    """Remove all notify tracks (and their events) from an animation."""
    result = _send_command("anim_remove_all_notify_tracks", {"anim_path": anim_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def anim_list_notifies(anim_path: str) -> str:
    """List all notify events on an animation with name/time/duration/track/class."""
    result = _send_command("anim_list_notifies", {"anim_path": anim_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def anim_add_notify(
    anim_path: str,
    track_name: str,
    time: float,
    notify_class: str,
) -> str:
    """Add a zero-duration typed notify event to a track.

    Args:
        anim_path: Full path to the animation asset.
        track_name: Existing notify track name (create first with anim_add_notify_track).
        time: Trigger time in seconds.
        notify_class: AnimNotify subclass name (e.g. 'AnimNotify_PlaySound',
            'AnimNotify_PlayParticleEffect').
    """
    result = _send_command("anim_add_notify", {
        "anim_path": anim_path,
        "track_name": track_name,
        "time": time,
        "notify_class": notify_class,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def anim_add_notify_state(
    anim_path: str,
    track_name: str,
    time: float,
    duration: float,
    notify_state_class: str,
) -> str:
    """Add a duration-based notify state event to a track.

    Args:
        anim_path: Full path to the animation asset.
        track_name: Existing notify track name.
        time: Start time in seconds.
        duration: Duration in seconds.
        notify_state_class: AnimNotifyState subclass name (e.g. 'AnimNotifyState_TimedParticleEffect').
    """
    result = _send_command("anim_add_notify_state", {
        "anim_path": anim_path,
        "track_name": track_name,
        "time": time,
        "duration": duration,
        "notify_state_class": notify_state_class,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def anim_add_float_curve(anim_path: str, curve_name: str) -> str:
    """Create a new float curve on an animation."""
    result = _send_command("anim_add_float_curve", {
        "anim_path": anim_path,
        "curve_name": curve_name,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def anim_add_float_curve_key(
    anim_path: str,
    curve_name: str,
    time: float,
    value: float,
) -> str:
    """Add a keyframe to a float curve on an animation."""
    result = _send_command("anim_add_float_curve_key", {
        "anim_path": anim_path,
        "curve_name": curve_name,
        "time": time,
        "value": value,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def anim_create_montage(source_animation_path: str, asset_path: str) -> str:
    """Create an AnimMontage asset from a source AnimSequence.

    Args:
        source_animation_path: Full path to an existing AnimSequence.
        asset_path: Full path for the new montage asset.
    """
    result = _send_command("anim_create_montage", {
        "source_animation_path": source_animation_path,
        "asset_path": asset_path,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def anim_create_blendspace(
    skeleton_path: str,
    asset_path: str,
    blendspace_type: str = "2D",
) -> str:
    """Create a BlendSpace (2D) or BlendSpace1D asset.

    Args:
        skeleton_path: Full path to the target Skeleton asset.
        asset_path: Full path for the new blendspace asset.
        blendspace_type: '1D' or '2D' (default '2D').
    """
    result = _send_command("anim_create_blendspace", {
        "skeleton_path": skeleton_path,
        "asset_path": asset_path,
        "blendspace_type": blendspace_type,
    })
    return json.dumps(result, indent=2)


# -- Static Mesh tools ------------------------------------------------------


@mcp.tool()
def staticmesh_get_info(asset_path: str) -> str:
    """Get static mesh diagnostics: verts, UVs, LOD count, collisions, Nanite state."""
    result = _send_command("staticmesh_get_info", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def staticmesh_enable_nanite(
    asset_path: str,
    enabled: bool = True,
    fallback_percent_triangles: float = 1.0,
) -> str:
    """Enable/disable Nanite on a static mesh. fallback_percent_triangles controls legacy fallback LOD."""
    result = _send_command("staticmesh_enable_nanite", {
        "asset_path": asset_path,
        "enabled": enabled,
        "fallback_percent_triangles": fallback_percent_triangles,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def staticmesh_set_lods(
    asset_path: str,
    percent_triangles: list[float],
    screen_sizes: Optional[list[float]] = None,
    auto_compute_screen_size: bool = True,
) -> str:
    """Set LODs by triangle reduction. First element = LOD0 (usually 1.0 = full).

    Example: percent_triangles=[1.0, 0.5, 0.25, 0.125] → 4 LODs.
    """
    params: dict[str, Any] = {
        "asset_path": asset_path,
        "percent_triangles": percent_triangles,
        "auto_compute_screen_size": auto_compute_screen_size,
    }
    if screen_sizes is not None:
        params["screen_sizes"] = screen_sizes
    result = _send_command("staticmesh_set_lods", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def staticmesh_remove_lods(asset_path: str) -> str:
    """Remove all auto-generated LODs."""
    result = _send_command("staticmesh_remove_lods", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def staticmesh_add_collision(asset_path: str, shape: str = "box") -> str:
    """Add a simple collision primitive.

    Args:
        shape: box | sphere | capsule | ndop10_x | ndop10_y | ndop10_z | ndop18 | ndop26.
    """
    result = _send_command("staticmesh_add_collision", {"asset_path": asset_path, "shape": shape})
    return json.dumps(result, indent=2)


@mcp.tool()
def staticmesh_remove_collisions(asset_path: str) -> str:
    """Remove all simple collisions from a static mesh."""
    result = _send_command("staticmesh_remove_collisions", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def staticmesh_generate_uv(
    asset_path: str,
    uv_type: str = "planar",
    lod_index: int = 0,
    uv_channel_index: int = 1,
    position: Optional[list[float]] = None,
    orientation: Optional[list[float]] = None,
    tiling: Optional[list[float]] = None,
) -> str:
    """Generate a UV channel on a static mesh (planar / box / cylindrical projection)."""
    params: dict[str, Any] = {
        "asset_path": asset_path,
        "uv_type": uv_type,
        "lod_index": lod_index,
        "uv_channel_index": uv_channel_index,
    }
    if position is not None:
        params["position"] = position
    if orientation is not None:
        params["orientation"] = orientation
    if tiling is not None:
        params["tiling"] = tiling
    result = _send_command("staticmesh_generate_uv", params)
    return json.dumps(result, indent=2)


# -- Asset Pipeline tools ---------------------------------------------------


@mcp.tool()
def asset_batch_rename(renames: list[dict]) -> str:
    """Rename (and/or move) multiple assets in one transaction.

    Args:
        renames: List of {"old_path": "...", "new_path": "/Package/Path/NewName"}.
    """
    result = _send_command("asset_batch_rename", {"renames": renames})
    return json.dumps(result, indent=2)


@mcp.tool()
def asset_set_metadata(asset_path: str, tag: str, value: str) -> str:
    """Set a metadata tag on an asset (e.g. 'Author', 'Category', 'Rarity')."""
    result = _send_command("asset_set_metadata", {
        "asset_path": asset_path, "tag": tag, "value": value,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def asset_get_metadata(asset_path: str) -> str:
    """Read all metadata tags on an asset."""
    result = _send_command("asset_get_metadata", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def asset_remove_metadata(asset_path: str, tag: str) -> str:
    """Remove a metadata tag from an asset."""
    result = _send_command("asset_remove_metadata", {"asset_path": asset_path, "tag": tag})
    return json.dumps(result, indent=2)


@mcp.tool()
def asset_find_referencers(asset_path: str) -> str:
    """List packages that reference this asset (i.e. what uses it)."""
    result = _send_command("asset_find_referencers", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def asset_find_dependencies(asset_path: str) -> str:
    """List packages this asset depends on (i.e. what it uses)."""
    result = _send_command("asset_find_dependencies", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def asset_find_unused(directory: str = "/Game/", class_filter: str = "") -> str:
    """Find assets with no referencers in a directory.

    Args:
        directory: Content path to scan.
        class_filter: Optional class name (e.g. 'Material', 'Texture2D', 'StaticMesh').
    """
    result = _send_command("asset_find_unused", {
        "directory": directory, "class_filter": class_filter,
    })
    return json.dumps(result, indent=2)


# -- DataTable tools --------------------------------------------------------


@mcp.tool()
def datatable_info(asset_path: str) -> str:
    """Get DataTable structure, row names, and column names."""
    result = _send_command("datatable_info", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def datatable_export_json(asset_path: str) -> str:
    """Export the whole DataTable as a JSON string."""
    result = _send_command("datatable_export_json", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def datatable_export_csv(asset_path: str) -> str:
    """Export the whole DataTable as a CSV string."""
    result = _send_command("datatable_export_csv", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def datatable_import_json(asset_path: str, json_string: str) -> str:
    """Replace all rows in a DataTable from a JSON string. Saves the asset."""
    result = _send_command("datatable_import_json", {
        "asset_path": asset_path, "json_string": json_string,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def datatable_import_csv(asset_path: str, csv_string: str) -> str:
    """Replace all rows in a DataTable from a CSV string. Saves the asset."""
    result = _send_command("datatable_import_csv", {
        "asset_path": asset_path, "csv_string": csv_string,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def datatable_get_row(asset_path: str, row_name: str) -> str:
    """Get a single row of a DataTable (parsed from JSON export)."""
    result = _send_command("datatable_get_row", {
        "asset_path": asset_path, "row_name": row_name,
    })
    return json.dumps(result, indent=2)


# -- Validation tools -------------------------------------------------------


@mcp.tool()
def validate_asset(asset_path: str) -> str:
    """Run all registered editor validators on one asset. Returns valid/invalid counts."""
    result = _send_command("validate_asset", {"asset_path": asset_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def validate_folder(directory: str = "/Game/", recursive: bool = True) -> str:
    """Validate all assets under a directory. Good for CI-style content audits."""
    result = _send_command("validate_folder", {"directory": directory, "recursive": recursive})
    return json.dumps(result, indent=2)


@mcp.tool()
def validate_selected() -> str:
    """Validate assets currently selected in the UEFN Content Browser."""
    result = _send_command("validate_selected")
    return json.dumps(result, indent=2)


# -- Screenshot tools -------------------------------------------------------


@mcp.tool()
def screenshot_viewport(
    output_path: str,
    width: int = 1280,
    height: int = 720,
    force_game_view: bool = False,
    timeout_sec: float = 20.0,
) -> str:
    """Capture the UEFN editor viewport as a PNG file.

    Fires a high-res screenshot via UEFN and polls for the file to appear.
    The returned path is readable via the Read tool so Claude can view it.

    Args:
        output_path: Absolute path to write the PNG. Parent dirs are created.
        width: Screenshot width in pixels (default 1280).
        height: Screenshot height in pixels (default 720).
        force_game_view: If True, hides editor gizmos/overlays. Default False
            (captures what you see in the editor, including helpers).
        timeout_sec: Max time to wait for the render thread to write the file.
    """
    output_path = os.path.abspath(output_path)
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    start = _send_command("screenshot_start", {
        "width": int(width),
        "height": int(height),
        "force_game_view": bool(force_game_view),
    })
    expected = start["expected_path"]

    deadline = time.time() + float(timeout_sec)
    while time.time() < deadline:
        if os.path.isfile(expected) and os.path.getsize(expected) > 1024:
            # Let the render thread finish the last flush before we move
            time.sleep(0.25)
            break
        time.sleep(0.2)
    else:
        raise TimeoutError(f"Viewport screenshot not written within {timeout_sec}s at {expected}")

    shutil.move(expected, output_path)
    return json.dumps({
        "path": output_path,
        "width": int(width),
        "height": int(height),
        "size_bytes": os.path.getsize(output_path),
    }, indent=2)


@mcp.tool()
def screenshot_desktop(output_path: str, monitor: int = 1) -> str:
    """Capture the whole desktop (or a specific monitor) as a PNG file.

    Uses the `mss` library. Does NOT go through UEFN — useful to see the
    entire editor window, status window, other applications, etc.

    Args:
        output_path: Absolute path to write the PNG.
        monitor: Monitor index. 0 = all monitors merged. 1 = primary, 2 = secondary, etc.
    """
    try:
        import mss
        from mss.tools import to_png
    except ImportError as e:
        raise RuntimeError("mss library is not installed. Run: pip install mss") from e

    output_path = os.path.abspath(output_path)
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with mss.mss() as sct:
        mons = sct.monitors
        if monitor < 0 or monitor >= len(mons):
            raise ValueError(f"Invalid monitor index {monitor}. Available: 0 (all) .. {len(mons)-1}")
        shot = sct.grab(mons[monitor])
        to_png(shot.rgb, shot.size, output=output_path)

    return json.dumps({
        "path": output_path,
        "width": shot.size[0],
        "height": shot.size[1],
        "monitor": monitor,
        "size_bytes": os.path.getsize(output_path),
    }, indent=2)


# -- Device tools (Verse @editable) -----------------------------------------


@mcp.tool()
def device_list_editables(actor_path: str) -> str:
    """List all Verse @editable fields on a creative_device actor in the level.

    Use this to discover which fields you can set before calling
    device_set_editable. Returns {actor, editables: [{name, current_value, value_type}]}.

    Args:
        actor_path: Actor label, name, or full path.
    """
    result = _send_command("device_list_editables", {"actor_path": actor_path})
    return json.dumps(result, indent=2)


@mcp.tool()
def device_set_editable(
    actor_path: str,
    field: str,
    value: Any,
    value_type: str = "auto",
) -> str:
    """Set a Verse @editable field on a creative_device actor.

    Equivalent to editing the Details panel in UEFN. Handy for wiring up
    world_accessor_device references and any @editable config value from code.

    Args:
        actor_path: Actor label, name, or full path.
        field: Verse @editable field name (e.g. "SomeButton", "MyConfig").
        value: New value. For value_type="actor" pass the referenced actor's label.
            For array types pass a list of elements.
        value_type: "auto" (infer from current value), "int", "float", "bool",
            "string", "actor", "vector" ({x,y,z}), "rotator" ({pitch,yaw,roll}),
            or "array:INNER" where INNER is one of the scalar types
            (e.g. "array:actor" for []button_device, "array:int" for []int).
    """
    params: dict[str, Any] = {
        "actor_path": actor_path,
        "field": field,
        "value": value,
        "value_type": value_type,
    }
    result = _send_command("device_set_editable", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def device_set_editables_bulk(
    actor_path: str,
    fields: list[dict[str, Any]],
) -> str:
    """Set multiple Verse @editable fields on one actor in a single call.

    Avoids per-field round-trips when configuring a device with many
    references (e.g. world_accessor_device with 30+ slots). Per-field
    failures are reported individually — the call does not abort on first error.

    Args:
        actor_path: Actor label, name, or full path.
        fields: List of entries, each: {"name": str, "value": Any,
            "value_type": str?}. value_type defaults to "auto". See
            device_set_editable for supported value_type values.

    Returns:
        {actor, actor_path, ok_count, fail_count, results: [{field, ok,
        new_value?, error?}]}.
    """
    params: dict[str, Any] = {
        "actor_path": actor_path,
        "fields": fields,
    }
    result = _send_command("device_set_editables_bulk", params)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Playtest (Play-In-Editor)
# ---------------------------------------------------------------------------


@mcp.tool()
def playtest_start() -> str:
    """Start a Play-In-Editor session (equivalent to the UEFN Play button).

    No-op if PIE is already running.
    """
    result = _send_command("playtest_start")
    return json.dumps(result, indent=2)


@mcp.tool()
def playtest_stop() -> str:
    """End the current Play-In-Editor session.

    No-op if PIE is not running.
    """
    result = _send_command("playtest_stop")
    return json.dumps(result, indent=2)


@mcp.tool()
def playtest_status() -> str:
    """Report whether a Play-In-Editor session is currently running."""
    result = _send_command("playtest_status")
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Mesh scatter
# ---------------------------------------------------------------------------


@mcp.tool()
def mesh_scatter(
    static_mesh_path: str,
    min_xyz: list[float],
    max_xyz: list[float],
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
) -> str:
    """Scatter StaticMeshActor instances randomly inside an axis-aligned box.

    Deterministic when ``seed`` is non-zero. ``clearance_radius`` enforces a
    minimum XY distance between placed instances via brute-force rejection —
    practical up to a few hundred points.

    Args:
        static_mesh_path: Path to a StaticMesh asset.
        min_xyz: [x, y, z] min corner of the scatter volume.
        max_xyz: [x, y, z] max corner of the scatter volume.
        count: Desired number of instances.
        seed: RNG seed. 0 = non-deterministic.
        scale_min / scale_max: Uniform scale range.
        yaw_random: Randomize yaw in [0, 360).
        pitch_random: Also randomize pitch in [-15, 15] for organic tilt.
        clearance_radius: Min XY distance between placed instances (cm). 0 disables.
        folder_path: Outliner folder path for organizing spawned actors.
        max_attempts: Placement attempt cap. Defaults to max(count*10, 50).
        material_path: Optional material to apply to slot 0 of each spawned mesh.
        collision_profile: Optional collision profile name (e.g. 'NoCollision').
    """
    params: dict[str, Any] = {
        "static_mesh_path": static_mesh_path,
        "min_xyz": min_xyz,
        "max_xyz": max_xyz,
        "count": count,
        "seed": seed,
        "scale_min": scale_min,
        "scale_max": scale_max,
        "yaw_random": yaw_random,
        "pitch_random": pitch_random,
        "clearance_radius": clearance_radius,
        "folder_path": folder_path,
        "max_attempts": max_attempts,
        "material_path": material_path,
        "collision_profile": collision_profile,
    }
    result = _send_command("mesh_scatter", params, timeout=120.0)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Verse introspection
# ---------------------------------------------------------------------------


@mcp.tool()
def verse_list_services() -> str:
    """List all Verse classes implementing ``i_service`` across the project.

    Parses `.verse` files under the UEFN project directory (excluding
    Intermediate/Saved/Binaries). Returns each service's name, path,
    line, parent interfaces, and which lifecycle interfaces it implements
    (i_initializable, i_player_listener, i_character_listener).
    """
    result = _send_command("verse_list_services")
    return json.dumps(result, indent=2)


@mcp.tool()
def verse_list_editables(class_filter: str = "") -> str:
    """List ``@editable`` fields grouped by their enclosing Verse class.

    Args:
        class_filter: Case-insensitive substring match on class name.
            Empty = include every class with @editable fields.
    """
    result = _send_command("verse_list_editables", {"class_filter": class_filter})
    return json.dumps(result, indent=2)


@mcp.tool()
def verse_service_graph(installer_filename: str = "_service_installer.verse") -> str:
    """Parse the composition-root file and return the DI graph.

    Follows the project convention: ``Name := class_name:`` archetype
    blocks with indented ``Field := Source`` dependency wiring. For each
    declared service, returns its class, declaration line, and dependency
    list.

    Args:
        installer_filename: Basename to search for. First match wins.
    """
    result = _send_command("verse_service_graph", {"installer_filename": installer_filename})
    return json.dumps(result, indent=2)


@mcp.tool()
def verse_find_resource_usage(
    enum_name: str = "resource",
    enum_filename: str = "_resource_type.verse",
    max_sites_per_variant: int = 20,
) -> str:
    """Find usages of each variant of a Verse enum across all `.verse` files.

    Defaults target the project's `resource` enum (Money, Crystal, etc.).
    Useful for locating every read/write site of a given resource token.

    Args:
        enum_name: Enum type name.
        enum_filename: Basename of the file containing the enum declaration.
        max_sites_per_variant: Cap per-variant site list to keep response size
            manageable. The ``count`` field is always the true total.
    """
    result = _send_command("verse_find_resource_usage", {
        "enum_name": enum_name,
        "enum_filename": enum_filename,
        "max_sites_per_variant": max_sites_per_variant,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
def verse_check_editable_coverage(
    config_class: str = "world_accessor_device",
) -> str:
    """Source-side audit: find @editable fields of a config class that are
    never referenced anywhere else in the project.

    UEFN's ScriptDevice bindings block reading Verse @editable values from
    Python, so runtime cross-check against the live level isn't possible.
    Instead this scans `.verse` sources: for each @editable field it counts
    references across the project and flags fields with zero references as
    potentially unused. Useful for spotting forgotten config slots after a
    refactor.

    Args:
        config_class: Verse class name to audit.
    """
    result = _send_command("verse_check_editable_coverage", {
        "config_class": config_class,
    })
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Verse build (workflow socket) & Verse LSP navigation
# ---------------------------------------------------------------------------
# These talk to UEFN directly (TCP 1962 / verse-lsp.exe subprocess) — they do
# NOT go through the editor HTTP listener, so they work even without it.

import verse_workflow
import verse_lsp_service

verse_workflow.register(mcp)
verse_lsp_service.register(mcp)


# ---------------------------------------------------------------------------
# Desktop control (host side, Windows)
# ---------------------------------------------------------------------------
# These run in this process (pure ctypes): window listing, GDI screenshots and
# SendInput input with safety rails.
# Importing desktop_control makes this process per-monitor DPI aware (V2).

import desktop_control

desktop_control.register(mcp)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Allow --port override (skips auto-discovery, uses fixed port)
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--port" and i < len(sys.argv) - 1:
            _discovered_port = int(sys.argv[i + 1])

    mcp.run()
