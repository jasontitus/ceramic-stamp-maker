#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "Setting up stamp tool..."

python3 -m venv .venv
.venv/bin/pip install --quiet opencv-python-headless numpy Pillow

command -v potrace >/dev/null || { echo "Error: potrace not found. Install with: brew install potrace"; exit 1; }

echo ""
echo "Ready! Run:  .venv/bin/python stamp_tool.py"
