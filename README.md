# Ceramic Stamps Processor

A web-based tool for extracting symbols, shapes, and motifs from images or vector files, converting them to clean SVGs, and generating 3D-printable `.3mf` ceramic stamps.

## Features

- **Visual region selection** — Draw rectangles or circles on uploaded images to select motifs
- **Move & delete selections** — Drag to reposition, right-click to remove
- **EPS/vector support** — Upload EPS, PS, or AI files with automatic high-DPI extraction
- **Erase tool** — Brush-based eraser to clean up unwanted dots or artifacts
- **Fill holes** — Automatically fill interior gaps in extracted patterns
- **Invert** — Swap black/white for patterns on dark backgrounds
- **SVG export** — Download clean vector SVGs traced with potrace
- **3D stamp generation** — Generate `.3mf` files via OpenSCAD with a raised face for recessed artwork in clay, or a concave face for raised artwork in clay
- **Fuzzing** — Locally smooth outlines and remove fine details before SVG export or stamp generation; no API token required
- **Non-blocking** — All processing runs in background threads; keep selecting while stamps generate
- **Smart filenames** — Output files named after the source image (e.g., `IMG-3570-extract-1.3mf`)

## Setup

On macOS, install [Homebrew](https://brew.sh) first. Then, from the repository root:

```bash
bash processor/setup.sh --install-system
bash processor/start.sh
```

The installer:

- Installs **missing** native tools with Homebrew; it does not upgrade existing converters.
- Creates `processor/.venv` using Python **3.13**, and installs the exact versions in `processor/requirements.txt` from PyPI.
- Preserves an incompatible old environment as `processor/.venv.backup.<timestamp>.<pid>` instead of deleting it.
- Checks package versions, conflicting OpenCV packages, the NumPy/OpenCV binary interface, and all required converters.

No environment activation, global `pip install`, Conda environment, or API token is needed. The start script and standalone converter use the project environment; server-launched conversions use the server's exact interpreter.

If native tools are already installed, use `bash processor/setup.sh`. An alternative Python 3.13 executable can be selected with `PYTHON=/path/to/python3.13 bash processor/setup.sh`.

### Native dependencies

| Tool | Used for | macOS package |
| --- | --- | --- |
| Python 3.13 | Isolated application runtime | `python@3.13` |
| potrace | Bitmap → SVG tracing | `potrace` |
| Ghostscript (`gs`) | EPS/PS/AI rasterization through Pillow | `ghostscript` |
| `rsvg-convert` | SVG upload previews | `librsvg` |
| OpenSCAD | SVG → printable geometry | `openscad@snapshot` cask |
| `bc` | Converter dimension calculations | Included with macOS; `bc` if missing |

On Linux, install Python 3.13 with venv/pip support plus `potrace`, `ghostscript`, `librsvg2-bin`, `openscad`, and `bc` through your distribution's package manager, then run `bash processor/setup.sh`. Automatic native installation is macOS-only.

The renderer defaults to `/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD`, then `openscad` on PATH. Set `OPENSCAD=/path/to/openscad` to override it for setup checks, startup, or CLI conversion. The verified renderer is OpenSCAD 2026.03.01 with Manifold support; older distribution builds may render more slowly.

New macOS installations use the snapshot cask: Homebrew disabled the old `openscad` 2021.01 cask in September 2026 because it fails Gatekeeper checks. Setup leaves an existing working renderer alone and never bypasses Gatekeeper.

### Check, repair, and update

```bash
bash processor/setup.sh --check   # Read-only dependency health check; no installation
bash processor/setup.sh           # Reapply the pinned package versions
bash processor/start.sh 8801      # Optional alternate server port
```

For a fresh environment, stop the server, move `processor/.venv` outside that path, and rerun setup. Old `.venv.backup.*` directories can be removed once the replacement is working; they are not used by the application. Virtual environments contain absolute paths, so recreate `.venv` after relocating the repository.

To upgrade Python packages intentionally, update the exact pins in `processor/requirements.txt`, rerun setup, and exercise PNG/JPEG/EPS/SVG uploads, extraction, fuzzing, and both stamp modes before keeping the upgrade. Install only `opencv-python-headless`, not another package that also provides `cv2`.

For standalone image helpers, use the same environment, for example `cd processor && .venv/bin/python extract_motifs.py`.

Native tools remain managed by Homebrew, independently of the Python pins. Check them with `brew outdated potrace ghostscript librsvg python@3.13`; use targeted `brew upgrade` commands only when wanted. Manage an existing manually installed OpenSCAD app through its own distribution.

### Verified version selection

The September 2026 cleanup standardized the project on Python **3.13**, OpenCV **4.14.0.94** (headless), NumPy **2.5.3**, and Pillow **12.3.0**. Two earlier environments were present: the working Anaconda environment used Python 3.9.18 / OpenCV 4.10.0 / NumPy 1.23.5 / Pillow 10.4.0, while the preserved project `.venv` used Python 3.9.18 / OpenCV 4.13.0 / NumPy 2.0.2 / Pillow 11.3.0.

- [OpenCV 4.14 release notes](https://github.com/opencv/opencv/wiki/OpenCV-Change-Logs): GaussianBlur race-condition and image-decoder fixes are relevant to this app. Stay on 4.x rather than introducing a 5.x major-version migration just for dependency maintenance.
- [Pillow 12.3 release notes](https://pillow.readthedocs.io/en/stable/releasenotes/12.3.0.html): image-processing security fixes and optimizations. Not every fix affects our formats or code paths.
- [NumPy 2.5 release notes](https://numpy.org/doc/stable/release/2.5.0-notes.html): supports Python 3.12–3.14. Compatibility with OpenCV and our image operations was exercised in the new environment. The measurements below compare complete environments, not NumPy or OpenCV in isolation.

Retained native tools: potrace 1.16, Ghostscript 10.08.0, librsvg 2.63.0, and OpenSCAD 2026.03.01. Homebrew reported no pending formula upgrades for the checked tools. Python patch versions and native tools are system-managed, not locked by `requirements.txt`.

### Compatibility and performance verification — September 17, 2026

Ran the same application code and native converters in all three Python environments on an Apple M2 Max. Each of four local artwork files was uploaded to each server. After one warm-up per operation and image, measured seven sequential HTTP extraction and fuzzing requests per image and environment, rotating environment order between rounds. Timings include server processing, potrace tracing, and local HTTP overhead; they exclude image upload and 3D rendering.

Median extraction times, rounded to milliseconds:

| Local artwork | Anaconda / OpenCV 4.10 | Previous venv / OpenCV 4.13 | New venv / OpenCV 4.14 |
| --- | ---: | ---: | ---: |
| `cleaned-trimmed.png` | 68 ms | 58 ms | 57 ms |
| `processor/IMG_3570.JPG` | 77 ms | 67 ms | 67 ms |
| `processor/IMG_3569.PNG` | 149 ms | 132 ms | 128 ms |
| `processor/extracted/06_floral_top.png` | 88 ms | 74 ms | 66 ms |

Summing the unrounded per-image medians, extraction took **16.7% less time** than the Anaconda environment and **4.0% less time** than the previous venv. Fuzzing took **6.8% less time** and **4.5% less time**, respectively. Fuzzing strength was 60 for three images and 15 for the fine-line `IMG_3569.PNG`; strength 60 erased that image's dark content and correctly returned the same protective error in all three environments.

Compatibility results:

- All four images produced pixel-identical extracted and fuzzed PNGs, and identical SVGs, across the three environments.
- Fuzzing 0 restored the exact original SVG in every environment.
- Raised and concave `.3mf` files generated from `cleaned-trimmed.png` at 36 mm had identical vertex and triangle arrays across environments. Every mesh edge belonged to exactly two triangles.
- The upgraded environment also uploaded and extracted the real `cleaned_stamp.svg` and `processor/shutterstock_1901339512.eps` sources successfully.
- Invert produced the exact pixel complement; edited PNG retracing restored the submitted bitmap; Fill Holes succeeded on the reference photo.
- Browser interactions exercised upload, selection, fuzzing, concave generation, and restoration to 0.

These are local smoke-test and timing results, not a general performance guarantee. The upgrade preserved artwork rather than improving its visual quality. No 3D-rendering speedup or physical printing/clay-release improvement was measured. The source artwork includes ignored local assets that are not all available in a fresh clone; repeat with your own representative artwork rather than treating these values as a portable benchmark suite.

## Usage

```bash
bash processor/start.sh
```

Open http://localhost:8800 in your browser.

1. Click **Choose File** to upload an image (JPG, PNG, EPS)
2. Draw rectangle or circle selections around motifs
3. Each selection auto-extracts to an SVG preview
4. Use **Paint / Erase**, **Invert**, or **Fill Holes** to refine
5. Adjust **Fuzzing** to reduce small details and smooth the shape. Release the slider to update the preview. Returning to 0 restores the artwork from before fuzzing; paint/erase and invert establish a new baseline.
6. Choose the stamp face: **Raised** presses the artwork into clay; **Concave** recesses the artwork into the stamp so it stands out in clay.
7. Click **Download SVG** or **Generate .3mf** for output. Fuzzing affects both; the face option affects only the 3D stamp.

## Pipeline

`png2stamp.sh` converts a PNG motif to a 3D-printable stamp:

```
PNG → PBM → potrace SVG → OpenSCAD SCAD → STL → .3mf
```

The stamp has a handle and prints face-up. Raised mode uses projecting artwork. Concave mode uses a continuous surrounding pressing face, recessed artwork, and solid backing beneath the cavities. That surrounding face leaves an impression/border in the clay; it is not just a black/white inversion of the artwork. Fine cavities may still require a lower fuzzing setting or a larger stamp, and physical clay release should be checked with a test print.

The CLI also accepts the face mode as its fifth argument:

```bash
bash png2stamp.sh design.svg output 36 0 concave
# Arguments: input, output base, design size in mm, thickening in mm (or auto), raised|concave
```

## Project Structure

```
ceramic-stamps/
├── png2stamp.sh          # CLI: PNG → 3D stamp (.3mf)
├── processor/
│   ├── setup.sh          # Install/update dependencies or --check their health
│   ├── start.sh          # Start using the project Python environment
│   ├── requirements.txt  # Exact Python dependency versions
│   ├── stamp_tool.py     # Python HTTP server + processing API
│   └── index.html        # Single-page web frontend
```
