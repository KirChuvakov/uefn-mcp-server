# Setup Guide

## Prerequisites

- UEFN editor with Python scripting enabled via **Project Settings**
- Python 3.10+ installed on your system (for the MCP server process)
- Claude Code CLI installed

## Step 1: Enable Python in UEFN

1. Open your project in UEFN
2. Go to **Edit > Project Settings**
3. Search for **Python** and check the boxes for **Python Editor Script Plugin**, **Editor Scripting Utilities**, and **Python Developer Mode**

After this, you should see **Tools > Execute Python Script** in the menu bar.

## Step 2: Start the Listener

### Manual start (recommended for first use)

1. In UEFN, go to **Tools > Execute Python Script**
2. Navigate to and select `uefn_listener.py`
3. Check the **Output Log** (Window > Output Log) for confirmation:

```
[MCP] Listener started on http://127.0.0.1:8765
[MCP] Registered 22 command handlers
```

You can also verify from a terminal:
```bash
curl http://127.0.0.1:8765/
```

Expected response:
```json
{
  "status": "ok",
  "port": 8765,
  "commands": ["ping", "get_log", "execute_python", ...]
}
```

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

### Restart the listener

Use **Tools > Execute Python Script** and run `uefn_listener.py` again. If already running, it will report the current port.

To force restart, create a small script:
```python
import uefn_listener
uefn_listener.restart_listener()
```

### Check status

```bash
curl http://127.0.0.1:8765/
```

Or from Claude Code, use the `ping` tool.
