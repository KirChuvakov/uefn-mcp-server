# Tools Reference

121 tools in 0.5.0 (unreleased; 107 in 0.4.0). The tools up to v0.3.1 have full pages below; the 0.4.0 additions are summarized in [New in v0.4.0](#new-in-v040-summary) and the 0.5.0 desktop and UEFN session tools in [New in v0.5.0](#new-in-v050-desktop-control-and-uefn-session) (each tool's docstring, which Claude Code shows, documents every parameter). Listener tools map 1:1 to a listener command; the Verse build and navigation tools talk to UEFN directly; the desktop and session tools run in the MCP server process.

## Conventions (0.5.0)

### Rotations: named axes

Every rotation parameter (`spawn_actor`, `set_actor_transform`, `set_viewport_camera`, `niagara_place_actor`,
`staticmesh_generate_uv` `orientation`, rotator values of `device_set_editable`) takes **named axes in degrees**:

```json
{"pitch": -10, "yaw": 90, "roll": 0}
```

- Missing axes are 0 (`{"yaw": 90}` is a pure turn). The whole rotation is replaced, not merged with the old one.
  Pitch turns around Y (positive = nose up), yaw around Z (the heading), roll around X. Tool results report
  rotations in the same form, so a `get_viewport_camera` result can be passed back as is.
- Unknown keys, non-numbers and NaN / infinity are refused.
- **Lists are refused unless all three values are equal** (`[0, 0, 0]`). Before 0.5.0 the tools documented
  `[pitch, yaw, roll]` but passed the list positionally to `unreal.Rotator`, whose Python constructor is
  `Rotator(roll, pitch, yaw)`, so UEFN applied it as `[roll, pitch, yaw]` with no error. The refusal names both
  readings, e.g. for `[0, 90, 0]`: `{"pitch": 0, "yaw": 90, "roll": 0}` (what the docs meant) or
  `{"pitch": 90, "yaw": 0, "roll": 0}` (what the old tools did).
- In `execute_python`, always build rotators with keywords: `unreal.Rotator(roll=0.0, pitch=-30.0, yaw=45.0)`.
- The listener reports protocol 0.3.3 for this convention. The MCP server does not send a rotation, a
  `focus_selected` or a `staticmesh_*` command to an older listener ("needs listener protocol 0.3.3 or later"):
  reload `uefn_listener.py` (reopen the project, or Tools > Execute Python Script).

### Static meshes: only crash-safe reads (UEFN 42.20)

UEFN 42.20 died with `EXCEPTION_ACCESS_VIOLATION` (reading `0x18` in the Engine DLL) on one read-only probe of a
project mesh that called `StaticMeshEditorSubsystem.get_lod_count / get_number_verts / get_number_materials /
get_simple_collision_count / get_collision_complexity / get_convex_collision_count / get_lod_screen_sizes /
get_nanite_settings / has_vertex_colors / get_num_uv_channels`, `StaticMesh.get_num_triangles / get_num_sections` and
`BodySetup.agg_geom.export_text()` (likeliest culprit `has_vertex_colors`). The `staticmesh_*` tools call none of
them:

- `staticmesh_get_info` reads the asset-registry tags (`Triangles`, `Vertices`, `UVChannels`, `LODs`,
  `CollisionPrims`, `CollisionComplexity`, `NaniteEnabled`, ...; raw values under `registry`), the `static_materials`
  slots, `get_bounding_box()` and the `nanite_settings` property. `simple_collision_count`, `convex_collision_count`,
  `has_vertex_colors` and `lod_screen_sizes` come back as **"not available safely in UEFN 42.20"**
  (`collision_prims` counts simple and convex shapes together). A read that fails in Python is reported under
  `read_errors` instead of failing the call.
- `staticmesh_remove_lods` / `staticmesh_remove_collisions` return `removed` and `saved` and no longer read the
  count back (`lod_count` / `simple_collision_count` carry the same marker): call `staticmesh_get_info` separately.
- `staticmesh_enable_nanite` starts from the mesh's `nanite_settings` property (other Nanite settings kept) and
  returns `nanite_settings_after`.
- Never call those getters from `execute_python` either. `staticmesh_set_lods` once crashed a long session
  (2026-08-24, cause unknown): save first and run it on one mesh per call.

Both conventions are verified offline (`tests/test_rotation_offline.py`, `tests/test_staticmesh_safety_offline.py`);
the live check in UEFN, `tests/test_safety_fixes_live.py`, is pending.

---

## System

### `ping`

Check if the UEFN editor listener is running and responsive.

**Parameters:** none

**Response:**
```json
{
  "status": "ok",
  "python_version": "3.11.8 ...",
  "port": 8765,
  "timestamp": 1710892800.0,
  "commands": ["ping", "get_log", "execute_python", ...]
}
```

---

### `execute_python`

Execute arbitrary Python code inside the UEFN editor. This is the most powerful tool — it can do anything the `unreal` module supports.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `code` | string | yes | Python code to execute |

**Pre-populated globals:**

| Variable | Value |
|----------|-------|
| `unreal` | The `unreal` module |
| `actor_sub` | `unreal.get_editor_subsystem(unreal.EditorActorSubsystem)` |
| `asset_sub` | `unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)` |
| `level_sub` | `unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)` |
| `result` | Assign to this to return a value |

**Response fields:**

| Field | Description |
|-------|-------------|
| `result` | Value of the `result` variable after execution (JSON-serialized) |
| `stdout` | Captured `print()` output |
| `stderr` | Captured error output / tracebacks |

**Examples:**

```python
# Get the world name
result = unreal.EditorLevelLibrary.get_editor_world().get_name()
```

```python
# List all StaticMeshActor labels
actors = actor_sub.get_all_level_actors()
result = [a.get_actor_label() for a in actors if a.get_class().get_name() == 'StaticMeshActor']
```

```python
# Create a material
mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
    'M_Test', '/Game/Materials', unreal.Material, unreal.MaterialFactoryNew()
)
result = str(mat.get_path_name())
```

```python
# Batch rename selected assets with prefix
selected = unreal.EditorUtilityLibrary.get_selected_assets()
renamed = []
for asset in selected:
    name = asset.get_name()
    if not name.startswith('T_'):
        old_path = asset.get_path_name()
        folder = unreal.Paths.get_path(old_path)
        unreal.EditorAssetLibrary.rename_asset(old_path, folder + '/T_' + name)
        renamed.append(name)
result = {"renamed": renamed, "count": len(renamed)}
```

---

### `get_log`

Get recent MCP listener log entries.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `last_n` | int | no | 50 | Number of recent log lines to return |

**Response:**
```json
{
  "lines": [
    "[MCP] Listener started on http://127.0.0.1:8765",
    "[MCP] Registered 22 command handlers",
    ...
  ]
}
```

---

## Actors

### `get_all_actors`

List all actors in the current level.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `class_filter` | string | no | `""` | Filter by class name (e.g. `StaticMeshActor`, `PointLight`) |

**Response:**
```json
{
  "actors": [
    {
      "name": "StaticMeshActor_0",
      "label": "Cube",
      "class": "StaticMeshActor",
      "path": "/Game/Maps/TestLevel.TestLevel:PersistentLevel.StaticMeshActor_0",
      "location": {"x": 100.0, "y": 200.0, "z": 0.0},
      "rotation": {"pitch": 0.0, "yaw": 45.0, "roll": 0.0},
      "scale": {"x": 1.0, "y": 1.0, "z": 1.0}
    }
  ],
  "count": 1
}
```

---

### `get_selected_actors`

Get currently selected actors in the viewport.

**Parameters:** none

**Response:** Same format as `get_all_actors`.

---

### `spawn_actor`

Spawn an actor in the current level. Provide either `asset_path` OR `actor_class` (not both).

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `asset_path` | string | no | `""` | Asset to spawn (e.g. `/Engine/BasicShapes/Cube`) |
| `actor_class` | string | no | `""` | UE class name (e.g. `PointLight`, `CameraActor`) |
| `location` | float[3] | no | `[0,0,0]` | World position `[x, y, z]` |
| `rotation` | object | no | zero | Named axes in degrees `{"pitch", "yaw", "roll"}`, missing axes 0 ([Rotations](#rotations-named-axes)) |

**Examples:**

Spawn a cube at position (500, 0, 100):
```json
{"asset_path": "/Engine/BasicShapes/Cube", "location": [500, 0, 100]}
```

Spawn a cube turned 90 degrees to the left:
```json
{"asset_path": "/Engine/BasicShapes/Cube", "location": [500, 0, 100], "rotation": {"yaw": 90}}
```

Spawn a point light:
```json
{"actor_class": "PointLight", "location": [0, 0, 300]}
```

**Response:**
```json
{
  "actor": {
    "name": "StaticMeshActor_1",
    "label": "Cube",
    "class": "StaticMeshActor",
    "path": "...",
    "location": {"x": 500.0, "y": 0.0, "z": 100.0},
    "rotation": {"pitch": 0.0, "yaw": 0.0, "roll": 0.0},
    "scale": {"x": 1.0, "y": 1.0, "z": 1.0}
  }
}
```

---

### `delete_actors`

Delete actors by path name or label.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `actor_paths` | string[] | yes | Actor path names or labels to delete |

**Response:**
```json
{
  "deleted": ["/Game/Maps/Level.Level:PersistentLevel.StaticMeshActor_0"],
  "count": 1
}
```

---

### `set_actor_transform`

Set an actor's location, rotation, and/or scale. Only provided fields are changed.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `actor_path` | string | yes | Actor path name or label |
| `location` | float[3] | no | `[x, y, z]` world coordinates |
| `rotation` | object | no | Named axes in degrees `{"pitch", "yaw", "roll"}`, missing axes 0; replaces the whole rotation |
| `scale` | float[3] | no | `[x, y, z]` scale factors |

**Response:** The updated actor object (same format as `spawn_actor`).

---

### `get_actor_properties`

Read specific properties from an actor using `get_editor_property()`.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `actor_path` | string | yes | Actor path name or label |
| `properties` | string[] | yes | Property names to read |

**Response:**
```json
{
  "actor_path": "Cube",
  "properties": {
    "static_mesh_component": "/Game/Maps/Level...:StaticMeshComponent_0",
    "mobility": "EComponentMobility.STATIC"
  }
}
```

---

## Assets

### `list_assets`

List assets in a content directory.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `directory` | string | no | `/Game/` | Content path to list |
| `recursive` | bool | no | `true` | Include subdirectories |
| `class_filter` | string | no | `""` | Filter by class (e.g. `Material`, `StaticMesh`) |

**Response:**
```json
{
  "assets": [
    "/Game/Materials/M_Base",
    "/Game/Materials/M_Ground"
  ],
  "count": 2
}
```

---

### `get_asset_info`

Get detailed info about a specific asset.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `asset_path` | string | yes | Full asset path |

**Response:**
```json
{
  "asset": {
    "asset_name": "M_Base",
    "asset_class": "Material",
    "package_name": "/Game/Materials/M_Base",
    "package_path": "/Game/Materials",
    "object_path": "Material'/Game/Materials/M_Base.M_Base'"
  }
}
```

---

### `get_selected_assets`

Get assets currently selected in the Content Browser.

**Parameters:** none

**Response:**
```json
{
  "assets": ["/Game/Materials/M_Base", "/Game/Textures/T_Wood"],
  "count": 2
}
```

---

### `rename_asset`

Rename or move an asset.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `old_path` | string | yes | Current asset path |
| `new_path` | string | yes | New asset path |

**Response:**
```json
{
  "success": true,
  "old_path": "/Game/Materials/OldName",
  "new_path": "/Game/Materials/M_NewName"
}
```

---

### `delete_asset`

Delete an asset.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `asset_path` | string | yes | Asset path to delete |

**Response:**
```json
{"success": true, "asset_path": "/Game/Materials/M_Unused"}
```

---

### `duplicate_asset`

Duplicate an asset to a new path.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `source_path` | string | yes | Source asset path |
| `dest_path` | string | yes | Destination path |

**Response:**
```json
{
  "success": true,
  "source": "/Game/Materials/M_Base",
  "dest": "/Game/Materials/M_Base_Copy"
}
```

---

### `does_asset_exist`

Check if an asset exists.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `asset_path` | string | yes | Asset path to check |

**Response:**
```json
{"exists": true, "asset_path": "/Game/Materials/M_Base"}
```

---

### `save_asset`

Save a modified asset.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `asset_path` | string | yes | Asset path to save |

**Response:**
```json
{"success": true, "asset_path": "/Game/Materials/M_Base"}
```

---

### `search_assets`

Search for assets using the Asset Registry with class and path filters.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `class_name` | string | no | `""` | Class filter (e.g. `Material`, `Texture2D`) |
| `directory` | string | no | `/Game/` | Directory to search |
| `recursive` | bool | no | `true` | Include subdirectories |

**Response:**
```json
{
  "assets": [
    {"asset_name": "M_Base", "asset_class": "Material", ...},
    {"asset_name": "M_Ground", "asset_class": "Material", ...}
  ],
  "count": 2
}
```

---

## Level

### `save_current_level`

Save the current level.

**Parameters:** none

**Response:**
```json
{"success": true}
```

---

### `get_level_info`

Get basic info about the current level.

**Parameters:** none

**Response:**
```json
{
  "world_name": "TestLevel",
  "actor_count": 156
}
```

---

## Viewport

### `get_viewport_camera`

Get the current viewport camera position and rotation.

**Parameters:** none

**Response:**
```json
{
  "location": {"x": 500.0, "y": -200.0, "z": 300.0},
  "rotation": {"pitch": -30.0, "yaw": 45.0, "roll": 0.0}
}
```

---

### `set_viewport_camera`

Move the viewport camera. Only provided fields are changed.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `location` | float[3] | no | `[x, y, z]` world coordinates |
| `rotation` | object | no | Named axes in degrees, e.g. `{"pitch": -90}` looks straight down; missing axes 0 |

**Response:** The new camera position (same format as `get_viewport_camera`, whose `rotation` can be passed back
as is).

---

## New in v0.2.0

### `shutdown`

Gracefully stop the listener, freeing the port. The listener finishes the current request before shutting down.

**Parameters:** none

**Response:**
```json
{ "status": "shutting_down", "port": 8765 }
```

---

### `set_actor_properties`

Set properties on an actor via `set_editor_property()`.

> **Note:** UEFN uses Fort\*-prefixed actor classes. Not all properties are writable — some are read-only or don't exist on Fort\* actors. For methods like `set_actor_hidden_in_game()`, use `execute_python` instead.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `actor_path` | string | yes | Actor path name or label |
| `properties` | object | yes | Dict of property names to values |

**Response:**
```json
{ "actor_path": "Cube", "properties": { "cast_shadow": "ok" } }
```

---

### `select_actors`

Programmatically select actors in the UEFN viewport.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `actor_paths` | string[] | yes | List of actor path names or labels |
| `add_to_selection` | bool | no | Add to current selection instead of replacing (default: false) |

**Response:**
```json
{ "selected": ["Cube", "Cube2"], "count": 2 }
```

---

### `focus_selected`

Move the viewport camera to focus on the currently selected actors (like pressing F in the editor). The camera
sits above and behind the selection's center and looks at it (pitch -35, yaw 45; before 0.5.0 it was built with the
axes swapped and looked up at the sky).

**Parameters:** none

**Response:**
```json
{ "center": { "x": 100, "y": 200, "z": 50 }, "camera": { "x": ..., "y": ..., "z": ... },
  "rotation": { "pitch": -35.0, "yaw": 45.0, "roll": 0.0 }, "actors_count": 2 }
```

---

### `get_editor_log`

Read recent lines from the Unreal Editor Output Log file (not the MCP log — the full editor log).

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `last_n` | int | no | Number of recent lines (default: 100) |
| `filter_str` | string | no | Only lines containing this string (case-insensitive) |

**Response:** Newline-joined log lines.

---

### `get_project_info`

Get the UEFN project name and content root path. Use this to determine the correct base path for asset operations — in UEFN the content root is `/{ProjectName}/`, **not** `/Game/`.

**Parameters:** none

**Response:**
```json
{ "project_name": "MyProject", "content_root": "/MyProject/", "project_dir": "../../../FortniteGame/" }
```

---

## New in v0.3.1

Play-In-Editor control, random mesh scatter, and Verse source introspection (5 tools for parsing `.verse` files and analysing the service-locator / `@editable` / resource-enum layout of a UEFN Creative project).

### `playtest_start`

Start a Play-In-Editor session (equivalent to the UEFN Play button). No-op if PIE is already running. Uses `LevelEditorSubsystem.editor_request_begin_play()`.

**Parameters:** none

**Response:**
```json
{ "status": "started" }
```
or `{ "status": "already_running" }`.

---

### `playtest_stop`

End the current PIE session. No-op if PIE is not running.

**Parameters:** none

**Response:**
```json
{ "status": "stopping" }
```
or `{ "status": "not_running" }`.

---

### `playtest_status`

Report whether a PIE session is currently running.

**Parameters:** none

**Response:**
```json
{ "in_pie": false }
```

---

### `mesh_scatter`

Scatter StaticMeshActor instances randomly inside an axis-aligned box. Deterministic when `seed` is non-zero. `clearance_radius` enforces a minimum XY distance between placed instances via brute-force rejection — practical up to a few hundred points.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `static_mesh_path` | string | yes | Path to a StaticMesh asset. |
| `min_xyz` | float[3] | yes | `[x, y, z]` min corner of the scatter volume. |
| `max_xyz` | float[3] | yes | `[x, y, z]` max corner of the scatter volume. |
| `count` | int | no | Desired number of instances (default: 100). |
| `seed` | int | no | RNG seed. `0` = non-deterministic (default: 0). |
| `scale_min` | float | no | Uniform scale lower bound (default: 1.0). |
| `scale_max` | float | no | Uniform scale upper bound (default: 1.0). |
| `yaw_random` | bool | no | Randomize yaw in `[0, 360)` (default: true). |
| `pitch_random` | bool | no | Also randomize pitch in `[-15, 15]` for organic tilt (default: false). |
| `clearance_radius` | float | no | Min XY distance between instances in cm. `0` disables (default: 0.0). |
| `folder_path` | string | no | Outliner folder for spawned actors. |
| `max_attempts` | int | no | Placement attempt cap. Defaults to `max(count*10, 50)`. |
| `material_path` | string | no | Optional material applied to slot 0 of each spawned mesh. |
| `collision_profile` | string | no | Optional collision profile name (e.g. `"NoCollision"`). |

**Response:**
```json
{
  "placed": 100,
  "attempted": 134,
  "skipped_clearance": 34,
  "actors": [{"label": "Cube_42", "path": "..."}, ...]
}
```

---

### `verse_list_services`

List all Verse classes implementing `i_service` across the project. Scans `.verse` files under the UEFN plugin content dir (excluding `Intermediate/`, `Saved/`, `Binaries/`). Resolves the project root via `PluginBlueprintLibrary.get_plugin_content_dir()` — `unreal.Paths.project_dir()` returns Fortnite's install, not the user's UEFN project.

**Parameters:** none

**Response:**
```json
{
  "count": 62,
  "services": [
    {
      "name": "accolade_service",
      "rel_path": "Analytics/accolade_service.verse",
      "line": 12,
      "parents": ["i_service", "i_player_listener"],
      "is_initializable": false,
      "is_player_listener": true,
      "is_character_listener": false
    },
    ...
  ]
}
```

---

### `verse_list_editables`

List `@editable` fields grouped by their enclosing Verse class.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `class_filter` | string | no | Case-insensitive substring match on class name. Empty = all classes with @editable fields. |

**Response:**
```json
{
  "class_count": 1,
  "field_count": 24,
  "by_class": {
    "world_accessor_device": {
      "rel_path": "_world_accessor_device.verse",
      "line": 14,
      "parents": ["creative_device"],
      "fields": [
        {"name": "AssetDatabase", "type": "asset_database", "line": 16},
        ...
      ]
    }
  }
}
```

---

### `verse_service_graph`

Parse the composition-root file and extract the DI graph. Follows the project convention: `Name := class_name:` archetype blocks with indented `Field := Source` dependency wiring. Heuristic parser — brittle if the file diverges from the convention. Skips declarations like `Foo := class:` (Verse keyword in `cls` position filtered out).

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `installer_filename` | string | no | Basename to search for. First match wins (default: `_service_installer.verse`). |

**Response:**
```json
{
  "path": "_service_installer.verse",
  "count": 63,
  "services": [
    {
      "name": "TimeService",
      "class": "time_service",
      "line": 24,
      "deps": [{"field": "Config", "source": "World.TimeServiceConfig"}]
    },
    ...
  ]
}
```

---

### `verse_find_resource_usage`

Find usages of each variant of a Verse enum across all `.verse` files. Defaults target the project's `resource` enum (Money, Crystal, etc.).

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `enum_name` | string | no | Enum type name (default: `"resource"`). |
| `enum_filename` | string | no | Basename of the file containing the enum declaration (default: `"_resource_type.verse"`). |
| `max_sites_per_variant` | int | no | Cap per-variant site list (count is always the true total). (default: 20). |

**Response:**
```json
{
  "enum": "resource",
  "variants": ["Money", "Crystal", "RebirthAmount", ...],
  "by_variant": {
    "Money": {
      "count": 62,
      "sites": [
        {"rel_path": "Core/resource_service.verse", "line": 42, "text": "..."},
        ...
      ]
    },
    ...
  }
}
```

---

### `verse_check_editable_coverage`

Source-side audit: find `@editable` fields of a config class that are never referenced anywhere else in the project. UEFN's ScriptDevice bindings block reading Verse `@editable` values from Python, so runtime cross-check against the live level isn't possible. Instead this scans `.verse` sources: for each field it counts references across the project and flags fields with zero references as potentially unused — useful for spotting forgotten config slots after a refactor.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `config_class` | string | no | Verse class name to audit (default: `"world_accessor_device"`). |

**Response:**
```json
{
  "config_class": "world_accessor_device",
  "rel_path": "_world_accessor_device.verse",
  "total": 24,
  "unused_count": 0,
  "fields": [
    {
      "name": "AssetDatabase",
      "type": "asset_database",
      "line": 16,
      "ref_count": 40,
      "unused": false,
      "sites": [{"rel_path": "...", "line": 42, "text": "..."}, ...]
    },
    ...
  ]
}
```

---

## New in v0.4.0 (summary)

One line per tool, taken from its docstring. Full parameter descriptions are in the docstrings.

### Materials

| Tool | Parameters | Purpose |
|---|---|---|
| `material_create` | `asset_path`, `domain`, `blend_mode`, `two_sided` | Create a new Material asset. |
| `material_create_instance` | `parent_path`, `asset_path` | Create a MaterialInstanceConstant from a parent Material. |
| `material_add_expression` | `material_path`, `expression_class`, `x`, `y` | Add an expression node to a material. Returns the node_name to use in later calls. |
| `material_set_expression_property` | `material_path`, `node_name`, `property_name`, `value` | Set an editor property on a material expression node. |
| `material_connect_expressions` | `material_path`, `from_node`, `from_output`, `to_node`, `to_input` | Connect an output pin of one expression to an input pin of another. Recompiles material. |
| `material_connect_property` | `material_path`, `from_node`, `from_output`, `material_property` | Connect an expression output to a material attribute (BaseColor, Metallic, etc). Recompiles material. |
| `material_list_expressions` | `material_path` | List all expression nodes in a material with names and classes. |
| `material_recompile` | `material_path` | Force recompile of a material and save it. |
| `material_set_scalar_param` | `instance_path`, `param_name`, `value` | Set a scalar parameter value on a MaterialInstanceConstant. |
| `material_set_vector_param` | `instance_path`, `param_name`, `r`, `g`, `b`, `a` | Set a vector (LinearColor) parameter value on a MaterialInstanceConstant. |
| `material_set_texture_param` | `instance_path`, `param_name`, `texture_path` | Set a texture parameter value on a MaterialInstanceConstant. |
| `material_set_static_switch_param` | `instance_path`, `param_name`, `value` | Set a static switch parameter on a MaterialInstanceConstant (triggers recompile). |

### Niagara

| Tool | Parameters | Purpose |
|---|---|---|
| `niagara_place_actor` | `system_path`, `location`, `rotation`, `label` | Spawn a NiagaraActor in the current level with the given NiagaraSystem asset. `rotation` = named axes ([Rotations](#rotations-named-axes)). |
| `niagara_set_system_asset` | `actor_path`, `system_path` | Swap the NiagaraSystem asset on an actor's NiagaraComponent. |
| `niagara_activate` | `actor_path`, `reset` | Activate the NiagaraComponent on an actor. If reset=True, restarts the simulation. |
| `niagara_deactivate` | `actor_path` | Deactivate the NiagaraComponent on an actor. |
| `niagara_reset` | `actor_path` | Reset the particle simulation on an actor's NiagaraComponent. |
| `niagara_set_float_param` | `actor_path`, `param_name`, `value` | Set a float user parameter on a NiagaraComponent. |
| `niagara_set_int_param` | `actor_path`, `param_name`, `value` | Set an int user parameter on a NiagaraComponent. |
| `niagara_set_bool_param` | `actor_path`, `param_name`, `value` | Set a bool user parameter on a NiagaraComponent. |
| `niagara_set_vec3_param` | `actor_path`, `param_name`, `x`, `y`, `z` | Set a vec3 user parameter on a NiagaraComponent (position or direction). |
| `niagara_set_color_param` | `actor_path`, `param_name`, `r`, `g`, `b`, `a` | Set a LinearColor user parameter on a NiagaraComponent. |
| `niagara_set_texture_param` | `actor_path`, `param_name`, `texture_path` | Set a Texture user parameter on a NiagaraComponent. |

### Animation

| Tool | Parameters | Purpose |
|---|---|---|
| `anim_get_info` | `anim_path` | Get basic info about an AnimSequence or AnimMontage (length, class, skeleton). |
| `anim_list_notify_tracks` | `anim_path` | List notify track names on an AnimSequence/Montage. |
| `anim_add_notify_track` | `anim_path`, `track_name`, `color` | Add a new notify track to an AnimSequence/Montage. |
| `anim_remove_all_notify_tracks` | `anim_path` | Remove all notify tracks (and their events) from an animation. |
| `anim_list_notifies` | `anim_path` | List all notify events on an animation with name/time/duration/track/class. |
| `anim_add_notify` | `anim_path`, `track_name`, `time`, `notify_class` | Add a zero-duration typed notify event to a track. |
| `anim_add_notify_state` | `anim_path`, `track_name`, `time`, `duration`, `notify_state_class` | Add a duration-based notify state event to a track. |
| `anim_add_float_curve` | `anim_path`, `curve_name` | Create a new float curve on an animation. |
| `anim_add_float_curve_key` | `anim_path`, `curve_name`, `time`, `value` | Add a keyframe to a float curve on an animation. |
| `anim_create_montage` | `source_animation_path`, `asset_path` | Create an AnimMontage asset from a source AnimSequence. |
| `anim_create_blendspace` | `skeleton_path`, `asset_path`, `blendspace_type` | Create a BlendSpace (2D) or BlendSpace1D asset. |

### Static meshes

Only crash-safe reads in UEFN 42.20: see [Static meshes: only crash-safe reads](#static-meshes-only-crash-safe-reads-uefn-4220).

| Tool | Parameters | Purpose |
|---|---|---|
| `staticmesh_get_info` | `asset_path` | Triangles / verts / UV channels / LOD count of LOD0 and `collision_prims` (asset-registry tags), material slots, bounds, Nanite settings; `simple_collision_count`, `convex_collision_count`, `has_vertex_colors`, `lod_screen_sizes` = "not available safely in UEFN 42.20". |
| `staticmesh_enable_nanite` | `asset_path`, `enabled`, `fallback_percent_triangles` | Enable/disable Nanite on a static mesh (other Nanite settings kept); returns `nanite_settings_after`. |
| `staticmesh_set_lods` | `asset_path`, `percent_triangles`, `screen_sizes`, `auto_compute_screen_size` | Set LODs by triangle reduction. First element = LOD0 (usually 1.0 = full). Crashed one long session (2026-08-24): save first, one mesh per call. |
| `staticmesh_remove_lods` | `asset_path` | Remove all LODs except LOD0; returns `removed` (read the new count with `staticmesh_get_info`). |
| `staticmesh_add_collision` | `asset_path`, `shape` | Add a simple collision primitive. |
| `staticmesh_remove_collisions` | `asset_path` | Remove all simple collisions from a static mesh; returns `removed`. |
| `staticmesh_generate_uv` | `asset_path`, `uv_type`, `lod_index`, `uv_channel_index`, `position`, `orientation`, `tiling`, `size` | Generate a UV channel with a planar / box / cylindrical gizmo: `position` [x, y, z] (default: bounds center), `orientation` named axes, `tiling` [u, v] (planar / cylindrical, default [1, 1]), `size` [x, y, z] (box, default: bounds size). Not live-tested in UEFN 42.20 yet. |

### Asset management

| Tool | Parameters | Purpose |
|---|---|---|
| `asset_batch_rename` | `renames` | Rename (and/or move) multiple assets in one transaction. |
| `asset_set_metadata` | `asset_path`, `tag`, `value` | Set a metadata tag on an asset (e.g. 'Author', 'Category', 'Rarity'). |
| `asset_get_metadata` | `asset_path` | Read all metadata tags on an asset. |
| `asset_remove_metadata` | `asset_path`, `tag` | Remove a metadata tag from an asset. |
| `asset_find_referencers` | `asset_path` | List packages that reference this asset (i.e. what uses it). |
| `asset_find_dependencies` | `asset_path` | List packages this asset depends on (i.e. what it uses). |
| `asset_find_unused` | `directory`, `class_filter` | Find assets with no referencers in a directory. |

### Data tables

| Tool | Parameters | Purpose |
|---|---|---|
| `datatable_info` | `asset_path` | Get DataTable structure, row names, and column names. |
| `datatable_export_json` | `asset_path` | Export the whole DataTable as a JSON string. |
| `datatable_export_csv` | `asset_path` | Export the whole DataTable as a CSV string. |
| `datatable_import_json` | `asset_path`, `json_string` | Replace all rows in a DataTable from a JSON string. Saves the asset. |
| `datatable_import_csv` | `asset_path`, `csv_string` | Replace all rows in a DataTable from a CSV string. Saves the asset. |
| `datatable_get_row` | `asset_path`, `row_name` | Get a single row of a DataTable (parsed from JSON export). |

### Validation

| Tool | Parameters | Purpose |
|---|---|---|
| `validate_asset` | `asset_path` | Run all registered editor validators on one asset. Returns valid/invalid counts. |
| `validate_folder` | `directory`, `recursive` | Validate all assets under a directory. Good for CI-style content audits. |
| `validate_selected` | — | Validate assets currently selected in the UEFN Content Browser. |

### Screenshots

`screenshot_viewport` needs the UEFN window in the foreground; `screenshot_desktop` runs on the host and needs `pip install mss`.

| Tool | Parameters | Purpose |
|---|---|---|
| `screenshot_viewport` | `output_path`, `width`, `height`, `force_game_view`, `timeout_sec` | Capture the UEFN editor viewport as a PNG file. |
| `screenshot_desktop` | `output_path`, `monitor` | Capture the whole desktop (or a specific monitor) as a PNG file. |

### Devices

These call `set_editor_property` / `get_editor_property` on the actor with the plain field name. That covers properties the actor exposes under their own name; Verse-declared `@editable` fields live on the device's inner object under a mangled name (`__verse_0x<HASH>_<Field>`) and fail with "Failed to find property" (README, Known limitations).

| Tool | Parameters | Purpose |
|---|---|---|
| `device_list_editables` | `actor_path` | List all Verse @editable fields on a creative_device actor in the level. |
| `device_set_editable` | `actor_path`, `field`, `value`, `value_type` | Set a Verse @editable field on a creative_device actor. Rotator values take named axes `{"pitch", "yaw", "roll"}`. |
| `device_set_editables_bulk` | `actor_path`, `fields` | Set multiple Verse @editable fields on one actor in a single call. |

### Verse build (no listener)

VerseWorkflowServer on TCP `127.0.0.1:1962` (`VERSE_WORKFLOW_HOST` / `VERSE_WORKFLOW_PORT`). UEFN must have the project open. `verse_push` works only while a session runs.

| Tool | Parameters | Purpose |
|---|---|---|
| `verse_compile` | — | Compile the Verse project in the open UEFN editor and return errors/warnings. |
| `verse_status` | — | Report current Verse build state & push-availability WITHOUT compiling. |
| `verse_push` | `verse_only` | Push changes to the live UEFN session (equivalent of "Push Changes"). |

### Verse navigation (no listener)

Persistent `verse-lsp.exe` from the `epicgames.verse` VS Code extension (`VERSE_LSP_EXE`), initialized with the newest UEFN `.code-workspace` (`VERSE_WORKSPACE_FILE`). `file_path` may be relative to the project's Content folder.

| Tool | Parameters | Purpose |
|---|---|---|
| `verse_symbols` | `file_path` | Outline a .verse file: classes, functions, vars with line numbers. |
| `verse_hover` | `file_path`, `line`, `col` | Type/signature info at a position in a .verse file (1-based line/col). |
| `verse_definition` | `file_path`, `line`, `col` | Go-to-definition from a position in a .verse file (1-based line/col). |
| `verse_find_symbol` | `query` | Workspace-wide symbol search by name (verse-lsp workspace/symbol). |
| `verse_lsp_restart` | — | Restart the persistent verse-lsp session. |

---

## New in v0.5.0: desktop control and UEFN session

Host-side tools (Windows): they run in the MCP server process and need neither UEFN nor the listener. Safety rails,
coordinates, UEFN facts and recipes: [desktop_control.md](desktop_control.md). "Acting" tools obey the rails
(allowlist `UEFN_DESKTOP_ALLOW`, focus and foreground re-check, top-left-corner kill switch, UIPI check, audit log).
The anti-cheat-protected Fortnite game client is screenshots only: it gets no input (not even a focus) unless the
owner opts in with `UEFN_DESKTOP_ALLOW=+FortniteClient-Win64-Shipping` (Epic's terms; the account is at risk).

Coordinates are physical pixels of the virtual desktop (the server is per-monitor DPI aware V2; monitors left of or
above the primary have negative origins). Pointer tools take `x`, `y` plus either `shot` (a `desktop_screenshot` path
or `shot_id`: the numbers are pixels of that image) or `relative_to` = `screen` | `monitor` (with `monitor`) |
`monitor_dip` | `window` | `client` (relative to the target window).

### Desktop control

| Tool | Parameters | Acting | Purpose |
|---|---|---|---|
| `desktop_list_windows` | `process`, `title`, `class_name`, `include_hidden`, `limit` | no | Top-level windows in z-order (process, pid, class, rect, monitor, visible / minimized / foreground, `input_allowed`), monitors, virtual desktop, cursor, kill-switch state, allowlist and `allowlist_warnings`, DPI awareness. |
| `desktop_screenshot` | `monitor`, `process`, `title`, `class_name`, `region`, `output_path`, `max_long_edge`, `max_pixels`, `full_resolution`, `count`, `interval_sec` | no | PNG of a region, a window or a monitor (0 = all); `path`, `shot_id`, `mapping` (`screen = origin + floor((image + 0.5) * scale)`), cursor position in the image, windows covering the target; bursts up to 120 s. |
| `desktop_focus_window` | `process`, `title`, `class_name`, `timeout_sec` | yes | Restore and bring to the foreground, verified; reports the method that worked. |
| `desktop_click` | `x`, `y`, `shot`, `relative_to`, `monitor`, `process`, `title`, `button`, `double`, `modifiers`, `hold_ms` | yes | Click after focusing the target; the window under the point must be the target's process. |
| `desktop_move` | `x`, `y`, `shot`, `relative_to`, `monitor`, `process`, `title` | yes | Hover. |
| `desktop_drag` | `x1`, `y1`, `x2`, `y2`, `shot`, `relative_to`, `monitor`, `process`, `title`, `button`, `duration_ms` | yes | Press, move in steps, release; aborts and releases if the user moves the mouse. |
| `desktop_scroll` | `x`, `y`, `clicks`, `horizontal`, `shot`, `relative_to`, `monitor`, `process`, `title` | yes | Wheel notches (positive = up / right), -50..50. |
| `desktop_type` | `text`, `process`, `title`, `interval_ms`, `sensitive` | yes | Unicode text into the focused control (newline = Enter, tab = Tab), max 4000 characters; `sensitive` redacts it in the log. |
| `desktop_key` | `keys`, `process`, `title`, `repeat`, `interval_ms`, `hold_ms`, `allow_editor_close` | yes | A key or combo (`f5`, `ctrl+s`, `alt+f4`, `enter`, `esc`, `ctrl+plus`); `alt+f4` on UEFN needs `allow_editor_close`. |
| `desktop_close_window` | `process`, `title`, `class_name`, `all_matches`, `wait_sec`, `allow_editor_main` | yes | `WM_CLOSE` (like the X button); several matches need `all_matches`; UEFN editor windows need `allow_editor_main`. |
| `desktop_wait_for_window` | `process`, `title`, `class_name`, `gone`, `timeout_sec`, `interval_sec` | no | Wait for a window to appear (or disappear). |

Key names for `desktop_key`: `a`-`z`, `0`-`9`, `f1`-`f24`, `enter`/`return`, `esc`/`escape`, `tab`, `space`,
`backspace`, `delete`/`del`, `insert`/`ins`, `home`, `end`, `pageup`/`pgup`, `pagedown`/`pgdn`, `up`, `down`, `left`,
`right`, `ctrl`/`control`, `shift`, `alt`, `win`, `lctrl`, `rctrl`, `lshift`, `rshift`, `lalt`, `ralt`, `rwin`,
`apps`/`contextmenu`, `capslock`, `numlock`, `scrolllock`, `printscreen`, `pause`, `num0`-`num9`, `add`, `subtract`,
`multiply`, `divide`, `decimal`, `separator`, `minus`, `equals`/`plus` (the `=/+` key), `comma`, `period`, `slash`,
`backslash`, `semicolon`, `quote`, `backquote`, `bracketleft`, `bracketright`, media keys (`volumeup`, `playpause`, ...).
`+` joins keys; `ctrl++` is Ctrl + the plus key.

### UEFN session

| Tool | Parameters | Acting | Purpose |
|---|---|---|---|
| `uefn_status` | `project`, `probe_ports` | no | State (`not_running`, `starting`, `hub`, `opening`, `project_open`, `other_project_open`, `open_failed`, `crash_dialog`) from the editor log, windows (responding?), crash dialog, Python / listener / Toolsets markers, ports, "Load on Startup", hook, install; hints. |
| `uefn_launch_project` | `project`, `launch`, `launch_via`, `retarget_last_project`, `enable_load_last_project`, `wait_sec`, `hub_grace_sec`, `focus_hub`, `wait_for_listener`, `listener_wait_sec` | yes | Start UEFN if needed (Epic launcher URI or exe), wait for the project or the HUB (`status="hub"` returns a screenshot to pick the tile), then for the listener. Results: `ready`, `project_open_no_listener`, `hub`, `crash_dialog`, `other_project_open`, `open_failed`, `not_running`, `timeout`. |
| `uefn_set_load_on_startup` | `value` (`HomeScreen` / `LastProject`), `dry_run` | yes | Owner opt-in: UEFN's "Load on Startup". UEFN must be closed; backup kept. |