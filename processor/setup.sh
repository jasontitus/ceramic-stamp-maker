#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

usage() {
    echo "Usage: bash processor/setup.sh [--install-system | --check]"
    echo "  default           Create/update .venv from requirements.txt, then check tools"
    echo "  --install-system  Also install missing native tools with Homebrew (macOS)"
    echo "  --check           Check versions, imports and tools without installing anything"
}
MODE="${1:-setup}"
case "$MODE" in
    setup|--install-system|--check) ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
esac
[[ $# -le 1 ]] || { usage >&2; exit 1; }

# Include Homebrew for launches from shells that have not loaded brew shellenv.
for brew_bin in /opt/homebrew/bin /usr/local/bin; do
    if [[ -x "$brew_bin/brew" ]]; then
        export PATH="$brew_bin:$PATH"
        break
    fi
done

if [[ "$MODE" == "--install-system" ]]; then
    if [[ "$(uname -s)" != "Darwin" ]] || ! command -v brew >/dev/null; then
        echo "Automatic native-tool installation requires macOS and Homebrew: https://brew.sh" >&2
        exit 1
    fi
    formulae=()
    command -v python3.13 >/dev/null || formulae+=(python@3.13)
    command -v potrace >/dev/null || formulae+=(potrace)
    command -v gs >/dev/null || formulae+=(ghostscript)
    command -v rsvg-convert >/dev/null || formulae+=(librsvg)
    if [[ ${#formulae[@]} -gt 0 ]]; then
        brew install "${formulae[@]}"
    fi
    if [[ ! -x /Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD ]] && ! command -v "${OPENSCAD:-openscad}" >/dev/null; then
        brew install --cask openscad@snapshot
    fi
fi

if [[ "$MODE" != "--check" ]]; then
    PYTHON="${PYTHON:-python3.13}"
    if ! command -v "$PYTHON" >/dev/null; then
        echo "Python 3.13 is required. On macOS: bash processor/setup.sh --install-system" >&2
        exit 1
    fi
    "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else "Use Python 3.13 to create the project environment.")'
    if [[ -e .venv ]] && ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))' 2>/dev/null; then
        backup=".venv.backup.$(date +%Y%m%d-%H%M%S).$$"
        mv .venv "$backup"
        echo "Preserved incompatible environment as processor/$backup"
    fi
    if [[ ! -x .venv/bin/python ]]; then
        "$PYTHON" -m venv .venv
    fi
    # Do not inherit unrelated global pip indexes or install into system Python.
    PIP_CONFIG_FILE=/dev/null PIP_INDEX_URL=https://pypi.org/simple PIP_EXTRA_INDEX_URL= \
        .venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
fi

if [[ ! -x .venv/bin/python ]]; then
    echo "Project environment is missing. Run: bash processor/setup.sh" >&2
    exit 1
fi
.venv/bin/python -m pip check
.venv/bin/python - <<'PY'
import importlib.metadata as metadata
import sys
from pathlib import Path

if sys.version_info[:2] != (3, 13):
    sys.exit("Project environment requires Python 3.13. Run: bash processor/setup.sh")
for line in Path("requirements.txt").read_text().splitlines():
    if not line or line.startswith("#"):
        continue
    name, expected = line.split("==")
    try:
        actual = metadata.version(name)
    except metadata.PackageNotFoundError:
        sys.exit(f"Missing {name}. Run: bash processor/setup.sh")
    if actual != expected:
        sys.exit(f"{name}: expected {expected}, found {actual}. Run: bash processor/setup.sh")
    print(f"  {name} {actual}")
for conflicting in ("opencv-python", "opencv-contrib-python", "opencv-contrib-python-headless"):
    try:
        metadata.version(conflicting)
    except metadata.PackageNotFoundError:
        continue
    sys.exit(f"Conflicting {conflicting} package in .venv. Move .venv aside and rerun setup.")
import cv2
import numpy as np
from PIL import Image
from pillow_heif import libheif_info, register_heif_opener
register_heif_opener(thumbnails=False)
# Exercise the compiled NumPy/OpenCV boundary, not just package metadata.
cv2.GaussianBlur(np.zeros((5, 5), dtype=np.uint8), (3, 3), 0)
Image.new("L", (1, 1))
print(f"  HEIF decoder: libheif {libheif_info()['libheif']}")
print(f"  Python {sys.version.split()[0]}: {sys.executable}")
PY

missing=()
for tool in potrace gs rsvg-convert; do
    command -v "$tool" >/dev/null || missing+=("$tool")
done
OPENSCAD="${OPENSCAD:-}"
if [[ -z "$OPENSCAD" ]]; then
    if [[ -x /Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD ]]; then
        OPENSCAD=/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD
    else
        OPENSCAD=openscad
    fi
fi
command -v "$OPENSCAD" >/dev/null || missing+=(openscad)
if [[ ${#missing[@]} -gt 0 ]]; then
    echo "Missing native tools: ${missing[*]}" >&2
    echo "macOS: bash processor/setup.sh --install-system" >&2
    echo "Linux: install potrace, ghostscript, librsvg2-bin and openscad with your package manager." >&2
    exit 1
fi
potrace --version
gs --version
rsvg-convert --version
"$OPENSCAD" --version

echo ""
echo "Setup checked. Start: bash processor/start.sh"
