"""PADS Logic operations. Every public method must run on the COM worker thread."""
from __future__ import annotations

import fnmatch
import os
import shutil
import time

import pythoncom
import win32com.client.dynamic

from . import geometry as G
from .edit import EditError, PinInfo, WireEditor, is_ground, is_power
from .sch_ascii import SchematicAscii

MILS = 2  # plogUnitMils


def getp(obj, name, *args):
    """Read a parameterized COM property, e.g. PositionX(unit)."""
    o = obj._oleobj_
    return o.Invoke(o.GetIDsOfNames(name), 0, pythoncom.DISPATCH_PROPERTYGET, True, *args)


def items(collection):
    for i in range(1, collection.Count + 1):
        yield collection.Item(i)


def _dyn(obj):
    return win32com.client.dynamic.Dispatch(obj)


def _rot_of(gate) -> int:
    r90, rx, ry = bool(gate.Rotated90), bool(gate.ReflectedX), bool(gate.ReflectedY)
    base = 90 if r90 else 0
    if rx and ry:
        return (base + 180) % 360
    return base


def _set_rot(gate, rotate: int, mirror: bool = False) -> None:
    rotate %= 360
    if rotate not in (0, 90, 180, 270):
        raise ValueError("rotate must be 0, 90, 180 or 270")
    gate.Rotated90 = rotate in (90, 270)
    flip = rotate in (180, 270)
    gate.ReflectedX = flip != bool(mirror)
    gate.ReflectedY = flip


class PadsLogic:
    PROGID = "PowerLogic.Application"

    def __init__(self, workdir: str):
        self.workdir = workdir
        self.tmpdir = os.path.join(workdir, "tmp")
        self.histdir = os.path.join(workdir, "history")
        os.makedirs(self.tmpdir, exist_ok=True)
        os.makedirs(self.histdir, exist_ok=True)
        self._app = None

    # ------------------------------------------------------------------ connection
    def app(self, launch: bool = True):
        if self._app is not None:
            try:
                _ = self._app.Name
                return self._app
            except Exception:  # noqa: BLE001 - stale proxy
                self._app = None
        try:
            unk = pythoncom.GetActiveObject(self.PROGID)
            a = _dyn(unk.QueryInterface(pythoncom.IID_IDispatch))
        except pythoncom.com_error:
            if not launch:
                return None
            a = win32com.client.dynamic.Dispatch(self.PROGID)
        a.Visible = True
        self._app = a
        return a

    def doc(self):
        d = self.app().ActiveDocument
        if d is None:
            raise RuntimeError("PADS Logic has no open schematic")
        return d

    def status(self) -> dict:
        a = self.app(launch=False)
        if a is None:
            return {"running": False}
        d = a.ActiveDocument
        out = {"running": True, "version": a.Version}
        if d is not None:
            out.update(document=d.Name, path=d.FullName, saved=bool(d.Saved),
                       sheets=[s.Name for s in items(d.Sheets)], active_sheet=d.ActiveSheet.Name,
                       components=d.Components.Count, nets=d.Nets.Count)
        return out

    # ------------------------------------------------------------------ files
    def open(self, path: str, copy_to_workdir: bool = True) -> dict:
        path = os.path.abspath(path)
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        target = path
        if copy_to_workdir and os.path.dirname(path).lower() != self.workdir.lower():
            base = os.path.basename(path)
            target = os.path.join(self.workdir, base)
            if os.path.exists(target):
                stem, ext = os.path.splitext(base)
                target = os.path.join(self.workdir, f"{stem}_{time.strftime('%Y%m%d_%H%M%S')}{ext}")
            shutil.copy2(path, target)
        d = self.app().OpenDocument(target)
        d = self.app().ActiveDocument
        if os.path.normcase(d.FullName) != os.path.normcase(target):
            raise RuntimeError(f"PADS Logic did not open {target} (active: {d.FullName})")
        return {"opened": target, "original": path if target != path else None, **self.status()}

    def new(self, path: str) -> dict:
        path = os.path.abspath(path)
        a = self.app()
        a.OpenDocument("")
        a.ActiveDocument.SaveAs(path)
        return self.status()

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
            d.ExportASCII(path, 0)
        elif kind == "netlist":
            d.ExportNetList(path, 0)
        else:
            raise ValueError("kind must be 'ascii' or 'netlist'")
        return {"exported": path, "bytes": os.path.getsize(path)}

    def import_ascii(self, path: str, save_as: str) -> dict:
        a = self.app()
        a.OpenDocument("")
        a.ActiveDocument.ImportASCII(os.path.abspath(path))
        a.ActiveDocument.SaveAs(os.path.abspath(save_as))
        return self.status()

    # ------------------------------------------------------------------ queries
    def _part_row(self, comp) -> dict:
        g = comp.Gates.Item(1)
        row = {"refdes": comp.Name, "part_type": comp.PartTypeObject.Name,
               "sheet": g.Sheet.Name,
               "x": round(getp(g, "PositionX", MILS)), "y": round(getp(g, "PositionY", MILS)),
               "rotate": _rot_of(g), "pins": comp.Pins.Count}
        try:
            v = comp.Attributes.Item("Value")
            if v is not None and v.Value:
                row["value"] = v.Value
        except pythoncom.com_error:
            pass
        return row

    def list_parts(self, sheet: str | None = None, pattern: str | None = None, limit: int = 300) -> dict:
        d = self.doc()
        rows = []
        for c in items(d.Components):
            if pattern and not (fnmatch.fnmatchcase(c.Name.upper(), pattern.upper())
                                or fnmatch.fnmatchcase(c.PartTypeObject.Name.upper(), pattern.upper())):
                continue
            row = self._part_row(c)
            if sheet and row["sheet"] != sheet:
                continue
            rows.append(row)
        rows.sort(key=lambda r: (r["sheet"], G_natural(r["refdes"])))
        return {"count": len(rows), "parts": rows[:limit], "truncated": len(rows) > limit}

    def get_part(self, refdes: str) -> dict:
        d = self.doc()
        comp = d.Components.Item(refdes)
        if comp is None:
            raise KeyError(f"no part {refdes}")
        out = self._part_row(comp)
        out["pcb_decal"] = comp.PCBDecal
        attrs = {}
        for a in items(comp.Attributes):
            try:
                attrs[a.Name] = a.Value
            except pythoncom.com_error:
                pass
        out["attributes"] = attrs
        pins = self.pin_infos(d, None, {refdes})
        rows = []
        for name, p in pins.items():
            if name != p.name:
                continue
            rows.append({"pin": p.name, "x": p.x, "y": p.y, "dir": G.DIR_NAMES.get(p.dir, str(p.dir)), "net": p.net})
        rows.sort(key=lambda r: G_natural(r["pin"]))
        xs = [r["x"] for r in rows] or [0]
        ys = [r["y"] for r in rows] or [0]
        out["pins"] = rows
        out["pin_bbox"] = {"x1": min(xs), "y1": min(ys), "x2": max(xs), "y2": max(ys)}
        aliases = {k: v.name for k, v in pins.items() if k != v.name}
        if aliases:
            out["pin_aliases"] = aliases
        return out

    def list_nets(self, pattern: str | None = None, limit: int = 400) -> dict:
        d = self.doc()
        rows = []
        for n in items(d.Nets):
            if pattern and not fnmatch.fnmatchcase(n.Name.upper(), pattern.upper()):
                continue
            rows.append({"net": n.Name, "pins": n.Pins.Count})
        rows.sort(key=lambda r: G_natural(r["net"]))
        return {"count": len(rows), "nets": rows[:limit], "truncated": len(rows) > limit}

    def get_net(self, name: str) -> dict:
        d = self.doc()
        n = d.Nets.Item(name)
        if n is None:
            raise KeyError(f"no net {name}")
        pins = []
        for p in items(n.Pins):
            pins.append({"pin": p.Name, "sheet": p.Gate.Sheet.Name if p.Gate is not None else None,
                         "x": round(getp(p, "PositionX", MILS)), "y": round(getp(p, "PositionY", MILS))})
        pins.sort(key=lambda r: G_natural(r["pin"]))
        return {"net": n.Name, "pins": pins}

    def search_library(self, pattern: str, limit: int = 100) -> dict:
        a = self.app()
        its = a.GetLibraryItems(0, pattern)  # plogLibraryItemTypePartType
        rows = []
        if its is not None:
            for it in items(its):
                try:
                    lib = it.Library.Name
                except Exception:  # noqa: BLE001
                    lib = None
                rows.append({"part_type": it.Name, "library": lib})
                if len(rows) >= limit:
                    break
        return {"count": len(rows), "part_types": rows}

    # ------------------------------------------------------------------ pin geometry
    def pin_infos(self, doc, sch: SchematicAscii | None, refs: set[str]) -> dict[str, PinInfo]:
        if sch is None:
            sch = self._export_model(doc)
        decal_cache: dict[str, tuple] = {}
        out: dict[str, PinInfo] = {}
        for ref in refs:
            comp = doc.Components.Item(ref)
            if comp is None:
                raise EditError(f"no part {ref!r}")
            for g in items(comp.Gates):
                sheet = g.Sheet.Name
                if sheet not in decal_cache:
                    sh = sch.sheet(sheet)
                    decal_cache[sheet] = (sh.decals(), sh.pin_decal_dirs())
                decals, pdirs = decal_cache[sheet]
                pins = list(items(g.Pins))
                world = [(getp(p, "PositionX", MILS), getp(p, "PositionY", MILS)) for p in pins]
                origin = (getp(g, "PositionX", MILS), getp(g, "PositionY", MILS))
                fit = G.fit_gate(origin, world, decals)
                dirs = G.outward_dirs(fit, decals[fit.decal], pdirs) if fit else {}
                for i, p in enumerate(pins):
                    d = dirs.get(i) or G.fallback_outward(world[i], world)
                    net = p.Net.Name if p.Net is not None else None
                    info = PinInfo(p.Name, sheet, round(world[i][0]), round(world[i][1]), d, net)
                    out[p.Name] = info
                    for alias in (_safe(lambda: p.GatePinName), _safe(lambda: p.FunctionName)):
                        if alias and "." not in alias and f"{ref}.{alias}" not in out:
                            out[f"{ref}.{alias}"] = info
        return out

    def _export_model(self, doc) -> SchematicAscii:
        path = os.path.join(self.tmpdir, "export.txt")
        doc.ExportASCII(path, 0)
        return SchematicAscii.load(path)

    # ------------------------------------------------------------------ COM part edits
    def add_sheet(self, name: str) -> dict:
        d = self.doc()
        for s in items(d.Sheets):
            if s.Name == name:
                return {"sheet": name, "created": False}
        s = d.Sheets.Add()
        s.Name = name
        return {"sheet": s.Name, "created": True, "sheets": [x.Name for x in items(d.Sheets)]}

    def _sheet(self, d, name: str | None):
        if not name:
            return d.ActiveSheet
        for s in items(d.Sheets):
            if s.Name == name:
                return s
        raise KeyError(f"no sheet {name!r}; sheets: {[s.Name for s in items(d.Sheets)]}")

    def add_part(self, part_type: str, refdes: str | None, sheet: str | None, x: int, y: int,
                 rotate: int = 0) -> dict:
        d = self.doc()
        sh = self._sheet(d, sheet)
        sh.Activate()
        comp = sh.AddComponent(part_type, refdes or "", float(x), float(y), 1000.0, 0.0, MILS)
        if comp is None:
            raise RuntimeError(f"AddComponent failed for part type {part_type!r} "
                               f"(not in the PADS libraries?)")
        comp = _dyn(comp) if not hasattr(comp, "Name") else comp
        if rotate:
            for g in items(comp.Gates):
                _set_rot(g, rotate)
        return {"added": comp.Name, "part": self.get_part(comp.Name)}

    def move_part(self, refdes: str, x: int | None, y: int | None, rotate: int | None,
                  mirror: bool | None, gate: int = 1) -> dict:
        d = self.doc()
        comp = d.Components.Item(refdes)
        if comp is None:
            raise KeyError(f"no part {refdes}")
        g = comp.Gates.Item(gate)
        if x is not None or y is not None:
            nx = x if x is not None else round(getp(g, "PositionX", MILS))
            ny = y if y is not None else round(getp(g, "PositionY", MILS))
            g.Move(float(nx), float(ny), MILS)
        if rotate is not None or mirror is not None:
            _set_rot(g, rotate if rotate is not None else _rot_of(g), bool(mirror))
        return self._part_row(comp)

    def delete_part(self, refdes: str) -> dict:
        d = self.doc()
        comp = d.Components.Item(refdes)
        if comp is None:
            raise KeyError(f"no part {refdes}")
        comp.Delete()
        return {"deleted": refdes}

    def rename_part(self, refdes: str, new: str) -> dict:
        comp = self.doc().Components.Item(refdes)
        if comp is None:
            raise KeyError(f"no part {refdes}")
        comp.Name = new
        return {"renamed": refdes, "to": comp.Name}

    def set_attribute(self, refdes: str, name: str, value: str) -> dict:
        comp = self.doc().Components.Item(refdes)
        if comp is None:
            raise KeyError(f"no part {refdes}")
        a = comp.Attributes.Item(name)
        if a is None:
            comp.Attributes.Add(name, value)
        else:
            a.Value = value
        return {"refdes": refdes, name: comp.Attributes.Item(name).Value}

    # ------------------------------------------------------------------ ASCII transactions
    def _backup(self, path: str) -> str | None:
        if not os.path.exists(path):
            return None
        stem, ext = os.path.splitext(os.path.basename(path))
        dst = os.path.join(self.histdir, f"{stem}__{time.strftime('%Y%m%d_%H%M%S')}{ext}")
        shutil.copy2(path, dst)
        return dst

    def _commit(self, sch: SchematicAscii, path: str) -> dict:
        tmp = os.path.join(self.tmpdir, "edited.txt")
        sch.save(tmp)
        backup = self._backup(path)
        a = self.app()
        t = time.time()
        a.OpenDocument("")
        try:
            a.ActiveDocument.ImportASCII(tmp)
        except pythoncom.com_error:
            a.OpenDocument(path)  # restore the previous state
            raise
        a.ActiveDocument.SaveAs(path)
        return {"import_seconds": round(time.time() - t, 1), "saved": a.ActiveDocument.FullName,
                "ascii": tmp, "backup": backup}

    def _begin(self):
        d = self.doc()
        path = d.FullName
        if not path or not os.path.isabs(path) or not path.lower().endswith(".sch"):
            raise RuntimeError("save the schematic to a .sch file first (logic_save with a path)")
        d.Save()
        return d, path

    def apply_edits(self, ops: list[dict]) -> dict:
        """Run wiring/clone operations as one atomic ASCII round trip."""
        d, path = self._begin()
        sch = self._export_model(d)
        refs = set()
        for op in ops:
            for k in ("from", "to", "pin", "a", "b"):
                v = op.get(k)
                if isinstance(v, str) and "." in v:
                    refs.add(v.split(".", 1)[0])
            for v in op.get("pins", []) or []:
                refs.add(v.split(".", 1)[0])
        pins = self.pin_infos(d, sch, refs)
        ed = WireEditor(sch, pins)
        for i, op in enumerate(ops):
            try:
                self._apply_op(ed, op)
            except (EditError, KeyError, ValueError) as e:
                raise EditError(f"op #{i} {op}: {e}") from None
        result = self._commit(sch, path)
        result.update(ed.summary())
        result["pin_nets_after"] = self._nets_of(sorted(ed.touched))
        return result

    def _apply_op(self, ed: WireEditor, op: dict) -> None:
        kind = op.get("op")
        if kind == "connect":
            ed.connect(op.get("from") or op["a"], op.get("to") or op["b"], op.get("net"))
        elif kind == "power":
            ed.power(op["pin"], op.get("net"), op.get("symbol"))
        elif kind == "label":
            ed.label(op["pin"], op.get("net"), int(op.get("length", 200)))
        elif kind == "disconnect":
            ed.disconnect(op["pin"])
        elif kind == "net":
            self._apply_net(ed, op["name"], op["pins"], op.get("style", "auto"))
        else:
            raise EditError(f"unknown op {kind!r} (connect|power|label|disconnect|net)")

    def _apply_net(self, ed: WireEditor, name: str, pins: list[str], style: str) -> None:
        infos = [ed.pin(p) for p in pins]
        if style == "auto":
            if is_ground(name) or is_power(name):
                style = "power"
            elif len({p.sheet for p in infos}) == 1 and len(infos) <= 4 and _span(infos) <= 2000:
                style = "wire"
            else:
                style = "label"
        if style == "power":
            for p in infos:
                ed.power(p.name, name)
        elif style == "label":
            for p in infos:
                ed.label(p.name, name)
        elif style == "wire":
            by_sheet: dict[str, list[PinInfo]] = {}
            for p in infos:
                by_sheet.setdefault(p.sheet, []).append(p)
            multi = len(by_sheet) > 1
            for group in by_sheet.values():
                group.sort(key=lambda p: (p.x, p.y))
                for a, b in zip(group, group[1:]):
                    ed.connect(a.name, b.name, name)
                if multi or len(group) == 1:
                    ed.label(group[0].name, name)
        else:
            raise EditError(f"unknown style {style!r} (auto|wire|label|power)")

    def clone_parts(self, clones: list[dict]) -> dict:
        d, path = self._begin()
        sch = self._export_model(d)
        ed = WireEditor(sch, {})
        for c in clones:
            ed.clone_part(c["source"], c["refdes"], c.get("sheet"), int(c["x"]), int(c["y"]),
                          int(c.get("rotate", 0)), c.get("attributes"))
        result = self._commit(sch, path)
        result["log"] = ed.log
        doc = self.doc()
        missing = [c["refdes"] for c in clones if doc.Components.Item(c["refdes"]) is None]
        if missing:
            raise RuntimeError(f"PADS dropped cloned parts {missing} during import; "
                               f"see {result['ascii']} (backup: {result['backup']})")
        return result

    def _nets_of(self, pins: list[str]) -> dict:
        d = self.doc()
        out = {}
        for name in pins:
            ref, _, pin = name.partition(".")
            comp = d.Components.Item(ref)
            if comp is None:
                continue
            for p in items(comp.Pins):
                if p.Name == name:
                    out[name] = p.Net.Name if p.Net is not None else None
        return out

    def history(self) -> list[str]:
        d = self.doc()
        stem = os.path.splitext(os.path.basename(d.FullName))[0]
        files = sorted(f for f in os.listdir(self.histdir) if f.startswith(stem + "__"))
        return [os.path.join(self.histdir, f) for f in files]

    def undo(self) -> dict:
        d = self.doc()
        path = d.FullName
        hist = self.history()
        if not hist:
            raise RuntimeError("no backup to restore")
        last = hist[-1]
        a = self.app()
        a.OpenDocument("")
        shutil.copy2(last, path)
        os.remove(last)
        a.OpenDocument(path)
        return {"restored_from": last, **self.status()}

    # ------------------------------------------------------------------ view
    def show(self, sheet: str | None, zoom: str = "sheet", region: list[int] | None = None) -> int:
        d = self.doc()
        if sheet:
            self._sheet(d, sheet).Activate()
        v = d.ActiveView
        if region:
            x1, y1, x2, y2 = region
            v.SetExtents(float(x1), float(y2), float(x2), float(y1), MILS)
        elif zoom == "all":
            v.SetExtentsToAll()
        elif zoom == "sheet":
            v.SetExtentsToSheet()
        v.Refresh()
        return 0


def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def _span(infos: list[PinInfo]) -> int:
    xs = [p.x for p in infos]
    ys = [p.y for p in infos]
    return max(max(xs) - min(xs), max(ys) - min(ys))


def G_natural(s: str):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]
