# pads-mcp

[한국어](README.ko.md)

An MCP server that lets an AI assistant (Claude Code, Codex, GitHub Copilot) drive **PADS Logic (schematics)** and
**PADS Layout / PADS Router (PCB)** 9.x through COM automation.

- **Schematics**: place parts, draw wires, attach power/GND symbols and net labels.
- **Agent PCB routing**: the AI agent plans each trace (layer, corners, vias, width) from pad and obstacle data;
  the server writes it into the design exactly as planned. All routing happens in PADS Layout; there is no autorouter.
  An optional DRC uses the PADS Router checker (needs a PADS Router license).
- **Rules and impedance**: net classes, width/clearance/high-speed rules, layer stack-up, and trace width for a
  target impedance.

## How it works

```
AI client ⇄ pads-mcp (Python, stdio) ⇄ COM ⇄ PADS Logic / PADS Layout / PADS Router
```

| Task | Method |
|---|---|
| Queries (parts, pins, pads, nets, rules, stack-up) | COM API (`PowerLogic.Application`, `PowerPCB.Application`) |
| Placing library parts, moving, rotating, attributes | COM API |
| **Schematic wires, power/GND symbols, net labels, copying parts** | No COM API → **ASCII round trip** (export → edit → import into a blank design → save) |
| **PCB traces and vias, net classes, rules, stack-up** | No COM API → the same **ASCII round trip** for PADS Layout |
| PCB design-rule check (optional, needs a PADS Router license) | PADS Router COM (`BlazeRouter.Application`) on a copy of the saved PCB |
| Visual check | Window capture (`pads_screenshot`) |

## Agent routing workflow (PCB)

1. `layout_overview` → board outline, layers, stack-up, via types, net classes/rules, unrouted connections per net.
2. `layout_get_net` / `layout_region` → pins with pad layer and size, existing tracks and vias around the path.
3. The model plans the path and calls `layout_route` with **several connections at once**:
   ```json
   {"routes": [
     {"from": "J1.3", "to": "R3.1", "layer": "bottom",
      "path": [[31.0, 144.8], [31.0, 139.8], [35.8, 139.8]]},
     {"from": "J1.2", "to": "R2.1", "layer": "top",
      "path": [[33.6, 146.8], [33.6, 142.8, "bottom"]]}
   ]}
   ```
   - mm, y up.
   - The pad centres are added automatically.
   - `[x, y, "bottom"]` places a via there and continues on that layer.
   - `width` defaults to the net class `REC_TRACK_WIDTH`.
4. Check the new copper with `layout_region` and visually with `pads_screenshot(app="layout", region=[x1, y1, x2, y2])`, then route the next batch.
5. When routing is done, run `layout_drc` once (PADS Router checker) and fix the errors on the new traces.
   Without a PADS Router license, use Tools > Verify Design in PADS Layout instead.

Each PCB edit re-imports the whole design. That takes about 30–60 s on a 160-part board, so batch your routes.

## Safety

- `logic_open` **copies** the schematic into `work\` and opens the copy (the original is never touched).
- PCB edits are made on the PCB that is open in PADS Layout.
- Every edit (schematic or PCB) is an **atomic transaction**.
  - The file is backed up to `work\history\` first.
  - If PADS rejects or changes anything (for example, the part or net count changes), nothing is saved and the original is reopened.
  - `logic_undo` / `layout_undo` restore the last backup.
- DRC runs on a **copy** opened without a file lock, so it never touches your file directly.
- The `Font Replacement` dialog during ASCII import is answered automatically. Other dialogs can be handled with
  `pads_dialogs` / `pads_click_dialog`.
- Only one AI client should drive PADS at a time (COM attaches to the running PADS instance).

## Tools

**Common**: `pads_status`, `pads_screenshot`, `pads_dialogs`, `pads_click_dialog`

**PADS Logic (schematic, mils)**
- Files: `logic_open`, `logic_new`, `logic_save`, `logic_export` (ascii/netlist), `logic_import_ascii`, `logic_undo`
- Queries: `logic_status`, `logic_list_parts`, `logic_get_part`, `logic_list_nets`, `logic_get_net`, `logic_search_library`
- Parts: `logic_add_sheet`, `logic_add_part`, `logic_move_part`, `logic_delete_part`, `logic_rename_part`, `logic_set_attribute`
- **Drawing**: `logic_draw` (parts + nets in one call), `logic_wire` (connect / power / label / disconnect / net)

**PADS Layout (PCB, mm)**
- Files: `layout_status`, `layout_open`, `layout_save`, `layout_export` (ascii/netlist/rules), `layout_undo`
- Queries: `layout_overview`, `layout_list_components`, `layout_get_component`, `layout_list_nets`, `layout_get_net`, `layout_region`
- **Routing**: `layout_route`, `layout_unroute`, `layout_drc` (PADS Router checker, optional)
- Rules: `layout_get_rules`, `layout_set_rules`, `layout_stackup`, `layout_set_stackup`, `layout_impedance`
- Placement: `layout_move_component`, `layout_select`

### logic_draw example

```json
{
  "sheet": "POWER2",
  "parts": [
    {"refdes": "U100", "clone_from": "U6", "x": 3000, "y": 6000},
    {"refdes": "C100", "clone_from": "C3", "x": 1900, "y": 5900, "rotate": 270,
     "attributes": {"Value": "CC2012/10u/K"}}
  ],
  "nets": [
    {"name": "5V",  "pins": ["U100.VIN", "C100.1"], "style": "wire"},
    {"name": "GND", "pins": ["U100.GND", "C100.2"], "style": "power"}
  ]
}
```
- `clone_from`: copies a part that is already in the design. No library is needed, and attributes can be overridden.
- Without `clone_from`: places `part_type` from the PADS library.
- `style`: `wire` | `label` | `power` | `auto`.

### layout_set_rules example

```json
{
  "net_classes": {"USB": ["USB_DP", "USB_DM"]},
  "rules": [
    {"target": "DEFAULT", "auto_route": true, "width": {"min": 0.2, "rec": 0.25, "max": 1.0}, "clearance": 0.2},
    {"target": "USB", "width": 0.3, "clearance": 0.2, "match_tolerance": 0.5, "impedance_min": 81, "impedance_max": 99}
  ]
}
```

## Installation (team members)

### 0. Prerequisites (once per PC)

1. **PADS 9.x**, with PADS Logic / PADS Layout launched once (this registers COM).
2. **uv** (Python launcher; it also installs Python):
   ```powershell
   winget install --id=astral-sh.uv -e
   ```
3. **Git**. For a private repository, sign in once so Git can read it:
   ```powershell
   gh auth login
   gh auth setup-git
   ```
4. Check the installation. The first run downloads the dependencies and takes 10–30 s:
   ```powershell
   uvx --from git+https://github.com/BlessingQ/pads-mcp@latest pads-mcp --check
   ```
   You should see `OK` for PADS Logic / Layout, then `ready`. PADS Router is needed only for `layout_drc`.

`uvx` runs the server from an isolated environment in the uv cache (`%LOCALAPPDATA%\uv\cache`) that holds Python and all dependencies.
Nothing is installed, and there is no venv or pip step. `uv cache clean` deletes the environment, and the next start rebuilds it.

**Versions.** `@latest` is a branch that always points at the newest release.
`uvx` checks GitHub every time the client starts the server, so a new release is used after the next client restart, with no change to the configuration.
To stay on one release, use its tag instead of `latest`, e.g. `@v0.2.0`.
Either way, the server can only start when GitHub is reachable.
To see the version in use, run the check command with `--version` instead of `--check`.

### Claude Code

**Option A (recommended): plugin.** Installs the MCP server together with the PCB design-rule skill (`skills/`).
```text
/plugin marketplace add BlessingQ/pads-mcp
/plugin install pads@pads-mcp
```
From a shell, use `claude plugin marketplace add BlessingQ/pads-mcp` then `claude plugin install pads@pads-mcp`.
The repository is itself the marketplace (`.claude-plugin/marketplace.json`), so nothing has to be published separately.

**Option B: MCP server only.**
```powershell
claude mcp add --scope user --transport stdio pads -- uvx --from git+https://github.com/BlessingQ/pads-mcp@latest pads-mcp
```
For the skill, copy `skills\pads-pcb-design` to `%USERPROFILE%\.claude\skills\`.
Check with `claude mcp list` (`pads ... ✔ Connected`) or `/mcp` in a session.

### OpenAI Codex (CLI / IDE extension / app)

Add the server to `%USERPROFILE%\.codex\config.toml`:
```toml
[mcp_servers.pads]
command = "uvx"
args = ["--from", "git+https://github.com/BlessingQ/pads-mcp@latest", "pads-mcp"]
startup_timeout_sec = 120   # default 10 s is too short for the first uvx download
tool_timeout_sec = 600      # default 60 s; a PCB edit + DRC can take a minute or more
```
`codex mcp add pads -- uvx --from git+https://github.com/BlessingQ/pads-mcp@latest pads-mcp` also works.
It has no timeout options, so add the two timeout lines to `config.toml` afterwards.
- Skill: copy `skills\pads-pcb-design` to `%USERPROFILE%\.agents\skills\` (user) or `.agents\skills\` in a repository.
- Check with `codex mcp list` or `/mcp` in a session.

### VS Code + GitHub Copilot

Use either of these configuration files.
- **Portable Copilot config** (current default of the "Add Server" flow): `%USERPROFILE%\.copilot\mcp-config.json`
  ```json
  { "mcpServers": { "pads": { "command": "uvx",
      "args": ["--from", "git+https://github.com/BlessingQ/pads-mcp@latest", "pads-mcp"] } } }
  ```
- **VS Code format**: Command Palette → **MCP: Open User Configuration**, or the workspace `.vscode/mcp.json`:
  ```json
  { "servers": { "pads": { "type": "stdio", "command": "uvx",
      "args": ["--from", "git+https://github.com/BlessingQ/pads-mcp@latest", "pads-mcp"] } } }
  ```

Then:
- Use the tools from Copilot Chat with the **Agent** agent, and check that `pads` is enabled under Tools.
- Skill: copy `skills\pads-pcb-design` to `%USERPROFILE%\.copilot\skills\` or the workspace `.github\skills\`.
- Copilot Business/Enterprise: an organization admin must enable the **MCP servers in Copilot** policy. It is disabled by default.

### Settings

| Environment variable | Meaning |
|---|---|
| `PADS_MCP_WORKDIR` | Work folder for schematic copies, temporary ASCII files and backups. Default: `<repo>\work` when run from a source checkout, otherwise `%USERPROFILE%\Documents\pads-mcp\work`. |

### Uninstall

Remove the entry for each client you set up, then restart that client.

| Client | Remove the server | Remove the skill |
|---|---|---|
| Claude Code (plugin) | `claude plugin uninstall pads@pads-mcp`, then `claude plugin marketplace remove pads-mcp` | Removed with the plugin |
| Claude Code (MCP server only) | `claude mcp remove pads --scope user` | Delete `%USERPROFILE%\.claude\skills\pads-pcb-design` |
| OpenAI Codex | `codex mcp remove pads`, or delete the `[mcp_servers.pads]` block from `%USERPROFILE%\.codex\config.toml` | Delete `pads-pcb-design` from `%USERPROFILE%\.agents\skills\` or the repository's `.agents\skills\` |
| VS Code + Copilot | Delete the `pads` entry from `mcp-config.json` or `mcp.json` | Delete `pads-pcb-design` from `%USERPROFILE%\.copilot\skills\` or `.github\skills\` |

Optional cleanup:
- `uv cache clean pads-mcp` deletes the cached builds. They only take disk space, and `uvx` rebuilds them when needed.
- No command above deletes the work folder (`%USERPROFILE%\Documents\pads-mcp\work`, or `PADS_MCP_WORKDIR`).
  It holds schematic copies and edit backups, so delete it yourself once you no longer need them.

## Where design knowledge lives (MCP vs skill)

| Content | Location | Why |
|---|---|---|
| How to operate PADS, and PADS rules that must hold (one wire per pin, SMD pads on their own layer, …) | MCP code + server instructions | Needed for correct behavior; changes together with the tools |
| Placement/routing rules, cautions, company standards (fab limits, stack-up, isolation distances) | **Skill** `skills/pads-pcb-design/SKILL.md` | Changes per company and product; editable markdown; loaded only when relevant |

The Claude Code plugin installs both. Codex and Copilot use the same `SKILL.md` by copying it.

## Releasing (maintainers)

1. Bump the version in `pyproject.toml`, `src/pads_mcp/__init__.py`, `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`.
2. Commit, then `git tag vX.Y.Z && git push && git push --tags`.
3. Move `latest` to the release with `git push origin main:latest`. Users on `@latest` get it at their next client restart.
   Do not push unreleased commits to `latest`; the install snippets never need editing.

- `work\` (design copies, backups) and design files (`*.sch`, `*.pcb`, `*.asc`) are excluded by `.gitignore`.

## Development

```powershell
git clone https://github.com/BlessingQ/pads-mcp; cd pads-mcp
uv venv .venv
uv pip install --python .venv\Scripts\python.exe -e .
claude mcp add --scope user pads -- "$PWD\.venv\Scripts\python.exe" -m pads_mcp.server
```
- Test a uvx install from the local source with `uvx --from . pads-mcp --check`.
- Test the plugin without installing it with `claude --plugin-dir . mcp list`.

| `tools\` file | Purpose |
|---|---|
| `mcp_call.py` | Call tools over the real MCP stdio protocol (end-to-end tests) |
| `typelib_dump.py` | List a COM server's methods without starting it (`typelib_dump.py BlazeRouter.Application`) |
| `validate_geometry.py`, `check_roundtrip.py`, `test_route.py` | Schematic parser / geometry / router tests |
| `sheet_dump.py`, `pin_degree.py`, `list_windows.py` | Diagnostics |

## PADS facts this relies on (verified on PADS 9.3)

**Schematic (PADS Logic ASCII)**
- A pin takes only one wire; branches go through a tie dot (3 wires).
- Pin decals must be defined before the CAE decals that use them.
- An empty `*PART*` section starts with 3 blank lines.

**PCB (PADS Layout ASCII)**
- `*ROUTE*` stores each net as 2-pin connections (`PINA PINB` + points `X Y LAYER WIDTH FLAGS [VIA]`).
  - Layer 0 means unrouted and 65 marks the end point.
  - A via point carries the layer after the via.
- A net's connections must be listed in **tree order**. Otherwise PADS reports "Connection not physically tied to the signal tree" and stops reading.
- Importing a route-only ASCII into an open design is rejected ("duplicate connection"), so edits re-import the whole design into a blank one.
- Rules live in `*MISC*` → `RULES_SECTION`.
  - `NET_CLASS DATA`
  - `RULE_SET` with `CLEARANCE_RULE` / `ROUTE_RULE` / `HIGH_SPEED_RULE`
  - An absent `AUTO_ROUTE` means N.
- PADS Layout COM cannot create traces or vias.
- PADS Router COM can route (`Router.Run`, `Route()` for the selection, `LCTune`) and check (`Checker`), but cannot set rules.
  pads-mcp never routes with it; only `layout_drc` starts PADS Router, which needs a PADS Router license (`blzshell`).

## Known limitations

- PCB edits take 30–60 s each (whole-design re-import), so batch connections in one `layout_route` call.
- Routes are point-to-point between pins. T-junctions in the middle of a trace and copper pours are not supported yet.
- Impedance uses closed-form formulas (±5–10 %). Confirm with the fab.
- Schematic: net label text can sit slightly off the symbol, and `logic_move_part` does not reroute attached wires.

## License

[MIT](LICENSE)
