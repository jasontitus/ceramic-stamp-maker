"""Physical-scale, planar nozzle-detail estimates; never modify exported geometry."""

import base64
import math
import subprocess
import xml.etree.ElementTree as ET

import cv2
import numpy as np

from stamp_geometry import MAX_DESIGN_MM, MOUTH_MM, ROUND_SEGMENTS, SVG_DPI, svg_dimensions


SAMPLE_PITCH_MM = 0.05
# Artwork too fine to cover a single sample at this size has no pattern left:
# report it as entirely lost rather than as a perfect 0 of 0.
_EMPTY_LOST = 100.0
NOZZLES_MM = (0.2, 0.4, 0.6, 0.8)
_SVG = "http://www.w3.org/2000/svg"
_DISCLAIMER = (
    "Approximate planar minimum-feature and gap estimate at 0.05 mm sampling, "
    "using disk closing then opening of solid material, not a slicer or toolpaths. "
    "Nominal line width is 1.05 × nozzle diameter; sampling and actual variable-width "
    "slicer behavior can differ. The intended pattern includes selected reinforcement "
    "and, in concave mode, the 0.2 mm cavity-mouth widening. Shown in normal clay-reading "
    "orientation, not the mirrored stamp face: black is retained pattern, red is lost "
    "pattern, amber is added/unwanted pattern. Percentages are relative to intended "
    "pattern area (zero when that area is empty). This does not simulate height, layers, "
    "filament, pressure, clay deformation or variable line widths. It does not change "
    "stamp settings, exported SVG/mesh or the Bambu profile, which remains 0.4 mm-oriented. "
    "Select matching nozzle and line-width settings in Bambu Studio."
)


def _dimension(dimensions, name, minimum, maximum):
    value = dimensions.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        value = float(value)
    except OverflowError:
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _disk(radius_mm):
    """Sample a physical disk, not a square pixel-footprint nozzle model."""
    radius_px = radius_mm / SAMPLE_PITCH_MM
    extent = math.ceil(radius_px)
    y, x = np.ogrid[-extent:extent + 1, -extent:extent + 1]
    return (x * x + y * y <= radius_px * radius_px + 1e-9).astype(np.uint8)


def _filter_material(solid, body, line_width_mm):
    """Close narrow voids, then remove narrow solid features, independently."""
    kernel = _disk(line_width_mm / 2)
    # Closing must be free to dilate beyond the canvas before eroding again.
    # Explicit air padding avoids OpenCV's implicit erosion border conventions.
    pad = 2 * (kernel.shape[0] // 2) + 1
    padded = cv2.copyMakeBorder(solid, pad, pad, pad, pad,
                                cv2.BORDER_CONSTANT, value=0)
    closed = cv2.morphologyEx(padded, cv2.MORPH_CLOSE, kernel,
                              borderType=cv2.BORDER_CONSTANT, borderValue=0)
    cropped = closed[pad:-pad, pad:-pad]
    cv2.bitwise_and(cropped, body, dst=cropped)
    # The closing can leave material outside the body in the padded region.
    # Rebuild padding from the clipped result before opening.
    padded = cv2.copyMakeBorder(cropped, pad, pad, pad, pad,
                                cv2.BORDER_CONSTANT, value=0)
    del closed, cropped
    opened = cv2.morphologyEx(padded, cv2.MORPH_OPEN, kernel,
                              borderType=cv2.BORDER_CONSTANT, borderValue=0)
    return cv2.bitwise_and(opened[pad:-pad, pad:-pad], body)


def _rasterize(svg_content, width, height, design_width, design_height, shape):
    """Render a uniformly scaled SVG on a centered, exact-pitch physical canvas."""
    svg_width, svg_height = svg_dimensions(svg_content)
    root = ET.fromstring(svg_content)
    # Nested SVG lengths are in CSS pixels before this uniform physical transform.
    source_width = svg_width * SVG_DPI / 25.4
    source_height = svg_height * SVG_DPI / 25.4
    scale = design_width / source_width
    if not math.isclose(design_height, source_height * scale, rel_tol=1e-7, abs_tol=1e-7):
        raise ValueError("Resolved artwork dimensions must preserve the SVG aspect ratio")
    root.set("width", str(source_width))
    root.set("height", str(source_height))
    root.set("x", "0")
    root.set("y", "0")
    pixel_width = math.ceil(width / SAMPLE_PITCH_MM)
    pixel_height = math.ceil(height / SAMPLE_PITCH_MM)
    canvas_width = pixel_width * SAMPLE_PITCH_MM
    canvas_height = pixel_height * SAMPLE_PITCH_MM
    canvas = ET.Element(f"{{{_SVG}}}svg", {
        "width": str(pixel_width), "height": str(pixel_height),
        "viewBox": f"0 0 {canvas_width} {canvas_height}",
    })
    group = ET.SubElement(canvas, f"{{{_SVG}}}g", {
        "transform": (
            f"translate({(canvas_width - design_width) / 2} "
            f"{(canvas_height - design_height) / 2}) scale({scale})"
        ),
    })
    group.append(root)
    # stdin/stdout avoids persistent files and external relative-file resolution.
    try:
        result = subprocess.run(
            ["rsvg-convert", "--background-color=white", "--format=png"],
            input=ET.tostring(canvas, encoding="utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("SVG preview rasterization timed out") from None
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Could not rasterize SVG preview with rsvg-convert") from exc
    try:
        raster = cv2.imdecode(np.frombuffer(result.stdout, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    except cv2.error as exc:
        raise RuntimeError("Could not decode the full-resolution SVG preview") from exc
    if raster is None or raster.shape != (pixel_height, pixel_width):
        raise RuntimeError("Could not decode the full-resolution SVG preview")
    _, artwork = cv2.threshold(raster, 127, 255, cv2.THRESH_BINARY_INV)
    body = np.zeros_like(artwork)
    if shape == "round":
        # Match the actual 256-sided OpenSCAD body rather than its bounding square.
        angles = np.arange(ROUND_SEGMENTS) * (2 * math.pi / ROUND_SEGMENTS)
        vertices = np.column_stack((
            (canvas_width / 2 + width / 2 * np.cos(angles)) / SAMPLE_PITCH_MM - 0.5,
            (canvas_height / 2 + height / 2 * np.sin(angles)) / SAMPLE_PITCH_MM - 0.5,
        ))
        cv2.fillPoly(body, [np.rint(vertices * 256).astype(np.int32)], 255, shift=8)
    else:
        # Fractional dimensions are centered within the ceiling-sized canvas.
        x = (np.arange(pixel_width) + 0.5) * SAMPLE_PITCH_MM - canvas_width / 2
        y = (np.arange(pixel_height) + 0.5) * SAMPLE_PITCH_MM - canvas_height / 2
        body[:] = ((np.abs(y[:, None]) <= height / 2) &
                   (np.abs(x[None, :]) <= width / 2)).astype(np.uint8) * 255
    return artwork, body


def _png(image):
    try:
        success, encoded = cv2.imencode(".png", image)
    except cv2.error as exc:
        raise RuntimeError("Could not encode nozzle preview PNG") from exc
    if not success:
        raise RuntimeError("Could not encode nozzle preview PNG")
    return "data:image/png;base64," + base64.b64encode(encoded).decode("ascii")


def _intended_pattern(svg_content, dimensions, mode):
    """Rasterize the top-face pattern the stamp intends, clipped to its body."""
    if mode not in ("raised", "concave"):
        raise ValueError("mode must be raised or concave")
    if not isinstance(dimensions, dict):
        raise ValueError("Resolved stamp dimensions are required")
    width = _dimension(dimensions, "width_mm", 12, 200)
    height = _dimension(dimensions, "height_mm", 12, 200)
    _dimension(dimensions, "total_height_mm", 8, 60)
    # Only a full-bleed fit may overflow the body; the body then trims it.
    clip = dimensions.get("fit") in ("bleed", "fill")
    design_width = _dimension(dimensions, "design_width_mm", 1e-9, MAX_DESIGN_MM if clip else width)
    design_height = _dimension(dimensions, "design_height_mm", 1e-9, MAX_DESIGN_MM if clip else height)
    thicken = _dimension(dimensions, "thicken_mm", 0, 2)
    shape = dimensions.get("body_shape")
    if shape not in ("auto", "rectangular", "round"):
        raise ValueError("body_shape must be auto, rectangular or round")
    if shape == "round" and width != height:
        raise ValueError("Round body height must equal its width (diameter)")

    intended, body = _rasterize(svg_content, width, height, design_width, design_height, shape)
    if thicken > 0:
        intended = cv2.dilate(intended, _disk(thicken),
                              borderType=cv2.BORDER_CONSTANT, borderValue=0)
    if mode == "concave":
        intended = cv2.dilate(intended, _disk(MOUTH_MM),
                              borderType=cv2.BORDER_CONSTANT, borderValue=0)
    cv2.bitwise_and(intended, body, dst=intended)
    return intended, body


def _nozzle_pattern(intended, body, mode, nozzle):
    """Return the estimated printed pattern and its lost / gained masks."""
    line_width = round(nozzle * 1.05, 2)
    solid = intended if mode == "raised" else cv2.subtract(body, intended)
    filtered = _filter_material(solid, body, line_width)
    pattern = filtered if mode == "raised" else cv2.subtract(body, filtered)
    return line_width, cv2.subtract(intended, pattern), cv2.subtract(pattern, intended)


def _percent(mask, area, empty=0.0):
    return round(100 * cv2.countNonZero(mask) / area, 2) if area else empty


def nozzle_detail(svg_content, dimensions, mode, nozzle):
    """Lost / extra percentages for one nozzle, exactly as the comparison reports them."""
    intended, body = _intended_pattern(svg_content, dimensions, mode)
    area = cv2.countNonZero(intended)
    _, lost, gained = _nozzle_pattern(intended, body, mode, nozzle)
    return _percent(lost, area, _EMPTY_LOST), _percent(gained, area)


def build_print_preview(svg_content, dimensions, mode, original_svg=None):
    """Estimate four nozzles against the same full-resolution intended top pattern."""
    intended, body = _intended_pattern(svg_content, dimensions, mode)
    intended_area = cv2.countNonZero(intended)

    # Reuse one display canvas; each nozzle starts from the unfiltered solid mask.
    image = np.empty((*body.shape, 3), dtype=np.uint8)

    def paint():
        image[:] = 238
        image[body != 0] = 255
        image[intended != 0] = 0

    paint()
    ideal_png = _png(image)
    previews = []
    for nozzle in NOZZLES_MM:
        line_width, lost, gained = _nozzle_pattern(intended, body, mode, nozzle)
        paint()
        image[lost != 0] = (96, 69, 233)  # OpenCV BGR: #e94560
        image[gained != 0] = (0, 165, 240)  # OpenCV BGR: #f0a500
        previews.append({
            "nozzle_mm": nozzle,
            "line_width_mm": line_width,
            "png": _png(image),
            "lost_percent": _percent(lost, intended_area, _EMPTY_LOST),
            "gained_percent": _percent(gained, intended_area),
        })
        del lost, gained
    result = {
        "dimensions": dict(dimensions),
        "mode": mode,
        "sample_pitch_mm": SAMPLE_PITCH_MM,
        "ideal_png": ideal_png,
        "previews": previews,
        "disclaimer": _DISCLAIMER,
    }
    if original_svg is not None:
        intended, _ = _intended_pattern(original_svg, dimensions, mode)
        paint()
        result["original_png"] = _png(image)
    return result
