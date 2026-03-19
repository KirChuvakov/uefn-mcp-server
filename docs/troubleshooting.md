# Troubleshooting

## Connection Issues

### "UEFN listener is not running"

**Cause:** The MCP server cannot reach the HTTP listener inside UEFN.

**Fix:**
1. Make sure UEFN editor is open
2. Go to **Tools > Execute Python Script** and select `uefn_listener.py`
3. Check the Output Log (Window > Output Log) for `[MCP] Listener started on http://127.0.0.1:8765`
4. Verify with curl: `curl http://127.0.0.1:8765/`

### "Connection refused" after listener was running

**Cause:** The listener crashed or the editor was restarted.

**Fix:** Re-run the listener via **Tools > Execute Python Script**. If you want auto-start, set up `init_unreal.py` (see [Setup Guide](setup.md)).

### Port conflict

**Cause:** Port 8765 is already in use by another process.

**Fix:** The listener auto-detects free ports in range 8765-8770. Check which port it bound to in the Output Log. Then configure the MCP server to use the same port:

```bash
python mcp_server.py --port 8766
```

Or update `.mcp.json` accordingly.

To find what's using the port:
```bash
netstat -ano | findstr :8765
```

## Command Errors

### "Command timed out after 30s"

**Cause:** The command took too long to execute on the main thread, or the editor is frozen/busy.

**Possible reasons:**
- Editor is compiling shaders
- Editor is loading a large level
- The Python code in `execute_python` has an infinite loop
- A very large operation (e.g., listing millions of assets)

**Fix:**
- Wait for the editor to finish its current operation
- For long operations, break them into smaller batches
- Check the UEFN Output Log for errors

### "Unknown command: xyz"

**Cause:** The command name doesn't match any registered handler.

**Fix:** Use `ping` to see the list of available commands. Make sure listener and MCP server versions match.

### "Actor not found" / "Asset not found"

**Cause:** The path or label doesn't match any existing object.

**Fix:**
- Use `get_all_actors` to list actors and find the correct path/label
- Use `list_assets` to browse the content directory
- Actor labels are case-sensitive
- Asset paths must start with `/Game/` (or `/Engine/` for engine assets)

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
- `AttributeError`: The API method doesn't exist in UEFN (check `docs/uefn_api_availability.md`)
- `TypeError`: Wrong argument types (use `unreal.Vector`, `unreal.Rotator`, etc.)
- `RuntimeError`: Editor state doesn't allow the operation (e.g., saving during PIE)

### `print()` output not visible

**Cause:** By default, `print()` output goes to `stdout` which is captured and returned in the response.

**Fix:** Check the `stdout` field in the response. If you want it in the UE Output Log too, use:
```python
unreal.log("My message")
```

## MCP Server Issues

### Claude Code doesn't show UEFN tools

**Cause:** `.mcp.json` not found or MCP server failed to start.

**Fix:**
1. Verify `.mcp.json` exists in the project root
2. Check the path to `mcp_server.py` is correct and absolute
3. Verify `mcp` SDK is installed: `pip install mcp`
4. Test the server manually: `python mcp_server.py` (should hang waiting for stdio)
5. Restart Claude Code

### "ModuleNotFoundError: No module named 'mcp'"

**Cause:** MCP SDK not installed in the Python used by Claude Code.

**Fix:**
```bash
pip install mcp
```

Make sure you're installing for the same Python that `.mcp.json` references. If you have multiple Python versions:
```bash
python3 -m pip install mcp
```

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

**No.** The listener runs inside the editor process. When the editor closes, the listener dies. You need to restart it (or use `init_unreal.py` for auto-start).

### Multiple editor instances

Each editor instance needs its own listener on a different port. The auto-detect range (8765-8770) supports up to 6 simultaneous instances. Configure each MCP server connection with the correct port.
