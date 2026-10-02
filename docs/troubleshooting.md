# Troubleshooting

## Connection Issues

### "UEFN listener is not running"

**Cause:** The MCP server cannot reach the HTTP listener inside UEFN.

**Fix:**
1. Make sure UEFN editor is open
2. Go to **Tools > Execute Python Script** and select `uefn_listener.py`
3. Check the Output Log (Window > Output Log) for `[MCP] Listener started on http://127.0.0.1:8765`
4. Verify with curl: `curl http://127.0.0.1:8765/`

`verse_compile`, `verse_status`, `verse_push` and the Verse navigation tools do not need the listener; they keep
working while it is down.

### No `[MCP] Auto-started` after a project opens

**Cause:** one of the two autostart gates is closed.

- **The hook is gone.** Fortnite updates overwrite Epic's `EditorToolset` `init_unreal.py`. Run
  `ensure_mcp_hook.ps1` (or wait for the hourly scheduled task) and reopen the project. The hook only runs at project
  open; in an already open project start the listener by hand.
- **Python is off for this project.** The log has no `Python enabled via IPythonScriptPlugin::ForceEnablePythonAtRuntime`
  after the project opens. Enable **Python Editor Script Plugin** in Project Settings for this project (UEFN stores it per
  project and per user as `EnablePythonLocallyPerProject`). Remotely: the official `unreal-mcp` tool
  `ValkyriePythonToolset.EnablePythonInUEFN` turns it on and `IsPythonEnabledInUEFN` checks it. Do not edit
  `EditorPerProjectUserSettings.ini` while UEFN runs; the editor rewrites it on exit.
- A `Python disabled via CVar` line on the UEFN hub screen (before any project opens) is normal.

### Listener logged "started", but every connection is refused (after switching projects)

**Cause:** before 0.4.0, closing a project destroyed the Python interpreter but leaked the listener's socket, which kept
port 8765 bound and reset every connection until the editor exited; the next listener bound over it. Signature:
`netstat -ano | findstr :8765` shows `LISTENING` owned by the UEFN process, yet connections are refused.

**Fix:** 0.4.0 releases the socket on Python shutdown, never reuses a bound port (the next listener takes 8766; the
server scans 8765-8770) and rebinds from a 30 s watchdog. With an older listener, restart the editor.

### "Connection refused" after listener was running

**Cause:** The listener crashed or the editor was restarted.

**Fix:** Re-run the listener via **Tools > Execute Python Script**. For auto-start, install the hook
(`ensure_mcp_hook.ps1`, see [Setup Guide](setup.md)).

### Port conflict

**Cause:** Port 8765 is already in use by another process.

**Fix:** The listener takes the first free port in 8765-8770 and the MCP server scans the same range, so nothing needs
configuring. To pin a port, pass `--port N` to `mcp_server.py` (or set `UEFN_MCP_PORT` to the first port to scan).

To find what's using the port:
```bash
netstat -ano | findstr :8765
```

### The MCP bridge died, but the listener is alive

**Fix:** talk to the listener directly — `POST http://127.0.0.1:<port>` with
`{"command": "<tool name>", "params": {...}}` (the same commands the MCP tools send; `execute_python` takes
`{"code": "..."}`). A `504` means the command is still running in the editor. Smoke-test the bridge itself with
`python tests/test_mcp_server_offline.py`, then reconnect the server in Claude Code (`/mcp`).

## Command Errors

### "Command timed out after 30s"

**Cause:** The command took too long to execute on the main thread, or the editor is frozen/busy. The command is
**not cancelled**: it keeps running inside UEFN, and the listener answers again when it finishes.

**Possible reasons:**
- Editor is compiling shaders
- Editor is loading a large level
- The Python code in `execute_python` has an infinite loop
- A very large operation (e.g., saving thousands of packages, building Nanite for dozens of meshes)
- A hidden modal dialog (see below)

**Fix:**
- Do not re-run the call blindly: poll `ping` until it answers, then check what was already applied
- Split bulk work into calls that finish in under ~25 s (e.g. at most ~400 package saves or ~10 Nanite builds per call)
- For a script that may stop halfway, write a marker (file or log line) at its end and check the marker
- Check the UEFN Output Log for errors

### `verse_compile` returns `ok: false` with no build activity

**Cause:** a modal dialog is open inside UEFN (for example "Overwrite Existing Object" after `create_asset` on an
existing path). Python keeps ticking, so `ping` still answers, but builds wait. The dialog can sit behind other windows.

**Fix:** find the second top-level window of the UEFN process and close it with a real click; avoid `create_asset` on
paths that may exist.

### "VerseWorkflowServer not reachable on 127.0.0.1:1962"

**Cause:** UEFN is not running or has no project open. The workflow socket belongs to the open project.

**Fix:** open the project; `verse_status` confirms the connection without compiling.

### Asset import or viewport screenshot hangs

**Cause:** UEFN defers some work while its window is in the background.

**Fix:** bring the UEFN window to the foreground; queued commands then finish. Check the result (`does_asset_exist`,
the screenshot file) rather than re-running.

### "Unknown command: xyz"

**Cause:** The command name doesn't match any registered handler.

**Fix:** Use `ping` to see the list of available commands. Make sure listener and MCP server versions match.

### "Actor not found" / "Asset not found"

**Cause:** The path or label doesn't match any existing object.

**Fix:**
- Use `get_all_actors` to list actors and find the correct path/label
- Use `list_assets` to browse the content directory
- Actor labels are case-sensitive
- Asset paths start with the project's mount point (in UEFN usually `/<ProjectOrPlugin>/`, not `/Game/`) or `/Engine/`

### `device_set_editable`: "Failed to find property"

**Cause:** Verse-declared `@editable` fields live on the device's inner object under a mangled name
(`__verse_0x<HASH>_<Field>`), not on the actor.

**Fix:** use `execute_python` on the inner object, or read the fields with the official `unreal-mcp` DeviceToolset.

### "rotation: the list [...] is ambiguous" / "unknown key(s)"

**Cause:** since 0.5.0 rotations are named axes. A list is accepted only when its three values are equal: before
0.5.0 the tools documented `[pitch, yaw, roll]` but UEFN applied the list as `[roll, pitch, yaw]`
(`unreal.Rotator`'s Python constructor is `Rotator(roll, pitch, yaw)`), silently.

**Fix:** pass `{"pitch": P, "yaw": Y, "roll": R}` (missing axes are 0); the error text spells out both readings of
your list. Notes and scripts written before 0.5.0 that say "`set_viewport_camera` reads `[roll, pitch, yaw]`"
describe the old bug: convert them to named axes. In `execute_python` build rotators with keywords
(`unreal.Rotator(roll=0.0, pitch=-90.0, yaw=0.0)`).

### "'<command>' needs listener protocol 0.3.3 or later"

**Cause:** the MCP server is 0.5.0 but UEFN still runs an older `uefn_listener.py` (loaded when the project opened).
That listener would apply rotation lists with swapped axes and its `staticmesh_*` handlers call getters that crash
UEFN 42.20, so rotations, `focus_selected` and `staticmesh_*` are not sent to it (nothing was changed).

**Fix:** load the current listener: reopen the project (autostart hook) or **Tools > Execute Python Script** >
`uefn_listener.py`; `ping` then reports version 0.3.3 or later.

### `staticmesh_get_info` says "not available safely in UEFN 42.20"

**Expected.** UEFN 42.20 crashed (`EXCEPTION_ACCESS_VIOLATION` reading `0x18` in the Engine DLL) on one read-only probe
of the `StaticMeshEditorSubsystem` metadata getters (`get_lod_count`, `get_number_verts`, `get_number_materials`,
`get_simple_collision_count`, `get_collision_complexity`, `get_convex_collision_count`, `get_lod_screen_sizes`,
`get_nanite_settings`, `has_vertex_colors`, `get_num_uv_channels`), `StaticMesh.get_num_triangles` /
`get_num_sections` and `BodySetup.agg_geom.export_text()`. The tools read asset-registry tags, material slots, bounds
and the `nanite_settings` property instead; `simple_collision_count`, `convex_collision_count`, `has_vertex_colors` and
`lod_screen_sizes` carry that marker (`collision_prims` counts simple and convex shapes together; vertex colours and
UV sets can be read from an exported GLB). `staticmesh_remove_lods` / `staticmesh_remove_collisions` no longer read
the count back: call `staticmesh_get_info` in a separate call.

## Python Execution Issues

### `execute_python` returns empty result

**Cause:** The code didn't assign to the `result` variable.

**Fix:** Assign your return value to `result`:
```python
# Wrong — no output
x = 1 + 1

# Correct
result = 1 + 1
```

### `execute_python` shows error in stderr

**Cause:** The Python code raised an exception.

**Fix:** Check the `stderr` field for the full traceback. Common issues:
- `AttributeError`: The API method doesn't exist in UEFN (check `docs/uefn_python_capabilities.md`)
- `TypeError`: Wrong argument types (use `unreal.Vector`, `unreal.Rotator`, etc.; `unreal.Rotator` takes
  `(roll, pitch, yaw)` positionally, so always pass keywords)
- `RuntimeError`: Editor state doesn't allow the operation (e.g., saving during PIE)

### Calls that crash the editor

Never call `tk.Tk()` (use `get_tk_root()` + `tk.Toplevel`), never open asset editors from Python
(`open_editor_for_assets`), and never run the console command `EDIT COPY` with a `None` world context.

In UEFN 42.20 never call the static-mesh metadata getters (`StaticMeshEditorSubsystem.has_vertex_colors`,
`get_lod_count`, `get_number_verts`, the other getters listed above, `StaticMesh.get_num_triangles` /
`get_num_sections`, `BodySetup.agg_geom.export_text()`): one probe killed the editor. Read the asset-registry tags
(`unreal.AssetRegistryHelpers.get_tag_value(asset_data, "Triangles")`), `static_materials`, `get_bounding_box()` or
`nanite_settings` instead, and try any new editor API on one asset in its own call before a bulk loop, after saving.

### `print()` output not visible

**Cause:** By default, `print()` output goes to `stdout` which is captured and returned in the response.

**Fix:** Check the `stdout` field in the response. If you want it in the UE Output Log too, use:
```python
unreal.log("My message")
```

## MCP Server Issues

### Claude Code doesn't show UEFN tools

**Cause:** `.mcp.json` not found, the server is waiting for approval, or it failed to start.

**Fix:**
1. `claude mcp list` — is `uefn` listed, and is it `Connected`, failed or pending approval?
2. Pending approval never goes away: approve it in `/mcp`, or register the server at user scope
   (`claude mcp add uefn -s user -- python "$env:UEFN_MCP_PATH\mcp_server.py"`)
3. Check that `UEFN_MCP_PATH` is set for the process that starts Claude Code (restart the terminal or IDE after setting it)
4. Verify `mcp` SDK is installed: `pip install -r requirements.txt`
5. Test the server manually: `python tests/test_mcp_server_offline.py`
6. Restart Claude Code

### Windows: `python` opens the Microsoft Store or does nothing

**Cause:** `python` resolves to the App execution alias in `%LOCALAPPDATA%\Microsoft\WindowsApps`, which Claude Code
cannot start.

**Fix:** turn off the `python.exe` / `python3.exe` App execution aliases (Windows Settings > Apps > Advanced app
settings), make sure a real Python is on `PATH`, or put the full path of a real `python.exe` into `command`.

### "ModuleNotFoundError: No module named 'mcp'"

**Cause:** MCP SDK not installed in the Python used by Claude Code.

**Fix:**
```bash
pip install -r requirements.txt
```

Make sure you're installing for the same Python that `.mcp.json` references. If you have multiple Python versions:
```bash
python3 -m pip install mcp
```

### Verse navigation returns nothing or stale results

**Fix:** run `verse_lsp_restart` after a compile regenerated the digests or after switching projects. The LSP picks
the most recently modified `.code-workspace` of UEFN; set `VERSE_WORKSPACE_FILE` to pin a project. It needs the
`epicgames.verse` VS Code extension (or `VERSE_LSP_EXE`). The LSP reports no compile errors — use `verse_compile`.

## Editor Issues

### Editor freezes briefly when executing commands

**Expected behavior.** Commands execute on the main thread, which blocks the editor for the duration of the operation. Keep operations fast. For batch operations, use `ScopedSlowTask` to show a progress bar:

```python
with unreal.ScopedSlowTask(100, 'Processing...') as task:
    task.make_dialog(True)
    for i in range(100):
        if task.should_cancel():
            break
        task.enter_progress_frame(1)
        # ... work
```

### Listener survives editor restart?

**No.** The listener runs inside the editor process. When the editor closes, the listener dies. It starts again at the
next project open when the autostart hook is installed; otherwise start it by hand.

### Multiple editor instances

Each editor instance needs its own listener on a different port. The auto-detect range (8765-8770) supports up to 6 simultaneous instances. Configure each MCP server connection with the correct port.

## UEFN session (uefn_* tools)

### UEFN crashed: let the agent recover by itself

UEFN crashes often. Set **Editor Preferences > Loading & Saving > Load on Startup = Most Recent Project** (the HUB
screen has the same selector; with UEFN closed, `uefn_set_load_on_startup('LastProject')` or
`setup.ps1 -EnableLoadLastProject` writes `ValkyrieLoadAtStartupMostRecentProject=LastProject` into
`%LOCALAPPDATA%\UnrealEditorFortnite\Saved\Config\WindowsEditor\EditorPerProjectUserSettings.ini`). Then one call to
`uefn_launch_project(project=...)` relaunches UEFN, the project opens without the HUB, and the listener autostarts
through the hook. Without this setting UEFN stops on the HUB after every relaunch and the user has to click the
project.

### `uefn_launch_project` returns "hub"

UEFN's "Load on Startup" is "Home Panel", so UEFN stopped on the HUB (project browser). The server sends no mouse or
keyboard input: open the project yourself, then the agent calls `uefn_launch_project(project=..., launch=False)` to
wait for the listener. To skip the HUB next time, see the previous entry.

### `uefn_status` / `uefn_launch_project` report "crash_dialog"

The crash reporter (`CrashReportClientEditor`) is showing. While an editor process still runs, wait for it to exit.
When it is left over from a crash (no editor process), close it, or let the agent call
`uefn_launch_project(close_crash_reporter=True)`, which stops it (only when no editor process runs) and then launches
UEFN. The session tools never close or kill an editor.

### `uefn_set_load_on_startup` refuses: UEFN is running

UEFN rewrites `EditorPerProjectUserSettings.ini` on exit, so an edit made while it runs would be lost. Close UEFN and
call again, or change the setting in the editor (Editor Preferences > Loading & Saving > Load on Startup).

### `uefn_status` says the listener is "busy"

The listener port accepts connections but did not answer within 2 s: a long command is running in the editor. Wait
and re-check before restarting anything.
