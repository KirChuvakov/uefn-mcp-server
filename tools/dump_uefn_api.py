"""Dump all available classes, functions, enums, and subsystems from the `unreal` module.

Run inside UEFN editor via: py dump_unreal_api.py
Output: Saved/uefn_api_dump.json in the project directory.

The dump can then be compared against the full UE5 stub to find what's
missing (or extra) in UEFN.
"""

import unreal
import inspect
import json
import os
from typing import Any, Dict, List


def _safe_getattr(obj: Any, name: str, default: Any = None) -> Any:
    """Get attribute without raising."""
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _get_members(obj: Any) -> List[str]:
    """Get all public member names of an object."""
    try:
        return [name for name in dir(obj) if not name.startswith("_")]
    except Exception:
        return []


def _classify_member(parent: Any, name: str) -> str:
    """Classify a member as method, property, constant, or unknown."""
    try:
        val = getattr(parent, name)
    except Exception:
        return "inaccessible"

    if callable(val):
        return "method"
    if isinstance(val, property):
        return "property"
    if isinstance(val, (int, float, str, bool)):
        return "constant"
    return "attribute"


def _get_method_signature(obj: Any, name: str) -> str:
    """Try to get the signature/docstring of a method."""
    try:
        member = getattr(obj, name)
        doc = getattr(member, "__doc__", None)
        if doc:
            # Return first line of docstring (usually the signature)
            first_line = doc.strip().split("\n")[0]
            return first_line[:300]
        return ""
    except Exception:
        return ""


def dump_api() -> Dict:
    """Introspect the entire unreal module and return structured data."""

    result = {
        "metadata": {
            "engine_version": str(_safe_getattr(unreal, "get_engine_version", lambda: "unknown")()),
            "source": "UEFN editor introspection",
        },
        "top_level_functions": {},
        "classes": {},
        "enums": {},
        "structs": {},
        "subsystems_available": [],
        "subsystems_unavailable": [],
    }

    all_names = dir(unreal)
    unreal.log(f"Total names in unreal module: {len(all_names)}")

    # Categorize every name in the unreal module
    for name in all_names:
        if name.startswith("_"):
            continue

        try:
            obj = getattr(unreal, name)
        except Exception as e:
            result["classes"][name] = {"error": str(e)}
            continue

        # Top-level functions
        if callable(obj) and not isinstance(obj, type):
            sig = _get_method_signature(unreal, name)
            result["top_level_functions"][name] = sig
            continue

        # Classes / Enums / Structs
        if isinstance(obj, type):
            members = _get_members(obj)
            class_info = {
                "base_classes": [],
                "members": {},
            }

            # Base classes
            try:
                for base in obj.__mro__[1:]:
                    base_name = base.__name__
                    if base_name not in ("object", "type"):
                        class_info["base_classes"].append(base_name)
            except Exception:
                pass

            # Classify members
            for member_name in members:
                kind = _classify_member(obj, member_name)
                sig = ""
                if kind == "method":
                    sig = _get_method_signature(obj, member_name)
                class_info["members"][member_name] = {"kind": kind, "signature": sig}

            # Determine if enum
            try:
                if issubclass(obj, unreal.EnumBase):
                    result["enums"][name] = class_info
                    continue
            except (TypeError, AttributeError):
                pass

            # Determine if struct-like (StructBase)
            try:
                if issubclass(obj, unreal.StructBase):
                    result["structs"][name] = class_info
                    continue
            except (TypeError, AttributeError):
                pass

            result["classes"][name] = class_info
            continue

    # Test common subsystems
    subsystem_classes = [
        "EditorActorSubsystem",
        "EditorAssetSubsystem",
        "LevelEditorSubsystem",
        "StaticMeshEditorSubsystem",
        "EditorValidatorSubsystem",
        "UnrealEditorSubsystem",
        "AssetEditorSubsystem",
        "ImportSubsystem",
        "LayersSubsystem",
    ]

    for sub_name in subsystem_classes:
        sub_cls = _safe_getattr(unreal, sub_name)
        if sub_cls is None:
            result["subsystems_unavailable"].append(sub_name)
            continue
        try:
            instance = unreal.get_editor_subsystem(sub_cls)
            if instance is not None:
                result["subsystems_available"].append(sub_name)
            else:
                result["subsystems_unavailable"].append(sub_name)
        except Exception as e:
            result["subsystems_unavailable"].append(f"{sub_name} (error: {e})")

    # Test common library classes
    library_classes = [
        "EditorAssetLibrary",
        "EditorLevelLibrary",
        "EditorUtilityLibrary",
        "EditorFilterLibrary",
        "EditorSkeletalMeshLibrary",
        "MaterialEditingLibrary",
        "EditorLevelUtils",
        "AutomationLibrary",
        "AssetToolsHelpers",
        "AssetRegistryHelpers",
        "KismetMathLibrary",
        "KismetSystemLibrary",
        "KismetStringLibrary",
        "GameplayStatics",
        "SystemLibrary",
    ]

    result["libraries_available"] = []
    result["libraries_unavailable"] = []
    for lib_name in library_classes:
        lib_cls = _safe_getattr(unreal, lib_name)
        if lib_cls is not None:
            result["libraries_available"].append(lib_name)
        else:
            result["libraries_unavailable"].append(lib_name)

    return result


def main() -> None:
    """Run the dump and save to JSON."""
    unreal.log("=== Starting UEFN API dump ===")

    data = dump_api()

    # Summary
    n_classes = len(data["classes"])
    n_enums = len(data["enums"])
    n_structs = len(data["structs"])
    n_funcs = len(data["top_level_functions"])
    total = n_classes + n_enums + n_structs + n_funcs

    unreal.log(f"Classes: {n_classes}")
    unreal.log(f"Enums: {n_enums}")
    unreal.log(f"Structs: {n_structs}")
    unreal.log(f"Top-level functions: {n_funcs}")
    unreal.log(f"Total API surface: {total}")
    unreal.log(f"Subsystems OK: {data['subsystems_available']}")
    unreal.log(f"Subsystems MISSING: {data['subsystems_unavailable']}")
    unreal.log(f"Libraries OK: {data['libraries_available']}")
    unreal.log(f"Libraries MISSING: {data['libraries_unavailable']}")

    # Save to project Saved/ directory
    output_path = os.path.join(
        unreal.Paths.project_saved_dir(), "uefn_api_dump.json"
    )
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    unreal.log(f"=== Dump saved to: {output_path} ===")


main()
