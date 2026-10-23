#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate references/tools.md from ai_contract.py.

This script only reads the contract module and writes the generated catalog
next to the skill. It never touches the running app and never uses the network.

Usage (from the skill root):
    python scripts/gen_skill_refs.py
    python scripts/gen_skill_refs.py --check   # exit 1 when the file is stale
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SKILL_ROOT.parents[1]
OUT_PATH = SKILL_ROOT / "references" / "tools.md"

TOOLSET_ORDER = ("core", "vision", "admin")


def load_contract():
    """Import ai_contract from the project root without requiring installation."""
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    import ai_contract  # noqa: PLC0415 - deliberate local import after sys.path setup

    return ai_contract


def _esc(value) -> str:
    """Escape a value for a Markdown table cell."""
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def _code(value) -> str:
    return "`%s`" % _esc(value)


def _json_value(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _type_name(schema) -> str:
    if not isinstance(schema, dict):
        return "any"
    kind = schema.get("type") or "any"
    if kind == "array":
        return "array<%s>" % _type_name(schema.get("items") or {})
    return str(kind)


def _safety(annotations) -> str:
    ann = annotations or {}
    labels = ["read" if ann.get("readOnlyHint") else "write"]
    if ann.get("destructiveHint"):
        labels.append("destructive")
    if ann.get("idempotentHint"):
        labels.append("idem")
    labels.append("net" if ann.get("openWorldHint") else "local")
    return " · ".join(labels)


def _returns(value) -> str:
    return "mixed" if value == "mixed" else "text"


def _param_notes(schema) -> str:
    if not isinstance(schema, dict):
        return ""
    bits = []
    desc = str(schema.get("description") or "").strip()
    if desc:
        bits.append(desc.rstrip("。.").rstrip())
    enum = schema.get("enum")
    if not enum and schema.get("type") == "array":
        enum = (schema.get("items") or {}).get("enum")
    if enum:
        bits.append("enum: " + ", ".join(_json_value(v) for v in enum))
    if "default" in schema:
        bits.append("default: " + _json_value(schema["default"]))
    if "minimum" in schema:
        bits.append("min: " + _json_value(schema["minimum"]))
    if "maximum" in schema:
        bits.append("max: " + _json_value(schema["maximum"]))
    if "minLength" in schema:
        bits.append("minLength: " + _json_value(schema["minLength"]))
    if "maxLength" in schema:
        bits.append("maxLength: " + _json_value(schema["maxLength"]))
    if schema.get("type") == "object" and not (schema.get("properties") or {}):
        bits.append("free-form object")
    return "; ".join(bits)


def _render_tool(tool) -> list[str]:
    lines = []
    lines.append("### `%s`" % tool["name"])
    lines.append("")
    lines.append(str(tool.get("description") or "").strip())
    lines.append("")
    lines.append("- Safety: %s" % _safety(tool.get("annotations")))
    lines.append("- Returns: %s" % _returns(tool.get("returns")))
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if not props:
        lines.append("- Parameters: none")
        lines.append("")
        return lines

    lines.append("")
    lines.append("| Parameter | Type | Required | Notes |")
    lines.append("| --- | --- | --- | --- |")
    for name, prop in props.items():
        lines.append(
            "| %s | %s | %s | %s |"
            % (
                _code(name),
                _esc(_type_name(prop)),
                "yes" if name in required else "no",
                _esc(_param_notes(prop)) or "—",
            )
        )
    lines.append("")
    return lines


def render(contract) -> str:
    tools = contract.TOOLS
    counts = {name: len(contract.TOOLSETS.get(name) or []) for name in TOOLSET_ORDER}
    profiles = ", ".join(
        "%s=%s" % (name, "+".join(sets)) for name, sets in (contract.PROFILES or {}).items()
    )
    lines = [
        "# CivitaiFreeTool tool reference",
        "",
        "<!-- GENERATED FILE. Do not edit by hand. -->",
        "<!-- Source: ai_contract.py; regenerate with: python scripts/gen_skill_refs.py -->",
        "",
        "This is the catalog shared by the MCP sidecar and the HTTP automation API. "
        "Tool names and argument names are identical on both transports.",
        "",
        "- Contract API: `%s`" % contract.API_VERSION,
        "- MCP server: `%s` `%s`" % (contract.MCP_SERVER_NAME, contract.MCP_SERVER_VERSION),
        "- Minimum app version: `%s`" % contract.MIN_APP_VERSION,
        "- Tools: **%d** (%s)"
        % (len(tools), ", ".join("%s %d" % (n, counts[n]) for n in TOOLSET_ORDER)),
        "- Toolsets are opt-in: `core` is the default; multimodal hosts add `vision`; "
        "`admin` is explicit only.",
        "- Profiles: %s." % profiles,
        "- `additionalProperties: false` on every schema: unknown arguments are rejected.",
        "",
        "## Safety labels",
        "",
        "- `read` / `write`: `readOnlyHint` true/false.",
        "- `destructive`: `destructiveHint` true. Requires explicit user confirmation and, "
        "where supported, a preview/dry-run.",
        "- `idem`: `idempotentHint` true.",
        "- `net` / `local`: `openWorldHint` true/false (network access expected).",
        "- `returns`: `text` = text only; `mixed` = text plus optional MCP image content "
        "(`delivery=inline`). `delivery=paths` returns local paths only, never inline bytes.",
        "",
    ]

    # Quick index
    lines.append("## Quick index")
    lines.append("")
    lines.append("| Tool | Toolset | Safety | Returns |")
    lines.append("| --- | --- | --- | --- |")
    for toolset in TOOLSET_ORDER:
        for name in contract.TOOLSETS.get(toolset) or []:
            tool = tools[name]
            lines.append(
                "| `%s` | %s | %s | %s |"
                % (name, toolset, _safety(tool.get("annotations")), _returns(tool.get("returns")))
            )
    lines.append("")

    for toolset in TOOLSET_ORDER:
        names = contract.TOOLSETS.get(toolset) or []
        lines.append("## %s (%d tools)" % (toolset, len(names)))
        lines.append("")
        if toolset == "core":
            lines.append("Text-only tools (no image bytes). A text-only host can complete the "
                         "download, verify, repair, library and gallery-metadata workflows with these.")
        elif toolset == "vision":
            lines.append("Image-aware tools. These return image content with `delivery=inline` "
                         "or paths with `delivery=paths`. Text-only hosts should use the core "
                         "`cft_describe_model` / `cft_browse_gallery` instead.")
        else:
            lines.append("Administrative tools. Enable explicitly; never default to `admin`.")
        lines.append("")
        for name in names:
            lines.extend(_render_tool(tools[name]))

    lines.append("## Limits")
    lines.append("")
    for key, value in (getattr(contract, "LIMITS", {}) or {}).items():
        lines.append("- `%s`: `%s`" % (key, value))
    lines.append("")
    lines.append("## Error codes")
    lines.append("")
    lines.append("| Code | HTTP | Retryable |")
    lines.append("| --- | --- | --- |")
    for code, spec in (getattr(contract, "ERROR_CODES", {}) or {}).items():
        lines.append("| `%s` | %s | %s |" % (code, spec.get("status", ""), "yes" if spec.get("retryable") else "no"))
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate references/tools.md from ai_contract.py")
    parser.add_argument("--check", action="store_true", help="only check that tools.md is current")
    args = parser.parse_args()

    contract = load_contract()
    text = render(contract)
    if args.check:
        if OUT_PATH.exists() and OUT_PATH.read_text(encoding="utf-8") == text:
            print("OK: %s is current" % OUT_PATH)
            return 0
        print("STALE: run python scripts/gen_skill_refs.py", file=sys.stderr)
        return 1

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print("wrote %s (%d lines, %d tools)" % (OUT_PATH, len(text.splitlines()), len(contract.TOOLS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
