#!/usr/bin/env python3
"""Subscription Sweater — send a list of prompts (txt/Excel/Word) to ChatGPT, Claude,
Gemini (browser, desktop app, or any app) and collect the answers in a fresh
file. Control web search / MCP / skills per run or per prompt.

  GUI:   python main.py
  CLI:   python main.py --site chatgpt -i prompts.txt -o answers.txt
  Help:  python main.py --help
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Windows' console defaults Python's stdout/stderr to the legacy code page
# (cp1252), which raises UnicodeEncodeError on the em-dashes this file's own
# log lines use (confirmed live crash on Ctrl+C and on the web-UI startup
# line) -- force UTF-8 so a real batch's interrupt handler can't itself crash
# instead of reporting "progress saved". No-op on platforms already UTF-8.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

CLI_FLAGS = {
    "--mode", "--site", "--url", "--in", "-i", "--out", "-o", "--headless",
    "--txt-mode", "--delay", "--stable", "--max-wait", "--login-timeout",
    "--min-chars", "--new-chat", "--no-new-chat", "--start", "--limit",
    "--auto-send", "--no-auto-send", "--timer", "--debug", "--list",
    "--desktop-exe", "--desktop-port", "--attach-only", "--focus-window",
    "--websearch", "--mcp", "--allow-mcp", "--deny", "--skills",
    "--instruct", "--no-instruct", "--directives", "--no-directives",
    "--mcp-json", "--web", "--port", "--host", "--model", "--images-dir",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="llm-batch-tool",
        description="Run a list of prompts (txt/xlsx/docx) through ChatGPT / "
                    "Claude / Gemini (browser, desktop app, or any LLM app) "
                    "and save the answers.",
    )
    p.add_argument("--mode", choices=["browser", "desktop", "terminal", "manual", "mock"],
                   default="browser",
                   help="browser = drive the chat site in Chromium; "
                        "desktop = drive the ChatGPT/Claude desktop app; "
                        "terminal = drive the official Gemini CLI / Qwen Code "
                        "(site gemini|qwen — their native MCP path); "
                        "manual = you focus the app, tool pastes/copies; "
                        "mock = offline self-test with fake answers")
    p.add_argument("--site", choices=["chatgpt", "claude", "gemini", "qwen", "custom"],
                   default="chatgpt")
    p.add_argument("--model", default="",
                   help='model to use, e.g. "GPT-4o", "Opus", "Qwen3-Max" '
                        '(empty = the app\'s default; a per-prompt @@model: line overrides it)')
    p.add_argument("--images-dir", default="",
                   help="folder with chart images for auto-attach "
                        "(<prompt-ID>.png is attached to that prompt; empty = input file's folder; "
                        "a per-prompt @@image: line overrides it)")
    p.add_argument("--url", default="", help="chat site URL (custom mode)")
    p.add_argument("-i", "--in", dest="input", default="",
                   help="input file with prompts (.txt/.csv/.xlsx/.docx)")
    p.add_argument("-o", "--out", dest="output", default="",
                   help="output file (.txt/.xlsx/.docx)")
    p.add_argument("--headless", action="store_true",
                   help="browser mode: no visible browser (profile must already be logged in)")
    p.add_argument("--txt-mode", choices=["auto", "marked", "blank-line", "line"],
                   default="auto",
                   help="auto = detect; marked = only >>> PASTE BELOW >>> blocks; "
                        "blank-line = blank line separates prompts; line = one per line")
    # desktop mode
    p.add_argument("--desktop-exe", default="",
                   help="desktop app executable (empty = auto-detect)")
    p.add_argument("--desktop-port", type=int, default=9222,
                   help="CDP debug port used to attach to the desktop app")
    p.add_argument("--attach-only", action="store_true",
                   help="desktop mode: app is already running with the flag; just attach")
    # terminal mode
    p.add_argument("--no-yolo", dest="yolo", action="store_false", default=True,
                   help="terminal mode: don't auto-approve tool calls "
                        "(prompts needing confirmation may fail headless)")
    # manual mode
    p.add_argument("--focus-window", default="",
                   help="manual mode: window title to auto-focus before pasting")
    # timing / run
    p.add_argument("--delay", type=float, default=2.0, help="seconds between prompts")
    p.add_argument("--stable", type=float, default=6.0,
                   help="seconds the answer must stay unchanged before it is captured")
    p.add_argument("--max-wait", type=float, default=300.0,
                   help="max seconds to wait for one answer")
    p.add_argument("--login-timeout", type=float, default=300.0,
                   help="max seconds to wait for you to log in on first run")
    p.add_argument("--min-chars", type=int, default=20)
    p.add_argument("--new-chat", dest="new_chat", action="store_true", default=True)
    p.add_argument("--no-new-chat", dest="new_chat", action="store_false")
    p.add_argument("--start", type=int, default=0,
                   help="1-based prompt number to start from (0 = auto-resume)")
    p.add_argument("--limit", type=int, default=0, help="max prompts to run (0 = all)")
    p.add_argument("--auto-send", dest="auto_send", action="store_true", default=True)
    p.add_argument("--no-auto-send", dest="auto_send", action="store_false")
    p.add_argument("--timer", type=float, default=0.0,
                   help="manual mode: fixed wait seconds instead of click steps")
    p.add_argument("--debug", action="store_true",
                   help="save screenshots/HTML to ./debug when something fails")
    p.add_argument("--list", action="store_true",
                   help="list the prompts that would be sent, then exit")
    # tool policy: web search / MCP / skills
    p.add_argument("--websearch", choices=["auto", "always", "never"], default="auto",
                   help="web search policy (auto = only when the prompt asks for it)")
    p.add_argument("--mcp", choices=["never", "auto", "always"], default="never",
                   help="MCP tools policy")
    p.add_argument("--allow-mcp", default="",
                   help="comma list of allowed MCP server names (empty = all)")
    p.add_argument("--deny", default="",
                   help="hard-off list: websearch, mcp, mcp:github, skill:pdf, …")
    p.add_argument("--skills", default="", help="comma list of skills to use")
    p.add_argument("--instruct", dest="instruct", action="store_true", default=True,
                   help="append tool instructions to the prompt (default)")
    p.add_argument("--no-instruct", dest="instruct", action="store_false")
    p.add_argument("--directives", dest="directives", action="store_true", default=True,
                   help="honor @@ directives inside the prompt file (default)")
    p.add_argument("--no-directives", dest="directives", action="store_false")
    p.add_argument("--mcp-json", default="",
                   help='MCP servers JSON: \'{"github": {"command": "npx", "args": [...]}}\'')
    # web UI
    p.add_argument("--web", action="store_true",
                   help="start the web UI (one browser tab for everything)")
    p.add_argument("--port", type=int, default=8321, help="web UI port (default 8321)")
    p.add_argument("--host", default="127.0.0.1",
                   help="web UI bind address (default 127.0.0.1 — local only; "
                        "use 0.0.0.0 to allow other devices)")
    return p


def run_web(args: argparse.Namespace) -> int:
    import threading
    import webbrowser

    from llm_batch.webui import create_app

    app = create_app()
    local = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{local}:{args.port}"
    print(f"Subscription Sweater — web UI:  {url}")
    print("Everything runs on this machine. Ctrl+C stops the server "
          "(any running batch keeps its saved progress).")
    if args.host != "0.0.0.0":
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)
    return 0


def run_cli(args: argparse.Namespace) -> int:
    from llm_batch.engine import Engine, RunConfig
    from llm_batch import readers
    from llm_batch.errors import ToolError

    if not args.input:
        print("ERROR: --in prompts_file is required (or run without flags for the GUI).")
        return 2

    if args.list:
        prompts = readers.read_prompts(args.input, args.txt_mode)
        print(f"{len(prompts)} prompts in {args.input}:")
        for i, p in enumerate(prompts, 1):
            print(f"  {i:4d}  {p[:110].replace(chr(10), ' / ')}")
        return 0

    mcp_servers = {}
    if args.mcp_json:
        try:
            mcp_servers = json.loads(args.mcp_json)
        except json.JSONDecodeError as e:
            print(f"ERROR: --mcp-json is not valid JSON: {e}")
            return 2

    cfg = RunConfig(
        mode=args.mode, site=args.site, custom_url=args.url, model=args.model,
        images_dir=args.images_dir,
        input_path=args.input, output_path=args.output,
        headless=args.headless, txt_mode=args.txt_mode,
        desktop_exe=args.desktop_exe, desktop_port=args.desktop_port,
        desktop_attach_only=args.attach_only, focus_window=args.focus_window,
        yolo=args.yolo,
        delay_between=args.delay, stable_seconds=args.stable,
        max_wait_seconds=args.max_wait, login_timeout=args.login_timeout,
        min_answer_chars=args.min_chars, new_chat=args.new_chat,
        start_index=args.start, limit=args.limit, auto_send=args.auto_send,
        timer_seconds=args.timer, debug=args.debug,
        web_search=args.websearch, mcp=args.mcp, mcp_servers=mcp_servers,
        allowed_mcp=args.allow_mcp, denied=args.deny, skills=args.skills,
        inject_instructions=args.instruct, use_directives=args.directives,
        profile_dir=os.path.join(BASE_DIR, "browser_profile"),
        debug_dir=os.path.join(BASE_DIR, "debug"),
    )
    hooks = None
    if args.mode == "manual":
        from llm_batch.manual_bot import TerminalHooks

        hooks = TerminalHooks()

    engine = Engine(cfg, log=lambda m: print(m, flush=True),
                    progress=lambda d, t: print(f"    [{d}/{t} done]"), hooks=hooks)
    try:
        engine.run()
        return 0
    except ToolError as e:
        print(f"\nERROR: {e}")
        return 1
    except KeyboardInterrupt:
        engine.stop()
        print("\nInterrupted — progress saved; re-run to resume.")
        return 130


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    effective_argv = list(sys.argv[1:] if argv is None else argv)

    if any(a in CLI_FLAGS for a in effective_argv):
        if args.web:
            return run_web(args)
        return run_cli(args)

    # GUI
    try:
        import tkinter as tk
    except ImportError:
        print("Tkinter is not available on this Python. Use the CLI instead, e.g.:\n"
              "  python main.py --site chatgpt -i prompts.txt -o answers.txt")
        return 2
    from llm_batch.gui import App

    try:
        root = tk.Tk()
    except tk.TclError as e:
        if "display" in str(e).lower():
            print("No graphical display was found, so the GUI cannot open here.\n"
                  "Use the CLI instead, e.g.:\n"
                  "  python main.py --site chatgpt -i prompts.txt -o answers.txt")
            return 2
        raise
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
