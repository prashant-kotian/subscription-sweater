"""Persist user settings to settings.json next to the app."""
from __future__ import annotations

import json
import os

SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json"
)

DEFAULTS = {
    # mode: browser | desktop | manual | mock
    "mode": "browser",
    "site": "chatgpt",            # chatgpt | claude | gemini | custom
    "custom_url": "",
    "input_path": "",
    "output_path": "",
    "output_format": "txt",       # txt | xlsx | docx (used for the auto output name)
    "headless": False,
    "txt_mode": "auto",           # auto | blank-line | line
    # desktop mode
    "desktop_exe": "",            # empty = auto-detect default install path
    "desktop_port": 9222,
    "desktop_attach_only": False,
    # manual mode
    "focus_window": "",           # window title substring to auto-focus
    # timing
    "delay_between": 2.0,
    "stable_seconds": 6.0,
    "max_wait_seconds": 300.0,
    "login_timeout": 300.0,
    "new_chat": True,
    "start_index": 0,
    "limit": 0,
    "auto_send": True,
    "timer_seconds": 0.0,
    "debug": False,
    # tool policy: web search / MCP / skills / model
    "web_search": "auto",         # auto | always | never
    "mcp": "never",               # never | auto | always
    "model": "",                  # "GPT-4o", "Opus", "Qwen3-Max"… ("" = app default)
    "allowed_mcp": "",            # comma list (empty = all configured)
    "denied": "",                 # "websearch, mcp, mcp:github, skill:pdf"
    "skills": "",                 # comma list
    "inject_instructions": True,
    "use_directives": True,
    "mcp_servers_json": "",       # JSON text: {"name": {"command":..., "args":..., "env":...}}
}


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data.update(json.load(f))
    except Exception:
        pass
    return data


def save(data: dict) -> None:
    merged = dict(DEFAULTS)
    merged.update(data or {})
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
    except Exception:
        pass
