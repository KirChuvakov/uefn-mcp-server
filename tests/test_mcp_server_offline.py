"""Offline smoke test for mcp_server.py: no UEFN, no listener, no network.

Starts a fresh mcp_server.py over stdio, runs the MCP handshake and
tools/list, and checks that every tool module registered, that rotation
parameters are published as named axes (pitch / yaw / roll) and that the
instructions carry the 0.5.0 safety rules. UEFN_MCP_PORT is set above the
listener's scan range (8765-8770), so the server's heartbeat never reaches a
running editor. Needs only the host Python with `pip install -r requirements.txt`.

Usage: python tests/test_mcp_server_offline.py   (pytest also collects it)
"""
import json
import os
import queue
import subprocess
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 107 in 0.4.0 + 11 desktop_* + 3 uefn_* session tools (0.5.0).
MIN_TOOLS = 121
DESKTOP_TOOLS = {
    "desktop_list_windows", "desktop_screenshot", "desktop_focus_window", "desktop_click", "desktop_move",
    "desktop_drag", "desktop_scroll", "desktop_type", "desktop_key", "desktop_close_window",
    "desktop_wait_for_window",
}
SESSION_TOOLS = {"uefn_status", "uefn_launch_project", "uefn_set_load_on_startup"}
# One tool per group (all of the new groups), so a module that fails to register is caught by name.
EXPECTED = {
    "ping", "execute_python", "get_all_actors", "list_assets", "get_viewport_camera",
    "material_create", "niagara_place_actor", "anim_get_info", "staticmesh_get_info",
    "asset_find_referencers", "datatable_info", "validate_asset", "screenshot_viewport",
    "device_set_editable", "playtest_start", "mesh_scatter", "verse_list_services",
    "verse_compile", "verse_status", "verse_push",
    "verse_symbols", "verse_hover", "verse_definition", "verse_find_symbol", "verse_lsp_restart",
} | DESKTOP_TOOLS | SESSION_TOOLS
# Tools whose rotation parameter must be published as named axes (0.5.0 rotation fix).
ROTATION_PARAMS = {"spawn_actor": "rotation", "set_actor_transform": "rotation", "set_viewport_camera": "rotation",
                   "niagara_place_actor": "rotation", "staticmesh_generate_uv": "orientation"}


def _run() -> dict:
    env = dict(os.environ, UEFN_MCP_PORT="18765", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "mcp_server.py")],
        cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
    )
    lines: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=lambda: [lines.put(l) for l in proc.stdout], daemon=True).start()

    def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv(req_id: int) -> dict:
        while True:
            msg = json.loads(lines.get(timeout=60))
            if msg.get("id") == req_id:
                return msg

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "offline-smoke", "version": "0"}}})
        init = recv(1)["result"]
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools, cursor, req_id = [], None, 2
        while True:
            send({"jsonrpc": "2.0", "id": req_id, "method": "tools/list",
                  "params": {"cursor": cursor} if cursor else {}})
            page = recv(req_id)["result"]
            tools += page["tools"]
            cursor, req_id = page.get("nextCursor"), req_id + 1
            if not cursor:
                break
    finally:
        proc.kill()
    return {"init": init, "tools": [t["name"] for t in tools], "schemas": {t["name"]: t["inputSchema"] for t in tools}}


def _check() -> dict:
    out = _run()
    names = set(out["tools"])
    assert out["init"]["serverInfo"]["name"] == "uefn-mcp"
    instructions = out["init"].get("instructions") or ""
    assert "v0.5.0" in instructions and "desktop_click" in instructions and "uefn_launch_project" in instructions
    assert '"pitch"' in instructions and "has_vertex_colors" in instructions and "Fortnite game client" in instructions
    assert len(out["tools"]) == len(names), "duplicate tool names"
    assert len(names) >= MIN_TOOLS, f"{len(names)} tools < {MIN_TOOLS}"
    missing = EXPECTED - names
    assert not missing, f"missing tools: {sorted(missing)}"
    for tool, param in ROTATION_PARAMS.items():
        schema = out["schemas"][tool]
        rotation = schema["$defs"]["Rotation"]
        assert set(rotation["properties"]) == {"pitch", "yaw", "roll"}, tool
        assert rotation.get("additionalProperties") is False, tool
        assert {"$ref": "#/$defs/Rotation"} in schema["properties"][param]["anyOf"], tool
    return out


def test_server_lists_all_tools():
    _check()


if __name__ == "__main__":
    result = _check()
    info = result["init"]["serverInfo"]
    names = set(result["tools"])
    print(f"OK: {info['name']} (mcp SDK {info.get('version')}), {len(result['tools'])} tools "
          f"({len(names & DESKTOP_TOOLS)} desktop_*, {len(names & SESSION_TOOLS)} uefn_* session), "
          f"{len(ROTATION_PARAMS)} rotation parameters with named axes")
