"""Design guides served by the pads_guide tool.

The guide text ships inside the package (built from skills/*/SKILL.md, see pyproject.toml), so the
guide always has the same version as the tools. Company rules come from a local Markdown file that
updates never overwrite.
"""
from __future__ import annotations

import os

from . import __version__

HERE = os.path.dirname(os.path.abspath(__file__))
# topic -> (path inside the installed package, path in a source checkout)
GUIDES = {"pcb": ("guides/pcb.md", "../../skills/pads-pcb-design/SKILL.md")}
RULES_FILE = os.path.abspath(os.environ.get("PADS_MCP_RULES")
                             or os.path.join(os.path.expanduser("~"), "Documents", "pads-mcp", "rules.md"))


def _read_text(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "cp949"):          # Notepad may still save the company file as ANSI
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def _guide_path(topic: str) -> str:
    for rel in GUIDES[topic]:
        p = os.path.normpath(os.path.join(HERE, rel))
        if os.path.isfile(p):
            return p
    raise FileNotFoundError(f"guide '{topic}' is missing from this pads-mcp installation")


def _body(text: str) -> str:
    """Drop the skill's YAML front matter."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip()
    return text


def rules_file_status() -> dict:
    return {"path": RULES_FILE, "exists": os.path.isfile(RULES_FILE)}


def guide(topic: str = "pcb") -> str:
    if topic not in GUIDES:
        raise ValueError(f"unknown topic {topic!r}; available: {list(GUIDES)}")
    out = [f"<!-- pads-mcp {__version__}, guide '{topic}' -->", _body(_read_text(_guide_path(topic))).rstrip(), "",
           "## Company rules in effect", ""]
    if os.path.isfile(RULES_FILE):
        rules = _read_text(RULES_FILE).strip()
        out += [f"From `{RULES_FILE}`. They take priority over the general rules above.", "",
                rules or "(the file is empty)"]
    else:
        out += [f"None: there is no `{RULES_FILE}`.",
                "Ask the user for the values listed under 'Company and product rules' before you rely on them.",
                "The user can write them into that file (Markdown) so every later session gets them."]
    return "\n".join(out) + "\n"
