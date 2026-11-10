---
name: pads-pcb-design
description: PCB placement and routing rules and pitfalls for PADS Layout. Use when placing parts on a PCB, routing, setting net classes, track widths, clearances, impedance or length-matching rules, reviewing a layout or DRC result, or working on a PCB with the pads MCP's layout_* tools.
---

# PADS PCB design rules

This document covers **what to follow** (design knowledge). The pads MCP tools cover **how to operate PADS** (mechanism).
Add company- or product-specific rules to the last section of this file. It is easier to maintain when the rules stay out of the code.

## Working principles

- Confirm with the user which `.pcb` may be edited (a copy is safest). Every pads MCP edit is backed up to
  `work\history` and can be reverted with `layout_undo`.
- Order: **rules → placement → user confirms placement → power and critical nets → the rest → user runs Verify Design → fix → user confirms**.
- All routing is done in PADS Layout with `layout_route`: you plan every trace yourself. There is no autorouter,
  so "route it automatically" also means planning the paths and drawing them in batches.
- The pads MCP never starts PADS Router and has no DRC tool. Keep clearances by planning; the final check is
  Tools > Verify Design in PADS Layout, run by the user.
- Set rules (width, clearance, net classes, impedance, length) **before routing**. If you change rules after routing, you must reroute.
- After each step, check with `pads_screenshot(app='layout')` and report the results to the user as numbers (routed / unrouted connections, vias).
- Never decide a value you cannot know (fab capability, isolation rating, mechanical constraints) on your own. Ask the user, or check the company rules section below.

## Placement

1. **Fixed positions first**: connectors, switches, LEDs, antennas, mounting holes, heat-dissipating parts. Use the mechanical drawing or the existing board as the reference.
2. **Group by function**: power, MCU, communication (ETH / RS-485 / Wi-Fi), isolated section, analog. Arrange the blocks so signals flow in a straight line.
3. **Decoupling caps (100 nF, etc.)**: place them right next to the IC power pin (≤ 2 mm), on the same side. Keep the loop pin → cap → GND via short. Put bulk caps near the regulator or power input.
4. **Crystal / oscillator**: within 5 mm of the MCU pins, with the load caps alongside. No other signals underneath, and surround it with GND.
5. **Switching regulators**: minimize the input cap–switch–inductor/diode loop. Route the feedback trace away from the SW node.
6. **Isolation boundary** (e.g. `3V3_ISO`/`SGND` ↔ `GND`): split parts onto the two sides of the boundary line. Copper, traces and parts must not cross it, except the isolation devices. **Ask the user** for the creepage/clearance distance (it depends on the isolation rating).
7. **Wi-Fi/BLE module antenna**: at the board edge. Follow the module datasheet keep-out: no copper on any layer under or around the antenna.
8. **Ethernet**: place RJ45 – magnetics (or MagJack) – PHY in a straight line. No copper in the isolation area under the magnetics.
9. Place ESD/TVS, fuses and reverse-polarity protection right next to their connector.
10. Orient parts of the same type the same way. Keep silkscreen off pads. Leave at least 0.5 mm between SMD parts.

## Routing

- **Order**: power/GND → clocks, high-speed and differential pairs → sensitive analog → general signals.
- **Track width (1 oz copper, 10 °C rise, approx. IPC-2221; double it for 0.5 oz)**:

  | Current | Outer layer | Inner layer |
  |---|---|---|
  | 0.5 A | ≥ 0.15 mm | ≥ 0.3 mm |
  | 1 A | ≥ 0.3 mm | ≥ 0.8 mm |
  | 2 A | ≥ 0.8 mm | ≥ 2.0 mm |
  | 3 A | ≥ 1.4 mm | ≥ 3.5 mm |

  Default signal width is 0.2–0.25 mm (subject to the fab minimum).
- **GND**: use wide copper (plane/pour) where possible, and don't cut the GND reference under signals. Put a GND via next to each layer-change via.
- **Differential pairs**: same layer, matched length, constant spacing, few vias. Typical targets:
  - USB 2.0: 90 Ω differential.
  - Ethernet: 100 Ω differential.
  - RS-485: the 120 Ω target is for the cable. On the board, simply route the two lines together as a pair.
- **Impedance** is set by the stackup (dielectric constant, thickness, copper thickness). Compute the width and put it in a net class rule. **Confirm the final value with the fab's impedance calculation.**
- No 90° corners and no acute angles (use 45° or arcs).
- Increase clearance between high voltage (24 V input, etc.) and signals (e.g. ≥ 0.5 mm, depending on rating).
- Keep crystal traces short, with no vias, surrounded by GND.

## How to route with the pads MCP (agent routing)

1. Start with `layout_overview`: board outline, layers, via types, rules, unrouted connections per net.
   - If rules are missing or wrong, fix them first with `layout_set_rules`: class widths and clearance.
   - If the stack-up is the PADS default (0.254 mm dielectric), ask the user for the real one before computing impedance.
2. For each area, call `layout_region(x1, y1, x2, y2)`. It returns:
   - pads: centre, `layer` (an SMD pad exists only on that layer; null = through-hole on all layers), and half extents `hx`/`hy`
   - existing tracks and vias
3. Plan the clearance arithmetic explicitly.
   - Distance from a trace centreline to a pad centre must be at least `pad half-extent + trace width / 2 + clearance`.
   - Example: 1.2 mm round pad, 0.3 mm trace, 0.2 mm clearance → ≥ 0.6 + 0.15 + 0.2 = 0.95 mm.
   - Two pins at 2.0 mm pitch with 1.2 mm pads leave a 0.8 mm gap. A 0.3 mm trace fits through the centre of that gap (0.25 mm clearance on each side).
4. Draw only 0°/45°/90° segments. Change layers with a via (`[x, y, "bottom"]`).
   - Keep vias at least clearance + via pad radius away from SMD pads. A via touching a pad edge leaves acid-trap/sliver errors (seen in testing).
5. On one layer, two nets cannot cross. Plan the order: the first route can block the second.
   - Use the other layer or go around the end of a connector row.
6. Send **many connections per `layout_route` call**. Each call re-imports the whole design (~30-60 s).
7. After each batch, check the new copper with `layout_region` (recheck the clearance arithmetic against the
   neighbouring pads, tracks and vias) and the remaining work with `layout_overview`.
8. Check visually with `pads_screenshot(app="layout", region=[...])`, then report to the user as numbers: routed / unrouted connections, vias.
9. There is no DRC tool. When routing is done, ask the user to run **Tools > Verify Design** in PADS Layout
   and share the error list (position, layer, objects).
   - Fix the errors on your new routes (re-routing a pin pair replaces it), then ask for Verify Design again.
   - Errors elsewhere usually come from placement. Report them, but don't "fix" them silently.

Suggested order on a board like this:
- power entry and regulators (wide)
- GND (traces first; later a pour if the user wants it)
- crystals and clocks
- differential/high-speed pairs (USB, Ethernet)
- short local connections
- long buses last

## PADS-specific notes (confirmed)

- **Rules live in the `*MISC*` → `RULES_SECTION` of the PADS Layout ASCII**:
  - `NET_CLASS DATA`: class names and their nets.
  - `DESIGN RULES` → `CLEARANCE_RULE`:
    - clearances: `TRACK_TO_TRACK`, `VIA_TO_TRACK`, `PAD_TO_TRACK`
    - track widths: `MIN_TRACK_WIDTH`, `REC_TRACK_WIDTH`, `MAX_TRACK_WIDTH`
  - High-speed rules: `MIN/MAX_LENGTH`, `MIN/MAX_IMPEDANCE`, `MATCH_LENGTH_TOLERANCE`.
- **PADS Layout COM cannot create traces or vias** (no such API).
  - `layout_route` therefore writes the `*ROUTE*` section of the ASCII and re-imports the design.
  - The connections of a net must be kept in tree order. The tool handles this.
- **PADS Router is not used** by the pads MCP (no routing, no DRC), so no PADS Router license is needed.
- PADS Layout COM has no Verify Design method, so the design-rule check stays a manual step in PADS Layout.
- Handle power/GND planes with Pour/Plane areas. Leave those connections unrouted for the pour unless the user wants traces.

## Company and product rules (to be filled in)

<!-- Add company rules here. These take priority over the general rules above. -->
- Fab minimum track/space: (e.g.) 0.15 / 0.15 mm
- Default via: (e.g.) drill 0.3 mm / pad 0.6 mm
- Default stackup: (e.g.) 4 layers, 1.6 mm, 1 oz outer / 0.5 oz inner
- Isolation clearance: (e.g.) ≥ 4 mm between `GND` and `SGND`
- Mechanical constraints: (e.g.) mounting hole positions, connector positions, height limits
