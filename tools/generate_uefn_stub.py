"""Generate a .pyi type stub from live UEFN editor introspection.

Run inside UEFN editor:
    py generate_uefn_stub.py

Output:
    <ProjectDir>/Saved/uefn_stub.pyi

This generates a Python type stub covering ALL types available in UEFN (37K+),
including Fortnite-specific classes not in the standard UE5 stub.

Use cases:
    - IDE autocomplete (VS Code / PyCharm) for ALL UEFN types
    - Input for tools/generate_api_docs.py to generate full documentation
    - Replace the UE5-only unreal_api_stub.py with actual UEFN API

Expected output size: ~30-50 MB (vs 384 MB for the JSON dump).
Expected time: 3-10 minutes depending on machine.
"""

import unreal
import os
from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe(func, default=None):
    """Call func, return default on any exception."""
    try:
        return func()
    except Exception:
        return default


def _safe_doc(obj: Any) -> str:
    try:
        d = getattr(obj, "__doc__", None)
        return d if isinstance(d, str) else ""
    except Exception:
        return ""


def _safe_dir(obj: Any) -> List[str]:
    try:
        return sorted(dir(obj))
    except Exception:
        return []


def _is_enum(cls: type) -> bool:
    try:
        return issubclass(cls, unreal.EnumBase)
    except (TypeError, AttributeError):
        return False


def _is_struct(cls: type) -> bool:
    try:
        return issubclass(cls, unreal.StructBase)
    except (TypeError, AttributeError):
        return False


def _immediate_bases(cls: type) -> List[str]:
    """Get immediate base class names (skip object/type/internal)."""
    skip = {"object", "type", "_WrapperBase", "_ObjectBase", "_EnumEntry"}
    try:
        mro = cls.__mro__
        if len(mro) < 2:
            return []
        bases = []
        for b in mro[1:]:
            name = b.__name__
            if name not in skip:
                bases.append(name)
                break  # Only immediate parent
        return bases
    except Exception:
        return []


def _format_doc_block(doc: str, indent: str = "    ") -> str:
    """Format docstring as a Python docstring block, keeping it compact."""
    if not doc:
        return ""

    lines = doc.strip().split("\n")

    # Keep: first paragraph + C++ Source section
    kept: List[str] = []
    in_cpp = False
    blank_seen = False

    for line in lines:
        s = line.strip()

        if "**C++ Source:**" in s:
            in_cpp = True
            kept.append("")
            kept.append(s)
            continue

        if in_cpp:
            if s.startswith("- **"):
                kept.append(s)
                continue
            elif s:
                in_cpp = False
            else:
                continue

        if not s:
            blank_seen = True
            continue

        if not blank_seen:
            kept.append(s)

    if not kept:
        return ""

    text = "\n".join(kept).strip()
    if "\n" in text:
        return f'{indent}r"""\n{indent}{text}\n{indent}"""'
    else:
        return f'{indent}r"""{text}"""'


# ---------------------------------------------------------------------------
# Stub generation
# ---------------------------------------------------------------------------


def _generate_enum_stub(name: str, cls: type) -> List[str]:
    """Generate stub lines for an enum class."""
    bases = _immediate_bases(cls)
    base_str = f"({bases[0]})" if bases else "(EnumBase)"

    lines = [f"class {name}{base_str}:"]

    doc = _format_doc_block(_safe_doc(cls))
    if doc:
        lines.append(doc)

    members = [m for m in _safe_dir(cls) if not m.startswith("_")]
    for member_name in members:
        try:
            val = getattr(cls, member_name)
            if hasattr(val, "value"):
                doc_str = getattr(val, "__doc__", "") or ""
                comment = f"  # {val.value}"
                if doc_str and doc_str != str(val):
                    first = doc_str.strip().split("\n")[0][:100]
                    comment = f"  # {val.value}: {first}"
                lines.append(f"    {member_name}: {name} = ...{comment}")
                continue
        except Exception:
            pass

        # Methods (cast, static_enum, etc.)
        try:
            member = getattr(cls, member_name)
            if callable(member):
                sig = _get_method_sig(cls, member_name)
                lines.append(f"    def {member_name}{sig}: ...")
        except Exception:
            pass

    if len(lines) == 1 or (len(lines) == 2 and doc):
        lines.append("    ...")

    lines.append("")
    return lines


def _get_method_sig(cls: type, name: str) -> str:
    """Extract method signature from docstring."""
    try:
        member = getattr(cls, name)
        doc = getattr(member, "__doc__", None)
        if doc:
            first = doc.strip().split("\n")[0]
            paren = first.find("(")
            if paren != -1:
                # Remove the " -- description" part
                sig = first[paren:]
                if " -- " in sig:
                    sig = sig.split(" -- ")[0]
                # Clean up trailing colon
                sig = sig.rstrip(":")
                return sig
    except Exception:
        pass
    return "(self, *args, **kwargs) -> Any"


def _get_method_desc(cls: type, name: str) -> str:
    """Extract method description from docstring."""
    try:
        member = getattr(cls, name)
        doc = getattr(member, "__doc__", None)
        if doc:
            first = doc.strip().split("\n")[0]
            if " -- " in first:
                return first.split(" -- ", 1)[1].strip()
    except Exception:
        pass
    return ""


def _generate_class_stub(name: str, cls: type) -> List[str]:
    """Generate stub lines for a regular class or struct."""
    bases = _immediate_bases(cls)
    base_str = f"({bases[0]})" if bases else ""

    lines = [f"class {name}{base_str}:"]

    doc = _format_doc_block(_safe_doc(cls))
    if doc:
        lines.append(doc)

    members = [m for m in _safe_dir(cls) if not m.startswith("_")]
    has_content = False

    for member_name in members:
        try:
            member = getattr(cls, member_name)
        except Exception:
            continue

        if isinstance(member, property):
            # Property
            ret = "Any"
            pdoc = _safe_doc(member.fget) if member.fget else ""
            if pdoc:
                first = pdoc.strip().split("\n")[0]
                if " -> " in first:
                    ret_part = first.split(" -> ")[-1].strip()
                    if " -- " in ret_part:
                        ret_part = ret_part.split(" -- ")[0].strip()
                    if ret_part:
                        ret = ret_part
            desc = ""
            if pdoc and " -- " in pdoc.split("\n")[0]:
                desc = pdoc.split("\n")[0].split(" -- ", 1)[1].strip()
            comment = f"  # {desc}" if desc else ""
            lines.append(f"    @property")
            lines.append(f"    def {member_name}(self) -> {ret}: ...{comment}")
            has_content = True
        elif callable(member):
            sig = _get_method_sig(cls, member_name)
            desc = _get_method_desc(cls, member_name)
            comment = f"  # {desc}" if desc else ""
            lines.append(f"    def {member_name}{sig}: ...{comment}")
            has_content = True

    if not has_content:
        lines.append("    ...")

    lines.append("")
    return lines


def generate_stub() -> str:
    """Generate the complete .pyi stub content."""
    all_names = sorted(_safe_dir(unreal))

    # Categorize
    functions: List[Tuple[str, Any]] = []
    enum_classes: List[Tuple[str, type]] = []
    struct_classes: List[Tuple[str, type]] = []
    regular_classes: List[Tuple[str, type]] = []

    for name in all_names:
        if name.startswith("_"):
            continue
        try:
            obj = getattr(unreal, name)
        except Exception:
            continue

        if isinstance(obj, type):
            if _is_enum(obj):
                enum_classes.append((name, obj))
            elif _is_struct(obj):
                struct_classes.append((name, obj))
            else:
                regular_classes.append((name, obj))
        elif callable(obj):
            functions.append((name, obj))

    total = len(functions) + len(enum_classes) + len(struct_classes) + len(regular_classes)
    unreal.log(f"Generating stub for {total} types "
               f"({len(regular_classes)} classes, {len(enum_classes)} enums, "
               f"{len(struct_classes)} structs, {len(functions)} functions)")

    # Header
    lines: List[str] = [
        "# UEFN Python API Type Stub",
        "# Auto-generated from live UEFN editor introspection",
        f"# Types: {total} ({len(regular_classes)} classes, "
        f"{len(enum_classes)} enums, {len(struct_classes)} structs, "
        f"{len(functions)} functions)",
        "#",
        "# Usage: add this file to python.analysis.extraPaths in VS Code settings",
        "",
        "from __future__ import annotations",
        "from typing import (Any, Callable, Dict, ItemsView, Iterable, Iterator,",
        "                    KeysView, List, Mapping, MutableMapping, MutableSequence,",
        "                    MutableSet, Optional, Set, Sequence, Text, Tuple, Type,",
        "                    TypeVar, Union, ValuesView)",
        "",
    ]

    progress_idx = 0

    with unreal.ScopedSlowTask(total, "Generating UEFN stub...") as task:
        task.make_dialog(True)

        # Top-level functions
        lines.append("# " + "=" * 70)
        lines.append("# Top-level functions")
        lines.append("# " + "=" * 70)
        lines.append("")

        for name, obj in functions:
            if task.should_cancel():
                unreal.log_warning("Stub generation cancelled by user")
                break
            task.enter_progress_frame(1, f"Function: {name}")
            sig = _get_method_sig_toplevel(name)
            desc = _get_desc_toplevel(name)
            comment = f"  # {desc}" if desc else ""
            lines.append(f"def {name}{sig}: ...{comment}")
            lines.append("")

        # Enums
        lines.append("# " + "=" * 70)
        lines.append(f"# Enums ({len(enum_classes)})")
        lines.append("# " + "=" * 70)
        lines.append("")

        for name, cls in enum_classes:
            if task.should_cancel():
                break
            task.enter_progress_frame(1, f"Enum: {name}")
            lines.extend(_generate_enum_stub(name, cls))

        # Structs
        lines.append("# " + "=" * 70)
        lines.append(f"# Structs ({len(struct_classes)})")
        lines.append("# " + "=" * 70)
        lines.append("")

        for name, cls in struct_classes:
            if task.should_cancel():
                break
            task.enter_progress_frame(1, f"Struct: {name}")
            lines.extend(_generate_class_stub(name, cls))

        # Classes
        lines.append("# " + "=" * 70)
        lines.append(f"# Classes ({len(regular_classes)})")
        lines.append("# " + "=" * 70)
        lines.append("")

        for name, cls in regular_classes:
            if task.should_cancel():
                break
            task.enter_progress_frame(1, f"Class: {name}")
            lines.extend(_generate_class_stub(name, cls))

    return "\n".join(lines)


def _get_method_sig_toplevel(name: str) -> str:
    """Get signature for a top-level unreal function."""
    try:
        func = getattr(unreal, name)
        doc = getattr(func, "__doc__", None)
        if doc:
            first = doc.strip().split("\n")[0]
            paren = first.find("(")
            if paren != -1:
                sig = first[paren:]
                if " -- " in sig:
                    sig = sig.split(" -- ")[0]
                return sig.rstrip(":")
    except Exception:
        pass
    return "(*args, **kwargs) -> Any"


def _get_desc_toplevel(name: str) -> str:
    """Get description for a top-level unreal function."""
    try:
        func = getattr(unreal, name)
        doc = getattr(func, "__doc__", None)
        if doc:
            first = doc.strip().split("\n")[0]
            if " -- " in first:
                return first.split(" -- ", 1)[1].strip()[:100]
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    unreal.log("=" * 60)
    unreal.log("  UEFN Stub Generator — Starting")
    unreal.log("=" * 60)

    content = generate_stub()

    output_path = os.path.join(
        unreal.Paths.project_saved_dir(), "uefn_stub.pyi"
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    n_lines = content.count("\n")

    unreal.log("=" * 60)
    unreal.log(f"  Stub saved: {output_path}")
    unreal.log(f"  Size: {size_mb:.1f} MB, {n_lines:,} lines")
    unreal.log("=" * 60)
    unreal.log("")
    unreal.log("Next steps:")
    unreal.log("  1. Copy uefn_stub.pyi to your project's docs/ folder")
    unreal.log("  2. Add to VS Code settings:")
    unreal.log('     "python.analysis.extraPaths": ["./docs"]')
    unreal.log("  3. Generate docs: python tools/generate_api_docs.py --stub docs/uefn_stub.pyi")


main()
