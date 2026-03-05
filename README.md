# Ceramic Stamps Processor

A web-based tool for extracting individual motifs from scanned pages of Lithuanian ceramic stamp patterns, converting them to clean SVGs, and generating 3D-printable `.3mf` stamp files.

## Features

- **Visual region selection** — Draw rectangles or circles on uploaded images to select motifs
- **Move & delete selections** — Drag to reposition, right-click to remove
- **EPS/vector support** — Upload EPS, PS, or AI files with automatic high-DPI extraction
- **Erase tool** — Brush-based eraser to clean up unwanted dots or artifacts
- **Fill holes** — Automatically fill interior gaps in extracted patterns
- **Invert** — Swap black/white for patterns on dark backgrounds
- **SVG export** — Download clean vector SVGs traced with potrace
- **3D stamp generation** — Generate `.3mf` files via OpenSCAD for 3D printing ceramic debossing stamps
- **Non-blocking** — All processing runs in background threads; keep selecting while stamps generate
- **Smart filenames** — Output files named after the source image (e.g., `IMG-3570-extract-1.3mf`)

## Prerequisites

- Python 3.9+
- [potrace](http://potrace.sourceforge.net/) — `brew install potrace`
- [OpenSCAD](https://openscad.org/) — nightly build recommended (2024+), install to `/Applications/OpenSCAD.app`
- [Ghostscript](https://www.ghostscript.com/) — required for EPS support — `brew install ghostscript`

## Setup

```bash
cd processor
bash setup.sh
```

## Usage

```bash
cd processor
.venv/bin/python stamp_tool.py
```

Open http://localhost:8800 in your browser.

1. Click **Choose File** to upload an image (JPG, PNG, EPS)
2. Draw rectangle or circle selections around motifs
3. Each selection auto-extracts to an SVG preview
4. Use **Erase**, **Invert**, or **Fill Holes** to refine
5. Click **Download SVG** or **Generate .3mf** for output

## Pipeline

`png2stamp.sh` converts a PNG motif to a 3D-printable stamp:

```
PNG → PBM → potrace SVG → OpenSCAD SCAD → STL → .3mf
```

The stamp has a handle (print face-up) with a base plate that follows the design outline, so only the pattern imprints into clay.

## Project Structure

```
ceramic-stamps/
├── png2stamp.sh          # CLI: PNG → 3D stamp (.3mf)
├── processor/
│   ├── setup.sh          # One-time venv + dependency setup
│   ├── stamp_tool.py     # Python HTTP server + processing API
│   └── index.html        # Single-page web frontend
```
