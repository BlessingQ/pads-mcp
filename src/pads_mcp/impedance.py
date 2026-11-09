"""Closed-form trace impedance (approximate, +-5..10 %; confirm with the fab's field solver).

microstrip: Hammerstad & Jensen (1980) with the usual strip-thickness correction
stripline : Wheeler / Cohn, symmetric, planes above and below
differential: edge-coupled approximations (IPC-2141 style)
All lengths in mm.
"""
from __future__ import annotations

import math

ETA0 = 376.730313


def _w_eff_micro(w: float, h: float, t: float) -> float:
    if t <= 0:
        return w
    if w / h <= 1 / (2 * math.pi):
        return w + (t / math.pi) * (1 + math.log(4 * math.pi * w / t))
    return w + (t / math.pi) * (1 + math.log(2 * h / t))


def microstrip_z0(w: float, h: float, er: float, t: float = 0.0) -> float:
    u = _w_eff_micro(w, h, t) / h
    f = 6 + (2 * math.pi - 6) * math.exp(-((30.666 / u) ** 0.7528))
    z_air = ETA0 / (2 * math.pi) * math.log(f / u + math.sqrt(1 + (2 / u) ** 2))
    a = 1 + math.log((u ** 4 + (u / 52) ** 2) / (u ** 4 + 0.432)) / 49 + math.log(1 + (u / 18.1) ** 3) / 18.7
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    er_eff = (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)
    return z_air / math.sqrt(er_eff)


def stripline_z0(w: float, b: float, er: float, t: float = 0.0) -> float:
    """b = plane-to-plane spacing."""
    if t > 0:
        w = w + (t / math.pi) * (1 + math.log(4 * math.pi * w / t)) * 0.5
    x = w / b
    we = w if x >= 0.35 else w - (0.35 - x) ** 2 * b
    return 30 * math.pi / math.sqrt(er) * b / (we + 0.441 * b)


def diff_microstrip(z0: float, s: float, h: float) -> float:
    return 2 * z0 * (1 - 0.48 * math.exp(-0.96 * s / h))


def diff_stripline(z0: float, s: float, b: float) -> float:
    return 2 * z0 * (1 - 0.347 * math.exp(-2.9 * s / b))


def _solve(fn, target: float, lo: float, hi: float) -> float | None:
    """width with fn(width) == target (fn decreases with width)."""
    flo, fhi = fn(lo), fn(hi)
    if not (fhi <= target <= flo):
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if fn(mid) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def geometry_for_layer(stackup: dict, layer: int) -> dict:
    """Microstrip/stripline parameters of electrical layer `layer` from PcbAscii.stackup()."""
    rows = {r["layer"]: r for r in stackup["layers"]}
    n = max(k for k in rows if k > 0)
    t = rows[layer]["copper_mm"] or 0.035
    if layer == 1:
        return {"kind": "microstrip", "h": rows[1]["dielectric_below_mm"], "er": rows[1]["er_below"], "t": t}
    if layer == n:
        return {"kind": "microstrip", "h": rows[n - 1]["dielectric_below_mm"], "er": rows[n - 1]["er_below"], "t": t}
    h1, h2 = rows[layer - 1]["dielectric_below_mm"], rows[layer]["dielectric_below_mm"]
    er = (rows[layer - 1]["er_below"] * h1 + rows[layer]["er_below"] * h2) / max(h1 + h2, 1e-9)
    return {"kind": "stripline", "b": h1 + h2 + t, "er": er, "t": t}


def evaluate(geo: dict, width: float | None = None, target: float | None = None,
             gap: float | None = None) -> dict:
    kind, er, t = geo["kind"], geo["er"], geo["t"]
    ref = geo["h"] if kind == "microstrip" else geo["b"]
    if not ref or ref <= 0 or not er:
        raise ValueError(f"stack-up has no dielectric for this layer: {geo}")
    z0 = (lambda w: microstrip_z0(w, ref, er, t)) if kind == "microstrip" else (lambda w: stripline_z0(w, ref, er, t))
    zd = None
    if gap is not None:
        zd = (lambda w: diff_microstrip(z0(w), gap, ref)) if kind == "microstrip" else \
             (lambda w: diff_stripline(z0(w), gap, ref))
    out = {"model": kind, "inputs_mm": {k: round(v, 5) for k, v in geo.items() if k != "kind"}}
    if target is not None:
        fn = zd or z0
        w = _solve(fn, target, ref * 0.01, ref * 30)
        if w is None:
            out["width_mm"] = None
            out["note"] = f"{target} ohm is not reachable on this stack-up"
        else:
            width = w
            out["width_mm"] = round(w, 4)
    if width is not None:
        out["z0_ohm"] = round(z0(width), 2)
        if zd:
            out["zdiff_ohm"] = round(zd(width), 2)
        out["width_mm"] = round(width, 4)
        if kind == "microstrip" and not 0.1 <= width / ref <= 3.0:
            out["warning"] = "w/h outside the comfortable range of the formula; accuracy is lower"
    out["disclaimer"] = "closed-form estimate (solder mask ignored); confirm with the PCB fab's impedance calculation"
    return out
