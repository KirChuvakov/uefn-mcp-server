# Setup Guide

## Prerequisites

- UEFN editor with Python scripting enabled via **Project Settings**
- Python 3.10+ installed on your system (for the MCP server process) — a real install, not the Microsoft Store alias
- Claude Code CLI installed
- Optional: the `epicgames.verse` VS Code extension (for the Verse navigation tools)

## Step 0: `setup.ps1` (the onboarding step)

From the clone (idempotent; `-DryRun` shows what it would do and changes nothing):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -DryRun
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -ScheduleHook -Project "<path>\<Island>.uefnproject"
```

It finds a real Python 3.10+ (never the Microsoft Store alias), installs `requirements.txt`, sets the user variable
`UEFN_MCP_PATH`, runs `ensure_mcp_hook.ps1` (listener autostart; `-ScheduleHook` adds the hourly task that re-adds the
hook after Fortnite updates), reports the UEFN install, "Load on Startup" and whether Python is enabled for the
project, and prints what is left. Options: `-WithTray`, `-WithMss`, `-EnableLoadLastProject` (explicit opt-in: UEFN
reopens the last project at startup instead of showing the HUB; UEFN must be closed), `-RegisterClaude`,
`-Python <python.exe>`, `-SkipPip`. Run it from an elevated PowerShell once if `Program Files` is write-protected (the
hook step says so).

What it cannot do for you: enable Python in each project (Step 2) and approve the server in Claude Code (Step 5).
Steps 1-6 below are the manual equivalent. Or ask Claude Code: *"Help me set up UEFN MCP server"*.

## Step 1: Clone and set `UEFN_MCP_PATH`

```powershell
git clone https://github.com/EndoWorldsHub/uefn-mcp-server
[Environment]::SetEnvironmentVariable('UEFN_MCP_PATH', (Resolve-Path .\uefn-mcp-server).Path, 'User')
```

The clone can live anywhere. Project configs refer to it only through `UEFN_MCP_PATH`, so they contain no
machine-specific path. Restart terminals and Claude Code afterwards so they see the variable.

## Step 2: Enable Python in UEFN

1. Open your project in UEFN
2. Go to **Project > Project Settings**
3. Search for **Python** and check the box for **Python Editor Script Plugin**

After this, you should see **Tools > Execute Python Script** in the menu bar. The setting is stored per project and
per user (`EnablePythonLocallyPerProject` in UEFN's `EditorPerProjectUserSettings.ini`); a project without it never
starts Python, so the listener cannot autostart there.

## Step 3: Start the Listener

### Manual start (always works)

1. In UEFN, go to **Tools > Execute Python Script**
2. Navigate to and select `uefn_listener.py` in the clone
3. A **status window** will appear:

```
UEFN MCP Listener  v0.3.3
● Listener: Running
● MCP Server: Connecting...

Port      8765
Uptime    0m 05s
Requests  0
...
```

The window shows real-time status — you don't need to check the Output Log. Closing the window hides it; the
listener keeps running, and the tray icon (if `vendor/` is populated, see the README) brings the window back.

### Auto-start on project open

UEFN runs `init_unreal.py` only from a fixed whitelist of engine plugin folders. Copying files into the project's
`Content/Python/` does not work, and `.py` files inside a UEFN project make session upload fail with
`[ContainsPythonData]`. Instead, run the hook installer once:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:UEFN_MCP_PATH\ensure_mcp_hook.ps1"
```

It appends a marked hook to Epic's `EditorToolset` `init_unreal.py` that runs this repo's `init_unreal.py` at every
project open. Fortnite updates remove the hook again: register the script as an hourly scheduled task (README,
"Auto-start"). Check the Output Log after a project opens for `[MCP] Auto-started on port 8765`.

## Step 4: Install MCP SDK

On your system (not inside UEFN):

```bash
pip install -r requirements.txt
```

Verify:
```bash
python -c "from mcp.server.fastmcp import FastMCP; print('OK')"
```

## Step 5: Configure Claude Code

### Option A: Project-level config

Create `.mcp.json` in your project root. Claude Code expands `${VAR}` and `${VAR:-default}` in it:

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

`unreal-mcp` is Epic's official server (UEFN with Toolsets enabled). Project-scope servers need a one-time approval
in Claude Code (`/mcp`).

### Option B: User-level registration

Works in every project and needs no approval:

```powershell
claude mcp add uefn -s user -- python "$env:UEFN_MCP_PATH\mcp_server.py"
```

If `python` resolves to the Microsoft Store alias, pass the full path of a real `python.exe` instead.

### Custom port

If the default port 8765 is in use, you can specify a different port:

```json
{
  "mcpServers": {
    "uefn": {
      "command": "python",
      "args": ["${UEFN_MCP_PATH}/mcp_server.py", "--port", "8766"]
    }
  }
}
```

Or via environment variable (first port of the 8765-8770 scan):

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

## Step 6: Restart Claude Code

Claude Code reads MCP servers on startup. Start a new session:

```bash
claude
claude mcp list    # expect: uefn ... Connected
```

The UEFN MCP tools should now be available. Test with: "ping the UEFN editor". `verse_compile` and `verse_status`
work as soon as UEFN has the project open, even before the listener runs.

## Listener Management

### Using the status window

The status window provides **Stop**, **Start**, and **Restart** buttons. When stopped, you can change the port number before starting again.

Status indicators:
- **Listener: Running** (green) — HTTP server is active
- **Listener: Stopped** (red) — HTTP server is not running
- **MCP Server: Connected** (green) — Claude Code is actively connected (heartbeat received)
- **MCP Server: Connecting...** (yellow) — listener just started, waiting for first heartbeat
- **MCP Server: Lost Xs ago** (gray) — Claude Code disconnected or was restarted

### Re-running the script

Running `uefn_listener.py` again via **Tools > Execute Python Script** is safe — it will cleanly replace the previous listener and open a new status window.

### Check status from Claude Code

Use the `ping` tool, or ask: *"Is the UEFN listener running?"*

### Shutdown from Claude Code

Use the `shutdown` tool to stop the listener remotely. The port is freed immediately.
