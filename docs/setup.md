# Setup Guide

## Prerequisites

- UEFN editor with Python scripting enabled via **Project Settings**
- Python 3.10+ installed on your system (for the MCP server process)
- Claude Code CLI installed

## Step 0: Let Claude do the setup

Open Claude Code and ask: *"Help me set up UEFN MCP server"* — it will install dependencies, create config files, and walk you through the rest.

If you prefer to do it manually, follow the steps below.

## Step 1: Enable Python in UEFN

1. Open your project in UEFN
2. Go to **Project > Project Settings**
3. Search for **Python** and check the box for **Python Editor Script Plugin**

After this, you should see **Tools > Execute Python Script** in the menu bar.

## Step 2: Start the Listener

### Manual start (recommended for first use)

1. In UEFN, go to **Tools > Execute Python Script**
2. Navigate to and select `uefn_listener.py`
3. A **status window** will appear:

```
UEFN MCP Listener  v0.2.0
● Listener: Running
● MCP Server: Connecting...

Port      8765
Uptime    0m 05s
Requests  0
...
```

The window shows real-time status — you don't need to check the Output Log.
You can safely close the window; the listener continues running in the background.

### Auto-start on editor launch

Copy both files to your UEFN project's `Content/Python/` directory:

```bash
cp uefn_listener.py  <YourUEFNProject>/Content/Python/uefn_listener.py
cp init_unreal.py     <YourUEFNProject>/Content/Python/init_unreal.py
```

The listener will start automatically every time you open the project in UEFN.

## Step 3: Install MCP SDK

On your system (not inside UEFN):

```bash
pip install mcp
```

Verify:
```bash
python -c "from mcp.server.fastmcp import FastMCP; print('OK')"
```

## Step 4: Configure Claude Code

### Option A: Project-level config (recommended)

Create `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "uefn": {
      "command": "python",
      "args": ["/path/to/uefn-mcp-server/mcp_server.py"]
    }
  }
}
```

### Option B: Global config

Add to `~/.claude/settings.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "uefn": {
      "command": "python",
      "args": ["/path/to/uefn-mcp-server/mcp_server.py"]
    }
  }
}
```

### Custom port

If the default port 8765 is in use, you can specify a different port:

```json
{
  "mcpServers": {
    "uefn": {
      "command": "python",
      "args": ["/path/to/uefn-mcp-server/mcp_server.py", "--port", "8766"]
    }
  }
}
```

Or via environment variable:

```json
{
  "mcpServers": {
    "uefn": {
      "command": "python",
      "args": ["/path/to/uefn-mcp-server/mcp_server.py"],
      "env": { "UEFN_MCP_PORT": "8766" }
    }
  }
}
```

## Step 5: Restart Claude Code

Claude Code reads `.mcp.json` on startup. Start a new session:

```bash
claude
```

The UEFN MCP tools should now be available. Test with: "ping the UEFN editor".

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
