"""Offline tests for the static-mesh safety fix (0.5.0). No UEFN, no listener, no network.

UEFN 42.20 died with EXCEPTION_ACCESS_VIOLATION on one read-only probe of a project mesh that called
the StaticMeshEditorSubsystem metadata getters, StaticMesh.get_num_triangles / get_num_sections and
BodySetup.agg_geom.export_text(). On the real uefn_listener.py these tests check that:

- none of those getters is called anywhere in the listener (AST scan, also through getattr);
- the staticmesh_* handlers, run against a fake `unreal` whose crashing getters raise, return the
  safe facts (asset-registry tags, material slots, bounds, Nanite settings) and mark the rest
  "not available safely in UEFN 42.20";
- staticmesh_enable_nanite keeps the mesh's other Nanite settings, and staticmesh_generate_uv
  passes a Vector position, a keyword-built Rotator orientation and a Vector2D tiling (box: a
  Vector size), validating everything before it touches the mesh;
- the docs carry the warning.

    python tests/test_staticmesh_safety_offline.py        (pytest also collects it)
"""
import ast
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _listener_ast as la  # noqa: E402

CRASHING_GETTERS = {
    "get_lod_count", "get_number_verts", "get_number_materials", "get_simple_collision_count",
    "get_collision_complexity", "get_convex_collision_count", "get_lod_screen_sizes", "get_nanite_settings",
    "has_vertex_colors", "get_num_uv_channels", "get_num_triangles", "get_num_sections",
}
MESH_DEFS = [
    "ROTATION_AXES", "_finite_number", "_parse_rotation", "_make_rotator", "_float_list", "_serialize",
    "STATICMESH_UNSAFE", "_STATIC_MESH_TAGS", "_UV_GEN_MAP", "_resolve_static_mesh", "_tag_text", "_tag_int",
    "_tag_bool", "_registry_tags", "_static_mesh_slots", "_static_mesh_bounds", "_static_mesh_nanite",
    "_read_or_note", "_staticmesh_info", "_cmd_staticmesh_get_info", "_cmd_staticmesh_enable_nanite",
    "_cmd_staticmesh_remove_lods", "_cmd_staticmesh_remove_collisions", "_cmd_staticmesh_generate_uv",
]
MESH = "/di_template/Meshes/SM_Rock"
TAGS = {"Triangles": "12", "Vertices": "24", "UVChannels": "2", "Materials": "2", "LODs": "3",
        "CollisionPrims": "1", "CollisionComplexity": "CTF_UseDefault", "NaniteEnabled": "True",
        "HasNaniteData": "True", "ApproxSize": "100x50x80"}


def raises(exc, fn, *a, match="", **kw):
    try:
        fn(*a, **kw)
    except exc as e:
        assert match in str(e), f"{match!r} not in {e!r}"
        return str(e)
    raise AssertionError(f"{getattr(fn, '__name__', fn)}{a}{kw} did not raise {exc.__name__}")


class Crash(AssertionError):
    """Raised by a fake getter that crashed UEFN 42.20."""


def crashing(name):
    def getter(*args, **kwargs):
        raise Crash(f"{name} was called: it crashed UEFN 42.20")
    return getter


class Material:
    def __init__(self, path):
        self.path = path

    def get_path_name(self):
        return self.path


class StaticMesh:
    def __init__(self):
        self.props = {
            "static_materials": [
                la.Struct(material_slot_name="Rock", material_interface=Material("/di_template/Materials/M_Rock.M_Rock")),
                la.Struct(material_slot_name="Moss", material_interface=None),
            ],
            "nanite_settings": la.Struct(enabled=True, fallback_percent_triangles=1.0, position_precision=4),
        }
        self.get_num_triangles = crashing("StaticMesh.get_num_triangles")
        self.get_num_sections = crashing("StaticMesh.get_num_sections")

    def get_path_name(self):
        return MESH + ".SM_Rock"

    def get_editor_property(self, name):
        if name == "body_setup":
            raise Crash("BodySetup.agg_geom.export_text() path")
        return self.props[name]

    def get_bounding_box(self):
        return la.Box(la.Vector(-50, -25, 0), la.Vector(50, 25, 80))


class MeshSubsystem:
    """StaticMeshEditorSubsystem: the crash-list getters raise, the writers record their calls."""

    def __init__(self):
        self.calls = []
        for name in CRASHING_GETTERS:
            setattr(self, name, crashing(f"StaticMeshEditorSubsystem.{name}"))

    def set_nanite_settings(self, mesh, settings, apply_changes=True):
        self.calls.append(("set_nanite_settings", settings, apply_changes))

    def remove_lods(self, mesh):
        self.calls.append(("remove_lods",))
        return True

    def remove_collisions(self, mesh):
        self.calls.append(("remove_collisions",))
        return True

    def _gizmo(self, kind, lod, channel, position, orientation, last, last_type):
        # the real signatures: (mesh, lod, channel, position: Vector, orientation: Rotator, tiling: Vector2D | size: Vector)
        if not (isinstance(position, la.Vector) and isinstance(orientation, la.Rotator) and isinstance(last, last_type)):
            raise TypeError(f"generate_{kind}_uv_channel: position must be Vector, orientation Rotator, "
                            f"last argument {last_type.__name__}")
        self.calls.append((kind, lod, channel, position, orientation, last))
        return True

    def generate_planar_uv_channel(self, mesh, lod, channel, position, orientation, tiling):
        return self._gizmo("planar", lod, channel, position, orientation, tiling, la.Vector2D)

    def generate_cylindrical_uv_channel(self, mesh, lod, channel, position, orientation, tiling):
        return self._gizmo("cylindrical", lod, channel, position, orientation, tiling, la.Vector2D)

    def generate_box_uv_channel(self, mesh, lod, channel, position, orientation, size):
        return self._gizmo("box", lod, channel, position, orientation, size, la.Vector)


class AssetData:
    def is_valid(self):
        return True


def env(tag_style="plain"):
    """(namespace, mesh, subsystem, saved paths). tag_style: how get_tag_value reports tags."""
    mesh, sub, saved = StaticMesh(), MeshSubsystem(), []

    def get_tag_value(data, name):
        value = TAGS.get(name)
        if tag_style == "none-string":  # UEFN 42.20: a plain string, "None" when absent
            return "None" if value is None else value
        if tag_style == "tuple":        # other builds: (found, value)
            return (value is not None, value or "")
        return value

    unreal = la.fake_unreal(
        StaticMesh=StaticMesh, StaticMeshEditorSubsystem=MeshSubsystem, get_editor_subsystem=lambda cls: sub,
        EditorAssetLibrary=types.SimpleNamespace(
            load_asset=lambda path: mesh, find_asset_data=lambda path: AssetData(),
            save_asset=lambda path: saved.append(path) or True),
        AssetRegistryHelpers=types.SimpleNamespace(get_tag_value=get_tag_value))
    return la.load(MESH_DEFS, unreal), mesh, sub, saved


# -- the source ------------------------------------------------------------------------------------

def test_no_crashing_getter_is_called_in_the_listener():
    bad = []
    for node, name in la.calls(la.listener_tree()):
        attr = name.rsplit(".", 1)[-1]
        if attr in CRASHING_GETTERS or (attr == "export_text" and "agg_geom" in name):
            bad.append(f"line {node.lineno}: {name}")
        if (name == "getattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in CRASHING_GETTERS):
            bad.append(f"line {node.lineno}: getattr(..., {node.args[1].value!r})")
    assert not bad, f"calls that crashed UEFN 42.20: {bad}"


# -- handlers ----------------------------------------------------------------------------------------

def test_get_info_reports_safe_facts_and_marks_the_rest():
    for style in ("plain", "none-string", "tuple"):
        ns, mesh, sub, _ = env(style)
        info = ns["_cmd_staticmesh_get_info"](MESH)
        assert (info["triangles_lod0"], info["verts_lod0"], info["uv_channels_lod0"], info["lod_count"]) == (12, 24, 2, 3)
        assert info["collision_prims"] == 1 and info["collision_complexity"] == "CTF_UseDefault"
        assert info["material_slots"] == 2
        assert info["materials"][0] == {"index": 0, "slot": "Rock", "material": "/di_template/Materials/M_Rock.M_Rock"}
        assert info["materials"][1]["material"] is None
        assert info["bounds"]["size"] == {"x": 100.0, "y": 50.0, "z": 80.0}
        assert info["bounds"]["center"] == {"x": 0.0, "y": 0.0, "z": 40.0}
        assert info["nanite_enabled"] is True and info["nanite_fallback_percent"] == 1.0 and info["has_nanite_data"]
        unsafe = ns["STATICMESH_UNSAFE"]
        assert unsafe == "not available safely in UEFN 42.20"
        for key in ("simple_collision_count", "convex_collision_count", "has_vertex_colors", "lod_screen_sizes"):
            assert str(info[key]).startswith(unsafe), (style, key)
        assert "read_errors" not in info and sub.calls == [], style
        assert "Triangles" in info["registry"] and "MinLOD" not in info["registry"], style  # absent tags left out


def test_get_info_survives_a_failed_read():
    ns, mesh, sub, _ = env()
    del mesh.props["nanite_settings"]
    info = ns["_cmd_staticmesh_get_info"](MESH)
    assert "nanite" in info["read_errors"]
    assert info["nanite_enabled"] is True            # from the NaniteEnabled tag instead
    assert info["nanite_fallback_percent"] is None and info["triangles_lod0"] == 12


def test_enable_nanite_keeps_other_settings():
    ns, mesh, sub, saved = env()
    mesh.props["nanite_settings"] = la.Struct(enabled=False, fallback_percent_triangles=1.0, position_precision=4)
    out = ns["_cmd_staticmesh_enable_nanite"](MESH, True, 0.5)
    name, settings, apply_changes = sub.calls[-1]
    assert name == "set_nanite_settings" and apply_changes is True
    assert settings.get_editor_property("enabled") is True
    assert settings.get_editor_property("fallback_percent_triangles") == 0.5
    assert settings.get_editor_property("position_precision") == 4
    assert out["saved"] is True and saved == [MESH]
    assert out["nanite_settings_after"] == {"enabled": True, "fallback_percent_triangles": 0.5}


def test_remove_lods_and_collisions_do_not_read_back():
    ns, mesh, sub, saved = env()
    unsafe = ns["STATICMESH_UNSAFE"]
    out = ns["_cmd_staticmesh_remove_lods"](MESH)
    assert out["removed"] is True and out["saved"] is True and out["lod_count"].startswith(unsafe)
    out = ns["_cmd_staticmesh_remove_collisions"](MESH)
    assert out["removed"] is True and out["simple_collision_count"].startswith(unsafe)
    assert [c[0] for c in sub.calls] == ["remove_lods", "remove_collisions"] and saved == [MESH, MESH]


def test_generate_uv_gizmo_types_and_defaults():
    ns, mesh, sub, saved = env()
    gen = ns["_cmd_staticmesh_generate_uv"]
    out = gen(MESH, "planar", 0, 1, orientation={"pitch": 90})
    kind, lod, channel, pos, rot, tiling = sub.calls[-1]
    assert (kind, lod, channel) == ("planar", 0, 1)
    assert (pos.x, pos.y, pos.z) == (0.0, 0.0, 40.0)                  # the bounds center
    assert (rot.pitch, rot.yaw, rot.roll) == (90.0, 0.0, 0.0)
    assert (tiling.x, tiling.y) == (1.0, 1.0)
    assert out["ok"] and out["saved"] and out["orientation"] == {"pitch": 90.0, "yaw": 0.0, "roll": 0.0}
    gen(MESH, "cylindrical", position=[1, 2, 3], tiling=[2, 4])
    kind, _, _, pos, rot, tiling = sub.calls[-1]
    assert kind == "cylindrical" and (pos.x, pos.y, pos.z) == (1.0, 2.0, 3.0) and (tiling.x, tiling.y) == (2.0, 4.0)
    assert (rot.pitch, rot.yaw, rot.roll) == (0.0, 0.0, 0.0)
    out = gen(MESH, "box")
    kind, _, _, pos, rot, size = sub.calls[-1]
    assert kind == "box" and (size.x, size.y, size.z) == (100.0, 50.0, 80.0) and out["size"] == [100.0, 50.0, 80.0]
    count = len(sub.calls)
    for kwargs, match in (({"uv_type": "box", "tiling": [1, 1]}, "tiling"), ({"size": [1, 1, 1]}, "size"),
                          ({"orientation": [0, 90, 0]}, "ambiguous"), ({"position": [1, 2]}, "position"),
                          ({"uv_type": "sphere"}, "uv_type"), ({"tiling": [1, 2, 3]}, "tiling")):
        raises(ValueError, gen, MESH, match=match, **kwargs)
    assert len(sub.calls) == count  # refused before the mesh was touched


def test_tag_parsing():
    ns = env()[0]
    text, as_int, as_bool = ns["_tag_text"], ns["_tag_int"], ns["_tag_bool"]
    assert text("12") == "12" and text(None) is None and text("None") is None and text("") is None
    assert text((True, "12")) == "12" and text((False, "")) is None and text(("12", True)) == "12"
    assert as_int({"A": "1,024"}, "A") == 1024 and as_int({"A": "x"}, "A") is None and as_int({}, "A") is None
    assert as_bool({"A": "True"}, "A") is True and as_bool({"A": "False"}, "A") is False and as_bool({}, "A") is None


def test_docs_warn_about_the_getters():
    for rel in ("docs/tools_reference.md", "docs/troubleshooting.md"):
        with open(os.path.join(la.ROOT, *rel.split("/")), encoding="utf-8") as fh:
            text = fh.read()
        assert "not available safely in UEFN 42.20" in text and "has_vertex_colors" in text, rel


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
    sys.exit(1 if _run_all() else 0)
