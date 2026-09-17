#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "Project environment missing. Run: bash \"$SCRIPT_DIR/setup.sh\" --install-system" >&2
    exit 1
fi
for brew_bin in /opt/homebrew/bin /usr/local/bin; do
    if [[ -x "$brew_bin/brew" ]]; then
        export PATH="$brew_bin:$PATH"
        break
    fi
done
exec "$PYTHON" "$SCRIPT_DIR/stamp_tool.py" "$@"
