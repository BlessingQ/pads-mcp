import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from pads_mcp.sch_ascii import SchematicAscii
for p in sys.argv[1:]:
    raw = open(p, encoding="cp949", errors="surrogateescape", newline="").read()
    print(os.path.basename(p), "identical round-trip:", SchematicAscii(raw).text() == raw)
