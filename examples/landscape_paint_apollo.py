"""Paint a Creative Apollo landscape like a mountain valley: an example for landscape_set_weights.

Grass on the flat (no paint: the material's base), forest floor along the ridges spreading higher up with a wide,
speckled edge, pebble stream beds with a half-sand / half-earth bank in the gullies between the ridges, pebble
patches high up. Built and tuned with an artist on a 4x4-proxy landscape (2026-11-18); see docs/landscape.md.

Layers (Creative Apollo, as named by the artist): no paint = grass, Layer1 half sand / half earth, Layer2 earth,
Layer3 sand, Layer4 asphalt, Layer5 forest floor, Layer6 pebbles. Do not use Layer1 at low weight as a soft
underlay: it renders as bare-earth blotches.

Usage (UEFN open with the listener running; the landscape's paint layers need LayerInfo assets):
    python examples/landscape_paint_apollo.py <heights.png> <out_dir> [--grid W H X0 Y0 SCALE]
<heights.png>: a 16-bit export of the same landscape (landscape_export_heights). --grid defaults to 505 505 -25200
-25200 100 (4x4 proxies of 2x2 components of 63 quads); take the values from landscape_info's grid otherwise.
"""
import sys, json, math, urllib.request, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import landscape_heights as lh
OUT = sys.argv[2] if len(sys.argv) > 2 else "."
W = H = 505; X0 = Y0 = -25200.0; S = 100.0
if "--grid" in sys.argv:
    _g = sys.argv[sys.argv.index("--grid") + 1:]
    W, H, X0, Y0, S = int(_g[0]), int(_g[1]), float(_g[2]), float(_g[3]), float(_g[4])
LAYERS = ["Layer1", "Layer2", "Layer3", "Layer4", "Layer5", "Layer6"]
MIXON = False   # Layer1 patches at low weight look like bare earth: off


def send(cmd, params, timeout=600):
    req = urllib.request.Request("http://127.0.0.1:8765", data=json.dumps({"command": cmd, "params": params}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def push(weights):
    paths = {}
    for name, vals in weights.items():
        p = os.path.join(OUT, name + ".png"); lh.write_gray8_png(p, W, H, vals); paths[name] = os.path.abspath(p)
    return send("landscape_import_weights", {"layers": paths})


def main():
    w0, h0, hv = lh.read_png_heights(sys.argv[1])
    if (w0, h0) != (W, H):
        sys.exit(f"heights PNG is {w0}x{h0}, the grid is {W}x{H}")
    hm = [(v - 32768) * 0.78125 / 100.0 for v in hv]   # metres at scale.z 100
    def sm(e0, e1, x):
        t = max(0.0, min(1.0, (x - e0) / (e1 - e0))); return t * t * (3 - 2 * t)
    def lapl(r):
        out = [0.0] * (W * H)
        for j in range(r, H - r):
            for i in range(r, W - r):
                k = j * W + i; out[k] = (hm[k - r] + hm[k + r] + hm[k - r * W] + hm[k + r * W]) / 4.0 - hm[k]
        return out
    lap5 = lapl(5); lap2 = lapl(2); lap4 = lapl(4)
    nA = lh._value_noise(21, 2200.0); nB = lh._value_noise(33, 1100.0); nC = lh._value_noise(47, 4000.0)
    nWarp = lh._value_noise(58, 1600.0); nSeg = lh._value_noise(64, 700.0)
    forest = [0.0] * (W * H); peb = [0.0] * (W * H); mix = [0.0] * (W * H); chan = [0.0] * (W * H); mountm = [0.0] * (W * H)
    for j in range(H):
        for i in range(W):
            k = j * W + i; x, y = X0 + i * S, Y0 + j * S; h = hm[k]
            a = 0.5 + 0.5 * nA(x, y); b = 0.5 + 0.5 * nB(x, y); c = 0.5 + 0.5 * nC(x, y)
            hx = hm[k + 1] - hm[k - 1] if 0 < i < W - 1 else 0.0
            hy = hm[k + W] - hm[k - W] if 0 < j < H - 1 else 0.0
            slope = math.hypot(hx, hy) / 2.0
            mount = sm(3.0, 22.0, h); mountm[k] = mount
            ridge = sm(0.1, 1.4, -lap5[k])
            # water channels: narrow concave lines on the slopes, broken into runs, a little wider lower down
            g = max(0.0, lap2[k], 0.55 * lap4[k])
            run = sm(0.32, 0.55, 0.5 + 0.5 * nSeg(x, y))
            low = 1.0 - sm(45, 75, h)
            channel = mount * sm(0.12, 0.45, slope) * sm(0.03, 0.26 - 0.08 * (1 - low), g) * run
            chan[k] = channel
            # forest: ridges first; the height band is warped by noise so it never closes into a ring
            hw = h + 18.0 * nWarp(x, y)
            spread = sm(18, 55, hw) * sm(0.3, 0.6, 0.65 * c + 0.35 * b)
            band = sm(0.25, 0.7, 0.6 * c + 0.4 * b)
            forest[k] = mount * min(1.0, ridge * (0.45 + 0.55 * sm(10, 45, hw)) * band + spread * 0.8) * (1.0 - 0.9 * channel)
            peb[k] = sm(35, 60, hw) * sm(0.55, 0.75, a) * (0.5 + 0.5 * ridge)
            mix[k] = mount * sm(0.8, 0.9, b) * 0.2 + (1 - mount) * sm(0.8, 0.92, a) * 0.2
    # widen + dither the grass/forest edge (as v6)
    def blur(src, r):
        tmp = [0.0] * (W * H); out = [0.0] * (W * H)
        for j in range(H):
            row = j * W; acc = sum(src[row + min(max(i, 0), W - 1)] for i in range(-r, r + 1))
            for i in range(W):
                tmp[row + i] = acc / (2 * r + 1); acc += src[row + min(i + r + 1, W - 1)] - src[row + max(i - r, 0)]
        for i in range(W):
            acc = sum(tmp[min(max(j, 0), H - 1) * W + i] for j in range(-r, r + 1))
            for j in range(H):
                out[j * W + i] = acc / (2 * r + 1); acc += tmp[min(j + r + 1, H - 1) * W + i] - tmp[max(j - r, 0) * W + i]
        return out
    fw = blur(blur(forest, 5), 5)
    g1 = lh._value_noise(71, 260.0); g2 = lh._value_noise(72, 110.0)
    for j in range(H):
        for i in range(W):
            k = j * W + i; x, y = X0 + i * S, Y0 + j * S; f = fw[k]
            if f <= 0.02: forest[k] = 0.0; continue
            fine = 0.6 * g1(x, y) + 0.4 * g2(x, y); t = 0.36 - 0.38 * fine
            forest[k] = sm(t - 0.12, t + 0.12, f) * (1.0 - 0.9 * chan[k])
            mix[k] = max(mix[k], 0.14 * 4.0 * f * (1 - f) * (1 - forest[k]))
    # channel bed (pebbles) with a half-sand/half-earth bank, the bank slightly wider (blur)
    bank = blur(chan, 1)
    wts = {n: [0.0] * (W * H) for n in LAYERS}
    cover = {}
    for k in range(W * H):
        cur = {}
        mixk = mix[k] if MIXON else 0.0
        layers = (("Layer5", forest[k]), ("Layer1", max(mixk, min(1.0, bank[k] * 1.3))),
                  ("Layer6", max(peb[k], sm(0.35, 0.8, chan[k]))))
        for name, al in layers:
            al = max(0.0, min(1.0, al))
            for kk in cur: cur[kk] *= (1 - al)
            cur[name] = cur.get(name, 0.0) + al
        for name, v in cur.items(): wts[name][k] = v
    print("coverage", {n: round(sum(v) / len(v), 3) for n, v in wts.items()}, "channel px", sum(1 for v in chan if v > 0.3))
    print(json.dumps(push(wts), indent=1))


if __name__ == "__main__":
    main()
