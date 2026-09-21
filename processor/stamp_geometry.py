"""Resolve overall stamp dimensions and generate face-up OpenSCAD geometry."""

import argparse
import json
import math
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET


MARGIN_MM = 1.5
RELIEF_MM = 2.8
MOUTH_MM = 0.2
SVG_DPI = 96
ROUND_SEGMENTS = 256  # A multiple of four keeps both diameter bounds exact.
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_LENGTH = re.compile(rf"({_NUMBER})\s*(px|pt|pc|in|mm|cm)?")
_MM_PER_UNIT = {
    "": 25.4 / SVG_DPI,
    "px": 25.4 / SVG_DPI,
    "pt": 25.4 / 72,
    "pc": 25.4 / 6,
    "in": 25.4,
    "mm": 1,
    "cm": 10,
}


def _number(value, name, minimum, maximum, *, allow_text=False):
    if allow_text and isinstance(value, str):
        if not re.fullmatch(_NUMBER, value.strip()):
            raise ValueError(f"{name} must be a finite number")
        value = float(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        value = float(value)
    except OverflowError:
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def svg_dimensions(svg_content):
    """Return the SVG viewport in mm, matching import(..., dpi=96)."""
    try:
        root = ET.fromstring(svg_content)
    except (ET.ParseError, TypeError, ValueError):
        raise ValueError("Artwork must contain a valid SVG document") from None
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise ValueError("Artwork must contain an SVG root element")

    viewbox = root.get("viewBox")
    if viewbox is not None:
        parts = re.split(r"[\s,]+", viewbox.strip())
        if len(parts) != 4:
            raise ValueError("SVG viewBox must contain four finite numbers")
        values = [
            _number(part, "SVG viewBox", -1e9, 1e9, allow_text=True)
            for part in parts
        ]
        if values[2] <= 0 or values[3] <= 0:
            raise ValueError("SVG viewBox width and height must be positive")
    else:
        values = None

    dimensions = []
    for name, index in (("width", 2), ("height", 3)):
        raw = root.get(name)
        if raw is None:
            if values is None:
                raise ValueError("SVG needs width and height or a viewBox")
            size = values[index] * _MM_PER_UNIT[""]
        else:
            match = _LENGTH.fullmatch(raw.strip())
            if match is None:
                raise ValueError(f"SVG {name} must use px, pt, pc, in, mm or cm")
            size = float(match.group(1)) * _MM_PER_UNIT[match.group(2) or ""]
        dimensions.append(_number(size, f"SVG {name} in mm", 1e-9, 1e9))
    return tuple(dimensions)


def resolve_dimensions(svg_content, width_mm=36, height_mm=None,
                       total_height_mm=22, body_shape="auto", thicken="0"):
    """Validate overall dimensions and uniformly fit artwork within the body.

    Auto derives a rectangular body from the SVG aspect and 1.5 mm margins.
    Round uses width as diameter. Explicit round height must match the diameter.
    Reinforcement and cavity-mouth expansion are reserved in both modes so the
    same artwork has the same scale and mirror orientation in either mode.

    ``width_mm`` and ``height_mm`` are the on-screen horizontal and vertical
    extents of the body, and they match the raster preview's X and Y axes.
    The SVG's own X/Y axes are the same axes: the extracted artwork is written
    in image pixel space, so a portrait source yields a portrait body. Callers
    must NOT swap width and height to "fix" a tall artwork; pass the intended
    horizontal size as ``width_mm`` and let auto height follow the aspect.
    """
    if not isinstance(body_shape, str) or body_shape not in ("auto", "rectangular", "round"):
        raise ValueError("body_shape must be auto, rectangular or round")
    width = _number(width_mm, "width_mm", 12, 200)
    total = _number(total_height_mm, "total_height_mm", 8, 60)
    svg_width, svg_height = svg_dimensions(svg_content)

    if body_shape == "rectangular":
        height = _number(height_mm, "height_mm", 12, 200)
    elif body_shape == "round":
        if height_mm is not None:
            height = _number(height_mm, "height_mm", 12, 200)
            if height != width:
                raise ValueError("Round body height must equal its width (diameter)")
        height = width
    else:
        if height_mm is not None:
            raise ValueError("Auto body height is derived; use rectangular for an explicit height")
        height = max(12.0, (width - 2 * MARGIN_MM) * svg_height / svg_width + 2 * MARGIN_MM)
        if not math.isfinite(height) or height > 200:
            raise ValueError("Auto body height exceeds 200 mm; reduce width or use rectangular")

    if isinstance(thicken, str) and thicken == "auto":
        thickening = max(0.0, 0.30 * (1 - min(width, height) / 36))
    else:
        thickening = _number(thicken, "thicken_mm", 0, 2, allow_text=True)
    inset = MARGIN_MM + thickening + MOUTH_MM
    if body_shape == "round":
        # Fit every bounding-box corner inside the polygon's incircle, not just
        # inside a square of the same diameter. Rounded offsets cannot escape.
        radius = width / 2 * math.cos(math.pi / ROUND_SEGMENTS) - inset
        scale = 2 * radius / math.hypot(svg_width, svg_height)
    else:
        scale = min((width - 2 * inset) / svg_width,
                    (height - 2 * inset) / svg_height)

    return {
        "width_mm": width,
        "height_mm": height,
        "total_height_mm": total,
        "body_shape": body_shape,
        "design_width_mm": svg_width * scale,
        "design_height_mm": svg_height * scale,
        "thicken_mm": thickening,
        "svg_width_mm": svg_width,
        "svg_height_mm": svg_height,
        "scale": scale,
    }


def scad_source(svg_file, dimensions, mode):
    """Create a broad, continuously supported block with mirrored face artwork."""
    if mode not in ("raised", "concave"):
        raise ValueError("mode must be raised or concave")
    d = dimensions
    return f'''// Ceramic stamp: all dimensions are millimeters, including grip and relief.
// Print FACE UP: broad grip on the plate, artwork on top; no supports needed.
// Artwork is uniformly scaled and mirrored once, identically in both modes.
body_w = {d["width_mm"]!r};
body_h = {d["height_mm"]!r};
total_h = {d["total_height_mm"]!r};
body_shape = "{d["body_shape"]}";
stamp_mode = "{mode}";
relief = {RELIEF_MM!r};
mouth = {MOUTH_MM!r};
thicken = {d["thicken_mm"]!r};
art_scale = {d["scale"]!r};
floor_h = total_h - relief;
e = 0.01;
$fn = {ROUND_SEGMENTS};

module body_2d() {{
    if (body_shape == "round")
        circle(d = body_w);
    else
        square([body_w, body_h], center = true);
}}

module artwork_2d() {{
    scale([art_scale, art_scale])
        mirror([1, 0, 0])
            import({json.dumps(str(svg_file), ensure_ascii=False)}, center = true, dpi = {SVG_DPI});
}}

module design_2d() {{
    // Rounded offsets have bounded expansion; retain native path holes.
    if (thicken > 0)
        offset(r = thicken) artwork_2d();
    else
        artwork_2d();
}}

if (stamp_mode == "concave") {{
    difference() {{
        linear_extrude(height = total_h, convexity = 10) body_2d();
        // Every cavity and island rests on the same uninterrupted solid floor.
        translate([0, 0, floor_h])
            linear_extrude(height = relief + e, convexity = 10) design_2d();
        // Widen the final 0.2 mm of the cavity mouth for clay release.
        translate([0, 0, total_h - mouth])
            linear_extrude(height = mouth + e, convexity = 10)
                offset(r = mouth) design_2d();
    }}
}} else {{
    union() {{
        // A full-footprint block connects even disconnected artwork.
        linear_extrude(height = floor_h, convexity = 10) body_2d();
        translate([0, 0, floor_h - e])
            linear_extrude(height = relief + e, convexity = 10) design_2d();
    }}
}}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_svg", type=Path)
    parser.add_argument("output_scad", type=Path)
    parser.add_argument("width_mm")
    parser.add_argument("height_mm_or_auto")
    parser.add_argument("total_height_mm")
    parser.add_argument("body_shape", choices=("auto", "rectangular", "round"))
    parser.add_argument("mode", choices=("raised", "concave"))
    parser.add_argument("thicken_mm_or_auto")
    args = parser.parse_args()
    try:
        width = _number(args.width_mm, "width_mm", 12, 200, allow_text=True)
        height = None if args.height_mm_or_auto == "auto" else _number(
            args.height_mm_or_auto, "height_mm", 12, 200, allow_text=True)
        total = _number(args.total_height_mm, "total_height_mm", 8, 60, allow_text=True)
        dimensions = resolve_dimensions(
            args.input_svg.read_text(), width, height, total,
            args.body_shape, args.thicken_mm_or_auto)
        # Sibling SVG makes the generated SCAD source portable with its artwork.
        svg_file = args.input_svg.name if args.input_svg.resolve().parent == args.output_scad.resolve().parent else args.input_svg.resolve()
        args.output_scad.write_text(scad_source(svg_file, dimensions, args.mode))
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"  Overall: {dimensions['width_mm']:g} x {dimensions['height_mm']:g} x "
          f"{dimensions['total_height_mm']:g} mm ({dimensions['body_shape']})")
    print(f"  Artwork: {dimensions['design_width_mm']:g} x "
          f"{dimensions['design_height_mm']:g} mm, mirrored, aspect preserved")
    print(f"  Relief: {RELIEF_MM:g} mm; solid floor: {total - RELIEF_MM:g} mm")
    print(f"  Reinforcement: {dimensions['thicken_mm']:g} mm; minimum margin: {MARGIN_MM:g} mm")
    return 0


if __name__ == "__main__":
    sys.exit(main())
