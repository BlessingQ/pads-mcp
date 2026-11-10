"""PADS Layout (PowerPCB) ASCII: design rules and layer stack-up, read and edit.

The rules live in a brace tree inside *MISC*:

    RULES_SECTION METRIC
    {
    NET_CLASS DATA
    {
    NET_CLASS PWR_5V
    {
    NET 5V
    }
    }
    DESIGN RULES
    {
    RULE_SET (1)
    {
    FOR :
    {
    DEFAULT :            <- or "NET_CLASS <name>" / "NET <name>"
    }
    AGAINST :
    {
    DEFAULT :
    }
    LAYER 0
    CLEARANCE_RULE :     <- or ROUTE_RULE / HIGH_SPEED_RULE
    {
    TRACK_TO_TRACK 0.2
    MIN_TRACK_WIDTH 0.3
    ...

A header line followed by a "{" line opens a block; everything else is a leaf line.
Values are in the unit named after RULES_SECTION / LAYER (METRIC = mm, MILS = mil).
Only the edited blocks are rewritten; every other line is kept verbatim.
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field

ENCODING = "cp949"
RULE_KINDS = {"clearance": "CLEARANCE_RULE", "route": "ROUTE_RULE", "high_speed": "HIGH_SPEED_RULE"}
MULTI_KEYS = {"VALID_LAYER", "VALID_VIA_TYPE"}
# clearance keys set by the friendly "clearance" value (signal-to-signal copper spacing)
SIGNAL_CLEARANCE_KEYS = [f"{a}_TO_{b}" for a in ("TRACK", "VIA", "PAD", "SMD")
                         for b in ("TRACK", "VIA", "PAD", "SMD")]
HS_LENGTH_KEYS = {"MIN_LENGTH", "MAX_LENGTH", "STUB_LENGTH", "PARALLEL_LENGTH", "PARALLEL_GAP",
                  "TANDEM_LENGTH", "TANDEM_GAP", "SHIELD_GAP", "MATCH_LENGTH_TOLERANCE"}


class AsciiError(ValueError):
    pass


@dataclass
class Node:
    head: str
    kids: list["Node"] | None = None          # None -> leaf line

    @property
    def key(self) -> str:
        return self.head.split(" ", 1)[0] if self.head else ""

    @property
    def value(self) -> str:
        parts = self.head.split(" ", 1)
        return parts[1].strip() if len(parts) > 1 else ""

    def child(self, head: str) -> "Node | None":
        for k in self.kids or []:
            if k.head.strip() == head:
                return k
        return None

    def leaves(self, key: str) -> list["Node"]:
        return [k for k in self.kids or [] if k.kids is None and k.key == key]

    def get(self, key: str) -> str | None:
        ls = self.leaves(key)
        return ls[0].value if ls else None

    def set(self, key: str, value) -> None:
        """value None removes the key; a list writes one line per item."""
        assert self.kids is not None
        idx = [i for i, k in enumerate(self.kids) if k.kids is None and k.key == key]
        new = [] if value is None else [Node(f"{key} {v}") for v in (value if isinstance(value, list) else [value])]
        if idx:
            first = idx[0]
            self.kids = [k for i, k in enumerate(self.kids) if i not in idx]
            self.kids[first:first] = new
        else:
            self.kids.extend(new)


def parse_block(lines: list[str], i: int) -> tuple[Node, int]:
    head = lines[i].strip()
    if i + 1 < len(lines) and lines[i + 1].strip() == "{":
        node, j = Node(head, []), i + 2
        while j < len(lines) and lines[j].strip() != "}":
            kid, j = parse_block(lines, j)
            node.kids.append(kid)
        if j >= len(lines):
            raise AsciiError(f"unterminated block '{head}'")
        return node, j + 1
    return Node(head), i + 1


def dump(node: Node) -> list[str]:
    if node.kids is None:
        return [node.head]
    out = [node.head, "{"]
    for k in node.kids:
        out += dump(k)
    out.append("}")
    return out


def fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


@dataclass
class _Block:
    start: int
    end: int
    node: Node
    dirty: bool = False


@dataclass
class PcbAscii:
    lines: list[str]
    newline: str = "\r\n"
    _blocks: dict[str, _Block] = field(default_factory=dict)

    # ------------------------------------------------------------------ io
    @classmethod
    def from_text(cls, text: str) -> "PcbAscii":
        nl = "\r\n" if "\r\n" in text else "\n"
        if not text.startswith("!PADS-POWERPCB"):
            raise AsciiError("not a PADS Layout ASCII file")
        return cls(text.split(nl), nl)

    @classmethod
    def load(cls, path: str) -> "PcbAscii":
        with open(path, encoding=ENCODING, errors="surrogateescape", newline="") as f:
            return cls.from_text(f.read())

    def text(self) -> str:
        lines = list(self.lines)
        for b in sorted(self._blocks.values(), key=lambda b: -b.start):
            if b.dirty:
                lines[b.start:b.end] = dump(b.node)
        return self.newline.join(lines)

    def save(self, path: str) -> None:
        with open(path, "w", encoding=ENCODING, errors="surrogateescape", newline="") as f:
            f.write(self.text())

    @property
    def header(self) -> str:
        return self.lines[0]

    def file_unit(self) -> str:
        """Coordinate unit of the file: "!PADS-POWERPCB-V9.3-METRIC! ..." -> METRIC."""
        head = self.header.split("!")[1] if self.header.startswith("!") else ""
        return head.rsplit("-", 1)[-1] if "-" in head else "MILS"

    def _block(self, name: str, pattern: str) -> _Block:
        if name not in self._blocks:
            rx = re.compile(pattern)
            for i, l in enumerate(self.lines):
                if rx.match(l.strip()):
                    node, end = parse_block(self.lines, i)
                    self._blocks[name] = _Block(i, end, node)
                    break
            else:
                raise AsciiError(f"no {name} block in the ASCII file")
        return self._blocks[name]

    # ------------------------------------------------------------------ units
    @staticmethod
    def _unit_factor(unit: str) -> float:
        """file units per mm"""
        return {"METRIC": 1.0, "MILS": 1 / 0.0254, "INCHES": 1 / 25.4}.get(unit.upper(), 0) or _bad_unit(unit)

    def rules_unit(self) -> str:
        return self._block("rules", r"^RULES_SECTION\b").node.value or "MILS"

    def signals(self) -> list[str]:
        return [l.split()[1] for l in self.lines if l.startswith("*SIGNAL* ") and len(l.split()) > 1]

    # ------------------------------------------------------------------ rules: read
    def _rules(self) -> Node:
        return self._block("rules", r"^RULES_SECTION\b").node

    def _class_data(self, create: bool = False) -> Node | None:
        """The NET_CLASS DATA block. A design without net classes has only a bare
        'NET_CLASS DATA' line (no braces); create=True turns it into a block (or adds one)."""
        rules = self._rules()
        n = rules.child("NET_CLASS DATA")
        if n is not None and n.kids is not None:
            return n
        if not create:
            return None
        if n is None:
            n = Node("NET_CLASS DATA")
            rules.kids.insert(0, n)
        n.kids = []
        return n

    def _design_rules(self) -> Node:
        n = self._rules().child("DESIGN RULES")
        if n is None or n.kids is None:
            raise AsciiError("RULES_SECTION has no DESIGN RULES block")
        return n

    def net_classes(self) -> dict[str, list[str]]:
        data = self._class_data()
        if data is None:
            return {}
        out = {}
        for k in data.kids:
            if k.key == "NET_CLASS" and k.kids is not None:
                out[k.value] = [n.value for n in k.leaves("NET")]
        return out

    @staticmethod
    def _scope(rs: Node, which: str) -> str:
        blk = rs.child(which)
        if blk is None or not blk.kids:
            return "?"
        h = blk.kids[0].head.strip()
        if h.startswith("DEFAULT"):
            return "DEFAULT"
        if h.startswith("NET_CLASS "):
            return h.split(" ", 1)[1]
        if h.startswith("NET "):
            return "NET:" + h.split(" ", 1)[1]
        return h

    @staticmethod
    def _rule_body(rs: Node) -> tuple[str, Node] | None:
        for k in rs.kids or []:
            for kind, kw in RULE_KINDS.items():
                if k.head.startswith(kw) and k.kids is not None:
                    return kind, k
        return None

    def rule_sets(self) -> list[tuple[Node, str, str, str, Node]]:
        """[(rule_set, for, against, kind, body)]"""
        out = []
        for rs in self._design_rules().kids:
            if rs.key != "RULE_SET" or rs.kids is None:
                continue
            body = self._rule_body(rs)
            if body:
                out.append((rs, self._scope(rs, "FOR :"), self._scope(rs, "AGAINST :"), body[0], body[1]))
        return out

    def get_rules(self, full: bool = False) -> dict:
        unit = self.rules_unit()
        rules: dict[str, dict] = {}
        for rs, f, against, kind, body in self.rule_sets():
            if against != "DEFAULT":
                f = f"{f} vs {against}"
            vals: dict = {}
            for leaf in body.kids:
                if leaf.kids is not None or not leaf.key:
                    continue
                if leaf.key in MULTI_KEYS:
                    vals.setdefault(leaf.key, []).append(leaf.value)
                else:
                    vals[leaf.key] = leaf.value
            if kind == "route":
                vals.setdefault("AUTO_ROUTE", "N")       # PADS omits the line when it is N
            if not full and kind == "clearance":
                keep = {"MIN_TRACK_WIDTH", "REC_TRACK_WIDTH", "MAX_TRACK_WIDTH", "TRACK_TO_TRACK",
                        "VIA_TO_TRACK", "PAD_TO_TRACK", "SMD_TO_TRACK", "COPPER_TO_TRACK", "OUTLINE_TO_TRACK"}
                vals = {k: v for k, v in vals.items() if k in keep}
            rules.setdefault(f, {})[kind] = vals
        return {"unit": "mm" if unit.upper() == "METRIC" else unit.lower(),
                "net_classes": self.net_classes(), "rules": rules}

    # ------------------------------------------------------------------ rules: edit
    def set_net_class(self, name: str, nets: list[str] | None) -> None:
        """Create/replace a net class; nets=None deletes it."""
        if not re.fullmatch(r"[A-Za-z0-9_+\-.]+", name):
            raise AsciiError(f"bad net class name {name!r}")
        known = set(self.signals())
        missing = [n for n in nets or [] if n not in known]
        if missing:
            raise AsciiError(f"unknown nets: {missing}")
        data = self._class_data(create=nets is not None)
        node = data.child(f"NET_CLASS {name}") if data is not None else None
        if nets is None:
            if node is not None:
                data.kids.remove(node)
                if not data.kids:
                    data.kids = None          # back to PADS' bare 'NET_CLASS DATA' line
        else:
            if node is None:
                node = Node(f"NET_CLASS {name}", [])
                data.kids.append(node)
            node.kids = [Node(f"NET {n}") for n in nets]
        self._blocks["rules"].dirty = True

    def _find_rule(self, target: str, kind: str) -> Node | None:
        for rs, f, against, k, body in self.rule_sets():
            if f == target and against == "DEFAULT" and k == kind:
                return body
        return None

    def _new_rule(self, target: str, kind: str) -> Node:
        """Copy the DEFAULT rule set of this kind for a class / net."""
        if target != "DEFAULT" and not target.startswith("NET:") and target not in self.net_classes():
            raise AsciiError(f"no net class {target!r} (create it with net_classes first)")
        base = None
        for rs, f, against, k, body in self.rule_sets():
            if f == "DEFAULT" and against == "DEFAULT" and k == kind:
                base = rs
        dr = self._design_rules()
        ids = [int(m.group(1)) for rs in dr.kids if (m := re.match(r"RULE_SET \((\d+)\)", rs.head))]
        new_id = max(ids, default=0) + 1
        if base is not None:
            rs = copy.deepcopy(base)
            rs.head = f"RULE_SET ({new_id})"
        else:
            rs = Node(f"RULE_SET ({new_id})", [Node("FOR :", []), Node("AGAINST :", [Node("DEFAULT :")]),
                                               Node("LAYER 0"), Node(f"{RULE_KINDS[kind]} :", [])])
        scope = ("DEFAULT :" if target == "DEFAULT" else
                 f"NET {target[4:]}" if target.startswith("NET:") else f"NET_CLASS {target}")
        rs.child("FOR :").kids = [Node(scope)]
        dr.kids.append(rs)
        return self._rule_body(rs)[1]

    def set_rule(self, target: str, kind: str, values: dict) -> dict:
        """Set keys of the `kind` rule ('clearance'|'route'|'high_speed') for DEFAULT / a class / NET:x.
        Values are raw PADS keys in file units; None removes a key; lists for VALID_LAYER/VALID_VIA_TYPE."""
        if kind not in RULE_KINDS:
            raise AsciiError(f"rule kind must be one of {list(RULE_KINDS)}")
        body = self._find_rule(target, kind)
        created = body is None
        if created:
            body = self._new_rule(target, kind)
        for k, v in values.items():
            body.set(k.upper(), v)
        self._blocks["rules"].dirty = True
        return {"target": target, "kind": kind, "created_rule_set": created, "set": values}

    def apply_rule_spec(self, spec: dict) -> list[dict]:
        """Friendly rule spec (lengths in mm) -> raw rule edits. See layout_set_rules for the keys."""
        target = spec.get("target") or "DEFAULT"
        f = self._unit_factor(self.rules_unit())
        L = lambda mm: fmt(float(mm) * f)  # noqa: E731
        yn = lambda b: "Y" if b else "N"   # noqa: E731
        clr, route, hs = {}, {}, {}
        w = spec.get("width")
        if w is not None:
            if isinstance(w, (int, float)):
                w = {"min": w, "rec": w, "max": max(w, float(w))}
            for k, key in (("min", "MIN_TRACK_WIDTH"), ("rec", "REC_TRACK_WIDTH"), ("max", "MAX_TRACK_WIDTH")):
                if w.get(k) is not None:
                    clr[key] = L(w[k])
        if spec.get("clearance") is not None:
            for key in SIGNAL_CLEARANCE_KEYS:
                clr[key] = L(spec["clearance"])
        if spec.get("auto_route") is not None:
            route["AUTO_ROUTE"] = "Y" if spec["auto_route"] else None   # PADS writes nothing for N
        if spec.get("priority") is not None:
            route["ROUTE_PRIORITY"] = int(spec["priority"])
        if spec.get("layers") is not None:
            route["VALID_LAYER"] = [int(x) for x in spec["layers"]]
        if spec.get("via_types") is not None:
            route["VALID_VIA_TYPE"] = list(spec["via_types"])
        if spec.get("max_vias") is not None:
            route["MAX_NUMBER_OF_VIAS"] = int(spec["max_vias"])
        for k, key in (("length_min", "MIN_LENGTH"), ("length_max", "MAX_LENGTH"),
                       ("match_tolerance", "MATCH_LENGTH_TOLERANCE"), ("stub_max", "STUB_LENGTH")):
            if spec.get(k) is not None:
                hs[key] = L(spec[k])
        for k, key in (("impedance_min", "MIN_IMPEDANCE"), ("impedance_max", "MAX_IMPEDANCE")):
            if spec.get(k) is not None:
                hs[key] = f"{float(spec[k]):.6f}"
        for kind, raw in (spec.get("raw") or {}).items():
            {"clearance": clr, "route": route, "high_speed": hs}[kind].update(raw)
        out = []
        for kind, vals in (("clearance", clr), ("route", route), ("high_speed", hs)):
            if vals:
                out.append(self.set_rule(target, kind, vals))
        if not out:
            raise AsciiError(f"rule spec for {target} sets nothing: {spec}")
        return out

    # ------------------------------------------------------------------ board / vias
    def _section(self, name: str) -> tuple[int, int]:
        start = next(i for i, l in enumerate(self.lines) if l.startswith(name))
        end = next((i for i in range(start + 1, len(self.lines))
                    if self.lines[i].startswith("*") and not self.lines[i].startswith("*REMARK*")), len(self.lines))
        return start, end

    def board_outline(self) -> dict | None:
        """Board outline polygon (file units) from *LINES* items of type BOARD."""
        s, e = self._section("*LINES*")
        L = self.lines
        i = s + 1
        while i < e:
            t = L[i].split()
            if len(t) >= 5 and t[1] == "BOARD":
                x0, y0, pieces = float(t[2]), float(t[3]), int(t[4])
                pts, j = [], i + 1
                for _ in range(pieces):
                    pt = L[j].split()
                    n = int(pt[1])
                    for k in range(n):
                        c = [float(v) for v in L[j + 1 + k].split()]
                        pts.append((x0 + c[0], y0 + c[1]))
                        if len(c) >= 8:          # x y begin-angle sweep (0.1 deg) and the circle's bbox
                            pts += [(x0 + x, y0 + y) for x, y in arc_points(*c[2:8])][1:-1]
                    j += 1 + n
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                return {"polygon": pts, "bbox": [min(xs), min(ys), max(xs), max(ys)]}
            i += 1
        return None

    def via_types(self) -> dict[str, dict]:
        """{name: {drill, pad, levels}} from the *VIA* section (file units). pad = largest pad;
        levels = {level: pad size} with -2 top, -1 inner layers, 0 bottom, n = that layer."""
        s, e = self._section("*VIA*")
        out, L = {}, self.lines
        for i in range(s + 1, e):
            t = L[i].split()
            if len(t) == 3 and not L[i].startswith("*") and re.fullmatch(r"[\d.]+", t[1]) and t[2].isdigit():
                pads = [L[i + 1 + k].split() for k in range(int(t[2]))]
                levels = {int(p[0]): float(p[1]) for p in pads if len(p) > 1 and re.fullmatch(r"-?\d+", p[0])}
                out[t[0]] = {"drill": float(t[1]), "pad": max(levels.values(), default=0), "levels": levels}
        return out

    # ------------------------------------------------------------------ parts and pads
    _DECAL = re.compile(r"^(\S+)\s+([A-Z])\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$")
    _TERM = re.compile(r"^T(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(\S+)")
    _PART = re.compile(r"^(\S+)\s+(\S+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+[UG]\s+([MN])\s+(\d+)")
    _PTYPE = re.compile(r"^(\S+)\s+(\S+)\s+\S+\s+\d+\s+\d+\s+\d+\s+\d+\s+[YN]\b")

    def decals(self) -> dict[str, dict]:
        """{name: {scale, terms: [(pin, x, y)], stacks: {pin or "0": [pad line tokens]}}} from *PARTDECAL*.
        Decal values are in the decal's own unit (M = mm, I = mil); scale converts them to file units."""
        s, e = self._section("*PARTDECAL*")
        per_mm = self._unit_factor(self.file_unit())
        out, L, cur, i = {}, self.lines, None, s + 1
        while i < e:
            line = L[i]
            m = self._DECAL.match(line)
            if m:
                scale = {"M": per_mm, "I": 0.0254 * per_mm}.get(m.group(2), 1.0)
                cur = out[m.group(1)] = {"scale": scale, "terms": [], "stacks": {}}
            elif cur is not None:
                t = self._TERM.match(line)
                tok = line.split()
                if t:
                    cur["terms"].append((t.group(5), float(t.group(1)), float(t.group(2))))
                elif len(tok) == 3 and tok[0] == "PAD" and tok[1].isdigit() and tok[2].isdigit():
                    k = int(tok[2])
                    cur["stacks"][tok[1]] = [L[i + 1 + j].split() for j in range(k)]
                    i += k
            i += 1
        return out

    def parts(self) -> list[dict]:
        """[{ref, decal, x, y, rotation, bottom}] from *PART* (file units)."""
        s, e = self._section("*PARTTYPE*")
        types = {m.group(1): m.group(2).split(":") for l in self.lines[s + 1:e] if (m := self._PTYPE.match(l))}
        s, e = self._section("*PART*")
        out = []
        for line in self.lines[s + 1:e]:
            m = self._PART.match(line)
            if not m:
                continue
            ptype, alt = m.group(2), int(m.group(7))
            if "@" in ptype:
                decal = ptype.split("@", 1)[1]
            else:
                names = types.get(ptype, [ptype])
                decal = names[alt] if alt < len(names) else names[0]
            out.append({"ref": m.group(1), "decal": decal, "x": float(m.group(3)), "y": float(m.group(4)),
                        "rotation": float(m.group(5)), "bottom": m.group(6) == "M"})
        return out

    def pads(self) -> list[dict]:
        """Copper of every part pin (file units): [{pin, x, y, bottom, smd, levels: {level: shape}}].
        level: -2 mounted side, -1 inner layers, 0 opposite side, n = that layer.
        shape: ("cap", ax, ay, bx, by, r) - a segment with a radius (a == b: circle), or
               ("poly", [(x, y), ...]) - a rectangle (corner rounding/chamfer ignored)."""
        decals, out = self.decals(), []
        for p in self.parts():
            dec = decals.get(p["decal"])
            if dec is None:
                continue
            k = dec["scale"]
            ca, sa = math.cos(math.radians(p["rotation"])), math.sin(math.radians(p["rotation"]))
            X, Y, mirror = p["x"], p["y"], p["bottom"]

            def T(x, y):      # decal point -> board point: scale, rotate, mirror (bottom side), move
                x, y = x * k, y * k
                x, y = x * ca - y * sa, x * sa + y * ca
                return (X - x if mirror else X + x), Y + y

            for n, (pin, tx, ty) in enumerate(dec["terms"], 1):
                stack = dec["stacks"].get(pin) or dec["stacks"].get(str(n)) or dec["stacks"].get("0") or []
                levels, drill = {}, 0.0
                for tok in stack:
                    if not tok or not re.fullmatch(r"-?\d+", tok[0]):
                        continue
                    shape, dr = _pad_shape(tok, tx, ty, T, k)
                    if shape is not None:
                        levels[int(tok[0])] = shape
                    drill = max(drill, dr)
                x, y = T(tx, ty)
                out.append({"pin": f"{p['ref']}.{pin}", "x": x, "y": y, "bottom": mirror, "smd": drill <= 0,
                            "levels": levels})
        return out

    # ------------------------------------------------------------------ stack-up
    def _layers(self) -> Node:
        return self._block("layers", r"^LAYER (METRIC|MILS|INCHES|BASIC)$").node

    def stackup(self) -> dict:
        """Electrical layers top->bottom. LAYER_THICKNESS/DIELECTRIC of layer n describe the
        dielectric BELOW layer n (layer 0 = coating above the top layer)."""
        top = self._layers()
        f = self._unit_factor(top.value)
        rows = []
        for k in top.kids or []:
            if k.key != "LAYER" or k.kids is None or not k.value.isdigit():
                continue
            n = int(k.value)
            cu = k.get("COPPER_THICKNESS")
            if n != 0 and (cu is None or (k.get("LAYER_TYPE") or "").upper() not in ("ROUTING", "PLANE")):
                continue                                    # unassigned / documentation layers
            rows.append({"layer": n, "name": k.get("LAYER_NAME") or ("coating" if n == 0 else ""),
                         "type": k.get("LAYER_TYPE"), "plane": k.get("PLANE"),
                         "copper_mm": round(float(cu) / f, 5) if cu else None,
                         "dielectric_below_mm": round(float(k.get("LAYER_THICKNESS") or 0) / f, 5),
                         "er_below": float(k.get("DIELECTRIC") or 0)})
        rows.sort(key=lambda r: r["layer"])
        thick = sum(r["dielectric_below_mm"] for r in rows) + sum(r["copper_mm"] or 0 for r in rows)
        return {"layers": rows, "board_thickness_mm": round(thick, 4)}

    def set_stackup(self, edits: list[dict]) -> list[dict]:
        """edits: [{layer, dielectric_below_mm?, er_below?, copper_mm?}]"""
        top = self._layers()
        f = self._unit_factor(top.value)
        done = []
        for e in edits:
            node = top.child(f"LAYER {int(e['layer'])}")
            if node is None or node.kids is None:
                raise AsciiError(f"no layer {e['layer']}")
            if e.get("dielectric_below_mm") is not None:
                node.set("LAYER_THICKNESS", fmt(float(e["dielectric_below_mm"]) * f))
            if e.get("er_below") is not None:
                node.set("DIELECTRIC", f"{float(e['er_below']):.6f}")
            if e.get("copper_mm") is not None:
                node.set("COPPER_THICKNESS", fmt(float(e["copper_mm"]) * f))
            done.append(e)
        self._blocks["layers"].dirty = True
        return done


def _bad_unit(unit: str):
    raise AsciiError(f"unsupported unit {unit!r}")


def arc_points(begin: float, sweep: float, x1: float, y1: float, x2: float, y2: float,
               step_deg: float = 10.0) -> list[tuple[float, float]]:
    """Points along a PADS arc: begin angle and sweep in 0.1 degree, circle bounding box x1 y1 x2 y2.
    Includes both end points."""
    cx, cy, r = (x1 + x2) / 2, (y1 + y2) / 2, abs(x2 - x1) / 2
    a0, da = math.radians(begin / 10), math.radians(sweep / 10)
    n = max(2, math.ceil(abs(sweep / 10) / step_deg))
    return [(cx + r * math.cos(a0 + da * i / n), cy + r * math.sin(a0 + da * i / n)) for i in range(n + 1)]


def _num(tok: list[str], i: int) -> float:
    try:
        return float(tok[i])
    except (IndexError, ValueError):
        return 0.0


def _pad_shape(tok: list[str], tx: float, ty: float, T, k: float):
    """One pad stack line -> (shape on the board, drill). Formats (decal units):
    R: level size R [drill [plated]]           S: level size S corner [drill [plated]]
    A: level size A inner [drill [plated]]     RF: level size RF ori length offset corner [drill [plated]]
    OF: level size OF ori length offset [drill [plated]]   RT/ST/RA/SA: plane thermals / anti-pads (skipped)"""
    size, kind = _num(tok, 1), tok[2] if len(tok) > 2 else "R"
    drill = {"R": _num(tok, 3), "S": _num(tok, 4), "A": _num(tok, 4), "RF": _num(tok, 7),
             "OF": _num(tok, 6)}.get(kind, 0.0) * k
    if size <= 0 or kind in ("RT", "ST", "RA", "SA"):
        return None, drill
    if kind in ("RF", "OF"):
        ang, length, off = math.radians(_num(tok, 3)), _num(tok, 4), _num(tok, 5)
        ux, uy = math.cos(ang), math.sin(ang)
        cx, cy = tx + off * ux, ty + off * uy
        if kind == "RF":
            hl, hw = length / 2, size / 2
            return ("poly", [T(cx + sx * hl * ux - sy * hw * uy, cy + sx * hl * uy + sy * hw * ux)
                             for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1))]), drill
        if length > size:
            h = (length - size) / 2
            ax, ay = T(cx - h * ux, cy - h * uy)
            bx, by = T(cx + h * ux, cy + h * uy)
            return ("cap", ax, ay, bx, by, size * k / 2), drill
        size = max(size, length)
    if kind == "S":
        h = size / 2
        return ("poly", [T(tx + sx * h, ty + sy * h) for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1))]), drill
    x, y = T(tx, ty)
    return ("cap", x, y, x, y, size * k / 2), drill


# =========================================================================== *ROUTE*
# A net is stored as 2-pin "connections". Each connection is a pin-pair line followed by points:
#   X Y LAYER WIDTH FLAGS [VIANAME] [THERMAL]
# LAYER = layer of the segment that LEAVES the point (0 = unrouted ratsnest, 65 = end point),
# FLAGS 1792 = start/end/via point, 1536 = corner. A via point carries the via name and the
# layer AFTER the via. Unrouted connection = 2 points: start layer 0, end layer 65.
END_LAYER = 65
F_TERM, F_CORNER = 1792, 1536


@dataclass
class RoutePoint:
    x: float
    y: float
    layer: int
    width: float
    flags: int
    extra: list[str] = field(default_factory=list)   # e.g. ["STANDARDVIA", "THERMAL"]

    @property
    def via(self) -> str | None:
        return self.extra[0] if self.extra and self.extra[0] not in ("THERMAL",) and "VIA" in self.extra[0].upper() \
            else None

    def line(self) -> str:
        s = f"{fmt(self.x):<6} {fmt(self.y):<6} {self.layer} {fmt(self.width)} {self.flags}"
        if self.via:
            return s + " " + " ".join(self.extra) + " "
        return s + ("  " + " ".join(self.extra) + " " if self.extra else " ")


@dataclass
class Connection:
    a: str
    b: str
    pts: list[RoutePoint]

    @property
    def routed(self) -> bool:
        return bool(self.pts) and self.pts[0].layer != 0

    def lines(self) -> list[str]:
        return [f"{self.a:<31} {self.b:<31} "] + [p.line() for p in self.pts]


@dataclass
class RouteSignal:
    header: str
    conns: list[Connection]

    @property
    def name(self) -> str:
        return self.header.split()[1]


def _is_pt(tokens: list[str]) -> bool:
    try:
        float(tokens[0]); float(tokens[1]); int(tokens[2])
        return len(tokens) >= 5
    except (ValueError, IndexError):
        return False


class Routes:
    """Parsed *ROUTE* section of a PcbAscii; write back with commit()."""

    def __init__(self, pcb: PcbAscii):
        self.pcb = pcb
        L = pcb.lines
        self.start = next(i for i, l in enumerate(L) if l.startswith("*ROUTE*"))
        self.end = next(i for i in range(self.start + 1, len(L))
                        if L[i].startswith("*") and not L[i].startswith(("*REMARK*", "*SIGNAL*")))
        self.prefix: list[str] = []          # *ROUTE* line, remarks, blank lines
        self.signals: list[RouteSignal] = []
        cur, conn = None, None
        for l in L[self.start:self.end]:
            t = l.split()
            if l.startswith("*SIGNAL*"):
                cur = RouteSignal(l, [])
                self.signals.append(cur)
                conn = None
            elif cur is None or not t:
                if cur is None:
                    self.prefix.append(l)
            elif _is_pt(t):
                if conn is None:
                    raise AsciiError(f"route point before a pin pair: {l!r}")
                conn.pts.append(RoutePoint(float(t[0]), float(t[1]), int(t[2]), float(t[3]), int(t[4]), t[5:]))
            else:
                conn = Connection(t[0], t[1] if len(t) > 1 else "", [])
                cur.conns.append(conn)

    def of(self, net: str) -> list[RouteSignal]:
        return [s for s in self.signals if s.name == net]

    def connections(self, net: str) -> list[Connection]:
        return [c for s in self.of(net) for c in s.conns]

    def replace(self, net: str, conns: list[Connection]) -> None:
        sigs = self.of(net)
        if not sigs:
            raise AsciiError(f"net {net} not found in *ROUTE*")
        sigs[0].conns = conns
        for s in sigs[1:]:
            self.signals.remove(s)

    def commit(self) -> None:
        out = list(self.prefix)
        for s in self.signals:
            out.append(s.header)
            for c in s.conns:
                out += c.lines()
            out.append("")
        self.pcb.lines[self.start:self.end] = out
        self.end = self.start + len(out)


def unrouted(a: str, b: str, pa: tuple[float, float], pb: tuple[float, float], width: float) -> Connection:
    return Connection(a, b, [RoutePoint(pa[0], pa[1], 0, width, F_TERM, ["THERMAL"]),
                             RoutePoint(pb[0], pb[1], END_LAYER, width, F_TERM, ["THERMAL"])])


def build_route(a: str, b: str, pa, pb, start_layer: int, width: float, path: list,
                via_name: str = "STANDARDVIA") -> Connection:
    """path: intermediate points [x, y] or [x, y, new_layer] (a layer change = via at that point)."""
    pts = [RoutePoint(pa[0], pa[1], start_layer, width, F_TERM, ["THERMAL"])]
    layer = start_layer
    for p in path:
        x, y = float(p[0]), float(p[1])
        if len(p) > 2 and p[2] is not None and int(p[2]) != layer:
            layer = int(p[2])
            pts.append(RoutePoint(x, y, layer, width, F_TERM, [via_name, "THERMAL"]))
        else:
            pts.append(RoutePoint(x, y, layer, width, F_CORNER))
    pts.append(RoutePoint(pb[0], pb[1], END_LAYER, width, F_TERM, ["THERMAL"]))
    # drop zero-length steps (a via may sit exactly on a corner)
    clean = [pts[0]]
    for p in pts[1:]:
        q = clean[-1]
        if abs(p.x - q.x) < 1e-6 and abs(p.y - q.y) < 1e-6 and not p.via and p.layer != END_LAYER:
            continue
        clean.append(p)
    return Connection(a, b, clean)
