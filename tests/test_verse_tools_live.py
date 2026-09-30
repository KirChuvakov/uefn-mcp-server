"""Live smoke test for verse_workflow + verse_lsp_service.

Usage: python tests/test_verse_tools_live.py [compile|lsp|all] [path/to/file.verse]

- compile: needs UEFN running with the project open (talks to the Verse
  workflow socket, TCP 127.0.0.1:1962) and triggers a real Verse build.
- lsp: needs the epicgames.verse VS Code extension (verse-lsp.exe) and a
  UEFN-generated .code-workspace (the project was opened in UEFN at least
  once); UEFN itself does not have to run. The .verse file comes from the
  second argument or the VERSE_TEST_FILE environment variable, and defaults to
  Core/service.verse relative to the workspace's Content folder.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_compile():
    import verse_workflow
    print("=== verse_compile ===")
    t0 = time.time()
    r = verse_workflow.compile_project()
    r["info"] = r["info"][-5:]  # keep output short
    print(json.dumps(r, indent=2, ensure_ascii=False))
    print(f"--- took {time.time() - t0:.1f}s")


def test_lsp(target: str):
    import verse_lsp_service as lsp

    print(f"=== verse_symbols (cold start) on {target} ===")
    t0 = time.time()
    syms = lsp.document_symbols(target)
    print(json.dumps(syms[:10], indent=2))
    print(f"--- {len(syms)} symbols, took {time.time() - t0:.1f}s")

    print("=== verse_symbols again (warm — should be instant) ===")
    t0 = time.time()
    syms = lsp.document_symbols(target)
    print(f"--- {len(syms)} symbols, took {time.time() - t0:.1f}s")

    if syms:
        line = syms[0]["line"]
        print(f"=== verse_hover at L{line}:1 ===")
        t0 = time.time()
        print(lsp.hover(target, line, 1) or "(empty)")
        print(f"--- took {time.time() - t0:.1f}s")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    target = (sys.argv[2] if len(sys.argv) > 2 else "") or os.environ.get(
        "VERSE_TEST_FILE", os.path.join("Core", "service.verse"))
    if what in ("compile", "all"):
        test_compile()
    if what in ("lsp", "all"):
        test_lsp(target)
