"""Offline tests for uefn_session.py: INI editing, editor-log state machine, state classification,
install discovery, hook check. Uses temp files only; never touches UEFN or its real settings.

    python tests/test_uefn_session_offline.py      (pytest also collects it)
"""
import atexit
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uefn_session as us  # noqa: E402

UTC = dt.timezone.utc

_TMP_DIRS = []


def _tmpdir(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    _TMP_DIRS.append(d)
    return d


atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True) for d in _TMP_DIRS])

SECTION = us.VALKYRIE_SECTION

SETTINGS = (
    "[EditorStartup]\r\nLastLevel=/MyIsland/Main\r\n\r\n"
    f"[{SECTION}]\r\nbValkyrieMode=True\r\n{us.LOAD_KEY}=HomeScreen\r\nbStartupWithLastProject=False\r\n"
    "LastProjectFileName=C:/Projects/A/A.uefnproject\r\n"
    f"{us.PYTHON_KEY}=((82f5daa4-4cac-4d1b-8ceb-4b8ddc1656aa, True),(8937AE72-46c8-b198-d5bf-7c8346c5acce, False))\r\n"
    "Weird=a=b=c\r\n\r\n[Other]\r\nValkyrieLoadAtStartupMostRecentProject=Nope\r\n"
)

LOG = """\ufeffLog file open, 09/30/26 19:35:18
LogWindows: Custom abort handler registered for crash reporting.
[2026.09.30-16.35.25:228][  0]LogVerseMessageServer: Display: Verse message server is ready for client connections on IP: 127.0.0.1:1962, Port: 1962!
[2026.09.30-16.35.32:824][  0]LogPython: Python disabled via CVar 'Engine.Python.IsEnabledByDefault'
[2026.09.30-16.35.43:985][848]LogInit: Display: Engine is initialized. Leaving FEngineLoop::Init()
[2026.09.30-16.35.44:555][848]LogValkyrie: Searching for projects under the following folder(s) took 0.35 sec
[2026.09.30-16.35.45:671][848]LogValkyrieProjectBrowser: Selected Project (Direct): {
[2026.09.30-16.35.53:965][849]LogValkyrie: Opening project 'C:/Projects/MyIsland/MyIsland.uefnproject'
[2026.09.30-16.36.20:195][982]LogPython: Warning: Python enabled via IPythonScriptPlugin::ForceEnablePythonAtRuntime:
[2026.09.30-16.36.28:615][982]LogPython: [MCP] Listener started on http://127.0.0.1:8765
[2026.09.30-16.36.28:710][982]LogPython: [MCP] Auto-started on port 8765
[2026.09.30-16.36.28:783][982]LogValkyrieToolsetRegistration: Started the ModelContextProtocol server on port 8000 for the UEFN MCP Toolsets setting.
[2026.09.30-16.36.29:151][982]LogValkyrie: OpenProject_End - Begin (bSuccess=1, bCanceled=0)
[2026.09.30-16.36.29:151][982]LogValkyrie: Display: Successfully opened project 'C:/Projects/MyIsland/MyIsland.uefnproject' and 0 dependency project(s) (took 35.12 sec)
"""


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    raise AssertionError(f"{fn.__name__} did not raise {exc.__name__}")


def test_ini_get():
    assert us.ini_get(SETTINGS, SECTION, us.LOAD_KEY) == "HomeScreen"
    assert us.ini_get(SETTINGS, SECTION, us.LOAD_KEY.lower()) == "HomeScreen"
    assert us.ini_get(SETTINGS, SECTION, "Weird") == "a=b=c"
    assert us.ini_get(SETTINGS, "Other", us.LOAD_KEY) == "Nope"
    assert us.ini_get(SETTINGS, SECTION, "Missing") is None and us.ini_get(SETTINGS, "Nope", "x") is None


def test_ini_set_keeps_every_other_byte():
    new, old = us.ini_set(SETTINGS, SECTION, us.LOAD_KEY, "LastProject")
    assert old == "HomeScreen"
    assert new == SETTINGS.replace(f"{us.LOAD_KEY}=HomeScreen\r\n", f"{us.LOAD_KEY}=LastProject\r\n")
    assert us.ini_get(new, "Other", us.LOAD_KEY) == "Nope"
    new2, old2 = us.ini_set(new, SECTION, "BrandNew", "1")
    assert old2 is None and f"[{SECTION}]\r\nBrandNew=1\r\n" in new2 and us.ini_get(new2, SECTION, "BrandNew") == "1"
    new3, _ = us.ini_set("[A]\nx=1\n", "B", "k", "v")
    assert new3 == "[A]\nx=1\n[B]\nk=v\n"
    new4, _ = us.ini_set("", SECTION, "k", "v")
    assert us.ini_get(new4, SECTION, "k") == "v"
    new5, _ = us.ini_set("[A]\nx=1", "A", "x", "2")
    assert new5 == "[A]\nx=2"


def test_decode_encode_roundtrip():
    for data in (b"\xef\xbb\xbf[A]\r\nk=\xc3\xa9\r\n", "[A]\r\nk=\u00e9\r\n".encode("utf-16-le"),
                 b"[A]\nk=v\n", b"[A]\nk=\xff\x80\n"):
        if not data.startswith(b"\xef") and data[1:2] == b"\x00":
            data = b"\xff\xfe" + data
        text, codec, bom = us.decode_text(data)
        assert us.encode_text(text, codec, bom) == data, codec


def test_python_enabled_and_load_values():
    m = us.parse_python_enabled(us.ini_get(SETTINGS, SECTION, us.PYTHON_KEY))
    assert m == {"82f5daa4-4cac-4d1b-8ceb-4b8ddc1656aa": True, "8937ae72-46c8-b198-d5bf-7c8346c5acce": False}
    assert us.parse_python_enabled(None) == {}
    assert us.normalize_load_value("lastproject") == "LastProject"
    assert us.normalize_load_value("Most Recent Project") == "LastProject"
    assert us.normalize_load_value("hub") == us.normalize_load_value("Home Panel") == "HomeScreen"
    raises(ValueError, us.normalize_load_value, "sometimes")


def test_log_scan_markers():
    scan = us.LogScan().feed_text(LOG)
    assert scan.header_epoch == time.mktime((2026, 9, 30, 19, 35, 18, 0, 0, -1))
    p = scan.project()
    assert p["state"] == "open" and p["path"].endswith("MyIsland.uefnproject")
    opening = scan.event("opening")
    assert scan.after("python_on", opening) and scan.after("mcp_started", opening)["port"] == "8765"
    assert scan.after("toolset_mcp", opening)["port"] == "8000"
    assert scan.event("verse_server")["port"] == "1962"
    assert scan.event("engine_init")["_ts"] == dt.datetime(2026, 9, 30, 16, 35, 43, 985000, tzinfo=UTC)
    assert scan.after("python_off", opening) is None
    # A second project starts loading: state goes back to opening, older markers no longer count.
    scan.feed("[2026.09.30-17.00.00:000][  1]LogValkyrie: Opening project 'C:/Projects/B/B.uefnproject'")
    p = scan.project()
    assert p["state"] == "opening" and p["path"].endswith("B.uefnproject")
    assert scan.after("python_on", scan.event("opening")) is None
    scan.feed("[2026.09.30-17.00.09:000][  1]LogValkyrie: OpenProject_End - Begin (bSuccess=0, bCanceled=0)")
    assert scan.project()["state"] == "open_failed"
    scan.feed("[2026.09.30-17.00.10:000][  1]LogPython: [MCP] Auto-start failed: No module named 'uefn_listener'")
    assert scan.mcp_errors[-1]["msg"].startswith("Auto-start failed")
    assert us.LogScan().feed_text("Log file open, 09/30/26 19:35:18\n").project()["state"] == "none"


def test_log_tail_incremental():
    tmp = _tmpdir(prefix="uefn-log-")
    path = os.path.join(tmp, "UnrealEditorFortnite.log")
    head, rest = LOG.encode("utf-8")[:900], LOG.encode("utf-8")[900:]
    with open(path, "wb") as fh:
        fh.write(head)
    tail = us._LogTail(path)
    first = tail.update()
    assert first.project()["state"] in ("none", "opening")
    with open(path, "ab") as fh:
        fh.write(rest)
    scan = tail.update()
    assert scan.project()["state"] == "open" and scan.event("mcp_started")["port"] == "8765"
    lines_before = scan.lines
    assert tail.update().lines == lines_before  # nothing new, nothing re-read
    with open(path, "wb") as fh:  # a new editor run replaces the file
        fh.write("\ufeffLog file open, 10/01/26 09:00:00\n".encode("utf-8"))
    scan = tail.update()
    assert scan.project()["state"] == "none" and scan.header_epoch == time.mktime((2026, 10, 1, 9, 0, 0, 0, 0, -1))


def test_classify():
    now = dt.datetime(2026, 9, 30, 16, 40, 0, tzinfo=UTC)
    ready = now - dt.timedelta(seconds=10)
    base = dict(editor_pids=[1], crash_dialog=False, log_current=True, project_state={"state": "none"},
                engine_ready_at=ready, now=now, load_setting="LastProject", wanted_path=None)
    c = us.classify
    assert c(**dict(base, crash_dialog=True, editor_pids=[])) == "crash_dialog"
    assert c(**dict(base, editor_pids=[])) == "not_running"
    assert c(**dict(base, log_current=False)) == "starting"
    assert c(**base) == "starting"                                   # LastProject: wait 30 s for auto-load
    assert c(**dict(base, engine_ready_at=now - dt.timedelta(seconds=31))) == "hub"
    assert c(**dict(base, load_setting="HomeScreen")) == "hub"      # HomeScreen: HUB right away
    assert c(**dict(base, engine_ready_at=None)) == "starting"
    assert c(**dict(base, project_state={"state": "opening", "path": "C:/P/A.uefnproject"})) == "opening"
    assert c(**dict(base, project_state={"state": "open_failed", "path": "x"})) == "open_failed"
    open_a = {"state": "open", "path": "C:/Projects/A/A.uefnproject"}
    assert c(**dict(base, project_state=open_a)) == "project_open"
    assert c(**dict(base, project_state=open_a, wanted_path=r"c:\projects\a\A.uefnproject")) == "project_open"
    assert c(**dict(base, project_state=open_a, wanted_path=r"C:\Projects\B\B.uefnproject")) == "other_project_open"


def test_find_install_from_manifests():
    tmp = _tmpdir(prefix="uefn-install-")
    install = os.path.join(tmp, "Epic Games", "Fortnite")
    exe = os.path.join(install, us.EDITOR_REL)
    os.makedirs(os.path.dirname(exe))
    open(exe, "wb").close()
    manifest = {"AppName": "Fortnite_Studio", "CatalogNamespace": "fn", "CatalogItemId": "abc123",
                "InstallLocation": install, "LaunchExecutable": "FortniteGame/Binaries/Win64/" + us.EDITOR_EXE}
    saved = {k: os.environ.pop(k, None) for k in ("UEFN_EDITOR_EXE", "UEFN_FORTNITE_DIR")}
    try:
        info = us.find_install([manifest], [], running_exe="")
        assert os.path.normcase(info["exe"]) == os.path.normcase(os.path.normpath(exe))
        assert info["exe_source"] == "Epic launcher manifest"
        assert os.path.normcase(info["fortnite_dir"]) == os.path.normcase(install)
        assert info["launcher_uri"] == "com.epicgames.launcher://apps/fn%3Aabc123%3AFortnite_Studio?action=launch&silent=true"
        info = us.find_install([], [{"AppName": "Fortnite", "InstallLocation": install}], running_exe="")
        assert info["exe_source"] == "LauncherInstalled.dat" and info["launcher_uri"] is None
        other = os.path.join(tmp, "custom.exe")
        open(other, "wb").close()
        os.environ["UEFN_EDITOR_EXE"] = other
        assert us.find_install([manifest], [], running_exe="")["exe_source"] == "UEFN_EDITOR_EXE"
        del os.environ["UEFN_EDITOR_EXE"]
        os.environ["UEFN_FORTNITE_DIR"] = install
        assert us.find_install([], [], running_exe="")["exe_source"] == "UEFN_FORTNITE_DIR"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_hook_status():
    tmp = _tmpdir(prefix="uefn-hook-")
    epic = os.path.join(tmp, us.HOOK_REL)
    os.makedirs(os.path.dirname(epic))
    target = os.path.join(tmp, "init_unreal.py")
    open(target, "w").close()
    with open(epic, "w", encoding="utf-8") as fh:
        fh.write(f'import unreal\n# --- {us.HOOK_MARKER} (added) ---\ntry:\n    import runpy\n'
                 f'    runpy.run_path(r"{target}")\nexcept Exception:\n    pass\n')
    st = us.hook_status(tmp)
    assert st["installed"] and os.path.normcase(st["target"]) == os.path.normcase(target)
    os.remove(target)
    assert not us.hook_status(tmp)["installed"]
    with open(epic, "w", encoding="utf-8") as fh:
        fh.write("import unreal\n")
    assert "missing" in us.hook_status(tmp)["reason"]
    assert not us.hook_status(None)["installed"] and not us.hook_status(os.path.join(tmp, "nope"))["installed"]


def test_write_settings_on_a_copy():
    tmp = _tmpdir(prefix="uefn-ini-")
    path = os.path.join(tmp, "EditorPerProjectUserSettings.ini")
    data = SETTINGS.encode("utf-8")
    with open(path, "wb") as fh:
        fh.write(data)
    dry = us.write_settings({us.LOAD_KEY: "LastProject"}, dry_run=True, path=path, allow_running=True)
    assert dry["changed"] and dry["changes"][us.LOAD_KEY] == {"old": "HomeScreen", "new": "LastProject"}
    with open(path, "rb") as fh:
        assert fh.read() == data
    res = us.write_settings({us.LOAD_KEY: "LastProject", us.LAST_PROJECT_KEY: "C:/Projects/B/B.uefnproject"},
                            path=path, allow_running=True)
    assert os.path.isfile(res["backup"])
    with open(path, "rb") as fh:
        new = fh.read().decode("utf-8")
    expected = SETTINGS.replace("=HomeScreen\r\n", "=LastProject\r\n", 1).replace(
        "LastProjectFileName=C:/Projects/A/A.uefnproject", "LastProjectFileName=C:/Projects/B/B.uefnproject")
    assert new == expected
    assert us.read_settings(path)["load_on_startup"] == "LastProject"
    again = us.write_settings({us.LOAD_KEY: "LastProject"}, path=path, allow_running=True)
    assert not again["changed"] and "backup" not in again


def test_project_file_and_resolution():
    tmp = _tmpdir(prefix="uefn-proj-")
    folder = os.path.join(tmp, "Fortnite Projects", "MyIsland")
    os.makedirs(folder)
    path = os.path.join(folder, "MyIsland.uefnproject")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"title": "My Island", "bindings": {"projectId": "82F5DAA4-4CAC-4D1B-8CEB-4B8DDC1656AA"},
                   "dataSets": {"experimental": {"pythonExperimental": {"bEnablePythonForProject": True},
                                                 "toolsets": {"bEnableToolsetsForProject": False}}}}, fh)
    info = us.read_project_file(path)
    assert info["title"] == "My Island" and info["project_id"] == "82f5daa4-4cac-4d1b-8ceb-4b8ddc1656aa"
    assert info["python_for_project"] is True and info["toolsets_for_project"] is False
    saved = os.environ.get("UEFN_SAVED_DIR")
    try:
        os.environ["UEFN_SAVED_DIR"] = os.path.join(tmp, "Saved")
        ini = us.settings_file()
        os.makedirs(os.path.dirname(ini))
        with open(ini, "w", encoding="utf-8") as fh:
            fh.write(f"[{SECTION}]\nLastCreatedProjectLocation={os.path.join(tmp, 'Fortnite Projects')}\n")
        assert us.resolve_project(path)["path"] == os.path.normpath(path)
        assert us.resolve_project(folder)["path"] == os.path.normpath(path)
        assert us.resolve_project("MyIsland")["path"] == os.path.normpath(path)
        assert us.resolve_project("my island")["path"] == os.path.normpath(path)  # title match
        raises(ValueError, us.resolve_project, "NoSuchIsland")
    finally:
        if saved is None:
            os.environ.pop("UEFN_SAVED_DIR", None)
        else:
            os.environ["UEFN_SAVED_DIR"] = saved


def test_hints():
    st = {"state": "project_open", "project": {"title": "X"}, "editor": {"main_window": {"responding": True}},
          "log": {"python_enabled": False, "mcp_errors": []}, "ports": {"listener": None, "listener_busy": []}}
    assert any("EnablePythonInUEFN" in h for h in us.hints_for(st))
    st["log"] = {"python_enabled": True, "mcp_autostart": None, "mcp_errors": []}
    assert any("ensure_mcp_hook.ps1" in h for h in us.hints_for(st))
    st["ports"] = {"listener": None, "listener_busy": [8765]}
    assert any("did not answer in time" in h for h in us.hints_for(st))
    st["ports"] = {"listener": 8765}
    assert us.hints_for(st) == ["Ready: listener on 8765."]
    crash = dict(st, state="crash_dialog", editor={"pids": []})
    assert any("close_crash_reporter=True" in h for h in us.hints_for(crash))
    assert not any("close_crash_reporter" in h for h in us.hints_for(dict(crash, editor={"pids": [1]})))
    hub = us.hints_for(dict(st, state="hub"))
    assert any("Most Recent Project" in h for h in hub) and not any("click" in h for h in hub)


def _run_all():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
