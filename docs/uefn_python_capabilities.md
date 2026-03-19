# UEFN Python API — Complete Capabilities Reference

> Generated from analysis of 37,227 types across 1,459 C++ modules.
> All capabilities verified via live UEFN editor introspection (March 2026).

---

## Executive Summary

UEFN exposes **37,276** Python-accessible types — **4.3x more** than standard UE5.
Python runs **editor-only** (not runtime). For gameplay logic use **Verse**.

| Domain | Score | Key Entry Points |
|--------|-------|-----------------|
| Asset Pipeline | 10/10 | EditorAssetLibrary (62), EditorAssetSubsystem (66), AssetTools |
| Actors & Levels | 10/10 | EditorActorSubsystem (45), LevelEditorSubsystem (49) |
| Materials | 10/10 | MaterialEditingLibrary (89 methods) |
| Static Meshes | 10/10 | StaticMeshEditorSubsystem (87 methods) |
| Geometry Scripting | 10/10 | 46 utility classes, 145+ operations (booleans, UVs, bake) |
| PCG | 10/10 | 597 types — procedural generation, point clouds, spawning |
| Screenshots & Testing | 10/10 | AutomationLibrary (58), image comparison |
| Rendering | 10/10 | MovieRenderPipeline (210 types), post-process |
| Validation | 10/10 | EditorValidatorSubsystem (30 methods) |
| Data Tables | 10/10 | Full CRUD on DataTables, Curves, GameplayTags |
| Sequencer | 9/10 | Level sequences, tracks, keyframes, Take Recorder |
| Niagara VFX | 9/10 | Spawn, parameters, SimCache, bake (no graph editing) |
| Audio/MetaSound | 9/10 | SoundWaves, cues, MetaSound, Quartz |
| Animation | 8/10 | AnimSequence, Montage, Modifiers, Notifies |
| Enhanced Input | 8/10 | InputAction, MappingContext creation |
| UI/UMG | 8/10 | Editor Utility Widgets, programmatic layout |
| Physics/Chaos | 7/10 | Destruction OK; vehicle physics stripped |
| Landscape | 6/10 | Splines, grass, layers (no sculpting) |
| **Fortnite Classes** | **5/10** | **28,850 types — mostly read-only inspection** |

---

## 1. Asset Pipeline (10/10)

**Entry points:** `EditorAssetLibrary` (62 methods), `EditorAssetSubsystem` (66), `AssetTools`, `AssetRegistry`

### Full CRUD
- `load_asset()`, `save_asset()`, `delete_asset()`, `rename_asset()`, `duplicate_asset()`
- `list_assets(dir, recursive=True)` — recursive listing with filtering
- `find_package_referencers_for_asset()` — dependency graph
- `consolidate_assets()` — merge duplicates

### Batch Operations
- `checkout_directory()` — source control batch checkout
- `save_directory()`, `delete_directory()`, `rename_directory()`
- `import_asset_tasks()` — batch import FBX/glTF/ABC/USD

### Metadata & Validation
- `set_metadata_tag()`, `get_metadata_tag_values()` — custom tags
- `EditorValidatorSubsystem` — rules-based validation on save
- `find_asset_data()` → `AssetData` with class, tags, package info

### Practical Use Cases
- Batch rename with prefixes (T_, SM_, M_, SK_, BP_)
- Find and archive unused assets via dependency graph
- Auto-organize by type into folders
- Validate naming conventions on save
- Source control automation (checkout, save, submit)

---

## 2. Actors & Levels (10/10)

**Entry points:** `EditorActorSubsystem` (45), `EditorLevelLibrary` (60), `LevelEditorSubsystem` (49), `EditorLevelUtils` (33)

### Actor Operations
- `spawn_actor_from_class()`, `spawn_actor_from_object()` — placement
- `duplicate_actors()` — batch duplication with offset
- `destroy_actor()` — removal
- `set_actor_transform()`, `set_actor_location()`, `set_actor_rotation()`, `set_actor_scale3d()`
- `get_all_level_actors()`, `get_selected_level_actors()`
- `EditorFilterLibrary` — filter by label, tag, class, layer, level, selection

### Level Management
- `load_level()`, `save_current_level()`, `new_level()`
- `add_level_to_world()`, `remove_level_from_world()` — streaming
- `set_current_level_by_name()` — sub-level switching
- Viewport camera: `set_level_viewport_camera_info(location, rotation)`

### Events
- `on_delete_actors_begin/end`, `on_duplicate_actors_begin/end`
- `on_edit_copy_actors_begin/end`

---

## 3. Materials (10/10)

**Entry point:** `MaterialEditingLibrary` (89 methods)

### Create Materials from Scratch
- `create_material_expression(material, ExpressionClass, x, y)` — ~200 node types
- `connect_material_expressions(from_expr, output, to_expr, input)`
- `connect_material_property(expr, output, MaterialProperty.MP_BASE_COLOR)`

### Material Instances
- `set_material_instance_scalar_parameter_value(instance, name, value)`
- `set_material_instance_vector_parameter_value(instance, name, color)`
- `set_material_instance_texture_parameter_value(instance, name, texture)`
- `set_material_instance_static_switch_parameter_value(instance, name, bool)`

### Material Functions
- Create reusable material functions
- Compile and validate programmatically

### Cannot Do
- Modify shader code directly
- Create custom expression types

---

## 4. Static Meshes (10/10)

**Entry point:** `StaticMeshEditorSubsystem` (87 methods)

### LODs
- `set_lods()` — auto-generate with reduction options
- `import_lod()` — from external file
- `set_lod_screen_sizes()`, `get_lod_count()`

### Collisions
- `add_simple_collisions()` — box, sphere, capsule, KDOP
- `set_convex_decomposition_collisions()` — complex geometry
- `remove_collisions()`

### UVs
- `generate_planar_uv_channel()`, `generate_cylindrical_uv_channel()`, `generate_box_uv_channel()`
- `add_uv_channel()`, `remove_uv_channel()`

### Other
- Nanite: `enable_nanite()`
- Vertex colors: `has_vertex_colors()`, `has_instance_vertex_colors()`
- Statistics: `get_number_verts()`, `get_num_uv_channels()`

---

## 5. Geometry Scripting (10/10) — NEW

**Module:** `GeometryScriptingCore` — 46 classes, 83 enums, 145 structs

### Mesh I/O
- `GeometryScript_AssetUtils.copy_mesh_from_static_mesh()` / `copy_mesh_to_static_mesh()`
- `copy_mesh_from_skeletal_mesh()` / `copy_mesh_to_skeletal_mesh()`

### Boolean Operations
- `apply_mesh_boolean()` — Union / Difference / Intersection
- `apply_mesh_plane_cut()`, `apply_mesh_mirror()`, `apply_mesh_self_union()`
- `compute_mesh_convex_hull()`, `compute_mesh_convex_decomposition()`

### Mesh Processing
- **Simplification:** `simplify_mesh()`, `cluster_simplify()`
- **Remeshing:** adaptive/uniform remesh with edge constraints
- **Repair:** `fill_holes()`, `resolve_t_junctions()`, `weld_edges()`, `remove_degenerate_triangles()`
- **Deformation:** bend, twist, flare warp

### UV & Textures
- `layout_uvs()`, `recompute_uvs()`, `generate_lightmap_uvs()`
- XAtlas automatic unwrapping
- **Baking:** normals, AO, height, curvature via `GeometryScript_Bake`

### Skinning (Skeletal)
- `compute_smooth_bone_weights()` — auto-weight vertices
- `transfer_bone_weights_from_mesh()`
- `copy_bones_from_skeleton()`

### Team Use Cases
- Batch mesh processing: load → simplify → bake → write back
- Procedural asset generation: primitives → booleans → collision
- LOD pipeline: simplify with different targets per LOD
- Import cleanup: repair, recompute UVs, generate lightmaps

---

## 6. PCG — Procedural Content Generation (10/10) — NEW

**Module:** `PCG` — 372 classes, 152 enums, 73 structs (597 total)

### Graph Execution from Python
- `PCGComponent.generate(force)` — trigger generation
- `PCGComponent.get_generated_graph_output()` — retrieve results
- Delegates: OnPCGGraphStartGenerating, OnPCGGraphGenerated, OnPCGGraphCleaned

### Data Types
- `PCGPointData`, `PCGSurfaceData`, `PCGVolumeData`, `PCGLandscapeData`
- `PCGSplineData`, `PCGPolygon2DData`, `PCGSpatialData`

### Operations
- **Point generation:** grid, sphere, from mesh, from spline
- **Data transforms:** merge, filter, partition, attribute operations
- **Actor spawning:** `PCGSpawnActorSettings`
- **Landscape integration:** grass maps, texture generation
- **GPU compute:** `PCGCustomHLSLKernel` for custom shaders

### Attribute System
- 40+ `PCGAttribute*` classes for data manipulation
- Remap, filter, noise, boolean operations on attributes

### Team Use Cases
- Automated level population (foliage, props, decorations)
- Procedural terrain features from splines
- Data-driven spawning with attribute-based filtering

---

## 7. Movie Render Pipeline (10/10) — NEW

**Module:** `MovieRenderPipelineCore` — 145 classes, 30 enums, 35 structs

### Pipeline Control
- `MovieGraphPipeline.initialize(job, config)` — setup render job
- `MoviePipelineExecutorJob` — define sequences, output, frame range
- State tracking: `get_pipeline_state()` → Rendering / Paused / Finished

### Render Configuration
- `MovieGraphCameraSettingNode` — camera parameters
- `MovieGraphColorSetting` — color grading, LUTs
- `MovieGraphApplyCVarPresetNode` — console variable presets
- `MovieGraphMaterialModifier` — material overrides during render

### Output
- `MovieGraphFileOutputNode` — EXR, PNG, JPEG, video codecs
- Resolution, frame rate, quality settings
- `MovieGraphCommandLineEncoderNode` — post-render encoding

### Team Use Cases
- Automated cinematic rendering overnight
- Batch render with different quality settings
- Visual regression testing (render + compare)
- Marketing asset generation pipeline

---

## 8. Screenshots & Automation (10/10)

**Entry point:** `AutomationLibrary` (58 methods)

- `take_high_res_screenshot(res_x, res_y, filename, camera, capture_hdr, force_game_view)`
- `take_automation_screenshot()`, `take_automation_screenshot_at_camera()`
- `take_automation_screenshot_of_ui()` — widget capture
- `compare_image_against_reference()` — visual regression
- UEFN extras: `ScreenshotActor`, `FortCreativeCaptureScreenshotHUD`

---

## 9. Niagara VFX (9/10)

**Module:** `Niagara` — 260 types (124 classes, 76 enums, 60 structs)

### Can Do
- Spawn systems: `NiagaraFunctionLibrary`
- Set parameters: float, vector, color, texture
- Data Channels: `NiagaraDataChannelLibrary` — GPU communication
- SimCache: record/playback for performance
- Bake VFX into textures or static meshes

### Cannot Do
- Edit emitter internals (stack architecture)
- Create custom Niagara modules
- Modify Niagara graphs programmatically

---

## 10. Sequencer & Cinematics (9/10)

**Modules:** `SequencerScripting` (38), `MovieScene` (86), `MovieSceneTracks` (119), `LevelSequence` (18)

- Create Level Sequences, add tracks (camera, transform, audio, visibility)
- Set keyframes for animated properties
- Camera animation: focus distance, aperture, FOV
- Take Recorder: `TakeRecorderBlueprintLibrary`

---

## 11. Audio / MetaSound (9/10)

**Modules:** `AudioMixer` (36), `MetasoundEngine` (24), `MetasoundEditor` (37), `Synthesis` (126)

- SoundWave, SoundCue management
- MetaSound asset creation and configuration
- `QuartzSubsystem` for rhythm-synchronized timing
- Audio analysis: `AudioSynesthesia` (38 types)

---

## 12. Animation (8/10)

**Modules:** `AnimGraph` (97), `AnimGraphRuntime` (152), `AnimationModifierLibrary` (18)

- AnimSequence, AnimMontage creation/modification
- AnimationModifiers: batch apply
- AnimNotifies: add/remove/configure
- Blend Spaces, retarget profiles, compression
- State machines via `AnimationStateMachineLibrary`

---

## 13. Enhanced Input (8/10)

**Module:** `EnhancedInput` — 75 types (47 classes, 16 enums, 12 structs)

- Create `InputAction` and `InputMappingContext` assets
- Configure modifiers and triggers
- Programmatic input mapping setup

---

## 14. Interchange — Import/Export (9/10)

**Modules:** `InterchangeImport` (73), `InterchangePipelines` (22), `InterchangeFactoryNodes` (45)

- **Formats:** FBX, glTF 2.0, USD, Alembic (ABC)
- Batch import with custom settings
- Custom translators for pipeline extension
- Source control integration

### Not Available
- Datasmith (CAD/BIM) — 38 classes stripped from UEFN

---

## 15. Fortnite-Specific Classes (5/10 — Mostly Read-Only)

**28,850 types** unique to UEFN. **Read-heavy with limited write.**

### What Works

| Operation | Status | How |
|-----------|--------|-----|
| Actor transforms | ✅ Write | `set_actor_location()`, `set_actor_rotation()` |
| Actor visibility | ✅ Write | `set_actor_hidden()`, `set_hidden_in_game()` |
| Editor properties | ⚠️ Limited | `set_editor_property(name, value)` — only exposed props |
| Component queries | ✅ Read | `get_component_by_class()`, `get_components_by_class()` |
| Gameplay data | ✅ Read | Weapons, items, abilities, game state |
| AI hot spots | ✅ Write | `assign_to_hotspot()`, `set_goal_actor()` |

### What Doesn't Work

- **No spawning** of Fortnite-specific actors from Python
- **No inventory modification** (read-only)
- **No combat stats changes** (read-only)
- **No quest completion** (read-only)
- **No Verse bridge** — can inspect Verse classes but not call functions
- **No game loop control** — can't start/stop matches

### By Game Mode

| Mode | Classes | Practical Use |
|------|---------|--------------|
| **Core Fortnite** (Fort*) | 9,333 | Inspect weapons, items, abilities, game state |
| **Creative** | 212 | Inspect devices, island data. Automation via Verse, not Python |
| **LEGO / Juno** | 1,044 | Inspect buildings, world, NPCs. Read-only |
| **Rocket Racing / DelMar** | 667 | Inspect vehicles, tracks, cosmetics |
| **Festival / Sparks** | 340 | Inspect music systems, playlists |
| **AI** | 806 | Inspect state, limited hot spot assignment |
| **Editor** (FortniteEditor) | 325 | Asset validation, bulk editing, quest tools — **most useful** |

### Bottom Line
- **Python** = editor tools, content pipelines, debugging, inspection
- **Verse** = game logic, progression, gameplay systems

---

## 16. What's Stripped from UEFN (204 classes)

| Category | Count | Why | Alternative |
|----------|-------|-----|-------------|
| Vehicle Physics (ChaosVehicle*) | 47 | UEFN uses DelMar* | DelMarCore module |
| Datasmith (CAD/BIM) | 38 | Not applicable | Standard import pipeline |
| Other niche plugins | 91 | Not in UEFN scope | — |
| MediaPlate | 8 | Standard Media works | MediaAssets module |
| Google PAD | 6 | Android only | — |
| Resonance Audio | 5 | UEFN audio pipeline | AudioMixer |
| Location Services | 5 | Not applicable | — |
| Mobile Patching | 3 | UEFN handles deploy | — |
| Niagara Factory | 1 | Niagara itself works | Niagara module (260 types) |

---

## Practical Automation Opportunities for Teams

### Daily Workflow (2-4 hours saved/week per person)
1. **Asset naming enforcement** — prefix validation on save
2. **Batch LOD generation** — all static meshes in one pass
3. **Material instance templating** — generate MIs from master
4. **Unused asset cleanup** — dependency scan + archive

### Pipeline Automation (10-100x speedup)
5. **PCG-driven level population** — procedural prop/foliage placement
6. **Geometry processing** — boolean ops, mesh repair, UV generation
7. **Batch rendering** — overnight cinematic export via MovieRenderPipeline
8. **Import pipeline** — auto-process FBX/glTF imports with naming + LOD + collision

### QA & CI/CD
9. **Visual regression** — screenshot + compare_image_against_reference
10. **Asset validation** — naming, LODs, materials, collisions on every save
11. **Fortnite class inspection** — verify item definitions, creative devices

---

## File Locations

| What | Where |
|------|-------|
| Full generated API | `docs/generated/README.md` (1,470 files) |
| UEFN stub for IDE | `docs/uefn_stub.pyi` (2.2M lines) |
| Module map overview | `docs/00_module_map.md` |
| Practical examples | `docs/16_practical_examples.md` |
| Tips & patterns | `docs/17_python_tips_and_patterns.md` |
| UEFN availability | `docs/uefn_api_availability.md` |
