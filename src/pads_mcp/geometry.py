"""Pin geometry: orientation fitting, outward directions and simple routing."""
from __future__ import annotations

from dataclasses import dataclass

from .sch_ascii import DecalPin

Vec = tuple[int, int]

# 2x2 integer matrices (a, b, c, d): (x, y) -> (a*x + b*y, c*x + d*y)
_R = {0: (1, 0, 0, 1), 90: (0, -1, 1, 0), 180: (-1, 0, 0, -1), 270: (0, 1, -1, 0)}
_MX = (-1, 0, 0, 1)


def _mul(m, n):
    a, b, c, d = m
    e, f, g, h = n
    return (a * e + b * g, a * f + b * h, c * e + d * g, c * f + d * h)


ORIENTATIONS = {f"R{k}": v for k, v in _R.items()}
ORIENTATIONS.update({f"R{k}M": _mul(v, _MX) for k, v in _R.items()})


def apply(m, v: Vec) -> Vec:
    a, b, c, d = m
    return (a * v[0] + b * v[1], c * v[0] + d * v[1])


DIR_NAMES = {(1, 0): "right", (-1, 0): "left", (0, 1): "up", (0, -1): "down"}
NAME_DIRS = {v: k for k, v in DIR_NAMES.items()}


def local_outward(pin: DecalPin, pin_decal_dirs: dict[str, Vec]) -> Vec:
    """Outward direction of a decal pin in decal-local coordinates."""
    v = pin_decal_dirs.get(pin.pin_decal, (1, 0))
    r = pin.rot % 360
    if r:
        v = apply(_R[r], v)
    if pin.mirror == 1:
        v = (-v[0], v[1])
    elif pin.mirror == 2:
        v = (v[0], -v[1])
    return v


@dataclass
class FitResult:
    decal: str
    orientation: str
    matrix: tuple
    mapping: dict[int, int]  # world pin index -> decal pin index


def fit_gate(origin: Vec, world: list[Vec], decals: dict[str, list[DecalPin]],
             prefer: str | None = None) -> FitResult | None:
    """Find a decal + orientation that maps decal pins exactly onto `world`."""
    names = [prefer] if prefer in decals else []
    names += [n for n, p in decals.items() if len(p) == len(world) and n != prefer]
    want = {(round(x), round(y)): i for i, (x, y) in enumerate(world)}
    for name in names:
        pins = decals[name]
        if len(pins) != len(world):
            continue
        for oname, m in ORIENTATIONS.items():
            mapping = {}
            for j, p in enumerate(pins):
                dx, dy = apply(m, (p.x, p.y))
                i = want.get((round(origin[0] + dx), round(origin[1] + dy)))
                if i is None or i in mapping:
                    break
                mapping[i] = j
            else:
                return FitResult(name, oname, m, mapping)
    return None


def outward_dirs(fit: FitResult, decal_pins: list[DecalPin], pin_decal_dirs) -> dict[int, Vec]:
    return {i: apply(fit.matrix, local_outward(decal_pins[j], pin_decal_dirs))
            for i, j in fit.mapping.items()}


def fallback_outward(pin: Vec, all_pins: list[Vec]) -> Vec:
    """Guess outward direction from the pin's place in the pin bounding box."""
    xs = [p[0] for p in all_pins]
    ys = [p[1] for p in all_pins]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    dx, dy = pin[0] - cx, pin[1] - cy
    if abs(dx) >= abs(dy) and dx:
        return (1 if dx > 0 else -1, 0)
    if dy:
        return (0, 1 if dy > 0 else -1)
    return (-1, 0)


def snap(v: float, grid: int = 100) -> int:
    return int(round(v / grid) * grid)


def _sgnv(p: Vec, q: Vec) -> Vec:
    dx, dy = q[0] - p[0], q[1] - p[1]
    return ((dx > 0) - (dx < 0), (dy > 0) - (dy < 0))


def route(a: Vec, a_dir: Vec, b: Vec, b_dir: Vec | None = None, stub: int = 100) -> list[Vec]:
    """Orthogonal route from pin a (leaving along a_dir) to point b (entering
    along -b_dir when b is a pin).

    Several candidate shapes (straight, L, stubbed L, Z) are generated; the
    shortest one that leaves `a` outwards, enters `b` from outside and has no
    U-turns wins."""
    a1 = (a[0] + a_dir[0] * stub, a[1] + a_dir[1] * stub)
    b1 = (b[0] + b_dir[0] * stub, b[1] + b_dir[1] * stub) if b_dir else b
    mx = snap((a1[0] + b1[0]) / 2)
    my = snap((a1[1] + b1[1]) / 2)
    cands = [
        [a, b],
        [a, (b[0], a[1]), b], [a, (a[0], b[1]), b],
        [a, a1, (b1[0], a1[1]), b1, b], [a, a1, (a1[0], b1[1]), b1, b],
        [a, a1, (mx, a1[1]), (mx, b1[1]), b1, b], [a, a1, (a1[0], my), (b1[0], my), b1, b],
    ]
    best, best_score = None, None
    for strict in (True, False):
        for c in cands:
            pts = _dedupe(c)
            if len(pts) < 2 or pts[0] != a or pts[-1] != b:
                continue
            segs = [_sgnv(p, q) for p, q in zip(pts, pts[1:])]
            if any(s[0] and s[1] for s in segs):          # diagonal
                continue
            if any(s == (-t[0], -t[1]) for s, t in zip(segs, segs[1:])):  # U-turn
                continue
            if segs[0] != a_dir:
                continue
            if strict and b_dir and segs[-1] != (-b_dir[0], -b_dir[1]):
                continue
            length = sum(abs(q[0] - p[0]) + abs(q[1] - p[1]) for p, q in zip(pts, pts[1:]))
            score = length + 150 * (len(pts) - 2)
            if best_score is None or score < best_score:
                best, best_score = pts, score
        if best:
            return best
    return _dedupe([a, a1, (b1[0], a1[1]), b1, b])


def _dedupe(pts: list[Vec]) -> list[Vec]:
    """Remove repeated points and collinear middle points until stable."""
    out = [(int(p[0]), int(p[1])) for p in pts]
    changed = True
    while changed:
        changed = False
        i = 1
        while i < len(out):
            if out[i] == out[i - 1]:
                out.pop(i)
                changed = True
            else:
                i += 1
        i = 1
        while i < len(out) - 1:
            (x0, y0), (x1, y1), (x2, y2) = out[i - 1], out[i], out[i + 1]
            if (x0 == x1 == x2) or (y0 == y1 == y2):
                out.pop(i)
                changed = True
            else:
                i += 1
    return out
