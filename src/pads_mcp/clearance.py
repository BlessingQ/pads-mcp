"""pads-mcp's own clearance check (no PADS Router): routed tracks and vias against pads, other tracks,
vias and the board outline, with the design's clearance and width rules. Pure geometry on the PADS
Layout ASCII; all distances in mm.

Shapes: ("cap", ax, ay, bx, by, r) is a segment with a radius (track, oval pad; a == b is a circle:
via, round pad). ("poly", [(x, y), ...]) is a rectangle (rectangular / square pad).

Simplifications: copper pours / planes / free copper, text, drill holes, same-net rules, layer-specific
rule sets and pad-to-pad spacing (placement) are not checked. Rounded or chamfered pad corners count as
square corners, so the check is slightly stricter than PADS there.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from . import pcb_ascii as P

TOL = 0.001              # mm; smaller shortfalls are rounding
CELL = 2.0               # mm; spatial grid cell
KEY = {"track": "TRACK", "via": "VIA", "pad": "PAD", "smd": "SMD", "outline": "OUTLINE"}
OUTLINE_NET = "<board outline>"
NOT_CHECKED = ("copper pours / planes / free copper, text, drill holes, same-net rules, layer-specific "
               "rules, pad-to-pad spacing, the gap inside a differential pair; pad corner rounding is ignored "
               "(stricter)")


# --------------------------------------------------------------------------- geometry
def _pt_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    qx, qy = ax + t * dx, ay + t * dy
    return math.hypot(px - qx, py - qy), (qx, qy)


def _cross(ox, oy, ax, ay, bx, by):
    return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)


def _seg_seg(a, b, c, d):
    """Distance between segments ab and cd, and the point midway between their closest points."""
    (ax, ay), (bx, by), (cx, cy), (dx, dy) = a, b, c, d
    d1, d2 = _cross(cx, cy, dx, dy, ax, ay), _cross(cx, cy, dx, dy, bx, by)
    d3, d4 = _cross(ax, ay, bx, by, cx, cy), _cross(ax, ay, bx, by, dx, dy)
    if d1 * d2 < 0 and d3 * d4 < 0:                         # proper crossing
        t = d1 / (d1 - d2)
        return 0.0, (ax + t * (bx - ax), ay + t * (by - ay))
    best = None
    for (px, py), (s, e) in (((ax, ay), (c, d)), ((bx, by), (c, d)), ((cx, cy), (a, b)), ((dx, dy), (a, b))):
        dist, q = _pt_seg(px, py, *s, *e)
        if best is None or dist < best[0]:
            best = (dist, ((px + q[0]) / 2, (py + q[1]) / 2))
    return best


def _inside(px, py, poly) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        (xi, yi), (xj, yj) = poly[i], poly[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _edges(poly):
    return [(poly[i - 1], poly[i]) for i in range(len(poly))]


def gap(s1, s2):
    """Copper-to-copper gap between two shapes (<= 0: they touch or overlap) and where it is."""
    if s1[0] == "poly" and s2[0] == "cap":
        s1, s2 = s2, s1
    if s1[0] == "cap" and s2[0] == "cap":
        d, at = _seg_seg(s1[1:3], s1[3:5], s2[1:3], s2[3:5])
        return d - s1[5] - s2[5], at
    if s1[0] == "cap":
        a, b, r, poly = s1[1:3], s1[3:5], s1[5], s2[1]
        for p in (a, b):
            if _inside(*p, poly):
                return -r, p
        d, at = min((_seg_seg(a, b, e0, e1) for e0, e1 in _edges(poly)), key=lambda t: t[0])
        return d - r, at
    p1, p2 = s1[1], s2[1]
    for p, other in ((p1[0], p2), (p2[0], p1)):
        if _inside(*p, other):
            return 0.0, p
    return min((_seg_seg(a, b, c, d) for a, b in _edges(p1) for c, d in _edges(p2)), key=lambda t: t[0])


def _bbox(shape):
    if shape[0] == "cap":
        _, ax, ay, bx, by, r = shape
        return min(ax, bx) - r, min(ay, by) - r, max(ax, bx) + r, max(ay, by) + r
    xs, ys = [p[0] for p in shape[1]], [p[1] for p in shape[1]]
    return min(xs), min(ys), max(xs), max(ys)


def _scale(shape, s):
    if shape[0] == "cap":
        return ("cap",) + tuple(v * s for v in shape[1:])
    return ("poly", [(x * s, y * s) for x, y in shape[1]])


# --------------------------------------------------------------------------- rules
class Rules:
    """Clearance values (mm) for a pair of objects: a net-vs-net / class-vs-class rule set if one
    exists, else the larger of the two nets' own rules (net -> net class -> DEFAULT)."""

    def __init__(self, pcb: P.PcbAscii):
        per_mm = pcb._unit_factor(pcb.rules_unit())
        self.single: dict[str, dict] = {}
        self.pair: dict[tuple[str, str], dict] = {}
        for rs, target, against, kind, body in pcb.rule_sets():
            if kind != "clearance" or (rs.get("LAYER") or "0") != "0":
                continue
            vals = {}
            for leaf in body.kids:
                if leaf.kids is None and leaf.key:
                    try:
                        vals[leaf.key] = float(leaf.value) / per_mm
                    except ValueError:
                        pass
            if against == "DEFAULT":
                self.single[target] = vals
            else:
                self.pair[(target, against)] = vals
        self.cls = {n: c for c, ns in pcb.net_classes().items() for n in ns}
        # differential pairs: their spacing is the pair's GAP rule, not a clearance between the two nets
        self.diff_pairs = {frozenset(n.value for n in k.leaves("NET"))
                           for k in pcb._design_rules().kids if k.key == "DIF_PAIR" and k.kids is not None}
        self._memo: dict = {}

    def chain(self, net: str | None) -> list[str]:
        out = []
        if net and net != OUTLINE_NET:
            out.append("NET:" + net)
            if net in self.cls:
                out.append(self.cls[net])
        return out + ["DEFAULT"]

    @staticmethod
    def _pick(vals: dict, keys) -> float | None:
        return next((vals[k] for k in keys if k in vals), None)

    def value(self, net: str | None, keys) -> float | None:
        for t in self.chain(net):
            if t in self.single and (v := self._pick(self.single[t], keys)) is not None:
                return v
        return None

    def need(self, n1, k1, n2, k2) -> float | None:
        memo = (n1, k1, n2, k2)
        if memo in self._memo:
            return self._memo[memo]
        keys = (f"{KEY[k1]}_TO_{KEY[k2]}", f"{KEY[k2]}_TO_{KEY[k1]}")
        out = None
        for t1 in self.chain(n1):
            for t2 in self.chain(n2):
                for p in ((t1, t2), (t2, t1)):
                    if out is None and p in self.pair:
                        out = self._pick(self.pair[p], keys)
        if out is None:
            vals = [v for v in (self.value(n1, keys), self.value(n2, keys)) if v is not None]
            out = max(vals) if vals else None
        self._memo[memo] = out
        return out

    def reach(self) -> float:
        vals = [v for d in list(self.single.values()) + list(self.pair.values())
                for k, v in d.items() if "_TO_" in k and not k.startswith(("SAME_NET", "TEXT", "BODY", "DRILL"))]
        return min(max(vals, default=0.5), 10.0)


# --------------------------------------------------------------------------- objects
@dataclass
class _Obj:
    kind: str                     # track | via | pad | smd | outline
    net: str | None
    shapes: dict                  # electrical layer -> shape (mm)
    label: str
    group: object                 # findings are reported once per (group, other group)
    checked: bool = False
    bbox: tuple = ()


def _layer_shapes(levels: dict, n: int, mounted: int, smd: bool, s: float) -> dict:
    opposite = 1 if mounted == n else n
    out = {}
    for layer in range(1, n + 1):
        if smd and layer != mounted:
            continue
        if layer in levels and layer not in (-2, -1, 0):
            sh = levels[layer]
        elif layer == mounted:
            sh = levels.get(-2)
        elif layer == opposite:
            sh = levels.get(0)
        else:
            sh = levels.get(-1)
        if sh is not None:
            out[layer] = _scale(sh, s)
    return out


def _track_pieces(c: P.Connection, n: int):
    """[(ax, ay, bx, by, layer, width)] in file units; arcs (a CW/CCW centre point) are flattened."""
    pts, out, i = c.pts, [], 0
    while i < len(pts) - 1:
        p, q = pts[i], pts[i + 1]
        if q.extra and q.extra[0] in ("CW", "CCW") and i + 2 < len(pts) and 1 <= p.layer <= n:
            e = pts[i + 2]
            r = math.hypot(p.x - q.x, p.y - q.y)
            a0, a1 = math.atan2(p.y - q.y, p.x - q.x), math.atan2(e.y - q.y, e.x - q.x)
            sweep = a1 - a0
            if q.extra[0] == "CCW" and sweep <= 0:
                sweep += 2 * math.pi
            if q.extra[0] == "CW" and sweep >= 0:
                sweep -= 2 * math.pi
            k = max(2, math.ceil(abs(math.degrees(sweep)) / 10))
            arc = [(q.x + r * math.cos(a0 + sweep * j / k), q.y + r * math.sin(a0 + sweep * j / k))
                   for j in range(k + 1)]
            out += [(*arc[j], *arc[j + 1], p.layer, p.width) for j in range(k)]
            i += 2
            continue
        if 1 <= p.layer <= n:
            out.append((p.x, p.y, q.x, q.y, p.layer, p.width))
        i += 1
    return out


def check(pcb: P.PcbAscii, n_layers: int, scope=None, max_report: int = 100) -> dict:
    """Check the routed connections for which scope(net, connection) is true (all when None)."""
    s = 1 / pcb._unit_factor(pcb.file_unit())          # mm per file unit
    rules = Rules(pcb)
    routes = P.Routes(pcb)
    objs: list[_Obj] = []

    # pins by name and by position (decal terminal numbers differ from alphanumeric pin names, e.g. BGA)
    net_of_pin, at_pos = {}, {}
    for sig in routes.signals:
        for c in sig.conns:
            net_of_pin[c.a] = net_of_pin[c.b] = sig.name
            for p, name in ((c.pts[0], c.a), (c.pts[-1], c.b)) if c.pts else ():
                at_pos[(round(p.x * s, 3), round(p.y * s, 3))] = (sig.name, name)
    for p in pcb.pads():
        net, name = net_of_pin.get(p["pin"]), p["pin"]
        hit = at_pos.get((round(p["x"] * s, 3), round(p["y"] * s, 3)))
        if hit and hit[1].split(".")[0] == name.split(".")[0]:
            net, name = net or hit[0], hit[1]
        mounted = n_layers if p["bottom"] else 1
        shapes = _layer_shapes(p["levels"], n_layers, mounted, p["smd"], s)
        if shapes:
            objs.append(_Obj("smd" if p["smd"] else "pad", net, shapes,
                             f"pad {name} ({net or 'no net'})", ("pad", p["pin"])))

    vias = pcb.via_types()
    checked = {"connections": 0, "track_segments": 0, "vias": 0}
    width_findings = []
    seen_vias = set()
    for sig in routes.signals:
        for c in sig.conns:
            # a partly routed connection can start unrouted (layer 0) and still hold tracks and vias
            pieces = _track_pieces(c, n_layers)
            via_pts = [p for p in c.pts if p.via]
            if not pieces and not via_pts:
                continue
            on = scope is None or scope(sig.name, c)
            group = ("conn", sig.name, c.a, c.b)
            label = f"track {sig.name} {c.a}-{c.b}"
            for ax, ay, bx, by, layer, w in pieces:
                shape = ("cap", ax * s, ay * s, bx * s, by * s, w * s / 2)
                objs.append(_Obj("track", sig.name, {layer: shape}, label, group, on))
            for p in via_pts:
                vkey = ("via", sig.name, round(p.x * s, 4), round(p.y * s, 4))
                if vkey in seen_vias:
                    continue
                seen_vias.add(vkey)
                levels = vias.get(p.via, {}).get("levels", {})
                shapes = _layer_shapes({k: ("cap", p.x, p.y, p.x, p.y, v / 2) for k, v in levels.items() if v > 0},
                                       n_layers, 1, False, s)
                if shapes:
                    objs.append(_Obj("via", sig.name, shapes, f"via {p.via} {sig.name} ({c.a}-{c.b})", vkey, on))
                    checked["vias"] += on
            if on:
                checked["connections"] += 1
                checked["track_segments"] += len(pieces)
                lo = rules.value(sig.name, ("MIN_TRACK_WIDTH",))
                hi = rules.value(sig.name, ("MAX_TRACK_WIDTH",))
                for ax, ay, bx, by, layer, w in pieces:
                    w *= s
                    if (lo is not None and w < lo - TOL) or (hi is not None and w > hi + TOL):
                        width_findings.append({"type": "width", "layer": layer,
                                               "at": [round((ax + bx) * s / 2, 4), round((ay + by) * s / 2, 4)],
                                               "width": round(w, 4), "allowed": [lo, hi], "object": label})
                        break

    board = pcb.board_outline()
    poly = [(x * s, y * s) for x, y in board["polygon"]] if board else None
    if poly:
        allL = range(1, n_layers + 1)
        for a, b in _edges(poly):
            objs.append(_Obj("outline", OUTLINE_NET, {L: ("cap", *a, *b, 0.0) for L in allL},
                             "board outline", ("outline",)))

    for o in objs:
        boxes = [_bbox(sh) for sh in o.shapes.values()]
        o.bbox = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    grid = defaultdict(list)
    for i, o in enumerate(objs):
        for cell in _cells(o.bbox, 0):
            grid[cell].append(i)

    reach = rules.reach()
    worst: dict = {}
    outside = {}
    for i, o in enumerate(objs):
        if not o.checked:
            continue
        if poly:
            for sh in o.shapes.values():
                for pt in (sh[1:3], sh[3:5]):
                    if not _inside(*pt, poly):
                        outside.setdefault(o.group, {"type": f"{o.kind}-outside-board", "at": [round(pt[0], 4), round(pt[1], 4)],
                                                     "object": o.label})
        seen = set()
        for cell in _cells(o.bbox, reach):
            for j in grid[cell]:
                if j == i or j in seen:
                    continue
                seen.add(j)
                q = objs[j]
                if (q.checked and j < i) or q.group == o.group or (q.net is not None and q.net == o.net):
                    continue
                if frozenset((o.net, q.net)) in rules.diff_pairs:
                    continue
                common = o.shapes.keys() & q.shapes.keys()
                if not common:
                    continue
                need = rules.need(o.net, o.kind, q.net if q.kind != "outline" else None, q.kind)
                if need is None or not _overlap(o.bbox, q.bbox, need):
                    continue
                g, at, layer = min((gap(o.shapes[L], q.shapes[L]) + (L,) for L in common), key=lambda t: t[0])
                if g < need - TOL:
                    key = (o.group, q.group)
                    if key not in worst or g - need < worst[key][0]:
                        worst[key] = (g - need, o, q, g, need, at, layer)

    findings = []
    for short, o, q, g, need, at, layer in sorted(worst.values(), key=lambda t: t[0]):
        f = {"type": f"{o.kind}-{q.kind}", "layer": layer, "at": [round(at[0], 4), round(at[1], 4)],
             "gap": round(max(g, 0.0), 4), "required": round(need, 4), "object": o.label, "other": q.label}
        if g <= 0:
            f["short"] = True
        findings.append(f)
    findings = width_findings + list(outside.values()) + findings
    by_type: dict[str, int] = {}
    for f in findings:
        by_type[f["type"]] = by_type.get(f["type"], 0) + 1
    return {"unit": "mm", "checked": checked, "violations": len(findings), "by_type": by_type,
            "findings": findings[:max_report], "truncated": len(findings) > max_report,
            "not_checked": NOT_CHECKED}


def _cells(bb, pad):
    x0, y0 = math.floor((bb[0] - pad) / CELL), math.floor((bb[1] - pad) / CELL)
    x1, y1 = math.floor((bb[2] + pad) / CELL), math.floor((bb[3] + pad) / CELL)
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def _overlap(a, b, pad):
    return not (a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1])
