"""Validate ASCII parser + geometry conventions against the live PADS Logic doc."""
import os, sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import pythoncom
import win32com.client
from pads_mcp.sch_ascii import SchematicAscii, _is_part_header
from pads_mcp import geometry as G

MILS = 2
asc_path = sys.argv[1]


def getp(obj, name, *args):
    o = obj._oleobj_
    return o.Invoke(o.GetIDsOfNames(name), 0, pythoncom.DISPATCH_PROPERTYGET, True, *args)


# 1. lossless round trip
raw = open(asc_path, encoding="cp949", errors="surrogateescape", newline="").read()
sch = SchematicAscii(raw)
print("1. round-trip identical:", sch.text() == raw, "| sheets:", [s.name for s in sch.sheets])
for s in sch.sheets:
    s.dirty = True
sch2 = SchematicAscii(sch.text())
same = all(
    [(g.name, [(x.a, x.b, x.pts) for x in g.segments]) for g in a.signals]
    == [(g.name, [(x.a, x.b, x.pts) for x in g.segments]) for g in b.signals]
    and a.offpage == b.offpage and a.tiedots == b.tiedots and a.netnames == b.netnames
    for a, b in zip(sch.sheets, sch2.sheets))
print("   regenerated sections parse back equal:", same)
for s in sch.sheets:
    s.dirty = False

# 2. fit all gates using COM positions
pythoncom.CoInitialize()
app = win32com.client.GetActiveObject("PowerLogic.Application")
doc = app.ActiveDocument
print("   COM doc:", doc.Name)
part_hdr = {}
for sh in sch.sheets:
    for l in sh.section("PART").body:
        t = l.split()
        if _is_part_header(t):
            part_hdr[t[0]] = (t[4], t[5])

outward = {}  # pin name -> dir
fit_ok = fit_fail = 0
orient_by_hdr = defaultdict(Counter)
fails = []
for gi in range(1, doc.Gates.Count + 1):
    g = doc.Gates.Item(gi)
    sh = sch.sheet(g.Sheet.Name)
    decals, pdirs = sh.decals(), sh.pin_decal_dirs()
    origin = (getp(g, "PositionX", MILS), getp(g, "PositionY", MILS))
    pins = [g.Pins.Item(i) for i in range(1, g.Pins.Count + 1)]
    world = [(getp(p, "PositionX", MILS), getp(p, "PositionY", MILS)) for p in pins]
    fit = G.fit_gate(origin, world, decals)
    if not fit:
        fit_fail += 1
        fails.append(g.Name)
        continue
    fit_ok += 1
    orient_by_hdr[part_hdr.get(g.Name)][fit.orientation] += 1
    for i, d in G.outward_dirs(fit, decals[fit.decal], pdirs).items():
        outward[pins[i].Name] = d
print(f"2. gate fit: ok={fit_ok} fail={fit_fail} {fails[:10]}")
print("   part header (angle, f5) -> fitted orientation:")
for k, c in sorted(orient_by_hdr.items(), key=lambda kv: str(kv[0])):
    print("     ", k, dict(c))

# 3. outward vs first wire segment direction at pins
agree = disagree = 0
bad = []
for sh in sch.sheets:
    for sig in sh.signals:
        for seg in sig.segments:
            for end, pts in ((seg.a, seg.pts), (seg.b, list(reversed(seg.pts)))):
                if end.startswith("@@@") or end not in outward or len(pts) < 2:
                    continue
                d = (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
                d = ((d[0] > 0) - (d[0] < 0), (d[1] > 0) - (d[1] < 0))
                if d == outward[end]:
                    agree += 1
                else:
                    disagree += 1
                    bad.append((end, outward[end], d))
print(f"3. outward vs wire: agree={agree} disagree={disagree}")
for b in bad[:12]:
    print("     ", b)

# 4. symbol orientation -> wire leaving direction
tab = defaultdict(Counter)
for sh in sch.sheets:
    ops = {o.id: o for o in sh.offpage}
    for sig in sh.signals:
        for seg in sig.segments:
            for end, pts in ((seg.a, seg.pts), (seg.b, list(reversed(seg.pts)))):
                o = ops.get(end)
                if not o or len(pts) < 2:
                    continue
                d = (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
                d = ((d[0] > 0) - (d[0] < 0), (d[1] > 0) - (d[1] < 0))
                tab[(o.sym, o.rot, o.flip)][G.DIR_NAMES.get(d, str(d))] += 1
print("4. symbol (set, rot, flip) -> wire leaves symbol toward:")
for k in sorted(tab):
    print("     ", k, dict(tab[k]))
