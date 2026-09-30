"""Offline tests for the rotation convention (0.5.0 safety fix). No UEFN, no listener, no network.

unreal.Rotator's Python constructor is Rotator(roll, pitch, yaw). Before 0.5.0 the tools documented
rotation lists as [pitch, yaw, roll] but passed them positionally, so UEFN applied [roll, pitch, yaw].
On the real sources these tests check that:

- uefn_listener.py builds every unreal.Rotator with keywords, and every handler parameter named
  rotation / orientation goes through _make_rotator (AST scan);
- _parse_rotation / _make_rotator accept named axes and equal-value lists and refuse the rest;
- the handlers (spawn_actor, set_actor_transform, set_viewport_camera, focus_selected,
  niagara_place_actor, device @editable rotators), run against a fake `unreal` whose Rotator has the
  real signature, apply the axes the caller named;
- mcp_server.py exposes named axes in the tool schemas, sends them as such, and does not send
  rotation or static-mesh commands to a listener older than protocol 0.3.3.

    python tests/test_rotation_offline.py        (pytest also collects it)
"""
import ast
import asyncio
import math
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _listener_ast as la  # noqa: E402

ROTATION_HELPERS = ["ROTATION_AXES", "_finite_number", "_parse_rotation", "_make_rotator", "_float_list"]
HANDLERS = ROTATION_HELPERS + [
    "_serialize", "_serialize_actor", "_resolve_niagara_system", "_cmd_spawn_actor", "_cmd_set_actor_transform",
    "_cmd_set_viewport_camera", "_cmd_focus_selected", "_cmd_niagara_place_actor",
]


def raises(exc, fn, *a, match="", **kw):
    try:
        fn(*a, **kw)
    except exc as e:
        assert match in str(e), f"{match!r} not in {e!r}"
        return str(e)
    raise AssertionError(f"{getattr(fn, '__name__', fn)}{a}{kw} did not raise {exc.__name__}")


def axes(r) -> tuple:
    return (r.pitch, r.yaw, r.roll)


# -- the listener source ---------------------------------------------------------------------------

def test_every_rotator_is_built_with_keywords():
    found = 0
    for node, name in la.calls(la.listener_tree()):
        if name == "unreal.Rotator":
            found += 1
            assert not node.args, f"uefn_listener.py:{node.lineno}: positional unreal.Rotator(...) (order is roll, pitch, yaw)"
            assert {k.arg for k in node.keywords} <= {"roll", "pitch", "yaw"}, node.lineno
    assert found >= 5, found


def test_rotation_parameters_go_through_make_rotator():
    checked = 0
    for fn in la.listener_tree().body:
        if isinstance(fn, ast.FunctionDef) and fn.name.startswith("_cmd_"):
            for param in {a.arg for a in fn.args.args} & {"rotation", "orientation"}:
                checked += 1
                assert f"_make_rotator({param}" in ast.unparse(fn), f"{fn.name}: {param} bypasses _make_rotator"
    assert checked >= 5, checked


# -- parsing -----------------------------------------------------------------------------------------

def test_named_axes():
    parse = la.load(ROTATION_HELPERS, la.fake_unreal())["_parse_rotation"]
    assert parse({"pitch": 10, "yaw": 20, "roll": 30}) == {"pitch": 10.0, "yaw": 20.0, "roll": 30.0}
    assert parse({"yaw": 90}) == {"pitch": 0.0, "yaw": 90.0, "roll": 0.0}
    assert parse({}) == {"pitch": 0.0, "yaw": 0.0, "roll": 0.0}
    assert parse({"pitch": -89.5, "yaw": 359.9, "roll": -180}) == {"pitch": -89.5, "yaw": 359.9, "roll": -180.0}
    # the listener's own output (get_viewport_camera, actor rotations) feeds straight back in
    shown = {"pitch": -30.0, "yaw": 45.0, "roll": 0.0}
    assert parse(shown) == shown


def test_lists_only_when_all_three_values_are_equal():
    parse = la.load(ROTATION_HELPERS, la.fake_unreal())["_parse_rotation"]
    assert parse([0, 0, 0]) == {"pitch": 0.0, "yaw": 0.0, "roll": 0.0}
    assert parse((15, 15, 15)) == {"pitch": 15.0, "yaw": 15.0, "roll": 15.0}
    msg = raises(ValueError, parse, [0, 90, 0], match="ambiguous")
    # both readings, spelled out as named axes
    assert '{"pitch": 0, "yaw": 90, "roll": 0}' in msg and '{"pitch": 90, "yaw": 0, "roll": 0}' in msg
    raises(ValueError, parse, [10, 20, 30], match="ambiguous")
    raises(ValueError, parse, [1, 2], match="list of 2")
    raises(ValueError, parse, [], match="list of 0")


def test_refusals():
    parse = la.load(ROTATION_HELPERS, la.fake_unreal())["_parse_rotation"]
    for bad in ({"yew": 90}, {"Pitch": 1}, {"x": 1, "y": 2, "z": 3}):
        raises(ValueError, parse, bad, match="unknown key")
    for bad in ({"pitch": "10"}, {"pitch": True}, {"yaw": None}, [None] * 3):
        raises(ValueError, parse, bad, match="expected a number")
    raises(ValueError, parse, {"roll": float("nan")}, match="finite")
    raises(ValueError, parse, [math.inf] * 3, match="finite")
    for bad in ("0,90,0", 90, None):
        raises(ValueError, parse, bad, match="named axes")
    assert raises(ValueError, parse, [1, 2, 3], "orientation").startswith("orientation:")


def test_make_rotator_uses_the_real_argument_order():
    ns = la.load(ROTATION_HELPERS, la.fake_unreal())
    assert axes(ns["_make_rotator"]({"pitch": 10, "yaw": 20, "roll": 30})) == (10.0, 20.0, 30.0)
    # what the pre-0.5.0 code did with the documented [pitch, yaw, roll] list
    assert axes(la.Rotator(*[10, 20, 30])) == (20.0, 30.0, 10.0)


def test_float_list():
    f = la.load(ROTATION_HELPERS, la.fake_unreal())["_float_list"]
    assert f([1, 2.5, -3], 3, "position") == [1.0, 2.5, -3.0]
    raises(ValueError, f, [1, 2], 3, "position", match="position")
    raises(ValueError, f, [1, "2", 3], 3, "position", match="position[1]")


# -- handlers against a fake editor ----------------------------------------------------------------------

class Actor:
    def __init__(self, label="Cube", loc=(0.0, 0.0, 0.0)):
        self.label = label
        self.location = la.Vector(*loc)
        self.rotation = la.Rotator()
        self.scale = la.Vector(1, 1, 1)
        self.props = {}

    def get_name(self):
        return self.label + "_0"

    def get_actor_label(self):
        return self.label

    def set_actor_label(self, label):
        self.label = label

    def get_class(self):
        return types.SimpleNamespace(get_name=lambda: "StaticMeshActor")

    def get_path_name(self):
        return "/Game/Map.Map:PersistentLevel." + self.label

    def get_actor_location(self):
        return self.location

    def get_actor_rotation(self):
        return self.rotation

    def get_actor_scale3d(self):
        return self.scale

    def set_actor_location(self, loc, sweep, teleport):
        self.location = loc

    def set_actor_rotation(self, rot, teleport):
        self.rotation = rot

    def set_actor_scale3d(self, scale):
        self.scale = scale

    def get_editor_property(self, name):
        return self.props[name]


class Editor:
    """The fake editor: actor subsystem, level library and viewport camera in one object."""

    def __init__(self):
        self.actors, self.selected, self.spawned = [], [], []
        self.camera = (la.Vector(1, 2, 3), la.Rotator(pitch=-10, yaw=20))

    def get_all_level_actors(self):
        return list(self.actors)

    def get_selected_level_actors(self):
        return list(self.selected)

    def _spawn(self, what, loc, rot, label):
        actor = Actor(label)
        actor.location, actor.rotation = loc, rot
        actor.props["niagara_component"] = types.SimpleNamespace(set_asset=lambda system, reset: None)
        self.actors.append(actor)
        self.spawned.append((what, loc, rot))
        return actor

    def spawn_actor_from_class(self, cls, loc, rot):
        return self._spawn(cls, loc, rot, "NiagaraActor")

    def spawn_actor_from_object(self, asset, loc, rot):
        return self._spawn(asset, loc, rot, "Cube")

    def get_level_viewport_camera_info(self):
        return self.camera

    def set_level_viewport_camera_info(self, loc, rot):
        self.camera = (loc, rot)


def editor_namespace(editor):
    niagara_system = type("NiagaraSystem", (), {"get_path_name": lambda self: "/Game/FX/NS_Test.NS_Test"})
    unreal = la.fake_unreal(
        EditorActorSubsystem=object, get_editor_subsystem=lambda cls: editor, EditorLevelLibrary=editor,
        EditorAssetLibrary=types.SimpleNamespace(
            load_asset=lambda path: niagara_system() if "/FX/" in path else object()),
        NiagaraSystem=niagara_system, NiagaraActor=type("NiagaraActor", (), {}))
    return la.load(HANDLERS, unreal)


def test_spawn_actor():
    ed = Editor()
    spawn = editor_namespace(ed)["_cmd_spawn_actor"]
    out = spawn(asset_path="/Engine/BasicShapes/Cube", location=[1, 2, 3], rotation={"pitch": 10, "yaw": 20, "roll": 30})
    assert axes(ed.spawned[-1][2]) == (10.0, 20.0, 30.0)
    assert out["actor"]["rotation"] == {"pitch": 10.0, "yaw": 20.0, "roll": 30.0}
    assert spawn(asset_path="/Engine/BasicShapes/Cube")["actor"]["rotation"] == {"pitch": 0.0, "yaw": 0.0, "roll": 0.0}
    count = len(ed.spawned)
    raises(ValueError, spawn, asset_path="/Engine/BasicShapes/Cube", rotation=[0, 90, 0], match="ambiguous")
    assert len(ed.spawned) == count  # refused before anything was spawned


def test_set_actor_transform():
    ed = Editor()
    cube = Actor("Cube")
    ed.actors.append(cube)
    move = editor_namespace(ed)["_cmd_set_actor_transform"]
    out = move("Cube", rotation={"pitch": -20, "yaw": 135, "roll": 5})
    assert axes(cube.rotation) == (-20.0, 135.0, 5.0)
    assert out["actor"]["rotation"] == {"pitch": -20.0, "yaw": 135.0, "roll": 5.0}
    raises(ValueError, move, "Cube", location=[9, 9, 9], rotation=[1, 2, 3], match="ambiguous")
    assert (cube.location.x, cube.location.y, cube.location.z) == (0.0, 0.0, 0.0)  # nothing moved


def test_set_viewport_camera():
    ed = Editor()
    cam = editor_namespace(ed)["_cmd_set_viewport_camera"]
    out = cam(rotation={"pitch": -90})
    assert out["rotation"] == {"pitch": -90.0, "yaw": 0.0, "roll": 0.0}
    assert out["location"] == {"x": 1.0, "y": 2.0, "z": 3.0}           # location kept
    out = cam(location=[5, 6, 7])
    assert out["rotation"] == {"pitch": -90.0, "yaw": 0.0, "roll": 0.0}  # rotation kept
    assert cam(rotation=out["rotation"])["rotation"] == out["rotation"]    # its own output round-trips
    raises(ValueError, cam, rotation=[0, -90, 0], match="ambiguous")


def test_focus_selected_looks_at_the_selection():
    ed = Editor()
    ed.selected = [Actor("A", (0, 0, 0)), Actor("B", (400, 200, 100))]
    out = editor_namespace(ed)["_cmd_focus_selected"]()
    loc, rot = ed.camera
    c = out["center"]
    to_center = [c["x"] - loc.x, c["y"] - loc.y, c["z"] - loc.z]
    p, y = math.radians(rot.pitch), math.radians(rot.yaw)
    forward = [math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), math.sin(p)]  # FRotator::Vector()
    cos_angle = sum(f * t for f, t in zip(forward, to_center)) / math.sqrt(sum(t * t for t in to_center))
    assert cos_angle > math.cos(math.radians(1.0)), f"camera misses the selection by {math.degrees(math.acos(cos_angle)):.1f} deg"
    assert out["rotation"] == {"pitch": -35.0, "yaw": 45.0, "roll": 0.0}


def test_niagara_place_actor():
    ed = Editor()
    place = editor_namespace(ed)["_cmd_niagara_place_actor"]
    out = place("/Game/FX/NS_Test", location=[0, 0, 100], rotation={"pitch": 90})
    assert axes(ed.spawned[-1][2]) == (90.0, 0.0, 0.0)
    assert out["rotation"] == {"pitch": 90.0, "yaw": 0.0, "roll": 0.0}
    count = len(ed.spawned)
    raises(ValueError, place, "/Game/FX/NS_Test", rotation=[90, 0, 0], match="ambiguous")
    assert len(ed.spawned) == count


def test_device_rotator_fields():
    ns = la.load(ROTATION_HELPERS + ["_coerce_editable_value", "_is_unreal_array", "_sniff_array_inner_type",
                                     "_find_actor"], la.fake_unreal())
    coerce = ns["_coerce_editable_value"]
    dev = Actor("Device")
    dev.props["SpawnRotation"] = la.Rotator(roll=3, pitch=1, yaw=2)
    assert axes(coerce(dev, "SpawnRotation", {"yaw": 90}, "auto")) == (0.0, 90.0, 0.0)
    assert axes(coerce(dev, "SpawnRotation", {"pitch": 10, "yaw": 20, "roll": 30}, "rotator")) == (10.0, 20.0, 30.0)
    assert axes(coerce(dev, "SpawnRotation", [0, 0, 0], "rotator")) == (0.0, 0.0, 0.0)
    raises(ValueError, coerce, dev, "SpawnRotation", [10, 20, 30], "rotator", match="SpawnRotation (rotator)")
    assert [r.yaw for r in coerce(dev, "Turns", [{"yaw": 1}, {"yaw": 2}], "array:rotator")] == [1.0, 2.0]


# -- mcp_server.py ---------------------------------------------------------------------------------

def _server():
    """Import mcp_server without reaching a listener (the port scan range becomes empty)."""
    if "mcp_server" not in sys.modules:
        old = os.environ.get("UEFN_MCP_PORT")
        os.environ["UEFN_MCP_PORT"] = "18765"
        sys.path.insert(0, la.ROOT)
        try:
            import mcp_server  # noqa: F401
        finally:
            if old is None:
                os.environ.pop("UEFN_MCP_PORT", None)
            else:
                os.environ["UEFN_MCP_PORT"] = old
    return sys.modules["mcp_server"]


ROTATION_TOOLS = (("spawn_actor", "rotation"), ("set_actor_transform", "rotation"),
                  ("set_viewport_camera", "rotation"), ("niagara_place_actor", "rotation"),
                  ("staticmesh_generate_uv", "orientation"))


def test_server_schemas_name_the_axes():
    ms = _server()
    tools = {t.name: t for t in asyncio.run(ms.mcp.list_tools())}
    for tool, param in ROTATION_TOOLS:
        schema = tools[tool].inputSchema
        rot = schema["$defs"]["Rotation"]
        assert set(rot["properties"]) == {"pitch", "yaw", "roll"} and rot["additionalProperties"] is False, tool
        options = schema["properties"][param]["anyOf"]
        assert {"$ref": "#/$defs/Rotation"} in options, (tool, options)
        arr = next(o for o in options if o.get("type") == "array")
        assert arr["minItems"] == arr["maxItems"] == 3, tool
        assert "named axes" in tools[tool].description.lower(), tool


def test_server_sends_named_axes_and_guards_old_listeners():
    ms = _server()
    sent = []

    def fake_send(command, params=None, timeout=30.0, min_protocol=None):
        sent.append((command, params, min_protocol))
        return {"ok": True}

    original, ms._send_command = ms._send_command, fake_send
    try:
        def call(tool, args):
            asyncio.run(ms.mcp.call_tool(tool, args))
            return sent[-1]

        cube = {"asset_path": "/Engine/BasicShapes/Cube"}
        assert call("spawn_actor", dict(cube, rotation={"yaw": 90})) == (
            "spawn_actor", dict(cube, rotation={"pitch": 0.0, "yaw": 90.0, "roll": 0.0}), ms.SAFETY_PROTOCOL)
        assert call("spawn_actor", cube)[2] is None  # no rotation: any listener will do
        assert call("set_actor_transform", {"actor_path": "Cube", "rotation": {"roll": 5}})[1]["rotation"] == {
            "pitch": 0.0, "yaw": 0.0, "roll": 5.0}
        assert call("set_viewport_camera", {"location": [0, 0, 500]})[2] is None
        assert call("set_viewport_camera", {"rotation": {"pitch": -90}})[2] == ms.SAFETY_PROTOCOL
        assert call("niagara_place_actor", {"system_path": "/x", "rotation": [0, 0, 0]})[1]["rotation"] == [0.0, 0.0, 0.0]
        assert call("focus_selected", {})[2] == ms.SAFETY_PROTOCOL
        for tool in ("staticmesh_get_info", "staticmesh_enable_nanite", "staticmesh_remove_lods",
                     "staticmesh_remove_collisions", "staticmesh_generate_uv"):
            assert call(tool, {"asset_path": "/Game/SM"})[2] == ms.SAFETY_PROTOCOL, tool
        uv = call("staticmesh_generate_uv", {"asset_path": "/Game/SM", "uv_type": "box", "orientation": {"yaw": 45},
                                              "size": [10, 10, 10]})[1]
        assert uv["orientation"] == {"pitch": 0.0, "yaw": 45.0, "roll": 0.0} and uv["size"] == [10, 10, 10]
        assert call("device_set_editable", {"actor_path": "D", "field": "F", "value": {"yaw": 1}})[2] == ms.SAFETY_PROTOCOL
        assert call("device_set_editable", {"actor_path": "D", "field": "F", "value": 5})[2] is None
        assert call("device_set_editables_bulk", {"actor_path": "D", "fields": [
            {"name": "A", "value": 1}, {"name": "B", "value": [0, 0, 0], "value_type": "rotator"}]})[2] == ms.SAFETY_PROTOCOL
        for bad in ({"yew": 1}, [1, 2], {"pitch": "up"}):
            count = len(sent)
            raises(Exception, call, "spawn_actor", dict(cube, rotation=bad), match="rotation")
            assert len(sent) == count, bad  # refused by the schema, nothing sent
    finally:
        ms._send_command = original


def test_require_protocol():
    ms = _server()
    saved = ms._listener_protocol
    try:
        for version, ok in (("0.3.3", True), ("0.3.10", True), ("0.4.0", True), ("1.0", True),
                            ("0.3.2", False), ("0.2.0", False), ("", False), (None, False)):
            ms._listener_protocol = version
            if ok:
                ms._require_protocol("spawn_actor", ms.SAFETY_PROTOCOL)
            else:
                msg = raises(RuntimeError, ms._require_protocol, "spawn_actor", ms.SAFETY_PROTOCOL, match="0.3.3")
                assert "nothing was sent" in msg and "uefn_listener.py" in msg
    finally:
        ms._listener_protocol = saved


def test_listener_protocol_satisfies_the_guard():
    version = la.load(["PROTOCOL_VERSION"])["PROTOCOL_VERSION"]
    ms = _server()
    assert ms._version_tuple(version) >= ms.SAFETY_PROTOCOL, version


def test_device_rotator_detection():
    f = _server()._device_value_may_be_rotator
    assert f("rotator", 5) and f("array:rotator", []) and f("Rotator", {"yaw": 1})
    assert f("auto", {"yaw": 1}) and f("auto", [0, 90, 0]) and f("auto", [{"pitch": 1}, {"yaw": 2}])
    for value_type, value in (("auto", 5), ("auto", "Label"), ("int", [1, 2, 3]), ("auto", [1, 2]),
                              ("auto", True), ("auto", ["a", "b", "c"]), ("vector", {"x": 1})):
        assert not f(value_type, value), (value_type, value)


def test_server_wire_form_parses_in_the_listener():
    ms = _server()
    parse = la.load(ROTATION_HELPERS, la.fake_unreal())["_parse_rotation"]
    for model in (ms.Rotation(), ms.Rotation(yaw=90), ms.Rotation(pitch=-10, yaw=45, roll=5)):
        assert parse(ms._wire_rotation(model)) == {"pitch": model.pitch, "yaw": model.yaw, "roll": model.roll}


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
