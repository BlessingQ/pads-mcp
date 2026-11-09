"""Parser / writer for PADS Logic ASCII schematic files (PADS-LOGIC-V9.x).

Only the parts needed for editing connectivity are modelled; everything else is
kept verbatim so that a parse -> serialize round trip is lossless for sheets
that were not modified.

Per sheet, the file contains these sections (in order):
    *CAE* *TEXT* *LINES* *CAEDECAL* *PARTTYPE* *PART* *BUSSES*
    *OFFPAGE REFS* *TIEDOTS* *CONNECTION* *NETNAMES*

Connectivity model:
    *CONNECTION* holds `*SIGNAL* <net>` blocks; each block is a list of
    segments `<A> <B> <npts> <flag>` followed by npts coordinate lines.
    Endpoints are `REFDES.PIN`, tie dots `@@@Dn` or off-page / power / ground
    symbols `@@@On`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ENCODING = "cp949"
EOL = "\r\n"

SHEET_SECTIONS = (
    "CAE", "TEXT", "LINES", "CAEDECAL", "PARTTYPE", "PART", "BUSSES",
    "OFFPAGE REFS", "TIEDOTS", "CONNECTION", "NETNAMES",
)
_SECTION_RE = re.compile(r"^\*(" + "|".join(re.escape(s) for s in SHEET_SECTIONS) + r")\*")
_PIN_RE = re.compile(r"^T(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s.*\s(\S+)\s*$")


# --------------------------------------------------------------------------- model
@dataclass
class Segment:
    a: str
    b: str
    pts: list[tuple[int, int]]
    flag: str = "0"

    def ends(self) -> tuple[str, str]:
        return self.a, self.b


@dataclass
class Signal:
    name: str
    extra: str = "0 0"
    segments: list[Segment] = field(default_factory=list)


@dataclass
class OffPage:
    """Off-page reference, power or ground symbol instance."""
    id: str
    net: str
    sym: str  # symbol set, e.g. $GND_SYMS, $PWR_SYMS, $OSR_SYMS
    x: int
    y: int
    rot: int = 0
    flip: int = 0
    variant: int = 0


@dataclass
class TieDot:
    id: str
    x: int
    y: int


@dataclass
class DecalPin:
    x: int
    y: int
    rot: int
    mirror: int
    pin_decal: str


@dataclass
class SymbolSet:
    name: str                 # $GND_SYMS
    variants: list[tuple[str, str]]  # (decal, default net)
    lines: list[str]          # raw PARTTYPE block


@dataclass
class Section:
    name: str
    header: str
    body: list[str]


class Sheet:
    def __init__(self, header: str):
        self.header = header
        tok = header.split()
        self.num = int(tok[1])
        self.name = tok[2]
        self.sections: list[Section] = []
        self.offpage: list[OffPage] = []
        self.tiedots: list[TieDot] = []
        self.signals: list[Signal] = []
        self.netnames: list[str] = []
        self.dirty = False

    # ----- section helpers
    def section(self, name: str) -> Section:
        for s in self.sections:
            if s.name == name:
                return s
        raise KeyError(f"sheet {self.name}: no *{name}* section")

    # ----- parsed views
    def parse(self) -> None:
        self.offpage = [_parse_offpage(l) for l in self.section("OFFPAGE REFS").body if l.startswith("@@@O")]
        self.tiedots = [_parse_tiedot(l) for l in self.section("TIEDOTS").body if l.startswith("@@@D")]
        self.signals = _parse_connection(self.section("CONNECTION").body)
        self.netnames = [l for l in self.section("NETNAMES").body if l.strip()]

    def decals(self) -> dict[str, list[DecalPin]]:
        return parse_decals(self.section("CAEDECAL").body)

    def pin_decal_dirs(self) -> dict[str, tuple[int, int]]:
        return parse_pin_decal_dirs(self.section("CAEDECAL").body)

    def symbol_sets(self) -> dict[str, SymbolSet]:
        return parse_symbol_sets(self.section("PARTTYPE").body)

    def part_refs(self) -> list[str]:
        out = []
        for l in self.section("PART").body:
            t = l.split()
            if _is_part_header(t):
                out.append(t[0])
        return out

    # ----- lookup / id allocation
    def signal(self, name: str, create: bool = False) -> Signal | None:
        for s in self.signals:
            if s.name == name:
                return s
        if create:
            s = Signal(name)
            self.signals.append(s)
            return s
        return None

    def next_id(self, prefix: str) -> str:
        used = {o.id for o in self.offpage} | {t.id for t in self.tiedots}
        n = 0
        while f"{prefix}{n}" in used:
            n += 1
        return f"{prefix}{n}"

    def offpage_by_id(self, oid: str) -> OffPage | None:
        return next((o for o in self.offpage if o.id == oid), None)

    def tiedot_by_id(self, tid: str) -> TieDot | None:
        return next((t for t in self.tiedots if t.id == tid), None)

    # ----- serialize
    def lines(self) -> list[str]:
        out = [self.header]
        for s in self.sections:
            out.append(s.header)
            if self.dirty and s.name in ("OFFPAGE REFS", "TIEDOTS", "CONNECTION", "NETNAMES"):
                out.extend(self._emit(s.name))
            else:
                out.extend(s.body)
        return out

    def _emit(self, name: str) -> list[str]:
        if name == "OFFPAGE REFS":
            return ["", "", ""] + [
                f"{o.id:<8} {o.net} {o.sym} {o.x:<6} {o.y:<6} {o.rot} {o.flip} {o.variant}" for o in self.offpage
            ]
        if name == "TIEDOTS":
            return ["", "", ""] + [f"{t.id:<8} {t.x:<6} {t.y:<6}" for t in self.tiedots]
        if name == "CONNECTION":
            out = ["", ""]
            for sig in self.signals:
                if not sig.segments:
                    continue
                out.append(f"*SIGNAL* {sig.name} {sig.extra}")
                for seg in sig.segments:
                    out.append(f"{seg.a:<12} {seg.b:<12} {len(seg.pts)} {seg.flag}")
                    out.extend(f"{x:<6} {y:<6}" for x, y in seg.pts)
            return out
        if name == "NETNAMES":
            return ["", "", ""] + self.netnames + [""]
        raise KeyError(name)


class SchematicAscii:
    """A whole PADS Logic ASCII file."""

    def __init__(self, text: str):
        self.trailing_eol = text.endswith(("\r\n", "\n"))
        lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
        if self.trailing_eol:
            lines.pop()
        self.prefix: list[str] = []
        self.suffix: list[str] = []
        self.sheets: list[Sheet] = []
        cur: Sheet | None = None
        sec: Section | None = None
        for line in lines:
            if line.startswith("*SHT*"):
                cur = Sheet(line)
                self.sheets.append(cur)
                sec = None
                continue
            if line.startswith("*END*"):
                cur = None
                self.suffix.append(line)
                continue
            if cur is None:
                (self.suffix if self.suffix else self.prefix).append(line)
                continue
            m = _SECTION_RE.match(line)
            if m:
                sec = Section(m.group(1), line, [])
                cur.sections.append(sec)
            elif sec is not None:
                sec.body.append(line)
        for s in self.sheets:
            s.parse()

    @classmethod
    def load(cls, path: str) -> "SchematicAscii":
        with open(path, encoding=ENCODING, errors="surrogateescape", newline="") as f:
            return cls(f.read())

    def save(self, path: str) -> None:
        with open(path, "w", encoding=ENCODING, errors="surrogateescape", newline="") as f:
            f.write(self.text())

    def text(self) -> str:
        out = list(self.prefix)
        for s in self.sheets:
            out.extend(s.lines())
        out.extend(self.suffix)
        return EOL.join(out) + (EOL if self.trailing_eol else "")

    def sheet(self, name_or_num) -> Sheet:
        for s in self.sheets:
            if s.name == str(name_or_num) or str(s.num) == str(name_or_num):
                return s
        raise KeyError(f"no sheet {name_or_num!r}; sheets: {[s.name for s in self.sheets]}")

    def sheet_of_part(self, refdes: str) -> Sheet:
        for s in self.sheets:
            if refdes in s.part_refs():
                return s
        raise KeyError(f"part {refdes} not found in any sheet")

    def nets_of_pins(self) -> dict[str, str]:
        """pin ('U1.3') -> net name, from wiring endpoints."""
        out: dict[str, str] = {}
        for sh in self.sheets:
            for sig in sh.signals:
                for seg in sig.segments:
                    for e in seg.ends():
                        if not e.startswith("@@@"):
                            out[e] = sig.name
        return out


# --------------------------------------------------------------------------- parsers
def _is_part_header(t: list[str]) -> bool:
    if len(t) != 16 or t[0][0] in '"-0123456789':
        return False
    try:
        [int(x) for x in t[2:]]
        return True
    except ValueError:
        return False


def _parse_offpage(line: str) -> OffPage:
    t = line.split()
    return OffPage(t[0], t[1], t[2], int(t[3]), int(t[4]), int(t[5]), int(t[6]), int(t[7]))


def _parse_tiedot(line: str) -> TieDot:
    t = line.split()
    return TieDot(t[0], int(t[1]), int(t[2]))


def _parse_connection(body: list[str]) -> list[Signal]:
    sigs: list[Signal] = []
    cur: Signal | None = None
    i = 0
    while i < len(body):
        line = body[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("*SIGNAL*"):
            t = line.split(None, 2)
            cur = Signal(t[1], t[2].strip() if len(t) > 2 else "0 0")
            sigs.append(cur)
            i += 1
            continue
        t = line.split()
        n = int(t[2])
        pts = []
        for j in range(n):
            x, y = body[i + 1 + j].split()[:2]
            pts.append((int(x), int(y)))
        assert cur is not None, f"segment outside *SIGNAL*: {line!r}"
        cur.segments.append(Segment(t[0], t[1], pts, t[3] if len(t) > 3 else "0"))
        i += 1 + n
    return sigs


def _decal_blocks(body: list[str]):
    """Yield (name, lines) for each CAEDECAL definition."""
    i = 0
    while i < len(body):
        if i + 1 < len(body) and body[i + 1].startswith("TIMESTAMP") and body[i].strip():
            name = body[i].split()[0]
            j = i + 1
            while j < len(body) and not (j + 1 < len(body) and body[j + 1].startswith("TIMESTAMP") and body[j].strip()):
                j += 1
            yield name, body[i:j]
            i = j
        else:
            i += 1


def parse_decals(body: list[str]) -> dict[str, list[DecalPin]]:
    out = {}
    for name, blk in _decal_blocks(body):
        pins = []
        for l in blk:
            m = _PIN_RE.match(l)
            if m:
                # symbol decals (GND, off-page...) end with numbers instead of a pin decal name
                pdec = m.group(5) if not m.group(5).lstrip("-").isdigit() else ""
                pins.append(DecalPin(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), pdec))
        out[name] = pins
    return out


def parse_pin_decal_dirs(body: list[str]) -> dict[str, tuple[int, int]]:
    """For pin decals (no T-lines), the outward unit vector at the hot spot.

    A pin decal draws a line from its hot spot (0,0) towards the part body,
    so 'outward' is the opposite of that line's direction."""
    out = {}
    for name, blk in _decal_blocks(body):
        if any(_PIN_RE.match(l) for l in blk):
            continue
        for k, l in enumerate(blk):
            if l.startswith("OPEN") or l.startswith("CLOSED"):
                try:
                    x0, y0 = map(int, blk[k + 1].split()[:2])
                    x1, y1 = map(int, blk[k + 2].split()[:2])
                except (ValueError, IndexError):
                    break
                if (x0, y0) != (0, 0):
                    x0, y0, x1, y1 = x1, y1, x0, y0
                dx, dy = x1 - x0, y1 - y0
                if abs(dx) >= abs(dy) and dx:
                    out[name] = (-_sgn(dx), 0)
                elif dy:
                    out[name] = (0, -_sgn(dy))
                break
    return out


def parse_symbol_sets(body: list[str]) -> dict[str, SymbolSet]:
    out = {}
    i = 0
    while i < len(body):
        l = body[i]
        if l.startswith("$") and "SYMS" in l.split()[0]:
            name = l.split()[0]
            j = i + 1
            while j < len(body) and body[j].strip():
                j += 1
            blk = body[i:j]
            variants = []
            for v in blk[3:]:
                t = v.split()
                if len(t) >= 2:
                    variants.append((t[0], t[2] if len(t) > 2 else ""))
            out[name] = SymbolSet(name, variants, blk)
            i = j
        else:
            i += 1
    return out


def _sgn(v: int) -> int:
    return (v > 0) - (v < 0)
