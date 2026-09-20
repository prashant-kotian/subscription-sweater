"""Terminal (CLI) bot: drive the official Google / Qwen CLI clients.

Gemini and Qwen ship NO MCP attachment in their consumer web products — but
their official CLI clients natively speak MCP. Terminal mode is the bridge:

* one headless call per prompt:  agy -p "…"  /  gemini -p "…"  /  qwen -p "…"
* each call is a FRESH session (equivalent to "new chat" ON); with
  --no-new-chat the bot passes the client's own "continue most recent
  session" flag so prompts share a session (agy: --continue;
  legacy gemini: --resume latest; qwen: --continue — qwen's own --resume
  takes a real session ID with no "latest" special case, unlike gemini's)
* MCP servers are written into the client's config by the engine before the
  run (mcp_config.ensure_cli_mcp_servers) and then SCOPED PER PROMPT:
    - gemini / qwen: --allowed-mcp-server-names allow-list (a prompt with
      mcp=OFF gets an allow-list that matches nothing, so it really has no
      MCP tools)
    - agy (Antigravity CLI, no allow-list flag): the run's servers are
      toggled in ~/.gemini/config/mcp_config.json before each launch
* auto-approve so the run never hangs on a confirmation prompt:
  agy: --dangerously-skip-permissions · gemini/qwen: --yolo
  (disable with --no-yolo if you prefer to run without tools)
* agy answers are captured from its --output-format json envelope
  {status, response, error, usage}; gemini/qwen from plain stdout

Google RETIRED the open-source Gemini CLI for individual accounts on
2026-06-18; its replacement is the Antigravity CLI (the `agy` binary, a Go
app that reuses the ~/.gemini home). Terminal mode auto-detects: agy when
installed, legacy gemini otherwise (still alive for paid-API-key /
enterprise installs).

Login is one-time and happens in the user's own terminal:
  agy      -> sign in with a Google account (free with your plan, no API key)
  gemini   -> same (legacy)
  qwen     -> sign in with a Qwen account (free tier, no API key)
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
import time

from .errors import Stopped, ToolError

#: Sites available in terminal mode (official CLI clients that speak MCP).
CLI_SITES = ("gemini", "qwen")

#: NO-MCP trick: an allow-list containing a name no server will ever have.
_EMPTY_ALLOWLIST = "__subscription_sweater_no_mcp__"


def select_client(site: str) -> str:
    """Pick the CLI client for *site*.

    Google retired the open-source Gemini CLI for individual accounts on
    2026-06-18; the replacement is the Antigravity CLI (`agy`). Prefer agy
    when it is installed; fall back to the legacy `gemini` binary (still
    functional for paid-API-key / Gemini Code Assist enterprise installs).
    """
    if site == "gemini" and shutil.which("agy"):
        return "agy"
    return site


# --------------------------------------------------------------------------- #
# Per-client specs
# --------------------------------------------------------------------------- #
_AGY_SPEC = {
    "bin": "agy",
    "name": "Antigravity CLI",
    "install": ("Windows PowerShell:  irm https://antigravity.google/cli/install.ps1 | iex\n"
                "macOS / Linux:       curl -fsSL https://antigravity.google/cli/install.sh | bash"),
    "auth": ("run `agy` once in your terminal and sign in with your Google account "
             "(browser login) — free with your plan, no API key; headless runs "
             "reuse the cached login"),
    "settings": "~/.gemini/config/mcp_config.json",
    "mcp_tool": "agy mcp add <name> <command>",
    "model_hint": ("agy: `agy models` lists what your account can use "
                   "(Gemini / Claude / gpt-oss) — empty = CLI default"),
    # Official headless docs: "-c / --continue resumes the MOST RECENT
    # conversation" (the JSON envelope's conversation_id works too, via
    # --conversation <id>).
    "continue_args": ["--continue"],
    # Official headless docs: agy has NO --yolo;
    # --dangerously-skip-permissions sets the permission mode to
    # "always-proceed" — the headless equivalent of gemini --yolo.
    "yolo_args": ["--dangerously-skip-permissions"],
    # --output-format json -> one JSON envelope {status, response, error, …}
    # on completion (diagnostics go to stderr, so stdout stays clean).
    "output_json": True,
    # agy has no per-invocation MCP allow-list flag -> scope per prompt by
    # rewriting the run's servers in mcp_config.json before each launch.
    "mcp_allowlist_flag": False,
}

_GEMINI_SPEC = {
    "bin": "gemini",
    "name": "Gemini CLI (legacy)",
    "install": "npm install -g @google/gemini-cli   (or: brew install gemini-cli)",
    "auth": ("run `gemini` once in your terminal and sign in with your "
             "Google account — free tier, no API key"),
    "settings": "~/.gemini/settings.json",
    "mcp_tool": "gemini mcp add <name> <command>",
    "model_hint": "e.g. gemini-3.1-pro-preview, gemini-3-flash-preview — empty = client Auto",
    # confirmed via `gemini --help`: -r/--resume explicitly documents
    # "latest" as a real special value ("Use 'latest' for most recent").
    "continue_args": ["--resume", "latest"],
    "yolo_args": ["--yolo"],
    "output_json": False,
    "mcp_allowlist_flag": True,
}

_QWEN_SPEC = {
    "bin": "qwen",
    "name": "Qwen Code",
    "install": "npm install -g @qwen-code/qwen-code   (or: brew install qwen-code)",
    "auth": ("run `qwen` once in your terminal and sign in with your "
             "Qwen account — free tier, no API key"),
    "settings": "~/.qwen/settings.json",
    "mcp_tool": "qwen mcp add <name> <command>",
    "model_hint": "e.g. qwen3-coder-next, qwen3-max — empty = client default",
    # confirmed via `qwen --help`: unlike gemini, --resume/-r here takes
    # a REAL session ID ("Resume a specific session by its ID") with no
    # "latest" special-case -- passing the literal string "latest" would
    # be treated as an id that doesn't exist. The real "most recent
    # session" flag is the separate -c/--continue boolean.
    "continue_args": ["--continue"],
    "yolo_args": ["--yolo"],
    "output_json": False,
    "mcp_allowlist_flag": True,
}


def _cli_spec(site: str, client: str | None = None) -> dict:
    if site == "gemini":
        client = client or select_client("gemini")
        return _AGY_SPEC if client == "agy" else _GEMINI_SPEC
    if site == "qwen":
        return _QWEN_SPEC
    raise ToolError(
        f"Terminal mode supports: gemini, qwen (their official CLI clients are "
        f"what speak MCP). ChatGPT/Claude use browser or desktop mode; any other "
        f"app: manual paste mode.")


class TerminalCLIBot:
    def __init__(self, site: str, yolo: bool = True, new_chat: bool = True,
                 has_mcp_servers: bool = False, mcp_servers: dict | None = None,
                 client: str | None = None, max_wait_seconds: float = 300.0,
                 log=print, should_stop=None):
        self.site = site
        self.client = client or select_client(site)
        self.spec = _cli_spec(site, self.client)
        self.yolo = yolo
        self.new_chat = new_chat
        self.has_mcp_servers = has_mcp_servers
        self.mcp_servers = dict(mcp_servers or {})
        self.max_wait = max_wait_seconds
        self.log = log
        self._should_stop = should_stop or (lambda: False)

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        bin_path = shutil.which(self.spec["bin"])
        if not bin_path:
            raise ToolError(self._missing_client_error())
        self.log(f"Terminal mode: {self.spec['name']} "
                 f"({self.spec['bin']}) — one headless call per prompt, "
                 f"answers captured from stdout.")
        if not self.new_chat:
            self.log("  (sessions: continuing the latest CLI session per run "
                     f"({' '.join(self.spec['continue_args'])}))")
        if self.yolo:
            self.log(f"  (auto-approve: ON — tool calls run without confirmation "
                     f"({' '.join(self.spec['yolo_args'])}); CLI flag --no-yolo disables this)")
        else:
            self.log("  (auto-approve: OFF — prompts that need tool confirmation "
                     "may fail in headless mode)")

    def _missing_client_error(self) -> str:
        if self.site == "gemini":
            return (
                "Terminal mode (Gemini) needs a CLI client, but neither `agy` "
                "(Antigravity CLI) nor `gemini` (legacy Gemini CLI) was found on PATH.\n"
                "  install Antigravity CLI (Windows PowerShell):  "
                "irm https://antigravity.google/cli/install.ps1 | iex\n"
                "  install (macOS / Linux):                       "
                "curl -fsSL https://antigravity.google/cli/install.sh | bash\n"
                "  login:     run `agy` once and sign in with your Google account — "
                "free with your plan, no API key\n"
                "  (legacy:  npm install -g @google/gemini-cli — retired for "
                "individual accounts 2026-06-18; works only with a paid API key "
                "or Gemini Code Assist Standard/Enterprise)\n"
                "(Then start this run again.)")
        return (
            f"Terminal mode needs the {self.spec['name']} client, but "
            f"`{self.spec['bin']}` was not found on PATH.\n"
            f"  install:  {self.spec['install']}\n"
            f"  login:    {self.spec['auth']}\n"
            "(Node.js 18+ required. Then start this run again.)")

    # ------------------------------------------------------------------ #
    def build_command(self, prompt: str, decision=None) -> list:
        """The exact argv for one prompt (pure — unit-testable)."""
        s = self.spec
        cmd = [s["bin"], "-p", prompt]
        if decision is not None:
            if decision.target_model:
                cmd += ["--model", decision.target_model]
            if s["output_json"]:
                cmd += ["--output-format", "json"]
            if s["mcp_allowlist_flag"]:
                if decision.mcp and decision.mcp_names:
                    for n in decision.mcp_names:          # repeated array flag
                        cmd += ["--allowed-mcp-server-names", n]
                elif self.has_mcp_servers and not decision.mcp:
                    cmd += ["--allowed-mcp-server-names", _EMPTY_ALLOWLIST]
        if self.yolo:
            cmd += s["yolo_args"]
        if not self.new_chat:
            cmd += s["continue_args"]
        return cmd

    # ------------------------------------------------------------------ #
    def answer(self, prompt: str, decision=None) -> str:
        # agy has no per-invocation MCP allow-list flag: scope by toggling
        # the run's servers in the user-level mcp_config.json right before
        # launch (each `agy -p` is a fresh process that reads the file at
        # startup). gemini/qwen do this via --allowed-mcp-server-names.
        if self.spec["bin"] == "agy" and self.mcp_servers:
            from . import mcp_config

            enabled = set()
            if decision is not None and decision.mcp and decision.mcp_names:
                enabled = set(decision.mcp_names)
            mcp_config.set_cli_mcp_scope("agy", self.mcp_servers, enabled, log=self.log)
        cmd = self.build_command(prompt, decision)
        shown = [cmd[0], "-p", "…"] + cmd[3:]          # skip the prompt itself
        self.log("  (terminal: " + " ".join(shlex.quote(c) for c in shown) + ")")
        t0 = time.time()
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=os.getcwd(),
        )
        result: dict = {}

        def _drain():
            result["out"], result["err"] = proc.communicate()

        drainer = threading.Thread(target=_drain, daemon=True)
        drainer.start()
        try:
            while True:
                if self._should_stop():
                    raise Stopped()
                if time.time() - t0 > self.max_wait:
                    raise ToolError(
                        f"timed out after {self.max_wait:.0f}s "
                        f"(raise --max-wait for longer answers)")
                if proc.poll() is not None:
                    drainer.join(15)
                    break
                time.sleep(0.5)
        except Stopped:
            proc.kill()
            raise
        except ToolError:
            proc.kill()
            drainer.join(10)
            raise

        out = (result.get("out") or "").strip()
        err = (result.get("err") or "").strip()
        if proc.returncode != 0:
            raise ToolError(f"{self.spec['name']} exited with code {proc.returncode}: "
                            f"{_tail(err or out)}{_auth_hint(err, self.spec)}")
        if not out:
            raise ToolError(f"{self.spec['name']} returned no output "
                            f"{_tail(err)}{_auth_hint(err, self.spec)}")
        if self.spec["output_json"]:
            env = _parse_json_envelope(out)
            if env is not None:
                if env.get("status") == "SUCCESS":
                    return (env.get("response") or "").strip()
                raise ToolError(
                    f"{self.spec['name']} ended with status {env.get('status')}: "
                    f"{env.get('error') or _tail(out)}")
        return out

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        pass


def _parse_json_envelope(out: str):
    """agy --output-format json prints ONE JSON envelope on completion
    ({status, response, error, …}). Returns the dict, or None when stdout
    is not that envelope (plain text / older build) so the caller can fall
    back to raw output."""
    t = (out or "").strip()
    if not t.startswith("{"):
        return None
    try:
        data = json.loads(t)
    except Exception:
        return None
    return data if isinstance(data, dict) and "status" in data else None


def _tail(s: str, n: int = 500) -> str:
    s = (s or "").strip()
    return ("\n" + s[-n:]) if len(s) > n else ("\n" + s if s else "")


def _auth_hint(err: str, spec: dict) -> str:
    low = (err or "").lower()
    if any(k in low for k in ("sign in", "log in", "login", "auth", "oauth",
                              "api key", "unauthorized", "401")):
        return (f"\nHint: {' '.join(spec['auth'].split())} "
                f"(same step once per client).")
    return ""
