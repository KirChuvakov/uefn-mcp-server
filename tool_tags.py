"""The [experimental] tag for tools that need the user's confirmation before each call.

A tagged tool does something that is hard to undo or reaches outside the open level: runs
arbitrary code, deletes or renames assets, pushes to a live session, stops the listener, starts
UEFN or edits its settings. Its MCP title starts with "[experimental]", its annotations say
destructiveHint=true, and its description starts with EXPERIMENTAL_NOTE, so a client shows the tag
and an agent asks the user first.
"""

from mcp.types import ToolAnnotations

EXPERIMENTAL_NOTE = "[experimental] Ask the user to confirm before each call."


def experimental(title: str) -> dict:
    """Keyword arguments for @mcp.tool(...) that tag a tool as experimental."""
    return {"title": f"[experimental] {title}",
            "annotations": ToolAnnotations(title=f"[experimental] {title}", destructiveHint=True)}
