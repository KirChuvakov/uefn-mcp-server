# Changelog

Release versions of the repo. The listener also reports a protocol version in `ping` (`PROTOCOL_VERSION` in
`uefn_listener.py`); it changes only when the listener/server wire format changes.

## 0.5.0 (unreleased)

121 MCP tools (14 new), all host-side: they run in the MCP server process and need neither UEFN nor the listener.
Listener protocol unchanged (0.3.2).

### Added — desktop control (`desktop_control.py`, 11 tools, Windows, pure `ctypes`)

- `desktop_list_windows`, `desktop_screenshot`, `desktop_focus_window`, `desktop_click`, `desktop_move`,
  `desktop_drag`, `desktop_scroll`, `desktop_type`, `desktop_key`, `desktop_close_window`, `desktop_wait_for_window`:
  for UI that editor Python cannot reach (crash report dialog, the HUB, Launch Session, modal dialogs, the Fortnite
  client).
- The server process becomes per-monitor DPI aware (V2) at import: every coordinate is a physical pixel of the virtual
  desktop (negative origins included), matching `mss` / GDI captures. Screenshots are GDI captures written as PNG with
  `zlib` (no `mss` needed), downscaled for the model by default, with a pixel mapping returned and stored next to the
  PNG; `desktop_click(shot=...)` clicks in image pixels. Bursts (`count`, `interval_sec`) for session captures.
- Input through `SendInput`: absolute moves aimed at pixel centers (verified with `GetCursorPos`), buttons, wheel,
  Unicode typing (`KEYEVENTF_UNICODE`), virtual keys with scan codes and extended flags from an explicit key-name table.
  Focus with the foreground-lock workarounds (`SetForegroundWindow`, `AttachThreadInput`, `SwitchToThisWindow`, ALT
  tap), verified afterwards.
- Safety rails: process allowlist (UEFN editor, `CrashReportClientEditor*`, `EpicGamesLauncher`,
  `FortniteClient-Win64-Shipping*`, `FortniteLauncher`; `UEFN_DESKTOP_ALLOW` replaces / `+` extends / `*` disables);
  every input call resolves and focuses its target and re-checks the foreground right before sending; clicks check the
  window under the point; kill switch (cursor within 5 px of a monitor's top-left corner); elevated targets refused
  (UIPI); closing the UEFN editor window (`WM_CLOSE`, `alt+f4`) needs an explicit flag; `UEFN_DESKTOP_DISABLE`; JSON-lines
  audit log `%TEMP%\uefn-mcp\desktop_control.log` (rotating) with password-like text redacted.

### Added — UEFN session (`uefn_session.py`, 3 tools)

- `uefn_status`: state (`not_running`, `starting`, `hub`, `opening`, `project_open`, `other_project_open`,
  `open_failed`, `crash_dialog`) from the editor log markers (the window title never names the project), windows,
  ports (listener 8765-8770 incl. "busy", 1962, 8000), Python-on / listener-autostart / Toolsets markers for the open
  project, "Load on Startup", the autostart hook, the install; next-step hints.
- `uefn_launch_project`: resolves a project (path, folder or name), starts UEFN through the Epic launcher URI
  (`Fortnite_Studio`, with sign-in) or the editor exe, waits for the load or the HUB (returns a screenshot + mapping
  for picking the tile), then waits for the listener and explains a missing one. With "Load on Startup" = Most Recent
  Project it points `LastProjectFileName` at the requested project first (UEFN closed only).
- `uefn_set_load_on_startup`: `ValkyrieLoadAtStartupMostRecentProject` = `HomeScreen` | `LastProject` in
  `EditorPerProjectUserSettings.ini` (`[/Script/ValkyrieEditor.ValkyrieEditorConfig]`), byte-exact edit with a
  backup, refused while UEFN runs. Opt-in only.
- Verified facts behind them: no UI Automation tree in UEFN's Slate UI (vision needed on the HUB); log markers; the
  settings keys; the launcher manifest and URI.

### Added — onboarding

- `setup.ps1`: idempotent machine setup (`-DryRun`): real Python 3.10+, `requirements.txt`, `UEFN_MCP_PATH`,
  `ensure_mcp_hook.ps1` (+ `-ScheduleHook`), UEFN report (install, "Load on Startup", Python enabled for the project,
  hook), `-WithTray`, `-WithMss`, `-EnableLoadLastProject` (opt-in), `-RegisterClaude`, and what is left to do.
- `uefn_session.py` doubles as a CLI (`status`, `find-install`, `set-load-on-startup`) used by `setup.ps1`.

### Tests and docs

- `tests/test_desktop_control_offline.py` (+ `--live` read-only smoke), `tests/test_uefn_session_offline.py`,
  `tests/test_desktop_input_live.py` (opt-in, sandbox window only); `tests/test_mcp_server_offline.py` expects 121
  tools including every new one.
- `docs/desktop_control.md` (rails, coordinates, UEFN facts, recipes, pending live tests); README, `docs/setup.md`,
  `docs/tools_reference.md`, `docs/architecture.md`, `docs/troubleshooting.md` updated.

### Pending live verification

The HUB flow, `LastProjectFileName` retargeting, closing a real crash dialog and the input path were not exercised
against UEFN (another agent was driving the editor); see `docs/desktop_control.md`, "Pending live tests".

## 0.4.0 — 2026-09-30

The EndoWorlds fork merged into the base repo. Compared with 0.2.0 (`3f65857`): 107 MCP tools instead of 28
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
