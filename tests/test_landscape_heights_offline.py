"""Offline tests for landscape_heights.py: shapes, PNG round trips, resampling. No UEFN."""
import os
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


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("ok")
