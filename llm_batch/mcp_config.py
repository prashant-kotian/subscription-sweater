"""Manage MCP server configuration for the clients that read a JSON file:

* Claude *desktop* app  ->  claude_desktop_config.json
* Gemini CLI (official) ->  ~/.gemini/settings.json   ("mcpServers" + "mcp" keys)
* Qwen Code (official)  ->  ~/.qwen/settings.json     (same shape)

Gemini and Qwen have no MCP button in their consumer web products, but their
official open-source CLI clients natively speak MCP: they read an
`mcpServers` object (command/args/env for stdio, or httpUrl/headers for
remote) from their settings file — the same JSON shape this tool already
collects in the "MCP servers" box. This module merges the configured servers
into the right file (backing up the original first) before a batch run, so
ANY MCP server can be connected per run and scoped per prompt by the
engine's allow-list.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time


# ---------------------------------------------------------------- Claude ---- #
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


# ------------------------------------------------- official CLI clients ----- #
#: site key -> (settings path, human name, manager command)
CLI_SETTINGS = {
    "gemini": ("~/.gemini/settings.json", "Gemini CLI", "gemini mcp"),
    "qwen": ("~/.qwen/settings.json", "Qwen Code", "qwen mcp"),
}


def cli_settings_path(site: str) -> str | None:
    """Settings file the CLI client for *site* reads MCP servers from."""
    entry = CLI_SETTINGS.get(site)
    if not entry:
        return None
    # expanduser() only swaps "~" for the native-separator home dir and leaves
    # the rest of the literal ("/.gemini/settings.json") as forward slashes --
    # on Windows this gives a real, silently-mixed-separator path (still
    # openable, since Windows accepts either separator, but not equal as a
    # string to a path built via os.path.join elsewhere, e.g. this module's
    # own tests). normpath() makes it consistently native.
    return os.path.normpath(os.path.expanduser(entry[0]))


def _merge_mcp_into(path: str, servers: dict, log=print, note: str = "") -> str | None:
    """Merge *servers* {name: spec} into the `mcpServers` of JSON file *path*.

    Existing user entries are never removed or overwritten unless identical;
    a timestamped .bak copy of the original file is kept.
    Returns the path, or None on failure.
    """
    cfg: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            log(f"  (!) could not parse existing config {os.path.basename(path)} ({e}) — leaving it untouched.")
            return None
    if not isinstance(cfg, dict):
        log(f"  (!) config {os.path.basename(path)} is not a JSON object — leaving it untouched.")
        return None

    ms = cfg.setdefault("mcpServers", {})
    if not isinstance(ms, dict):
        ms = cfg["mcpServers"] = {}
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
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    except Exception:
        pass
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log(f"  (!) could not write MCP config {os.path.basename(path)}: {e}")
        return None

    log(f"  MCP servers written to {os.path.basename(path)}: {', '.join(servers)}")
    if note:
        log(f"  ({note})")
    return path


def ensure_mcp_servers(servers: dict, log=print) -> str | None:
    """Merge *servers* into Claude desktop's config (desktop-app mode)."""
    if not servers:
        return None
    path = claude_desktop_config_path()
    if not path:
        log("  (Claude desktop config not found — MCP setup skipped. "
            "Open Claude once to create it, or use web/browser mode.)")
        return None
    return _merge_mcp_into(
        path, servers, log=log,
        note="new servers may need a one-time approval in Claude: "
             "profile picture > Connectors > refresh")


def ensure_cli_mcp_servers(site: str, servers: dict, log=print) -> str | None:
    """Merge *servers* into the official CLI client's settings file.

    site = "gemini" -> ~/.gemini/settings.json (Gemini CLI)
    site = "qwen"   -> ~/.qwen/settings.json   (Qwen Code)
    The parent folder is created if it doesn't exist yet (the client creates
    the rest on first launch).
    """
    if not servers:
        return None
    path = cli_settings_path(site)
    if not path:
        log(f"  (no CLI settings file known for site '{site}')")
        return None
    entry = CLI_SETTINGS[site]
    return _merge_mcp_into(
        path, servers, log=log,
        note=f"manage anytime with `{entry[2]} list`; the file is read at client "
             f"startup, so it takes effect from the next prompt")
