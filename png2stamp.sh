#!/bin/bash
# png2stamp.sh - Convert PNG/SVG artwork to a 3D-printable ceramic stamp
#
# Usage: ./png2stamp.sh input.png [output_base] [width_mm] [height_mm|auto] [total_height_mm] [auto|rectangular|round] [raised|concave] [thicken_mm|auto]
#
# Produces a .3mf file ready to open in Bambu Studio with all print
# settings pre-configured. Print face UP for sharpest detail.
#
# Raised faces press the artwork into clay; concave faces leave raised artwork.
# Concave faces include a surrounding pressing surface and a solid cavity floor.
#
# Dependencies: python3 + Pillow, potrace, openscad

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# The server supplies its interpreter; direct CLI calls use the project venv.
PYTHON="${STAMP_PYTHON:-$SCRIPT_DIR/processor/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
    echo "Project Python missing. Run: bash \"$SCRIPT_DIR/processor/setup.sh\"" >&2
    exit 1
fi
for brew_bin in /opt/homebrew/bin /usr/local/bin; do
    if [[ -x "$brew_bin/brew" ]]; then
        export PATH="$brew_bin:$PATH"
        break
    fi
done


usage() {
    echo "Usage: $0 <input.png|input.svg> [output_base] [width_mm] [height_mm|auto] [total_height_mm] [auto|rectangular|round] [raised|concave] [thicken_mm|auto]"
    echo ""
    echo "  input         Black-on-white or transparent artwork"
    echo "  output_base   Base name for outputs (default: <input>_stamp)"
    echo "  width_mm      Overall body width, or round diameter, 12..200 (default: 36)"
    echo "  height_mm     Overall body height, 12..200; auto derives it (default: auto)"
    echo "  total_height  Entire stamp including grip and 2.8mm relief, 8..60 (default: 22)"
    echo "  body_shape    auto (artwork proportions), rectangular, or round"
    echo "                rectangular requires a height; round uses its diameter"
    echo "  mode          raised (default): recessed artwork in clay"
    echo "                concave: raised artwork in clay, enclosed cavity and floor"
    echo "  thicken_mm    Printability offset, 0..2, or auto (default: auto)"
    echo ""
    echo "Produces:"
    echo "  *_stamp.3mf   - Bambu Studio project (open directly, settings included)"
    echo "  *_stamp.stl   - plain STL (for other slicers)"
    echo "  *_stamp.scad  - OpenSCAD source (tweak and re-export)"
    echo "  *_stamp.svg   - traced vector design"
    exit 1
}

# ── Parse arguments ──
[[ $# -lt 1 || $# -gt 8 ]] && usage
INPUT_FILE="$1"
[[ ! -f "$INPUT_FILE" ]] && echo "Error: File not found: $INPUT_FILE" && exit 1

INPUT_FILE="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
INPUT_EXT="${INPUT_FILE##*.}"
BASENAME="$(basename "$INPUT_FILE" ".$INPUT_EXT")"
OUT_DIR="$(dirname "$INPUT_FILE")"
WIDTH_MM="${3:-36}"
HEIGHT_MM="${4:-auto}"
TOTAL_HEIGHT_MM="${5:-22}"
BODY_SHAPE="${6:-auto}"
STAMP_MODE="${7:-raised}"
THICKEN_ARG="${8:-auto}"
case "$STAMP_MODE" in
    raised|concave) ;;
    *) echo "Error: mode must be raised or concave" >&2; exit 1 ;;
esac
case "$BODY_SHAPE" in
    auto|rectangular|round) ;;
    *) echo "Error: body_shape must be auto, rectangular or round" >&2; exit 1 ;;
esac
# When input is SVG, skip bitmap conversion and potrace (use SVG directly)
SVG_INPUT=false
if [[ "$INPUT_EXT" == "svg" || "$INPUT_EXT" == "SVG" ]]; then
    SVG_INPUT=true
fi

OUTPUT_BASE="${2:-${OUT_DIR}/${BASENAME}_stamp}"
OUTPUT_BASE="${OUTPUT_BASE%.3mf}"
OUTPUT_BASE="${OUTPUT_BASE%.stl}"
mkdir -p "$(dirname "$OUTPUT_BASE")"
OUTPUT_BASE="$(cd "$(dirname "$OUTPUT_BASE")" && pwd)/$(basename "$OUTPUT_BASE")"

OUTPUT_STL="${OUTPUT_BASE}.stl"
OUTPUT_3MF="${OUTPUT_BASE}.3mf"
OUTPUT_SVG="${OUTPUT_BASE}.svg"
OUTPUT_SCAD="${OUTPUT_BASE}.scad"

# ── Check dependencies ──
# Preserve an explicit renderer override, otherwise prefer the installed app.
if [[ -z "${OPENSCAD:-}" ]]; then
    if [[ -x "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD" ]]; then
        OPENSCAD="/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"
    else
        OPENSCAD="openscad"
    fi
fi

MISSING=()
if ! $SVG_INPUT; then
    command -v potrace &>/dev/null || MISSING+=("potrace")
fi
command -v "$OPENSCAD" &>/dev/null || MISSING+=("openscad")
if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "Error: Missing dependencies: ${MISSING[*]}"
    exit 1
fi
"$PYTHON" -c "from PIL import Image" 2>/dev/null || {
    echo "Project Pillow missing. Run: bash \"$SCRIPT_DIR/processor/setup.sh\""
    exit 1
}

# ── Temp dir ──
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "=== Ceramic Stamp Generator (${STAMP_MODE}) ==="
echo "  Input:  $INPUT_FILE"
echo "  Output: $OUTPUT_3MF"
echo ""

if $SVG_INPUT; then
    # ── SVG input: skip bitmap conversion, use directly ──
    echo "[1/5] Using SVG directly (skipping bitmap conversion)..."
    if [[ "$INPUT_FILE" != "$OUTPUT_SVG" ]]; then
        cp "$INPUT_FILE" "$OUTPUT_SVG"
    fi
    echo "[2/5] Reading SVG dimensions..."
else
    # ── Step 1: Flatten alpha, convert to monochrome PBM ──
    echo "[1/5] Preparing image..."
    "$PYTHON" - "$INPUT_FILE" "$WORK/input.pbm" << 'PYEOF'
import sys
from pathlib import Path
from PIL import Image, ImageOps

if Path(sys.argv[1]).suffix.lower() in ('.heif', '.heic', '.hif', '.heifs', '.heics'):
    from pillow_heif import register_heif_opener
    register_heif_opener(thumbnails=False)

with Image.open(sys.argv[1]) as source:
    img = ImageOps.exif_transpose(source)

# Flatten transparency to white background
if img.mode in ('RGBA', 'LA', 'PA'):
    alpha = img.getchannel('A')
    if 'RGB' in img.mode:
        bg = Image.new('RGB', img.size, (255, 255, 255))
    else:
        bg = Image.new('L', img.size, 255)
    bg.paste(img, mask=alpha)
    img = bg

# Convert to 1-bit black and white
img = img.convert('1')

# Keep the original aspect ratio; potrace --tight trims only empty margins.
print(f"  {img.width}x{img.height}px")
img.save(sys.argv[2])
print(f"  Saved monochrome PBM")
PYEOF

    # ── Step 2: Trace to SVG with potrace ──
    echo "[2/5] Tracing design with potrace..."
    potrace "$WORK/input.pbm" \
        --svg \
        --tight \
        -o "$OUTPUT_SVG"
fi

# ── Step 3: Resolve overall dimensions and generate OpenSCAD model ──
# Shared with the server: no independent shell scaling or grip calculations.
echo "[3/5] Building 3D stamp model..."
"$PYTHON" "$SCRIPT_DIR/processor/stamp_geometry.py" \
    "$OUTPUT_SVG" "$OUTPUT_SCAD" "$WIDTH_MM" "$HEIGHT_MM" \
    "$TOTAL_HEIGHT_MM" "$BODY_SHAPE" "$STAMP_MODE" "$THICKEN_ARG"
echo "  Saved: $OUTPUT_SVG"

echo "  Saved: $OUTPUT_SCAD"

# ── Step 4: Render 3MF directly from OpenSCAD ──
echo "[4/5] Rendering 3MF (this may take a moment)..."

# Render fresh temporary outputs. A failed renderer must not package an old file.
#
# --hardwarnings is deliberately NOT used. OpenSCAD escalates its
# GeometryEvaluator cache notice ("Node didn't fit into cache") to a fatal error
# under that flag: detailed artwork exceeds the cache, the process exits 1, and
# NO output file is written even though the geometry itself rendered fine. That
# made large stamps fail outright. OpenSCAD's default warning behaviour renders
# the same geometry successfully. The -s checks below still fail on a genuine
# renderer error, where no output is produced.
"$OPENSCAD" -o "$WORK/stamp.3mf" "$OUTPUT_SCAD"
"$OPENSCAD" -o "$WORK/stamp.stl" "$OUTPUT_SCAD"
if [[ ! -s "$WORK/stamp.3mf" || ! -s "$WORK/stamp.stl" ]]; then
    echo "Error: 3MF or STL rendering failed." >&2
    echo "Debug by opening $OUTPUT_SCAD in OpenSCAD." >&2
    exit 1
fi

# ── Step 5: Inject Bambu Studio settings into the .3mf ──
echo "[5/5] Adding Bambu Studio print settings..."

"$PYTHON" - "$WORK/stamp.3mf" << 'PY3MF'
import sys, zipfile, os, tempfile, shutil

tmf_path = sys.argv[1]

# Bambu Studio metadata to inject
model_settings = '''<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="2">
    <metadata key="name" value="CeramicStamp"/>
    <part id="0">
    </part>
  </object>
</config>'''

# Optimised for ceramic debossing stamps printed FACE UP
process_config = """; generated by png2stamp.sh - Ceramic Stamp Profile
; Print FACE UP for sharpest raised edges

; === Quality ===
layer_height = 0.12
initial_layer_print_height = 0.2
line_width = 0.42
outer_wall_line_width = 0.42
inner_wall_line_width = 0.45
sparse_infill_line_width = 0.45
internal_solid_infill_line_width = 0.42
top_surface_line_width = 0.42

; === Walls (strong for pressing into clay) ===
wall_loops = 4
detect_thin_wall = 1

; === Infill (broad block with a continuous solid shell) ===
sparse_infill_density = 60%
sparse_infill_pattern = grid

; === Shell ===
top_shell_layers = 5
bottom_shell_layers = 5
top_surface_pattern = monotonicline
bottom_surface_pattern = monotonic

; === Support (none needed) ===
enable_support = 0

; === Speed (slower outer wall for sharper edges) ===
outer_wall_speed = 120

; === Other ===
elefant_foot_compensation = 0.1
print_sequence = by layer
bridge_flow = 0.95
"""

plate_config = """; generated by png2stamp.sh
"""

# Read existing .3mf, add Bambu settings, write back
tmp_fd, tmp_path = tempfile.mkstemp(suffix='.3mf')
os.close(tmp_fd)

with zipfile.ZipFile(tmf_path, 'r') as zin, \
     zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zout:
    # Copy all existing entries
    for item in zin.infolist():
        zout.writestr(item, zin.read(item.filename))
    # Add Bambu settings
    zout.writestr('Metadata/Slic3r_PE.config', process_config)
    zout.writestr('Metadata/model_settings.config', model_settings)
    zout.writestr('Metadata/project_settings.config', plate_config)

shutil.move(tmp_path, tmf_path)
print("  Added Bambu Studio metadata")
PY3MF

# Publish only after both renders and metadata packaging have succeeded.
mv "$WORK/stamp.stl" "$OUTPUT_STL"
mv "$WORK/stamp.3mf" "$OUTPUT_3MF"

if [[ -f "$OUTPUT_3MF" && -s "$OUTPUT_3MF" ]]; then
    STL_SIZE=$(du -h "$OUTPUT_STL" | cut -f1)
    TMF_SIZE=$(du -h "$OUTPUT_3MF" | cut -f1)

    echo ""
    echo "=== Done! ==="
    echo ""
    echo "  Bambu Studio project: $OUTPUT_3MF ($TMF_SIZE)"
    echo "  Also saved:  .stl ($STL_SIZE), .scad, .svg"
    echo ""
    echo "  Body:         ${BODY_SHAPE}; overall height ${TOTAL_HEIGHT_MM}mm"
    echo "  Face:         ${STAMP_MODE}; fixed 2.8mm relief"
    echo "  Backing:      continuous broad grip and solid floor"
    echo ""
    echo "=== Next Steps ==="
    echo "  1. Open $OUTPUT_3MF in Bambu Studio"
    echo "  2. Select your printer (A1 Mini) and filament (PLA)"
    echo "  3. Print settings are already configured:"
    echo "       Layer height: 0.12mm  |  Infill: 60%"
    echo "       Wall loops: 4         |  Supports: off"
    echo "       Outer wall speed: reduced for sharp edges"
    echo "  4. Print FACE UP (broad grip on build plate, design on top)"
    echo "     -> Sharpest raised edges, avoids elephant foot rounding"
    echo "  5. Optional: light CA glue seal after printing"
    echo ""
else
    echo "Error: 3MF packaging failed."
    exit 1
fi
