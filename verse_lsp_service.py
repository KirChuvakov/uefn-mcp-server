"""Persistent Verse LSP client for the UEFN MCP server.

Spawns the bundled verse-lsp.exe (the same language server VS Code uses) over
stdio and keeps it alive between tool calls, so repeat queries skip the cold
start (process launch + digest indexing + per-file ~5s semantic analysis).

The Verse LSP does NOT publish error diagnostics — compile errors come from
the workflow socket (see verse_workflow.py). The LSP is navigation only:
document symbols, hover, go-to-definition, workspace symbol search.

CRITICAL: the LSP only resolves symbols when initialized with the
UEFN-generated multi-root workspace (Content + Verse/Fortnite/UnrealEngine/
Assets digest folders). The .code-workspace is auto-discovered as the most
recently modified one under the UEFN VerseProject dir (UEFN keeps the open
project's workspace fresh), overridable via VERSE_WORKSPACE_FILE.

Exposed MCP tools (see register()): verse_symbols, verse_hover,
verse_definition, verse_find_symbol, verse_lsp_restart.
"""

import glob
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse

LSP_EXE_OVERRIDE = os.environ.get("VERSE_LSP_EXE", "")
WORKSPACE_FILE_OVERRIDE = os.environ.get("VERSE_WORKSPACE_FILE", "")

EXTENSIONS_GLOB = os.path.expanduser(
    r"~\.vscode\extensions\epicgames.verse-*\bin\Win64\verse-lsp.exe"
)
VERSE_PROJECT_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "UnrealEditorFortnite", "Saved", "VerseProject"
)

INIT_INGEST_WAIT = 3.0   # after initialized: let the server ingest digests
OPEN_ANALYSIS_WAIT = 5.0  # after didOpen: let the server analyze the buffer
REQUEST_TIMEOUT = 40.0

SYMBOL_KIND = {5: "class", 6: "method", 12: "function", 13: "var", 14: "const",
               22: "struct", 10: "enum", 11: "interface", 23: "module", 9: "namespace"}


def _log(msg: str) -> None:
    # stdout carries the MCP protocol — diagnostics go to stderr only
    print(f"[verse-lsp] {msg}", file=sys.stderr, flush=True)


def _to_uri(path: str) -> str:
    return "file:///" + urllib.parse.quote(os.path.abspath(path).replace("\\", "/"))


def _from_uri(uri: str) -> str:
    return urllib.parse.unquote(uri.replace("file:///", ""))


def discover_lsp_exe() -> str:
    if LSP_EXE_OVERRIDE:
        return LSP_EXE_OVERRIDE
    candidates = glob.glob(EXTENSIONS_GLOB)
    if not candidates:
        raise FileNotFoundError(
            f"verse-lsp.exe not found via {EXTENSIONS_GLOB}. Install the "
            "'epicgames.verse' VS Code extension or set VERSE_LSP_EXE."
        )

    def version_key(path: str):
        m = re.search(r"epicgames\.verse-([\d.]+)", path)
        return [int(x) for x in m.group(1).split(".")] if m else [0]

    return max(candidates, key=version_key)


def discover_workspace_file() -> str:
    if WORKSPACE_FILE_OVERRIDE:
        return WORKSPACE_FILE_OVERRIDE
    candidates = [
        p for p in glob.glob(os.path.join(VERSE_PROJECT_DIR, "*.code-workspace"))
        # FortniteGame is the engine workspace UEFN also touches — never a project
        if os.path.basename(p).lower() != "fortnitegame.code-workspace"
    ]
    if not candidates:
        raise FileNotFoundError(
            f"no .code-workspace found in {VERSE_PROJECT_DIR}. Open the project "
            "in UEFN once, or set VERSE_WORKSPACE_FILE."
        )
    return max(candidates, key=os.path.getmtime)


def _load_workspace_folders(workspace_file: str) -> list:
    folders = []
    with open(workspace_file, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    for f in data.get("folders", []):
        p = f.get("path")
        if p and os.path.isdir(p):
            folders.append({"uri": _to_uri(p), "name": f.get("name", os.path.basename(p))})
    return folders


class _LspSession:
    """One running verse-lsp.exe process with an initialized workspace."""

    def __init__(self):
        self.exe = discover_lsp_exe()
        self.workspace_file = discover_workspace_file()
        self.folders = _load_workspace_folders(self.workspace_file)
        if not self.folders:
            raise RuntimeError(
                f"workspace file {self.workspace_file} has no existing folders — "
                "the LSP would resolve nothing. Re-open the project in UEFN."
            )
        self.proc = subprocess.Popen(
            [self.exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
        self._id = 0
        self.inbox = queue.Queue()
        self.open_docs = {}  # uri -> {"version": int, "mtime": float}
        threading.Thread(target=self._read_loop, daemon=True).start()
        self._initialize()
        _log(f"started {os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(self.exe))))} "
             f"with workspace {os.path.basename(self.workspace_file)} ({len(self.folders)} folders)")

    def alive(self) -> bool:
        return self.proc.poll() is None

    def shutdown(self) -> None:
        try:
            self._notify("exit", {})
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass

    # -- transport ----------------------------------------------------------

    def _read_loop(self):
        f, buf = self.proc.stdout, b""
        # bufsize=0 gives a raw FileIO which has no read1 — fall back to read
        read = f.read1 if hasattr(f, "read1") else f.read
        while True:
            try:
                chunk = read(65536)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while True:
                sep = buf.find(b"\r\n\r\n")
                if sep == -1:
                    break
                header = buf[:sep].decode("utf-8", "replace")
                clen = next((int(l.split(":", 1)[1]) for l in header.split("\r\n")
                             if l.lower().startswith("content-length:")), None)
                if clen is None or len(buf) < sep + 4 + clen:
                    break
                body = buf[sep + 4:sep + 4 + clen]
                buf = buf[sep + 4 + clen:]
                try:
                    self.inbox.put(json.loads(body.decode("utf-8", "replace")))
                except Exception:
                    pass

    def _send(self, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body)
        self.proc.stdin.flush()

    def request(self, method: str, params) -> int:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        return self._id

    def _notify(self, method: str, params) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def wait_for(self, req_id: int, timeout: float = REQUEST_TIMEOUT):
        end = time.time() + timeout
        while time.time() < end:
            try:
                msg = self.inbox.get(timeout=0.5)
            except queue.Empty:
                if not self.alive():
                    return None
                continue
            if msg.get("id") == req_id and ("result" in msg or "error" in msg):
                return msg
            if msg.get("method") == "client/registerCapability" and "id" in msg:
                # server requires a reply to its registration request
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
        return None

    # -- lifecycle ----------------------------------------------------------

    def _initialize(self) -> None:
        rid = self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": self.folders[0]["uri"],
            "trace": "off",
            "capabilities": {
                "textDocument": {
                    "hover": {}, "definition": {},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                },
                "workspace": {"symbol": {}, "workspaceFolders": True},
            },
            "workspaceFolders": self.folders,
        })
        if not self.wait_for(rid, 30):
            self.shutdown()
            raise RuntimeError("verse-lsp initialize timed out")
        self._notify("initialized", {})
        time.sleep(INIT_INGEST_WAIT)

    def ensure_open(self, path: str) -> None:
        """didOpen the file (or reopen if it changed on disk since last open)."""
        uri = _to_uri(path)
        mtime = os.path.getmtime(path)
        doc = self.open_docs.get(uri)
        if doc and doc["mtime"] == mtime:
            return  # already open and analyzed — query immediately
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        version = 1
        if doc:
            self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})
            version = doc["version"] + 1
        self._notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": "verse", "version": version, "text": text}})
        self.open_docs[uri] = {"version": version, "mtime": mtime}
        time.sleep(OPEN_ANALYSIS_WAIT)


# ---------------------------------------------------------------------------
# Module-level session management
# ---------------------------------------------------------------------------

_session: _LspSession = None
_lock = threading.Lock()


def _get_session() -> _LspSession:
    global _session
    with _lock:
        if _session is None or not _session.alive():
            if _session is not None:
                _log("previous verse-lsp process died — restarting")
                _session.shutdown()
            _session = _LspSession()
        return _session


def restart_session() -> str:
    global _session
    with _lock:
        if _session is not None:
            _session.shutdown()
            _session = None
        _session = _LspSession()
        return (f"restarted: exe={_session.exe} "
                f"workspace={_session.workspace_file} folders={len(_session.folders)}")


def _resolve_path(file_path: str) -> str:
    """Absolute paths pass through; relative ones resolve against the
    workspace's first folder (the project Content root)."""
    if os.path.isabs(file_path):
        return file_path
    s = _get_session()
    candidate = os.path.join(_from_uri(s.folders[0]["uri"]), file_path)
    return candidate if os.path.isfile(candidate) else os.path.abspath(file_path)


def _flat_symbols(syms, depth=0, out=None):
    if out is None:
        out = []
    for s in syms:
        out.append((depth, s))
        if s.get("children"):
            _flat_symbols(s["children"], depth + 1, out)
    return out


# ---------------------------------------------------------------------------
# Core operations (plain functions — testable without MCP)
# ---------------------------------------------------------------------------

def document_symbols(file_path: str) -> list:
    s = _get_session()
    path = _resolve_path(file_path)
    s.ensure_open(path)
    rid = s.request("textDocument/documentSymbol", {"textDocument": {"uri": _to_uri(path)}})
    resp = s.wait_for(rid)
    res = (resp or {}).get("result") or []
    out = []
    for depth, sym in _flat_symbols(res):
        rng = sym.get("range") or sym.get("location", {}).get("range", {})
        out.append({
            "line": rng.get("start", {}).get("line", 0) + 1,
            "kind": SYMBOL_KIND.get(sym.get("kind"), str(sym.get("kind"))),
            "name": sym.get("name"),
            "depth": depth,
        })
    return out


def hover(file_path: str, line: int, col: int) -> str:
    s = _get_session()
    path = _resolve_path(file_path)
    s.ensure_open(path)
    rid = s.request("textDocument/hover", {
        "textDocument": {"uri": _to_uri(path)},
        "position": {"line": line - 1, "character": col - 1},
    })
    resp = s.wait_for(rid)
    res = (resp or {}).get("result")
    if not res:
        return ""
    contents = res.get("contents")
    if isinstance(contents, dict):
        return contents.get("value", "")
    if isinstance(contents, list):
        return "\n".join(c.get("value", "") if isinstance(c, dict) else str(c) for c in contents)
    return str(contents)


def definition(file_path: str, line: int, col: int) -> list:
    s = _get_session()
    path = _resolve_path(file_path)
    s.ensure_open(path)
    rid = s.request("textDocument/definition", {
        "textDocument": {"uri": _to_uri(path)},
        "position": {"line": line - 1, "character": col - 1},
    })
    resp = s.wait_for(rid)
    res = (resp or {}).get("result")
    if not res:
        return []
    locs = res if isinstance(res, list) else [res]
    out = []
    for loc in locs:
        start = loc.get("range", {}).get("start", {})
        out.append({
            "file": _from_uri(loc.get("uri", "")),
            "line": start.get("line", 0) + 1,
            "col": start.get("character", 0) + 1,
        })
    return out


def workspace_symbols(query: str) -> list:
    s = _get_session()
    rid = s.request("workspace/symbol", {"query": query})
    resp = s.wait_for(rid)
    res = (resp or {}).get("result") or []
    out = []
    for sym in res:
        loc = sym.get("location", {})
        out.append({
            "name": sym.get("name"),
            "kind": SYMBOL_KIND.get(sym.get("kind"), str(sym.get("kind"))),
            "file": _from_uri(loc.get("uri", "")),
            "line": loc.get("range", {}).get("start", {}).get("line", 0) + 1,
        })
    return out


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------

def register(mcp) -> None:
    """Attach the Verse LSP tools to a FastMCP instance."""

    @mcp.tool()
    def verse_symbols(file_path: str) -> str:
        """Outline a .verse file: classes, functions, vars with line numbers.

        Uses a persistent verse-lsp.exe session (cold start ~10s on first call,
        instant afterwards; a changed file re-analyzes for ~5s). file_path may
        be absolute or relative to the project Content folder. The LSP does NOT
        report compile errors — use verse_compile for that.
        """
        return json.dumps(document_symbols(file_path), indent=2, ensure_ascii=False)

    @mcp.tool()
    def verse_hover(file_path: str, line: int, col: int) -> str:
        """Type/signature info at a position in a .verse file (1-based line/col).

        Resolves symbols from the UEFN digests (Verse/Fortnite/UnrealEngine API),
        so it works for built-in API calls too. Empty result = nothing known at
        that position (try the identifier's first character).
        """
        result = hover(file_path, line, col)
        return result if result else "(no hover info at this position)"

    @mcp.tool()
    def verse_definition(file_path: str, line: int, col: int) -> str:
        """Go-to-definition from a position in a .verse file (1-based line/col).

        Returns file:line:col locations — including jumps into the UEFN digest
        files for built-in API symbols.
        """
        locs = definition(file_path, line, col)
        if not locs:
            return "(no definition found)"
        return json.dumps(locs, indent=2, ensure_ascii=False)

    @mcp.tool()
    def verse_find_symbol(query: str) -> str:
        """Workspace-wide symbol search by name (verse-lsp workspace/symbol).

        LIMITED: the Verse LSP often returns empty results for this request —
        prefer Grep over .verse sources when this comes back empty.
        """
        syms = workspace_symbols(query)
        if not syms:
            return "(no matches — the Verse LSP's workspace search is unreliable; try grep)"
        return json.dumps(syms, indent=2, ensure_ascii=False)

    @mcp.tool()
    def verse_lsp_restart() -> str:
        """Restart the persistent verse-lsp session.

        Use when results look stale — e.g. after a compile regenerated the UEFN
        digests, after switching the open UEFN project, or if queries start
        returning nothing for known-good positions. Re-discovers the newest
        verse-lsp.exe and the freshest .code-workspace.
        """
        return restart_session()
