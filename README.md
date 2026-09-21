# Ceramic Stamps Processor

A web-based tool for extracting symbols, shapes, and motifs from images or vector files, converting them to clean SVGs, and generating 3D-printable `.3mf` ceramic stamps.

## Features

- **Visual region selection** — Draw rectangles or circles on uploaded images to select motifs
- **Resize, move & delete selections** — Select a region and drag its corner or edge handles to resize; drag inside to move; use **Delete selection** or right-click to remove
- **EPS/vector support** — Upload EPS, PS, or AI files with automatic high-DPI extraction
- **HEIF/HEIC support** — Import iPhone photos with orientation correction, primary-image selection, and transparent areas flattened onto white
- **Erase tool** — Brush-based eraser to clean up unwanted dots or artifacts
- **Fill holes** — Automatically fill interior gaps in extracted patterns
- **Invert** — Swap black/white for patterns on dark backgrounds
- **SVG export** — Download clean vector SVGs traced with potrace
- **3D stamp generation** — Generate `.3mf` files via OpenSCAD with a raised face for recessed artwork in clay, or a concave face for raised artwork in clay
- **Overall body dimensions** — Auto-proportioned, rectangular, or round bodies with explicit width, face height, and total height; optional 65 × 15 × 20 mm signature preset
- **Fuzzing** — Locally smooth outlines and remove fine details before SVG export or stamp generation; no API token required
- **Nozzle detail comparison** — Inspect approximate pattern loss and unwanted additions for 0.2, 0.4, 0.6, and 0.8 mm nozzles at the chosen physical stamp size
- **Preserve thin strokes** — Selectively widen undersized strokes at the final physical size, keeping original vector contours and reporting conflicts with protected gaps
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

On Linux, install Python 3.13 with venv/pip support plus `potrace`, `ghostscript`, `librsvg2-bin`, and `openscad` through your distribution's package manager, then run `bash processor/setup.sh`. Automatic native installation is macOS-only.

The renderer defaults to `/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD`, then `openscad` on PATH. Set `OPENSCAD=/path/to/openscad` to override it for setup checks, startup, or CLI conversion. The verified renderer is OpenSCAD 2026.03.01 with Manifold support; older distribution builds may render more slowly.

New macOS installations use the snapshot cask: Homebrew disabled the old `openscad` 2021.01 cask in September 2026 because it fails Gatekeeper checks. Setup leaves an existing working renderer alone and never bypasses Gatekeeper.

### Check, repair, and update

```bash
bash processor/setup.sh --check   # Read-only dependency health check; no installation
bash processor/setup.sh           # Reapply the pinned package versions
bash processor/start.sh 8801      # Optional alternate server port
```

For a fresh environment, stop the server, move `processor/.venv` outside that path, and rerun setup. Old `.venv.backup.*` directories can be removed once the replacement is working; they are not used by the application. Virtual environments contain absolute paths, so recreate `.venv` after relocating the repository.

To upgrade Python packages intentionally, update the exact pins in `processor/requirements.txt`, rerun setup, and exercise PNG/JPEG/HEIF/EPS/SVG uploads, extraction, fuzzing, nozzle comparison, and both stamp modes before keeping the upgrade. Install only `opencv-python-headless`, not another package that also provides `cv2`.

For standalone image helpers, use the same environment, for example `cd processor && .venv/bin/python extract_motifs.py`.

Native tools remain managed by Homebrew, independently of the Python pins. Check them with `brew outdated potrace ghostscript librsvg python@3.13`; use targeted `brew upgrade` commands only when wanted. Manage an existing manually installed OpenSCAD app through its own distribution.

### Verified version selection

The September 2026 cleanup standardized the project on Python **3.13**, OpenCV **4.14.0.94** (headless), NumPy **2.5.3**, and Pillow **12.3.0**. Two earlier environments were present: the working Anaconda environment used Python 3.9.18 / OpenCV 4.10.0 / NumPy 1.23.5 / Pillow 10.4.0, while the preserved project `.venv` used Python 3.9.18 / OpenCV 4.13.0 / NumPy 2.0.2 / Pillow 11.3.0.

- [OpenCV 4.14 release notes](https://github.com/opencv/opencv/wiki/OpenCV-Change-Logs): GaussianBlur race-condition and image-decoder fixes are relevant to this app. Stay on 4.x rather than introducing a 5.x major-version migration just for dependency maintenance.
- [Pillow 12.3 release notes](https://pillow.readthedocs.io/en/stable/releasenotes/12.3.0.html): image-processing security fixes and optimizations. Not every fix affects our formats or code paths.
- [NumPy 2.5 release notes](https://numpy.org/doc/stable/release/2.5.0-notes.html): supports Python 3.12–3.14. Compatibility with OpenCV and our image operations was exercised in the new environment. The measurements below compare complete environments, not NumPy or OpenCV in isolation.

Retained native tools: potrace 1.16, Ghostscript 10.08.0, librsvg 2.63.0, and OpenSCAD 2026.03.01. Homebrew reported no pending formula upgrades for the checked tools. Python patch versions and native tools are system-managed, not locked by `requirements.txt`.

HEIF import uses pinned `pillow-heif` 1.7.0; the installed macOS wheel bundles libheif 1.23.3. The web app converts the primary image to PNG, so the browser does not need HEIF support. The standalone converter also accepts HEIF/HEIC.

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

That dependency comparison predates the overall-dimension body redesign. The sizing update was separately checked against exported meshes: rectangular 113 × 72 × 22 mm, auto 113 × 14 × 22 mm for 10:1 artwork, and round 72 × 72 × 22 mm, in both face modes. Bounds were exact, every mesh edge had two adjacent triangles, artwork proportions and mirroring were preserved, and concave floor/island/rim geometry was checked.

The corrected signature preset was verified separately: both raised and concave exported meshes have exact 65 × 15 × 20 mm bounds and two triangles per edge. HEIF smoke checks covered EXIF rotation, alpha flattening, primary-image selection, malformed input, extraction, and actual CLI/HTTP `.3mf` generation. Nozzle checks exercised fine bars and gaps at three physical scales in both modes; comparing nozzles left existing SVG, PNG, and `.3mf` downloads byte-identical.

## Deployment (Cloud Run + Firebase Hosting)

The app is a stateful Python HTTP server that shells out to OpenSCAD, potrace, Ghostscript, and librsvg, so it runs as a **container on Cloud Run**, with **Firebase Hosting** in front rewriting every request to it. Static Hosting alone cannot serve this app.

| Setting | Value |
| --- | --- |
| GCP / Firebase project | `ceramic-stamps-titus` |
| Cloud Run service | `ceramic-stamp-maker` (region `us-central1`) |
| Artifact Registry repo | `us-central1-docker.pkg.dev/ceramic-stamps-titus/ceramic-stamp-maker` |
| Hosting URL | https://ceramic-stamps-titus.web.app |

`firebase.json` holds the rewrite to the `ceramic-stamp-maker` service, and `.firebaserc` pins the default project. Both the service name and its region must match the `run` block, or Hosting returns 404.

### Prerequisites

Install the Google Cloud SDK through Homebrew — do not keep ad-hoc SDK copies on disk:

```bash
brew install --cask gcloud-cli
gcloud auth login
gcloud config set project ceramic-stamps-titus
gcloud billing projects link ceramic-stamps-titus --billing-account=<ACCOUNT_ID>
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com
```

The cask links `gcloud`, `gsutil`, `bq`, and `docker-credential-gcloud` into `/opt/homebrew/bin`, and installs zsh completion at `/opt/homebrew/share/zsh/site-functions/_google_cloud_sdk`. `gcloud auth configure-docker us-central1-docker.pkg.dev` registers the Docker credential helper; that helper must stay on `PATH`, or `docker push` fails with `Operation not permitted`.

### Build and deploy

Cloud Run requires **`linux/amd64`**. Docker Desktop on Apple silicon builds `arm64` by default, and Cloud Run rejects it with `Container manifest type 'application/vnd.oci.image.index.v1+json' must support amd64/linux`. Always pass `--platform`:

```bash
export PATH="/opt/homebrew/bin:$PATH"
IMAGE=us-central1-docker.pkg.dev/ceramic-stamps-titus/ceramic-stamp-maker/ceramic-stamp-maker:latest

docker build --platform linux/amd64 -t ceramic-stamp-maker:amd64 .
docker tag ceramic-stamp-maker:amd64 "$IMAGE"
docker push "$IMAGE"

gcloud run deploy ceramic-stamp-maker --image "$IMAGE" \
  --region us-central1 --project ceramic-stamps-titus \
  --allow-unauthenticated --memory 2Gi --cpu 2 \
  --timeout 600 --concurrency 8 --port 8080

firebase deploy --only hosting --project ceramic-stamps-titus
```

Generous memory and timeout matter: OpenSCAD renders take roughly 10–60 s per stamp and produce meshes in the hundreds of megabytes uncompressed.

### Container notes

- The base image is `debian:trixie-slim`. `python3.13` and `openscad` are both available only in **trixie** or later, not bookworm.
- `.dockerignore` excludes `processor/.venv/`. The macOS virtualenv must never enter the build context: its interpreter symlink points at Homebrew and its wheels are Mach-O, so the container would fail to start. The Dockerfile builds `.venv` inside the image and asserts the interpreter and imports resolve before the build can succeed.
- The server reads `PORT` (Cloud Run sets it) and `STAMP_HOST`, and caps concurrent request threads with `STAMP_MAX_WORKERS` (default 32).
- All uploaded images, extractions, and stamp jobs live in in-memory dictionaries. A restart or a second Cloud Run instance loses them, so the service is pinned to a low instance count for interactive use.

### Orientation contract

Width and height are the artwork's **on-screen horizontal and vertical** extents, the same axes as the raster preview and the SVG viewBox. A portrait artwork therefore produces a **taller-than-wide** stamp. Do not swap width and height to "correct" a tall design — that rotates the printed stamp 90° relative to what the preview shows.

## Usage

```bash
bash processor/start.sh
```

Open http://localhost:8800 in your browser.

1. Click **Choose File** to upload an image (including JPG, PNG, HEIF/HEIC, EPS, or SVG)
2. Draw rectangle or circle selections around motifs. Click a region (or its extraction thumbnail) to select it, then drag its white corner/edge handles to resize or drag inside to move it. Releasing refreshes the extraction and resets its edits/fuzzing. Use **Delete selection** to remove the selected region and its extraction; right-clicking a region also deletes it.
3. Each selection auto-extracts to an SVG preview
4. Use **Paint / Erase**, **Invert**, or **Fill Holes** to refine
5. Adjust **Fuzzing** to reduce small details and smooth the shape. Release the slider to update the preview. Returning to 0 restores the artwork from before fuzzing; paint/erase and invert establish a new baseline.
6. Choose the stamp face: **Raised** presses the artwork into clay; **Concave** recesses the artwork into the stamp so it stands out in clay.
7. Choose **Body shape** and overall dimensions. **Auto** follows the artwork ratio; **Rectangular** accepts independent width and face height; **Round** uses a diameter.
8. For the 15 × 65 × 20 mm signature stamp, click **Use 65 × 15 × 20 mm**: 65 mm along the horizontal lettering, 15 mm across it, and 20 mm total height. This is an optional rectangular preset, not the default for other stamps.
9. For delicate signatures, enable **Preserve thin strokes** and choose a target nozzle to fill in a starting minimum width, or enter your own. Then click **Compare nozzles** to inspect original, strengthened, and estimated printed detail for 0.2, 0.4, 0.6, and 0.8 mm nozzles. Select a card for full-resolution inspection and up to 400% zoom. Recompare after changing artwork or stamp settings.
10. Click **Download SVG** or **Generate .3mf** for output. Fuzzing affects both; face mode, dimensions, preservation, and reinforcement affect the 3D stamp and its nozzle comparison, not the source SVG download.

All sizing controls are in **millimeters**. Width and face height describe the outside footprint — width is the artwork's horizontal extent and face height its vertical extent, both matching the preview, so a portrait artwork yields a taller-than-wide stamp. **Overall height** includes the broad grip block and the 2.8 mm stamping relief, not an additional handle. The artwork fits uniformly inside a minimum 1.5 mm margin without stretching. Round bodies fit the artwork's bounding-box corners inside the circle. Auto sizing derives face height from the artwork ratio, with a 12 mm minimum. Width/face height are limited to 12–200 mm; overall height is 8–60 mm. A very tall auto result over 200 mm needs a smaller width or a rectangular fit.

The SVG preview shows artwork, not a 3D body preview. Mirroring for readable clay impressions is automatic in both face modes; do not mirror text again in Bambu Studio. Keep Bambu Studio's model scale at 100% when matching the preset to other stamps. Exported dimensions do not compensate for printer tolerances or clay shrinkage.

### Reading the nozzle comparison

Black is retained pattern, red is lost pattern, and amber is unwanted added pattern, shown in normal clay-reading orientation. Percentages measure changes relative to intended pattern area, not the whole body. White is the body and light gray is outside it. Reinforcement and concave cavity-mouth widening are included.

This is an approximate planar feature/gap estimate sampled every 0.05 mm, using assumed line widths of 1.05 × nozzle diameter (0.21, 0.42, 0.63, and 0.84 mm). It is not a slicer simulation: toolpaths, variable-width extrusion, layer height, filament behavior, clay pressure, and release are not modeled. Screen size is not physical print size.

Comparing nozzles does **not** itself change the artwork, exported mesh, or print settings. It uses the same selected preservation/reinforcement treatment as stamp generation. The bundled Bambu profile remains 0.4 mm-oriented; select your actual nozzle and matching line widths in Bambu Studio, then re-slice before printing.

### Preserving delicate strokes

**Preserve thin strokes** works at the selected stamp size. Its nozzle presets suggest minimum stroke widths of 0.24, 0.46, 0.68, and 0.90 mm; the minimum is adjustable from 0.15 to 1.2 mm. These are starting points, not guaranteed printable widths or printer-profile selections. Settings apply across selections. Changing the body size recalculates the treatment at that physical scale.

The algorithm measures local widths around sampled centerlines and adds material only around undersized portions, rather than uniformly expanding the whole drawing. Original vector contours are retained. Negative-space corridors and topology checks constrain additions to avoid merging strokes or closing loops at the sampled resolution. Sampling is normally 0.025 mm, increasing up to 0.05 mm for large designs. Existing undersized gaps are retained, not repaired; subpixel features and vector tracing can differ from the estimate.

The comparison shows **Original reference** beside **Strengthened intended**. Added area is relative to original sampled ink area. **Gap / edge conflicts** counts thin-centerline samples where the full target width could not fit without violating gap, topology, or viewport constraints; it is not a percentage of missing printed ink. Nozzle loss/extra percentages are relative to the strengthened pattern when preservation is enabled.

Preservation is reversible and never accumulates: switching it off restores untreated stamp geometry. It is mutually exclusive with **Reinforce thin lines**, which uniformly expands outlines. The main artwork preview and **Download SVG** remain unchanged; both raised and concave `.3mf` exports use the same preserved SVG geometry as the comparison. In concave mode, normal cavity-mouth widening still applies afterward. A slicer may resolve tight gaps differently, so inspect the final sliced top layers and test-print before using the stamp in clay.

Physical stroke/topology and OpenSCAD export regressions can be run with:

```bash
processor/.venv/bin/python -m unittest discover -s processor -p test_stroke_preservation.py -v
```

## Pipeline

`png2stamp.sh` converts a PNG motif to a 3D-printable stamp:

```
PNG → PBM → potrace SVG → OpenSCAD SCAD → STL → .3mf
```

The stamp uses a broad, continuous grip block that follows the chosen footprint and prints face-up. There is no oversized round cone behind narrow artwork. Raised mode has projecting artwork above the backing. Concave mode cuts the artwork into the top face, with a solid floor and a slightly widened cavity mouth. The surrounding face leaves an impression/border in the clay; it is not just a black/white inversion. Physical clay release should still be checked with a test print.

The CLI uses the same overall-dimension rules. Its positional interface has changed; the former single-size argument order is no longer supported:

```bash
# Exact rectangular overall dimensions; concave face:
bash png2stamp.sh design.svg output 65 15 20 rectangular concave 0

# Body follows the image proportions at 36 mm wide:
bash png2stamp.sh design.svg output 36 auto 22 auto raised auto

# Round body, 36 mm diameter:
bash png2stamp.sh design.svg output 36 auto 22 round raised 0

# Arguments: input, output base, width/diameter, face height or auto,
#            total height, auto|rectangular|round, raised|concave, thickening mm|auto
```

## Project Structure

```
ceramic-stamps/
├── png2stamp.sh          # CLI: PNG → 3D stamp (.3mf)
├── Dockerfile            # Cloud Run image (Debian trixie + OpenSCAD toolchain)
├── .dockerignore         # Keeps the host macOS venv out of the image
├── firebase.json         # Hosting config; rewrites everything to Cloud Run
├── .firebaserc           # Default Firebase project
├── public/index.html     # Hosting placeholder; all real traffic hits the rewrite
├── processor/
│   ├── setup.sh          # Install/update dependencies or --check their health
│   ├── start.sh          # Start using the project Python environment
│   ├── requirements.txt  # Exact Python dependency versions
│   ├── stamp_tool.py     # Python HTTP server + processing API
│   ├── stamp_geometry.py # Shared dimension validation and OpenSCAD geometry
│   ├── print_preview.py  # Physical-scale nozzle feature/gap estimates
│   ├── stroke_preservation.py # Selective, reversible physical stroke widening
│   ├── test_stroke_preservation.py # Stroke/gap and real-renderer regressions
│   └── index.html        # Single-page web frontend
```
