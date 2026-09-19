"""Tkinter GUI for the Subscription Sweater."""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import config as cfgmod
from . import readers
from .engine import Engine, RunConfig
from .errors import Stopped, ToolError

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INPUT_TYPES = [
    ("Supported", "*.txt *.csv *.xlsx *.xls *.docx *.doc"),
    ("Text", "*.txt"),
    ("Excel", "*.xlsx *.xls"),
    ("Word", "*.docx *.doc"),
    ("All files", "*.*"),
]
OUTPUT_TYPES = [
    ("Text", "*.txt"),
    ("Excel", "*.xlsx"),
    ("Word", "*.docx"),
    ("All files", "*.*"),
]

MODES = [
    "Browser automation (ChatGPT / Claude / Gemini)",
    "Desktop app automation (ChatGPT / Claude desktop)",
    "Manual paste mode (any LLM app, desktop or web)",
    "Mock mode (offline self-test)",
]
MODE_KEYS = ["browser", "desktop", "manual", "mock"]
SITES = ["ChatGPT", "Claude", "Gemini", "Qwen", "Custom URL…"]
SITE_KEYS = ["chatgpt", "claude", "gemini", "qwen", "custom"]
FMTS = ["txt", "xlsx", "docx"]
WEB_POLICIES = ["Auto (detect from prompt text)", "Always on", "Never"]
WEB_POLICY_KEYS = ["auto", "always", "never"]
MCP_POLICIES = ["Never", "Auto (only when prompt mentions it)", "Always on"]
MCP_POLICY_KEYS = ["never", "auto", "always"]
TXTMODE_OPTIONS = ["auto", "marked (>>>/<<< blocks)", "blank-line", "line"]
TXTMODE_KEYS = {
    "auto": "auto",
    "marked (>>>/<<< blocks)": "marked",
    "blank-line": "blank-line",
    "line": "line",
}
TXTMODE_KEYS_REVERSE = {v: k for k, v in TXTMODE_KEYS.items()}


class ManualHooks:
    """Worker-thread side of the manual-mode step buttons."""

    def __init__(self, app: "App"):
        self.app = app

    def wait_for_click(self, step: int, description: str) -> None:
        app = self.app
        app.post(("manual_wait", step, description))
        ev = app.manual_event
        ev.clear()
        while not ev.wait(0.25):
            if app.engine is not None and app.engine.is_stopped:
                raise Stopped()
        if app.engine is not None and app.engine.is_stopped:
            raise Stopped()
        app.post(("manual_resume", 0, ""))

    def countdown(self, seconds: float, description: str) -> None:
        app = self.app
        for s in range(int(seconds), -1, -1):
            if app.engine is not None and app.engine.is_stopped:
                raise Stopped()
            app.post(("status", f"{description} — {s}s left", ""))
            time.sleep(1)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Subscription Sweater — prompts in, answers out")
        root.geometry("1060x820")
        root.minsize(900, 700)

        self.settings = cfgmod.load()
        self.q: "queue.Queue" = queue.Queue()
        self.manual_event = threading.Event()
        self.engine: Engine | None = None
        self.worker: threading.Thread | None = None
        self.running = False
        self.paused = False

        self._build_widgets()
        self._apply_settings()
        self._refresh_preview()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll)

    # ------------------------------------------------------------------ #
    # building
    # ------------------------------------------------------------------ #
    def _build_widgets(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        pad = {"padx": 8, "pady": 4}

        # --- 1 · source ----------------------------------------------------
        src = ttk.LabelFrame(root, text=" 1 · Source (prompts) ")
        src.grid(row=0, column=0, sticky="ew", **pad)
        src.columnconfigure(1, weight=1)

        ttk.Label(src, text="Input file:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.var_input = tk.StringVar()
        ttk.Entry(src, textvariable=self.var_input).grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        self._btn_browse_input = ttk.Button(src, text="Browse…", command=self._browse_input)
        self._btn_browse_input.grid(row=0, column=2, padx=6)

        ttk.Label(src, text="Txt split:").grid(row=1, column=0, sticky="e", padx=6, pady=4)
        self.var_txtmode = tk.StringVar(value="auto")
        ttk.Combobox(src, textvariable=self.var_txtmode,
                     values=TXTMODE_OPTIONS,
                     state="readonly", width=22).grid(row=1, column=1, sticky="w", padx=4, pady=4)
        self._btn_reload = ttk.Button(src, text="Reload preview", command=self._refresh_preview)
        self._btn_reload.grid(row=1, column=2, padx=6)

        self.lbl_count = ttk.Label(src, text="no input file yet", foreground="#666")
        self.lbl_count.grid(row=2, column=1, sticky="w", padx=8)

        self.preview = tk.Listbox(src, height=5, activestyle="none")
        self.preview.grid(row=3, column=0, columnspan=3, sticky="ew", padx=6, pady=(2, 6))

        # --- 2 · destination -------------------------------------------------
        dst = ttk.LabelFrame(root, text=" 2 · Destination (answers) ")
        dst.grid(row=1, column=0, sticky="ew", **pad)
        dst.columnconfigure(1, weight=1)

        ttk.Label(dst, text="Output file:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.var_output = tk.StringVar()
        ttk.Entry(dst, textvariable=self.var_output).grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        self._btn_browse_output = ttk.Button(dst, text="Choose…", command=self._browse_output)
        self._btn_browse_output.grid(row=0, column=2, padx=6)

        ttk.Label(dst, text="(leave empty for  <input>_responses.<format>)",
                  foreground="#666").grid(row=1, column=1, sticky="w", padx=4)
        self.var_fmt = tk.StringVar(value="txt")
        ttk.Combobox(dst, textvariable=self.var_fmt, values=FMTS,
                     state="readonly", width=8).grid(row=1, column=2, sticky="w", padx=6, pady=(0, 4))

        # --- 3 · where the prompts go ----------------------------------------
        mid = ttk.LabelFrame(root, text=" 3 · Where the prompts go ")
        mid.grid(row=2, column=0, sticky="ew", **pad)
        mid.columnconfigure(1, weight=1)

        ttk.Label(mid, text="Mode:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.var_mode = tk.StringVar(value=MODES[0])
        self.cmb_mode = ttk.Combobox(mid, textvariable=self.var_mode, values=MODES,
                                     state="readonly", width=52)
        self.cmb_mode.grid(row=0, column=1, columnspan=2, sticky="ew", padx=4, pady=4)
        self.cmb_mode.bind("<<ComboboxSelected>>", lambda e: self._on_mode_change())

        # browser sub-options
        self.frame_site = ttk.Frame(mid)
        self.frame_site.columnconfigure(3, weight=1)
        ttk.Label(self.frame_site, text="Site:").grid(row=0, column=0, sticky="w", padx=6, pady=2)
        self.var_site = tk.StringVar(value=SITES[0])
        ttk.Combobox(self.frame_site, textvariable=self.var_site, values=SITES,
                     state="readonly", width=16).grid(row=0, column=1, padx=4)
        ttk.Label(self.frame_site, text="Custom URL:").grid(row=0, column=2, sticky="e", padx=4)
        self.var_url = tk.StringVar()
        ttk.Entry(self.frame_site, textvariable=self.var_url).grid(row=0, column=3,
                                                                   sticky="ew", padx=4, pady=2)
        self.chk_headless = ttk.Checkbutton(self.frame_site, text="headless (only if already logged in)")
        self.chk_headless.grid(row=0, column=4, padx=8)

        ttk.Label(self.frame_site, text="Model:").grid(row=1, column=0, sticky="e", padx=6)
        self.var_model = tk.StringVar()
        self.ent_model = ttk.Entry(self.frame_site, textvariable=self.var_model)
        self.ent_model.grid(row=1, column=1, sticky="w", padx=4, pady=2)
        self.lbl_model_hint = ttk.Label(self.frame_site, text="", foreground="#888")
        self.lbl_model_hint.grid(row=1, column=3, sticky="w", padx=2)

    def _update_model_hint(self) -> None:
        hints = {
            "chatgpt": "e.g. GPT-5, GPT-4o, o3 — empty = app default",
            "claude": "e.g. Opus, Sonnet, Haiku — empty = app default",
            "gemini": "e.g. Gemini 2.5 Pro — empty = app default",
            "qwen": "e.g. Qwen3-Max, Qwen3 — empty = app default",
            "custom": "empty = app default",
        }
        self.lbl_model_hint.config(text=hints.get(self._site_key(), ""))

        # desktop sub-options
        self.frame_desktop = ttk.Frame(mid)
        self.frame_desktop.columnconfigure(1, weight=1)
        ttk.Label(self.frame_desktop, text="App executable:").grid(row=0, column=0,
                                                                    sticky="w", padx=6, pady=2)
        self.var_deskexe = tk.StringVar()
        ttk.Entry(self.frame_desktop, textvariable=self.var_deskexe).grid(
            row=0, column=1, sticky="ew", padx=4, pady=2)
        self.var_deskport = tk.StringVar(value="9222")
        ttk.Label(self.frame_desktop, text="Debug port:").grid(row=0, column=2, sticky="e", padx=4)
        ttk.Entry(self.frame_desktop, textvariable=self.var_deskport, width=7).grid(
            row=0, column=3, sticky="w")
        self.chk_attach = ttk.Checkbutton(
            self.frame_desktop,
            text="app is already running with the flag (attach only)")
        self.chk_attach.grid(row=0, column=4, padx=8)
        ttk.Label(self.frame_desktop,
                  text="The tool starts the app with --remote-debugging-port so it can attach. "
                  "Leave the path empty for the default install location. "
                  "Microsoft Store builds may ignore the flag — use the direct-download app.",
                  foreground="#666", wraplength=900, justify="left").grid(
            row=1, column=0, columnspan=5, sticky="w", padx=6, pady=(0, 2))

        # manual sub-options
        self.frame_manual = ttk.Frame(mid)
        self.frame_manual.columnconfigure(1, weight=1)
        ttk.Label(self.frame_manual,
                  text="Manual mode: you focus the app & select the answer; the tool "
                  "pastes/copies for you — use the two big buttons in row 6 during a run.").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=6, pady=2)
        self.chk_autosend = ttk.Checkbutton(self.frame_manual, text="press Enter after pasting (auto-send)")
        self.chk_autosend.grid(row=1, column=0, sticky="w", padx=6)
        ttk.Label(self.frame_manual, text="window title to auto-focus (optional):").grid(
            row=1, column=1, sticky="e", padx=4)
        self.var_focuswin = tk.StringVar()
        ttk.Entry(self.frame_manual, textvariable=self.var_focuswin, width=24).grid(
            row=1, column=2, sticky="w", padx=2)
        ttk.Label(self.frame_manual, text="fixed wait (s, 0 = use buttons):").grid(
            row=2, column=1, sticky="e", padx=4)
        self.var_timer = tk.StringVar(value="0")
        ttk.Entry(self.frame_manual, textvariable=self.var_timer, width=8).grid(
            row=2, column=2, sticky="w", padx=2, pady=(0, 4))

        # --- 4 · tools: web search / MCP / skills -----------------------------
        tools = ttk.LabelFrame(root, text=" 4 · Tools: web search · MCP · skills ")
        tools.grid(row=3, column=0, sticky="ew", **pad)
        tools.columnconfigure(1, weight=1)
        tools.columnconfigure(3, weight=1)

        ttk.Label(tools, text="Web search:").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        self.var_web = tk.StringVar(value=WEB_POLICIES[0])
        ttk.Combobox(tools, textvariable=self.var_web, values=WEB_POLICIES,
                     state="readonly", width=30).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(tools, text="MCP tools:").grid(row=0, column=2, sticky="e", padx=4)
        self.var_mcp = tk.StringVar(value=MCP_POLICIES[0])
        ttk.Combobox(tools, textvariable=self.var_mcp, values=MCP_POLICIES,
                     state="readonly", width=34).grid(row=0, column=3, sticky="w", padx=4)

        ttk.Label(tools, text="Allowed MCP (empty = all):").grid(row=1, column=0, sticky="e", padx=2)
        self.var_allowed = tk.StringVar()
        ttk.Entry(tools, textvariable=self.var_allowed).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Label(tools, text="Denied (websearch, mcp, mcp:github, skill:x):").grid(
            row=1, column=2, sticky="e", padx=4)
        self.var_denied = tk.StringVar()
        ttk.Entry(tools, textvariable=self.var_denied).grid(row=1, column=3, sticky="ew", padx=4)

        ttk.Label(tools, text="Skills:").grid(row=2, column=0, sticky="e", padx=2)
        self.var_skills = tk.StringVar()
        ttk.Entry(tools, textvariable=self.var_skills).grid(row=2, column=1, sticky="ew", padx=4)
        self.chk_instruct = ttk.Checkbutton(tools, text="append tool instructions to prompts")
        self.chk_instruct.grid(row=2, column=2, columnspan=2, sticky="w", padx=2)
        self.chk_directives = ttk.Checkbutton(tools, text="honor @@ directives inside the prompt file")
        self.chk_directives.grid(row=3, column=2, columnspan=2, sticky="w", padx=2)
        ttk.Label(tools, text="MCP servers (JSON, same shape as Claude desktop config):",
                  foreground="#666").grid(row=3, column=0, columnspan=2, sticky="e", padx=2, pady=(4, 0))

        self.mcp_json_box = tk.Text(tools, height=3, width=80, font=("Courier", 9))
        self.mcp_json_box.grid(row=4, column=0, columnspan=4, sticky="ew", padx=6, pady=(2, 6))
        ttk.Label(tools,
                  text='e.g.  {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], '
                  '"env": {"GITHUB_TOKEN": "…"}}}   — written to Claude desktop before a desktop run',
                  foreground="#888", wraplength=950, justify="left").grid(
            row=5, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 6))

        ttk.Label(tools, text="Images folder (auto-attach by prompt ID; empty = input folder):")\
            .grid(row=6, column=0, columnspan=2, sticky="e", padx=2)
        self.var_imagesdir = tk.StringVar()
        ttk.Entry(tools, textvariable=self.var_imagesdir).grid(
            row=6, column=2, columnspan=2, sticky="ew", padx=4, pady=(0, 4))

        # --- 5 · timing ------------------------------------------------------
        tim = ttk.LabelFrame(root, text=" 5 · Timing & run control ")
        tim.grid(row=4, column=0, sticky="ew", **pad)

        def entry(label: str, var: tk.StringVar, row: int, col: int) -> None:
            ttk.Label(tim, text=label).grid(row=row, column=col * 2, sticky="e", padx=2, pady=2)
            ttk.Entry(tim, textvariable=var, width=7).grid(row=row, column=col * 2 + 1,
                                                           sticky="w", padx=2, pady=2)

        self.var_delay = tk.StringVar(value="2")
        entry("wait between prompts (s):", self.var_delay, 0, 0)
        self.var_stable = tk.StringVar(value="6")
        entry("answer stable for (s):", self.var_stable, 0, 1)
        self.var_maxwait = tk.StringVar(value="300")
        entry("max wait per answer (s):", self.var_maxwait, 0, 2)
        self.var_start = tk.StringVar(value="0")
        entry("start from prompt # (0 = resume):", self.var_start, 0, 3)
        self.var_limit = tk.StringVar(value="0")
        entry("limit (0 = all):", self.var_limit, 1, 0)
        self.chk_newchat = ttk.Checkbutton(tim, text="new chat per prompt")
        self.chk_newchat.grid(row=1, column=1, columnspan=2, sticky="w", padx=2)
        self.chk_debug = ttk.Checkbutton(tim, text="save debug dumps on failure")
        self.chk_debug.grid(row=1, column=3, columnspan=2, sticky="w", padx=2)

        # --- 6 · run controls -------------------------------------------------
        ctl = ttk.Frame(root)
        ctl.grid(row=5, column=0, sticky="ew", **pad)
        ctl.columnconfigure(6, weight=1)

        self.btn_start = ttk.Button(ctl, text="▶  START", command=self._start)
        self.btn_start.grid(row=0, column=0, padx=4, ipady=6)
        self.btn_pause = ttk.Button(ctl, text="Pause", command=self._toggle_pause, state="disabled")
        self.btn_pause.grid(row=0, column=1, padx=4)
        self.btn_stop = ttk.Button(ctl, text="Stop", command=self._stop, state="disabled")
        self.btn_stop.grid(row=0, column=2, padx=4)
        self.btn_step1 = ttk.Button(ctl, text="1) Pasted ok — app focused", command=self._step,
                                    state="disabled")
        self.btn_step1.grid(row=0, column=3, padx=4)
        self.btn_step2 = ttk.Button(ctl, text="2) Answer selected", command=self._step,
                                    state="disabled")
        self.btn_step2.grid(row=0, column=4, padx=4)

        self.progress = ttk.Progressbar(ctl, mode="determinate")
        self.progress.grid(row=0, column=5, sticky="ew", padx=8)
        self.lbl_status = ttk.Label(ctl, text="Ready. Pick an input file, then press START.")
        self.lbl_status.grid(row=1, column=0, columnspan=7, sticky="w", pady=(2, 0))

        # --- 7 · log -----------------------------------------------------------
        logf = ttk.LabelFrame(root, text=" Log ")
        logf.grid(row=6, column=0, sticky="nsew", **pad)
        root.rowconfigure(6, weight=1)
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.logbox = ScrolledText(logf, height=10, state="disabled", font=("Courier", 9))
        self.logbox.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

    # ------------------------------------------------------------------ #
    # settings
    # ------------------------------------------------------------------ #
    def _apply_settings(self) -> None:
        s = self.settings
        self.var_input.set(s.get("input_path", ""))
        self.var_output.set(s.get("output_path", ""))
        self.var_fmt.set(s.get("output_format", "txt"))
        self.var_txtmode.set(TXTMODE_KEYS_REVERSE.get(s.get("txt_mode", "auto"), "auto"))
        self.var_delay.set(str(s.get("delay_between", 2)))
        self.var_stable.set(str(s.get("stable_seconds", 6)))
        self.var_maxwait.set(str(s.get("max_wait_seconds", 300)))
        self.var_start.set(str(s.get("start_index", 0)))
        self.var_limit.set(str(s.get("limit", 0)))
        self.var_timer.set(str(s.get("timer_seconds", 0)))
        self.var_url.set(s.get("custom_url", ""))
        self.var_model.set(s.get("model", ""))
        self.var_deskexe.set(s.get("desktop_exe", ""))
        self.var_deskport.set(str(s.get("desktop_port", 9222)))
        self.var_focuswin.set(s.get("focus_window", ""))
        self.var_allowed.set(s.get("allowed_mcp", ""))
        self.var_denied.set(s.get("denied", ""))
        self.var_skills.set(s.get("skills", ""))
        self.var_imagesdir.set(s.get("images_dir", ""))
        self.chk_headless.state(["selected"] if s.get("headless") else ["deselected"])
        self.chk_newchat.state(["selected"] if s.get("new_chat", True) else ["deselected"])
        self.chk_autosend.state(["selected"] if s.get("auto_send", True) else ["deselected"])
        self.chk_debug.state(["selected"] if s.get("debug") else ["deselected"])
        self.chk_attach.state(["selected"] if s.get("desktop_attach_only") else ["deselected"])
        self.chk_instruct.state(["selected"] if s.get("inject_instructions", True) else ["deselected"])
        self.chk_directives.state(["selected"] if s.get("use_directives", True) else ["deselected"])
        if s.get("site") in SITE_KEYS:
            self.var_site.set(SITES[SITE_KEYS.index(s["site"])])
        if s.get("web_search") in WEB_POLICY_KEYS:
            self.var_web.set(WEB_POLICIES[WEB_POLICY_KEYS.index(s["web_search"])])
        if s.get("mcp") in MCP_POLICY_KEYS:
            self.var_mcp.set(MCP_POLICIES[MCP_POLICY_KEYS.index(s["mcp"])])
        if s.get("mode") in MODE_KEYS:
            self.var_mode.set(MODES[MODE_KEYS.index(s["mode"])])
        self.mcp_json_box.delete("1.0", "end")
        self.mcp_json_box.insert("1.0", s.get("mcp_servers_json", ""))
        self._on_mode_change()
        self._update_model_hint()

    def _parse_mcp_json(self) -> dict:
        raw = self.mcp_json_box.get("1.0", "end").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ToolError(f"MCP servers JSON is invalid: {e}")
        if not isinstance(data, dict):
            raise ToolError("MCP servers must be a JSON object: {\"name\": {\"command\": …}}")
        for name, spec in data.items():
            if not isinstance(spec, dict) or "command" not in spec and "url" not in spec:
                raise ToolError(f"MCP server '{name}' needs a \"command\" (or \"url\") key.")
        return data

    def _collect_settings(self) -> dict:
        d = {
            "mode": self._mode_key(),
            "site": self._site_key(),
            "custom_url": self.var_url.get().strip(),
            "model": self.var_model.get().strip(),
            "input_path": self.var_input.get().strip(),
            "output_path": self.var_output.get().strip(),
            "output_format": self._fmt(),
            "txt_mode": self._txtmode_key(),
            "headless": bool(self.chk_headless.instate(["selected"])),
            "new_chat": bool(self.chk_newchat.instate(["selected"])),
            "auto_send": bool(self.chk_autosend.instate(["selected"])),
            "debug": bool(self.chk_debug.instate(["selected"])),
            "desktop_exe": self.var_deskexe.get().strip(),
            "focus_window": self.var_focuswin.get().strip(),
            "allowed_mcp": self.var_allowed.get().strip(),
            "denied": self.var_denied.get().strip(),
            "skills": self.var_skills.get().strip(),
            "images_dir": self.var_imagesdir.get().strip(),
            "inject_instructions": bool(self.chk_instruct.instate(["selected"])),
            "use_directives": bool(self.chk_directives.instate(["selected"])),
            "mcp_servers_json": self.mcp_json_box.get("1.0", "end").strip(),
        }
        try:
            d["desktop_port"] = int(self.var_deskport.get().strip() or 9222)
        except ValueError:
            d["desktop_port"] = 9222
        d["desktop_attach_only"] = bool(self.chk_attach.instate(["selected"]))
        d["web_search"] = WEB_POLICY_KEYS[WEB_POLICIES.index(self.var_web.get())] \
            if self.var_web.get() in WEB_POLICIES else "auto"
        d["mcp"] = MCP_POLICY_KEYS[MCP_POLICIES.index(self.var_mcp.get())] \
            if self.var_mcp.get() in MCP_POLICIES else "never"
        for key, var in (("delay_between", self.var_delay),
                         ("stable_seconds", self.var_stable),
                         ("max_wait_seconds", self.var_maxwait),
                         ("timer_seconds", self.var_timer)):
            try:
                d[key] = float(var.get())
            except ValueError:
                d[key] = cfgmod.DEFAULTS[key]
        for key, var in (("start_index", self.var_start), ("limit", self.var_limit)):
            try:
                d[key] = int(float(var.get()))
            except ValueError:
                d[key] = cfgmod.DEFAULTS[key]
        return d

    def _mode_key(self) -> str:
        return MODE_KEYS[self.var_mode.get()] if self.var_mode.get() in MODES else "browser"

    def _site_key(self) -> str:
        name = self.var_site.get()
        return SITE_KEYS[SITES.index(name)] if name in SITES else "chatgpt"

    def _fmt(self) -> str:
        return self.var_fmt.get() if self.var_fmt.get() in FMTS else "txt"

    def _txtmode_key(self) -> str:
        return TXTMODE_KEYS.get(self.var_txtmode.get(), "auto")

    def _on_mode_change(self) -> None:
        key = self._mode_key()
        if key == "browser":
            self.frame_site.grid(row=1, column=0, columnspan=3, sticky="ew")
            self.frame_desktop.grid_forget()
            self.frame_manual.grid_forget()
        elif key == "desktop":
            self.frame_site.grid(row=1, column=0, columnspan=3, sticky="ew")
            self.frame_desktop.grid(row=2, column=0, columnspan=3, sticky="ew")
            self.frame_manual.grid_forget()
        elif key == "manual":
            self.frame_site.grid_forget()
            self.frame_desktop.grid_forget()
            self.frame_manual.grid(row=1, column=0, columnspan=3, sticky="ew")
        else:
            self.frame_site.grid_forget()
            self.frame_desktop.grid_forget()
            self.frame_manual.grid_forget()
        self._update_model_hint()

    # ------------------------------------------------------------------ #
    # browsing / preview
    # ------------------------------------------------------------------ #
    def _browse_input(self) -> None:
        p = filedialog.askopenfilename(title="Pick the file with your prompts",
                                       filetypes=INPUT_TYPES)
        if p:
            self.var_input.set(p)
            self._refresh_preview()

    def _browse_output(self) -> None:
        base = os.path.splitext(self.var_input.get().strip() or "answers")[0]
        p = filedialog.asksaveasfilename(
            title="Where to save the answers",
            defaultextension=f".{self._fmt()}",
            initialfile=f"{base}_responses.{self._fmt()}",
            filetypes=OUTPUT_TYPES,
        )
        if p:
            self.var_output.set(p)

    def _refresh_preview(self) -> None:
        self.preview.delete(0, "end")
        path = self.var_input.get().strip()
        if not path:
            self.lbl_count.config(text="no input file yet")
            return
        try:
            prompts = readers.read_prompts(path, self._txtmode_key())
        except ToolError as e:
            self.lbl_count.config(text=f"error: {e}")
            return
        self.lbl_count.config(
            text=f"{len(prompts)} prompts found in {os.path.basename(path)}")
        for i, p in enumerate(prompts[:300], 1):
            shown = " ".join(p.split())[:140]
            self.preview.insert("end", f"{i:4d}  {shown}")

    # ------------------------------------------------------------------ #
    # queue polling (worker -> UI)
    # ------------------------------------------------------------------ #
    def post(self, msg) -> None:
        self.q.put(msg)

    def _poll(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._append_log(msg[1])
                elif kind == "status":
                    self.lbl_status.config(text=msg[1])
                elif kind == "progress":
                    self.progress.configure(value=msg[1], maximum=max(1, msg[2]))
                    self.lbl_status.config(text=f"… {msg[1]} of {msg[2]} done")
                elif kind == "manual_wait":
                    self._manual_buttons(msg[1])
                    self.lbl_status.config(text=msg[2])
                elif kind == "manual_resume":
                    self._manual_buttons(0)
                elif kind == "done":
                    note = msg[2]
                    self.lbl_status.config(text=note or "Finished.")
                    self._append_log(note or "Run finished.")
                    self._finish_run()
                elif kind == "error":
                    self._append_log(f"ERROR: {msg[1]}")
                    messagebox.showerror("Subscription Sweater", msg[1])
                    self._finish_run()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _append_log(self, text: str) -> None:
        self.logbox.config(state="normal")
        self.logbox.insert("end", text if text.endswith("\n") else text + "\n")
        if int(self.logbox.index("end-1c").split(".")[0]) > 6000:
            self.logbox.delete("1.0", "1000.0")
        self.logbox.config(state="disabled")
        self.logbox.see("end")

    # ------------------------------------------------------------------ #
    # run control
    # ------------------------------------------------------------------ #
    def _set_running(self, on: bool) -> None:
        self.running = on
        run = "disabled" if on else "normal"
        idle = "normal" if on else "disabled"
        self.btn_start.config(state=run)
        self.btn_pause.config(state=idle)
        self.btn_stop.config(state=idle)
        self._btn_browse_input.config(state=run)
        self._btn_browse_output.config(state=run)
        self._btn_reload.config(state=run)
        self.cmb_mode.config(state="disabled" if on else "readonly")

    def _toggle_pause(self) -> None:
        if self.engine is None:
            return
        if self.paused:
            self.engine.resume()
            self.paused = False
            self.btn_pause.config(text="Pause")
            self._append_log("Resumed.")
        else:
            self.engine.pause()
            self.paused = True
            self.btn_pause.config(text="Resume")
            self._append_log("Paused (takes effect between prompts).")

    def _stop(self) -> None:
        if self.engine is not None:
            self.engine.stop()
        self.manual_event.set()
        self._append_log("Stopping after the current step…")

    def _step(self) -> None:
        self.manual_event.set()

    def _manual_buttons(self, step: int) -> None:
        if step == 1:
            self.btn_step1.config(state="normal")
            self.btn_step2.config(state="disabled")
        elif step == 2:
            self.btn_step1.config(state="disabled")
            self.btn_step2.config(state="normal")
        else:
            self.btn_step1.config(state="disabled")
            self.btn_step2.config(state="disabled")

    # ------------------------------------------------------------------ #
    # start / finish
    # ------------------------------------------------------------------ #
    def _start(self) -> None:
        if self.running:
            return
        try:
            s = self._collect_settings()
            mcp_servers = self._parse_mcp_json()
        except ToolError as e:
            messagebox.showerror("Subscription Sweater", str(e))
            return
        cfgmod.save(s)
        mode, site = s["mode"], s["site"]
        input_path = s["input_path"]
        if not input_path or not os.path.exists(input_path):
            messagebox.showerror("Subscription Sweater",
                                 "Choose an input file first (.txt / .csv / .xlsx / .docx).")
            return
        s["txt_mode"] = self._txtmode_key()
        try:
            prompts = readers.read_prompts(input_path, s["txt_mode"])
        except ToolError as e:
            messagebox.showerror("Subscription Sweater", str(e))
            return
        if not prompts:
            messagebox.showerror("Subscription Sweater", "That file has no prompts in it.")
            return
        if mode == "browser" and site == "custom" and not s["custom_url"]:
            messagebox.showerror("Subscription Sweater",
                                 "Custom site mode needs a URL (e.g. https://chatgpt.com/).")
            return
        if mode == "desktop" and site == "custom":
            messagebox.showerror("Subscription Sweater",
                                 "Desktop mode currently supports the ChatGPT and Claude apps.")
            return

        out = s["output_path"] or os.path.splitext(input_path)[0] + f"_responses.{self._fmt()}"
        try:
            cfg = RunConfig(
                mode=mode, site=site, custom_url=s["custom_url"], model=s["model"],
                input_path=input_path, output_path=out,
                headless=s["headless"], txt_mode=s["txt_mode"],
                desktop_exe=s["desktop_exe"], desktop_port=s["desktop_port"],
                desktop_attach_only=s["desktop_attach_only"],
                focus_window=s["focus_window"],
                delay_between=s["delay_between"], stable_seconds=s["stable_seconds"],
                max_wait_seconds=s["max_wait_seconds"], login_timeout=s.get("login_timeout", 300),
                new_chat=s["new_chat"], start_index=s["start_index"], limit=s["limit"],
                auto_send=s["auto_send"], timer_seconds=s["timer_seconds"], debug=s["debug"],
                web_search=s["web_search"], mcp=s["mcp"], mcp_servers=mcp_servers,
                allowed_mcp=s["allowed_mcp"], denied=s["denied"], skills=s["skills"],
                inject_instructions=s["inject_instructions"], use_directives=s["use_directives"],
                images_dir=s["images_dir"],
                profile_dir=os.path.join(BASE_DIR, "browser_profile"),
                debug_dir=os.path.join(BASE_DIR, "debug"),
            )
        except Exception as e:
            messagebox.showerror("Subscription Sweater", f"Invalid setting: {e}")
            return

        self.progress.configure(value=0, maximum=max(1, len(prompts)))
        self.lbl_status.config(text="Starting…")
        hooks = ManualHooks(self) if mode == "manual" else None
        engine = Engine(cfg, log=lambda m: self.post(("log", m)),
                        progress=lambda d, t: self.post(("progress", d, t)),
                        hooks=hooks)
        self.engine = engine
        self._set_running(True)
        self.worker = threading.Thread(target=self._worker, args=(engine,), daemon=True)
        self.worker.start()

    def _worker(self, engine: Engine) -> None:
        try:
            engine.run()
            self.post(("done", True, f"Done — answers saved to {engine.cfg.output_path}"))
        except Stopped:
            self.post(("done", False,
                       "Stopped by user — progress saved; press START again to resume."))
        except ToolError as e:
            self.post(("error", str(e)))
        except Exception as e:
            self.post(("error", f"Unexpected error: {e!r}"))

    def _finish_run(self) -> None:
        self._set_running(False)
        self._manual_buttons(0)
        self.paused = False
        self.btn_pause.config(text="Pause")
        self.engine = None
        self._refresh_preview()

    def _on_close(self) -> None:
        try:
            cfgmod.save(self._collect_settings())
        except Exception:
            pass
        if self.running:
            if not messagebox.askokcancel(
                    "Subscription Sweater",
                    "A run is in progress. Quit and stop the run? (Progress is saved.)"):
                return
            if self.engine is not None:
                self.engine.stop()
            self.manual_event.set()
            self.root.after(300, self.root.destroy)
        else:
            self.root.destroy()
