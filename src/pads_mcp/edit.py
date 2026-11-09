"""Connectivity / part editing on a SchematicAscii model.

All coordinates are mils, PADS convention (y grows upwards).
Pins are addressed as 'REFDES.PIN' (e.g. 'U1.3').
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from . import geometry as G
from .sch_ascii import (OffPage, SchematicAscii, Segment, Sheet, TieDot, _decal_blocks,
                        _is_part_header, parse_decals)

GND_RE = re.compile(r"(^|_)(A|D|P|S|C|E|SIG|CHASSIS|EARTH)?GND|^VSS|^0$|GND$", re.I)
PWR_RE = re.compile(
    r"^[+-]?\d+(\.\d+)?V\d*(_\w+)?$|^\d+V\d+|^V(CC|DD|EE|BAT|IN|USB|BUS|SYS|REF)|^[+-]?\d+_\d+V|"
    r"^P\d+V\d*|^\+", re.I)

# (rot, flip) candidates in preference order. flip bit0 = mirror X, bit1 = mirror Y.
_ORIENT_CANDIDATES = [(0, 0), (0, 3), (0, 2), (0, 1), (90, 0), (90, 3)]
# part header (angle, f5) for the 4 rotations (validated against PADS 9.3 exports)
PART_ROT_FIELDS = {0: ("0", "0"), 90: ("90", "0"), 180: ("0", "3"), 270: ("90", "3")}


class EditError(Exception):
    pass


@dataclass
class PinInfo:
    name: str          # canonical 'REF.NUM'
    sheet: str
    x: int
    y: int
    dir: G.Vec         # outward direction
    net: str | None


def is_ground(net: str) -> bool:
    return bool(GND_RE.search(net))


def is_power(net: str) -> bool:
    return bool(PWR_RE.search(net)) and not is_ground(net)


def _add(p: G.Vec, d: G.Vec, k: int = 1) -> G.Vec:
    return (p[0] + d[0] * k, p[1] + d[1] * k)


def _orient(ext: G.Vec, want: G.Vec) -> tuple[int, int]:
    for rot, flip in _ORIENT_CANDIDATES:
        v = G.apply(G._R[rot], ext)
        if flip & 1:
            v = (-v[0], v[1])
        if flip & 2:
            v = (v[0], -v[1])
        if v == want:
            return rot, flip
    return 0, 0


def decal_extension(sheet: Sheet, decal: str) -> G.Vec | None:
    """Direction in which a symbol decal's graphics extend from its origin."""
    blk = dict(_decal_blocks(sheet.section("CAEDECAL").body)).get(decal)
    if not blk:
        return None
    xs, ys = [], []
    i = 0
    while i < len(blk):
        t = blk[i].split()
        if t and t[0] in ("OPEN", "CLOSED", "CIRCLE") and len(t) > 1 and t[1].isdigit():
            n = int(t[1])
            for l in blk[i + 1:i + 1 + n]:
                c = l.split()
                try:
                    xs.append(int(c[0])); ys.append(int(c[1]))
                except (ValueError, IndexError):
                    pass
            i += 1 + n
        else:
            i += 1
    if not xs:
        return None
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    if abs(cx) >= abs(cy) and cx:
        return (1 if cx > 0 else -1, 0)
    if cy:
        return (0, 1 if cy > 0 else -1)
    return None


def _blank_trimmed_end(body: list[str]) -> int:
    i = len(body)
    while i > 0 and not body[i - 1].strip():
        i -= 1
    return i


def _parttype_blocks(body: list[str]) -> dict[str, list[str]]:
    """PARTTYPE section blocks (blank-line separated), keyed by part type name."""
    out, cur = {}, []
    for l in body + [""]:
        if l.strip():
            cur.append(l)
        elif cur:
            out[cur[0].split()[0]] = cur
            cur = []
    return out


class WireEditor:
    def __init__(self, sch: SchematicAscii, pins: dict[str, PinInfo]):
        self.sch = sch
        self.pins = pins
        self.log: list[str] = []
        self.touched: set[str] = set()
        names = {s.name for sh in sch.sheets for s in sh.signals}
        self._auto = (f"N{n}" for n in range(90000001, 99999999) if f"N{n}" not in names)

    # ------------------------------------------------------------------ helpers
    def pin(self, name: str) -> PinInfo:
        try:
            return self.pins[name]
        except KeyError:
            raise EditError(f"unknown pin {name!r}") from None

    def _sheet(self, p: PinInfo) -> Sheet:
        sh = self.sch.sheet(p.sheet)
        sh.dirty = True
        return sh

    def _resolve_net(self, pins: list[PinInfo], requested: str | None) -> str:
        existing = {p.net for p in pins if p.net}
        if len(existing) > 1:
            raise EditError(f"pins {[p.name for p in pins]} are on different nets {sorted(existing)}")
        if existing:
            e = existing.pop()
            if requested and requested != e:
                who = next(p.name for p in pins if p.net)
                raise EditError(f"{who} is already on net {e!r}, not {requested!r}")
            return e
        return requested or next(self._auto)

    def _font(self) -> str:
        for sh in self.sch.sheets:
            for l in sh.netnames:
                m = re.search(r'"[^"]*"\s*$', l)
                if m:
                    return m.group(0)
        return '"Default Font"'

    # ------------------------------------------------------------------ symbols
    def ensure_symbol(self, sh: Sheet, set_name: str, net: str, decal: str | None = None) -> tuple[int, str]:
        """Make sure symbol set `set_name` with a suitable decal exists on `sh`.
        Returns (variant index, decal name)."""
        def pick(variants):
            names = [d for d, _ in variants]
            if decal:
                return decal if decal in names else None
            if net in names:
                return net
            matches = [d for d, default_net in variants if default_net == net]
            if matches:
                return min(matches, key=len)
            if set_name == "$GND_SYMS":
                return "GND" if "GND" in names else (names[0] if names else None)
            if set_name == "$PWR_SYMS":
                for d in names:
                    if re.search(r"ARROW|VCC|PWR|BAR|POWER", d, re.I):
                        return d
                return next((d for d in names if d != "0"), None)
            return names[0] if names else None

        local = sh.symbol_sets().get(set_name)
        want = pick(local.variants) if local else None
        if want and decal_extension(sh, want) is not None:
            return [d for d, _ in local.variants].index(want), want

        # find a donor sheet that has the symbol set + decal
        for donor in self.sch.sheets:
            dset = donor.symbol_sets().get(set_name)
            if not dset:
                continue
            dwant = want or pick(dset.variants)
            if not dwant or dwant not in [d for d, _ in dset.variants]:
                continue
            self._copy_decal(donor, sh, dwant)
            pt = sh.section("PARTTYPE").body
            if local is None:
                end = _blank_trimmed_end(pt)
                pt[end:end] = [""] + dset.lines
            elif dwant not in [d for d, _ in local.variants]:
                vline = next(l for l in dset.lines[3:] if l.split()[0] == dwant)
                start = next(i for i, l in enumerate(pt) if l.split()[:1] == [set_name])
                end = start
                while end < len(pt) and pt[end].strip():
                    end += 1
                pt.insert(end, vline)
                kind, cnt = pt[start + 2].split()[:2]
                pt[start + 2] = f"{kind} {int(cnt) + 1}"
            new = sh.symbol_sets()[set_name]
            self.log.append(f"copied symbol {set_name}/{dwant} from sheet {donor.name} to {sh.name}")
            return [d for d, _ in new.variants].index(dwant), dwant
        raise EditError(f"no {set_name} symbol available in this design for net {net!r}")

    def _copy_decal(self, donor: Sheet, sh: Sheet, decal: str) -> None:
        """Copy a CAE decal (and the pin decals it references) from donor to sh."""
        have = dict(_decal_blocks(sh.section("CAEDECAL").body))
        if decal in have:
            return
        donor_blocks = dict(_decal_blocks(donor.section("CAEDECAL").body))
        blk = list(donor_blocks[decal])
        while blk and not blk[-1].strip():
            blk.pop()
        # PADS only resolves pin decals that are defined BEFORE the decal using them
        for pin in parse_decals(blk).get(decal, []):
            if pin.pin_decal and pin.pin_decal in donor_blocks:
                self._copy_decal(donor, sh, pin.pin_decal)
        body = sh.section("CAEDECAL").body
        end = _blank_trimmed_end(body)
        body[end:end] = [""] + blk

    def _netname_line(self, net: str, oid: str, o: OffPage) -> str:
        width = max(1, round(54 * len(net)))
        for sh in self.sch.sheets:
            ops = {x.id: x for x in sh.offpage}
            for l in sh.netnames:
                t = l.split()
                ref = ops.get(t[1]) if len(t) > 7 else None
                if ref and (ref.sym, ref.rot, ref.flip) == (o.sym, o.rot, o.flip):
                    dx, dy, ref_w = int(t[2]), int(t[3]), int(t[6])
                    if dx < 0:
                        dx -= width - ref_w
                    rest = l.split(None, 7)[7] if len(l.split(None, 7)) > 7 else ""
                    return f"{net:<12} {oid:<12} {dx:<6} {dy:<6} {t[4]} {t[5]} {width:<6} {rest}"
        dx = 120 if o.flip in (0, 2) else -(width + 120)
        return f"{net:<12} {oid:<12} {dx:<6} {-10:<6} 0 0 {width:<6} 0      0 0 -1 10 93 {self._font()}"

    # PADS Logic allows exactly ONE wire per pin; branches need a tie dot (3 wires).
    def _pin_seg(self, sh: Sheet, pin: str):
        for sig in sh.signals:
            for seg in sig.segments:
                if pin in seg.ends():
                    return sig, seg
        return None, None

    def _attach(self, p: PinInfo, toward: G.Vec | None) -> tuple[str, G.Vec, G.Vec]:
        """Where a new wire for pin `p` should start: the pin itself if it is free,
        otherwise a new tie dot splitting the pin's existing wire.
        Returns (endpoint name, point, outgoing direction)."""
        sh = self._sheet(p)
        sig, seg = self._pin_seg(sh, p.name)
        if seg is None:
            return p.name, (p.x, p.y), p.dir
        pts = seg.pts if seg.a == p.name else list(reversed(seg.pts))
        other = seg.b if seg.a == p.name else seg.a
        if len(pts) >= 3:
            s = pts[1]
            head, tail = pts[:2], pts[1:]
        else:
            (x0, y0), (x1, y1) = pts
            length = abs(x1 - x0) + abs(y1 - y0)
            step = 100 if length > 100 else length // 2
            d = G._sgnv(pts[0], pts[1])
            s = (x0 + d[0] * step, y0 + d[1] * step)
            head, tail = [pts[0], s], [s, pts[1]]
        tid = sh.next_id("@@@D")
        sh.tiedots.append(TieDot(tid, s[0], s[1]))
        sig.segments.remove(seg)
        sig.segments.append(Segment(p.name, tid, head))
        sig.segments.append(Segment(tid, other, tail))
        used = {G._sgnv(s, head[-2]), G._sgnv(s, tail[1])}
        options = [d for d in ((0, 1), (0, -1), (1, 0), (-1, 0)) if d not in used]
        if toward:
            options.sort(key=lambda d: -((toward[0] - s[0]) * d[0] + (toward[1] - s[1]) * d[1]))
        self.log.append(f"tie dot {tid} at {s} on the existing wire of {p.name}")
        return tid, s, options[0]

    def _place_symbol(self, p: PinInfo, net: str, set_name: str, stub: int,
                      decal: str | None = None) -> str:
        sh = self._sheet(p)
        variant, dname = self.ensure_symbol(sh, set_name, net, decal)
        ext = decal_extension(sh, dname) or {"$GND_SYMS": (0, -1), "$PWR_SYMS": (0, 1)}.get(set_name, (1, 0))
        start_name, start, sdir = self._attach(p, None)
        if set_name == "$GND_SYMS":
            want_dir = (0, 1) if sdir == (0, 1) else (0, -1)
        elif set_name == "$PWR_SYMS":
            want_dir = (0, -1) if sdir == (0, -1) else (0, 1)
        else:
            want_dir = sdir
        a1 = _add(start, sdir, stub)
        s = a1 if want_dir == sdir else _add(a1, want_dir, 100)
        rot, flip = _orient(ext, want_dir)
        oid = sh.next_id("@@@O")
        o = OffPage(oid, net, set_name, s[0], s[1], rot, flip, variant)
        sh.offpage.append(o)
        if set_name != "$GND_SYMS":
            sh.netnames.append(self._netname_line(net, oid, o))
        sh.signal(net, create=True).segments.append(Segment(start_name, oid, G._dedupe([start, a1, s])))
        return oid

    # ------------------------------------------------------------------ operations
    def connect(self, a: str, b: str, net: str | None = None) -> str:
        pa, pb = self.pin(a), self.pin(b)
        if pa.sheet != pb.sheet:
            raise EditError(f"{a} (sheet {pa.sheet}) and {b} (sheet {pb.sheet}) are on different sheets; "
                            f"use 'label' on both pins with the same net name")
        n = self._resolve_net([pa, pb], net)
        sh = self._sheet(pa)
        a_name, a_pt, a_dir = self._attach(pa, (pb.x, pb.y))
        b_name, b_pt, b_dir = self._attach(pb, a_pt)
        pts = G.route(a_pt, a_dir, b_pt, b_dir)
        sh.signal(n, create=True).segments.append(Segment(a_name, b_name, pts))
        pa.net = pb.net = n
        self.touched |= {pa.name, pb.name}
        self.log.append(f"wire {a_name} -> {b_name} on net {n} via {pts}")
        return n

    def power(self, pin: str, net: str | None = None, decal: str | None = None) -> str:
        p = self.pin(pin)
        n = net or p.net
        if not n:
            raise EditError(f"power: net name required for unconnected pin {pin}")
        self._resolve_net([p], n)
        set_name = "$GND_SYMS" if is_ground(n) else "$PWR_SYMS"
        try:
            oid = self._place_symbol(p, n, set_name, 100, decal)
        except EditError:
            if set_name == "$GND_SYMS":
                raise
            self.log.append(f"no power symbol for {n}; using a net label on {pin}")
            return self.label(pin, n)
        p.net = n
        self.touched.add(p.name)
        self.log.append(f"{set_name} symbol {oid} ({n}) on {p.name}")
        return n

    def label(self, pin: str, net: str | None = None, length: int = 200) -> str:
        p = self.pin(pin)
        n = net or p.net
        if not n:
            raise EditError(f"label: net name required for unconnected pin {pin}")
        self._resolve_net([p], n)
        oid = self._place_symbol(p, n, "$OSR_SYMS", length)
        p.net = n
        self.touched.add(p.name)
        self.log.append(f"net label {oid} ({n}) on {p.name}")
        return n

    def disconnect(self, pin: str) -> int:
        p = self.pin(pin)
        sh = self._sheet(p)
        affected: set[str] = set()
        removed = 0
        for sig in sh.signals:
            keep = []
            for seg in sig.segments:
                if p.name in seg.ends():
                    removed += 1
                    affected |= {e for e in seg.ends() if e.startswith("@@@")}
                else:
                    keep.append(seg)
            sig.segments = keep
        self._cleanup(sh, affected)
        p.net = None
        self.touched.add(p.name)
        self.log.append(f"removed {removed} wire segment(s) at {p.name}")
        return removed

    def _cleanup(self, sh: Sheet, affected: set[str]) -> None:
        while affected:
            node = affected.pop()
            touching = [(sig, seg) for sig in sh.signals for seg in sig.segments if node in seg.ends()]
            if node.startswith("@@@D"):
                td = sh.tiedot_by_id(node)
                if not td:
                    continue
                if len(touching) <= 1:
                    for sig, seg in touching:
                        sig.segments.remove(seg)
                        affected |= {e for e in seg.ends() if e.startswith("@@@") and e != node}
                    sh.tiedots.remove(td)
                elif len(touching) == 2 and touching[0][0] is touching[1][0]:
                    (sig, s1), (_, s2) = touching
                    p1 = s1.pts if s1.b == node else list(reversed(s1.pts))
                    x = s1.a if s1.b == node else s1.b
                    p2 = s2.pts if s2.a == node else list(reversed(s2.pts))
                    y = s2.b if s2.a == node else s2.a
                    sig.segments.remove(s1)
                    sig.segments.remove(s2)
                    sig.segments.append(Segment(x, y, G._dedupe(p1 + p2[1:])))
                    sh.tiedots.remove(td)
            elif node.startswith("@@@O") and not touching:
                o = sh.offpage_by_id(node)
                if o:
                    sh.offpage.remove(o)
                    sh.netnames = [l for l in sh.netnames if l.split()[1:2] != [node]]

    # ------------------------------------------------------------------ parts (ASCII clone)
    def clone_part(self, source: str, refdes: str, sheet: str | None, x: int, y: int,
                   rotate: int = 0, attributes: dict[str, str] | None = None) -> str:
        src_sheet = self.sch.sheet_of_part(source)
        dst = self.sch.sheet(sheet) if sheet else src_sheet
        if any(refdes in s.part_refs() for s in self.sch.sheets):
            raise EditError(f"refdes {refdes} already exists")
        if rotate not in PART_ROT_FIELDS:
            raise EditError("rotate must be 0, 90, 180 or 270")
        body = src_sheet.section("PART").body
        start = next(i for i, l in enumerate(body) if _is_part_header(l.split()) and l.split()[0] == source)
        f = body[start].split()
        n = 1 + 2 + 2 * int(f[10]) + int(f[11]) + int(f[12])
        blk = list(body[start:start + n])
        ang, f5 = PART_ROT_FIELDS[rotate]
        f[0], f[2], f[3], f[4], f[5] = refdes, str(x), str(y), ang, f5
        blk[0] = f"{f[0]:<12} {f[1]:<16} {f[2]:<5} {f[3]:<6} " + " ".join(f[4:])
        if attributes:
            nattr_start = 1 + 2 + 2 * int(f[10])
            for k in range(nattr_start, nattr_start + int(f[11])):
                m = re.match(r'^"([^"]+)"\s?(.*)$', blk[k])
                if m and m.group(1) in attributes:
                    blk[k] = f'"{m.group(1)}" {attributes[m.group(1)]}'
        # make sure part type + decals exist on the destination sheet
        if dst is not src_sheet:
            ptype = f[1]
            src_pt = _parttype_blocks(src_sheet.section("PARTTYPE").body)
            dst_pt = _parttype_blocks(dst.section("PARTTYPE").body)
            if ptype not in dst_pt and ptype in src_pt:
                pt_body = dst.section("PARTTYPE").body
                end = _blank_trimmed_end(pt_body)
                pt_body[end:end] = [""] + src_pt[ptype]
                for gl in [i for i, l in enumerate(src_pt[ptype]) if l.startswith("GATE ")]:
                    ndecals = int(src_pt[ptype][gl].split()[1])
                    for d in src_pt[ptype][gl + 1:gl + 1 + ndecals]:
                        self._copy_decal(src_sheet, dst, d.strip())
        dbody = dst.section("PART").body
        end = _blank_trimmed_end(dbody)
        if end == 0:
            # PADS skips the 3 lines after the *PART* header; an empty section must keep them
            dbody[:] = ["", "", ""] + blk + [""]
        else:
            dbody[end:end] = [""] + blk
        dst.dirty = True
        self.log.append(f"cloned {source} -> {refdes} on sheet {dst.name} at ({x},{y}) rot {rotate}")
        return refdes

    def summary(self) -> dict:
        used = Counter(l.split()[0] for l in self.log)
        return {"operations": len(self.log), "by_kind": dict(used), "log": self.log}
