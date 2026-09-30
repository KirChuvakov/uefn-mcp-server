"""Offline access to uefn_listener.py for tests: parse it, and compile selected definitions.

The listener imports `unreal` and starts its HTTP server when imported, so the offline tests never
import it. They read the real source with `ast`, scan it (for example for calls that crash UEFN),
and compile only the top-level functions and constants they exercise into a namespace whose
`unreal` is a small fake. Not a test module itself (no `test_` prefix).
"""
import __future__
import ast
import json
import math
import os
import re
import types
from typing import Any, Callable, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LISTENER = os.path.join(ROOT, "uefn_listener.py")


def listener_source() -> str:
    with open(LISTENER, encoding="utf-8") as fh:
        return fh.read()


def listener_tree() -> ast.Module:
    return ast.parse(listener_source(), LISTENER)


def _defined_names(node: ast.stmt) -> List[str]:
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def load(names, fake_unreal: Any = None, extra: Optional[dict] = None) -> dict:
    """Compile the named top-level definitions of uefn_listener.py and return their namespace.

    Every definition of a name is taken in file order, so the last one wins as it does when Python
    runs the file. `@_register("cmd")` decorators record the handler in ns["_HANDLERS"].
    """
    wanted = set(names)
    body = [node for node in listener_tree().body if set(_defined_names(node)) & wanted]
    found = {n for node in body for n in _defined_names(node)}
    if wanted - found:
        raise KeyError(f"not defined in uefn_listener.py: {sorted(wanted - found)}")
    handlers: Dict[str, Callable] = {}

    def _register(name: str):
        def deco(fn: Callable) -> Callable:
            handlers[name] = fn
            return fn
        return deco

    ns = {"__builtins__": __builtins__, "__name__": "uefn_listener_offline", "Any": Any, "Callable": Callable,
          "Dict": Dict, "List": List, "Optional": Optional, "json": json, "math": math, "os": os, "re": re,
          "unreal": fake_unreal, "_register": _register, "_HANDLERS": handlers}
    ns.update(extra or {})
    code = compile(ast.Module(body=body, type_ignores=[]), LISTENER, "exec",
                   flags=__future__.annotations.compiler_flag, dont_inherit=True)
    exec(code, ns)
    return ns


def calls(tree: ast.AST):
    """Every call node with the dotted source text of what is called ('unreal.Rotator', 'sub.remove_lods')."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node, ast.unparse(node.func)


# -- fake unreal structs (the Python signatures of the real ones) ---------------------------------

class Rotator:
    """unreal.Rotator: Python constructor Rotator(roll=0.0, pitch=0.0, yaw=0.0)."""

    def __init__(self, roll: float = 0.0, pitch: float = 0.0, yaw: float = 0.0):
        self.roll, self.pitch, self.yaw = float(roll), float(pitch), float(yaw)

    def __repr__(self) -> str:
        return f"Rotator(pitch={self.pitch}, yaw={self.yaw}, roll={self.roll})"


class Vector:
    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)


class Vector2D:
    def __init__(self, x: float = 0.0, y: float = 0.0):
        self.x, self.y = float(x), float(y)


class Box:
    def __init__(self, lo: Vector, hi: Vector):
        self.min, self.max = lo, hi


class Struct:
    """A property bag with get/set_editor_property, like unreal struct wrappers."""

    def __init__(self, **props):
        self._props = dict(props)

    def get_editor_property(self, name: str):
        if name not in self._props:
            raise AttributeError(f"no property {name!r}")
        return self._props[name]

    def set_editor_property(self, name: str, value) -> None:
        if name not in self._props:
            raise AttributeError(f"no property {name!r}")
        self._props[name] = value


def fake_unreal(**attrs) -> types.SimpleNamespace:
    """A fake `unreal` module with the struct types _serialize() checks, plus `attrs`."""
    class _Unused:
        pass

    base = {"Rotator": Rotator, "Vector": Vector, "Vector2D": Vector2D, "LinearColor": type("LinearColor", (), {}),
            "Color": type("Color", (), {}), "Transform": type("Transform", (), {}),
            "AssetData": type("AssetData", (), {}), "Actor": _Unused}
    base.update(attrs)
    return types.SimpleNamespace(**base)
