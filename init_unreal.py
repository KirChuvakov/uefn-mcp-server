"""Autostart entry point: start the MCP listener when UEFN brings Python up.

UEFN starts Python right after a project opens only when the project has
"Enable Python Locally" turned on, and then runs init_unreal.py only from a
fixed whitelist of engine plugin folders - not from this repo, not from
Documents/UnrealEngine/Python and not via UE_PYTHONPATH. ensure_mcp_hook.ps1
therefore appends a small hook to Epic's whitelisted EditorToolset
init_unreal.py that runs this file with runpy (README, "Auto-start").

This file puts its own folder on sys.path and imports uefn_listener, whose
bootstrap block stops any stale instance and binds the first free port in
8765-8770. Running it twice is harmless (Python caches the module).

Never copy .py files into a UEFN project: session upload rejects them with
[ContainsPythonData].
"""

import os
import sys

import unreal

# runpy.run_path() sets __file__; fall back to sys.path when it is missing.
_HERE = globals().get("__file__")
_MCP_DIR = os.path.dirname(os.path.abspath(_HERE)) if _HERE else ""


def _start_mcp():
    try:
        if _MCP_DIR and _MCP_DIR not in sys.path:
            sys.path.append(_MCP_DIR)
        # Importing the module runs its bottom bootstrap block, which
        # stops any stale instance and calls start_listener() (port 8765+).
        import uefn_listener  # noqa: F401

        port = getattr(unreal, "_mcp_bound_port", 0)
        if port:
            unreal.log(f"[MCP] Auto-started on port {port}")
        else:
            unreal.log_warning("[MCP] Import finished but listener is not bound")
    except Exception as e:
        unreal.log_error(f"[MCP] Auto-start failed: {e}")


_start_mcp()
