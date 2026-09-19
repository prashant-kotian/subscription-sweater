# Changelog

All notable changes to Subscription Sweater are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] — 2026-09-20

### Added
- **Terminal (CLI) mode** — the native MCP path for **Gemini and Qwen**:
  drives the official open-source clients headless (`gemini -p "…"` /
  `qwen -p "…"`, one call per prompt, `--model`, `--yolo` auto-approve,
  `--resume latest` for shared sessions) and captures the answer from stdout.
- **MCP settings merge for the CLI clients** — your MCP servers JSON is merged
  into `~/.gemini/settings.json` / `~/.qwen/settings.json` (same `mcpServers`
  shape as Claude desktop) with a timestamped backup, exactly like the Claude
  desktop config merge.
- **Per-prompt CLI allow-list** — terminal mode passes
  `--allowed-mcp-server-names` with the exact servers the policy decided for
  that prompt; an MCP-off prompt gets an allow-list that matches nothing, so
  it physically cannot call an MCP tool.
- GUI + web UI: Terminal mode with site restriction (Gemini/Qwen), model hints
  per client, install/login instructions in-place.
- `selftest.py`: terminal-mode coverage (command builder, settings merge with
  backup, missing-client error, full e2e run against a stub CLI).

### Changed
- `mcp_config.py` generalised: one merge engine for Claude desktop + Gemini
  CLI + Qwen Code.
- Terminal/browser model hints updated to the current line-ups
  (`gemini-3.1-pro-preview`, `gemini-3-flash-preview`, `qwen3-coder-next`, …).

### Fixed (reported by review, commit `cc95acf`)
- Windows cp1252 console: `UnicodeEncodeError` on em-dash/checkmark output in
  `main.py` (Ctrl+C handler, web-UI startup) and `selftest.py`'s success
  banner — stdout/stderr now forced to UTF-8 (`errors="replace"`) at both
  entry points.
- `mcp_config.cli_settings_path()` returned a mixed-separator path on Windows
  (`~` → backslash home, rest of the literal left as `/`) — wrapped in
  `os.path.normpath()`.

### Documented
- Known gap: ChatGPT + local MCP (web connectors accept public HTTPS only;
  no ChatGPT CLI to bridge through — see README "Tool control").

## [1.0.0] — 2026-09-19

### Added
- **Four ways to reach the LLM**: browser automation (ChatGPT / Claude /
  Gemini / Qwen), **official desktop-app automation** (ChatGPT / Claude via
  CDP attach), manual paste mode for *any* app (with window auto-focus), and
  mock mode for free pipeline testing.
- **Model selection** per run and per prompt (`@@model:`) — the tool drives the
  site's own model picker (fuzzy match, cached, warning-only fallback).
- **Tool control** (web search / MCP / skills): run-level policies
  (auto/always/never), per-prompt `@@` directives, hard deny lists, and
  triple enforcement (UI toggle → MCP config → appended instruction).
- **Connect any MCP server**: paste JSON in the web UI/GUI; for Claude desktop
  it's merged into `claude_desktop_config.json` with a timestamped backup.
- **Auto chart-image attach**: images named after the prompt ID
  (`GZ-15_chart.png` → prompt `GZ-15`) are attached via the site's paperclip
  (Playwright file-chooser interception); `@@image:` override.
- **Benchmark-sheet parsing**: files wrapped in
  `>>> PASTE BELOW >>> … <<< PASTE ABOVE <<<` markers send *only* the marked
  sections; the ID above each marker is kept as the answer's label
  (`PROMPT 37 [GZ-37]` in txt, column A in xlsx, heading in docx).
- **Three frontends**: local web UI (upload, toggles, live log via SSE,
  download; binds 127.0.0.1), Tkinter desktop GUI, and a full CLI.
- **Crash-proof pipeline**: every answer saved immediately (autosave per
  prompt), auto-resume on restart, pause/stop, `(FAILED)` rows, per-prompt
  timing, and `--debug` dumps (screenshot + HTML) on failure.
- **Formats**: input `.txt .csv .xlsx .docx(.doc)`; output `.txt .xlsx .docx`.
- **Offline self-test** (`selftest.py`): policy engine, marked-block parser,
  image attach, readers/writers, resume — no network, no accounts.
- GitHub-ready: CI (selftest on py3.10–3.13), issue/PR templates, dependabot.
