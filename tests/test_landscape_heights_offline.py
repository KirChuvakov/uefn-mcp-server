"""Offline tests for landscape_heights.py: shapes, PNG round trips, resampling. No UEFN."""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import landscape_heights as lh  # noqa: E402

GRID = {"vertices": [65, 65], "scale": [100.0, 100.0, 100.0], "min_xy": [-3200.0, -3200.0], "origin": [0, 0, 0]}


def test_flat_and_units():
    v = lh.build_heights(GRID, [{"type": "flat", "height_m": 1.0}])
    assert set(v) == {lh.ZERO + 128}          # 1 m at scale.z 100 = 128 units
    assert lh.height_unit_from_cm(100.0, 0.0, 100.0) == lh.ZERO + 128
    assert lh.cm_from_height_unit(lh.ZERO - 128, 50.0, 100.0) == -50.0


def test_plateau_hill_ring_noise():
    v = lh.build_heights(GRID, [
        {"type": "plateau", "x": 0, "y": 0, "radius": 1000, "height_m": 2.0, "falloff": 500},
        {"type": "hill", "x": 2500, "y": 2500, "radius": 600, "height_m": 5.0},
        {"type": "ring", "x": 0, "y": 0, "inner": 2000, "outer": 3000, "height_m": 1.0},
        {"type": "noise", "amplitude_m": 0.5, "cell": 800, "seed": 3, "keep_inside": {"x": 0, "y": 0, "radius": 1000}},
    ])
    centre = v[32 * 65 + 32]
    assert centre == lh.ZERO + 256            # plateau kept, noise masked out
    assert max(v) > lh.ZERO + 600             # the hill
    assert len(v) == 65 * 65 and all(0 <= x <= 65535 for x in v)


def test_png_round_trips():
    d = tempfile.mkdtemp()
    vals = [(i * 997) % 65536 for i in range(37 * 23)]
    p = os.path.join(d, "rg.png")
    lh.write_rg_png(p, 37, 23, vals)
    assert lh.read_png_heights(p) == (37, 23, vals)
    p = os.path.join(d, "g16.png")
    lh.write_gray16_png(p, 37, 23, vals)
    assert lh.read_png_heights(p) == (37, 23, vals)


def test_resample():
    vals = [0, 100, 200, 300]
    assert lh.resample(vals, 2, 2, 3, 3) == [0, 50, 100, 100, 150, 200, 200, 250, 300]


class _Tools:
    def __init__(self):
        self.fns = {}

    def tool(self, **_):
        def add(fn):
            self.fns[fn.__name__] = fn
            return fn
        return add


def test_paste_keeps_a_non_ascii_clipboard():
    """The real PowerShell calls, with the clipboard replaced by a file: the user's clipboard is not touched."""
    if not shutil.which("powershell"):
        return
    import landscape_tools as lt
    d = tempfile.mkdtemp()
    clip, t3d = os.path.join(d, "clip.txt"), os.path.join(d, "land.t3d")
    original, template = "\u041f\u0440\u0438\u0432\u0435\u0442, caf\u00e9\n", "Begin Map \u00c4\u00d6\nEnd Map\n"
    for path, text in ((clip, original), (t3d, template)):
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def clipboard():
        with open(clip, encoding="utf-8", newline="") as f:
            return f.read()

    seen = {}

    def send(command, params, timeout, min_protocol):
        seen.update(command=command, clipboard=clipboard())
        return {"new_actors": []}

    tools = _Tools()
    lt.register(tools, send)
    kept = lt._CLIPBOARD_READ, lt._CLIPBOARD_WRITE
    os.environ["UEFN_TEST_CLIPBOARD"] = clip
    lt._CLIPBOARD_READ = "[IO.File]::ReadAllText($env:UEFN_TEST_CLIPBOARD, [Text.Encoding]::UTF8)"
    lt._CLIPBOARD_WRITE = ("[IO.File]::WriteAllText($env:UEFN_TEST_CLIPBOARD, $text, "
                           "(New-Object Text.UTF8Encoding($false)))")
    try:
        assert json.loads(tools.fns["landscape_paste_t3d"](t3d)) == {"new_actors": []}
    finally:
        lt._CLIPBOARD_READ, lt._CLIPBOARD_WRITE = kept
        del os.environ["UEFN_TEST_CLIPBOARD"]
    assert seen == {"command": "landscape_paste", "clipboard": template}
    assert clipboard() == original


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("ok")
