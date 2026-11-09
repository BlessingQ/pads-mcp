"""PADS Layout operations. Every public method must run on the COM worker thread.

PCB coordinates are millimetres (COM unit ppcbUnitMetric) regardless of the design's own unit.
Traces cannot be created through PADS Layout COM, so routing edits are an ASCII round trip:
export ASCII -> edit *ROUTE* / rules / stack-up -> import into a blank design -> Save As the same file
(with a backup in work/history; layout_undo restores it).
"""
from __future__ import annotations

import fnmatch
import math
import os
import shutil
import time

import pythoncom
import win32com.client.dynamic

from . import pcb_ascii as P
from .logic import G_natural, getp, items

MM = 4  # ppcbUnitMetric


class PadsLayout:
    PROGID = "PowerPCB.Application"

    def __init__(self, workdir: str):
        self.workdir = workdir
        self.tmpdir = os.path.join(workdir, "tmp")
        self.histdir = os.path.join(workdir, "history")
        os.makedirs(self.tmpdir, exist_ok=True)
        os.makedirs(self.histdir, exist_ok=True)
        self._app = None

    # ------------------------------------------------------------------ connection
    def app(self, launch: bool = False):
        if self._app is not None:
            try:
                _ = self._app.Name
                return self._app
            except Exception:  # noqa: BLE001
                self._app = None
        try:
            unk = pythoncom.GetActiveObject(self.PROGID)
            a = win32com.client.dynamic.Dispatch(unk.QueryInterface(pythoncom.IID_IDispatch))
        except pythoncom.com_error:
            if not launch:
                return None
            a = win32com.client.dynamic.Dispatch(self.PROGID)
            a.Visible = True
        self._app = a
        return a

    def doc(self):
        a = self.app()
        if a is None:
            raise RuntimeError("PADS Layout is not running")
        d = a.ActiveDocument
        if d is None:
            raise RuntimeError("PADS Layout has no open design")
        return d

    def status(self) -> dict:
        a = self.app()
        if a is None:
            return {"running": False}
        d = a.ActiveDocument
        out = {"running": True, "version": a.Version, "unit": "mm"}
        if d is not None:
            out.update(document=d.Name, path=d.FullName, saved=bool(d.Saved),
                       components=d.Components.Count, nets=d.Nets.Count,
                       electrical_layers=d.ElectricalLayerCount,
                       route_segments=d.RouteSegments.Count, vias=d.Vias.Count,
                       connections=d.Connections.Count)
        return out

    def open(self, path: str) -> dict:
        a = self.app(launch=True)
        a.OpenDocument(os.path.abspath(path))
        return self.status()

    # ------------------------------------------------------------------ components / nets
    def _comp_row(self, c) -> dict:
        return {"refdes": c.Name, "part_type": c.PartType, "decal": c.Decal,
                "x": round(getp(c, "PositionX", MM), 4), "y": round(getp(c, "PositionY", MM), 4),
                "rotation": c.Orientation, "side": c.LayerName, "glued": bool(c.Glued),
                "smd": bool(c.IsSMD)}

    def list_components(self, pattern: str | None = None, limit: int = 400) -> dict:
        d = self.doc()
        rows = []
        for c in items(d.Components):
            if pattern and not (fnmatch.fnmatchcase(c.Name.upper(), pattern.upper())
                                or fnmatch.fnmatchcase(str(c.PartType).upper(), pattern.upper())
                                or fnmatch.fnmatchcase(str(c.Decal).upper(), pattern.upper())):
                continue
            rows.append(self._comp_row(c))
        rows.sort(key=lambda r: G_natural(r["refdes"]))
        return {"unit": "mm", "count": len(rows), "components": rows[:limit], "truncated": len(rows) > limit}

    def _pin_side(self, d, comp, pin) -> int | None:
        """Copper layer of an SMD pad (1 = top, N = bottom); None for through-hole."""
        if not pin.IsSMD:
            return None
        return d.ElectricalLayerCount if "bottom" in str(comp.LayerName).lower() else 1

    @staticmethod
    def _pin_name(comp, p) -> str:
        n = p.Name                      # PADS Layout already returns "REF.PIN"
        return n if "." in n else f"{comp.Name}.{n}"

    def _pin_row(self, d, comp, p, pad: bool = False) -> dict:
        row = {"pin": self._pin_name(comp, p), "x": round(getp(p, "PositionX", MM), 5),
               "y": round(getp(p, "PositionY", MM), 5), "layer": self._pin_side(d, comp, p),
               "net": p.Net.Name if p.Net is not None else None}
        if pad:
            row["pad"] = _pad_geometry(comp, p)
        return row

    def get_component(self, refdes: str) -> dict:
        d = self.doc()
        c = d.Components.Item(refdes)
        if c is None:
            raise KeyError(f"no component {refdes}")
        out = self._comp_row(c)
        out["pins"] = sorted((self._pin_row(d, c, p, pad=True) for p in items(c.Pins)),
                             key=lambda r: G_natural(r["pin"]))
        out["pin_layer_note"] = "layer: copper layer of an SMD pad; null = through-hole (all layers)"
        return out

    def move_component(self, refdes: str, x: float | None, y: float | None, rotation: float | None) -> dict:
        d = self.doc()
        c = d.Components.Item(refdes)
        if c is None:
            raise KeyError(f"no component {refdes}")
        if c.Glued:
            raise RuntimeError(f"{refdes} is glued; unglue it in PADS Layout first")
        if x is not None or y is not None:
            nx = x if x is not None else getp(c, "PositionX", MM)
            ny = y if y is not None else getp(c, "PositionY", MM)
            c.Move(float(nx), float(ny), MM)
        if rotation is not None:
            c.Orientation = float(rotation)
        return self._comp_row(c)

    def list_nets(self, pattern: str | None = None, limit: int = 400) -> dict:
        d = self.doc()
        rows = []
        for n in items(d.Nets):
            if pattern and not fnmatch.fnmatchcase(n.Name.upper(), pattern.upper()):
                continue
            rows.append({"net": n.Name, "pins": n.Pins.Count})
        rows.sort(key=lambda r: G_natural(r["net"]))
        routed = _routed_counts(self._export(d))
        for r in rows:
            r.update(routed.get(r["net"], {}))
        return {"count": len(rows), "nets": rows[:limit], "truncated": len(rows) > limit}

    def pins_of(self, d, names: set[str] | None = None) -> dict[str, dict]:
        """{'U1.3': {x, y, layer, net}} for all pins (or only `names`)."""
        out = {}
        for c in items(d.Components):
            if names is not None and not any(n.split(".", 1)[0] == c.Name for n in names):
                continue
            for p in items(c.Pins):
                r = self._pin_row(d, c, p)
                if names is None or r["pin"] in names:
                    out[r["pin"]] = r
        return out

    def get_net(self, name: str) -> dict:
        d = self.doc()
        n = d.Nets.Item(name)
        if n is None:
            raise KeyError(f"no net {name}")
        pcb = self._export(d)
        pins = [self._pin_name(p.Component, p) for p in items(n.Pins)]
        pinfo = self.pins_of(d, set(pins))
        cls = next((c for c, nets in pcb.net_classes().items() if name in nets), None)
        f = 1 / pcb._unit_factor(_header_unit(pcb))
        conns = []
        for c in P.Routes(pcb).connections(name):
            row = {"from": c.a, "to": c.b, "routed": c.routed}
            if c.routed:
                row["start_layer"] = c.pts[0].layer
                row["path"] = _path_of(c, f)
                row["width"] = round(c.pts[0].width * f, 4)
            conns.append(row)
        return {"net": name, "unit": "mm", "net_class": cls,
                "rules": self._rules_for(pcb, cls),
                "pins": sorted(pinfo.values(), key=lambda r: G_natural(r["pin"])),
                "connections": conns,
                "routed_length": round(getp(n, "Length", True, MM), 3),
                "pin_layer_note": "layer = copper layer of an SMD pad (1 top); null = through-hole"}

    @staticmethod
    def _rules_for(pcb: P.PcbAscii, cls: str | None) -> dict:
        r = pcb.get_rules()["rules"]
        out = dict(r.get("DEFAULT", {}).get("clearance", {}))
        out.update((r.get(cls, {}) or {}).get("clearance", {}) if cls else {})
        rt = dict(r.get("DEFAULT", {}).get("route", {}))
        rt.update((r.get(cls, {}) or {}).get("route", {}) if cls else {})
        return {"track_width_min": out.get("MIN_TRACK_WIDTH"), "track_width_rec": out.get("REC_TRACK_WIDTH"),
                "track_width_max": out.get("MAX_TRACK_WIDTH"), "track_to_track": out.get("TRACK_TO_TRACK"),
                "layers": rt.get("VALID_LAYER"), "via_types": rt.get("VALID_VIA_TYPE")}

    # ------------------------------------------------------------------ overview / region
    def overview(self) -> dict:
        d = self.doc()
        pcb = self._export(d)
        f = 1 / pcb._unit_factor(_header_unit(pcb))          # mm per file unit
        board = pcb.board_outline()
        routed = _routed_counts(pcb)
        nets = []
        for n in items(d.Nets):
            nets.append({"net": n.Name, "pins": n.Pins.Count, **routed.get(n.Name, {})})
        nets.sort(key=lambda r: G_natural(r["net"]))
        cls_of = {net: c for c, ns in pcb.net_classes().items() for net in ns}
        for r in nets:
            if r["net"] in cls_of:
                r["class"] = cls_of[r["net"]]
        return {"unit": "mm", "document": d.FullName,
                "board_bbox": [round(v * f, 4) for v in board["bbox"]] if board else None,
                "board_polygon": [[round(x * f, 4), round(y * f, 4)] for x, y in board["polygon"]] if board else None,
                "electrical_layers": [{"layer": i, "name": d.LayerName(i)} for i in range(1, d.ElectricalLayerCount + 1)],
                "via_types": {k: {"drill": round(v["drill"] * f, 4), "pad": round(v["pad"] * f, 4)}
                              for k, v in pcb.via_types().items()},
                "stackup": pcb.stackup(), "rules": pcb.get_rules(),
                "components": d.Components.Count, "route_segments": d.RouteSegments.Count, "vias": d.Vias.Count,
                "connections": sum(r.get("connections", 0) for r in nets),
                "unrouted": sum(r.get("unrouted", 0) for r in nets), "nets": nets}

    def region(self, x1: float, y1: float, x2: float, y2: float) -> dict:
        """Pins and routed copper inside a window (for planning a path around obstacles)."""
        d = self.doc()
        lo_x, hi_x, lo_y, hi_y = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
        inside = lambda x, y: lo_x <= x <= hi_x and lo_y <= y <= hi_y  # noqa: E731
        pins = []
        for c in items(d.Components):
            cx, cy = getp(c, "PositionX", MM), getp(c, "PositionY", MM)
            if not (lo_x - 30 <= cx <= hi_x + 30 and lo_y - 30 <= cy <= hi_y + 30):
                continue
            for p in items(c.Pins):
                x, y = getp(p, "PositionX", MM), getp(p, "PositionY", MM)
                if inside(x, y):
                    pins.append(self._pin_row(d, c, p, pad=True))
        pcb = self._export(d)
        f = 1 / pcb._unit_factor(_header_unit(pcb))
        tracks, vias = [], set()
        for s in P.Routes(pcb).signals:
            for c in s.conns:
                if not c.routed:
                    continue
                for p, q in zip(c.pts, c.pts[1:]):
                    ax, ay, bx, by = p.x * f, p.y * f, q.x * f, q.y * f
                    if _seg_hits_box(ax, ay, bx, by, lo_x, lo_y, hi_x, hi_y):
                        tracks.append({"net": s.name, "layer": p.layer, "width": round(p.width * f, 4),
                                       "from": [round(ax, 4), round(ay, 4)], "to": [round(bx, 4), round(by, 4)]})
                    if p.via and inside(ax, ay):
                        vias.add((s.name, round(ax, 4), round(ay, 4), p.via))
        uniq = {(t["net"], t["layer"], tuple(t["from"]), tuple(t["to"])): t for t in tracks}
        return {"unit": "mm", "window": [lo_x, lo_y, hi_x, hi_y],
                "pad_note": "pad.hx/hy = half extents along x/y (axis-aligned box around the pad)",
                "pins": sorted(pins, key=lambda r: G_natural(r["pin"])),
                "tracks": list(uniq.values()),
                "vias": [{"net": n, "x": x, "y": y, "type": t} for n, x, y, t in sorted(vias)]}

    # ------------------------------------------------------------------ view / files
    def select(self, refdes: list[str]) -> dict:
        d = self.doc()
        for c in items(d.Components):
            c.selected = c.Name in refdes
        d.ActiveView.SetExtentsToSelection()
        return {"selected": refdes}

    def show(self, zoom: str = "board", region: list[float] | None = None) -> None:
        d = self.doc()
        v = d.ActiveView
        if region:
            x1, y1, x2, y2 = region
            v.SetExtents(float(x1), float(y2), float(x2), float(y1), MM)
        elif zoom == "all":
            v.SetExtentsToAll()
        elif zoom == "board":
            v.SetExtentsToBoard()
        v.Refresh()

    def save(self, path: str | None = None) -> dict:
        d = self.doc()
        if path:
            d.SaveAs(os.path.abspath(path))
        else:
            d.Save()
        return {"saved": self.doc().FullName}

    def export(self, kind: str, path: str) -> dict:
        d = self.doc()
        path = os.path.abspath(path)
        if kind == "ascii":
            d.ExportASCII(path, -1, 0, False)
        elif kind == "netlist":
            d.ExportNetList(path, 0)
        elif kind == "rules":
            d.ExportRules(path, 0)
        else:
            raise ValueError("kind must be 'ascii', 'netlist' or 'rules'")
        return {"exported": path, "bytes": os.path.getsize(path) if os.path.exists(path) else None}

    def import_eco(self, path: str) -> dict:
        self.doc().ImportECOFile(os.path.abspath(path))
        return self.status()

    # ------------------------------------------------------------------ ASCII transactions
    def _export(self, d) -> P.PcbAscii:
        tmp = os.path.join(self.tmpdir, "layout_export.asc")
        if os.path.exists(tmp):
            os.remove(tmp)
        d.ExportASCII(tmp, -1, 0, False)
        return P.PcbAscii.load(tmp)

    def _begin(self):
        d = self.doc()
        path = d.FullName
        if not path or not path.lower().endswith(".pcb"):
            raise RuntimeError("save the design to a .pcb file first (layout_save with a path)")
        d.Save()
        return d, path

    def _backup(self, path: str) -> str:
        stem, ext = os.path.splitext(os.path.basename(path))
        dst = os.path.join(self.histdir, f"{stem}__{time.strftime('%Y%m%d_%H%M%S')}{ext}")
        shutil.copy2(path, dst)
        return dst

    def _commit(self, pcb: P.PcbAscii, path: str, before: dict) -> dict:
        tmp = os.path.join(self.tmpdir, "layout_edited.asc")
        pcb.save(tmp)
        backup = self._backup(path)
        a = self.app()
        t = time.time()
        a.OpenDocument("")
        nd = a.ActiveDocument
        t_blank = time.time()
        try:
            nd.ImportNetList(tmp)
            nd = a.ActiveDocument
            after = {"components": nd.Components.Count, "nets": nd.Nets.Count}
            t_import = time.time()
            lost = {k: (before[k], after[k]) for k in after if after[k] != before[k]}
            if lost:
                log = ""
                err = os.path.join(r"C:\PADS Projects", "ascii.err")
                if os.path.exists(err) and os.path.getmtime(err) >= t - 1:
                    with open(err, errors="replace") as fh:
                        log = fh.read()[:1200]
                raise RuntimeError(f"PADS import changed {lost}; nothing was saved. ascii.err:\n{log}")
        except Exception:
            a.OpenDocument(path)               # the original file is untouched
            raise
        os.remove(path)                        # SaveAs onto an existing file would prompt; backup exists
        try:
            nd.SaveAs(path)
        except Exception:
            shutil.copy2(backup, path)
            a.OpenDocument(path)
            raise
        d = self.doc()
        return {"commit_seconds": round(time.time() - t, 1), "import_seconds": round(t_import - t_blank, 1),
                "saved": d.FullName, "backup": backup,
                "route_segments": d.RouteSegments.Count, "vias": d.Vias.Count}

    def _transaction(self, edit) -> dict:
        d, path = self._begin()
        before = {"components": d.Components.Count, "nets": d.Nets.Count}
        pcb = self._export(d)
        summary = edit(d, pcb)
        res = self._commit(pcb, path, before)
        return {**summary, **res}

    def history(self) -> list[str]:
        stem = os.path.splitext(os.path.basename(self.doc().FullName))[0]
        files = sorted(f for f in os.listdir(self.histdir) if f.startswith(stem + "__") and f.endswith(".pcb"))
        return [os.path.join(self.histdir, f) for f in files]

    def undo(self) -> dict:
        path = self.doc().FullName
        hist = self.history()
        if not hist:
            raise RuntimeError("no PCB backup to restore")
        last = hist[-1]
        a = self.app()
        a.OpenDocument("")
        shutil.copy2(last, path)
        os.remove(last)
        a.OpenDocument(path)
        return {"restored_from": last, **self.status()}

    # ------------------------------------------------------------------ routing
    def route(self, routes: list[dict]) -> dict:
        """Write agent-planned routes. Each route: {from, to, layer, path, width?, via?}."""
        def edit(d, pcb: P.PcbAscii):
            f = pcb._unit_factor(_header_unit(pcb))         # file units per mm
            R = P.Routes(pcb)
            names = {r[k] for r in routes for k in ("from", "to")}
            pins = self.pins_of(d, names)
            missing = sorted(n for n in names if n not in pins)
            if missing:
                raise ValueError(f"unknown pins: {missing}")
            vias = pcb.via_types()
            nlayers = d.ElectricalLayerCount
            by_net: dict[str, list[P.Connection]] = {}
            report = []
            for i, r in enumerate(routes):
                a, b = pins[r["from"]], pins[r["to"]]
                if a["net"] != b["net"] or a["net"] is None:
                    raise ValueError(f"route {i}: {r['from']} ({a['net']}) and {r['to']} ({b['net']}) are not on one net")
                net = a["net"]
                layer = _layer_no(r.get("layer", 1), nlayers)
                path = [[float(p[0]), float(p[1])] + ([_layer_no(p[2], nlayers)] if len(p) > 2 and p[2] is not None else [])
                        for p in r.get("path", [])]
                # layer of the last segment
                end_layer = layer
                for p in path:
                    if len(p) > 2:
                        end_layer = p[2]
                if a["layer"] is not None and a["layer"] != layer:
                    raise ValueError(f"route {i}: {r['from']} is an SMD pad on layer {a['layer']}, "
                                     f"but the route starts on layer {layer}. Start on {a['layer']} and add a via.")
                if b["layer"] is not None and b["layer"] != end_layer:
                    raise ValueError(f"route {i}: {r['to']} is an SMD pad on layer {b['layer']}, "
                                     f"but the route arrives on layer {end_layer}.")
                via = r.get("via") or ("STANDARDVIA" if "STANDARDVIA" in vias else next(iter(vias), "STANDARDVIA"))
                if via not in vias:
                    raise ValueError(f"unknown via type {via}; available: {list(vias)}")
                width = r.get("width")
                if width is None:
                    cls = next((c for c, ns in pcb.net_classes().items() if net in ns), None)
                    rec = self._rules_for(pcb, cls)["track_width_rec"]
                    width = float(rec) / f if rec else 0.25
                conn = P.build_route(r["from"], r["to"],
                                     (a["x"] * f, a["y"] * f), (b["x"] * f, b["y"] * f), layer, float(width) * f,
                                     [[p[0] * f, p[1] * f] + p[2:] for p in path], via)
                by_net.setdefault(net, []).append(conn)
                report.append({"from": r["from"], "to": r["to"], "net": net, "layer": layer, "width_mm": width,
                               "vias": sum(1 for p in conn.pts if p.via), "length_mm": round(_length(conn) / f, 3),
                               "points": [[a["x"], a["y"]]] + [p[:2] for p in path] + [[b["x"], b["y"]]]})
            for net, new in by_net.items():
                pins_all = {k: (x * f, y * f) for k, (x, y) in self._net_pins(d, net).items()}
                R.replace(net, _merge(R.connections(net), new, pins_all=pins_all,
                                      unrouted_width=new[0].pts[0].width))
            R.commit()
            return {"routes": report}
        return self._transaction(edit)

    def _net_pins(self, d, net: str) -> dict[str, tuple[float, float]]:
        n = d.Nets.Item(net)
        return {self._pin_name(p.Component, p): (getp(p, "PositionX", MM), getp(p, "PositionY", MM))
                for p in items(n.Pins)}

    def unroute(self, nets: list[str] | None = None, pairs: list[list[str]] | None = None) -> dict:
        """Turn routed connections back into ratsnest: whole nets and/or specific [from, to] pairs."""
        def edit(d, pcb: P.PcbAscii):
            R = P.Routes(pcb)
            want = {frozenset(p) for p in pairs or []}
            done = 0
            for s in R.signals:
                for c in s.conns:
                    if c.routed and ((nets and s.name in nets) or frozenset((c.a, c.b)) in want):
                        c.pts = [P.RoutePoint(c.pts[0].x, c.pts[0].y, 0, c.pts[0].width, P.F_TERM, ["THERMAL"]),
                                 P.RoutePoint(c.pts[-1].x, c.pts[-1].y, P.END_LAYER, c.pts[0].width, P.F_TERM,
                                              ["THERMAL"])]
                        done += 1
            if not done:
                raise ValueError("nothing routed matched")
            R.commit()
            return {"unrouted": done}
        return self._transaction(edit)

    # ------------------------------------------------------------------ rules / stack-up
    def get_rules(self, full: bool = False) -> dict:
        return self._export(self.doc()).get_rules(full)

    def set_rules(self, net_classes: dict | None = None, rules: list[dict] | None = None) -> dict:
        def edit(d, pcb: P.PcbAscii):
            changed = []
            for name, nets in (net_classes or {}).items():
                pcb.set_net_class(name, nets)
                changed.append({"net_class": name, "nets": nets})
            for spec in rules or []:
                changed += pcb.apply_rule_spec(spec)
            return {"changed": changed, "rules_now": pcb.get_rules()}
        return self._transaction(edit)

    def stackup(self) -> dict:
        return self._export(self.doc()).stackup()

    def set_stackup(self, layers: list[dict]) -> dict:
        def edit(d, pcb: P.PcbAscii):
            pcb.set_stackup(layers)
            return {"stackup": pcb.stackup()}
        return self._transaction(edit)


# --------------------------------------------------------------------------- helpers
def _header_unit(pcb: P.PcbAscii) -> str:
    # "!PADS-POWERPCB-V9.3-METRIC! ..." -> METRIC
    head = pcb.header.split("!")[1] if pcb.header.startswith("!") else ""
    return head.rsplit("-", 1)[-1] if "-" in head else "MILS"


def _layer_no(v, nlayers: int) -> int:
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("top", "t"):
            return 1
        if s in ("bottom", "bot", "b"):
            return nlayers
        v = int(s)
    v = int(v)
    if not 1 <= v <= nlayers:
        raise ValueError(f"layer {v} out of range 1..{nlayers}")
    return v


def _length(c: P.Connection) -> float:
    return sum(math.hypot(q.x - p.x, q.y - p.y) for p, q in zip(c.pts, c.pts[1:]))


def _path_of(c: P.Connection, f: float = 1.0) -> list:
    """Intermediate points in mm; [x, y, layer] where a via switches to `layer`."""
    out = []
    for p in c.pts[1:-1]:
        x, y = round(p.x * f, 5), round(p.y * f, 5)
        out.append([x, y, p.layer] if p.via else [x, y])
    return out


def _merge(existing: list[P.Connection], new: list[P.Connection], pins_all: dict, unrouted_width: float):
    """New routes replace connections with the same pin pair; keep every routed connection;
    keep unrouted ones only where still needed so the net stays one spanning tree."""
    parent = {p: p for p in pins_all}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b) -> bool:
        ra, rb = find(a), find(b)
        if ra == rb:
            return False
        parent[ra] = rb
        return True

    pairs = {frozenset((c.a, c.b)) for c in new}
    keep = set()
    for c in new + [c for c in existing if c.routed and frozenset((c.a, c.b)) not in pairs]:
        union(c.a, c.b)
        keep.add(id(c))
    for c in existing:
        if not c.routed and frozenset((c.a, c.b)) not in pairs and union(c.a, c.b):
            keep.add(id(c))
    # existing ones first, in their original (valid tree) order, then the new routes
    out = [c for c in existing if id(c) in keep] + new
    # still split? connect the components with the shortest ratsnest pairs
    while len({find(p) for p in pins_all}) > 1:
        best = None
        for a, pa in pins_all.items():
            for b, pb in pins_all.items():
                if find(a) != find(b):
                    dd = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
                    if best is None or dd < best[0]:
                        best = (dd, a, b)
        _, a, b = best
        union(a, b)
        out.append(P.unrouted(a, b, pins_all[a], pins_all[b], unrouted_width))
    return _tree_order(out)


def _tree_order(conns: list[P.Connection]) -> list[P.Connection]:
    """PADS reads a net's connections as a growing tree: every connection after the first must
    touch a pin that is already in the tree ('Connection not physically tied to the signal tree')."""
    rest = list(conns)
    out: list[P.Connection] = []
    tree: set[str] = set()
    while rest:
        idx = next((i for i, c in enumerate(rest) if c.a in tree or c.b in tree), 0 if not out else None)
        if idx is None:                     # a separate piece (should not happen): start a new subtree
            idx = 0
        c = rest.pop(idx)
        out.append(c)
        tree |= {c.a, c.b}
    return out


def _routed_counts(pcb: P.PcbAscii) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for s in P.Routes(pcb).signals:
        r = out.setdefault(s.name, {"connections": 0, "unrouted": 0})
        for c in s.conns:
            r["connections"] += 1
            r["unrouted"] += 0 if c.routed else 1
    return out


def _pad_geometry(comp, pin) -> dict | None:
    """Mounted-side pad: shape, size and the axis-aligned half extents hx/hy (mm)."""
    try:
        pad = pin.PadStackLayers.Item(1).Pad
        shape = {1: "rect", 2: "round", 3: "oval", 4: "square"}.get(pad.Shape, str(pad.Shape))
        w, ln = getp(pad, "Width", MM), getp(pad, "Length", MM)
        ang = math.radians((pad.Orientation or 0) + (comp.Orientation or 0))
    except Exception:  # noqa: BLE001
        return None
    if not ln or shape == "round":
        hx = hy = w / 2
    else:
        hx = abs(ln / 2 * math.cos(ang)) + abs(w / 2 * math.sin(ang))
        hy = abs(ln / 2 * math.sin(ang)) + abs(w / 2 * math.cos(ang))
    return {"shape": shape, "w": round(w, 4), "l": round(ln, 4), "hx": round(hx, 4), "hy": round(hy, 4)}


def _seg_hits_box(ax, ay, bx, by, x1, y1, x2, y2) -> bool:
    if max(ax, bx) < x1 or min(ax, bx) > x2 or max(ay, by) < y1 or min(ay, by) > y2:
        return False
    return True
