"""Landscape tools (0.6.0): read, sculpt by shapes or PNG, set the material, export / paste through T3D.

UEFN has no Python call that creates landscape components, so a new landscape comes from a T3D paste
(`landscape_export_t3d` of an existing landscape once, then `landscape_paste_t3d` wherever it is
needed) or from Landscape Mode by hand. Heights go through the listener as an RG PNG
(landscape_heights.py). See docs/landscape.md. Untested on a live editor at the time of writing.
"""

import base64
import json
import os
import struct
import subprocess
import tempfile
from typing import Any, Callable, Optional

import landscape_heights as lh
from tool_tags import experimental

LANDSCAPE_PROTOCOL = (0, 4, 0)


# Windows PowerShell pipes use the console code page, which turns non-ASCII clipboard text into "?" on stdout and
# garbles it on stdin, so clipboard text travels through UTF-8 files.
_CLIPBOARD_READ = "Get-Clipboard -Raw"
_CLIPBOARD_WRITE = "Set-Clipboard -Value $text"


def _ps(script: str, clipboard_file: str) -> None:
    """Run a PowerShell snippet in this process's session (outside UEFN)."""
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       env={**os.environ, "UEFN_CLIPBOARD_FILE": clipboard_file},
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"PowerShell failed: {r.stderr.strip()[:400]}")


def _clipboard_to_file(path: str) -> None:
    _ps(f"$text = {_CLIPBOARD_READ}; [IO.File]::WriteAllText($env:UEFN_CLIPBOARD_FILE, [string]$text, "
        "(New-Object Text.UTF8Encoding($false)))", path)


def _clipboard_from_file(path: str) -> None:
    """Put a UTF-8 file's text on the clipboard; an empty file changes nothing."""
    _ps("$text = [IO.File]::ReadAllText($env:UEFN_CLIPBOARD_FILE, [Text.Encoding]::UTF8); "
        f"if ($text) {{ {_CLIPBOARD_WRITE} }}", path)


def register(mcp, send: Callable[..., dict]) -> None:
    """Add the landscape tools. `send(command, params, timeout, min_protocol)` is mcp_server._send_command."""

    def call(command: str, params: Optional[dict] = None, timeout: float = 60.0) -> dict:
        return send(command, params or {}, timeout, LANDSCAPE_PROTOCOL)

    @mcp.tool()
    def landscape_info() -> str:
        """Landscape actors in the open level (Landscape + streaming proxies), component counts, material,
        and the vertex grid every height tool works on: origin, min_xy, scale, quads, vertices,
        cm_per_height_unit. A Landscape with 0 components is an empty shell: make the landscape in
        Landscape Mode or paste one with landscape_paste_t3d."""
        return json.dumps(call("landscape_info"), indent=2)

    @mcp.tool()
    def landscape_set_material(material_path: str, label: str = "", save: bool = True) -> str:
        """Set the landscape material (on the Landscape actor; proxies follow unless they override it).

        Args:
            material_path: Material or material instance asset path.
            label: Landscape actor label when the level has several; empty = the first Landscape.
            save: Save the landscape actor packages.
        """
        return json.dumps(call("landscape_set_material",
                               {"material_path": material_path, "label": label, "save": save}), indent=2)

    @mcp.tool()
    def landscape_export_heights(out_png: str = "", label: str = "") -> str:
        """Export the whole landscape's heights to a 16-bit grayscale PNG (one pixel per vertex,
        32768 = the landscape actor's Z, one unit = scale.z/128 cm) and report min / max in metres.

        Args:
            out_png: Output path; empty = a file in the system temp folder.
            label: Landscape actor label; empty = the first Landscape.
        """
        r = call("landscape_export_heights", {"label": label}, timeout=120.0)
        w, h = r["width"], r["height"]
        vals = list(struct.unpack(f"<{w * h}H", base64.b64decode(r["data_b64"])))
        out_png = out_png or os.path.join(tempfile.gettempdir(), "uefn_landscape_heights.png")
        lh.write_gray16_png(out_png, w, h, vals)
        g = r["grid"]
        oz, sz = g["origin"][2], g["scale"][2]
        return json.dumps({
            "png": out_png, "width": w, "height": h, "min": r["min"], "max": r["max"],
            "min_z_cm": lh.cm_from_height_unit(r["min"], oz, sz), "max_z_cm": lh.cm_from_height_unit(r["max"], oz, sz),
            "grid": g}, indent=2)

    @mcp.tool(**experimental("Overwrite landscape heights"))
    def landscape_set_heights(shapes: list[dict[str, Any]] | None = None, base_m: float = 0.0,
                              heights_png: str = "", label: str = "", save: bool = True) -> str:
        """[experimental] Ask the user to confirm before each call. Overwrite the whole landscape's heights (edit layer 0) from shapes or a PNG. Export first
        (landscape_export_heights) if the current heights should be kept.

        Shapes (world cm for positions and radii, metres above the landscape actor for heights), applied in order:
          {"type": "flat", "height_m": h}
          {"type": "plateau", "x", "y", "radius", "height_m", "falloff"}   flat circle (a base pad), soft edge
          {"type": "rect", "x", "y", "size_x", "size_y", "height_m", "falloff"}
          {"type": "hill", "x", "y", "radius", "height_m"}                 added bump; negative = dip
          {"type": "ring", "x", "y", "inner", "outer", "height_m"}         added ridge between two radii
          {"type": "noise", "amplitude_m", "cell", "seed", "keep_inside": {"x", "y", "radius"}}
          {"type": "mountain", "x", "y", "radius", "height_m", "sharpness", "ridges", "ridge_depth", "phase"}
              ridged cone merged with max(); a row of them makes a mountain range

        Args:
            shapes: Shape list (ignored when heights_png is given).
            base_m: Height of everything before the shapes, metres above the landscape actor.
            heights_png: A 16-bit (or 8-bit) grayscale PNG or an RG PNG; resampled to the vertex grid.
            label: Landscape actor label; empty = the first Landscape.
            save: Save the landscape actor packages.
        """
        info = call("landscape_info")
        grid = info.get("grid") or {}
        if "vertices" not in grid:
            raise RuntimeError(f"No usable landscape: {grid}")
        w, h = grid["vertices"]
        if heights_png:
            w0, h0, vals = lh.read_png_heights(heights_png)
            vals = lh.resample(vals, w0, h0, w, h)
        else:
            vals = lh.build_heights(grid, shapes or [], base_m)
        png = os.path.join(tempfile.gettempdir(), "uefn_landscape_heights_rg.png")
        lh.write_rg_png(png, w, h, vals)
        r = call("landscape_import_heights", {"png_path": png, "label": label, "save": save}, timeout=180.0)
        r["sent_min"], r["sent_max"] = min(vals), max(vals)
        r["match"] = (r.get("readback_min"), r.get("readback_max")) == (r["sent_min"], r["sent_max"])
        return json.dumps(r, indent=2)

    @mcp.tool(**experimental("Overwrite landscape paint layers"))
    def landscape_set_weights(layers: dict[str, str], label: str = "", save: bool = True) -> str:
        """[experimental] Ask the user to confirm before each call. Overwrite paint-layer weights:
        {layer name: 8-bit grayscale PNG sized to the vertex grid (landscape_info grid.vertices), 0..255 = weight}.
        Give every layer that should change, with weights that sum to 1 per pixel (weight-blended layers).
        The layers need LayerInfo assets (Landscape Mode > Paint > Target Layers); a layer without one is
        reported, not written. landscape_heights.write_gray8_png writes the PNGs.
        """
        return json.dumps(call("landscape_import_weights", {"layers": layers, "label": label, "save": save},
                               timeout=600.0), indent=2)

    @mcp.tool()
    def landscape_export_t3d(out_path: str, labels: list[str] | None = None) -> str:
        """Export landscape actors (all, or by label) to a T3D file: a template a later landscape_paste_t3d
        can place in another level or project. Selection is restored afterwards.

        Args:
            out_path: T3D file path (keep it outside the project folder).
            labels: Actor labels; empty = every Landscape and proxy.
        """
        return json.dumps(call("landscape_export_t3d", {"out_path": out_path, "labels": labels or []},
                               timeout=120.0), indent=2)

    @mcp.tool(**experimental("Paste a landscape from T3D"))
    def landscape_paste_t3d(t3d_path: str, offset: list[float] | None = None) -> str:
        """[experimental] Ask the user to confirm before each call. Paste landscape actors from a T3D file into the open level (EDIT PASTE with the world context).
        The clipboard is filled from outside UEFN and restored afterwards; save the level first (a paste
        crash rolls back to the last save). Returns the new actors and their component counts.

        Args:
            t3d_path: T3D file from landscape_export_t3d.
            offset: Optional [x, y, z] cm: the pasted actors are moved by it after the paste.
        """
        with open(t3d_path, "r", encoding="utf-8", errors="replace", newline="") as f:
            text = f.read()
        with tempfile.TemporaryDirectory(prefix="uefn_landscape_") as tmp:
            saved = os.path.join(tmp, "clipboard.txt")
            t3d = os.path.join(tmp, "paste.t3d")
            with open(t3d, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            _clipboard_to_file(saved)
            try:
                _clipboard_from_file(t3d)
                r = call("landscape_paste", {"offset": list(offset or [])}, timeout=300.0)
            finally:
                _clipboard_from_file(saved)
        return json.dumps(r, indent=2)
