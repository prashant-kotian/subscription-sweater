#!/bin/bash
# macOS launcher — double-click me (or run from Terminal).
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Run install_macos.command first."
  exit 1
fi
. .venv/bin/activate
python main.py
