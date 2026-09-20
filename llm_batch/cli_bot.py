"""Terminal (CLI) bot: drive the official subscription CLI clients.

Terminal mode = no API key, no extra bill — the run is driven through the
user's own subscription inside the vendor's official CLI:

  site      client          binary    model example
  chatgpt   Codex CLI       codex     GPT-5.6        (OpenAI)
  gemini    Antigravity CLI agy       Gemini Pro     (Google; legacy `gemini`
                                                    binary is the fallback)
  claude    Claude Code     claude    Claude Opus    (Anthropic)
  qwen      Qwen Code       qwen      Qwen3          (Alibaba)

Mechanics:

* one headless call per prompt, each call a FRESH process (that IS a "new
  chat"). With new_chat=OFF the client's own "continue the most recent
  session" feature is used so prompts share a conversation:
    agy: --continue · gemini: --resume latest · qwen: --continue
    codex: `exec resume --last` · claude: --continue
* auto-approve so the run never hangs on a confirmation prompt:
    agy/claude: --dangerously-skip-permissions
    gemini/qwen/codex: --yolo
* answers are captured per client:
    codex:   --output-last-message <tmpfile>  (final assistant message)
    agy:     --output-format json  ->  envelope {status, response, …}
    claude:  --output-format json  ->  {"result": …, "is_error": …}
    gemini/qwen: plain stdout
* MCP servers are written into the client's config by the engine BEFORE the
  run (mcp_config.ensure_cli_mcp_servers) and SCOPED PER PROMPT so a prompt
  with MCP off really has no MCP tools:
    gemini/qwen: --allowed-mcp-server-names allow-list flag
    agy:         run's servers toggled in ~/.gemini/config/mcp_config.json
    codex:       -c mcp_servers.<name>.enabled=false launch overrides
    claude:      per-launch --mcp-config <file> --strict-mcp-config file
* IMAGES in terminal mode = a file LOCATION on disk (the CLI runs locally
  and has file tools):
    codex:  attached natively via --image <path>
    others: the absolute path + a one-line instruction is appended to the
            prompt and the model reads the file itself (agy explicitly
            supports file paths in the prompt — it has no binary paste;
            claude/gemini/qwen all have file-reading tools)

Google retired the open-source Gemini CLI for individual accounts on
2026-06-18; agy is the replacement (Go binary, reuses the ~/.gemini home).
Terminal mode auto-detects: agy when installed, legacy gemini otherwise.

Login is one-time and happens in the user's own terminal:
  codex    -> `codex` once, log in with your ChatGPT account (no API key)
  agy      -> `agy` once, sign in with your Google account
  claude   -> `claude` once, sign in with your Claude account
  qwen     -> `qwen` once, sign in with your Qwen account
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time

from .errors import Stopped, ToolError

#: Sites available in terminal mode (each maps to a vendor's official CLI).
CLI_SITES = ("chatgpt", "gemini", "claude", "qwen")

#: NO-MCP trick: an allow-list containing a name no server will ever have.
_EMPTY_ALLOWLIST = "__subscription_sweater_no_mcp__"


def select_client(site: str) -> str:
    """Map a benchmark site to the CLI client that serves it."""
    if site == "chatgpt":
        return "codex"                        # ChatGPT subscription -> Codex CLI
    if site == "gemini":
        # agy is Google's current CLI (gemini-cli EOL 2026-06-18 for
        # individual accounts); fall back to the legacy binary.
        return "agy" if shutil.which("agy") else "gemini"
    if site in ("claude", "qwen"):
        return site
    raise ToolError(
        f"Terminal mode has no CLI client for site '{site}' — use: "
        f"chatgpt (Codex), gemini (Antigravity/agy), claude (Claude Code), "
        f"qwen (Qwen Code).")


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
    "prompt_style": "flag",
    "prompt_flag": ["-p"],
    "subcommand": [],
    # Official headless docs: "-c / --continue resumes the MOST RECENT
    # conversation" (the JSON envelope's conversation_id works too, via
    # --conversation <id>).
    "continue_style": "flag",
    "continue_args": ["--continue"],
    # Official headless docs: agy has NO --yolo;
    # --dangerously-skip-permissions sets the permission mode to
    # "always-proceed" — the headless equivalent of gemini --yolo.
    "yolo_args": ["--dangerously-skip-permissions"],
    # --output-format json -> one JSON envelope {status, response, error, …}
    # on completion (diagnostics go to stderr, so stdout stays clean).
    "output": "json-envelope",
    # No per-invocation MCP allow-list flag -> scope per prompt by toggling
    # the run's servers in mcp_config.json before each launch.
    "mcp_scope": "config-file",
    "mcp_scope_note": ("per-prompt MCP scope: run's servers are toggled in "
                       "mcp_config.json before each launch (agy has no allow-list flag)"),
    # agy has no binary image paste; the model reads file paths with its
    # file tools, so the image reaches it as a path + instruction.
    "image": "prompt",
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
    "prompt_style": "flag",
    "prompt_flag": ["-p"],
    "subcommand": [],
    "continue_style": "flag",
    "continue_args": ["--resume", "latest"],
    "yolo_args": ["--yolo"],
    "output": "raw",
    "mcp_scope": "flag",
    "mcp_scope_note": "per-prompt MCP scope: --allowed-mcp-server-names allow-list",
    "image": "prompt",
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
    # a REAL session ID with no "latest" special case — the real "most
    # recent session" flag is the separate -c/--continue boolean.
    "prompt_style": "flag",
    "prompt_flag": ["-p"],
    "subcommand": [],
    "continue_style": "flag",
    "continue_args": ["--continue"],
    "yolo_args": ["--yolo"],
    "output": "raw",
    "mcp_scope": "flag",
    "mcp_scope_note": "per-prompt MCP scope: --allowed-mcp-server-names allow-list",
    "image": "prompt",
}

_CODEX_SPEC = {
    "bin": "codex",
    "name": "Codex CLI (OpenAI)",
    "install": "npm install -g @openai/codex   (or: brew install codex)",
    "auth": ("run `codex` once in your terminal and log in with your ChatGPT "
             "account (Plus/Pro) — the CLI reuses the subscription, no API key"),
    "settings": "~/.codex/config.toml",
    "mcp_tool": "codex mcp add <name> --command <command>",
    "model_hint": "e.g. gpt-5.6 — empty = account default",
    # `codex exec` takes the prompt as a POSITIONAL argument (or "-" from
    # stdin); -p is a profile flag, NOT a prompt flag — do not confuse.
    "prompt_style": "positional",
    "prompt_flag": [],
    "subcommand": ["exec"],
    # `codex exec resume --last` = continue the most recent exec session.
    "continue_style": "subcommand",
    "continue_args": ["resume", "--last"],
    # --yolo is the documented short alias of
    # --dangerously-bypass-approvals-and-sandbox.
    "yolo_args": ["--yolo"],
    # Outside a git repo (most batch folders) codex refuses without this.
    "extra_args": ["--skip-git-repo-check"],
    # -o/--output-last-message writes the final assistant message to a file
    # — the cleanest way to capture the answer (stdout is formatted TUI text
    # or --json JSONL events, both noisier).
    "output": "last-message-file",
    # MCP servers live in [mcp_servers.*] of config.toml; per-prompt scope =
    # -c mcp_servers.<name>.enabled=false launch overrides (the file itself
    # is never rewritten per prompt).
    "mcp_scope": "config-override",
    "mcp_scope_note": ("per-prompt MCP scope: -c mcp_servers.<name>.enabled=false "
                       "launch overrides (config.toml itself untouched per prompt)"),
    # Native image attachment.
    "image": "flag",
}

_CLAUDE_SPEC = {
    "bin": "claude",
    "name": "Claude Code",
    "install": "npm install -g @anthropic-ai/claude-code   (or: brew install claude-code)",
    "auth": ("run `claude` once in your terminal and sign in with your Claude "
             "account (Pro/Max) — no API key"),
    "settings": "~/.claude.json",
    "mcp_tool": "claude mcp add <name> -s user -- <command>",
    "model_hint": "e.g. opus, sonnet, claude-opus-4-8 — empty = account default",
    "prompt_style": "flag",
    "prompt_flag": ["-p"],
    "subcommand": [],
    # --continue = resume the most recent session; --resume takes an id.
    "continue_style": "flag",
    "continue_args": ["--continue"],
    "yolo_args": ["--dangerously-skip-permissions"],
    # --output-format json -> one JSON object {result, is_error, …}.
    "output": "json-result",
    # Per-launch --mcp-config <file> --strict-mcp-config: the file contains
    # exactly the servers this prompt may use (user's own + allowed run
    # servers), everything else is ignored — a hard per-prompt scope.
    "mcp_scope": "per-launch-file",
    "mcp_scope_note": ("per-prompt MCP scope: per-launch --mcp-config file with "
                       "--strict-mcp-config (only the allowed servers exist)"),
    # Claude Code reads image file paths with its Read tool (multimodal);
    # on Windows the path-in-prompt is the documented reliable route.
    "image": "prompt",
}

_SPECS = {
    "agy": _AGY_SPEC,
    "gemini": _GEMINI_SPEC,
    "qwen": _QWEN_SPEC,
    "codex": _CODEX_SPEC,
    "claude": _CLAUDE_SPEC,
}


def _cli_spec(client: str) -> dict:
    if client not in _SPECS:
        raise ToolError(f"Unknown terminal client '{client}' (known: {', '.join(_SPECS)}).")
    return _SPECS[client]


class TerminalCLIBot:
    def __init__(self, site: str, yolo: bool = True, new_chat: bool = True,
                 has_mcp_servers: bool = False, mcp_servers: dict | None = None,
                 client: str | None = None, max_wait_seconds: float = 300.0,
                 log=print, should_stop=None):
        self.site = site
        self.client = client or select_client(site)
        self.spec = _cli_spec(self.client)
        self.yolo = yolo
        self.new_chat = new_chat
        self.has_mcp_servers = has_mcp_servers
        self.mcp_servers = dict(mcp_servers or {})
        self.max_wait = max_wait_seconds
        self.log = log
        self._should_stop = should_stop or (lambda: False)
        # Scratch files (one per process, overwritten per prompt).
        self._codex_msg = os.path.join(tempfile.gettempdir(),
                                       f"sub-sweater-codex-{os.getpid()}.txt")
        self._claude_mcp_file = os.path.join(
            tempfile.gettempdir(), "subscription-sweater",
            f"claude-mcp-{os.getpid()}.json")

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        bin_path = shutil.which(self.spec["bin"])
        if not bin_path:
            raise ToolError(self._missing_client_error())
        self.log(f"Terminal mode: {self.spec['name']} "
                 f"({self.spec['bin']}) — one headless call per prompt, "
                 f"answers captured from stdout.")
        if not self.new_chat:
            cont = (" ".join(self.spec["continue_args"])
                    if self.spec["continue_style"] == "flag"
                    else f"{' '.join(self.spec['subcommand'] + self.spec['continue_args'])}")
            self.log(f"  (sessions: continuing the latest CLI session per run ({cont}))")
        if self.yolo:
            self.log(f"  (auto-approve: ON — tool calls run without confirmation "
                     f"({' '.join(self.spec['yolo_args'])}); CLI flag --no-yolo disables this)")
        else:
            self.log("  (auto-approve: OFF — prompts that need tool confirmation "
                     "may fail in headless mode)")
        if self.mcp_servers:
            self.log(f"  (MCP servers: {', '.join(self.mcp_servers)}) — {self.spec['mcp_scope_note']}")

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
        s = self.spec
        return (
            f"Terminal mode needs the {s['name']} client, but `{s['bin']}` "
            f"was not found on PATH.\n"
            f"  install:  {s['install']}\n"
            f"  login:    {s['auth']}\n"
            "(Then start this run again.)")

    # ------------------------------------------------------------------ #
    def build_command(self, prompt: str, decision=None, image_path: str = "") -> list:
        """The exact argv for one prompt (pure — unit-testable)."""
        s = self.spec
        text = prompt
        if image_path and s["image"] == "prompt":
            text += ("\n\n[Image for this prompt: " + image_path
                     + " — it is part of the question; read that file with your "
                       "file tools and analyze it.]")
        cmd = [s["bin"]] + list(s.get("subcommand") or [])
        if not self.new_chat and s.get("continue_style") == "subcommand":
            cmd += list(s["continue_args"])
        flags: list = []
        if decision is not None:
            if decision.target_model:
                flags += ["--model", decision.target_model]
            if s["output"] in ("json-envelope", "json-result"):
                flags += ["--output-format", "json"]
            if s["output"] == "last-message-file":
                flags += ["--output-last-message", self._codex_msg]
            if s["mcp_scope"] == "flag":
                if decision.mcp and decision.mcp_names:
                    for n in decision.mcp_names:          # repeated array flag
                        flags += ["--allowed-mcp-server-names", n]
                elif self.has_mcp_servers and not decision.mcp:
                    flags += ["--allowed-mcp-server-names", _EMPTY_ALLOWLIST]
            elif s["mcp_scope"] == "config-override" and self.mcp_servers:
                # Codex: disable the run's servers this prompt is NOT allowed.
                enabled = set(decision.mcp_names or ()) if decision.mcp else set()
                for n in self.mcp_servers:
                    if n not in enabled:
                        flags += ["-c", f"mcp_servers.{n}.enabled=false"]
            elif s["mcp_scope"] == "per-launch-file" and self.mcp_servers:
                f = self._claude_mcp_file_for(decision)
                if f:
                    flags += ["--mcp-config", f, "--strict-mcp-config"]
            if s["image"] == "flag" and image_path:
                flags += ["--image", image_path]
        if self.yolo:
            flags += list(s["yolo_args"])
        flags += list(s.get("extra_args") or [])
        if not self.new_chat and s.get("continue_style") == "flag":
            flags += list(s["continue_args"])
        if s["prompt_style"] == "flag":
            return cmd + list(s["prompt_flag"]) + [text] + flags
        return cmd + flags + [text]

    def _claude_mcp_file_for(self, decision) -> str:
        """Write the per-launch MCP config for Claude Code and return its
        path: the user's own servers (from ~/.claude.json, minus this
        run's) plus exactly the run's servers this prompt may use."""
        from . import mcp_config
        if decision is None:
            enabled = set(self.mcp_servers)
        else:
            enabled = set(decision.mcp_names or ()) if decision.mcp else set()
        user = {k: v for k, v in mcp_config.cli_user_mcp_servers("claude").items()
                if k not in self.mcp_servers}
        servers = {**user, **{k: v for k, v in self.mcp_servers.items()
                              if k in enabled}}
        try:
            os.makedirs(os.path.dirname(self._claude_mcp_file) or ".", exist_ok=True)
            with open(self._claude_mcp_file, "w", encoding="utf-8") as f:
                json.dump({"mcpServers": servers}, f, indent=2)
        except Exception as e:
            self.log(f"  (!) could not write the per-prompt MCP scope file: {e}")
            return ""
        return self._claude_mcp_file

    # ------------------------------------------------------------------ #
    def answer(self, prompt: str, decision=None) -> str:
        image_path = (getattr(decision, "image", "") or "") if decision else ""
        # agy has no per-invocation MCP allow-list flag: scope by toggling
        # the run's servers in the user-level mcp_config.json right before
        # launch (each `agy -p` is a fresh process that reads the file at
        # startup). The other clients scope via flags (see build_command).
        if self.client == "agy" and self.mcp_servers:
            from . import mcp_config
            enabled = set()
            if decision is not None and decision.mcp and decision.mcp_names:
                enabled = set(decision.mcp_names)
            mcp_config.set_cli_mcp_scope("agy", self.mcp_servers, enabled, log=self.log)
        if self.spec["output"] == "last-message-file":
            try:
                if os.path.exists(self._codex_msg):
                    os.remove(self._codex_msg)
            except OSError:
                pass
        cmd = self.build_command(prompt, decision, image_path=image_path)
        text_sent = prompt + _image_suffix(prompt, image_path, self.spec)
        shown = ["…" if c == text_sent else c for c in cmd]
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
        if self.spec["output"] == "last-message-file":
            ans = ""
            try:
                if os.path.exists(self._codex_msg):
                    ans = open(self._codex_msg, "r", encoding="utf-8").read().strip()
            except OSError:
                pass
            if ans:
                return ans
            if out:
                return out                     # formatted fallback
            raise ToolError(f"{self.spec['name']} returned no output "
                            f"{_tail(err)}{_auth_hint(err, self.spec)}")
        if not out:
            raise ToolError(f"{self.spec['name']} returned no output "
                            f"{_tail(err)}{_auth_hint(err, self.spec)}")
        if self.spec["output"] in ("json-envelope", "json-result"):
            env = _parse_json_object(out)
            if env is not None:
                if self.spec["output"] == "json-envelope":
                    if env.get("status") == "SUCCESS":
                        return (env.get("response") or "").strip()
                    raise ToolError(
                        f"{self.spec['name']} ended with status {env.get('status')}: "
                        f"{env.get('error') or _tail(out)}")
                else:  # claude json-result
                    if env.get("is_error"):
                        raise ToolError(f"{self.spec['name']} failed: "
                                        f"{env.get('result') or env.get('error') or _tail(out)}")
                    if "result" in env:
                        return str(env.get("result") or "").strip()
        return out

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        pass


def _image_suffix(prompt: str, image_path: str, spec: dict) -> str:
    """The exact text build_command appends for prompt-style image clients
    (kept in one place so the log-masking logic stays in sync)."""
    if image_path and spec["image"] == "prompt":
        return ("\n\n[Image for this prompt: " + image_path
                + " — it is part of the question; read that file with your "
                  "file tools and analyze it.]")
    return ""


def _parse_json_object(out: str):
    """Parse a single-JSON-object stdout (agy envelope / claude result).
    Returns the dict, or None when stdout is not that object (plain text)
    so the caller can fall back to raw output."""
    t = (out or "").strip()
    if not t.startswith("{"):
        return None
    try:
        data = json.loads(t)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


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
