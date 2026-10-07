#!/usr/bin/env python3
"""Stamp Extractor Web Tool — Python HTTP server + image processing API."""

import http.server
import json
import math
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import uuid
from urllib.parse import urlparse

import cv2
import numpy as np
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from stamp_geometry import resolve_dimensions
from nozzle_sizing import size_for_nozzle
from print_preview import build_print_preview
from stroke_preservation import preserve_strokes

register_heif_opener(thumbnails=False)

HOST = os.environ.get("STAMP_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("PORT", "8800"))

# In-memory storage
images = {}       # id -> {"path": str, "img": np.array (grayscale)}
extractions = {}  # id -> PNG/SVG, name, immutable baseline bitmap/SVG, and fuzz
stamp_jobs = {}   # job_id -> {"status": "running"|"done"|"error", "path": str, "name": str, "error": str}
preview_lock = threading.Lock()  # Bound full-resolution preview/preservation buffers.

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PNG2STAMP = os.path.join(SCRIPT_DIR, "..", "png2stamp.sh")


def preservation_settings(data, thin_lines):
    enabled = data.get("preserve_thin_strokes", False)
    if not isinstance(enabled, bool):
        raise ValueError("preserve_thin_strokes must be true or false")
    minimum = data.get("min_stroke_mm", 0.24)
    if enabled:
        if thin_lines:
            raise ValueError("Choose Preserve thin strokes or Reinforce thin lines, not both")
        if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
            raise ValueError("Minimum stroke width must be between 0.15 and 1.2 mm")
        try:
            minimum = float(minimum)
        except OverflowError:
            raise ValueError("Minimum stroke width must be between 0.15 and 1.2 mm") from None
        if not math.isfinite(minimum) or not 0.15 <= minimum <= 1.2:
            raise ValueError("Minimum stroke width must be between 0.15 and 1.2 mm")
    return enabled, minimum


def simplify_bitmap(bw, fuzz):
    """Remove details at an image-relative scale without accumulating edits."""
    if fuzz == 0:
        return bw
    strength = fuzz / 100
    radius = max(1, round(max(bw.shape) * 0.012 * strength ** 1.4))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                       (2 * radius + 1, 2 * radius + 1))
    ink = cv2.bitwise_not(bw)
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, kernel,
                          borderType=cv2.BORDER_REPLICATE)
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, kernel,
                          borderType=cv2.BORDER_REPLICATE)
    ink = cv2.GaussianBlur(ink, (2 * radius + 1, 2 * radius + 1),
                           max(0.5, radius * 0.5), borderType=cv2.BORDER_REPLICATE)
    _, ink = cv2.threshold(ink, 127, 255, cv2.THRESH_BINARY)

    # Connected components remove isolated remnants, not intentional large holes.
    _, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    keep = stats[:, cv2.CC_STAT_AREA] >= max(4, 4 * radius * radius)
    keep[0] = False
    return np.where(keep[labels], 0, 255).astype(np.uint8)


class StampHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Quieter logging — just method + path
        print(f"  {args[0]}")

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, msg):
        self._send_json({"error": msg}, status)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    # ── Routing ──

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._serve_file("index.html", "text/html")
        elif path.startswith("/image/"):
            self._serve_image(path[7:])
        elif path.startswith("/extraction/svg/"):
            self._serve_extraction_svg(path[16:])
        elif path.startswith("/extraction/png/"):
            self._serve_extraction_png(path[16:])
        elif path.startswith("/stamp/status/"):
            self._handle_stamp_status(path[14:])
        elif path.startswith("/stamp/download/"):
            self._handle_stamp_download(path[16:])
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/upload":
            self._handle_upload()
        elif path == "/extract":
            self._handle_extract()
        elif path == "/retrace":
            self._handle_retrace()
        elif path == "/invert":
            self._handle_invert()
        elif path == "/simplify":
            self._handle_simplify()
        elif path == "/stamp":
            self._handle_stamp()
        elif path == "/print-preview":
            self._handle_print_preview()
        elif path == "/nozzle-size":
            self._handle_nozzle_size()
        else:
            self.send_error(404)

    # ── GET handlers ──

    def _serve_file(self, filename, content_type):
        filepath = os.path.join(SCRIPT_DIR, filename)
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def _serve_image(self, image_id):
        rec = images.get(image_id)
        if not rec:
            self.send_error(404)
            return
        try:
            with open(rec["path"], "rb") as f:
                data = f.read()
            ext = os.path.splitext(rec["path"])[1].lower()
            ctype = "image/png" if ext == ".png" else "image/jpeg"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def _serve_extraction_svg(self, ext_id_and_query):
        from urllib.parse import parse_qs
        ext_id = ext_id_and_query.split("?")[0]
        rec = extractions.get(ext_id)
        if not rec:
            self.send_error(404)
            return
        svg = rec["svg"].encode()
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", len(svg))
        # If dl param present, force download
        query = urlparse(self.path).query
        params = parse_qs(query)
        if "dl" in params:
            fname = params["dl"][0]
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.end_headers()
        self.wfile.write(svg)

    def _serve_extraction_png(self, ext_id):
        rec = extractions.get(ext_id)
        if not rec or not os.path.isfile(rec.get("png_path", "")):
            self.send_error(404)
            return
        with open(rec["png_path"], "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    # ── POST /upload ──

    def _handle_upload(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_error(400, "Expected multipart/form-data")
            return

        # Parse boundary
        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip('"')
        if not boundary:
            self._send_error(400, "No boundary in multipart")
            return

        body = self._read_body()
        # Extract file data and filename from multipart
        file_data, filename = self._parse_multipart(body, boundary)
        if not file_data:
            self._send_error(400, "No file found in upload")
            return

        image_id = str(uuid.uuid4())[:8]
        ext = os.path.splitext(filename)[1].lower() if filename else ""
        is_eps = ext in (".eps", ".ps", ".ai")
        is_svg = ext in (".svg",)
        is_heif = ext in (".heif", ".heic", ".hif", ".heifs", ".heics")

        # Save to temp file with correct extension
        tmp = tempfile.NamedTemporaryFile(suffix=ext or ".jpg", delete=False)
        tmp.write(file_data)
        tmp.close()

        if is_heif:
            # Decode once to an oriented PNG: browsers need not support HEIF.
            png_path = None
            try:
                with Image.open(tmp.name) as source:
                    normalized = ImageOps.exif_transpose(source)
                    if "A" in normalized.getbands():
                        rgba = normalized.convert("RGBA")
                        normalized = Image.new("RGB", rgba.size, "white")
                        normalized.paste(rgba, mask=rgba.getchannel("A"))
                    else:
                        normalized = normalized.convert("RGB")
                    png_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    png_path = png_tmp.name
                    png_tmp.close()
                    normalized.save(png_path, format="PNG")
                tmp_path = png_path
            except Exception as e:
                if png_path is not None:
                    os.unlink(png_path)
                self._send_error(400, f"Could not decode HEIF/HEIC image: {e}")
                return
            finally:
                os.unlink(tmp.name)

        elif is_svg:
            # Rasterize SVG via rsvg-convert at high DPI for canvas preview
            try:
                png_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                png_tmp.close()
                subprocess.run(
                    ["rsvg-convert", "-d", "300", "-p", "300", "-o", png_tmp.name, tmp.name],
                    check=True, capture_output=True
                )
                tmp_path = png_tmp.name
                print(f"  Converted SVG to PNG via rsvg-convert")
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                os.unlink(tmp.name)
                self._send_error(400, f"Could not convert SVG: {e}")
                return

        elif is_eps:
            # Convert EPS/PS/AI to PNG via Pillow+Ghostscript
            # Keep original vector for high-res re-rendering during extraction
            try:
                Image.MAX_IMAGE_PIXELS = None
                pil_img = Image.open(tmp.name)
                w0, h0 = pil_img.size
                # Display image: ~4000px for canvas preview
                disp_scale = max(1, min(4, 4000 // max(w0, h0)))
                pil_img.load(scale=disp_scale)
                png_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                png_tmp.close()
                pil_img.convert("L").save(png_tmp.name)
                tmp_path = png_tmp.name
                print(f"  Converted EPS to PNG: {pil_img.size[0]}x{pil_img.size[1]} (display)")
            except Exception as e:
                os.unlink(tmp.name)
                self._send_error(400, f"Could not convert EPS: {e}")
                return
        else:
            tmp_path = tmp.name

        # Load with OpenCV
        img = cv2.imread(tmp_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            os.unlink(tmp_path)
            self._send_error(400, "Could not decode image")
            return

        h, w = img.shape
        rec = {"path": tmp_path, "img": img}
        if is_eps:
            # Store original vector path for high-DPI extraction
            rec["vector_src"] = tmp.name
            rec["display_scale"] = disp_scale
        images[image_id] = rec
        print(f"  Uploaded image {image_id}: {w}x{h}")
        self._send_json({"id": image_id, "width": w, "height": h})

    def _parse_multipart(self, body, boundary):
        """Extract the first file's bytes and filename from a multipart body."""
        import re
        delim = f"--{boundary}".encode()
        parts = body.split(delim)
        for part in parts:
            if b"filename=" in part:
                # Extract filename from Content-Disposition header
                header_end = part.find(b"\r\n\r\n")
                if header_end == -1:
                    continue
                headers = part[:header_end].decode("utf-8", errors="replace")
                fname_match = re.search(r'filename="([^"]*)"', headers)
                filename = fname_match.group(1) if fname_match else ""
                file_bytes = part[header_end + 4:]
                # Strip trailing \r\n-- if present
                if file_bytes.endswith(b"\r\n"):
                    file_bytes = file_bytes[:-2]
                if file_bytes.endswith(b"--"):
                    file_bytes = file_bytes[:-2]
                if file_bytes.endswith(b"\r\n"):
                    file_bytes = file_bytes[:-2]
                return file_bytes, filename
        return None, ""

    # ── High-DPI vector re-render ──

    def _extract_from_vector(self, vector_path, rec, x, y, w, h):
        """Re-render a region from the original vector file at high DPI."""
        Image.MAX_IMAGE_PIXELS = None
        pil_img = Image.open(vector_path)
        native_w, native_h = pil_img.size
        disp_scale = rec.get("display_scale", 1)

        # Map display pixel coords back to native EPS coords
        nx = x / disp_scale
        ny = y / disp_scale
        nw = w / disp_scale
        nh = h / disp_scale

        # Render at scale that gives ~3000px for the crop region
        crop_max = max(nw, nh)
        hires_scale = max(1, int(3000 / crop_max)) if crop_max > 0 else 1
        hires_scale = min(hires_scale, 10)  # cap at 10x

        pil_img.load(scale=hires_scale)
        # Crop the region at high-res coordinates
        hx = int(nx * hires_scale)
        hy = int(ny * hires_scale)
        hw = int(nw * hires_scale)
        hh = int(nh * hires_scale)
        pil_crop = pil_img.crop((hx, hy, hx + hw, hy + hh))
        # Convert to grayscale numpy array
        gray_crop = np.array(pil_crop.convert("L"))
        return gray_crop

    # ── POST /extract ──

    def _handle_extract(self):
        try:
            self._do_extract()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_error(500, f"Extract failed: {e}")

    def _do_extract(self):
        data = json.loads(self._read_body())

        image_id = data.get("image_id")
        rec = images.get(image_id)
        if not rec:
            self._send_error(404, "Image not found")
            return

        x = int(data["x"])
        y = int(data["y"])
        w = int(data["w"])
        h = int(data["h"])
        shape = data.get("shape", "rect")

        gray = rec["img"]
        img_h, img_w = gray.shape
        print(f"  Extract request: x={x} y={y} w={w} h={h} img={img_w}x{img_h}")

        # Clamp to image bounds
        x = max(0, min(x, img_w - 1))
        y = max(0, min(y, img_h - 1))
        w = min(w, img_w - x)
        h = min(h, img_h - y)

        if w < 2 or h < 2:
            self._send_error(400, f"Selection too small after clamp: w={w} h={h}")
            return

        # For vector sources, re-render the selected region at high DPI
        vector_src = rec.get("vector_src")
        if vector_src and os.path.isfile(vector_src):
            try:
                crop = self._extract_from_vector(vector_src, rec, x, y, w, h)
                print(f"  Vector re-render: {crop.shape[1]}x{crop.shape[0]}px")
            except Exception as e:
                print(f"  Vector re-render failed ({e}), falling back to raster")
                crop = gray[y:y+h, x:x+w].copy()
        else:
            # 1. Crop from raster
            crop = gray[y:y+h, x:x+w].copy()

        # 2. Circle mask
        if shape == "circle":
            mask = np.zeros_like(crop)
            ch, cw = crop.shape[:2]
            cv2.ellipse(mask, (cw // 2, ch // 2), (cw // 2, ch // 2), 0, 0, 360, 255, -1)
            crop = np.where(mask == 255, crop, 255).astype(np.uint8)

        # 3. Upscale (skip for vector sources — already high-res)
        if not vector_src:
            scale = 8
            crop = cv2.resize(crop, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)

        # 4. Gaussian blur
        crop = cv2.GaussianBlur(crop, (5, 5), 0)

        # 5. Threshold (Otsu auto-detects the best level for any contrast)
        _, bw = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        # 6. Tight-crop whitespace
        inv = 255 - bw
        rows_ink = np.any(inv > 0, axis=1)
        cols_ink = np.any(inv > 0, axis=0)

        if not np.any(rows_ink):
            self._send_error(400, "Selection contains no dark content")
            return

        pad = 20
        r1 = max(0, np.argmax(rows_ink) - pad)
        r2 = min(bw.shape[0], bw.shape[0] - np.argmax(rows_ink[::-1]) + pad)
        c1 = max(0, np.argmax(cols_ink) - pad)
        c2 = min(bw.shape[1], bw.shape[1] - np.argmax(cols_ink[::-1]) + pad)
        bw = bw[r1:r2, c1:c2]

        # 7. Scale to target longest dimension
        #    Vector sources: 2400px for much finer potrace detail
        #    Raster sources:  800px (upscaled 8x from original, so already good)
        target_px = 2400 if vector_src else 800
        ch, cw = bw.shape
        if max(cw, ch) > 0:
            s = target_px / max(cw, ch)
            if s != 1.0:
                bw = cv2.resize(bw, (int(cw * s), int(ch * s)),
                                interpolation=cv2.INTER_LANCZOS4)

        # 8. Final threshold
        _, bw = cv2.threshold(bw, 128, 255, cv2.THRESH_BINARY)

        # 8b. Fill holes — fill small interior gaps (dots, thin lines)
        #     Skip large white regions that are intentional parts of the design
        fill_holes = data.get("fill_holes", False)
        if fill_holes:
            total_area = bw.shape[0] * bw.shape[1]
            max_fill = total_area * 0.05  # only fill holes smaller than 5% of image
            # Find contours on INVERTED image so black design shapes become white.
            # RETR_CCOMP gives 2-level hierarchy: design shapes (level 0) and
            # their internal holes (level 1). Level-1 contours with parent >= 0
            # are white holes inside black shapes — exactly what we want to fill.
            inv = cv2.bitwise_not(bw)
            contours, hierarchy = cv2.findContours(
                inv, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
            if hierarchy is not None:
                for i, cnt in enumerate(contours):
                    if hierarchy[0][i][3] >= 0:
                        area = cv2.contourArea(cnt)
                        if area < max_fill:
                            cv2.drawContours(bw, [cnt], -1, 0, cv2.FILLED)

        # 8c. Remove tiny speck noise — connected components smaller than 0.02%
        #     of image area. Invisible in SVG but become bumps in 3D.
        total_px = bw.shape[0] * bw.shape[1]
        min_speck = max(20, int(total_px * 0.0002))
        inv_bw = cv2.bitwise_not(bw)
        n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(inv_bw, connectivity=8)
        for lbl in range(1, n_labels):
            if stats[lbl, cv2.CC_STAT_AREA] < min_speck:
                bw[labels == lbl] = 255  # erase speck (set to white)

        # Trace and publish the PNG/SVG together only after all processing succeeds.
        ext_id = str(uuid.uuid4())[:8]
        tmp_dir = tempfile.mkdtemp()
        rec = {
            "png_path": os.path.join(tmp_dir, "extract.png"),
            "name": data.get("name", f"extract_{ext_id}"),
        }
        if not self._update_extraction(rec, bw, reset_baseline=True):
            os.rmdir(tmp_dir)
            return
        extractions[ext_id] = rec

        print(f"  Extraction {ext_id}: {bw.shape[1]}x{bw.shape[0]}px → SVG")
        self._send_json({"id": ext_id, "svg": rec["svg"], "fuzz": 0})

    def _update_extraction(self, rec, bw, fuzz=0, reset_baseline=False):
        """Stage both formats; a failed encode or trace leaves the record intact."""
        try:
            with tempfile.TemporaryDirectory(dir=os.path.dirname(rec["png_path"])) as tmp_dir:
                png_path = os.path.join(tmp_dir, "result.png")
                if not cv2.imwrite(png_path, bw):
                    raise OSError("Could not encode extraction PNG")
                if fuzz == 0 and not reset_baseline:
                    svg_content = rec["baseline_svg"]
                else:
                    bmp_path = os.path.join(tmp_dir, "result.bmp")
                    svg_path = os.path.join(tmp_dir, "result.svg")
                    if not cv2.imwrite(bmp_path, bw):
                        raise OSError("Could not encode tracing bitmap")
                    subprocess.run(
                        ["potrace", bmp_path, "-s", "--tight", "-o", svg_path],
                        check=True, capture_output=True
                    )
                    with open(svg_path) as f:
                        svg_content = f.read()
                os.replace(png_path, rec["png_path"])
        except (OSError, subprocess.CalledProcessError, cv2.error) as e:
            self._send_error(500, f"Could not update extraction: {e}")
            return False

        rec.update(svg=svg_content, fuzz=fuzz)
        if reset_baseline:
            rec.update(baseline_bw=bw, baseline_svg=svg_content)
        return True

    # ── POST /simplify — always process the most recent edit baseline ──

    def _handle_simplify(self):
        try:
            data = json.loads(self._read_body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_error(400, "Invalid JSON")
            return
        if not isinstance(data, dict):
            self._send_error(400, "Expected a JSON object")
            return
        fuzz = data.get("fuzz")
        if type(fuzz) is not int or not 0 <= fuzz <= 100:
            self._send_error(400, "fuzz must be a finite integer from 0 to 100")
            return
        ext_id = data.get("extraction_id")
        rec = extractions.get(ext_id) if isinstance(ext_id, str) else None
        if not rec:
            self._send_error(404, "Extraction not found")
            return
        try:
            bw = rec["baseline_bw"] if fuzz == 0 else simplify_bitmap(rec["baseline_bw"], fuzz)
        except cv2.error as e:
            self._send_error(500, f"Could not simplify extraction: {e}")
            return
        if fuzz > 0 and not np.any(bw == 0):
            self._send_error(400, "Simplification removed all dark content; try a lower fuzz value")
            return
        if not self._update_extraction(rec, bw, fuzz=fuzz):
            return
        self._send_json({"id": ext_id, "svg": rec["svg"], "fuzz": fuzz})

    # ── POST /invert — invert extraction B&W and re-trace ──

    def _handle_invert(self):
        try:
            data = json.loads(self._read_body())
        except json.JSONDecodeError:
            self._send_error(400, "Invalid JSON")
            return

        ext_id = data.get("extraction_id")
        rec = extractions.get(ext_id)
        if not rec:
            self._send_error(404, "Extraction not found")
            return

        png_path = rec.get("png_path", "")
        if not os.path.isfile(png_path):
            self._send_error(500, "Extraction PNG missing")
            return

        # Inversion becomes the new baseline only after tracing succeeds.
        bw = cv2.imread(png_path, cv2.IMREAD_GRAYSCALE)
        if bw is None:
            self._send_error(500, "Could not read extraction PNG")
            return

        bw = cv2.bitwise_not(bw)

        if not self._update_extraction(rec, bw, reset_baseline=True):
            return
        print(f"  Invert {ext_id}: {bw.shape[1]}x{bw.shape[0]}px → SVG")
        self._send_json({"id": ext_id, "svg": rec["svg"], "fuzz": 0})

    # ── POST /retrace — receive edited PNG, re-run potrace ──

    def _handle_retrace(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_error(400, "Expected multipart/form-data")
            return

        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip('"')
        if not boundary:
            self._send_error(400, "No boundary")
            return

        body = self._read_body()

        # Parse fields from multipart
        delim = f"--{boundary}".encode()
        parts = body.split(delim)
        file_data = None
        ext_id = None
        for part in parts:
            if b'name="extraction_id"' in part:
                idx = part.find(b"\r\n\r\n")
                if idx != -1:
                    ext_id = part[idx + 4:].strip().decode()
            elif b'name="image"' in part:
                idx = part.find(b"\r\n\r\n")
                if idx != -1:
                    file_data = part[idx + 4:]
                    if file_data.endswith(b"\r\n"):
                        file_data = file_data[:-2]
                    if file_data.endswith(b"--"):
                        file_data = file_data[:-2]
                    if file_data.endswith(b"\r\n"):
                        file_data = file_data[:-2]

        if not ext_id or not file_data:
            self._send_error(400, "Missing extraction_id or image")
            return

        rec = extractions.get(ext_id)
        if not rec:
            self._send_error(404, "Extraction not found")
            return

        # Decode the uploaded PNG
        img_array = np.frombuffer(file_data, dtype=np.uint8)
        bw = cv2.imdecode(img_array, cv2.IMREAD_GRAYSCALE)
        if bw is None:
            self._send_error(400, "Could not decode image")
            return

        # Threshold to clean B&W
        _, bw = cv2.threshold(bw, 128, 255, cv2.THRESH_BINARY)

        if not self._update_extraction(rec, bw, reset_baseline=True):
            return
        print(f"  Retrace {ext_id}: {bw.shape[1]}x{bw.shape[0]}px → SVG")
        self._send_json({"id": ext_id, "svg": rec["svg"], "fuzz": 0})

    # ── POST /print-preview — advisory only; never mutates artwork or jobs ──

    def _handle_print_preview(self):
        try:
            data = json.loads(self._read_body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_error(400, "Invalid JSON")
            return
        if not isinstance(data, dict):
            self._send_error(400, "Expected a JSON object")
            return
        ext_id = data.get("extraction_id")
        rec = extractions.get(ext_id) if isinstance(ext_id, str) else None
        if not rec:
            self._send_error(404, "Extraction not found")
            return
        mode = data.get("mode", "raised")
        if mode not in ("raised", "concave"):
            self._send_error(400, "mode must be raised or concave")
            return
        thin_lines = data.get("thin_lines", False)
        if not isinstance(thin_lines, bool):
            self._send_error(400, "thin_lines must be true or false")
            return
        svg_content = rec["svg"]
        try:
            preserve, minimum = preservation_settings(data, thin_lines)
            sizing = {
                "width_mm": data.get("width_mm", 36),
                "height_mm": data.get("height_mm"),
                "total_height_mm": data.get("total_height_mm", 22),
                "body_shape": data.get("body_shape", "auto"),
                "thicken": "auto" if thin_lines else "0",
                "fit": data.get("fit", "margin"),
            }
            dimensions = resolve_dimensions(svg_content, **sizing)
            with preview_lock:
                original = svg_content if preserve else None
                report = None
                if preserve:
                    svg_content, report = preserve_strokes(svg_content, dimensions, minimum)
                    # Match the exporter, which resolves the preserved SVG: a
                    # round full bleed fits measured ink that widening can extend.
                    dimensions = resolve_dimensions(svg_content, **sizing)
                result = build_print_preview(svg_content, dimensions, mode, original_svg=original)
                result["preservation"] = report
        except ValueError as e:
            self._send_error(400, str(e))
            return
        except (RuntimeError, OSError, cv2.error) as e:
            self._send_error(500, f"Could not build nozzle preview: {e}")
            return
        self._send_json(result)

    # ── POST /nozzle-size — advisory: smallest body that keeps one nozzle's detail ──

    def _handle_nozzle_size(self):
        try:
            data = json.loads(self._read_body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_error(400, "Invalid JSON")
            return
        if not isinstance(data, dict):
            self._send_error(400, "Expected a JSON object")
            return
        ext_id = data.get("extraction_id")
        rec = extractions.get(ext_id) if isinstance(ext_id, str) else None
        if not rec:
            self._send_error(404, "Extraction not found")
            return
        mode = data.get("mode", "raised")
        if mode not in ("raised", "concave"):
            self._send_error(400, "mode must be raised or concave")
            return
        thin_lines = data.get("thin_lines", False)
        if not isinstance(thin_lines, bool):
            self._send_error(400, "thin_lines must be true or false")
            return
        settings = {
            "width_mm": data.get("width_mm", 36),
            "height_mm": data.get("height_mm"),
            "total_height_mm": data.get("total_height_mm", 22),
            "body_shape": data.get("body_shape", "auto"),
            "thicken": "auto" if thin_lines else "0",
            "fit": data.get("fit", "margin"),
        }
        try:
            with preview_lock:
                result = size_for_nozzle(rec["svg"], settings, mode,
                                         data.get("nozzle_mm"), data.get("tolerance_percent"))
        except ValueError as e:
            self._send_error(400, str(e))
            return
        except (RuntimeError, OSError, cv2.error) as e:
            self._send_error(500, f"Could not size stamp for nozzle: {e}")
            return
        self._send_json(result)

    # ── POST /stamp → starts background job, returns job_id ──

    def _handle_stamp(self):
        try:
            data = json.loads(self._read_body())
        except json.JSONDecodeError:
            self._send_error(400, "Invalid JSON")
            return

        if not isinstance(data, dict):
            self._send_error(400, "Expected a JSON object")
            return
        mode = data.get("mode", "raised")
        if mode not in ("raised", "concave"):
            self._send_error(400, "mode must be raised or concave")
            return

        ext_id = data.get("extraction_id")
        rec = extractions.get(ext_id)
        if not rec:
            self._send_error(404, "Extraction not found")
            return

        if not os.path.isfile(PNG2STAMP):
            self._send_error(500, f"png2stamp.sh not found at {PNG2STAMP}")
            return

        # Write extraction SVG to a temp file for png2stamp.sh
        # Using SVG directly preserves connection quality from the extraction
        svg_content = rec.get("svg", "")
        if not svg_content:
            self._send_error(500, "Extraction SVG missing")
            return
        print(f"  [stamp] Using SVG directly ({len(svg_content)} chars)")

        if "size_mm" in data:
            self._send_error(400, "Stamp sizing now uses overall width, height and total height. Refresh the page.")
            return
        thin_lines = data.get("thin_lines", False)
        if not isinstance(thin_lines, bool):
            self._send_error(400, "thin_lines must be true or false")
            return
        body_shape = data.get("body_shape", "auto")
        try:
            preserve, minimum = preservation_settings(data, thin_lines)
            dimensions = resolve_dimensions(
                svg_content,
                width_mm=data.get("width_mm", 36),
                height_mm=data.get("height_mm"),
                total_height_mm=data.get("total_height_mm", 22),
                body_shape=body_shape,
                thicken="auto" if thin_lines else "0",
                fit=data.get("fit", "margin"),
            )
        except ValueError as e:
            self._send_error(400, str(e))
            return
        except (RuntimeError, OSError, cv2.error) as e:
            self._send_error(500, f"Could not fit artwork: {e}")
            return

        name = data.get("name", rec["name"])
        if mode == "concave":
            name += "_concave"
        job_id = str(uuid.uuid4())[:8]
        stamp_jobs[job_id] = {
            "status": "running", "path": None, "name": name,
            "error": None, "dimensions": dimensions,
        }

        # Run in background thread
        def run_stamp():
            tmp_dir = tempfile.mkdtemp()
            svg_path = os.path.join(tmp_dir, "input.svg")
            output_base = os.path.join(tmp_dir, name)
            try:
                processed_svg = svg_content
                if preserve:
                    with preview_lock:
                        processed_svg, report = preserve_strokes(svg_content, dimensions, minimum)
                    stamp_jobs[job_id]["preservation"] = report
                with open(svg_path, "w") as f:
                    f.write(processed_svg)
                cmd = [
                    "bash", PNG2STAMP, svg_path, output_base,
                    str(dimensions["width_mm"]),
                    str(dimensions["height_mm"]) if body_shape == "rectangular" else "auto",
                    str(dimensions["total_height_mm"]), body_shape, mode,
                    "auto" if thin_lines else "0",
                    dimensions["fit"],
                ]
                print(f"  [stamp] Running: {' '.join(cmd)}")
                env = os.environ.copy()
                env["STAMP_PYTHON"] = sys.executable
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
                print(f"  [stamp] stdout: {result.stdout[:500]}")
                if result.stderr:
                    print(f"  [stamp] stderr: {result.stderr[:500]}")
                if result.returncode != 0:
                    stamp_jobs[job_id]["status"] = "error"
                    stamp_jobs[job_id]["error"] = f"png2stamp.sh failed:\n{result.stderr}"
                    print(f"  Stamp job {job_id} failed")
                    return
            except subprocess.TimeoutExpired:
                stamp_jobs[job_id]["status"] = "error"
                stamp_jobs[job_id]["error"] = "Timed out (>5min)"
                return
            except (ValueError, RuntimeError, OSError, cv2.error) as e:
                stamp_jobs[job_id]["status"] = "error"
                stamp_jobs[job_id]["error"] = f"Could not prepare stamp: {e}"
                return

            tmf_path = output_base + ".3mf"
            if not os.path.isfile(tmf_path):
                stamp_jobs[job_id]["status"] = "error"
                stamp_jobs[job_id]["error"] = "3mf file not generated"
                return

            stamp_jobs[job_id]["path"] = tmf_path
            stamp_jobs[job_id]["status"] = "done"
            print(f"  Stamp job {job_id} done: {name}.3mf")

        threading.Thread(target=run_stamp, daemon=True).start()
        print(f"  Stamp job {job_id} started for {name}")
        self._send_json({"job_id": job_id, "dimensions": dimensions})

    # ── GET /stamp/status/<job_id> ──

    def _handle_stamp_status(self, job_id):
        job = stamp_jobs.get(job_id)
        if not job:
            self._send_error(404, "Job not found")
            return
        resp = {"status": job["status"], "name": job["name"], "dimensions": job["dimensions"]}
        if "preservation" in job:
            resp["preservation"] = job["preservation"]
        if job["error"]:
            resp["error"] = job["error"]
        self._send_json(resp)

    # ── GET /stamp/download/<job_id> ──

    def _handle_stamp_download(self, job_id):
        job = stamp_jobs.get(job_id)
        if not job:
            self.send_error(404)
            return
        if job["status"] != "done" or not job["path"]:
            self._send_error(400, "Not ready")
            return
        try:
            with open(job["path"], "rb") as f:
                tmf_data = f.read()
        except FileNotFoundError:
            self._send_error(500, "File missing")
            return
        filename = f"{job['name']}.3mf"
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.ms-package.3dmanufacturing-3dmodel+xml")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", len(tmf_data))
        self.end_headers()
        self.wfile.write(tmf_data)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    request_queue_size = 128
    # Cloud Run instances are small; cap concurrency so stamp renders (OpenSCAD)
    # and image work cannot exhaust memory on a shared container.
    _semaphore = threading.BoundedSemaphore(int(os.environ.get("STAMP_MAX_WORKERS", "32")))

    def process_request(self, request, client_address):
        with self._semaphore:
            super().process_request(request, client_address)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT

    # Try the requested port, then scan upward if busy
    for p in range(port, port + 20):
        try:
            server = ThreadedHTTPServer((HOST, p), StampHandler)
            print(f"Stamp Extractor running at http://{HOST}:{p}")
            print("Press Ctrl+C to stop.\n")
            server.serve_forever()
            break
        except OSError as e:
            if "Address already in use" in str(e) or e.errno == 48:
                continue
            raise
        except KeyboardInterrupt:
            print("\nShutting down.")
            server.server_close()
            break
    else:
        print(f"Error: Could not find a free port in range {port}-{port+19}")
        sys.exit(1)


if __name__ == "__main__":
    main()
