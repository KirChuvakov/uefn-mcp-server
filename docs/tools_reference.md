# Tools Reference

22 tools organized in 5 categories. Each tool maps 1:1 to a listener command.

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
| `rotation` | float[3] | no | `[0,0,0]` | Rotation `[pitch, yaw, roll]` in degrees |

**Examples:**

Spawn a cube at position (500, 0, 100):
```json
{"asset_path": "/Engine/BasicShapes/Cube", "location": [500, 0, 100]}
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
| `rotation` | float[3] | no | `[pitch, yaw, roll]` in degrees |
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
| `rotation` | float[3] | no | `[pitch, yaw, roll]` in degrees |

**Response:** The new camera position (same format as `get_viewport_camera`).
