"""LIVE check of the 0.5.0 safety fixes against a running UEFN listener. It CHANGES THE OPEN LEVEL.

Not run yet (pending): run it on a scratch level of a scratch project, never on a production island,
with nobody else driving the editor, after saving everything:

    python tests/test_safety_fixes_live.py --yes-touch-editor [--camera] [--niagara /Mount/FX/NS_X] [--mesh /Mount/Meshes/SM_X]

What it does, through the MCP server's own tool functions (so the Rotation model, the wire form and the
listener are all exercised):

1. ping: the listener must report protocol 0.3.3 or later (0.5.0 safety fixes).
2. spawn_actor: a cube far from the play space with rotation {pitch 10, yaw 20, roll 30}; the actor
   must report exactly those axes. set_actor_transform to {pitch -20, yaw 135, roll 5}, same check.
   spawn_actor with the ambiguous list [0, 90, 0] must be refused.
3. --niagara: niagara_place_actor with {pitch 90}, checked the same way.
4. --camera (moves the viewport camera, restores it afterwards; the studio rule is never to move the
   user's camera, so only with their consent): set_viewport_camera {pitch -45, yaw 90} read back with
   get_viewport_camera, and focus_selected on the test cube must look at it (pitch -35, yaw 45).
5. staticmesh_get_info on /Engine/BasicShapes/Cube (and --mesh): safe facts present, the crash-list
   values marked "not available safely in UEFN 42.20". The editor must still answer ping afterwards.
6. Deletes every actor it spawned. It never saves: discard the level changes (or save, your call).
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAR = [987654.0, 0.0, 50000.0]  # out of the way of any island content
UNSAFE = "not available safely in UEFN 42.20"


def main() -> int:
    argv = sys.argv[1:]
    if "--yes-touch-editor" not in argv:
        print(__doc__)
        return 2

    def opt(name):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else ""

    import mcp_server as ms

    results, spawned = [], []

    def check(name, ok, detail=""):
        results.append((name, bool(ok)))
        print(f"{'ok  ' if ok else 'FAIL'} {name} {detail}")

    def call(fn, **kwargs):
        return json.loads(fn(**kwargs))

    def close(rot, pitch, yaw, roll, tol=0.01):
        return all(abs(((a - b) + 180.0) % 360.0 - 180.0) <= tol
                   for a, b in ((rot["pitch"], pitch), (rot["yaw"], yaw), (rot["roll"], roll)))

    try:
        ping = json.loads(ms.ping())
        version = ms._version_tuple(ping.get("version"))
        check("listener protocol >= 0.3.3", version is not None and version >= ms.SAFETY_PROTOCOL, ping.get("version"))
        if not results[-1][1]:
            return 1

        out = call(ms.spawn_actor, asset_path="/Engine/BasicShapes/Cube", location=FAR,
                   rotation=ms.Rotation(pitch=10, yaw=20, roll=30))
        cube = out["actor"]["path"]
        spawned.append(cube)
        check("spawn_actor applies named axes", close(out["actor"]["rotation"], 10, 20, 30), out["actor"]["rotation"])
        out = call(ms.set_actor_transform, actor_path=cube, rotation=ms.Rotation(pitch=-20, yaw=135, roll=5))
        check("set_actor_transform applies named axes", close(out["actor"]["rotation"], -20, 135, 5),
              out["actor"]["rotation"])
        try:
            extra = call(ms.spawn_actor, asset_path="/Engine/BasicShapes/Cube", location=FAR, rotation=[0, 90, 0])
            spawned.append(extra["actor"]["path"])
            check("ambiguous rotation list refused", False, "an actor was spawned")
        except RuntimeError as e:
            check("ambiguous rotation list refused", "ambiguous" in str(e), str(e).splitlines()[0][:120])

        niagara = opt("--niagara")
        if niagara:
            out = call(ms.niagara_place_actor, system_path=niagara, location=FAR, rotation=ms.Rotation(pitch=90),
                       label="MCP_SAFETY_TEST_FX")
            spawned.append(out["actor_path"])
            check("niagara_place_actor applies named axes", close(out["rotation"], 90, 0, 0), out["rotation"])

        if "--camera" in argv:
            saved = call(ms.get_viewport_camera)
            try:
                call(ms.set_viewport_camera, rotation=ms.Rotation(pitch=-45, yaw=90))
                now = call(ms.get_viewport_camera)
                check("set_viewport_camera applies named axes", close(now["rotation"], -45, 90, 0), now["rotation"])
                call(ms.select_actors, actor_paths=[cube])
                out = call(ms.focus_selected)
                check("focus_selected looks down at the selection", close(out["rotation"], -35, 45, 0), out["rotation"])
            finally:
                loc = saved["location"]
                call(ms.set_viewport_camera, location=[loc["x"], loc["y"], loc["z"]],
                     rotation=ms.Rotation(**saved["rotation"]))
                print("camera restored")

        for mesh in ["/Engine/BasicShapes/Cube"] + ([opt("--mesh")] if opt("--mesh") else []):
            info = call(ms.staticmesh_get_info, asset_path=mesh)
            safe = [info.get(k) for k in ("triangles_lod0", "verts_lod0", "lod_count", "material_slots", "bounds")]
            check(f"staticmesh_get_info safe facts ({mesh})", all(v is not None for v in safe),
                  {k: info.get(k) for k in ("triangles_lod0", "verts_lod0", "lod_count", "material_slots",
                                            "nanite_enabled", "read_errors")})
            check(f"staticmesh_get_info marks the crash-list values ({mesh})",
                  str(info.get("has_vertex_colors", "")).startswith(UNSAFE))
            time.sleep(1.0)
            check("editor alive after staticmesh_get_info", json.loads(ms.ping()).get("status") == "ok")
    finally:
        if spawned:
            try:
                out = json.loads(ms.delete_actors(actor_paths=spawned))
                print(f"deleted {out.get('count')} of {len(spawned)} test actors (the level is not saved)")
            except Exception as e:  # report, never hide
                print(f"CLEANUP FAILED, delete these actors by hand: {spawned} ({e})")
    failed = [n for n, ok in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
