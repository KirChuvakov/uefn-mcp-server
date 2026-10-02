# Architecture

## Overview

The system consists of two independently running Python processes connected by HTTP on localhost.

```
┌──────────────┐     stdio      ┌──────────────────┐     HTTP POST      ┌──────────────────────────┐
│  Claude Code │ ◄────────────► │   MCP Server     │ ◄────────────────► │   UEFN Listener          │
│  (AI client) │                │                  │   127.0.0.1:8765   │                          │
│              │                │  mcp_server.py   │                    │  uefn_listener.py        │
└──────────────┘                └──────────────────┘                    └──────────────────────────┘
                                 Python 3.10+                            Python 3.11 (embedded)
                                 External process                        Inside UEFN editor
```

### Why two processes?

1. **Thread safety**: All `unreal.*` API calls must happen on the UEFN editor's main thread. A background HTTP server receives commands, but execution is deferred to the main thread via tick callbacks.
2. **Python version split**: The MCP SDK requires Python 3.10+ with async support. UEFN embeds its own Python interpreter (3.11.8) inside the editor process; extra packages can only be vendored next to the code (`vendor/`, used for the optional tray icon).
3. **Decoupling**: The MCP server can restart independently without affecting the running editor. The listener can restart without breaking the MCP server connection permanently.

## Component 1: UEFN Listener

**File:** `uefn_listener.py`

### Three-layer design

```
┌─────────────────────────────────────────────────────────────┐
│                    UEFN Editor Process                       │
│                                                             │
│  ┌──────────────────┐          ┌─────────────────────────┐  │
│  │   HTTP Server    │          │    Main Thread           │  │
│  │  (daemon thread) │  Queue   │    (tick callback)       │  │
│  │                  │ ───────► │                          │  │
│  │  Receives POST   │          │  Drains queue            │  │
│  │  Creates req_id  │ ◄─────── │  Dispatches command      │  │
│  │  Polls for result│ Response │  Calls unreal.* API      │  │
│  │  Returns JSON    │   Dict   │  Stores result           │  │
│  └──────────────────┘          └─────────────────────────┘  │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              Command Handlers (22)                    │   │
│  │  ping, execute_python, get_all_actors, spawn_actor,  │   │
│  │  list_assets, get_viewport_camera, ...               │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### Request lifecycle

1. HTTP POST arrives on the daemon thread with JSON body: `{"command": "...", "params": {...}}`
2. Handler generates a unique `req_id`, puts `(req_id, command, params)` into `queue.Queue()`
3. Handler enters a polling loop, checking `_responses[req_id]` every 20ms
4. On the next editor tick, `_tick_handler()` drains the queue (up to 5 commands per tick)
5. Each command is dispatched to its registered handler on the **main thread**
6. Result is stored in `_responses[req_id]` under a threading lock
7. The HTTP handler detects the result, removes it from the dict, and returns the JSON response
8. Stale responses (>60s old) are cleaned up automatically

### Configuration constants

| Constant | Default | Description |
|----------|---------|-------------|
| `DEFAULT_PORT` | 8765 | First port to try |
| `MAX_PORT` | 8770 | Last port in auto-detect range |
| `TICK_BATCH_LIMIT` | 5 | Max commands processed per editor tick |
| `HTTP_TIMEOUT_SEC` | 30.0 | HTTP request timeout before 504 |
| `POLL_INTERVAL_SEC` | 0.02 | Response poll interval (50 Hz) |
| `STALE_CLEANUP_SEC` | 60.0 | Age after which orphan responses are deleted |
| `LOG_RING_SIZE` | 200 | Max entries in the in-memory log buffer |

### Serialization

Unreal objects are not JSON-serializable. The `_serialize()` function converts them:

| UE Type | JSON Output |
|---------|-------------|
| `unreal.Vector` | `{"x": 0.0, "y": 0.0, "z": 0.0}` |
| `unreal.Rotator` | `{"pitch": 0.0, "yaw": 0.0, "roll": 0.0}` |
| `unreal.LinearColor` | `{"r": 1.0, "g": 0.0, "b": 0.0, "a": 1.0}` |
| `unreal.Transform` | `{"location": ..., "rotation": ..., "scale": ...}` |
| `unreal.AssetData` | `{"asset_name": ..., "asset_class": ..., "package_name": ..., ...}` |
| `unreal.Actor` | Full path name string |
| `unreal.Object` | Path name string |
| Enums | String representation |

### Command registration

Handlers are registered with the `@_register("command_name")` decorator:

```python
@_register("my_command")
def _cmd_my_command(param1: str, param2: int = 0) -> dict:
    # This runs on the main thread — unreal.* calls are safe
    return {"result": "value"}
```

## Component 2: MCP Server

**File:** `mcp_server.py`

### Structure

```
┌────────────────────────────────────────────┐
│            MCP Server Process              │
│                                            │
│  ┌──────────────────────────────────────┐  │
│  │          FastMCP Framework           │  │
│  │                                      │  │
│  │  @mcp.tool() decorated functions     │  │
│  │  99 tools matching listener commands │  │
│  └───────────────┬──────────────────────┘  │
│                  │                          │
│  ┌───────────────▼──────────────────────┐  │
│  │       _send_command() helper         │  │
│  │                                      │  │
│  │  Serializes params to JSON           │  │
│  │  POSTs to http://127.0.0.1:8765     │  │
│  │  Parses response                     │  │
│  │  Raises on error / timeout           │  │
│  └──────────────────────────────────────┘  │
└────────────────────────────────────────────┘
```

Each MCP tool is a thin wrapper:
1. Accepts typed parameters from Claude Code
2. Calls `_send_command("command_name", params)`
3. Formats the result as a human-readable string
4. Returns to Claude Code

### Error handling

| Scenario | Behavior |
|----------|----------|
| Listener not running | `ConnectionError` with instructions to start it |
| Listener older than the command needs | `RuntimeError`, nothing sent: rotations, `focus_selected` and `staticmesh_*` need protocol 0.3.3+ (the version comes from `GET /`) |
| Command fails in UEFN | `RuntimeError` with error message and Python traceback |
| Command times out | `TimeoutError` after 30 seconds |
| Invalid JSON response | Exception propagated to Claude Code |

## Protocol

### HTTP Endpoints

**GET /** — Health check and tool manifest
```json
{
  "status": "ok",
  "version": "0.3.3",
  "port": 8765,
  "commands": ["ping", "get_log", "execute_python", ...]
}
```

`version` is the listener protocol (`PROTOCOL_VERSION`); the MCP server reads it during port discovery.

**POST /** — Execute a command
```json
// Request
{
  "command": "get_all_actors",
  "params": {"class_filter": "StaticMeshActor"}
}

// Response (success)
{
  "success": true,
  "result": {
    "actors": [...],
    "count": 42
  }
}

// Response (error)
{
  "success": false,
  "error": "Actor not found: MyActor",
  "traceback": "Traceback (most recent call last):\n..."
}
```

## Component 3: Verse channels (0.4.0)

Eight tools bypass the listener and work whenever UEFN has the project open, even if Python is off:

- **`verse_workflow.py`** (`verse_compile`, `verse_status`, `verse_push`) connects to the VerseWorkflowServer that the
  editor exposes on TCP `127.0.0.1:1962` — the channel the `epicgames.verse` VS Code extension uses for "Build Verse
  Code" and "Push Verse Changes". Messages use LSP-style `Content-Length` framing: requests
  `{"seq","type":1,"command","params"}`, responses `type:2`, notifications `type:0` (`logMessage` with severity
  1-4, `updateBuildState` 0-4, `canPushVerseChanges`). `verse_status` only listens to the state the server pushes on
  connect, so it never starts a build.
- **`verse_lsp_service.py`** (`verse_symbols`, `verse_hover`, `verse_definition`, `verse_find_symbol`,
  `verse_lsp_restart`) keeps one `verse-lsp.exe` alive over stdio, initialized with the multi-root `.code-workspace`
  UEFN generates (project Content plus the Verse / Fortnite / UnrealEngine digests). Navigation only: compile errors
  come from `verse_compile`.

## Component 4: UEFN session (0.5.0)

Two more modules run inside the MCP server process and need neither the listener nor any extra package (Windows only,
`ctypes`). Neither sends mouse or keyboard input.

- **`uefn_session.py`** (3 `uefn_*` tools). Reads the editor log incrementally (markers such as `Opening project` /
  `Successfully opened project` / `[MCP] Auto-started`), UEFN's `EditorPerProjectUserSettings.ini` (Load on Startup,
  last project, per-project Python switch), the Epic launcher manifests (editor exe, launch URI) and probes the ports,
  then classifies the session (`not_running`, `starting`, `hub`, `opening`, `project_open`, `other_project_open`,
  `open_failed`, `crash_dialog`). `uefn_launch_project` starts UEFN through the launcher URI or the exe and waits on
  these signals; when UEFN stops on the HUB it returns `hub` and the user opens the project. With "Load on Startup" =
  Most Recent Project it first points `LastProjectFileName` at the requested project, so a relaunch after a crash
  opens it without the HUB. Also a CLI for `setup.ps1` (`status`, `find-install`, `set-load-on-startup`).
- **`win_procs.py`**: read-only process and window queries (`CreateToolhelp32Snapshot`, `EnumWindows`, hung-window
  checks) that tell whether the editor runs, whether its windows respond and whether the crash reporter shows. Its
  only acting call terminates an orphaned crash reporter, used by `uefn_launch_project(close_crash_reporter=True)` when
  no editor process runs.

## Experimental tag (0.5.0)

`tool_tags.py` defines the `[experimental]` tag for tools that are hard to undo or act outside the open level
(`execute_python`, `delete_asset`, `asset_batch_rename`, `shutdown`, `verse_push`, `uefn_launch_project`,
`uefn_set_load_on_startup`). `experimental(title)` returns the keyword arguments for `@mcp.tool(...)`: a title that
starts with `[experimental]` and `ToolAnnotations(destructiveHint=True)`; each tagged tool's docstring starts with
"[experimental] Ask the user to confirm before each call." The server instructions list the same tools, and
`tests/test_mcp_server_offline.py` checks that exactly these seven carry the tag.

## Autostart (0.4.0)

UEFN runs `init_unreal.py` only from a whitelist of engine plugin folders. `ensure_mcp_hook.ps1` appends a marked
block to Epic's `EditorToolset/Content/Python/init_unreal.py` that runs this repo's `init_unreal.py` with `runpy`; that
file puts the repo on `sys.path` and imports `uefn_listener`, whose bootstrap block starts the listener. Fortnite
updates overwrite the Epic file, so the script is idempotent and meant to run again after updates (hourly task).

## Adding New Commands

### 1. Add handler in `uefn_listener.py`

```python
@_register("my_new_command")
def _cmd_my_new_command(param1: str, param2: int = 0) -> dict:
    """Runs on the main thread inside the editor."""
    # Safe to call unreal.* here
    result = unreal.EditorAssetLibrary.does_asset_exist(param1)
    return {"exists": result, "param2": param2}
```

### 2. Add tool in `mcp_server.py`

```python
@mcp.tool()
def my_new_command(param1: str, param2: int = 0) -> str:
    """Description that Claude reads to decide when to use this tool.

    Args:
        param1: What this parameter does.
        param2: Optional parameter with default.
    """
    result = _send_command("my_new_command", {"param1": param1, "param2": param2})
    return json.dumps(result, indent=2)
```

### 3. Restart both

- Restart the listener in UEFN: `py -c "import uefn_listener; uefn_listener.restart_listener()"`
- Restart Claude Code to pick up the new tool

## Design Decisions

### Why HTTP and not TCP sockets / named pipes?

- `http.server` is stdlib — no dependencies needed inside UEFN
- JSON over HTTP is easy to debug (curl, browser)
- Proven by the feasibility test to work inside UEFN's sandbox

### Why not run MCP directly inside UEFN?

- The `mcp` SDK uses `asyncio`, `pydantic`, and other dependencies that cannot be pip-installed into UEFN's embedded Python
- Separating the MCP protocol layer from the editor layer allows each to restart independently

### Why tick callback instead of async execution?

- Unreal Engine's Python API is not thread-safe
- All `unreal.*` calls must happen on the game/editor main thread
- `register_slate_post_tick_callback` is the official UE mechanism for deferring work to the main thread
- The callback fires every editor frame (typically 30-120 fps), giving sub-frame latency for command execution
