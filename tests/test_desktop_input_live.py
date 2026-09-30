"""LIVE input test for desktop_control: it MOVES THE MOUSE, CLICKS AND TYPES.

The only target is a sandbox Tk window owned by this test process; the input allowlist is narrowed
to this Python interpreter, so no other window can receive input. It checks, end to end: focus,
click position (exact pixel), double click, drag, wheel, Unicode typing, key combos, a click in
screenshot pixels (shot=...), the allowlist refusal, the kill switch and WM_CLOSE.

Run it only when nobody is using the machine and no agent drives UEFN:

    python tests/test_desktop_input_live.py --yes-send-input

Stop it at any time: move the mouse into the top-left corner of a monitor (kill switch).
"""
import os
import sys
import threading
import time
import tkinter as tk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_EXE = os.path.basename(sys.executable)
os.environ["UEFN_DESKTOP_ALLOW"] = os.path.splitext(_EXE)[0]  # this interpreter only

import desktop_control as dc  # noqa: E402  (sets per-monitor DPI awareness before Tk starts)

TITLE = "UEFN-MCP desktop input test"
TEXT = "Hello UEFN - Привіт ✓"


def main() -> int:
    if "--yes-send-input" not in sys.argv[1:]:
        print(__doc__)
        return 2
    root = tk.Tk()
    root.title(TITLE)
    root.geometry("700x460+240+160")
    entry = tk.Entry(root, width=70)
    entry.pack(pady=10)
    canvas = tk.Canvas(root, width=640, height=340, bg="white")
    canvas.pack()
    ev = {"clicks": [], "double": 0, "drag": [], "wheel": [], "keys": [], "closed": False}
    canvas.bind("<Button-1>", lambda e: ev["clicks"].append((e.x_root, e.y_root)))
    canvas.bind("<Double-Button-1>", lambda e: ev.__setitem__("double", ev["double"] + 1))
    canvas.bind("<B1-Motion>", lambda e: ev["drag"].append((e.x_root, e.y_root)))
    root.bind_all("<MouseWheel>", lambda e: ev["wheel"].append(e.delta))
    root.bind_all("<Control-s>", lambda e: ev["keys"].append("ctrl+s"))
    root.bind_all("<F5>", lambda e: ev["keys"].append("f5"))
    root.protocol("WM_DELETE_WINDOW", lambda: (ev.__setitem__("closed", True), root.after(300, root.destroy)))
    root.update()
    geo = {"x": canvas.winfo_rootx(), "y": canvas.winfo_rooty(), "w": canvas.winfo_width(),
           "h": canvas.winfo_height(), "ex": entry.winfo_rootx() + 20, "ey": entry.winfo_rooty() + 8}
    results: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), str(detail)))
        print(f"{'ok  ' if ok else 'FAIL'} {name} {detail}")

    def read_entry() -> str:
        box, done = {}, threading.Event()
        root.after(0, lambda: (box.__setitem__("v", entry.get()), done.set()))
        done.wait(5)
        return box.get("v", "")

    def worker():
        try:
            time.sleep(0.8)
            cx, cy = geo["x"] + geo["w"] // 2, geo["y"] + geo["h"] // 2
            r = dc.op_focus(title=TITLE)
            check("focus", r["ok"], r["method"])
            dc.op_click(cx, cy, title=TITLE)
            time.sleep(0.3)
            check("click lands on the exact pixel", ev["clicks"] and ev["clicks"][-1] == (cx, cy),
                  f"{ev['clicks'][-1:]} vs {(cx, cy)}")
            dc.op_click(cx + 40, cy, title=TITLE, double=True)
            time.sleep(0.3)
            check("double click", ev["double"] >= 1)
            dc.op_drag(cx - 150, cy - 60, cx + 150, cy + 60, title=TITLE, duration_ms=500)
            time.sleep(0.3)
            last = ev["drag"][-1] if ev["drag"] else None
            check("drag", last and abs(last[0] - (cx + 150)) <= 2 and abs(last[1] - (cy + 60)) <= 2,
                  f"{len(ev['drag'])} motion events, last {last}")
            dc.op_scroll(cx, cy, 3, title=TITLE)
            time.sleep(0.3)
            check("wheel", len(ev["wheel"]) >= 3 and all(d > 0 for d in ev["wheel"]), ev["wheel"])
            dc.op_click(geo["ex"], geo["ey"], title=TITLE)
            dc.op_type(TEXT, title=TITLE)
            time.sleep(0.3)
            check("unicode typing", read_entry() == TEXT, repr(read_entry()))
            dc.op_key("ctrl+s", title=TITLE)
            dc.op_key("f5", title=TITLE)
            time.sleep(0.3)
            check("key combos", ev["keys"][-2:] == ["ctrl+s", "f5"], ev["keys"])
            shot = dc.screenshot(title=TITLE)
            mp = shot["mapping"]
            ix, iy = dc.screen_to_image(cx, cy, mp)
            dc.op_click(ix, iy, shot=shot["path"])
            time.sleep(0.3)
            got = ev["clicks"][-1]
            check("click in screenshot pixels", abs(got[0] - cx) <= mp["scale_x"] and abs(got[1] - cy) <= mp["scale_y"],
                  f"{got} vs {(cx, cy)} (scale {mp['scale_x']:.2f})")
            try:
                dc.op_key("f5", process="explorer")
                check("allowlist refuses other processes", False, "explorer was accepted")
            except dc.DesktopRefused as e:
                check("allowlist refuses other processes", True, str(e)[:80])
            m1 = dc.list_monitors()[0]
            dc._SetCursorPos(m1["left"] + 1, m1["top"] + 1)
            try:
                dc.op_click(cx, cy, title=TITLE)
                check("kill switch", False, "click was accepted with the cursor in the corner")
            except dc.DesktopRefused as e:
                check("kill switch", "kill switch" in str(e), str(e)[:60])
            dc._SetCursorPos(cx, cy)
            r = dc.op_close(title=TITLE, wait_sec=3)
            check("close via WM_CLOSE", ev["closed"] and r["ok"], r)
        except Exception as e:  # report instead of hanging the Tk loop
            check("unexpected error", False, f"{type(e).__name__}: {e}")
            root.after(0, root.destroy)

    threading.Thread(target=worker, daemon=True).start()
    root.mainloop()
    failed = [n for n, ok, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed; audit log: {dc.log_path()}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
