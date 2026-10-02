# Changelog

Release versions of the repo. The listener also reports a protocol version in `ping` (`PROTOCOL_VERSION` in
`uefn_listener.py`); it changes only when the listener/server wire format changes.

## 0.5.0 — 2026-10-02

110 MCP tools (3 new UEFN session tools, host-side: they run in the MCP server process and need no listener).
Listener protocol 0.3.3 (was 0.3.2): rotation parameters take named axes and the static-mesh handlers changed (see
Fixed). The server sends no mouse or keyboard input anywhere.

### Added — UEFN session (`uefn_session.py` + `win_procs.py`, 3 tools, Windows)

- `uefn_status` (read-only): state (`not_running`, `starting`, `hub`, `opening`, `project_open`,
  `other_project_open`, `open_failed`, `crash_dialog`) from the editor log markers (the window title never names the
  project), editor windows and whether they respond, the crash reporter, ports (listener 8765-8770 incl. "busy", 1962,
  8000), Python-on / listener-autostart / Toolsets markers for the open project, "Load on Startup", the autostart hook,
  the install; next-step hints.
- `uefn_launch_project`: resolves a project (path, folder or name), starts UEFN through the Epic launcher URI
  (`Fortnite_Studio`, with sign-in) or the editor exe, waits for the project load, then for the listener, and explains
  a missing one. With "Load on Startup" = Most Recent Project it points `LastProjectFileName` at the requested project
  first (UEFN closed only), so the project opens without the HUB. When UEFN stops on the HUB it returns status `hub`:
  the user opens the project, then the agent calls again with `launch=False`. `close_crash_reporter` (default false)
  stops a crash reporter left over from a crash, only when no editor process runs. It never closes or kills an editor.
- `uefn_set_load_on_startup`: `ValkyrieLoadAtStartupMostRecentProject` = `HomeScreen` | `LastProject` in
  `EditorPerProjectUserSettings.ini` (`[/Script/ValkyrieEditor.ValkyrieEditorConfig]`), byte-exact edit with a
  backup, refused while UEFN runs, `dry_run`.
- `win_procs.py`: read-only Windows process and window queries through `ctypes` (no package). Its only acting call
  stops an orphaned crash reporter for `close_crash_reporter`.
- Recommendation: set **Editor Preferences > Loading & Saving > Load on Startup = Most Recent Project** (also a
  selector on the HUB screen). UEFN crashes often; with this setting `uefn_launch_project` relaunches UEFN, the project
  opens without the HUB and the listener autostarts through the hook, so an agent recovers by itself. Without it the
  user clicks the project on the HUB.

### Added — `[experimental]` tag (`tool_tags.py`)

- 7 tools that are hard to undo or act outside the open level need the user's confirmation before each call:
  `execute_python`, `delete_asset`, `asset_batch_rename`, `shutdown`, `verse_push`, `uefn_launch_project`,
  `uefn_set_load_on_startup`. Their MCP title starts with `[experimental]`, their annotations set
  `destructiveHint=true`, and their description starts with "[experimental] Ask the user to confirm before each call."
  The server instructions list them too.

### Added — setup

- `setup.ps1`: idempotent machine setup (`-DryRun`): real Python 3.10+, `requirements.txt`, `UEFN_MCP_PATH`,
  `ensure_mcp_hook.ps1` (+ `-ScheduleHook`), UEFN report (install, "Load on Startup", Python enabled for the project,
  hook), `-WithTray`, `-WithMss`, `-EnableLoadLastProject`, `-RegisterClaude`, `-SkipPip`, and what is left to do.
- `uefn_session.py` doubles as a CLI (`status`, `find-install`, `set-load-on-startup`) used by `setup.ps1`.

### Fixed — safety

- **Rotation axis order.** `unreal.Rotator`'s Python constructor is `Rotator(roll, pitch, yaw)`. `spawn_actor`,
  `set_actor_transform`, `set_viewport_camera`, `niagara_place_actor` and the `device_set_editable` rotator path passed
  the documented `[pitch, yaw, roll]` list positionally, so UEFN applied `[roll, pitch, yaw]` with no error (the
  `{pitch, yaw, roll}` dict of `device_set_editable` was swapped the same way), and `focus_selected` looked up at the
  sky (pitch 45, roll -35) instead of at the selection. Rotations are now named axes `{"pitch", "yaw", "roll"}` in
  degrees (a `Rotation` schema with `additionalProperties: false`; missing axes are 0), every `unreal.Rotator` is built
  with keywords, and a list is accepted only when its three values are equal: any other list is refused with both of
  its readings spelled out. `staticmesh_generate_uv` passed `Vector2D` for all gizmo arguments; it now takes
  `position` [x, y, z] (default: bounds center), a named-axes `orientation`, `tiling` [u, v] and, for box, a new
  `size` [x, y, z] (default: bounds size).
- **Static-mesh getters that crash UEFN 42.20.** `staticmesh_get_info`, `staticmesh_enable_nanite`,
  `staticmesh_remove_lods` and `staticmesh_remove_collisions` called `StaticMeshEditorSubsystem` metadata getters
  (`has_vertex_colors`, `get_lod_count`, `get_number_verts`, `get_nanite_settings`, ...), which crash UEFN 42.20
  (EXCEPTION_ACCESS_VIOLATION). The tools now read asset-registry tags, `static_materials`, `get_bounding_box()` and
  the `nanite_settings` property only; `simple_collision_count`, `convex_collision_count`, `has_vertex_colors`,
  `lod_screen_sizes` and the read-back counts of the two remove tools say "not available safely in UEFN 42.20".
  `staticmesh_get_info` adds `triangles_lod0`, `materials`, `collision_prims`, `bounds`, `registry` and
  `read_errors`; `staticmesh_enable_nanite` keeps the mesh's other Nanite settings.
- **Mixed versions.** The listener with these fixes reports protocol 0.3.3. The MCP server reads the version from
  `GET /` and does not send a rotation, `focus_selected` or a `staticmesh_*` command to an older listener (which would
  swap the axes or call the crashing getters): it asks to reload `uefn_listener.py` and sends nothing.

### Removed

- Desktop control (mouse / keyboard input and window tools), which was never released: the server sends no input.
  `screenshot_desktop` (optional `mss`) is unchanged.

### Tests and docs

- `tests/test_uefn_session_offline.py`; `tests/test_mcp_server_offline.py` expects 110 tools, named-axes rotation
  schemas and exactly the 7 `[experimental]` tools tagged.
- Safety fixes: `tests/test_rotation_offline.py` (AST scan for keyword-built rotators, `_parse_rotation`, the rotation
  handlers against a fake `unreal` with the real `Rotator` signature, server schemas and the listener-version guard),
  `tests/test_staticmesh_safety_offline.py` (AST scan: no crash-list getter anywhere in the listener; the
  `staticmesh_*` handlers against a fake `unreal` whose crashing getters raise); helper `tests/_listener_ast.py`
  compiles listener functions without `unreal`. `tests/test_safety_fixes_live.py --yes-touch-editor [--camera]`
  (scratch level, spawns and deletes a test cube, never saves) checks the rotation axes, `focus_selected`, the
  ambiguous-list refusal and the static-mesh reads in UEFN.
- README, `docs/setup.md`, `docs/tools_reference.md` ("Conventions (0.5.0)": rotations, crash-safe static-mesh reads),
  `docs/architecture.md`, `docs/troubleshooting.md`, `docs/uefn_python_capabilities.md` updated.

## 0.4.0 — 2026-09-30

A community fork merged into the base repo. Compared with 0.2.0 (`3f65857`): 107 MCP tools instead of 28
(79 new), a Verse module that works without the editor listener, listener autostart, a tray icon and a more robust
listener. Listener protocol 0.3.2 (was 0.2.0).

### Added — editor tools (listener, `mcp_server.py` + `uefn_listener.py`)

- **Materials (12):** `material_create`, `material_create_instance`, `material_add_expression`,
  `material_set_expression_property`, `material_connect_expressions`, `material_connect_property`,
  `material_list_expressions`, `material_recompile`, `material_set_scalar_param`, `material_set_vector_param`,
  `material_set_texture_param`, `material_set_static_switch_param`.
- **Niagara (11):** `niagara_place_actor`, `niagara_set_system_asset`, `niagara_activate`, `niagara_deactivate`,
  `niagara_reset`, `niagara_set_float_param`, `niagara_set_int_param`, `niagara_set_bool_param`,
  `niagara_set_vec3_param`, `niagara_set_color_param`, `niagara_set_texture_param`.
- **Animation (11):** `anim_get_info`, `anim_list_notify_tracks`, `anim_add_notify_track`,
  `anim_remove_all_notify_tracks`, `anim_list_notifies`, `anim_add_notify`, `anim_add_notify_state`,
  `anim_add_float_curve`, `anim_add_float_curve_key`, `anim_create_montage`, `anim_create_blendspace`.
- **Static meshes (7):** `staticmesh_get_info`, `staticmesh_enable_nanite`, `staticmesh_set_lods`,
  `staticmesh_remove_lods`, `staticmesh_add_collision`, `staticmesh_remove_collisions`, `staticmesh_generate_uv`.
- **Asset management (7):** `asset_batch_rename`, `asset_set_metadata`, `asset_get_metadata`,
  `asset_remove_metadata`, `asset_find_referencers`, `asset_find_dependencies`, `asset_find_unused`.
- **Data tables (6):** `datatable_info`, `datatable_export_json`, `datatable_export_csv`, `datatable_import_json`,
  `datatable_import_csv`, `datatable_get_row`.
- **Validation (3):** `validate_asset`, `validate_folder`, `validate_selected`.
- **Screenshots (2):** `screenshot_viewport` (listener command `screenshot_start` + file polling on the host),
  `screenshot_desktop` (host only, optional `mss`).
- **Device `@editable` fields (3):** `device_list_editables`, `device_set_editable`, `device_set_editables_bulk`.
- **Play-In-Editor (3):** `playtest_start`, `playtest_stop`, `playtest_status`.
- **Scatter (1):** `mesh_scatter` (StaticMeshActors in a box, seeded, clearance radius, folder, material, collision).
- **Verse source introspection (5):** `verse_list_services`, `verse_list_editables`, `verse_service_graph`,
  `verse_find_resource_usage`, `verse_check_editable_coverage` (static parsing of the project's `.verse` files,
  run by the listener).

### Added — Verse module (no listener needed)

- `verse_workflow.py` (3 tools): `verse_compile`, `verse_status`, `verse_push` over the VerseWorkflowServer that the
  open editor exposes on TCP `127.0.0.1:1962` (the channel of the `epicgames.verse` VS Code extension). Structured
  `errors[]` / `warnings[]`, a capped `info[]` log, the build state; `verse_status` never compiles.
  `VERSE_WORKFLOW_HOST` / `VERSE_WORKFLOW_PORT` override the address.
- `verse_lsp_service.py` (5 tools): `verse_symbols`, `verse_hover`, `verse_definition`, `verse_find_symbol`,
  `verse_lsp_restart` through a persistent `verse-lsp.exe` (from the `epicgames.verse` extension) initialized with the
  UEFN-generated multi-root `.code-workspace`. `VERSE_LSP_EXE` / `VERSE_WORKSPACE_FILE` override discovery.

### Added — autostart

- `ensure_mcp_hook.ps1`: appends a marked hook to Epic's whitelisted
  `Engine/Plugins/Experimental/Toolsets/EditorToolset/Content/Python/init_unreal.py` that runs `init_unreal.py`
  with `runpy` when UEFN starts Python (projects with "Enable Python Locally" on). Idempotent, replaces a hook that
  points at another file, backs up the Epic file, logs to `ensure_mcp_hook.log`, `-DryRun`. Fortnite updates
  overwrite the Epic file, so run it after updates or as an hourly scheduled task. Paths come from parameters or
  `UEFN_FORTNITE_DIR` / `UEFN_MCP_HOOK_TARGET`; nothing machine-specific is committed.
- `init_unreal.py` rewritten as a self-locating entry point: puts its own folder on `sys.path` and imports
  `uefn_listener`. The old advice (copy `init_unreal.py` into `Content/Python`, or `UE_PYTHONPATH`) does not work in
  UEFN and breaks session upload (`[ContainsPythonData]`).

### Added — listener UI and robustness

- Status window: closing it hides it; a system-tray icon (optional `pystray` + `Pillow` vendored into `vendor/`,
  `icon.png`) re-opens it and offers Restart / Start / Stop / Quit. Without `vendor/` everything works, just no tray.
- Task labels: an `execute_python` call whose code starts with `# DESC: <text>` shows that text in the status window.
- No zombie port after a project switch: the listener binds without `SO_REUSEADDR` (`_ExclusiveHTTPServer`), releases
  its socket and tray on Python shutdown (`atexit` + `unreal.register_python_shutdown_callback`), and a 30 s watchdog
  rebinds when the port stops answering. A port held by a leaked socket is skipped; the server scans 8765-8770.

### Changed

- `mcp_server.py` instructions announce v0.4.0 and the tool groups.
- README, `docs/setup.md`, `docs/troubleshooting.md`, `docs/architecture.md`, `docs/tools_reference.md` updated for the
  above; `requirements.txt` added; `tests/test_verse_tools_live.py` takes the `.verse` file as an argument or
  `VERSE_TEST_FILE`; `tests/test_mcp_server_offline.py` added (handshake + tool list, no UEFN).

### Known issues

- `get_editor_log` picks the newest `.log` in the project log folder, which is often the revision-control log
  (upstream PR #3 fixes it).

## 0.2.0 — 2026-03-20 (`3f65857`)

28 tools: status window, port discovery 8765-8770, heartbeat and metrics, `set_actor_properties`, `select_actors`,
`focus_selected`, `get_editor_log`, `get_project_info`, `shutdown`.

## 0.1.0 — 2026-03-20 (`0c1705f`)

Initial release: 22 tools (`ping`, `execute_python`, `get_log`, actors, assets, level, viewport camera).
