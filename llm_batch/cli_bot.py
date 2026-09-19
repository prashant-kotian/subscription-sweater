"""Terminal (CLI) bot: drive the official Gemini CLI / Qwen Code clients.

Gemini and Qwen ship NO MCP attachment in their consumer web products — but
their official open-source CLI clients natively speak MCP (they read an
`mcpServers` block from ~/.gemini/settings.json / ~/.qwen/settings.json).
Terminal mode is the bridge between that and this tool:

* one headless call per prompt:  gemini -p "…"   /   qwen -p "…"
* each call is a FRESH session (equivalent to "new chat" ON); with
  --no-new-chat the bot passes the site's own "continue most recent session"
  flag so prompts share a session (gemini: --resume latest; qwen: --continue
  -- qwen's own --resume takes a real session ID with no "latest" special
  case, unlike gemini's)
* MCP servers the client configured are written into the client's settings
  file by the engine before the run (mcp_config.ensure_cli_mcp_servers) and
  then SCOPED PER PROMPT with --allowed-mcp-server-names: the exact servers
  the policy decided ON for that prompt — and a prompt with mcp=OFF gets an
  allow-list that matches nothing, so it really has no MCP tools
* --yolo auto-approves tool calls so the run never hangs on a confirmation
  prompt (disable with --no-yolo if you prefer to run without MCP/tools)

Login is one-time and happens in the user's own terminal:
  gemini   -> sign in with a Google account (free tier, no API key)
  qwen     -> sign in with a Qwen account      (free tier, no API key)
"""
from __future__ import annotations

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


def _cli_spec(site: str) -> dict:
    if site == "gemini":
        return {
            "bin": "gemini",
            "name": "Gemini CLI",
            "install": "npm install -g @google/gemini-cli   (or: brew install gemini-cli)",
            "auth": ("run `gemini` once in your terminal and sign in with your "
                     "Google account — free tier, no API key"),
            "settings": "~/.gemini/settings.json",
            "mcp_tool": "gemini mcp add <name> <command>",
            "model_hint": "e.g. gemini-3.1-pro-preview, gemini-3-flash-preview — empty = client Auto",
            # confirmed via `gemini --help`: -r/--resume explicitly documents
            # "latest" as a real special value ("Use 'latest' for most recent").
            "continue_args": ["--resume", "latest"],
        }
    if site == "qwen":
        return {
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
        }
    raise ToolError(
        f"Terminal mode supports: gemini, qwen (their official CLI clients are "
        f"what speak MCP). ChatGPT/Claude use browser or desktop mode; any other "
        f"app: manual paste mode.")


class TerminalCLIBot:
    def __init__(self, site: str, yolo: bool = True, new_chat: bool = True,
                 has_mcp_servers: bool = False, max_wait_seconds: float = 300.0,
                 log=print, should_stop=None):
        self.spec = _cli_spec(site)
        self.yolo = yolo
        self.new_chat = new_chat
        self.has_mcp_servers = has_mcp_servers
        self.max_wait = max_wait_seconds
        self.log = log
        self._should_stop = should_stop or (lambda: False)

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        bin_path = shutil.which(self.spec["bin"])
        if not bin_path:
            raise ToolError(
                f"Terminal mode needs the {self.spec['name']} client, but "
                f"`{self.spec['bin']}` was not found on PATH.\n"
                f"  install:  {self.spec['install']}\n"
                f"  login:    {self.spec['auth']}\n"
                f"(Node.js 18+ required. Then start this run again.)")
        self.log(f"Terminal mode: {self.spec['name']} "
                 f"({self.spec['bin']}) — one headless call per prompt, "
                 f"answers captured from stdout.")
        if not self.new_chat:
            self.log("  (sessions: continuing the latest CLI session per run "
                     f"({' '.join(self.spec['continue_args'])}))")
        if self.yolo:
            self.log("  (auto-approve: ON — tool calls run without confirmation; "
                     "CLI flag --no-yolo disables this)")
        else:
            self.log("  (auto-approve: OFF — prompts that need tool confirmation "
                     "may fail in headless mode)")

    # ------------------------------------------------------------------ #
    def build_command(self, prompt: str, decision=None) -> list:
        """The exact argv for one prompt (pure — unit-testable)."""
        s = self.spec
        cmd = [s["bin"], "-p", prompt]
        if decision is not None:
            if decision.target_model:
                cmd += ["--model", decision.target_model]
            if decision.mcp and decision.mcp_names:
                for n in decision.mcp_names:          # repeated array flag
                    cmd += ["--allowed-mcp-server-names", n]
            elif self.has_mcp_servers and not decision.mcp:
                cmd += ["--allowed-mcp-server-names", _EMPTY_ALLOWLIST]
        if self.yolo:
            cmd += ["--yolo"]
        if not self.new_chat:
            cmd += s["continue_args"]
        return cmd

    # ------------------------------------------------------------------ #
    def answer(self, prompt: str, decision=None) -> str:
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
                            f"{_tail(err or out)}{_auth_hint(err)}")
        if not out:
            raise ToolError(f"{self.spec['name']} returned no output "
                            f"{_tail(err)}{_auth_hint(err)}")
        return out

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        pass


def _tail(s: str, n: int = 500) -> str:
    s = (s or "").strip()
    return ("\n" + s[-n:]) if len(s) > n else ("\n" + s if s else "")


def _auth_hint(err: str) -> str:
    low = (err or "").lower()
    if any(k in low for k in ("sign in", "log in", "login", "auth", "oauth",
                              "api key", "unauthorized", "401")):
        return (f"\nHint: { ' '.join(_cli_spec('gemini')['auth'].split()) } "
                f"(same step once per client).")
    return ""
