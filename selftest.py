#!/usr/bin/env python3
"""Offline self-test for the LLM Batch Tool.

Creates sample input files (.txt/.xlsx/.docx/.csv), checks the tool-policy
engine (web search / MCP / skills, directives, deny lists), checks terminal
mode (official Gemini CLI / Qwen Code clients — command building, MCP settings
merge, and a full run against a stub CLI), then runs a MOCK batch into each
output format and verifies the round-trip. No browser, no LLM, no network,
no real CLI installs needed:

    python selftest.py
"""
from __future__ import annotations

import os
import sys
import tempfile

# Same Windows cp1252-vs-UTF8 fix as main.py -- this file's own final banner
# uses a checkmark that otherwise crashes the selftest with a false failure
# after every real check has already passed.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm_batch import readers, writers  # noqa: E402
from llm_batch.engine import Engine, RunConfig  # noqa: E402
from llm_batch.policy import ToolPolicy, decide  # noqa: E402

SAMPLE_PROMPTS = [
    "What is the capital of France?",
    "Explain TCP in one short paragraph.\nKeep it under 50 words.",
    "List 3 fruits, one per line.",
    'Write a haiku about robots.',
]

SERVERS = {
    "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
    "files": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
}


# --------------------------------------------------------------------------- #
def test_policy() -> None:
    quiet = lambda *a, **k: None  # noqa: E731

    # auto: web search only when the prompt looks real-time / lookup-y
    d = decide(ToolPolicy(web_search="auto"), "Search the web for today's gold price", quiet)
    assert d.web_search is True, "auto should detect web intent"
    d = decide(ToolPolicy(web_search="auto"), "What is the capital of France?", quiet)
    assert d.web_search is False, "auto should not force web search"

    # never wins over intent; always wins over nothing
    d = decide(ToolPolicy(web_search="never"), "search the web for X", quiet)
    assert d.web_search is False and d.force_off_web
    assert any("do NOT use web search" in i for i in d.instructions)
    d = decide(ToolPolicy(web_search="always"), "Write a haiku", quiet)
    assert d.web_search is True and any("use web search" in i for i in d.instructions)

    # per-prompt directives override the run policy (and are stripped)
    d = decide(ToolPolicy(web_search="always"),
               "@@websearch: off\nWhat's the weather in Mumbai?", quiet)
    assert d.web_search is False and d.force_off_web
    assert "@@" not in d.clean_prompt and "Mumbai" in d.clean_prompt

    # deny list is a hard constraint (beats directives + policy)
    d = decide(ToolPolicy(web_search="always", denied="websearch"),
               "@@websearch: on\nlatest news", quiet)
    assert d.web_search is False and d.force_off_web

    # MCP: always + allow list + per-server deny
    d = decide(ToolPolicy(mcp="always", mcp_servers=SERVERS), "do it", quiet)
    assert d.mcp and d.mcp_names == ["github", "files"]
    d = decide(ToolPolicy(mcp="always", mcp_servers=SERVERS, allowed_mcp="github"), "do it", quiet)
    assert d.mcp_names == ["github"]
    d = decide(ToolPolicy(mcp="always", mcp_servers=SERVERS, denied="mcp:github"), "do it", quiet)
    assert d.mcp and d.mcp_names == ["files"]
    d = decide(ToolPolicy(mcp="always", mcp_servers=SERVERS, denied="mcp"), "do it", quiet)
    assert d.mcp is False and d.force_off_mcp

    # MCP auto: on when the prompt mentions a configured server name or "mcp"
    d = decide(ToolPolicy(mcp="auto", mcp_servers=SERVERS), "push a commit via the github server", quiet)
    assert d.mcp is True and d.mcp_names == ["github", "files"]
    d = decide(ToolPolicy(mcp="auto", mcp_servers=SERVERS), "write a poem", quiet)
    assert d.mcp is False
    # directive selects a subset
    d = decide(ToolPolicy(mcp="always", mcp_servers=SERVERS),
               "@@mcp: use: files\ntask", quiet)
    assert d.mcp and d.mcp_names == ["files"]

    # enabled but nothing available -> off
    d = decide(ToolPolicy(mcp="always"), "use tools", quiet)
    assert d.mcp is False

    # skills: run-level + directive + deny
    d = decide(ToolPolicy(skills="pdf"), "summarize", quiet)
    assert d.skills == ["pdf"] and any("skill" in i.lower() for i in d.instructions)
    d = decide(ToolPolicy(skills="pdf"), "@@skill: excel\nsummarize", quiet)
    assert d.skills == ["pdf", "excel"]
    d = decide(ToolPolicy(skills="pdf", denied="skill:pdf"), "summarize", quiet)
    assert d.skills == []

    # no instructions when injection is off
    d = decide(ToolPolicy(web_search="always", inject_instructions=False), "x", quiet)
    assert d.web_search is True and d.instructions == []

    # model: run-level, per-prompt override, and empty default
    d = decide(ToolPolicy(model="GPT-4o"), "hi", quiet)
    assert d.target_model == "GPT-4o" and "model=GPT-4o" in d.summary()
    d = decide(ToolPolicy(model="GPT-4o"), "@@model: GPT-5\nhi", quiet)
    assert d.target_model == "GPT-5"
    d = decide(ToolPolicy(model="Qwen3-Max"), "@@model: qwen-long\nhi", quiet)
    assert d.target_model == "qwen-long"
    d = decide(ToolPolicy(), "hi", quiet)
    assert d.target_model == ""

    print("  policy  : web-search / MCP / skills / model decisions  OK")


# --------------------------------------------------------------------------- #
def test_marked(tmp: str) -> None:
    """Marked-block extraction: benchmark files wrap the real prompt between
    >>> PASTE BELOW >>> / <<< PASTE ABOVE <<< markers; only the marked part
    may go to the LLM, and the ID line above the marker is kept as a label."""
    mk = os.path.join(tmp, "marked.txt")
    with open(mk, "w", encoding="utf-8") as f:
        f.write(
            "GZ-01\n"
            "[reference only, do not paste -- source: secret gold data]\n\n"
            ">>> PASTE BELOW >>>\n"
            "Prompt one text.\n"
            "<<< PASTE ABOVE <<<\n\n"
            "==== BATCH HEADER — also must not be sent ====\n\n"
            "GZ-02\n"
            "[reference only, do not paste -- more secrets]\n\n"
            ">>> PASTE BELOW >>>\n"
            "Prompt two\n"
            "second line.\n"
            "<<< PASTE ABOVE <<<\n"
        )
    ps, labels = readers.read_prompts_labeled(mk)
    assert ps == ["Prompt one text.", "Prompt two\nsecond line."], ps
    assert labels == ["GZ-01", "GZ-02"], labels
    assert all("reference only" not in p for p in ps)
    assert all("BATCH HEADER" not in p for p in ps)

    # …and the IDs survive into a labeled txt output
    out = os.path.join(tmp, "marked_answers.txt")
    Engine(RunConfig(mode="mock", input_path=mk, output_path=out),
           log=lambda m: None, progress=lambda d, t: None).run()
    with open(out, encoding="utf-8") as f:
        content = f.read()
    assert "PROMPT 1 [GZ-01]" in content and "PROMPT 2 [GZ-02]" in content, content[:400]
    assert "secret gold data" not in content
    w = writers.make_writer(out, log=lambda m: None)
    assert w.count() == 2
    w.close()
    print("  marked  : marker extraction + labeled output  OK")


# --------------------------------------------------------------------------- #
def test_images(tmp: str) -> None:
    """Auto-attach by prompt label + explicit @@image: directive."""
    import os as _os

    d = _os.path.join(tmp, "imgs")
    _os.makedirs(d, exist_ok=True)
    for name in ("GZ-01_chart.png", "GZ-03_photo.jpg"):
        with open(_os.path.join(d, name), "wb") as f:
            f.write(b"\x89PNG fake")

    mk = _os.path.join(d, "prompts.txt")
    with open(mk, "w", encoding="utf-8") as f:
        f.write(
            "GZ-01\n>>> PASTE BELOW >>>\nChart question one.\n<<< PASTE ABOVE <<<\n\n"
            "GZ-02\n>>> PASTE BELOW >>>\nPlain text question.\n<<< PASTE ABOVE <<<\n\n"
            "GZ-03\n>>> PASTE BELOW >>>\n@@image: GZ-03_photo.jpg\nQuestion with directive.\n"
            "<<< PASTE ABOVE <<<\n"
        )
    logs: list = []
    out = _os.path.join(tmp, "img_answers.txt")
    Engine(RunConfig(mode="mock", input_path=mk, output_path=out, images_dir=d),
           log=logs.append, progress=lambda a, b: None).run()
    joined = "\n".join(logs)
    assert "(mock: image attached: GZ-01_chart.png)" in joined, joined
    assert "(mock: image attached: GZ-03_photo.jpg)" in joined, joined  # directive
    assert joined.count("image attached") == 2, joined  # GZ-02 gets none

    # explicit directive pointing at a missing file is announced, not fatal
    mk2 = _os.path.join(d, "prompts2.txt")
    with open(mk2, "w", encoding="utf-8") as f:
        f.write("GZ-09\n>>> PASTE BELOW >>>\n@@image: no_such_file.png\nQ\n<<< PASTE ABOVE <<<\n")
    logs2: list = []
    Engine(RunConfig(mode="mock", input_path=mk2, output_path=_os.path.join(tmp, "a2.txt"),
                     images_dir=d), log=logs2.append, progress=lambda a, b: None).run()
    assert "image not found: no_such_file.png" in "\n".join(logs2)
    print("  images  : auto-attach by label + @@image directive  OK")


# --------------------------------------------------------------------------- #
def test_terminal(tmp: str, txt: str) -> None:
    import json as json_mod

    out_term = os.path.join(tmp, "answers_terminal.txt")
    from llm_batch import mcp_config
    from llm_batch.cli_bot import CLI_SITES, TerminalCLIBot
    from llm_batch.errors import ToolError
    from llm_batch.policy import PromptDecision

    quiet = lambda *a, **k: None  # noqa: E731

    # -- build_command: exact argv per policy decision ----------------------- #
    bot = TerminalCLIBot(site="gemini", log=quiet)
    d = PromptDecision(clean_prompt="hello")
    cmd = bot.build_command("hello", d)
    assert cmd[:3] == ["gemini", "-p", "hello"] and "--yolo" in cmd, cmd

    d = PromptDecision(clean_prompt="hello", target_model="gemini-2.5-pro")
    assert bot.build_command("hello", d)[3:5] == ["--model", "gemini-2.5-pro"]

    d = PromptDecision(clean_prompt="hello", mcp=True, mcp_names=["github", "files"])
    cmd = bot.build_command("hello", d)
    i = cmd.index("--allowed-mcp-server-names")
    assert cmd[i + 1] == "github" and cmd.count("--allowed-mcp-server-names") == 2, cmd

    # mcp=OFF prompt in a run that HAS servers → allow-list matches nothing
    bot_srv = TerminalCLIBot(site="gemini", has_mcp_servers=True, log=quiet)
    d = PromptDecision(clean_prompt="hello", mcp=False)
    cmd = bot_srv.build_command("hello", d)
    i = cmd.index("--allowed-mcp-server-names")
    assert cmd[i + 1] == "__subscription_sweater_no_mcp__", cmd
    # …but a run with no servers configured → no allow-list flag at all
    cmd = TerminalCLIBot(site="qwen", log=quiet).build_command("hello",
                                                               PromptDecision(clean_prompt="h"))
    assert "--allowed-mcp-server-names" not in cmd, cmd

    # resume flag is real and site-specific: gemini's --resume documents
    # "latest" as a special value; qwen's --resume takes a real session ID
    # with no such case, so qwen uses the separate --continue flag instead.
    bot_nc_g = TerminalCLIBot(site="gemini", new_chat=False, log=quiet)
    assert bot_nc_g.build_command("h", PromptDecision(clean_prompt="h"))[-2:] == ["--resume", "latest"]
    bot_nc_q = TerminalCLIBot(site="qwen", new_chat=False, log=quiet)
    assert bot_nc_q.build_command("h", PromptDecision(clean_prompt="h"))[-1:] == ["--continue"]

    for s in CLI_SITES:
        assert TerminalCLIBot(site=s, log=quiet).spec["bin"] in ("gemini", "qwen")
    print("  terminal  : command builder (model / mcp scoping / resume)  OK")

    # -- MCP settings merge (fake HOME, never the real one) ------------------ #
    home = os.path.join(tmp, "fakehome")
    os.makedirs(os.path.join(home, ".gemini"), exist_ok=True)
    gpath = os.path.join(home, ".gemini", "settings.json")
    with open(gpath, "w", encoding="utf-8") as f:
        json_mod.dump({"theme": "dark",
                       "mcpServers": {"existing": {"command": "echo"}}}, f)
    old_home = os.environ.get("HOME"), os.environ.get("USERPROFILE")
    os.environ["HOME"] = home
    if old_home[1]:
        os.environ["USERPROFILE"] = home
    try:
        assert mcp_config.cli_settings_path("gemini") == gpath
        assert mcp_config.cli_settings_path("qwen").endswith(os.path.join(".qwen", "settings.json"))
        p = mcp_config.ensure_cli_mcp_servers("gemini", SERVERS, log=quiet)
        assert p == gpath
        data = json_mod.load(open(gpath, encoding="utf-8"))
        assert data["theme"] == "dark"                        # user keys untouched
        assert set(data["mcpServers"]) == {"existing", "github", "files"}
        backups = [x for x in os.listdir(os.path.join(home, ".gemini"))
                   if x.startswith("settings.json.bak-")]
        assert backups, "original must be backed up before merge"
        mcp_config.ensure_cli_mcp_servers("gemini", SERVERS, log=quiet)  # idempotent
    finally:
        os.environ["HOME"] = old_home[0]
        if old_home[1]:
            os.environ["USERPROFILE"] = old_home[1]
    print("  terminal  : MCP settings merge (backup + user keys kept)  OK")

    # -- missing client → actionable error, not a crash ----------------------- #
    empty = os.path.join(tmp, "empty"); os.makedirs(empty, exist_ok=True)
    old_path = os.environ["PATH"]
    os.environ["PATH"] = empty
    try:
        try:
            TerminalCLIBot(site="gemini", log=quiet).start()
            raise AssertionError("expected ToolError for missing client")
        except ToolError as e:
            assert "npm install -g @google/gemini-cli" in str(e), str(e)
    finally:
        os.environ["PATH"] = old_path
    print("  terminal  : missing-client error message  OK")

    # -- end-to-end with a stub 'gemini' on PATH (posix only) ----------------- #
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        print("  terminal  : e2e stub skipped (posix only)")
        return
    bin_dir = os.path.join(tmp, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    stub = os.path.join(bin_dir, "gemini")
    with open(stub, "w", encoding="utf-8") as f:
        f.write('#!/bin/sh\nprintf "%s\\n" "$@" > "$STUB_LOG"\n'
                'printf "STUB ANSWER: %s\\n" "$2"\n')
    os.chmod(stub, 0o755)
    stub_log = os.path.join(tmp, "stub_args.txt")
    os.environ["PATH"] = bin_dir + os.pathsep + old_path
    os.environ["STUB_LOG"] = stub_log
    os.environ["HOME"] = home                      # MCP merge must not touch the real one
    try:
        Engine(
            RunConfig(mode="terminal", site="gemini", input_path=txt,
                      output_path=out_term, web_search="never", mcp="always",
                      mcp_servers=SERVERS, delay_between=0.0),
            log=lambda m: None, progress=lambda d, t: None,
        ).run()
        w = writers.make_writer(out_term, log=lambda m: None)
        n = w.count(); w.close()
        assert n == len(SAMPLE_PROMPTS), f"terminal e2e: expected {len(SAMPLE_PROMPTS)}, got {n}"
        calls = open(stub_log, encoding="utf-8").read()
        assert "--yolo" in calls and "--allowed-mcp-server-names" in calls, calls
        g = json_mod.load(open(gpath, encoding="utf-8"))
        assert "github" in g["mcpServers"]
    finally:
        os.environ["PATH"] = old_path
        os.environ["HOME"] = old_home[0]
        if old_home[1]:
            os.environ["USERPROFILE"] = old_home[1]
        os.environ.pop("STUB_LOG", None)
    print(f"  terminal  : e2e run via stub CLI ({n} answers, flags verified)  OK")


def make_inputs(tmp: str):
    txt = os.path.join(tmp, "prompts.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write("\n\n".join(SAMPLE_PROMPTS))

    xlsx = os.path.join(tmp, "prompts.xlsx")
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["#", "Prompt"])
    for i, p in enumerate(SAMPLE_PROMPTS, 1):
        ws.append([i, p])
    wb.save(xlsx)

    docx = os.path.join(tmp, "prompts.docx")
    import docx as docx_mod

    d = docx_mod.Document()
    for p in SAMPLE_PROMPTS:
        d.add_paragraph(p)
    d.save(docx)

    csv = os.path.join(tmp, "prompts.csv")
    import csv as csv_mod

    with open(csv, "w", newline="", encoding="utf-8") as f:
        w = csv_mod.writer(f)
        w.writerow(["prompt"])
        for p in SAMPLE_PROMPTS:
            w.writerow([p])

    return txt, xlsx, docx, csv


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="llm_batch_selftest_")
    print(f"sample inputs written to: {tmp}\n")

    # 1 · tool policy --------------------------------------------------------
    test_policy()
    test_marked(tmp)
    test_images(tmp)

    # 2 · reading ----------------------------------------------------------
    txt, xlsx, docx, csv = make_inputs(tmp)
    for name, src in [("txt", txt), ("xlsx", xlsx), ("docx", docx), ("csv", csv)]:
        got = readers.read_prompts(src)
        assert got == SAMPLE_PROMPTS, f"{name}: expected {len(SAMPLE_PROMPTS)} prompts, got {len(got)}: {got!r}"
        print(f"  read  {name:5s}: {len(got)} prompts  OK")

    # 3a · terminal mode (official Gemini CLI / Qwen Code clients) ----------- #
    test_terminal(tmp, txt)

    # 3 · mock run into every output format (with tool policy active) --------
    for ext in ["txt", "xlsx", "docx"]:
        out = os.path.join(tmp, f"answers.{ext}")
        Engine(
            RunConfig(mode="mock", input_path=txt, output_path=out,
                      web_search="always", mcp="always", mcp_servers=SERVERS,
                      skills="pdf"),
            log=lambda m: None, progress=lambda d, t: None,
        ).run()
        w = writers.make_writer(out, log=lambda m: None)
        n = w.count()
        w.close()
        assert n == len(SAMPLE_PROMPTS), f"{ext}: expected {len(SAMPLE_PROMPTS)} answers, got {n}"
        print(f"  write {ext:5s}: {n} answers (policy applied)  OK")

    # 4 · resume: a second run must not duplicate ----------------------------
    out = os.path.join(tmp, "answers.txt")
    Engine(RunConfig(mode="mock", input_path=txt, output_path=out),
           log=lambda m: None, progress=lambda d, t: None).run()
    w = writers.make_writer(out, log=lambda m: None)
    n = w.count()
    w.close()
    assert n == len(SAMPLE_PROMPTS), f"resume: expected {len(SAMPLE_PROMPTS)} answers, got {n}"
    print(f"  resume        : no duplicates  OK")

    print("\nSELFTEST PASSED ✔  (you can now run:  python main.py  to open the GUI)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
