# Changelog

All notable changes to Subscription Sweater are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
