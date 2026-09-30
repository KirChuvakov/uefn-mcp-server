# Desktop control

`desktop_*` and `uefn_*` session tools (0.5.0, Windows) let an agent see the desktop and drive the UEFN / Fortnite UI
where editor Python cannot reach:

- the **crash report dialog** after UEFN crashes;
- the **HUB** (project browser) that UEFN shows at startup when "Load on Startup" is "Home Panel";
- **Launch Session** / **Push Changes** (no Python API; `verse_push` covers Push Verse Changes while a session runs);
- **modal dialogs** that block the editor (for example "Overwrite Existing Object", which makes `verse_compile` return
  `ok: false` with no build activity);
- the **Fortnite client** during a play session (captures at fixed moments for 1:1 references).

Everything that editor Python can do stays with `execute_python` and the structured editor tools: they are faster,
deterministic and work while the UEFN window is in the background. The desktop tools run inside the MCP server process
(pure `ctypes`, no extra dependency); they need neither UEFN nor the listener.

## Which tools drive the desktop

| Session | Use |
|---|---|
| Claude Code inside the **Claude Desktop app** with computer use turned on (Settings > General > Computer use; Pro or Max plan) | The official `mcp__computer-use__*` tools. Call `request_access` for the UEFN editor, the Fortnite client and the crash reporter first; approval is per app and per session; browsers are view-only, terminals and IDEs click-only, other apps full control. |
| Claude Code **CLI** on Windows (terminal), background agents, teammates' machines | This server's `desktop_*` tools. The CLI's own computer-use server is macOS-only. |

The recipes below are the same for both. **One driver at a time**: one agent drives a given UEFN editor (desktop input
or listener calls); never click while another agent runs editor commands, and never while the owner is using the
machine for something else unless they asked for it.

## Tools

| Tool | Acting | Purpose |
|---|---|---|
| `desktop_list_windows` | no | Top-level windows (z-order) with process, class, rect, monitor, flags, `input_allowed`; monitors, virtual desktop, cursor, kill-switch state, allowlist, DPI awareness |
| `desktop_screenshot` | no | PNG of a monitor (0 = all), a window or a region, downscaled for the model by default; returns `path`, `shot_id`, the pixel mapping and where the cursor is; `count` / `interval_sec` for bursts |
| `desktop_focus_window` | yes | Restore and bring a window to the foreground, verified (foreground-lock workarounds) |
| `desktop_click` | yes | Click / double-click / right-click, optional modifiers, in screen, monitor, window, client or screenshot pixels |
| `desktop_move` | yes | Hover |
| `desktop_drag` | yes | Press, move in steps, release; aborts if the user moves the mouse |
| `desktop_scroll` | yes | Wheel notches, vertical or horizontal |
| `desktop_type` | yes | Unicode text (layout-independent) into the focused control |
| `desktop_key` | yes | Keys and combos: `f5`, `ctrl+s`, `alt+f4`, `enter`, `esc` (explicit key-name table) |
| `desktop_close_window` | yes | Graceful `WM_CLOSE` by process / title / class |
| `desktop_wait_for_window` | no | Wait until a window appears or disappears |
| `uefn_status` | no | UEFN state from the editor log, windows and ports, with next-step hints |
| `uefn_launch_project` | yes | Start UEFN if needed, open a project (HUB aware), wait for the listener |
| `uefn_set_load_on_startup` | yes | "Load on Startup" = Home Panel or Most Recent Project; only on the owner's request, UEFN closed |

## Safety rails

Input tools (click, move, drag, scroll, type, key) and the other acting tools (focus, close, launch) obey:

1. **Allowlist of target processes.** Default: `UnrealEditorFortnite-Win64-Shipping` (the editor, including its HUB),
   `CrashReportClientEditor*`, `EpicGamesLauncher`, `FortniteClient-Win64-Shipping*`, `FortniteLauncher`. Names are
   matched case-insensitively without `.exe`; `*` / `?` are globs. `UEFN_DESKTOP_ALLOW` replaces the list
   (`UEFN_DESKTOP_ALLOW=UnrealEditorFortnite-Win64-Shipping,CrashReportClientEditor*`); a leading `+` extends it
   (`+CrashReportClient` adds the Fortnite client's crash reporter); `*` alone disables the check, which is
   **dangerous**: any window, including a terminal or a browser, can then receive input. Set it in the `env` of the
   `uefn` server in `.mcp.json`.
2. **Named target, focused, re-checked.** Every input call names its target (`process` / `title`, else the
   screenshot's window, else the window under the point, else the foreground window), brings it to the foreground and
   checks, right before each input batch, that the foreground window belongs to an allowed process and to the target's
   process. Clicks also check the window under the point. Anything else is refused with a clear error; nothing is sent.
3. **Kill switch.** If the mouse cursor is within 5 px of the top-left corner of any monitor when an acting call
   starts, the call is refused. To stop a runaway agent, park the mouse in a top-left corner and leave it there; every
   later call is refused until it moves away. Drags and typing re-check between steps; points inside that corner are
   never clicked.
4. **UIPI.** A target running elevated would silently drop injected input; such targets are refused.
5. **Editor-quitting guards.** `desktop_close_window` refuses a top-level UEFN editor window unless
   `allow_editor_main=true`, and `desktop_key` refuses `alt+f4` on UEFN unless `allow_editor_close=true`: pass them only
   when the owner asked to close the editor. Never kill a healthy editor (unsaved scene work is lost).
6. **Audit log.** One JSON line per acting call (time, tool, outcome ok / refused / error, target process and title,
   coordinates or keys, the cursor at start, duration) in `%TEMP%\uefn-mcp\desktop_control.log` (rotating, 1 MB x 4;
   `UEFN_DESKTOP_LOG` overrides). Typed text is logged only when it does not look sensitive: it is redacted to its
   length for `sensitive=true`, for Win32 password controls, for sign-in / password windows, for the Epic launcher and
   for secret-looking strings.
7. `UEFN_DESKTOP_DISABLE=1` turns every acting call off.

**Focus.** Windows only lets the process that received the last input take the foreground (the foreground lock), and
the MCP server never receives input. `desktop_focus_window` (and every input tool before sending) therefore tries, in
order and verifying after each step: restore if minimized; `SetForegroundWindow`; `AttachThreadInput` to the
foreground and target threads + `BringWindowToTop` / `SetForegroundWindow` / `SetFocus`; `SwitchToThisWindow`; last, an
ALT tap (one injected ALT press makes this process the last input source, which lifts the lock). The result names the
method that worked; if none did, the call is refused.

Screenshots and window lists are read-only and unrestricted. They can show private windows: capture the target window
(`process=...`) rather than a whole monitor when that is enough.

## Coordinates

- The server process is **per-monitor DPI aware (V2)**, so every coordinate is a **physical pixel of the virtual
  desktop**: the space of GDI / `mss` captures, `GetWindowRect`, `SetCursorPos` and `GetCursorPos`. Monitors left of or
  above the primary have **negative** origins. Display scaling (125 %, 150 %) does not change anything: physical pixels
  stay physical.
- Monitor numbers are in `EnumDisplayMonitors` order, the same as `mss.monitors[1:]`; `0` is the whole virtual desktop.
- `desktop_screenshot` downscales to at most 1568 px on the long edge and about 1.19 megapixels (what the model reads
  without further resizing) and returns the mapping `screen = origin + floor((image + 0.5) * scale)` per axis. The
  mapping is also written next to the PNG (`<png>.json`). Pass the PNG path or `shot_id` as `shot` to `desktop_click`,
  `desktop_move`, `desktop_drag` or `desktop_scroll` and give pixel coordinates read off the image. For small text use
  `region=[x, y, width, height]` (screen pixels) or `full_resolution=true`.
- If the captured window moved since the screenshot, clicks are shifted by the same amount (`window_moved_by`).
- Other spaces: `relative_to="monitor"` (from monitor N's top-left), `"monitor_dip"` (monitor-relative 96-dpi units),
  `"window"` (from the window's top-left), `"client"` (from its client area).

## UEFN facts the tools rely on (verified 2026-09-30, UEFN 42.20)

- The editor window is class `UnrealWindow`, title **"Unreal Editor for Fortnite" with or without a project**: the title
  never names the project, so readiness comes from the editor log.
- **No UI Automation.** UEFN's Slate UI exposes no UIA / MSAA tree: the main window is a bare `UnrealWindow` with zero
  children, and the engine binaries do not reference the UIA provider API. A HUB tile cannot be found by name; picking
  it needs a screenshot and a click.
- Editor log `%LOCALAPPDATA%\UnrealEditorFortnite\Saved\Logs\UnrealEditorFortnite.log` (new file per run; first line
  `Log file open, <local time>`; line timestamps in UTC). Markers: `LogInit: Display: Engine is initialized`,
  `LogValkyrie: Searching for projects under` (the HUB is populated), `LogValkyrieProjectBrowser: Selected Project
  (OnMouseClick)` (a tile was clicked), `LogValkyrie: Opening project '<path>'`, `LogValkyrie: Display: Successfully
  opened project '<path>'`, `Python enabled via IPythonScriptPlugin::ForceEnablePythonAtRuntime`, `[MCP] Auto-started
  on port <p>`, `Started the ModelContextProtocol server on port 8000` (Epic's Toolsets MCP).
- Port 1962 (Verse workflow) is already open on the HUB; it does not mean "project open".
- **Load on Startup** (Editor Preferences > Loading & Saving; also a selector on the HUB screen):
  `ValkyrieLoadAtStartupMostRecentProject` = `HomeScreen` ("Home Panel", the HUB) or `LastProject` ("Most Recent
  Project") in `[/Script/ValkyrieEditor.ValkyrieEditorConfig]` of
  `%LOCALAPPDATA%\UnrealEditorFortnite\Saved\Config\WindowsEditor\EditorPerProjectUserSettings.ini`, next to
  `LastProjectFileName` and `EnablePythonLocallyPerProject=((<projectId>, True), ...)` (the per-project "Enable Python
  Locally" switch; `projectId` is `bindings.projectId` in the `.uefnproject`). UEFN rewrites this file on exit: edit it
  only while UEFN is closed.
- The Epic launcher starts UEFN as app `Fortnite_Studio` (manifest `%ProgramData%\Epic\EpicGamesLauncher\Data\Manifests\*.item`,
  `LaunchExecutable` `FortniteGame/Binaries/Win64/UnrealEditorFortnite-Win64-Shipping.exe`); the URI
  `com.epicgames.launcher://apps/<CatalogNamespace>%3A<CatalogItemId>%3AFortnite_Studio?action=launch&silent=true`
  passes the sign-in arguments. Passing a project path to the editor exe does not skip the HUB.
- `CrashReportClientEditor` also runs **next to a healthy editor** as a monitor (`-MONITOR=<editor pid>`) with no visible
  window. A **visible** window of that process is the crash dialog.

## Recipes

### Close the crash report dialog

```
uefn_status()                                          -> state "crash_dialog"
desktop_close_window(process="CrashReportClientEditor")  -> WM_CLOSE, waits, reports what is still open
uefn_launch_project(project="<Island>")                   -> relaunch (next recipe)
```

If the dialog stays open (it may ask to send the report), `desktop_screenshot(process="CrashReportClientEditor")` and
click its close / "Don't send" button with `desktop_click(x, y, shot=<path>)`. Never stop the crash reporter process
while the editor is still alive: it is the healthy editor's monitor.

### Open a project (HUB screen or not)

```
uefn_launch_project(project="C:/.../MyIsland.uefnproject")    # or the folder, or the project name
```

- UEFN not running: it starts through the Epic launcher URI (else the exe; `launch_via="exe"` forces it). If "Load on
  Startup" is already Most Recent Project and the last project is another one, `LastProjectFileName` is pointed at this
  project first (UEFN closed only; reported under `actions`). The preference itself changes only with
  `enable_load_last_project=true` (explicit opt-in).
- Then it waits for the project load (log markers) and the listener (8765-8770) and returns `status="ready"`, or
  `project_open_no_listener` with the reason (Python off for the project, hook missing, autostart error, listener busy).
- **HUB shown** (`status="hub"`): the result has a screenshot of the UEFN window (focused first if it was minimized or
  covered) and its mapping. Read the PNG, find the tile titled like the project (`project.title`, under Recent Projects
  or My Projects), then:

  ```
  desktop_click(x, y, shot="<screenshot path>")                      # the tile
  desktop_screenshot(process="UnrealEditorFortnite", class_name="UnrealWindow")
  desktop_click(x, y, shot="<new path>")                             # Launch (or double-click the tile)
  uefn_launch_project(project="<same>", launch=False)                  # wait for the load and the listener
  ```

  Leave the HUB's "Load on Startup" selector alone unless the owner asked.
- Other results: `crash_dialog`, `other_project_open` (never kill a healthy editor: switch through File > Open Project
  with the desktop tools, or ask the owner), `open_failed`, `timeout` (call again with `launch=False`).

### Launch Session / Push Changes

- Push Verse Changes while a session runs: `verse_push` (no desktop input needed).
- Launch Session: `desktop_screenshot(process="UnrealEditorFortnite")`, find the **Launch Session** toolbar button, then
  `desktop_click(x, y, shot=<path>)`. With a keyboard shortcut bound to it (Editor Preferences > Keyboard Shortcuts,
  search "Launch Session"): `desktop_key(keys="<shortcut>", process="UnrealEditorFortnite")`. Confirm with a new
  screenshot or `desktop_wait_for_window(process="FortniteClient*")`.

### Capture the Fortnite client at fixed moments

```
desktop_wait_for_window(process="FortniteClient-Win64-Shipping*", timeout_sec=180)
desktop_focus_window(process="FortniteClient-Win64-Shipping*")
desktop_screenshot(process="FortniteClient-Win64-Shipping*", full_resolution=true, count=6, interval_sec=5,
                   output_path="<refs folder>/lobby.png")          # lobby_001.png ... lobby_006.png
```

Captures read the screen, so the client must be visible and in windowed or borderless mode (exclusive fullscreen can
capture black). In gameplay the client clips the cursor; menus accept clicks.

### Answer a modal dialog

`verse_compile` returns `ok: false` with no build activity, or `uefn_status` lists `other_windows`: take
`desktop_screenshot(process="UnrealEditorFortnite")` (the dialog is usually the foreground UEFN window), read it and
click the right button, or `desktop_key(keys="esc")` / `"enter"`.

## Tests

| Command | What it does |
|---|---|
| `python tests/test_desktop_control_offline.py` | Key names and combos, allowlist parsing and matching, coordinate mapping with synthetic layouts (negative origins, 125 % / 150 %), SendInput normalization, kill switch, redaction, PNG encoder, audit log |
| `python tests/test_desktop_control_offline.py --live` | + read-only live smoke: monitors vs `mss`, windows, cursor, monitor-1 screenshots. Sends no input |
| `python tests/test_uefn_session_offline.py` | INI editing (byte-exact), log state machine, state classification, install discovery, hook check |
| `python tests/test_desktop_input_live.py --yes-send-input` | **Moves the mouse and types**, only into its own sandbox Tk window (allowlist narrowed to that interpreter): exact click pixel, double click, drag, wheel, Unicode typing, combos, click in screenshot pixels, allowlist refusal, kill switch, WM_CLOSE. Run when nobody uses the machine |

### Pending live tests

Written while another agent was driving UEFN, so no input was sent to UEFN and no editor was launched or closed:

1. `tests/test_desktop_input_live.py --yes-send-input` (sandbox input path).
2. HUB flow: close UEFN; `uefn_set_load_on_startup("HomeScreen")`; `uefn_launch_project(project=...)` must return
   `status="hub"` with a screenshot; click the tile and Launch; `uefn_launch_project(..., launch=False)` must return
   `ready`; restore the owner's setting with `uefn_set_load_on_startup("LastProject")`.
3. Retargeting: with Most Recent Project and UEFN closed, `uefn_launch_project` on a project other than the last one
   must open that project (checks that UEFN reads `LastProjectFileName` for the auto-load).
4. Crash dialog: after a real crash, `desktop_close_window(process="CrashReportClientEditor")` closes the dialog.
5. Foreground lock: `desktop_focus_window(process="UnrealEditorFortnite")` from a CLI session while a terminal has
   focus; report which `method` won.
