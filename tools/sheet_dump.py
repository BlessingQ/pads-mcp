"""Summarize one sheet of a PADS Logic ASCII file: decals, part types, parts, symbols, signals."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from pads_mcp.sch_ascii import SchematicAscii, _decal_blocks
from pads_mcp.edit import _parttype_blocks

sch = SchematicAscii.load(sys.argv[1])
sh = sch.sheet(sys.argv[2])
print("decals   :", list(dict(_decal_blocks(sh.section("CAEDECAL").body)).keys()))
print("parttypes:", list(_parttype_blocks(sh.section("PARTTYPE").body).keys()))
print("parts    :", sh.part_refs())
print("offpage  :", [(o.id, o.net, o.sym) for o in sh.offpage])
print("signals  :", [(s.name, len(s.segments)) for s in sh.signals])
if len(sys.argv) > 3:
    body = sh.section(sys.argv[3]).body
    print(f"--- *{sys.argv[3]}* body ({len(body)} lines) ---")
    print("\n".join(body[:int(sys.argv[4]) if len(sys.argv) > 4 else 80]))
