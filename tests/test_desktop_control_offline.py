"""Offline tests for desktop_control.py. No input is ever sent; nothing needs UEFN.

Covers key-name and combo parsing, allowlist parsing / matching (the anti-cheat-protected Fortnite
client gets input only through an explicit opt-in), coordinate mapping with synthetic monitor layouts
(negative origins, 125 % / 150 % scaling), SendInput absolute normalization, the kill switch,
sensitive-text redaction, the PNG encoder and the audit log.

    python tests/test_desktop_control_offline.py          # offline tests (pytest also collects them)
    python tests/test_desktop_control_offline.py --live   # + read-only live smoke: windows, cursor,
                                                          #   monitor-1 screenshots (no input, no focus)
"""
import json
import os
import struct
import sys
import tempfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import desktop_control as dc  # noqa: E402


def mon(index, left, top, width, height, dpi=96, primary=False):
    return {"index": index, "left": left, "top": top, "width": width, "height": height, "dpi": dpi,
            "scale": dc.scale_from_dpi(dpi), "primary": primary,
            "work_area": {"x": left, "y": top, "width": width, "height": height - 40}}


# Primary 1920x1080 @100 % at the origin; a 2560x1440 @125 % monitor to the left and 180 px higher
# (negative origin); a 3840x2160 @150 % monitor to the right.
LAYOUT = [
    mon(1, 0, 0, 1920, 1080, 96, True),
    mon(2, -2560, -180, 2560, 1440, 120),
    mon(3, 1920, 0, 3840, 2160, 144),
]
# The owner's real layout (2026-09-30): two 2560x1440 @125 %, the secondary on the left.
LAYOUT_TWIN = [mon(1, 0, 0, 2560, 1440, 120, True), mon(2, -2560, 0, 2560, 1440, 120)]


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    raise AssertionError(f"{fn.__name__}{a} did not raise {exc.__name__}")


# -- keys --------------------------------------------------------------------------------------

def test_key_names_and_combos():
    assert dc.parse_combo("f5") == [("f5", 0x74)]
    assert [vk for _, vk in dc.parse_combo("ctrl+s")] == [0x11, 0x53]
    assert [vk for _, vk in dc.parse_combo("Alt+F4")] == [0x12, 0x73]
    assert dc.parse_combo("enter")[0][1] == dc.parse_combo("return")[0][1] == 0x0D
    assert dc.parse_combo("esc")[0][1] == dc.parse_combo("escape")[0][1] == 0x1B
    assert [vk for _, vk in dc.parse_combo(" ctrl + shift + p ")] == [0x11, 0x10, 0x50]
    assert [vk for _, vk in dc.parse_combo("ctrl++")] == [0x11, 0xBB]
    assert dc.parse_combo("ctrl+plus") == dc.parse_combo("ctrl++")
    assert dc.parse_combo("+") == [("plus", 0xBB)]
    assert dc.parse_combo("num5")[0][1] == 0x65 and dc.parse_combo("f24")[0][1] == 0x87
    assert dc.parse_combo("add")[0][1] == 0x6B
    assert [vk for _, vk in dc.parse_combo("ctrl+shift+alt+delete")] == [0x11, 0x10, 0x12, 0x2E]


def test_combo_errors():
    for bad in ("", "   ", "ctrl+", "+ctrl", "ctrl+foo", "ctrl+ctrl", "ctrl+control", "a++b"):
        raises(ValueError, dc.parse_combo, bad)


def test_key_table():
    codes = dc.KEY_CODES
    assert all(0 < vk < 0xFF for vk in codes.values())
    assert sum(1 for k in codes if len(k) == 1 and k.isalpha()) == 26
    assert all(f"f{i}" in codes for i in range(1, 25))
    assert all(codes[m] in dc.MODIFIER_VKS for m in ("ctrl", "shift", "alt", "win", "lctrl", "rctrl", "lalt", "ralt"))
    for name in ("up", "down", "left", "right", "home", "end", "pageup", "pagedown", "insert", "delete", "ralt",
                 "rctrl", "divide", "numlock"):
        assert codes[name] in dc.EXTENDED_VKS, name
    for name in ("a", "enter", "space", "f5", "ctrl", "shift", "num5"):
        assert codes[name] not in dc.EXTENDED_VKS, name


def test_mouse_buttons():
    assert dc.parse_button("left") == (0x0002, 0x0004, 0)
    assert dc.parse_button("RIGHT") == (0x0008, 0x0010, 0)
    assert dc.parse_button("back") == (0x0080, 0x0100, 1)
    assert dc.parse_button("x2")[2] == 2
    raises(ValueError, dc.parse_button, "wheel")


# -- allowlist -----------------------------------------------------------------------------------

FORTNITE_CLIENT = ["FortniteClient-Win64-Shipping.exe", "FortniteClient-Win64-Shipping_EAC_EOS.exe",
                   "FortniteClient-Win64-Shipping_BE.exe", "FortniteClient-Win64-Shipping_EAC.exe",
                   r"C:\Program Files\Epic Games\Fortnite\FortniteGame\Binaries\Win64\FortniteClient-Win64-Shipping.exe",
                   "FortniteLauncher.exe"]


def test_allowlist_defaults():
    patterns, disabled = dc.parse_allowlist(None)
    assert not disabled and len(patterns) == 3
    yes = ["UnrealEditorFortnite-Win64-Shipping.exe", r"C:\Program Files\Epic Games\Fortnite\Engine\Binaries\Win64"
           r"\CrashReportClientEditor.exe", "CrashReportClientEditor-Win64-Shipping", "EpicGamesLauncher.exe",
           '"unrealeditorfortnite-win64-shipping.EXE"']
    no = ["explorer.exe", "WindowsTerminal.exe", "Code.exe", "UnrealEditor.exe", "CrashReportClient.exe",
          "chrome.exe", "", "UnrealEditorFortnite-Win64-Shipping-evil.exe"] + FORTNITE_CLIENT
    for name in yes:
        assert dc.process_allowed(name, patterns), name
    for name in no:
        assert not dc.process_allowed(name, patterns), name
    assert dc.allowlist_warnings(patterns, disabled) == []


def test_allowlist_env_override():
    p, d = dc.parse_allowlist("notepad")
    assert p == ["notepad"] and not d
    assert dc.process_allowed("NOTEPAD.EXE", p) and not dc.process_allowed("UnrealEditorFortnite-Win64-Shipping", p)
    p, d = dc.parse_allowlist("+notepad, CrashReportClient")
    assert len(p) == 5 and "notepad" in p and "crashreportclient" in p
    assert dc.process_allowed("UnrealEditorFortnite-Win64-Shipping.exe", p)
    p, d = dc.parse_allowlist("*")
    assert p == ["*"] and d and dc.process_allowed("anything.exe", p, d)
    assert dc.parse_allowlist("   ") == dc.parse_allowlist(None)
    p, _ = dc.parse_allowlist("Foo.EXE, bar*, foo")
    assert p == ["foo", "bar*"]
    assert dc.process_allowed("bargain.exe", p) and not dc.process_allowed("rebar.exe", p)


def test_fortnite_client_needs_an_explicit_opt_in():
    client, eac, launcher = "FortniteClient-Win64-Shipping.exe", "FortniteClient-Win64-Shipping_EAC_EOS.exe", \
        "FortniteLauncher.exe"
    for name in FORTNITE_CLIENT:
        assert dc.protected_stem(name), name
    for name in ("UnrealEditorFortnite-Win64-Shipping.exe", "EpicGamesLauncher.exe", "Fortnite.exe", ""):
        assert dc.protected_stem(name) is None, name
    # the documented opt-in names the game client exactly; the defaults stay
    p, d = dc.parse_allowlist(dc.PROTECTED_OPT_IN)
    assert dc.PROTECTED_OPT_IN == "+FortniteClient-Win64-Shipping"
    assert dc.process_allowed(client, p, d) and not dc.process_allowed(eac, p, d) and not dc.process_allowed(launcher, p, d)
    assert dc.process_allowed("UnrealEditorFortnite-Win64-Shipping.exe", p, d)
    p, d = dc.parse_allowlist("+FortniteClient-Win64-Shipping*")
    assert dc.process_allowed(client, p, d) and dc.process_allowed(eac, p, d)
    p, d = dc.parse_allowlist("+FortniteLauncher")
    assert dc.process_allowed(launcher, p, d) and not dc.process_allowed(client, p, d)
    # broad patterns never reach it, not even with the check disabled
    for broad in ("*", "+*", "+Fortnite*", "Fortnite*,*Shipping*", "+*client*", "+?ortniteClient-Win64-Shipping",
                  "+*FortniteClient-Win64-Shipping"):
        p, d = dc.parse_allowlist(broad)
        for name in FORTNITE_CLIENT:
            assert not dc.process_allowed(name, p, d), (broad, name)
    # `*` plus an entry that names it
    p, d = dc.parse_allowlist("*, FortniteClient-Win64-Shipping")
    assert p == ["*", "fortniteclient-win64-shipping"] and d
    assert dc.process_allowed(client, p, d) and dc.process_allowed("anything.exe", p, d)
    # warnings and refusal hints
    w = dc.allowlist_warnings(*dc.parse_allowlist(dc.PROTECTED_OPT_IN))
    assert len(w) == 1 and "anti-cheat" in w[0] and "fortniteclient-win64-shipping" in w[0]
    assert any("disables the allowlist" in x for x in dc.allowlist_warnings(*dc.parse_allowlist("*")))
    hint = dc.refusal_hint(client)
    assert "UEFN_DESKTOP_ALLOW=+FortniteClient-Win64-Shipping" in hint and "Screenshots" in hint
    assert dc.refusal_hint("UnrealEditorFortnite-Win64-Shipping.exe") == "" and dc.refusal_hint("") == ""


def test_process_matches():
    assert dc.process_matches("UnrealEditorFortnite-Win64-Shipping.exe", "UnrealEditorFortnite")
    assert dc.process_matches("CrashReportClientEditor.exe", "CrashReportClientEditor*")
    assert not dc.process_matches("CrashReportClient.exe", "CrashReportClientEditor*")
    assert dc.process_matches("anything.exe", "")


# -- geometry ------------------------------------------------------------------------------------

def test_virtual_screen_and_lookup():
    assert dc.virtual_screen(LAYOUT) == {"left": -2560, "top": -180, "width": 8320, "height": 2340}
    assert dc.virtual_screen(LAYOUT_TWIN) == {"left": -2560, "top": 0, "width": 5120, "height": 1440}
    assert dc.monitor_for_point(0, 0, LAYOUT)["index"] == 1
    assert dc.monitor_for_point(-1, 0, LAYOUT)["index"] == 2
    assert dc.monitor_for_point(-2560, -180, LAYOUT)["index"] == 2
    assert dc.monitor_for_point(1920, 5, LAYOUT)["index"] == 3
    assert dc.monitor_for_point(5759, 2159, LAYOUT)["index"] == 3
    assert dc.monitor_for_point(100, 1500, LAYOUT) is None   # below the primary, left of monitor 3
    assert dc.monitor_for_point(-100, -181, LAYOUT) is None
    assert dc.scale_from_dpi(120) == 1.25 and dc.scale_from_dpi(144) == 1.5 and dc.scale_from_dpi(0) == 1.0


def test_plan_capture_size():
    w, h = dc.plan_capture_size(3840, 2160)
    assert w <= dc.DEFAULT_MAX_LONG_EDGE and w * h <= dc.DEFAULT_MAX_PIXELS
    assert abs(w / h - 3840 / 2160) < 0.01
    w, h = dc.plan_capture_size(1080, 1920)
    assert max(w, h) <= dc.DEFAULT_MAX_LONG_EDGE and w * h <= dc.DEFAULT_MAX_PIXELS
    assert dc.plan_capture_size(1280, 720) == (1280, 720)
    assert dc.plan_capture_size(3840, 2160, full_resolution=True) == (3840, 2160)
    assert dc.plan_capture_size(8320, 2340, max_long_edge=0, max_pixels=0) == (8320, 2340)
    assert dc.plan_capture_size(5, 3) == (5, 3)


def _roundtrip_monitor(m, full=False):
    out_w, out_h = dc.plan_capture_size(m["width"], m["height"], full_resolution=full)
    mp = dc.make_mapping(m["left"], m["top"], m["width"], m["height"], out_w, out_h)
    assert mp["scale_x"] >= 1.0 and mp["scale_y"] >= 1.0
    for px in range(out_w):
        sx, _ = dc.image_to_screen(px, 0, mp)
        assert m["left"] <= sx < m["left"] + m["width"]
        assert dc.screen_to_image(sx, m["top"], mp)[0] == px, (m, px, sx)
    for py in range(out_h):
        _, sy = dc.image_to_screen(0, py, mp)
        assert m["top"] <= sy < m["top"] + m["height"]
        assert dc.screen_to_image(m["left"], sy, mp)[1] == py
    assert dc.image_to_screen(0, 0, mp) == (m["left"] + int(0.5 * mp["scale_x"]), m["top"] + int(0.5 * mp["scale_y"]))
    last = dc.image_to_screen(out_w - 1, out_h - 1, mp)
    assert last[0] <= m["left"] + m["width"] - 1 and last[1] <= m["top"] + m["height"] - 1
    # out-of-range image points clamp to the captured rect
    assert dc.image_to_screen(-50, 10 ** 6, mp) == (m["left"], m["top"] + m["height"] - 1)


def test_image_screen_mapping_all_monitors():
    for m in LAYOUT + LAYOUT_TWIN:
        _roundtrip_monitor(m)
        _roundtrip_monitor(m, full=True)
    vs = dc.virtual_screen(LAYOUT)
    _roundtrip_monitor({"left": vs["left"], "top": vs["top"], "width": vs["width"], "height": vs["height"]})


def test_window_region_mapping_example():
    # A 1456x819 shot of a 2560x1440 window at (-2560, 0): pixel (728, 409) is the window center.
    mp = dc.make_mapping(-2560, 0, 2560, 1440, 1456, 819)
    assert dc.image_to_screen(728, 409, mp) == (-2560 + int(728.5 * 2560 / 1456), int(409.5 * 1440 / 819))
    x, y = dc.image_to_screen(728, 409, mp)
    assert abs(x - (-1280)) <= 2 and abs(y - 720) <= 2


def test_dip_conversion():
    m150, m125 = LAYOUT[2], LAYOUT[1]
    assert dc.dip_to_physical(100, 100, m150) == (1920 + 150, 150)
    assert dc.physical_to_dip(2070, 150, m150) == (100.0, 100.0)
    assert dc.dip_to_physical(0, 0, m125) == (-2560, -180)
    assert dc.dip_to_physical(800, 400, m125) == (-2560 + 1000, -180 + 500)
    assert dc.physical_to_dip(-1560, 320, m125) == (800.0, 400.0)
    assert dc.dip_to_physical(10, 10, LAYOUT[0]) == (10, 10)


def test_resolve_point_spaces():
    mp = dc.make_mapping(-2560, -180, 2560, 1440, 1280, 720)
    shot = {"mapping": mp, "window": None}
    assert dc.resolve_point(0, 0, LAYOUT, shot)[:2] == (-2559, -179)
    assert dc.resolve_point(10, 20, LAYOUT)[:2] == (10, 20)
    assert dc.resolve_point(10, 20, LAYOUT, relative_to="monitor", monitor=2)[:2] == (-2550, -160)
    assert dc.resolve_point(100, 100, LAYOUT, relative_to="monitor_dip", monitor=3)[:2] == (2070, 150)
    target = {"hwnd": 7, "rect": {"x": 100, "y": 50, "width": 800, "height": 600}}
    assert dc.resolve_point(5, 6, LAYOUT, relative_to="window", target=target)[:2] == (105, 56)
    # A window screenshot remaps when the window moved since the capture.
    wshot = {"mapping": dc.make_mapping(100, 50, 800, 600, 800, 600),
             "window": {"hwnd": 7, "rect": {"x": 100, "y": 50, "width": 800, "height": 600}}}
    moved = {"hwnd": 7, "rect": {"x": 130, "y": 40, "width": 800, "height": 600}}
    x, y, info = dc.resolve_point(10, 10, LAYOUT, wshot, target=moved)
    assert (x, y) == (140, 50) and info["window_moved_by"] == [30, -10]
    raises(ValueError, dc.resolve_point, 1, 1, LAYOUT, relative_to="monitor", monitor=9)
    raises(ValueError, dc.resolve_point, 1, 1, LAYOUT, relative_to="window")
    raises(ValueError, dc.resolve_point, 1, 1, LAYOUT, relative_to="sky")


def test_normalized_absolute_lands_on_the_pixel():
    for layout in (LAYOUT, LAYOUT_TWIN):
        vs = dc.virtual_screen(layout)
        xs = list(range(vs["left"], vs["left"] + 300)) + list(range(vs["left"] + vs["width"] - 300,
                                                                     vs["left"] + vs["width"])) + [0, -1, 1]
        ys = list(range(vs["top"], vs["top"] + vs["height"], 7)) + [vs["top"] + vs["height"] - 1]
        for x in xs:
            nx, _ = dc.normalized_absolute(x, vs["top"], vs)
            assert 0 <= nx <= 65535
            for denom in (65536, 65535):  # both mappings seen in the wild
                assert vs["left"] + nx * vs["width"] // denom == x, (x, nx, denom)
        for y in ys:
            _, ny = dc.normalized_absolute(vs["left"], y, vs)
            assert vs["top"] + ny * vs["height"] // 65536 == y


def test_kill_switch():
    assert dc.kill_switch_hit((0, 0), LAYOUT)
    assert dc.kill_switch_hit((5, 5), LAYOUT)
    assert not dc.kill_switch_hit((6, 0), LAYOUT) and not dc.kill_switch_hit((0, 6), LAYOUT)
    assert dc.kill_switch_hit((-2560, -180), LAYOUT) and dc.kill_switch_hit((-2555, -175), LAYOUT)
    assert dc.kill_switch_hit((1920, 0), LAYOUT) and not dc.kill_switch_hit((1919, 0), LAYOUT)
    assert not dc.kill_switch_hit((100, 100), LAYOUT) and not dc.kill_switch_hit((-1, 0), LAYOUT)
    assert dc.kill_switch_hit((-2560, 0), LAYOUT_TWIN) and dc.kill_switch_hit((3, 2), LAYOUT_TWIN)
    assert not dc.kill_switch_hit((2555, 0), LAYOUT_TWIN)
    assert dc.kill_switch_hit((10, 10), LAYOUT, margin=10) and not dc.kill_switch_hit((10, 10), LAYOUT, margin=9)


def test_pick_main_window():
    def w(h, owner=0, minimized=False, fg=False, area=100, tool=False, visible=True):
        return {"hwnd": h, "owner": owner, "minimized": minimized, "foreground": fg, "visible": visible,
                "cloaked": False, "tool_window": tool, "rect": {"x": 0, "y": 0, "width": area, "height": 1}, "z": h}
    main_minimized, floating_panel = w(1, minimized=True, area=200), w(2, owner=1, area=5000)
    assert dc.pick_main_window([floating_panel, main_minimized])["hwnd"] == 1
    assert dc.pick_main_window([main_minimized, w(3, owner=1, fg=True)])["hwnd"] == 3  # the foreground wins
    assert dc.pick_main_window([w(4, area=10), w(5, area=20)])["hwnd"] == 5
    assert dc.pick_main_window([w(6, tool=True, area=900), w(7, area=10)])["hwnd"] == 7
    assert dc.pick_main_window([]) is None


def test_rect_intersection():
    a = {"x": -9, "y": -9, "width": 2578, "height": 1398}
    wa = {"x": 0, "y": 0, "width": 2560, "height": 1380}
    assert dc.rect_intersection(a, wa) == wa
    assert dc.rect_intersection(wa, {"x": 2560, "y": 0, "width": 10, "height": 10}) is None


# -- redaction, PNG, audit -------------------------------------------------------------------------------

def test_looks_sensitive():
    assert dc.looks_sensitive("Tr0ub4dor&3")
    assert dc.looks_sensitive("ghp_abcdefghijklmnopqrstuvwxyz0123")
    assert not dc.looks_sensitive("hello world")
    assert not dc.looks_sensitive("UnrealEditor")
    assert not dc.looks_sensitive("NewSkyFort")
    assert dc.looks_sensitive("anything", window_title="Sign In - Epic Games")
    assert dc.looks_sensitive("anything", window_title="Enter your password")
    assert dc.looks_sensitive("anything", process="EpicGamesLauncher.exe")
    assert dc.looks_sensitive("anything", forced=True) and dc.looks_sensitive("x", password_control=True)
    assert not dc.looks_sensitive("NewSkyFort", window_title="Unreal Editor for Fortnite")


def _parse_png(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, []
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos:pos + 4])
        tag, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        (crc,) = struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])
        assert crc == zlib.crc32(tag + body) & 0xFFFFFFFF
        chunks.append((tag, body))
        pos += 12 + n
    return chunks


def test_png_encoder_and_bgra():
    w, h = 3, 2
    bgra = bytes([0, 0, 255, 255, 0, 255, 0, 255, 255, 0, 0, 255,
                  10, 20, 30, 0, 40, 50, 60, 0, 70, 80, 90, 0])
    rgb = dc.bgra_to_rgb(bgra, w, h)
    assert rgb == bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 30, 20, 10, 60, 50, 40, 90, 80, 70])
    chunks = _parse_png(dc.encode_png(w, h, rgb))
    assert [c[0] for c in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    assert struct.unpack(">IIBBBBB", chunks[0][1]) == (3, 2, 8, 2, 0, 0, 0)
    raw = zlib.decompress(chunks[1][1])
    assert raw == b"\x00" + rgb[:9] + b"\x00" + rgb[9:]
    raises(ValueError, dc.encode_png, 4, 2, rgb)


def test_audit_log_json_lines():
    import logging
    tmp = tempfile.mkdtemp(prefix="uefn-mcp-test-")
    path = os.path.join(tmp, "audit.log")
    old_env, old_logger = os.environ.get(dc.LOG_ENV), dc._audit_logger
    lg = logging.getLogger("uefn_mcp.desktop_audit")
    old_handlers = list(lg.handlers)
    try:
        os.environ[dc.LOG_ENV] = path
        dc._audit_logger = None
        for h in old_handlers:
            lg.removeHandler(h)
        dc.audit("desktop_test", "ok", point=[1, 2], text=None)
        try:
            with dc._ActionLog("desktop_click") as act:
                act.fields["target"] = {"process": "x.exe"}
                raise dc.DesktopRefused("kill switch")
        except dc.DesktopRefused:
            pass
        for h in lg.handlers:
            h.flush()
        with open(path, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh]
        assert lines[0]["tool"] == "desktop_test" and lines[0]["point"] == [1, 2] and "text" not in lines[0]
        assert lines[1]["outcome"] == "refused" and lines[1]["reason"] == "kill switch"
        assert lines[1]["target"] == {"process": "x.exe"} and "ms" in lines[1]
    finally:
        for h in list(lg.handlers):
            h.close()
            lg.removeHandler(h)
        for h in old_handlers:
            lg.addHandler(h)
        dc._audit_logger = old_logger
        if old_env is None:
            os.environ.pop(dc.LOG_ENV, None)
        else:
            os.environ[dc.LOG_ENV] = old_env
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# -- Windows structures ------------------------------------------------------------------------------

def test_windows_structs_and_dpi():
    if not dc.IS_WINDOWS:
        return
    import ctypes
    big = ctypes.sizeof(ctypes.c_void_p) == 8
    assert ctypes.sizeof(dc.INPUT) == (40 if big else 28)
    assert ctypes.sizeof(dc.MOUSEINPUT) == (32 if big else 24)
    assert ctypes.sizeof(dc.KEYBDINPUT) == (24 if big else 16)
    assert dc.dpi_awareness() in ("per_monitor_v2", "per_monitor")


# -- live smoke (read-only) ------------------------------------------------------------------------------

def live_smoke():
    """Read-only: list windows, the cursor, and screenshots of monitor 1. Sends no input."""
    assert dc.IS_WINDOWS, "the live smoke needs Windows"
    rep = dc.list_windows_report(limit=5)
    print(f"dpi awareness: {rep['dpi_awareness']}")
    for m in rep["monitors"]:
        print(f"monitor {m['index']}: {m['left']},{m['top']} {m['width']}x{m['height']} dpi {m['dpi']} "
              f"primary={m['primary']}")
    print(f"virtual screen: {rep['virtual_screen']}")
    print(f"cursor: {rep['cursor']}  kill switch active: {rep['kill_switch_active']}")
    print(f"windows: {rep['count']} visible; foreground: {rep['foreground'] and rep['foreground']['process']}")
    uefn = dc.find_windows(process="UnrealEditorFortnite")
    print(f"UEFN windows: {[(w['title'], w['class'], 'minimized' if w['minimized'] else 'shown') for w in uefn]}")
    try:
        import mss
        with mss.mss() as sct:
            ours = [(m["left"], m["top"], m["width"], m["height"]) for m in rep["monitors"]]
            theirs = [(m["left"], m["top"], m["width"], m["height"]) for m in sct.monitors[1:]]
            assert ours == theirs, (ours, theirs)
            print("monitor order and rects match mss")
    except ImportError:
        print("mss not installed: skipped the mss comparison")
    tmp = tempfile.mkdtemp(prefix="uefn-desktop-smoke-")
    m1 = rep["monitors"][0]
    for full in (False, True):
        out = os.path.join(tmp, f"monitor1_{'full' if full else 'model'}.png")
        shot = dc.screenshot(monitor=1, output_path=out, full_resolution=full)
        exp = dc.plan_capture_size(m1["width"], m1["height"], full_resolution=full)
        with open(out, "rb") as fh:
            ihdr = _parse_png(fh.read())[0][1]
        assert struct.unpack(">II", ihdr[:8]) == exp == (shot["width"], shot["height"])
        assert (shot["mapping"]["origin_x"], shot["mapping"]["origin_y"]) == (m1["left"], m1["top"])
        assert os.path.isfile(out + ".json") and dc.load_shot(out)["shot_id"] == shot["shot_id"]
        cur = shot["cursor"]
        if cur["in_image"]:
            back = dc.image_to_screen(cur["in_image"]["x"], cur["in_image"]["y"], shot["mapping"])
            assert abs(back[0] - cur["x"]) <= shot["mapping"]["scale_x"] and abs(back[1] - cur["y"]) <= shot["mapping"]["scale_y"]
        print(f"screenshot {shot['width']}x{shot['height']} (scale {shot['mapping']['scale_x']:.3f}) -> {out}")
    print("live smoke OK (read-only)")


def _run_all():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:  # report and continue
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    rc = _run_all()
    if "--live" in sys.argv[1:]:
        live_smoke()
    sys.exit(1 if rc else 0)
