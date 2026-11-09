import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from pads_mcp.geometry import route

U, D, L, R = (0, 1), (0, -1), (-1, 0), (1, 0)
cases = [
    ("C101.1 up -> R100.2 up (was broken)", (4700, 6000), U, (5700, 6100), U),
    ("C100.1 up -> U100.VIN left", (2000, 6000), U, (2700, 6300), L),
    ("U100.VOUT right -> C101.1 up", (4000, 6300), R, (4700, 6000), U),
    ("R100.1 down -> D100.1 up", (5700, 5700), D, (5900, 4800), U),
    ("facing pins, aligned", (1000, 1000), R, (2000, 1000), L),
    ("same side (both right)", (1000, 1000), R, (1000, 2000), R),
    ("pin behind start", (2000, 1000), R, (1000, 1500), L),
]
for name, a, ad, b, bd in cases:
    print(f"{name:38} {route(a, ad, b, bd)}")
