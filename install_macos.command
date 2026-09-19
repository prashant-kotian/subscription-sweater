#!/bin/bash
# macOS installer — double-click me (or run from Terminal).
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null; then
  echo "python3 not found. Install it from https://www.python.org/downloads/ and try again."
  exit 1
fi
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
echo
echo "Install finished. Now double-click  run_macos.command  to start the tool."
echo "(If macOS later asks about Accessibility control, allow it for your terminal.)"
