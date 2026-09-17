#!/bin/bash
# png2stamp.sh - Convert PNG/SVG artwork to a 3D-printable ceramic stamp
#
# Usage: ./png2stamp.sh input.png [output_base] [size_mm] [thicken_mm] [raised|concave]
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

# ===================== STAMP PARAMETERS =====================
# These are reference values for a 36mm stamp; they scale for other sizes
REF_SIZE=36            # mm - reference stamp size for parameter scaling
REF_DESIGN_DEPTH=2.8   # mm - raised feature height at reference size
REF_BASE_THICKNESS=6   # mm - solid plate behind design
REF_BASE_OFFSET=1.5    # mm - structural margin around design outline
REF_BEVEL=0.2          # mm - edge chamfer for clay release
HANDLE_HEIGHT=22       # mm - handle length (~0.9in)
HANDLE_D=12            # mm - handle shaft diameter
# =============================================================

usage() {
    echo "Usage: $0 <input.png|input.svg> [output_base] [size_mm] [thicken_mm] [raised|concave]"
    echo ""
    echo "  input.png    Black-on-white (or transparent) PNG of your design"
    echo "  output_base  Base name for outputs (default: <input>_stamp)"
    echo "  size_mm      Design face size in mm (default: 18 for ~1.8cm)"
    echo "  thicken_mm   Override feature thickening in mm (default: auto)"
    echo "               Use 0.25 for thin-line sources like hatched artwork"
    echo "  mode         raised (default): recessed artwork in clay"
    echo "               concave: raised artwork in clay, with a surrounding impression"
    echo ""
    echo "Produces:"
    echo "  *_stamp.3mf   - Bambu Studio project (open directly, settings included)"
    echo "  *_stamp.stl   - plain STL (for other slicers)"
    echo "  *_stamp.scad  - OpenSCAD source (tweak and re-export)"
    echo "  *_stamp.svg   - traced vector design"
    exit 1
}

# ── Parse arguments ──
[[ $# -lt 1 ]] && usage
INPUT_FILE="$1"
[[ ! -f "$INPUT_FILE" ]] && echo "Error: File not found: $INPUT_FILE" && exit 1

INPUT_FILE="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
INPUT_EXT="${INPUT_FILE##*.}"
BASENAME="$(basename "$INPUT_FILE" ".$INPUT_EXT")"
OUT_DIR="$(dirname "$INPUT_FILE")"
STAMP_SIZE="${3:-18}"
STAMP_MODE="${5:-raised}"
case "$STAMP_MODE" in
    raised|concave) ;;
    *) echo "Error: mode must be raised or concave" >&2; exit 1 ;;
esac
# When input is SVG, skip bitmap conversion and potrace (use SVG directly)
SVG_INPUT=false
if [[ "$INPUT_EXT" == "svg" || "$INPUT_EXT" == "SVG" ]]; then
    SVG_INPUT=true
fi

# Scale parameters proportionally to stamp size (reference: 36mm)
# awk ensures proper decimal formatting (no leading-dot issues from bc)
DESIGN_DEPTH=$(awk "BEGIN{v=$REF_DESIGN_DEPTH*$STAMP_SIZE/$REF_SIZE; printf \"%.2f\", (v<1.2?1.2:v)}")
BASE_THICKNESS=$(awk "BEGIN{v=$REF_BASE_THICKNESS*$STAMP_SIZE/$REF_SIZE; printf \"%.2f\", (v<3?3:v)}")
BASE_OFFSET=$(awk "BEGIN{v=$REF_BASE_OFFSET*$STAMP_SIZE/$REF_SIZE; printf \"%.2f\", (v<0.8?0.8:v)}")
BEVEL=$(awk "BEGIN{v=$REF_BEVEL*$STAMP_SIZE/$REF_SIZE; printf \"%.2f\", (v<0.05?0.05:v)}")
# Minimum feature thickening: ensures features are wide enough to print.
# At small sizes, features scale below nozzle width (~0.42mm) and the slicer
# drops them. This offset (applied in stamp-mm AFTER scaling) compensates.
# Auto: 0 at 36mm+, ~0.15mm at 18mm, ~0.20mm at 12mm.
# Override: pass thicken_mm as 4th arg (e.g. 0.25 for thin-line sources).
THICKEN_ARG="${4:-auto}"
if [[ "$THICKEN_ARG" == "auto" ]]; then
    MIN_THICKEN=$(awk "BEGIN{v=0.30*(1-$STAMP_SIZE/36); printf \"%.2f\", (v<0?0:v)}")
else
    MIN_THICKEN="$THICKEN_ARG"
fi
OUTPUT_BASE="${2:-${OUT_DIR}/${BASENAME}_stamp}"
OUTPUT_BASE="${OUTPUT_BASE%.3mf}"
OUTPUT_BASE="${OUTPUT_BASE%.stl}"

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
command -v potrace  &>/dev/null || MISSING+=("potrace")
command -v "$OPENSCAD" &>/dev/null || MISSING+=("openscad")
command -v bc &>/dev/null || MISSING+=("bc")
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
echo "  Face:   ${STAMP_SIZE}mm (~$(echo "scale=1; $STAMP_SIZE/10" | bc)cm)"
echo ""

if $SVG_INPUT; then
    # ── SVG input: skip bitmap conversion, use directly ──
    echo "[1/5] Using SVG directly (skipping bitmap conversion)..."
    cp "$INPUT_FILE" "$OUTPUT_SVG"
    echo "[2/5] Reading SVG dimensions..."
else
    # ── Step 1: Flatten alpha, convert to monochrome PBM ──
    echo "[1/5] Preparing image..."
    "$PYTHON" - "$INPUT_FILE" "$WORK/input.pbm" << 'PYEOF'
import sys
from PIL import Image

img = Image.open(sys.argv[1])

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

# Pad to square canvas (centers design, prevents distortion on resize)
w, h = img.size
if w != h:
    side = max(w, h)
    padded = Image.new('1', (side, side), 1)  # white background
    padded.paste(img, ((side - w) // 2, (side - h) // 2))
    img = padded
    print(f"  {w}x{h}px -> padded to {side}x{side}px square")
else:
    print(f"  {w}x{h}px")

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

# Read native SVG size, compute design dimensions.
# Keep SVG at its native size so OpenSCAD imports at high resolution.
# SCAD scales to actual stamp size — preserves thin connections.
DIMS=$("$PYTHON" - "$OUTPUT_SVG" "$STAMP_SIZE" << 'PYSVG'
import re, sys
svg = open(sys.argv[1]).read()
stamp = float(sys.argv[2])
# Read native SVG dimensions (may be pt or mm)
wm = re.search(r'width="([\d.]+)\s*(pt|mm|)', svg)
hm = re.search(r'height="([\d.]+)\s*(pt|mm|)', svg)
raw_w, raw_h = float(wm.group(1)), float(hm.group(1))
unit = wm.group(2) if wm.group(2) else 'pt'
# Convert to mm for OpenSCAD
if unit == 'pt':
    svg_w_mm = raw_w * 0.3528
    svg_h_mm = raw_h * 0.3528
else:  # already mm
    svg_w_mm = raw_w
    svg_h_mm = raw_h
# Compute actual stamp dimensions preserving aspect ratio
if svg_w_mm >= svg_h_mm:
    mw, mh = stamp, stamp * svg_h_mm / svg_w_mm
else:
    mw, mh = stamp * svg_w_mm / svg_h_mm, stamp
print(f"{mw:.4f} {mh:.4f} {svg_w_mm:.4f} {svg_h_mm:.4f}")
PYSVG
)
DESIGN_W=$(echo "$DIMS" | cut -d' ' -f1)
DESIGN_H=$(echo "$DIMS" | cut -d' ' -f2)
SVG_W=$(echo "$DIMS" | cut -d' ' -f3)
SVG_H=$(echo "$DIMS" | cut -d' ' -f4)
echo "  Design: ${DESIGN_W} x ${DESIGN_H} mm (native proportions, no resize)"
echo "  Saved: $OUTPUT_SVG"

# ── Step 3: Generate OpenSCAD model ──
echo "[3/5] Building 3D stamp model..."

SVG_FILE="$(basename "$OUTPUT_SVG")"

cat > "$OUTPUT_SCAD" << OPENSCAD
// ============================================
// Ceramic Stamp (${STAMP_MODE})
// Generated by png2stamp.sh
//
// PRINT FACE UP for sharpest edges.
// Concave mode recesses the artwork into a solid surrounding face.
// Raised mode presses the artwork into the clay.
//
// To customize: edit parameters below, then
// re-export with: openscad -o output.stl this_file.scad
// ============================================

// --- Tuneable Parameters ---
design_w       = ${DESIGN_W};        // mm - design width  (aspect ratio preserved)
design_h       = ${DESIGN_H};        // mm - design height (aspect ratio preserved)
design_depth   = ${DESIGN_DEPTH};    // mm - raised feature height
base_thick     = ${BASE_THICKNESS};  // mm - solid plate (prevents flex)
base_offset    = ${BASE_OFFSET};     // mm - structural margin around design
handle_height  = ${HANDLE_HEIGHT};   // mm - total handle height
handle_d       = ${HANDLE_D};        // mm - handle shaft diameter
bevel          = ${BEVEL};           // mm - edge chamfer for clay release
stamp_mode     = "${STAMP_MODE}";

// Computed
max_dim        = max(design_w, design_h);
cone_top_d     = max_dim + base_offset * 2 + 1;
transition_h   = (cone_top_d - handle_d) / 2;
palm_h         = 3;
shaft_h        = handle_height - transition_h - palm_h;

z_base         = handle_height;
z_design       = z_base + base_thick;

\$fn = 80;

// --- 2D design shape ---
// SVG is imported at native size for precision, then scaled to stamp dimensions.
// A small offset (in stamp-mm) ensures features stay above nozzle width.
svg_w = ${SVG_W};
svg_h = ${SVG_H};
min_thicken = ${MIN_THICKEN};  // mm - printability offset (0 at 36mm+)

module design_2d() {
    offset(delta = min_thicken)   // thicken in stamp-mm for printability
    scale([design_w / svg_w, design_h / svg_h])
    mirror([1, 0, 0])
        import("${SVG_FILE}", center = true);
}

// A continuous pressing face around all artwork, including disconnected motifs.
// Keep the cavity enclosed and support islands inside holes with a solid floor.
module concave_outline_2d() {
    offset(r = base_offset)
        hull() design_2d();
}

// Small overlap at joints to ensure manifold mesh
e = 0.01;

union() {
    // === HANDLE (bottom, sits on build plate) ===

    // Palm-press pad (wider base for stability)
    cylinder(h = palm_h + e, d1 = handle_d + 4, d2 = handle_d);

    // Shaft
    translate([0, 0, palm_h])
        cylinder(h = shaft_h + e, d = handle_d);

    // Cone transition toward base
    translate([0, 0, palm_h + shaft_h])
        cylinder(h = transition_h + e, d1 = handle_d, d2 = cone_top_d);

    if (stamp_mode == "concave") {
        // Leave base_thick of solid material below the cavity.
        translate([0, 0, z_base])
            difference() {
                linear_extrude(height = base_thick + design_depth, convexity = 10)
                    concave_outline_2d();
                translate([0, 0, base_thick])
                    linear_extrude(height = design_depth + e, convexity = 10)
                        design_2d();
                // Widen the mouth, rather than narrowing it and trapping clay.
                translate([0, 0, base_thick + design_depth - bevel])
                    linear_extrude(height = bevel + e, convexity = 10)
                        offset(delta = bevel) design_2d();
            }
    } else {
        // === BASE PLATE (exact design outline) ===
        translate([0, 0, z_base])
            linear_extrude(height = base_thick + e)
                design_2d();

        // === DESIGN FEATURES (top, stamp face UP) ===
        translate([0, 0, z_design]) {
            linear_extrude(height = design_depth - bevel + e, convexity = 10)
                design_2d();
            translate([0, 0, design_depth - bevel])
                linear_extrude(height = bevel, convexity = 10)
                offset(delta = -bevel)
                design_2d();
        }
    }
}
OPENSCAD

echo "  Saved: $OUTPUT_SCAD"

# ── Step 4: Render 3MF directly from OpenSCAD ──
echo "[4/5] Rendering 3MF (this may take a moment)..."

# Export .3mf directly from OpenSCAD (Manifold backend produces clean mesh)
"$OPENSCAD" -o "$OUTPUT_3MF" "$OUTPUT_SCAD" 2>&1 | grep -v "^$" || true

# Also export STL for other slicers
"$OPENSCAD" -o "$OUTPUT_STL" "$OUTPUT_SCAD" 2>&1 | grep -v "^$" || true

if [[ ! -f "$OUTPUT_3MF" || ! -s "$OUTPUT_3MF" ]]; then
    echo "Error: 3MF rendering failed."
    echo "Debug by opening $OUTPUT_SCAD in OpenSCAD."
    exit 1
fi

# ── Step 5: Inject Bambu Studio settings into the .3mf ──
echo "[5/5] Adding Bambu Studio print settings..."

"$PYTHON" - "$OUTPUT_3MF" << 'PY3MF'
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

; === Infill (60% is sufficient with 4 walls + 6mm base) ===
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
print(f"  Saved: {tmf_path}")
PY3MF

if [[ -f "$OUTPUT_3MF" && -s "$OUTPUT_3MF" ]]; then
    STL_SIZE=$(du -h "$OUTPUT_STL" | cut -f1)
    TMF_SIZE=$(du -h "$OUTPUT_3MF" | cut -f1)
    TOTAL_H=$(echo "$DESIGN_DEPTH + $BASE_THICKNESS + $HANDLE_HEIGHT" | bc)

    echo ""
    echo "=== Done! ==="
    echo ""
    echo "  Bambu Studio project: $OUTPUT_3MF ($TMF_SIZE)"
    echo "  Also saved:  .stl ($STL_SIZE), .scad, .svg"
    echo ""
    echo "  Design:       ${DESIGN_W} x ${DESIGN_H}mm, ${DESIGN_DEPTH}mm relief (${STAMP_MODE})"
    echo "  Thicken:      ${MIN_THICKEN}mm"
    echo "  Base plate:   ${BASE_THICKNESS}mm solid backing"
    echo "  Bevel:        ${BEVEL}mm edge chamfer"
    echo "  Total height: ${TOTAL_H}mm"
    echo ""
    echo "=== Next Steps ==="
    echo "  1. Open $OUTPUT_3MF in Bambu Studio"
    echo "  2. Select your printer (A1 Mini) and filament (PLA)"
    echo "  3. Print settings are already configured:"
    echo "       Layer height: 0.12mm  |  Infill: 60%"
    echo "       Wall loops: 4         |  Supports: off"
    echo "       Outer wall speed: reduced for sharp edges"
    echo "  4. Print FACE UP (handle on build plate, design on top)"
    echo "     -> Sharpest raised edges, avoids elephant foot rounding"
    echo "  5. Optional: light CA glue seal after printing"
    echo ""
else
    echo "Error: 3MF packaging failed."
    exit 1
fi
