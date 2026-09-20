"""Manage MCP server configuration for the clients that read a config file:

* Claude *desktop* app      ->  claude_desktop_config.json
* Antigravity CLI (agy)     ->  ~/.gemini/config/mcp_config.json   (current
                                Google CLI; gemini-cli was retired for
                                individual accounts on 2026-06-18)
* Gemini CLI (legacy)       ->  ~/.gemini/settings.json
* Qwen Code (official)      ->  ~/.qwen/settings.json     (same shape)
* Codex CLI (OpenAI)        ->  ~/.codex/config.toml      (TOML, append-only)
* Claude Code (Anthropic)   ->  ~/.claude.json            ("mcpServers" key)

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
import re
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


# --------------------------------------------------------------------------- #
# Official CLI clients
# --------------------------------------------------------------------------- #
#: client key -> (settings path, human name, manager command)
#:
#: Gemini has TWO generations of CLI:
#:   * agy    — Antigravity CLI (Google's current client; the open-source
#:              gemini-cli was retired for individual accounts on 2026-06-18).
#:              It reads MCP servers from a standalone profile file.
#:   * gemini — the legacy open-source CLI (still alive for paid-API-key /
#:              enterprise installs); reads them from settings.json.
#: Codex keeps everything in a TOML file; Claude Code keeps user-scope
#: servers in the JSON "mcpServers" section of ~/.claude.json.
CLI_SETTINGS = {
    "agy": ("~/.gemini/config/mcp_config.json", "Antigravity CLI", "agy mcp"),
    "gemini": ("~/.gemini/settings.json", "Gemini CLI", "gemini mcp"),
    "qwen": ("~/.qwen/settings.json", "Qwen Code", "qwen mcp"),
    "codex": ("~/.codex/config.toml", "Codex CLI", "codex mcp"),
    "claude": ("~/.claude.json", "Claude Code", "claude mcp"),
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


def ensure_cli_mcp_servers(client: str, servers: dict, log=print) -> str | None:
    """Merge *servers* into the official CLI client's settings file.

    client = "agy"     -> ~/.gemini/config/mcp_config.json (Antigravity CLI)
    client = "gemini"  -> ~/.gemini/settings.json          (legacy Gemini CLI)
    client = "qwen"    -> ~/.qwen/settings.json            (Qwen Code)
    client = "codex"   -> ~/.codex/config.toml             (Codex CLI, TOML)
    client = "claude"  -> ~/.claude.json                   (Claude Code)
    The parent folder is created if it doesn't exist yet (the client creates
    the rest on first launch).
    """
    if not servers:
        return None
    path = cli_settings_path(client)
    if not path:
        log(f"  (no CLI settings file known for client '{client}')")
        return None
    entry = CLI_SETTINGS[client]
    note = (f"manage anytime with `{entry[2]} list`; the file is read at client "
            f"startup, so it takes effect from the next prompt")
    if client == "codex":
        return _merge_mcp_into_toml(path, servers, log=log, note=note)
    return _merge_mcp_into(path, servers, log=log, note=note)


def cli_user_mcp_servers(client: str) -> dict:
    """The MCP servers the USER has in *client*'s config file (whatever the
    tool did not add). Returns {} when the file is missing/unreadable."""
    path = cli_settings_path(client)
    if not path or not os.path.exists(path):
        return {}
    try:
        if client == "codex":
            try:
                import tomllib
                data = tomllib.loads(open(path, "r", encoding="utf-8").read())
            except ImportError:
                return {}
            ms = data.get("mcp_servers")
            return ms if isinstance(ms, dict) else {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        ms = data.get("mcpServers")
        return ms if isinstance(ms, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------- Codex ---- #
def _toml_str(v) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_server_section(name: str, spec: dict) -> list:
    """Emit a [mcp_servers.<name>] table for one stdio/HTTP server spec
    (the same JSON shape the user pastes in the UI). Keys Codex doesn't
    know (trust, type, …) are dropped."""
    key = name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else _toml_str(name)
    lines = [f"[mcp_servers.{key}]"]
    if spec.get("command"):
        lines.append(f"command = {_toml_str(spec['command'])}")
    args = spec.get("args")
    if isinstance(args, list) and args:
        lines.append("args = [" + ", ".join(_toml_str(a) for a in args) + "]")
    if spec.get("url") or spec.get("serverUrl"):
        lines.append(f"url = {_toml_str(spec.get('url') or spec.get('serverUrl'))}")
    if spec.get("cwd"):
        lines.append(f"cwd = {_toml_str(spec['cwd'])}")
    env = spec.get("env")
    if isinstance(env, dict) and env:
        lines.append("env = { " + ", ".join(
            f"{_toml_str(k)} = {_toml_str(v)}" for k, v in env.items()) + " }")
    return lines


def _merge_mcp_into_toml(path: str, servers: dict, log=print, note: str = "") -> str | None:
    """Append missing [mcp_servers.*] tables to Codex's config.toml.

    Append-only: the existing file (comments, model, profiles…) is left
    byte-for-byte untouched; a timestamped .bak copy of the original is
    kept. A server name the user already configured is never rewritten —
    their entry wins and a note is logged.
    """
    existing: dict = {}
    text = ""
    if os.path.exists(path):
        text = open(path, "r", encoding="utf-8").read()
        try:
            import tomllib
            data = tomllib.loads(text)
            ms = data.get("mcp_servers")
            if isinstance(ms, dict):
                existing = ms
        except Exception as e:
            log(f"  (!) could not parse existing {os.path.basename(path)} ({e}) — leaving it untouched.")
            return None

    sections, added = [], []
    for name, spec in servers.items():
        if name in existing:
            if existing[name] != {k: v for k, v in spec.items()
                                  if k in ("command", "args", "url", "cwd", "env")}:
                log(f"  (MCP server '{name}' already exists in "
                    f"{os.path.basename(path)} — keeping YOUR entry, not this run's.)")
            continue
        sections.append(_toml_server_section(name, spec))
        added.append(name)
    if not added:
        log(f"  (MCP config already up to date: {', '.join(servers)})")
        return path

    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path):
            shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        with open(path, "a", encoding="utf-8") as f:
            if text and not text.endswith("\n"):
                f.write("\n")
            f.write("\n# --- subscription-sweater (managed per run) ---\n")
            for sec in sections:
                f.write("\n".join(sec) + "\n")
    except Exception as e:
        log(f"  (!) could not write MCP config {os.path.basename(path)}: {e}")
        return None

    log(f"  MCP servers written to {os.path.basename(path)}: {', '.join(added)}")
    if note:
        log(f"  ({note})")
    return path


# Paths already backed up in this process (per-prompt scope writes only need
# one backup of the original — the first merge already made one).
_SCOPED_PATHS: set = set()


def set_cli_mcp_scope(client: str, run_servers: dict, enabled, log=print) -> None:
    """Per-prompt MCP scoping for clients WITHOUT an allow-list flag.

    The Antigravity CLI (agy) has no `--allowed-mcp-server-names` equivalent,
    so the run's servers are toggled in the user-level mcp_config.json right
    before each launch (every `agy -p` is a fresh process that reads the
    file at startup): the servers the policy turned ON for this prompt are
    present; all other run servers are absent. Servers the user configured
    outside this run are preserved untouched, in both directions.
    """
    if client != "agy":
        return
    run_servers = run_servers or {}
    if not run_servers:
        return
    path = cli_settings_path("agy")
    if not path:
        return
    cfg: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            return
    if not isinstance(cfg, dict):
        return
    ms = cfg.get("mcpServers")
    if ms is None:
        ms = cfg["mcpServers"] = {}
    if not isinstance(ms, dict):
        return
    enabled = set(enabled or ())
    changed = False
    for name, spec in run_servers.items():
        if name in enabled:
            if ms.get(name) != spec:
                ms[name] = spec
                changed = True
        elif name in ms:
            del ms[name]
            changed = True
    if not changed:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if path not in _SCOPED_PATHS and os.path.exists(path):
            shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        _SCOPED_PATHS.add(path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log(f"  (!) could not write the per-prompt MCP scope: {e}")
