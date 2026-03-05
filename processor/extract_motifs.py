#!/usr/bin/env python3
"""
Extract the leftmost motif from each row of pattern reference page IMG_3570.

Two extraction modes:
  "auto"  - find leftmost motif via connected component clustering
  "crop"  - use a fixed crop box (for dense rows where auto-separation fails)
"""

import cv2
import numpy as np
import os

INPUT = "IMG_3570.JPG"
OUT_DIR = "extracted"
UPSCALE = 8              # higher for smoother edges
THRESH_VAL = 140
OUTPUT_SIZE = 800        # larger for better potrace tracing
BLUR_RADIUS = 5          # Gaussian blur for smooth contours

# (name, mode, y1, y2, x1, x2, param)
# mode="auto": param = cluster_dist (upscaled px)
# mode="crop": x1-x2 defines exactly one motif (tight crop)
ROW_DEFS = [
    # --- Left column simple patterns ---
    ("01_dot",          "auto",  24,  47,   44, 200,  20),
    ("02_dash",         "auto",  49,  68,   44, 200,   8),
    ("03_dot_grid",     "crop",  69,  87,   46,  66,   0),  # tiny dots - may be empty
    ("04_exclamation",  "crop",  87, 100,   44,  62,   0),  # ! mark
    ("05_teardrop",     "auto", 100, 164,   44, 200,  20),
    # --- Right column floral (dense, fixed crops) ---
    ("06_floral_top",   "crop",  26,  68,  255, 302,   0),  # narrower to avoid 2nd flower
    ("07_floral_mid",   "crop",  72, 112,  262, 316,   0),  # centered on one flower
    ("08_floral_bot",   "crop", 114, 168,  258, 316,   0),
    # --- Lower section ---
    ("09_leaf_pair",    "crop", 168, 216,   44,  98,   0),
    ("10_comma_pair",   "auto", 216, 262,   44, 200,  12),
    ("11_flower_dot",   "auto", 262, 318,   44, 200,  12),
    ("12_daisy_1",      "auto", 318, 395,   44, 200,  12),
    ("13_daisy_2",      "auto", 395, 462,   44, 250,  12),
    ("14_triangle",     "crop", 462, 544,   44, 122,   0),
    ("15_garland",      "crop", 544, 570,   44, 115,   0),
    ("16_zigzag",       "crop", 570, 586,   44, 118,   0),  # stop before caption
]


def find_leftmost_motif(bw_region, cluster_dist):
    """Find the leftmost motif cluster in a binary region."""
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(bw_region, connectivity=8)

    comps = []
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        if area > 15:
            comps.append({'x': x, 'y': y, 'w': cw, 'h': ch,
                          'area': area, 'cx': centroids[i][0]})

    if not comps:
        return None

    if len(comps) == 1:
        c = comps[0]
        return {'x': c['x'], 'y': c['y'], 'w': c['w'], 'h': c['h'],
                'area': c['area'], 'n_parts': 1}

    # Union-find clustering
    nc = len(comps)
    parent = list(range(nc))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(nc):
        a = comps[i]
        for j in range(i + 1, nc):
            b = comps[j]
            dx = max(0, max(a['x'], b['x']) - min(a['x'] + a['w'], b['x'] + b['w']))
            dy = max(0, max(a['y'], b['y']) - min(a['y'] + a['h'], b['y'] + b['h']))
            dist = (dx ** 2 + dy ** 2) ** 0.5
            if dist < cluster_dist:
                union(i, j)

    clusters = {}
    for i in range(nc):
        r = find(i)
        clusters.setdefault(r, []).append(comps[i])

    motifs = []
    for members in clusters.values():
        mx1 = min(c['x'] for c in members)
        my1 = min(c['y'] for c in members)
        mx2 = max(c['x'] + c['w'] for c in members)
        my2 = max(c['y'] + c['h'] for c in members)
        motifs.append({
            'x': mx1, 'y': my1, 'w': mx2 - mx1, 'h': my2 - my1,
            'area': sum(c['area'] for c in members),
            'n_parts': len(members),
        })

    motifs.sort(key=lambda m: m['x'])

    # Return leftmost with reasonable area
    for m in motifs:
        if m['area'] > 30:
            return m
    return motifs[0]


def extract_and_save(gray, bw, name, mode, y1o, y2o, x1o, x2o, param, S, out_dir):
    y1, y2 = y1o * S, y2o * S
    x1, x2 = x1o * S, x2o * S

    region_gray = gray[y1:y2, x1:x2]
    region_bw = bw[y1:y2, x1:x2]

    if mode == "crop":
        # Direct crop — the box IS the motif
        tv = 160 if "dot_grid" in name else THRESH_VAL
        # Blur for smooth edges, then threshold
        blurred = cv2.GaussianBlur(region_gray, (BLUR_RADIUS, BLUR_RADIUS), 0)
        _, clean = cv2.threshold(blurred, tv, 255, cv2.THRESH_BINARY)

        # Tight-crop whitespace
        inv = 255 - clean
        rows_ink = np.any(inv > 0, axis=1)
        cols_ink = np.any(inv > 0, axis=0)

        if not np.any(rows_ink):
            print(f"  {name}: empty region, skipping")
            return

        pad = 6 * S
        r1 = max(0, np.argmax(rows_ink) - pad)
        r2 = min(clean.shape[0], clean.shape[0] - np.argmax(rows_ink[::-1]) + pad)
        c1 = max(0, np.argmax(cols_ink) - pad)
        c2 = min(clean.shape[1], clean.shape[1] - np.argmax(cols_ink[::-1]) + pad)
        clean = clean[r1:r2, c1:c2]
        info = f"crop ({x2o-x1o}x{y2o-y1o}px orig)"

    else:
        # Auto mode — find leftmost motif
        motif = find_leftmost_motif(region_bw, param)
        if motif is None:
            print(f"  {name}: no motifs found, skipping")
            return

        # Smaller padding to avoid capturing adjacent motifs
        pad = 4 * S
        px1 = max(0, motif['x'] - pad)
        py1 = max(0, motif['y'] - pad)
        px2 = min(x2 - x1, motif['x'] + motif['w'] + pad)
        py2 = min(y2 - y1, motif['y'] + motif['h'] + pad)

        crop = region_gray[py1:py2, px1:px2]
        blurred = cv2.GaussianBlur(crop, (BLUR_RADIUS, BLUR_RADIUS), 0)
        _, clean = cv2.threshold(blurred, THRESH_VAL, 255, cv2.THRESH_BINARY)
        info = f"auto {motif['n_parts']}p {motif['w']//S}x{motif['h']//S}px"

    # Scale up for better potrace tracing
    ch, cw = clean.shape
    if max(cw, ch) > 0:
        scale = OUTPUT_SIZE / max(cw, ch)
        if scale > 1:
            new_w, new_h = int(cw * scale), int(ch * scale)
            clean = cv2.resize(clean, (new_w, new_h),
                               interpolation=cv2.INTER_LANCZOS4)
            _, clean = cv2.threshold(clean, 128, 255, cv2.THRESH_BINARY)

    fpath = os.path.join(out_dir, f"{name}.png")
    cv2.imwrite(fpath, clean)
    print(f"  {name}.png: {clean.shape[1]:3d}x{clean.shape[0]:3d}px [{info}]")
    return fpath


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for f in os.listdir(OUT_DIR):
        if f.endswith('.png'):
            os.remove(os.path.join(OUT_DIR, f))

    img = cv2.imread(INPUT, cv2.IMREAD_GRAYSCALE)
    oh, ow = img.shape
    S = UPSCALE

    gray = cv2.resize(img, (ow * S, oh * S), interpolation=cv2.INTER_CUBIC)
    # NO morphological open — preserves tiny features like dot grid
    _, bw = cv2.threshold(gray, THRESH_VAL, 255, cv2.THRESH_BINARY_INV)

    print(f"Original: {ow}x{oh}, Upscaled: {ow*S}x{oh*S}\n")

    png_files = []
    for name, mode, y1o, y2o, x1o, x2o, param in ROW_DEFS:
        result = extract_and_save(gray, bw, name, mode, y1o, y2o, x1o, x2o, param, S, OUT_DIR)
        if result:
            png_files.append(result)

    # Trace all PNGs to SVG using potrace (smooth vector output)
    print(f"\nTracing to SVG with potrace...")
    import subprocess
    for png_path in png_files:
        base = os.path.splitext(png_path)[0]
        bmp_path = base + ".bmp"
        svg_path = base + ".svg"

        # Convert PNG to BMP (potrace needs BMP/PBM input)
        pimg = cv2.imread(png_path, cv2.IMREAD_GRAYSCALE)
        _, pbw = cv2.threshold(pimg, 128, 255, cv2.THRESH_BINARY)
        cv2.imwrite(bmp_path, pbw)

        try:
            subprocess.run(
                ["potrace", bmp_path, "-s", "--tight", "-o", svg_path],
                check=True, capture_output=True
            )
            os.remove(bmp_path)
            name = os.path.basename(svg_path)
            print(f"  {name}")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"  potrace failed for {png_path}: {e}")
            if os.path.exists(bmp_path):
                os.remove(bmp_path)

    # Clean up any leftover BMP files
    for f in os.listdir(OUT_DIR):
        if f.endswith('.bmp'):
            os.remove(os.path.join(OUT_DIR, f))

    print(f"\nDone! PNGs and SVGs saved to {OUT_DIR}/")
    print("Each PNG can be used as input to png2stamp.sh")


if __name__ == "__main__":
    main()
