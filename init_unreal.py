"""Auto-start MCP listener when UEFN editor opens.

Place this file (or a copy) in your UEFN project's Content/Python/ directory.
It will be executed automatically when the editor starts.

Also copy uefn_listener.py to the same directory so it can be imported.
"""

import unreal


def _start_mcp():
    """Import and start the MCP listener."""
    try:
        import uefn_listener

        if uefn_listener._server is None:
            port = uefn_listener.start_listener()
            unreal.log(f"[MCP] Auto-started on port {port}")
        else:
            unreal.log(f"[MCP] Already running on port {uefn_listener._bound_port}")
    except ImportError:
        unreal.log_warning(
            "[MCP] uefn_listener.py not found in Python path. "
            "Copy it to Content/Python/ or add its directory to sys.path."
        )
    except Exception as e:
        unreal.log_error(f"[MCP] Auto-start failed: {e}")


_start_mcp()
