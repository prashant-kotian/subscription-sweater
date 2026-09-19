#!/bin/bash
# Linux installer.
cd "$(dirname "$0")"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
# If the browser fails to start, also run (needs sudo):
#   python -m playwright install-deps chromium
echo
echo "Install finished. Now run  ./run_linux.sh  to start the tool."
