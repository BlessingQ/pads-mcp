"""How many wire segments end at each pin / tie dot in a PADS Logic ASCII file?"""
import os, sys
from collections import Counter
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from pads_mcp.sch_ascii import SchematicAscii

sch = SchematicAscii.load(sys.argv[1])
pin_deg, dot_deg = Counter(), Counter()
for sh in sch.sheets:
    for sig in sh.signals:
        for seg in sig.segments:
            for e in seg.ends():
                if e.startswith("@@@D"):
                    dot_deg[(sh.name, e)] += 1
                elif not e.startswith("@@@"):
                    pin_deg[e] += 1
print("pins by number of wires:", sorted(Counter(pin_deg.values()).items()))
print("tie dots by number of wires:", sorted(Counter(dot_deg.values()).items()))
print("pins with >1 wire:", [p for p, n in pin_deg.items() if n > 1][:10])
# where does a tie dot sit relative to its wires? (on a wire corner / middle)
sh = sch.sheets[0]
td = sh.tiedots[0]
print("example tie dot", td, "segments:")
for sig in sh.signals:
    for seg in sig.segments:
        if td.id in seg.ends():
            print("   ", sig.name, seg.a, seg.b, seg.pts)
