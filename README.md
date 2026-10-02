# UEFN MCP Server

Control [UEFN](https://dev.epicgames.com/documentation/en-us/fortnite/unreal-editor-for-fortnite) (Unreal Editor for Fortnite) from [Claude Code](https://docs.anthropic.com/en/docs/claude-code) via the [Model Context Protocol](https://modelcontextprotocol.io/).

```
Claude Code  <--stdio-->  MCP Server (mcp_server.py)  <--HTTP 127.0.0.1:8765-->  Listener (uefn_listener.py, inside UEFN)
                                   |--TCP 127.0.0.1:1962-->  UEFN VerseWorkflowServer   (verse_compile / verse_status / verse_push)
                                   |--stdio------------->  verse-lsp.exe                (verse_symbols / verse_hover / ...)
                                   '--files / Win32 ---->  editor log, settings ini, processes   (uefn_status / uefn_launch_project / ...)
```

- **110 tools**: actors, assets, levels, viewport, materials, Niagara, animations, static meshes, data tables, asset validation, device `@editable` wiring, Play-In-Editor control, StaticMeshActor scatter, Verse source introspection, Verse compile/push, Verse code navigation, UEFN session status / launch, and arbitrary Python execution
- **Zero C++ compilation** — pure Python, works across UEFN versions
- **Main-thread safe** — all `unreal.*` calls dispatched via editor tick callback
- **Autostart** — the listener starts by itself when a project opens (see [Auto-start](#auto-start))
- **Crash recovery** — the UEFN session tools read the editor state and relaunch UEFN after a crash; see [UEFN session and crash recovery](#uefn-session-and-crash-recovery)
- **No input simulation** — the server never sends mouse or keyboard input; tools that are hard to undo are tagged [experimental](#experimental-tools)

## What's new in 0.5.0

- **UEFN session** (`uefn_session.py`, 3 tools): `uefn_status` (read-only state from the editor log, processes and
  ports), `uefn_launch_project` (start UEFN, open a project, wait for the listener; can stop a crash reporter left over
  from a crash), `uefn_set_load_on_startup`. They send no mouse or keyboard input: when UEFN stops on the HUB, the user
  opens the project.
- **[experimental] tag**: 7 tools that are hard to undo or act outside the open level ask the user to confirm before
  each call. See [Experimental tools](#experimental-tools).
- **`setup.ps1`**: one idempotent setup step for a new machine (`-DryRun`).
- **Safety fixes** (listener protocol 0.3.3): rotations are named axes `{"pitch", "yaw", "roll"}` (a documented
  `[pitch, yaw, roll]` list used to be applied as `[roll, pitch, yaw]`); the `staticmesh_*` tools stop calling the
  getters that crash UEFN 42.20. See [Conventions](docs/tools_reference.md#conventions-050).

Full list: [CHANGELOG.md](CHANGELOG.md).

## What's new in 0.4.0

A community fork was merged into this repo: 79 new tools (materials, Niagara, animation, static meshes, asset
management, data tables, validation, screenshots, device `@editable` fields, Play-In-Editor, scatter, Verse
introspection), a Verse module that compiles, pushes and navigates Verse **without the editor listener**, listener
autostart through `ensure_mcp_hook.ps1`, a system-tray icon, and a listener that survives project switches.
Full list: [CHANGELOG.md](CHANGELOG.md).

## Quick Start

### 0. One step: `setup.ps1`

```powershell
git clone https://github.com/kirchuvakov/uefn-mcp-server
powershell -NoProfile -ExecutionPolicy Bypass -File .\uefn-mcp-server\setup.ps1 -DryRun    # shows what it would do
powershell -NoProfile -ExecutionPolicy Bypass -File .\uefn-mcp-server\setup.ps1 -ScheduleHook -Project "<path>\<Island>.uefnproject"
```

The script is idempotent (run it again after moving the clone or after a Fortnite update): it finds a real Python
3.10+, installs `requirements.txt`, sets `UEFN_MCP_PATH`, installs the listener autostart hook (`-ScheduleHook` adds the
hourly task that re-adds it after Fortnite updates), reports the UEFN install, "Load on Startup" and whether Python is
enabled for your project, and prints what is left. Options: `-WithTray` (tray-icon packages into `vendor/`),
`-WithMss`, `-EnableLoadLastProject` (UEFN reopens the last project at startup instead of the HUB; recommended, see
below), `-RegisterClaude` (user-scope registration), `-Python <python.exe>`, `-SkipPip`, `-DryRun`.

> **Recommended: Load on Startup = Most Recent Project.** UEFN crashes often. To let an agent recover by itself, set
> **Editor Preferences > Loading & Saving > Load on Startup** to **Most Recent Project** (the HUB screen has the same
> selector). Then `uefn_launch_project` relaunches UEFN, the project opens without the HUB, and the listener autostarts
> through the [hook](#auto-start). Without it, UEFN stops on the HUB after every relaunch and you have to click the
> project. With UEFN closed you can also run `setup.ps1 -EnableLoadLastProject` or let the agent call
> `uefn_set_load_on_startup('LastProject')`; details in [UEFN session and crash recovery](#uefn-session-and-crash-recovery).

Steps 1-6 below are what the script automates, plus the two things only you can do: enable Python in each project
(step 3) and approve the server in Claude Code (step 5). Prefer a conversation? Ask Claude Code *"Help me set up UEFN
MCP server"*.

### 1. Clone and point `UEFN_MCP_PATH` at the clone

```powershell
git clone https://github.com/kirchuvakov/uefn-mcp-server
[Environment]::SetEnvironmentVariable('UEFN_MCP_PATH', (Resolve-Path .\uefn-mcp-server).Path, 'User')
```

Any folder works; the config snippets below find the server through `UEFN_MCP_PATH`, so no absolute path is
written into a project. Restart terminals and Claude Code after setting it.

### 2. Install the MCP SDK (host Python, not inside UEFN)

```bash
pip install -r requirements.txt
```

Use a real Python 3.10+ install. On Windows, a bare `python` can resolve to the Microsoft Store alias
(`%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe`), which Claude Code cannot start: turn off the `python.exe`
App execution aliases in Windows settings, or put the full path of a real `python.exe` into `command` below.

### 3. Enable Python in UEFN

1. Open your project in UEFN
2. Go to **Project > Project Settings**
3. Search for **Python** and check the box for **Python Editor Script Plugin**

UEFN keeps this per project and per user (`EnablePythonLocallyPerProject`); every project that should autostart the
listener needs it.

### 4. Start the listener inside UEFN

Autostart (recommended, one-time setup): see [Auto-start](#auto-start). Manual start at any time:
**Tools > Execute Python Script** > `uefn_listener.py` from the clone.

A **status window** will appear showing:
- **Listener status** — green when running, red when stopped
- **MCP Server status** — green when Claude Code is connected (heartbeat every 10s)
- **Port** — editable when listener is stopped
- **Metrics** — uptime, request count, errors, last command, avg response time
- **Controls** — Stop / Start / Restart buttons

Closing this window **does not stop the listener** — it just hides it. A **system-tray icon** (optional, see
[Tray icon](#tray-icon)) stays in the notification area while the editor session lives: left-click (or *Show window*)
re-opens the window; the menu also has *Hide window*, *Restart listener*, *Start/Stop listener* and
*Quit (stop & remove tray)*.

### 5. Configure Claude Code

`.mcp.json` in your project root (Claude Code expands `${VAR}` and `${VAR:-default}` in it):

```json
{
  "mcpServers": {
    "uefn": {
      "command": "python",
      "args": ["${UEFN_MCP_PATH}/mcp_server.py"]
    },
    "unreal-mcp": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

`unreal-mcp` is Epic's own MCP server, available while UEFN runs with Toolsets enabled; it complements this one
(asset/actor search, reading device properties, sessions). A project-scope `.mcp.json` server must be approved once
in Claude Code (`/mcp`); if it stays "pending approval", register this server at user scope instead:

```powershell
claude mcp add uefn -s user -- python "$env:UEFN_MCP_PATH\mcp_server.py"
```

### 6. Restart Claude Code

Claude Code picks up MCP servers on startup. Check with `claude mcp list` (expect `uefn ... Connected`) and ask
*"ping UEFN"*.

### Try it

Ask Claude Code:
- *"List all actors in the level"*
- *"Spawn a cube at position 100, 200, 300"*
- *"Compile the Verse code and show me the errors"*
- *"Move the viewport camera to look at the origin"*

## Tray icon

The tray icon needs `pystray` + `Pillow` built for UEFN's embedded Python (3.11), so they go into `./vendor`
(gitignored) instead of the host Python. Populate it once with UEFN's own `python.exe` (it ships with pip):

```powershell
$uefnPython = Join-Path $env:ProgramFiles 'Epic Games\Fortnite\Engine\Binaries\ThirdParty\Python3\Win64\python.exe'  # adjust if Fortnite lives elsewhere
& $uefnPython -m pip install --target "$env:UEFN_MCP_PATH\vendor" pystray Pillow
```

Tested with pystray 0.19.5 and Pillow 12.2.0 (`cp311` wheel). If `vendor/` is missing, everything still works — the
window just hides with no tray icon.

## Auto-start

UEFN starts Python right after a project opens (when step 3 is done for that project), but then runs
`init_unreal.py` **only from a fixed whitelist of engine plugin folders**. Copying `init_unreal.py` into
`Content/Python`, `Documents/UnrealEngine/Python` or `UE_PYTHONPATH` does not start anything, and `.py` files inside a
UEFN project make session upload fail with `[ContainsPythonData]`.

`ensure_mcp_hook.ps1` appends a small marked hook to Epic's whitelisted
`<Fortnite>\Engine\Plugins\Experimental\Toolsets\EditorToolset\Content\Python\init_unreal.py`. The hook runs this
repo's `init_unreal.py`, which puts the repo on `sys.path` and imports `uefn_listener`. The Output Log then shows
`[MCP] Listener started on http://127.0.0.1:8765` and `[MCP] Auto-started on port 8765`.

```powershell
$hook = Join-Path $env:UEFN_MCP_PATH 'ensure_mcp_hook.ps1'
powershell -NoProfile -ExecutionPolicy Bypass -File $hook -DryRun   # shows what it would do
powershell -NoProfile -ExecutionPolicy Bypass -File $hook           # appends the hook (elevated shell if Program Files is write-protected)
```

**Fortnite updates overwrite the Epic file and remove the hook** (symptom: no `[MCP] Auto-started` after a project
opens). The script is idempotent, so let Windows re-run it every hour:

```powershell
schtasks /Create /F /SC HOURLY /TN "UEFN-MCP-hook" /TR "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$hook`""
```

The hook takes effect at the next project open; there is no remote way to start the listener in a project that is
already open (use **Tools > Execute Python Script**). Options: `-FortniteDir` (or `UEFN_FORTNITE_DIR`; default from the
Epic launcher manifest, else `%ProgramFiles%\Epic Games\Fortnite`), `-Target` (or `UEFN_MCP_HOOK_TARGET`; default: the
per-user shim `Documents\UnrealEngine\Python\init_unreal.py` if an older install created it, else this repo's
`init_unreal.py`), `-DryRun`. The script backs up the Epic file before writing and logs to `ensure_mcp_hook.log`.

## Tools

| Category | Tools | Needs |
|----------|-------|-------|
| **System (5)** | `ping`, `execute_python`, `get_log`, `get_editor_log`, `shutdown` | listener |
| **Actors (9)** | `get_all_actors`, `get_selected_actors`, `spawn_actor`, `delete_actors`, `set_actor_transform`, `get_actor_properties`, `set_actor_properties`, `select_actors`, `focus_selected` | listener |
| **Assets (9)** | `list_assets`, `get_asset_info`, `get_selected_assets`, `rename_asset`, `delete_asset`, `duplicate_asset`, `does_asset_exist`, `save_asset`, `search_assets` | listener |
| **Project, level, viewport (5)** | `get_project_info`, `save_current_level`, `get_level_info`, `get_viewport_camera`, `set_viewport_camera` | listener |
| **Asset management (7)** | `asset_batch_rename`, `asset_set_metadata`, `asset_get_metadata`, `asset_remove_metadata`, `asset_find_referencers`, `asset_find_dependencies`, `asset_find_unused` | listener |
| **Materials (12)** | `material_create`, `material_create_instance`, `material_add_expression`, `material_set_expression_property`, `material_connect_expressions`, `material_connect_property`, `material_list_expressions`, `material_recompile`, `material_set_scalar_param`, `material_set_vector_param`, `material_set_texture_param`, `material_set_static_switch_param` | listener |
| **Niagara (11)** | `niagara_place_actor`, `niagara_set_system_asset`, `niagara_activate`, `niagara_deactivate`, `niagara_reset`, `niagara_set_float_param`, `niagara_set_int_param`, `niagara_set_bool_param`, `niagara_set_vec3_param`, `niagara_set_color_param`, `niagara_set_texture_param` | listener |
| **Animation (11)** | `anim_get_info`, `anim_list_notify_tracks`, `anim_add_notify_track`, `anim_remove_all_notify_tracks`, `anim_list_notifies`, `anim_add_notify`, `anim_add_notify_state`, `anim_add_float_curve`, `anim_add_float_curve_key`, `anim_create_montage`, `anim_create_blendspace` | listener |
| **Static meshes (7)** | `staticmesh_get_info`, `staticmesh_enable_nanite`, `staticmesh_set_lods`, `staticmesh_remove_lods`, `staticmesh_add_collision`, `staticmesh_remove_collisions`, `staticmesh_generate_uv` | listener |
| **Data tables (6)** | `datatable_info`, `datatable_export_json`, `datatable_export_csv`, `datatable_import_json`, `datatable_import_csv`, `datatable_get_row` | listener |
| **Validation (3)** | `validate_asset`, `validate_folder`, `validate_selected` | listener |
| **Screenshots (2)** | `screenshot_viewport`, `screenshot_desktop` | listener / host (`mss`) |
| **Devices (3)** | `device_list_editables`, `device_set_editable`, `device_set_editables_bulk` | listener |
| **Play-In-Editor (3)** | `playtest_start`, `playtest_stop`, `playtest_status` | listener |
| **Scatter (1)** | `mesh_scatter` | listener |
| **Verse introspection (5)** | `verse_list_services`, `verse_list_editables`, `verse_service_graph`, `verse_find_resource_usage`, `verse_check_editable_coverage` | listener |
| **Verse build (3)** | `verse_compile`, `verse_status`, `verse_push` | UEFN open (no listener) |
| **Verse navigation (5)** | `verse_symbols`, `verse_hover`, `verse_definition`, `verse_find_symbol`, `verse_lsp_restart` | `epicgames.verse` VS Code extension + a UEFN-generated workspace (no listener) |
| **UEFN session (3)** | `uefn_status`, `uefn_launch_project`, `uefn_set_load_on_startup` | Windows host (no listener) |

Tools marked [experimental](#experimental-tools) ask the user to confirm before each call: `execute_python`,
`shutdown`, `delete_asset`, `asset_batch_rename`, `verse_push`, `uefn_launch_project`, `uefn_set_load_on_startup`.

The `execute_python` tool is the most powerful — it runs arbitrary Python code inside the editor with full access to the `unreal` module:

```python
# DESC: list actor labels      <- optional: shown as the task label in the status window
# Pre-populated variables: unreal, actor_sub, asset_sub, level_sub, tk, get_tk_root
# Assign to `result` to return a value

actors = actor_sub.get_all_level_actors()
result = [a.get_actor_label() for a in actors]
```

> **Tkinter note:** When creating UI windows via `execute_python`, use `get_tk_root()` + `tk.Toplevel(root)`. Never call `tk.Tk()` — multiple instances crash the editor.

> **Timeouts:** a listener call returns after 30 s, but the code keeps running in the editor. Split long jobs into
> smaller calls and check the editor state afterwards instead of re-running a timed-out call.

## Experimental tools

Seven tools do something that is hard to undo or reaches outside the open level: run arbitrary code, delete or rename
assets, push to a live session, stop the listener, start UEFN or edit its settings. They carry the `[experimental]`
tag, and an agent should ask the user to confirm before each call:

| Tool | Why |
|------|-----|
| `execute_python` | Runs arbitrary Python inside the editor |
| `delete_asset` | Deletes an asset |
| `asset_batch_rename` | Renames many assets at once |
| `shutdown` | Stops the listener |
| `verse_push` | Pushes changes to a live session |
| `uefn_launch_project` | Starts UEFN; may retarget its last project or stop an orphaned crash reporter |
| `uefn_set_load_on_startup` | Edits UEFN's settings ini |

The tag shows in three places: the MCP title starts with `[experimental]`, the annotations set `destructiveHint=true`,
and the description starts with "[experimental] Ask the user to confirm before each call." The server instructions
repeat the list, so the agent sees the tag before it calls the tool.

## UEFN session and crash recovery

The `uefn_*` tools run in the MCP server process (Windows, no listener needed) and read the editor log, UEFN's settings
ini, the Epic launcher manifests, processes, windows and ports. They send no mouse or keyboard input.

- `uefn_status`: read-only state (`not_running`, `starting`, `hub`, `opening`, `project_open`, `other_project_open`,
  `open_failed`, `crash_dialog`), the open project, whether the editor windows respond, the crash reporter, Python /
  listener / Toolsets markers, ports, "Load on Startup", the autostart hook, and next-step hints.
- `uefn_launch_project`: starts UEFN if needed (Epic launcher URI or the editor exe), waits for the project, then for
  the listener. If UEFN stops on the HUB (project browser) it returns status `hub`: the user opens the project, then the
  agent calls again with `launch=False`. `close_crash_reporter=true` (default false) stops a crash reporter left over
  from a crash, only when no editor process runs. It never closes or kills an editor.
- `uefn_set_load_on_startup`: `HomeScreen` (the HUB) or `LastProject` (Most Recent Project). UEFN must be closed
  (it rewrites the file on exit); a backup is kept; `dry_run` shows the change only.

**Load on Startup = Most Recent Project** is what makes unattended recovery work. Set it in **Editor Preferences >
Loading & Saving > Load on Startup** (or with the selector on the HUB screen). The ini key is
`ValkyrieLoadAtStartupMostRecentProject=LastProject` in section `[/Script/ValkyrieEditor.ValkyrieEditorConfig]` of
`%LOCALAPPDATA%\UnrealEditorFortnite\Saved\Config\WindowsEditor\EditorPerProjectUserSettings.ini`; with UEFN closed,
`uefn_set_load_on_startup('LastProject')` or `setup.ps1 -EnableLoadLastProject` writes it. With this setting a crash
costs one call: `uefn_launch_project` relaunches UEFN (pointing the last project at the requested one first), the
project opens without the HUB, and the listener autostarts through the [hook](#auto-start).

## Architecture

The system uses two independently running Python processes, plus two direct channels for Verse:

| Component | File | Runs in | Python | Dependencies |
|-----------|------|---------|--------|--------------|
| **Listener** | `uefn_listener.py` | UEFN editor process | 3.11 (embedded) | stdlib; optional `pystray` + `Pillow` in `vendor/` |
| **MCP Server** | `mcp_server.py` | External process | 3.10+ (system) | `mcp` SDK; optional `mss` |
| **Verse build** | `verse_workflow.py` | MCP server process | — | UEFN's VerseWorkflowServer on TCP 1962 |
| **Verse navigation** | `verse_lsp_service.py` | MCP server process (spawns `verse-lsp.exe`) | — | `epicgames.verse` VS Code extension |
| **UEFN session** | `uefn_session.py`, `win_procs.py` | MCP server process | — | editor log, settings ini, Epic launcher manifests, ports; read-only Win32 process / window queries through `ctypes` |

**Why two processes?**
- All `unreal.*` calls must happen on the editor's main thread (tick callback)
- The MCP SDK needs pip-installable packages that can't be added to UEFN's embedded Python
- Each component can restart independently

See [docs/architecture.md](docs/architecture.md) for details.

## Configuration

| Variable | Used by | Default | Purpose |
|----------|---------|---------|---------|
| `UEFN_MCP_PATH` | `.mcp.json`, docs | — | Path of this checkout |
| `UEFN_MCP_PORT` | `mcp_server.py` | `8765` | First port of the listener scan (up to 8770); `--port N` pins one port |
| `VERSE_WORKFLOW_HOST`, `VERSE_WORKFLOW_PORT` | `verse_workflow.py` | `127.0.0.1`, `1962` | VerseWorkflowServer address |
| `VERSE_LSP_EXE` | `verse_lsp_service.py` | newest `~/.vscode/extensions/epicgames.verse-*/bin/Win64/verse-lsp.exe` | Language server binary |
| `VERSE_WORKSPACE_FILE` | `verse_lsp_service.py` | newest `*.code-workspace` in `%LOCALAPPDATA%\UnrealEditorFortnite\Saved\VerseProject` | Workspace of the open project |
| `UEFN_FORTNITE_DIR`, `UEFN_MCP_HOOK_TARGET` | `ensure_mcp_hook.ps1` | see [Auto-start](#auto-start) | Fortnite install root, file the hook runs |
| `VERSE_TEST_FILE` | `tests/test_verse_tools_live.py` | `Core/service.verse` | File for the LSP smoke test |
| `UEFN_EDITOR_EXE` | `uefn_session.py` | Epic launcher manifest, then `UEFN_FORTNITE_DIR`, then the default install folder | Editor executable for `uefn_launch_project` |
| `UEFN_PROJECT` | `uefn_session.py` | `LastProjectFileName` of UEFN | Default project for `uefn_launch_project` |
| `UEFN_SAVED_DIR` | `uefn_session.py` | `%LOCALAPPDATA%\UnrealEditorFortnite\Saved` | UEFN's per-user folder (logs, settings) |

Custom port example:

```json
{
  "mcpServers": {
    "uefn": {
      "command": "python",
      "args": ["${UEFN_MCP_PATH}/mcp_server.py"],
      "env": { "UEFN_MCP_PORT": "8766" }
    }
  }
}
```

## Known limitations

UEFN exposes a large subset of the Unreal Python API, but a few editor actions have no scripting surface:

- **Push Changes / Verse build** — no Python API (`FortniteEditorLibrary`, `FortEditorUtilityLibrary`, `Fort*`/`Creative*` libraries and console commands such as `UEFN.PushChanges` do not expose them). `verse_compile` and `verse_push` use the Verse workflow socket instead, the channel of the VS Code extension. `verse_push` works only while a session runs; new textures and other new assets still need a session relaunch. Launch Session and the other toolbar actions have no scripting surface either: the user clicks them.
- **Verse `@editable` fields** — ScriptDevice bindings block `get_editor_property` on Verse-declared fields, and plain-name writes fail ("Failed to find property"). `device_list_editables` / `device_set_editable` cover native device properties; for Verse fields use `execute_python` on the device's inner object (mangled name `__verse_0x<HASH>_<Field>`), or read them with the official `unreal-mcp` DeviceToolset. `verse_check_editable_coverage` audits the sources instead of the live level.
- **`get_editor_log`** picks the newest `.log` in the project log folder, which can be the revision-control log (upstream PR #3).
- **Static-mesh metadata in UEFN 42.20** — the `StaticMeshEditorSubsystem` getters (`has_vertex_colors`, `get_lod_count`, `get_number_verts`, ...), `StaticMesh.get_num_triangles` / `get_num_sections` and `BodySetup.agg_geom.export_text()` crashed the editor. `staticmesh_get_info` reads asset-registry tags, slots, bounds and Nanite settings instead and reports the rest as "not available safely in UEFN 42.20" ([details](docs/tools_reference.md#static-meshes-only-crash-safe-reads-uefn-4220)).

## Tests

| Command | Needs |
|---------|-------|
| `python tests/test_mcp_server_offline.py` | host Python with `requirements.txt`; no UEFN, no listener, no network |
| `python tests/test_rotation_offline.py` | host Python with `requirements.txt`; checks the listener source and handlers against a fake `unreal` (named axes, keyword-built rotators) and the server schemas / listener-version guard |
| `python tests/test_staticmesh_safety_offline.py` | nothing: no crash-list getter anywhere in the listener; `staticmesh_*` handlers against a fake `unreal` whose crashing getters raise |
| `python tests/test_uefn_session_offline.py` | nothing (temp files only) |
| `python tests/test_safety_fixes_live.py --yes-touch-editor [--camera] [--niagara <system>] [--mesh <mesh>]` | UEFN with a scratch level; spawns and deletes a test cube (never saves), checks the rotation axes and the safe static-mesh reads end to end |
| `python tests/test_verse_tools_live.py lsp [file.verse]` | `epicgames.verse` extension + a UEFN-generated workspace (UEFN may be closed) |
| `python tests/test_verse_tools_live.py compile` | UEFN open with the project; triggers a real Verse build |
| `tests/test_feasibility.py` | run inside UEFN (Tools > Execute Python Script) before the listener starts: it binds ports 8765/8766 |

## Bonus Tools

Scripts that run inside the UEFN editor to introspect the Python API.
Run via **Tools > Execute Python Script** in the UEFN menu bar.

| Script | Description |
|--------|-------------|
| [`tools/dump_uefn_api.py`](tools/dump_uefn_api.py) | Dump all classes, enums, structs, functions to JSON |
| [`tools/generate_uefn_stub.py`](tools/generate_uefn_stub.py) | Generate `.pyi` type stub for IDE autocomplete (37K+ types) |
| [`tests/test_feasibility.py`](tests/test_feasibility.py) | Verify UEFN sandbox supports HTTP/threading for MCP |

## Documentation

| Document | Description |
|----------|-------------|
| [Setup Guide](docs/setup.md) | Detailed installation and configuration |
| [Tools Reference](docs/tools_reference.md) | Every tool: detailed pages for the 0.1-0.3 tools, summary tables for the 0.4.0 and 0.5.0 additions |
| [Architecture](docs/architecture.md) | How the two-component system works internally |
| [Troubleshooting](docs/troubleshooting.md) | Common issues and solutions |
| [UEFN Python Capabilities](docs/uefn_python_capabilities.md) | Full API capabilities map — 37K types across 30 domains |
| [Changelog](CHANGELOG.md) | Release history |

## Requirements

- UEFN editor with Python enabled for the project (Project Settings)
- Python 3.10+ on the host system, `pip install -r requirements.txt`
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI
- Optional: the `epicgames.verse` VS Code extension (Verse navigation tools), `mss` (`screenshot_desktop`), `pystray` + `Pillow` in `vendor/` (tray icon)
- UEFN session tools: Windows; nothing to install

## License

MIT
