"""Test whether UEFN sandbox allows socket binding and other MCP prerequisites.

Run inside UEFN editor: py "<project>/Content/Python/test_mcp_feasibility.py"
Results are written to: <project_root>/mcp_test_results.txt
and also logged to UE Output Log.
"""

import os
import sys
import socket
import threading
import json
import tempfile
from datetime import datetime

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

# Try to find a writable location for the results file.
# Priority: project root > user Documents > temp dir
_CANDIDATE_DIRS = [
    os.path.join(os.path.expanduser("~"), "Documents"),
    tempfile.gettempdir(),
]

RESULTS_PATH = ""
for d in _CANDIDATE_DIRS:
    if os.path.isdir(d):
        RESULTS_PATH = os.path.join(d, "mcp_test_results.txt")
        break

_lines = []  # type: list[str]


def _log(msg, level="info"):
    # type: (str, str) -> None
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = "[{}] {}".format(timestamp, msg)
    _lines.append(entry)
    try:
        import unreal
        if level == "error":
            unreal.log_error(msg)
        elif level == "warning":
            unreal.log_warning(msg)
        else:
            unreal.log(msg)
    except ImportError:
        print(entry)


def _flush():
    # type: () -> None
    if not RESULTS_PATH:
        _log("Could not find writable directory for results file!", "error")
        return
    try:
        with open(RESULTS_PATH, "w", encoding="utf-8") as f:
            f.write("MCP Feasibility Test Results\n")
            f.write("=" * 50 + "\n")
            f.write("Date: {}\n".format(datetime.now().isoformat()) + "\n")
            f.write("Python: {}\n".format(sys.version) + "\n")
            f.write("=" * 50 + "\n\n")
            for line in _lines:
                f.write(line + "\n")
        _log("Results saved to: {}".format(RESULTS_PATH))
    except Exception as e:
        _log("Failed to write results file: {}".format(e), "error")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_python_version():
    # type: () -> bool
    _log("Python version: {}".format(sys.version))
    _log("Python executable: {}".format(sys.executable))
    return True


def test_stdlib_modules():
    # type: () -> bool
    """Check that all stdlib modules needed for MCP listener are importable."""
    modules = [
        "socket", "threading", "queue", "json",
        "http.server", "urllib.parse", "io",
    ]
    all_ok = True
    for mod_name in modules:
        try:
            __import__(mod_name)
            _log("  [OK] import {}".format(mod_name))
        except ImportError as e:
            _log("  [FAIL] import {}: {}".format(mod_name, e), "error")
            all_ok = False
    return all_ok


def test_socket_bind(port=8765):
    # type: (int) -> bool
    """Try to bind a TCP socket on localhost."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        _log("  [OK] TCP bind on 127.0.0.1:{}".format(port))
        s.close()
        return True
    except Exception as e:
        _log("  [FAIL] TCP bind on 127.0.0.1:{} - {}".format(port, e), "error")
        return False


def test_http_server(port=8766):
    # type: (int) -> bool
    """Start http.server on a background thread, make a request, shut down."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())

        def log_message(self, fmt, *args):
            pass  # silence default stderr logging

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        _log("  [OK] HTTPServer started on 127.0.0.1:{}".format(port))

        # Try to connect and make a request
        try:
            import urllib.request
            resp = urllib.request.urlopen(
                "http://127.0.0.1:{}/ping".format(port), timeout=3
            )
            body = json.loads(resp.read().decode())
            _log("  [OK] HTTP GET /ping -> {}".format(body))
        except Exception as e:
            _log("  [FAIL] HTTP request failed: {}".format(e), "error")
            server.shutdown()
            return False

        server.shutdown()
        _log("  [OK] HTTPServer shut down cleanly")
        return True
    except Exception as e:
        _log("  [FAIL] HTTPServer failed: {}".format(e), "error")
        return False


def test_background_thread():
    # type: () -> bool
    """Check that daemon threads work."""
    result = {"ok": False}

    def worker():
        result["ok"] = True

    try:
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=2)
        if result["ok"]:
            _log("  [OK] Daemon thread executed successfully")
            return True
        else:
            _log("  [FAIL] Daemon thread did not complete", "error")
            return False
    except Exception as e:
        _log("  [FAIL] Threading error: {}".format(e), "error")
        return False


def test_tick_callback():
    # type: () -> bool
    """Check that register_slate_post_tick_callback is available."""
    try:
        import unreal
        if hasattr(unreal, "register_slate_post_tick_callback"):
            _log("  [OK] register_slate_post_tick_callback available")
            return True
        else:
            _log("  [FAIL] register_slate_post_tick_callback not found", "error")
            return False
    except ImportError:
        _log("  [SKIP] unreal module not available (running outside editor)")
        return False


def test_file_write():
    # type: () -> bool
    """Check that we can write files (fallback IPC method)."""
    test_path = os.path.join(tempfile.gettempdir(), "uefn_mcp_write_test.tmp")
    try:
        with open(test_path, "w") as f:
            f.write("test")
        os.remove(test_path)
        _log("  [OK] File write to temp dir works")
        return True
    except Exception as e:
        _log("  [FAIL] File write: {}".format(e), "error")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # type: () -> None
    _log("=" * 50)
    _log("MCP FEASIBILITY TEST FOR UEFN")
    _log("=" * 50)
    _log("")

    results = {}

    tests = [
        ("Python Version", test_python_version),
        ("Stdlib Modules", test_stdlib_modules),
        ("Background Thread", test_background_thread),
        ("Socket Bind (TCP)", test_socket_bind),
        ("HTTP Server + Request", test_http_server),
        ("Tick Callback (unreal)", test_tick_callback),
        ("File Write (fallback)", test_file_write),
    ]

    for name, fn in tests:
        _log("")
        _log("--- {} ---".format(name))
        try:
            results[name] = fn()
        except Exception as e:
            _log("  [CRASH] {}: {}".format(name, e), "error")
            results[name] = False

    # Summary
    _log("")
    _log("=" * 50)
    _log("SUMMARY")
    _log("=" * 50)
    for name, ok in results.items():
        status = "PASS" if ok else "FAIL"
        _log("  {} : {}".format(status, name))

    http_ok = results.get("HTTP Server + Request", False)
    file_ok = results.get("File Write (fallback)", False)

    _log("")
    if http_ok:
        _log("VERDICT: HTTP-based MCP listener is FEASIBLE")
    elif file_ok:
        _log("VERDICT: HTTP blocked, but FILE-BASED IPC is feasible (fallback)")
    else:
        _log("VERDICT: Both HTTP and file IPC blocked. MCP not feasible.", "error")

    _log("")
    _log("Results file: {}".format(RESULTS_PATH))
    _flush()


main()
