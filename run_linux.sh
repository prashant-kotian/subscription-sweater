#!/bin/bash
# Linux launcher.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Run install_linux.sh first."
  exit 1
fi
. .venv/bin/activate
python main.py
