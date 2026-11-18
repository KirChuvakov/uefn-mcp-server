"""Offline test for get_editor_log: it reads the editor log, never a newer neighbour. No UEFN."""
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _listener_ast as la  # noqa: E402


def _handler(log_dir):
    paths = types.SimpleNamespace(project_log_dir=lambda: log_dir)
    return la.load(["_cmd_get_editor_log"], types.SimpleNamespace(Paths=paths))["_cmd_get_editor_log"]


def _write(log_dir, name, lines, age):
    path = os.path.join(log_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in lines))
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


def test_picks_the_editor_log_over_newer_logs():
    d = tempfile.mkdtemp()
    _write(d, "UnrealEditorFortnite.log", ["editor one", "LogMCP two", "editor three"], 100)
    _write(d, "UnrealEditorFortnite-backup-2026.11.19-10.00.00.log", ["backup"], 10)
    _write(d, "Lore.log", ["lore"], 1)
    _write(d, "UnrealRevisionControl.log", ["revision control"], 1)
    r = _handler(d)(last_n=2)
    assert os.path.basename(r["file"]) == "UnrealEditorFortnite.log"
    assert r["lines"] == ["LogMCP two", "editor three"]


def test_filter_runs_before_the_tail():
    d = tempfile.mkdtemp()
    _write(d, "UnrealEditorFortnite.log", ["LogMCP one", "other", "other"], 1)
    assert _handler(d)(last_n=1, filter_str="logmcp")["lines"] == ["LogMCP one"]


def test_no_editor_log_is_an_error_not_another_file():
    d = tempfile.mkdtemp()
    _write(d, "Lore.log", ["lore"], 1)
    r = _handler(d)()
    assert r["lines"] == [] and "UnrealEditorFortnite" in r["error"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
