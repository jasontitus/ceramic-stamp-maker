#!/bin/bash
# png2stamp.sh - Convert a PNG image to a 3D-printable ceramic debossing stamp
#
# Usage: ./png2stamp.sh input.png [output_base] [size_mm]
#
# Produces a .3mf file ready to open in Bambu Studio with all print
# settings pre-configured. Print face UP for sharpest detail.
#
# The base plate follows the design outline so ONLY the pattern
# imprints into clay — no blank edges or corners.
#
# Dependencies: python3 + Pillow, potrace, openscad

set -euo pipefail

# ===================== STAMP PARAMETERS =====================
DESIGN_DEPTH=2.8       # mm - raised feature height (2.5-3.0 recommended)
BASE_THICKNESS=6       # mm - solid plate behind design (prevents flex)
BASE_OFFSET=1.5        # mm - structural margin around design outline
HANDLE_HEIGHT=20       # mm - handle length
HANDLE_D=12            # mm - handle shaft diameter
BEVEL=0.2              # mm - edge chamfer for clean clay release
# =============================================================

usage() {
    echo "Usage: $0 <input.png> [output_base] [size_mm]"
    echo ""
    echo "  input.png    Black-on-white (or transparent) PNG of your design"
    echo "  output_base  Base name for outputs (default: <input>_stamp)"
    echo "  size_mm      Design face size in mm (default: 18 for ~1.8cm)"
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
INPUT_PNG="$1"
[[ ! -f "$INPUT_PNG" ]] && echo "Error: File not found: $INPUT_PNG" && exit 1

INPUT_PNG="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
BASENAME="$(basename "$INPUT_PNG" .png)"
OUT_DIR="$(dirname "$INPUT_PNG")"
STAMP_SIZE="${3:-18}"

OUTPUT_BASE="${2:-${OUT_DIR}/${BASENAME}_stamp}"
OUTPUT_BASE="${OUTPUT_BASE%.3mf}"
OUTPUT_BASE="${OUTPUT_BASE%.stl}"

OUTPUT_STL="${OUTPUT_BASE}.stl"
OUTPUT_3MF="${OUTPUT_BASE}.3mf"
OUTPUT_SVG="${OUTPUT_BASE}.svg"
OUTPUT_SCAD="${OUTPUT_BASE}.scad"

# ── Check dependencies ──
# Prefer nightly OpenSCAD (much faster CGAL) over homebrew version
if [[ -x "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD" ]]; then
    OPENSCAD="/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"
else
    OPENSCAD="openscad"
fi

MISSING=()
command -v python3  &>/dev/null || MISSING+=("python3")
command -v potrace  &>/dev/null || MISSING+=("potrace")
command -v "$OPENSCAD" &>/dev/null || MISSING+=("openscad")
if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "Error: Missing dependencies: ${MISSING[*]}"
    exit 1
fi
python3 -c "from PIL import Image" 2>/dev/null || {
    echo "Error: Python Pillow required. Install with: pip3 install Pillow"
    exit 1
}

# ── Temp dir ──
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "=== Ceramic Debossing Stamp Generator ==="
echo "  Input:  $INPUT_PNG"
echo "  Output: $OUTPUT_3MF"
echo "  Face:   ${STAMP_SIZE}mm (~$(echo "scale=1; $STAMP_SIZE/10" | bc)cm)"
echo ""

# ── Step 1: Flatten alpha, convert to monochrome PBM ──
echo "[1/5] Preparing image..."
python3 - "$INPUT_PNG" "$WORK/input.pbm" << 'PYEOF'
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

# ── Step 2: Trace to SVG with potrace, then set exact mm size ──
echo "[2/5] Tracing design with potrace..."
# 1) Trace at natural size (no -W/-H — avoids potrace stretching)
potrace "$WORK/input.pbm" \
    --svg \
    --tight \
    -o "$OUTPUT_SVG"

# 2) Rewrite SVG width/height to target mm, preserving aspect ratio.
#    The viewBox (path coordinates) stays untouched — zero distortion.
DIMS=$(python3 - "$OUTPUT_SVG" "$STAMP_SIZE" << 'PYSVG'
import re, sys
svg = open(sys.argv[1]).read()
stamp = float(sys.argv[2])
vb = re.search(r'viewBox="([^"]+)"', svg).group(1).split()
w, h = float(vb[2]), float(vb[3])
if w >= h:
    mw, mh = stamp, stamp * h / w
else:
    mw, mh = stamp * w / h, stamp
svg = re.sub(r'width="[^"]+"', f'width="{mw}mm"', svg, count=1)
svg = re.sub(r'height="[^"]+"', f'height="{mh}mm"', svg, count=1)
open(sys.argv[1], 'w').write(svg)
print(f"{mw:.4f} {mh:.4f}")
PYSVG
)
DESIGN_W=$(echo "$DIMS" | cut -d' ' -f1)
DESIGN_H=$(echo "$DIMS" | cut -d' ' -f2)
echo "  Design: ${DESIGN_W} x ${DESIGN_H} mm (native proportions, no resize)"
echo "  Saved: $OUTPUT_SVG"

# ── Step 3: Generate OpenSCAD model ──
echo "[3/5] Building 3D stamp model..."

SVG_FILE="$(basename "$OUTPUT_SVG")"

cat > "$OUTPUT_SCAD" << OPENSCAD
// ============================================
// Ceramic Debossing Stamp
// Generated by png2stamp.sh
//
// PRINT FACE UP for sharpest edges.
// Base plate follows design outline so ONLY
// the pattern imprints into clay.
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

// Computed
max_dim        = max(design_w, design_h);
cone_top_d     = max_dim + base_offset * 2 + 1;
transition_h   = (cone_top_d - handle_d) / 2;
palm_h         = 3;
shaft_h        = handle_height - transition_h - palm_h;

z_base         = handle_height;
z_design       = z_base + base_thick;

\$fn = 80;

// --- 2D design shape (imported at exact mm size from potrace, no resize) ---
// Mirrored so the imprint in clay reads correctly.
// Remove mirror() if your design is symmetric or pre-mirrored.
module design_2d() {
    mirror([1, 0, 0])
        import("${SVG_FILE}", center = true);
}

// === HANDLE (bottom, sits on build plate) ===

// Palm-press pad (wider base for stability)
cylinder(h = palm_h, d1 = handle_d + 4, d2 = handle_d);

// Shaft
translate([0, 0, palm_h])
    cylinder(h = shaft_h, d = handle_d);

// Cone transition toward base
translate([0, 0, palm_h + shaft_h])
    cylinder(h = transition_h, d1 = handle_d, d2 = cone_top_d);

// === BASE PLATE (exact design outline) ===
translate([0, 0, z_base])
    linear_extrude(height = base_thick)
        design_2d();

// === DESIGN FEATURES (top, stamp face UP) ===
translate([0, 0, z_design]) {
    // Main body
    linear_extrude(height = design_depth - bevel, convexity = 10)
        design_2d();

    // Beveled top edge: slightly inset for clean clay release
    translate([0, 0, design_depth - bevel])
        linear_extrude(height = bevel, convexity = 10)
        offset(delta = -bevel)
        design_2d();
}
OPENSCAD

echo "  Saved: $OUTPUT_SCAD"

# ── Step 4: Render STL ──
echo "[4/5] Rendering STL (this may take a moment)..."
"$OPENSCAD" -o "$OUTPUT_STL" "$OUTPUT_SCAD" 2>&1 | grep -v "^$" || true

if [[ ! -f "$OUTPUT_STL" || ! -s "$OUTPUT_STL" ]]; then
    echo "Error: STL rendering failed."
    echo "Debug by opening $OUTPUT_SCAD in OpenSCAD."
    exit 1
fi

# ── Step 5: Package as Bambu Studio .3mf project ──
echo "[5/5] Packaging Bambu Studio project (.3mf)..."

python3 - "$OUTPUT_STL" "$OUTPUT_3MF" << 'PY3MF'
import sys, struct, zipfile

stl_path = sys.argv[1]
out_3mf  = sys.argv[2]

# ── Parse STL (auto-detect ASCII vs binary) ──
vert_map = {}
verts = []
tris = []

def add_vertex(x, y, z):
    key = (round(x, 6), round(y, 6), round(z, 6))
    if key not in vert_map:
        vert_map[key] = len(verts)
        verts.append(key)
    return vert_map[key]

with open(stl_path, 'rb') as f:
    head = f.read(80)
    is_ascii = head.strip().startswith(b'solid') and b'\x00' not in head

if is_ascii:
    with open(stl_path, 'r') as f:
        tri_verts = []
        for line in f:
            line = line.strip()
            if line.startswith('vertex'):
                parts = line.split()
                idx = add_vertex(float(parts[1]), float(parts[2]), float(parts[3]))
                tri_verts.append(idx)
                if len(tri_verts) == 3:
                    tris.append(tuple(tri_verts))
                    tri_verts = []
else:
    with open(stl_path, 'rb') as f:
        f.read(80)
        n_tri = struct.unpack('<I', f.read(4))[0]
        for _ in range(n_tri):
            f.read(12)  # skip normal
            tri_idx = []
            for _ in range(3):
                x, y, z = struct.unpack('<3f', f.read(12))
                tri_idx.append(add_vertex(x, y, z))
            tris.append(tuple(tri_idx))
            f.read(2)

print(f"  Mesh: {len(verts)} vertices, {len(tris)} triangles")

# ── Build 3D/3dmodel.model XML ──
v_lines = []
for v in verts:
    v_lines.append(f'        <vertex x="{v[0]:.6f}" y="{v[1]:.6f}" z="{v[2]:.6f}"/>')
t_lines = []
for t in tris:
    t_lines.append(f'        <triangle v1="{t[0]}" v2="{t[1]}" v3="{t[2]}"/>')

model_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US"
  xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
  xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
  xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06"
  requiredextensions="p">
  <metadata name="BambuStudio:3mfVersion" value="1"/>
  <resources>
    <object id="2" type="model">
      <mesh>
        <vertices>
{chr(10).join(v_lines)}
        </vertices>
        <triangles>
{chr(10).join(t_lines)}
        </triangles>
      </mesh>
    </object>
  </resources>
  <build p:UUID="e9e25302-6382-11e8-a1c0-00055d171cb2">
    <item objectid="2" p:UUID="e9e25304-6382-11e8-a1c0-00055d171cb2"
          transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/>
  </build>
</model>'''

content_types = '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>'''

rels = '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel0"
    Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>'''

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

with zipfile.ZipFile(out_3mf, 'w', zipfile.ZIP_DEFLATED) as zf:
    zf.writestr('[Content_Types].xml', content_types)
    zf.writestr('_rels/.rels', rels)
    zf.writestr('3D/3dmodel.model', model_xml)
    zf.writestr('Metadata/Slic3r_PE.config', process_config)
    zf.writestr('Metadata/model_settings.config', model_settings)
    zf.writestr('Metadata/project_settings.config', plate_config)

print(f"  Saved: {out_3mf}")
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
    echo "  Design:       ${DESIGN_W} x ${DESIGN_H}mm, ${DESIGN_DEPTH}mm raised"
    echo "  Base plate:   follows design outline + ${BASE_OFFSET}mm margin"
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
