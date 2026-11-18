"""Landscape heights on the host side: shapes -> uint16 grid -> PNG, and back. No dependencies.

A landscape vertex height is a uint16: 32768 is the landscape actor's Z, one unit is scale.z / 128 cm
(0.78 cm at the usual scale 100). The listener moves heights through RGBA8 render targets packed
R = high byte, G = low byte, so the PNG handed to it is 8-bit RGB with that packing ("RG PNG").
A 16-bit grayscale PNG (World Machine, Gaea, ...) is read and converted.
"""

import math
import random
import struct
import zlib
from typing import Dict, List, Sequence

ZERO = 32768


def height_unit_from_cm(z_cm: float, origin_z: float, scale_z: float) -> int:
    """World Z in cm -> uint16 height, clamped."""
    v = ZERO + (z_cm - origin_z) * 128.0 / scale_z
    return max(0, min(65535, int(round(v))))


def cm_from_height_unit(h: int, origin_z: float, scale_z: float) -> float:
    return origin_z + (h - ZERO) * scale_z / 128.0


def _smoothstep(e0: float, e1: float, x: float) -> float:
    if e1 == e0:
        return 1.0 if x >= e1 else 0.0
    t = max(0.0, min(1.0, (x - e0) / (e1 - e0)))
    return t * t * (3 - 2 * t)


def _value_noise(seed: int, cell: float):
    table: Dict[tuple, float] = {}

    def at(ix: int, iy: int) -> float:
        k = (ix, iy)
        if k not in table:
            table[k] = random.Random(hash((seed, ix, iy)) & 0xFFFFFFFF).random() * 2 - 1
        return table[k]

    def f(x: float, y: float) -> float:
        gx, gy = x / cell, y / cell
        ix, iy = math.floor(gx), math.floor(gy)
        tx, ty = gx - ix, gy - iy
        sx, sy = tx * tx * (3 - 2 * tx), ty * ty * (3 - 2 * ty)
        a = at(ix, iy) + (at(ix + 1, iy) - at(ix, iy)) * sx
        b = at(ix, iy + 1) + (at(ix + 1, iy + 1) - at(ix, iy + 1)) * sx
        return a + (b - a) * sy

    return f


def build_heights(grid: dict, shapes: Sequence[dict], base_m: float = 0.0) -> List[int]:
    """uint16 heights for the listener's grid from shape operations applied in order.

    grid: landscape_info's "grid" (min_xy, scale, vertices, origin).
    base_m: height of everything before the shapes, metres above the landscape actor.
    shapes (world coordinates in cm, heights in metres above the landscape actor):
      {"type": "flat", "height_m": h}                                      whole landscape
      {"type": "plateau", "x": cx, "y": cy, "radius": r, "height_m": h, "falloff": f}
          circle at h, blended into what is under it over f cm outside r
      {"type": "rect", "x": cx, "y": cy, "size_x": sx, "size_y": sy, "height_m": h, "falloff": f}
      {"type": "hill", "x": cx, "y": cy, "radius": r, "height_m": h}       added smooth bump (negative = dip)
      {"type": "ring", "x": cx, "y": cy, "inner": r0, "outer": r1, "height_m": h}
          added bump between r0 and r1 (hills around a base); smooth on both edges
      {"type": "noise", "amplitude_m": a, "cell": c, "seed": s, "keep_inside": {"x":, "y":, "radius":}}
          added value noise; optional circle left untouched (with a soft edge of `cell` cm)
      {"type": "mountain", "x": cx, "y": cy, "radius": r, "height_m": h, "sharpness": 1.6,
       "ridges": 16, "ridge_depth": 0.3, "phase": 0.0}
          a cone with concave slopes and a rounded top, its flanks cut by `ridges` radial gullies
          (deeper towards the foot); merged with max(), so neighbours overlap into a range
    """
    w, h = grid["vertices"]
    sx, sy, sz = grid["scale"]
    x0, y0 = grid["min_xy"]
    unit = 128.0 / sz * 100.0  # height units per metre
    field = [base_m] * (w * h)
    for op in shapes:
        kind = op.get("type")
        noise = None
        if kind == "noise":
            noise = _value_noise(int(op.get("seed", 1)), float(op.get("cell", 2000.0)))
        for j in range(h):
            y = y0 + j * sy
            row = j * w
            for i in range(w):
                x = x0 + i * sx
                k = row + i
                cur = field[k]
                if kind == "flat":
                    field[k] = float(op["height_m"])
                elif kind == "plateau":
                    d = math.hypot(x - op["x"], y - op["y"])
                    r, fo = float(op["radius"]), float(op.get("falloff", 0.0))
                    t = 1.0 if d <= r else (1.0 - _smoothstep(r, r + fo, d) if fo > 0 else 0.0)
                    field[k] = cur + (float(op["height_m"]) - cur) * t
                elif kind == "rect":
                    dx = max(0.0, abs(x - op["x"]) - op["size_x"] / 2.0)
                    dy = max(0.0, abs(y - op["y"]) - op["size_y"] / 2.0)
                    d, fo = math.hypot(dx, dy), float(op.get("falloff", 0.0))
                    t = 1.0 if d == 0 else (1.0 - _smoothstep(0.0, fo, d) if fo > 0 else 0.0)
                    field[k] = cur + (float(op["height_m"]) - cur) * t
                elif kind == "hill":
                    d = math.hypot(x - op["x"], y - op["y"]) / float(op["radius"])
                    if d < 1.0:
                        field[k] = cur + float(op["height_m"]) * (0.5 + 0.5 * math.cos(math.pi * d))
                elif kind == "ring":
                    d = math.hypot(x - op["x"], y - op["y"])
                    r0, r1 = float(op["inner"]), float(op["outer"])
                    if r0 < d < r1:
                        t = (d - r0) / (r1 - r0)
                        field[k] = cur + float(op["height_m"]) * math.sin(math.pi * t)
                elif kind == "noise":
                    a = float(op["amplitude_m"]) * noise(x, y)
                    keep = op.get("keep_inside")
                    if keep:
                        d = math.hypot(x - keep["x"], y - keep["y"])
                        a *= _smoothstep(keep["radius"], keep["radius"] + float(op.get("cell", 2000.0)), d)
                    field[k] = cur + a
                elif kind == "mountain":
                    dx, dy = x - op["x"], y - op["y"]
                    d = math.hypot(dx, dy) / float(op["radius"])
                    if d < 1.0:
                        sh = float(op.get("sharpness", 1.6))
                        prof = (1.0 - d) ** sh
                        if d < 0.12:  # round the summit
                            top = (1.0 - 0.12) ** sh
                            prof = top + (1.0 - top) * (0.5 + 0.5 * math.cos(math.pi * d / 0.12)) * 0.6
                        n = int(op.get("ridges", 16))
                        if n > 0:
                            th = math.atan2(dy, dx) * n + float(op.get("phase", 0.0))
                            gully = 0.5 * (1.0 - math.cos(th))
                            prof *= 1.0 - float(op.get("ridge_depth", 0.3)) * d * gully
                        field[k] = max(cur, float(op["height_m"]) * prof)
                else:
                    raise ValueError(f"Unknown shape type: {kind!r}")
    return [max(0, min(65535, int(round(ZERO + m * unit)))) for m in field]


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write_rg_png(path: str, width: int, height: int, values: Sequence[int]) -> None:
    """8-bit RGB PNG with R = high byte, G = low byte, B = 0."""
    if len(values) != width * height:
        raise ValueError(f"{len(values)} values for {width}x{height}")
    raw = bytearray()
    for j in range(height):
        raw.append(0)
        for v in values[j * width:(j + 1) * width]:
            raw += bytes((v >> 8, v & 0xFF, 0))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
                + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _png_chunk(b"IEND", b""))


def write_gray8_png(path: str, width: int, height: int, values: Sequence[float]) -> None:
    """8-bit grayscale PNG from 0..1 floats (paint-layer weights)."""
    raw = bytearray()
    for j in range(height):
        raw.append(0)
        raw += bytes(max(0, min(255, int(round(v * 255)))) for v in values[j * width:(j + 1) * width])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
                + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _png_chunk(b"IEND", b""))


def write_gray16_png(path: str, width: int, height: int, values: Sequence[int]) -> None:
    """16-bit grayscale PNG (what terrain tools read)."""
    raw = bytearray()
    for j in range(height):
        raw.append(0)
        for v in values[j * width:(j + 1) * width]:
            raw += struct.pack(">H", v)
    ihdr = struct.pack(">IIBBBBB", width, height, 16, 0, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
                + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _png_chunk(b"IEND", b""))


def _unfilter(data: bytes, width: int, height: int, bpp: int) -> bytes:
    stride = width * bpp
    out = bytearray()
    prev = bytearray(stride)
    pos = 0
    for _ in range(height):
        ft = data[pos]
        line = bytearray(data[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if ft == 1:
                line[i] = (line[i] + a) & 0xFF
            elif ft == 2:
                line[i] = (line[i] + b) & 0xFF
            elif ft == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 0xFF
            elif ft == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
            elif ft != 0:
                raise ValueError(f"Bad PNG filter {ft}")
        out += line
        prev = line
    return bytes(out)


def read_png_heights(path: str):
    """(width, height, uint16 values) from a 16-bit or 8-bit grayscale PNG, or an RG PNG (8-bit RGB/RGBA)."""
    with open(path, "rb") as f:
        blob = f.read()
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Not a PNG")
    pos, idat, ihdr = 8, bytearray(), None
    while pos < len(blob):
        ln, tag = struct.unpack(">I4s", blob[pos:pos + 8])
        data = blob[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", data)
        elif tag == b"IDAT":
            idat += data
        pos += 12 + ln
    width, height, depth, ctype, _, _, interlace = ihdr
    if interlace:
        raise ValueError("Interlaced PNG is not supported")
    channels = {0: 1, 2: 3, 6: 4}.get(ctype)
    if channels is None or depth not in (8, 16):
        raise ValueError(f"Unsupported PNG: colour type {ctype}, depth {depth}")
    bpp = channels * depth // 8
    px = _unfilter(zlib.decompress(bytes(idat)), width, height, bpp)
    vals: List[int] = []
    if ctype == 0 and depth == 16:
        vals = list(struct.unpack(f">{width * height}H", px))
    elif ctype == 0:
        vals = [v * 257 for v in px]
    elif depth == 8:
        vals = [(px[k] << 8) | px[k + 1] for k in range(0, len(px), bpp)]
    else:
        vals = [struct.unpack(">H", px[k:k + 2])[0] for k in range(0, len(px), bpp)]
    return width, height, vals


def resample(values: Sequence[int], w0: int, h0: int, w1: int, h1: int) -> List[int]:
    """Bilinear resample of a height grid to the landscape's vertex grid."""
    if (w0, h0) == (w1, h1):
        return list(values)
    out = []
    for j in range(h1):
        fy = j * (h0 - 1) / max(1, h1 - 1)
        y0 = int(fy); y1 = min(h0 - 1, y0 + 1); ty = fy - y0
        for i in range(w1):
            fx = i * (w0 - 1) / max(1, w1 - 1)
            x0 = int(fx); x1 = min(w0 - 1, x0 + 1); tx = fx - x0
            a = values[y0 * w0 + x0] + (values[y0 * w0 + x1] - values[y0 * w0 + x0]) * tx
            b = values[y1 * w0 + x0] + (values[y1 * w0 + x1] - values[y1 * w0 + x0]) * tx
            out.append(int(round(a + (b - a) * ty)))
    return out
