"""MCP server exposing PADS Logic / PADS Layout (PADS 9.x) automation."""
from __future__ import annotations

import asyncio
import os

import win32con
import win32gui
from mcp.server.fastmcp import FastMCP, Image

from . import screenshot
from .comworker import ComWorker, DialogWatcher, click_button, com_error_text, main_window
from . import impedance
from .layout import PadsLayout
from .logic import PadsLogic

# NOTE: stay DPI-unaware. PADS 9.x is DPI-unaware, so PrintWindow renders it at its logical
# size; a DPI-aware caller would get physical window rects and a partly black capture.

def _default_workdir() -> str:
    # source checkout (git clone / editable install): <repo>/work
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if os.path.isfile(os.path.join(repo, "pyproject.toml")):
        return os.path.join(repo, "work")
    # installed package (uvx / pip): never write next to site-packages
    return os.path.join(os.path.expanduser("~"), "Documents", "pads-mcp", "work")


WORKDIR = os.path.abspath(os.environ.get("PADS_MCP_WORKDIR") or _default_workdir())
os.makedirs(WORKDIR, exist_ok=True)

INSTRUCTIONS = f"""
Controls PADS Logic (schematics) and PADS Layout (PCB) 9.x running on this PC via COM.

Conventions
- Units are mils; y grows upwards (PADS convention). Schematic grid is 100 mil.
- Pins are 'REFDES.PIN', e.g. 'U1.3' (pin aliases like 'U1.PA9' are accepted when the part defines pin names).
- Schematic edits work on a COPY in the work folder ({WORKDIR}); logic_open copies by default.
  Wiring edits (logic_wire / logic_draw) are atomic: export ASCII -> edit -> re-import -> save,
  with a backup in work/history (logic_undo restores it). Each re-imports the whole design
  (a few seconds), so batch operations.

Typical schematic workflow
1. logic_status / logic_open
2. logic_get_part to learn pin positions, outward directions and current nets
3. logic_draw (parts + nets in one go) or logic_wire (connect / power / label / disconnect)
4. pads_screenshot(app='logic', sheet=...) to visually check the result

Drawing guidance: place parts on the 100 mil grid with >= 600 mil clearance; prefer net labels
('label') for long or crowded connections, power/ground symbols for supply nets, wires for short
local connections between nearby pins.

PCB (PADS Layout) - all PCB coordinates are mm, y up. Layers: 1 = Top, N = Bottom.
- All routing is done in PADS Layout. There is no autorouter: when the user asks for (automatic)
  routing, plan every connection yourself and draw it with layout_route, many connections per call.
- PADS Layout COM cannot draw traces: layout_route writes YOUR planned path into the design
  (ASCII round trip of the whole PCB, atomic, backup in work/history, layout_undo restores).
- Agent routing loop: layout_overview -> layout_get_net / layout_region (pins, pad layers, existing
  copper) -> plan the path yourself (layer, corners, vias, width from the net-class rules) ->
  layout_route (batch several connections) -> fix what its `check` reports -> next batch.
- layout_route checks the new routes itself (clearance to pads / tracks / vias / board outline and
  track width, against the design rules) and returns the findings in `check`. Fix them by re-routing
  the pin pair. layout_check runs the same check on all routed copper (or some nets / a region).
  pads-mcp never starts PADS Router. The check skips pours/planes, text and drill holes, so when routing
  is done, ask the user to run Tools > Verify Design in PADS Layout as the final check.
- SMD pads exist only on their side's layer: a route must leave/enter them on that layer; change
  layers with a via (a path point [x, y, new_layer]). Through-hole pads (layer null) work on any layer.
- Keep 45/90 degree geometry, respect track width / clearance rules, keep clear of other nets' pads.
- Rules and impedance: layout_get_rules / layout_set_rules, layout_stackup / layout_impedance.
If a call times out, PADS is probably showing a modal dialog: use pads_dialogs / pads_click_dialog.
"""

mcp = FastMCP("pads", instructions=INSTRUCTIONS)
worker = ComWorker()
watcher = DialogWatcher()
logic = PadsLogic(WORKDIR)
layout = PadsLayout(WORKDIR)


async def run(fn, *args, timeout: float = 120, **kw):
    fut = worker.submit(fn, *args, **kw)
    try:
        return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(fut)), timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"PADS did not answer within {timeout:.0f}s (the call is still running). "
            f"Open PADS dialogs: {watcher.current()}. Answer with pads_click_dialog, then check status.")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(com_error_text(e)) from None


# =========================================================================== common
@mcp.tool()
async def pads_status() -> dict:
    """Show whether PADS Logic / PADS Layout are running, which files are open, and object counts."""
    return {"logic": await run(logic.status), "layout": await run(layout.status),
            "open_dialogs": watcher.current(), "auto_answered_dialogs": watcher.recent_auto()[-5:],
            "workdir": WORKDIR}


@mcp.tool()
async def pads_dialogs() -> list:
    """List modal dialogs currently shown by PADS (title, texts, buttons)."""
    return watcher.current()


@mcp.tool()
async def pads_click_dialog(title: str, button: str) -> dict:
    """Click a button (by caption, e.g. 'OK', 'Yes', 'Cancel') in an open PADS dialog with the given title."""
    for d in watcher.current():
        if d["title"] == title:
            return {"clicked": click_button(d["hwnd"], button), "dialog": d}
    return {"clicked": False, "error": f"no dialog titled {title!r}", "open": watcher.current()}


@mcp.tool()
async def pads_screenshot(app: str = "logic", sheet: str | None = None, zoom: str = "sheet",
                          region: list[int] | None = None, maximize: bool = False,
                          max_width: int = 1600) -> Image:
    """Capture the PADS window as an image so you can see the drawing.

    app: 'logic' or 'layout'. sheet: schematic sheet to show (logic only).
    zoom: 'sheet' (whole sheet) | 'all' | 'board' (layout) | 'none' (keep current view).
    region: [x1, y1, x2, y2] to zoom into (overrides zoom) - mils for logic, mm for layout.
    maximize: maximize the PADS window first for a larger, more detailed image."""
    if maximize:
        hwnd = main_window(app)
        if hwnd:
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
            await asyncio.sleep(0.8)
    if app == "logic":
        await run(logic.show, sheet, zoom, region)
    elif app == "layout":
        await run(layout.show, "board" if zoom == "sheet" else zoom, region)
    else:
        raise ValueError("app must be 'logic' or 'layout'")
    await asyncio.sleep(0.8)
    hwnd = main_window(app)
    if not hwnd:
        raise RuntimeError(f"PADS {app} window not found")
    png = await asyncio.to_thread(screenshot.capture, hwnd, max_width)
    return Image(data=png, format="png")


# =========================================================================== PADS Logic
@mcp.tool()
async def logic_status() -> dict:
    """Active schematic in PADS Logic: path, sheets, part/net counts."""
    return await run(logic.status)


@mcp.tool()
async def logic_open(path: str, copy_to_workdir: bool = True) -> dict:
    """Open a PADS Logic .sch (launches PADS Logic if needed). By default the file is first COPIED
    into the work folder and the copy is opened, so the original is never modified."""
    return await run(logic.open, path, copy_to_workdir, timeout=180)


@mcp.tool()
async def logic_new(path: str) -> dict:
    """Create a new blank schematic and save it at `path` (.sch)."""
    return await run(logic.new, path, timeout=120)


@mcp.tool()
async def logic_save(path: str | None = None) -> dict:
    """Save the active schematic (optionally Save As `path`)."""
    return await run(logic.save, path)


@mcp.tool()
async def logic_list_parts(sheet: str | None = None, pattern: str | None = None, limit: int = 300) -> dict:
    """List parts (refdes, part type, sheet, x, y, rotation, value). `pattern` is a wildcard on refdes or part type, e.g. 'U*'."""
    return await run(logic.list_parts, sheet, pattern, limit)


@mcp.tool()
async def logic_get_part(refdes: str) -> dict:
    """Part details: attributes, every pin with position, outward direction (left/right/up/down) and net,
    plus the pin bounding box (useful for placing neighbours and planning wires)."""
    return await run(logic.get_part, refdes)


@mcp.tool()
async def logic_list_nets(pattern: str | None = None, limit: int = 400) -> dict:
    """List nets with pin counts. `pattern` is a wildcard, e.g. '*3V3*'."""
    return await run(logic.list_nets, pattern, limit)


@mcp.tool()
async def logic_get_net(name: str) -> dict:
    """Pins (with sheet and position) on a net."""
    return await run(logic.get_net, name)


@mcp.tool()
async def logic_search_library(pattern: str, limit: int = 100) -> dict:
    """Search PADS libraries for part types (wildcards allowed, e.g. '*1117*')."""
    return await run(logic.search_library, pattern, limit, timeout=180)


@mcp.tool()
async def logic_add_sheet(name: str) -> dict:
    """Add a new schematic sheet (no-op if it already exists)."""
    return await run(logic.add_sheet, name)


@mcp.tool()
async def logic_add_part(part_type: str, x: int, y: int, refdes: str | None = None,
                         sheet: str | None = None, rotate: int = 0) -> dict:
    """Place a part from the PADS LIBRARY at (x, y) mils. For part types that exist only in this design
    (not in a library), use logic_draw with clone_from instead."""
    return await run(logic.add_part, part_type, refdes, sheet, x, y, rotate)


@mcp.tool()
async def logic_move_part(refdes: str, x: int | None = None, y: int | None = None,
                          rotate: int | None = None, mirror: bool | None = None) -> dict:
    """Move / rotate (0, 90, 180, 270) / mirror a part. Attached wires are not re-routed;
    check with pads_screenshot and fix with logic_wire if needed."""
    return await run(logic.move_part, refdes, x, y, rotate, mirror)


@mcp.tool()
async def logic_delete_part(refdes: str) -> dict:
    """Delete a part."""
    return await run(logic.delete_part, refdes)


@mcp.tool()
async def logic_rename_part(refdes: str, new_refdes: str) -> dict:
    """Change a part's reference designator."""
    return await run(logic.rename_part, refdes, new_refdes)


@mcp.tool()
async def logic_set_attribute(refdes: str, name: str, value: str) -> dict:
    """Set (or add) a part attribute such as 'Value'."""
    return await run(logic.set_attribute, refdes, name, value)


@mcp.tool()
async def logic_wire(ops: list[dict]) -> dict:
    """Edit schematic connectivity in ONE atomic transaction (all succeed or nothing changes).

    ops items:
      {"op": "connect", "from": "U1.3", "to": "R5.1", "net": "optional"}   orthogonal wire between two pins (same sheet)
      {"op": "power", "pin": "C5.2", "net": "GND"}        ground / supply symbol with a short stub
      {"op": "label", "pin": "U1.10", "net": "UART_TX"}   net label (off-sheet symbol) on a stub
      {"op": "disconnect", "pin": "U1.3"}                 remove wires at the pin (+ dangling tie dots/symbols)
      {"op": "net", "name": "VIN", "pins": [...], "style": "auto|wire|label|power"}
    A pin that already has a net can only be joined to that same net."""
    return await run(logic.apply_edits, ops, timeout=300)


@mcp.tool()
async def logic_draw(parts: list[dict] | None = None, nets: list[dict] | None = None,
                     sheet: str | None = None) -> dict:
    """Draw a circuit: place parts, then wire nets — the main tool for automatic schematic drawing.

    sheet: target sheet (created if missing); parts default to it.
    parts: [{"refdes": "C100", "part_type": "C", "x": 1000, "y": 2000, "rotate": 0,
             "clone_from": "C3", "attributes": {"Value": "10uF"}}]
       - with clone_from: copies an existing part of this design (no library needed; attributes override)
       - without clone_from: part_type is taken from the PADS libraries
    nets: [{"name": "GND", "pins": ["C100.2", "U5.4"], "style": "auto"}]
       style auto = power/ground symbols for supply nets, wires for a few nearby pins, labels otherwise.
    Place parts on a 100 mil grid with generous spacing; then check with pads_screenshot."""
    out: dict = {}
    if sheet:
        out["sheet"] = await run(logic.add_sheet, sheet)
    clones, added = [], []
    for p in parts or []:
        if p.get("clone_from"):
            clones.append({"source": p["clone_from"], "refdes": p["refdes"], "sheet": p.get("sheet", sheet),
                           "x": p["x"], "y": p["y"], "rotate": p.get("rotate", 0),
                           "attributes": p.get("attributes")})
        else:
            r = await run(logic.add_part, p["part_type"], p.get("refdes"), p.get("sheet", sheet),
                          p["x"], p["y"], p.get("rotate", 0))
            added.append(r["added"])
            for k, v in (p.get("attributes") or {}).items():
                await run(logic.set_attribute, r["added"], k, v)
    out["library_parts"] = added
    if clones:
        out["cloned"] = await run(logic.clone_parts, clones, timeout=300)
    if nets:
        ops = [{"op": "net", "name": n["name"], "pins": n["pins"], "style": n.get("style", "auto")} for n in nets]
        out["wiring"] = await run(logic.apply_edits, ops, timeout=300)
    elif added:
        await run(logic.save)
    return out


@mcp.tool()
async def logic_undo() -> dict:
    """Restore the schematic to the state before the last logic_wire / logic_draw transaction."""
    return await run(logic.undo, timeout=180)


@mcp.tool()
async def logic_export(kind: str, path: str) -> dict:
    """Export the active schematic: kind='ascii' (PADS Logic ASCII .txt) or 'netlist' (PADS Layout netlist .asc)."""
    return await run(logic.export, kind, path)


@mcp.tool()
async def logic_import_ascii(path: str, save_as: str) -> dict:
    """Advanced: import a PADS Logic ASCII file into a new schematic and save it as `save_as`."""
    return await run(logic.import_ascii, path, save_as, timeout=300)


# =========================================================================== PADS Layout
@mcp.tool()
async def layout_status() -> dict:
    """Active PCB in PADS Layout: path, counts of components, nets, route segments, vias, unrouted connections."""
    return await run(layout.status)


@mcp.tool()
async def layout_open(path: str) -> dict:
    """Open a .pcb in PADS Layout (launches it if needed)."""
    return await run(layout.open, path, timeout=180)


@mcp.tool()
async def layout_overview() -> dict:
    """Start here for PCB work: board outline/bbox, electrical layers, stack-up, via types, net classes and
    rules, and every net with its unrouted-connection count (mm)."""
    return await run(layout.overview, timeout=180)


@mcp.tool()
async def layout_list_components(pattern: str | None = None, limit: int = 400) -> dict:
    """List PCB components (refdes, part type, decal, x, y in mm, rotation, side, glued)."""
    return await run(layout.list_components, pattern, limit)


@mcp.tool()
async def layout_get_component(refdes: str) -> dict:
    """PCB component with every pad: position (mm), pad copper layer (SMD) and net."""
    return await run(layout.get_component, refdes)


@mcp.tool()
async def layout_move_component(refdes: str, x: float | None = None, y: float | None = None,
                                rotation: float | None = None) -> dict:
    """Move / rotate a PCB component (mm, degrees). Changes are in memory until layout_save."""
    return await run(layout.move_component, refdes, x, y, rotation)


@mcp.tool()
async def layout_list_nets(pattern: str | None = None, limit: int = 400) -> dict:
    """List PCB nets with pin counts and unrouted-connection counts."""
    return await run(layout.list_nets, pattern, limit)


@mcp.tool()
async def layout_get_net(name: str) -> dict:
    """Everything needed to route a net: pins (mm, pad layer), net class + width/clearance rules,
    and its connections (pin pairs) with their current routed paths."""
    return await run(layout.get_net, name, timeout=180)


@mcp.tool()
async def layout_region(x1: float, y1: float, x2: float, y2: float) -> dict:
    """Obstacles inside a window (mm): pads with nets/layers, routed track segments and vias.
    Use it to plan a path that keeps clear of other nets."""
    return await run(layout.region, x1, y1, x2, y2, timeout=180)


@mcp.tool()
async def layout_route(routes: list[dict]) -> dict:
    """Draw traces exactly as you planned them (one atomic edit for the whole batch).

    Each route: {"from": "U2.26", "to": "R2.1", "layer": "top"|"bottom"|1..N,
                 "path": [[x, y], [x, y], [x, y, "bottom"], ...], "width": mm (optional), "via": name (optional)}
    - from/to are pins of the same net; their exact pad centres are added automatically,
      so `path` holds only the intermediate corner points (mm). An empty path = straight line.
    - A point with a third element changes layer there (a via of type `via`, default STANDARDVIA).
    - Start on the from-pad's layer and arrive on the to-pad's layer if they are SMD.
    - width defaults to the net class REC_TRACK_WIDTH. Re-routing a pin pair replaces its old route;
      other connections of the net are kept, ratsnest is rebuilt as needed.
    Uses PADS Layout only. The result's `check` lists clearance / width problems of the NEW routes
    (gap and required value in mm, position, the other object); an empty findings list means clean."""
    return await run(layout.route, routes, timeout=600)


@mcp.tool()
async def layout_check(nets: list[str] | None = None, region: list[float] | None = None,
                       max_report: int = 100) -> dict:
    """pads-mcp's own clearance check (no PADS Router): routed tracks and vias against pads, other
    nets' tracks and vias, and the board outline, plus track width, using the design's clearance
    rules. Scope: all routed copper, only `nets`, or connections touching `region` [x1, y1, x2, y2] mm.
    Findings: type (e.g. track-smd, via-track, width, track-outline), layer, at [x, y], gap and
    required (mm), short=true when copper touches. Not checked: pours/planes, text, drill holes,
    pad-to-pad; finish with Tools > Verify Design in PADS Layout."""
    return await run(layout.check, nets, region, max_report, timeout=300)


@mcp.tool()
async def layout_unroute(nets: list[str] | None = None, pairs: list[list[str]] | None = None) -> dict:
    """Remove routed copper: whole `nets` and/or specific connections `pairs` [[from, to], ...]."""
    return await run(layout.unroute, nets, pairs, timeout=600)


@mcp.tool()
async def layout_get_rules(full: bool = False) -> dict:
    """Net classes and design rules (widths, clearances, routing, high-speed incl. impedance/length)."""
    return await run(layout.get_rules, full, timeout=180)


@mcp.tool()
async def layout_set_rules(net_classes: dict[str, list[str] | None] | None = None,
                           rules: list[dict] | None = None) -> dict:
    """Create/replace net classes and set rules (atomic, backed up).

    net_classes: {"USB": ["USB_DP", "USB_DM"], "OLD": null (delete)}
    rules: [{"target": "DEFAULT" | <class> | "NET:<net>",
             "width": mm or {"min","rec","max"}, "clearance": mm (track/via/pad/smd spacing),
             "auto_route": bool, "priority": 1-5, "layers": [1, 2], "via_types": ["STANDARDVIA"],
             "max_vias": int, "length_min"/"length_max"/"match_tolerance"/"stub_max": mm,
             "impedance_min"/"impedance_max": ohm,
             "raw": {"clearance"|"route"|"high_speed": {PADS_KEY: value}}}]"""
    return await run(layout.set_rules, net_classes, rules, timeout=600)


@mcp.tool()
async def layout_stackup() -> dict:
    """Electrical layer stack-up: copper thickness and the dielectric (thickness, Er) below each layer."""
    return await run(layout.stackup, timeout=180)


@mcp.tool()
async def layout_set_stackup(layers: list[dict]) -> dict:
    """Edit the stack-up (atomic, backed up): [{"layer": 1, "dielectric_below_mm": 1.5, "er_below": 4.5,
    "copper_mm": 0.035}]. Layer n's dielectric is the one directly below copper layer n."""
    return await run(layout.set_stackup, layers, timeout=600)


@mcp.tool()
async def layout_impedance(layer: int = 1, target_ohm: float | None = None, width_mm: float | None = None,
                           diff_gap_mm: float | None = None, dielectric_mm: float | None = None,
                           er: float | None = None, copper_mm: float | None = None) -> dict:
    """Trace impedance from the stack-up: give target_ohm to get the width, or width_mm to get Z0.
    diff_gap_mm makes it a differential pair (target = Zdiff). dielectric_mm/er/copper_mm override the
    design's stack-up (for what-if). Closed-form estimate; confirm with the fab."""
    st = await run(layout.stackup, timeout=180)
    geo = impedance.geometry_for_layer(st, layer)
    if dielectric_mm is not None:
        geo["h" if geo["kind"] == "microstrip" else "b"] = dielectric_mm
    if er is not None:
        geo["er"] = er
    if copper_mm is not None:
        geo["t"] = copper_mm
    if target_ohm is None and width_mm is None:
        raise ValueError("give target_ohm or width_mm")
    return impedance.evaluate(geo, width_mm, target_ohm, diff_gap_mm)


@mcp.tool()
async def layout_undo() -> dict:
    """Restore the PCB from the most recent backup in work/history."""
    return await run(layout.undo, timeout=180)


@mcp.tool()
async def layout_select(refdes: list[str]) -> dict:
    """Select components in PADS Layout and zoom to them."""
    return await run(layout.select, refdes)


@mcp.tool()
async def layout_save(path: str | None = None) -> dict:
    """Save the PCB (optionally Save As `path`)."""
    return await run(layout.save, path)


@mcp.tool()
async def layout_export(kind: str, path: str) -> dict:
    """Export the PCB: kind='ascii', 'netlist' or 'rules'."""
    return await run(layout.export, kind, path)


PROGIDS = {"PADS Logic": "PowerLogic.Application", "PADS Layout": "PowerPCB.Application"}


def _com_server(progid: str) -> str | None:
    """Executable registered for a COM ProgID (PADS 9.x registers in the 32-bit view)."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\CLSID") as k:
            clsid = winreg.QueryValueEx(k, "")[0]
    except OSError:
        return None
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32", 0,
                                winreg.KEY_READ | view) as k:
                return winreg.QueryValueEx(k, "")[0]
        except OSError:
            pass
    return None


def _check() -> int:
    """Installation self-test: version, work folder and PADS COM registration."""
    from . import __version__
    print(f"pads-mcp {__version__}\nworkdir: {WORKDIR}")
    found = {}
    for name, progid in PROGIDS.items():
        found[name] = exe = _com_server(progid)
        print(f"  {'OK' if exe else '--'}  {name:12} {progid:26} {exe or 'not registered'}")
    if not (found["PADS Logic"] or found["PADS Layout"]):
        print("PADS Logic/Layout COM servers not found: install PADS 9.x (and run it once).")
        return 1
    print("ready")
    return 0


def main():
    import argparse
    from . import __version__
    p = argparse.ArgumentParser(prog="pads-mcp",
                                description="MCP server (stdio) for PADS Logic / PADS Layout 9.x")
    p.add_argument("--version", action="version", version=f"pads-mcp {__version__}")
    p.add_argument("--check", action="store_true",
                   help="check the installation and PADS COM registration, then exit")
    if p.parse_args().check:
        raise SystemExit(_check())
    mcp.run()


if __name__ == "__main__":
    main()
