"""Verse workflow-socket client for the UEFN MCP server.

Talks to the VerseWorkflowServer that the open UEFN editor exposes on
TCP 127.0.0.1:1962 (LSP-style Content-Length framing) — the same channel
the epicgames.verse VS Code extension uses for "Build Verse Code" /
"Push Verse Changes".

Protocol:
    Request : {"seq":N,"type":1,"command":<cmd>,"params":<params>}
    Response: {"seq":N,"type":2,"command":<cmd>,"result":...} (or "error":...)
    Server notifications (type:0): logMessage, updateBuildState, canPushVerseChanges
        BuildState: 0=Success 1=Warnings 2=Errors 3=Building 4=NoBuild
        Severity (logMessage): 1=Error 2=Warning 3=Info 4=Log

Exposed MCP tools (see register()): verse_compile, verse_push.
"""

import json
import os
import socket
import time

from tool_tags import experimental

WORKFLOW_HOST = os.environ.get("VERSE_WORKFLOW_HOST", "127.0.0.1")
WORKFLOW_PORT = int(os.environ.get("VERSE_WORKFLOW_PORT", "1962"))

SEVERITY = {1: "Error", 2: "Warning", 3: "Info", 4: "Log"}
BUILD_STATE = {0: "Success", 1: "Warnings", 2: "Errors", 3: "Building", 4: "NoBuild"}

COMPILE_TIMEOUT = 300.0
PUSH_TIMEOUT = 240.0
# Info/Log messages are capped in the response; errors/warnings never are.
MAX_INFO_MESSAGES = 40


def _frame(obj: dict) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body


def _read_frames(sock: socket.socket, timeout: float):
    sock.settimeout(timeout)
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        while True:
            sep = buf.find(b"\r\n\r\n")
            if sep == -1:
                break
            header = buf[:sep].decode("utf-8", "replace")
            clen = None
            for line in header.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    clen = int(line.split(":", 1)[1].strip())
            if clen is None:
                buf = buf[sep + 4:]
                continue
            if len(buf) < sep + 4 + clen:
                break
            body = buf[sep + 4:sep + 4 + clen]
            buf = buf[sep + 4 + clen:]
            try:
                yield json.loads(body.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                pass
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            return
        if not chunk:
            return
        buf += chunk


def _run_command(command: str, params, timeout: float) -> dict:
    """Send one workflow command, collect notifications until the response.

    Returns {"ok": bool, "result"/"error": ..., "build_state": str|None,
             "errors": [...], "warnings": [...], "info": [...], "truncated_info": int}.
    Raises ConnectionError if the workflow socket is unreachable (UEFN closed).
    """
    seq = 1
    msg = {"seq": seq, "type": 1, "command": command, "params": params}
    out = {
        "ok": False,
        "build_state": None,
        "errors": [],
        "warnings": [],
        "info": [],
        "truncated_info": 0,
    }
    try:
        conn = socket.create_connection((WORKFLOW_HOST, WORKFLOW_PORT), timeout=10)
    except OSError as e:
        raise ConnectionError(
            f"VerseWorkflowServer not reachable on {WORKFLOW_HOST}:{WORKFLOW_PORT}. "
            "Is UEFN running with the project open?"
        ) from e
    with conn:
        conn.sendall(_frame(msg))
        for resp in _read_frames(conn, timeout):
            rtype = resp.get("type")
            if rtype == 2 and resp.get("seq") == seq:
                if "result" in resp:
                    out["ok"] = True
                    out["result"] = resp["result"]
                else:
                    out["error"] = resp.get("error")
                return out
            cmd, p = resp.get("command"), resp.get("params")
            if cmd == "logMessage" and isinstance(p, dict):
                sev = p.get("type")
                text = p.get("message", "")
                if sev == 1:
                    out["errors"].append(text)
                elif sev == 2:
                    out["warnings"].append(text)
                else:
                    if len(out["info"]) < MAX_INFO_MESSAGES:
                        out["info"].append(text)
                    else:
                        out["truncated_info"] += 1
            elif cmd == "updateBuildState":
                out["build_state"] = BUILD_STATE.get(p, str(p))
    out["error"] = f"no response from workflow server within {timeout}s"
    return out


def compile_project() -> dict:
    """Compile the Verse project; returns structured build results."""
    out = _run_command("compileProject", {}, COMPILE_TIMEOUT)
    out["summary"] = (
        f"build_state={out.get('build_state')} "
        f"errors={len(out['errors'])} warnings={len(out['warnings'])}"
    )
    return out


def push_changes(verse_only: bool = True) -> dict:
    """Push changes to the live session (only works while a session is running)."""
    return _run_command("pushChanges", verse_only, PUSH_TIMEOUT)


# On connect, the VerseWorkflowServer immediately pushes its current
# updateBuildState and canPushVerseChanges notifications (verified empirically).
# We just listen briefly and report them — no command is sent, so this NEVER
# triggers a compile. ~STATUS_LISTEN seconds.
STATUS_LISTEN = 2.5


def workflow_status() -> dict:
    """Read current build state & push-availability WITHOUT compiling.

    Returns {"reachable": True, "build_state": str|None, "can_push": bool|None,
             "summary": str}. Raises ConnectionError if UEFN/workflow socket is down.
    """
    try:
        conn = socket.create_connection((WORKFLOW_HOST, WORKFLOW_PORT), timeout=10)
    except OSError as e:
        raise ConnectionError(
            f"VerseWorkflowServer not reachable on {WORKFLOW_HOST}:{WORKFLOW_PORT}. "
            "Is UEFN running with the project open?"
        ) from e
    out = {"reachable": True, "build_state": None, "can_push": None}
    with conn:
        for resp in _read_frames(conn, STATUS_LISTEN):
            cmd, p = resp.get("command"), resp.get("params")
            if cmd == "updateBuildState":
                out["build_state"] = BUILD_STATE.get(p, str(p))
            elif cmd == "canPushVerseChanges":
                out["can_push"] = bool(p)
    out["summary"] = f"build_state={out['build_state']} can_push={out['can_push']}"
    return out


def register(mcp) -> None:
    """Attach the workflow tools to a FastMCP instance."""

    @mcp.tool()
    def verse_compile() -> str:
        """Compile the Verse project in the open UEFN editor and return errors/warnings.

        Talks directly to the editor's VerseWorkflowServer (TCP 1962) — the same
        channel as UEFN's "Build Verse Code" button. UEFN must be running with
        the project open. Returns JSON with build_state (Success/Warnings/Errors),
        full errors[] and warnings[] lists, and a capped info[] log.
        """
        return json.dumps(compile_project(), indent=2, ensure_ascii=False)

    @mcp.tool()
    def verse_status() -> str:
        """Report current Verse build state & push-availability WITHOUT compiling.

        Connects to the editor's VerseWorkflowServer (TCP 1962) and reads the
        state it pushes on connect: build_state (Success/Warnings/Errors/Building/
        NoBuild) and can_push (whether a live session/playtest is running). Cheap
        (~2.5s), never triggers a compile. Use to check "is the build green" or
        "can I push right now" before deciding to run verse_compile / verse_push.
        """
        return json.dumps(workflow_status(), indent=2, ensure_ascii=False)

    @mcp.tool(**experimental("Push changes to live session"))
    def verse_push(verse_only: bool = True) -> str:
        """[experimental] Ask the user to confirm before each call. Push changes to the live UEFN session (equivalent of "Push Changes").

        Only works while a session/playtest is running (the editor must have
        announced canPushVerseChanges=true). verse_only=True pushes only Verse
        code; False pushes all content changes. NOTE: new texture assets still
        require a session relaunch — push only updates code.
        """
        return json.dumps(push_changes(verse_only), indent=2, ensure_ascii=False)
