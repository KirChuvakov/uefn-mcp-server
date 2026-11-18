# Landscape tools (0.6.0, listener protocol 0.4.0)

UEFN's Python API has no call that creates landscape components (no `LandscapeEditorSubsystem`, no
create / add-component function; checked against the full generated stub). These tools work around it:

| Tool | Listener command | What it does |
|---|---|---|
| `landscape_info` | `landscape_info` | Landscape + proxies, component counts, material, the vertex grid (origin, min_xy, scale, quads, vertices) |
| `landscape_set_material` | `landscape_set_material` | Sets `landscape_material` on the Landscape actor, saves the packages |
| `landscape_export_heights` | `landscape_export_heights` | Whole landscape -> 16-bit grayscale PNG on the host, min / max in cm |
| `landscape_set_heights` [experimental] | `landscape_import_heights` | Shapes (flat, plateau, rect, hill, ring, noise) or a PNG -> RG PNG -> texture -> material -> RGBA32F render target -> `landscape_import_heightmap_from_render_target` (edit layer 0); reads back min / max |
| `landscape_export_t3d` | `landscape_export_t3d` | Landscape actors -> T3D file (LevelExporterT3D, selected only): a template |
| `landscape_paste_t3d` [experimental] | `landscape_paste` | Clipboard filled from outside UEFN, `EDIT PASTE` with the world context, new actors moved by `offset`, clipboard restored |

Heights: uint16, 32768 = the landscape actor's Z, one unit = scale.z / 128 cm. The listener moves them through
RTF_RGBA32F render targets with the RG flag OFF: the raw uint16 sits in R (verified 2026-11-18; the flag ON packs
R = high byte, G = low byte as 0..1 values, and RTF_RGBA8 with the flag off exports 255 everywhere). The host
sends an RG-packed 8-bit PNG; `M_LandscapeHeightWriter` (unlit, emissive = dot(tex.rgb, (65280, 255, 0)))
decodes it into the float target. Helper assets live in `/<mount>/_LandscapeTools/` (`T_LandscapeHeights`,
re-imported each call).

`LandscapeComponent` has no reflected `bounds` and no `component_size_quads` in UEFN: the grid comes from the
proxies' actor bounds and the component size from the step between `section_base_x` values.

## Verified live (UEFN, 2026-11-18, a 4x4-proxy World Partition landscape, 64 components of 63 quads)

- `landscape_info`: grid 505 x 505, origin (-25200, -25200).
- `landscape_export_heights`: real heights (0 .. 109 m) to a 16-bit PNG; the check point matched the RG export.
- `landscape_set_heights` with flat + ring + 20 hills + a centre plateau: `match` true (32768 .. 38474 sent and
  read back), 17 packages saved, the shape correct on a render. Painted layers (rock) stay where they were.

## Still to check

Items 1-3 below passed on 2026-11-18; 4 and 5 are open.

1. `landscape_info` on a level with a landscape: the grid's `vertices` must equal components x
   component_size_quads + 1 per axis (4x4 components of 63 quads -> 253 x 253).
2. `landscape_export_heights` on a flat landscape: min = max = 32768 (R = 128, G = 0). Another value means the
   RGBA8 packing is not R-high / G-low; fix `(c.r << 8) | c.g` in the listener and `write_rg_png` together.
3. `landscape_set_heights` with `[{"type": "plateau", "x": 0, "y": 0, "radius": 4000, "height_m": 1, "falloff": 1500}]`:
   `match` true, the plateau visible on a render at the landscape centre and not mirrored (X along texture
   columns, Y along rows). Then `[{"type": "flat", "height_m": 0}]` puts it back.
   - If the texture import is not exact (sRGB, compression, filtering), `readback` drifts by a few units: check
     `T_LandscapeHeights` settings (VectorDisplacementmap, sRGB off, nearest, no mips, NPOT kept).
   - World Partition: the import runs on the Landscape actor with all proxies; check every proxy changed.
4. The heights reach disk: reopen the level, export again, compare.
5. `landscape_export_t3d` of the 4x4 landscape: does the file hold `LandscapeHeightData` per component? Then
   `landscape_paste_t3d` with an offset outside the existing landscape, in a scratch level or after a save:
   a new landscape with components and the same heights, or a refusal. Record what UEFN does with the
   landscape GUID and edit layers. If the paste works, this is how a new landscape is made without Landscape
   Mode; if not, Landscape Mode stays the only route and the paste tool should be removed.

## Paint layers: blocked by unassigned layer infos (2026-11-18)

The Creative Apollo landscape material has target layers Layer1..Layer7 and ready LayerInfo assets in
`/Game/Creative/Materials/Landscape/Creative_Landscape_Apollo/LayerInfo/LayerN_LayerInfo`, but a landscape made in
Landscape Mode starts with every target layer unassigned (`target_layers` shows `{}` values,
`get_target_layer_names()` returns only the visibility layer). Then `render_weightmap` and
`landscape_import_weightmap_from_render_target` fail for every layer. `target_layers` is read-only from Python and
unreadable through unreal-mcp ObjectToolset, so the assignment is one manual step in Landscape Mode > Paint > Target
Layers (pick the existing LayerN_LayerInfo per layer). After it, a weights tool can write through
`landscape_import_weightmap_from_render_target`. The material also blends rock onto steep slopes by itself.

### The other route: colour from height and slope (verified 2026-11-18)

A landscape material that computes its colour itself needs no paint layers at all. Built from Python in the project
(`/<mount>/Landscape/M_RetroLandscape` + `MI_RetroLandscape`) and applied with `landscape_set_material`:
WorldPosition Z and VertexNormalWS Z drive four masks (snow above `SnowStartCm`, rock on slopes steeper than
`RockSlopeStart` above `RockMinHeightCm`, a lighter ridge colour with height, two grass colours mixed by a world
Noise node), chained with Lerps into Base Color. Every colour and threshold is an MI parameter. No Custom node
(validation rejects it). It drops the Creative material's grass meshes and textures: a stylised, painted look.

## Paint layers (verified 2026-11-18, after the owner assigned LayerInfo assets)

`landscape_set_weights` -> listener `landscape_import_weights`: one 8-bit grayscale PNG per layer -> texture ->
`M_LandscapeWeightWriter` (emissive = R) -> RTF_RGBA32F -> `landscape_import_weightmap_from_render_target`. Returned
True for all six layers and changed the look. `render_weightmap` (runtime) returns False in the editor: no
weight read-back yet. Creative Apollo layers as the owner named them (distant renders misled: name layers from close shots and confirm):
no paint = grass (the base), Layer1 half sand / half earth, Layer2 earth, Layer3 sand, Layer4 asphalt, Layer5
forest floor (needles, cones), Layer6 pebbles. Partial weights blend over the grass base. The Apollo material puts rock on
steep slopes by itself, whatever is painted, and no MI parameter tried changed that: keep slopes under ~35 degrees
where grass should show. The weights used (by height, slope, value noise, a path ring) are in the project's
`RetroTycoon_Scripts/landscape_paint_v3.py`.
