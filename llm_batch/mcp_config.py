"""Manage MCP servers in the Claude *desktop* app's config file.

Claude desktop reads MCP servers from `claude_desktop_config.json`
(`mcpServers` — the same shape you use in the app's Developer > MCP menu).
This module merges the servers the client configured in the tool into that
file (backing up the original first) so ANY MCP server can be connected
before a desktop batch run.

New servers may need a one-time approval in the app (Connectors menu) —
the run log tells the user when that is likely.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time


def claude_desktop_config_path() -> str | None:
    """Path of claude_desktop_config.json, or None if Claude isn't installed."""
    if sys.platform == "darwin":
        p = os.path.expanduser(
            "~/Library/Application Support/Claude/claude_desktop_config.json")
    elif sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA", "")
        p = os.path.join(appdata, "Claude", "claude_desktop_config.json")
    else:
        p = os.path.expanduser("~/.config/Claude/claude_desktop_config.json")
    d = os.path.dirname(p)
    if d and os.path.isdir(d):
        return p
    return None


def ensure_mcp_servers(servers: dict, log=print) -> str | None:
    """Merge *servers* {name: spec} into Claude desktop's config.

    Existing user entries are never removed/overwritten unless identical;
    a timestamped .bak copy of the original file is kept.
    Returns the config path, or None if Claude desktop wasn't found.
    """
    if not servers:
        return None
    path = claude_desktop_config_path()
    if not path:
        log("  (Claude desktop config not found — MCP setup skipped. "
            "Open Claude once to create it, or use web/browser mode.)")
        return None

    cfg: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            log(f"  (!) could not parse existing Claude config ({e}) — leaving it untouched.")
            return None

    ms = cfg.setdefault("mcpServers", {})
    changed, added = False, []
    for name, spec in servers.items():
        if ms.get(name) != spec:
            if name not in ms:
                added.append(name)
            ms[name] = spec
            changed = True

    if not changed:
        log(f"  (MCP config already up to date: {', '.join(servers)})")
        return path

    try:
        shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    except Exception:
        pass
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log(f"  (!) could not write Claude MCP config: {e}")
        return None

    log(f"  MCP servers written to Claude desktop config: {', '.join(servers)}")
    if added:
        log("  (new servers may need a one-time approval in Claude: "
            "profile picture > Connectors > refresh)")
    return path
