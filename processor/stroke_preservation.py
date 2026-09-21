"""Selective, physical-scale additions; keep the source vector artwork intact.

Ink centerlines below the requested local width grow toward a disk footprint.
Negative-space centerlines reserve a corridor before topology-safe pixel growth.
The original SVG is then overlaid with traced additions, never replaced by a
retrace. All raster coordinates map back to the unchanged document viewport.
"""

import math
import subprocess
import time
import xml.etree.ElementTree as ET

import cv2
import numpy as np

from stamp_geometry import SVG_DPI, svg_dimensions


_SVG = "http://www.w3.org/2000/svg"
# OpenSCAD's SVG importer expects unprefixed SVG element names.
ET.register_namespace("", _SVG)
_SAMPLE_MM = 0.025
_MAX_SIDE = 4000
_ALGORITHM_SECONDS = 90
# Clockwise neighbors, starting north. Masks contain 0/1, not 0/255.
_NEIGHBORS = ((-1, 0), (-1, 1), (0, 1), (1, 1),
              (1, 0), (1, -1), (0, -1), (-1, -1))
_NEIGHBOR_KERNEL = np.array(((128, 1, 2), (64, 0, 4), (32, 16, 8)), dtype=np.float32)


def _number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        value = float(value)
    except OverflowError:
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _lookup_tables():
    """Guo–Hall thinning and (8-connected ink, 4-connected air) simple points."""
    thinning = np.zeros((2, 256), dtype=np.uint8)
    additions = np.zeros(256, dtype=np.uint8)
    for pattern in range(256):
        p = [(pattern >> bit) & 1 for bit in range(8)]
        transitions = sum((not p[i]) and (p[(i + 1) % 8] or p[(i + 2) % 8])
                          for i in (0, 2, 4, 6))
        n1 = sum(p[(i - 1) % 8] or p[i] for i in (0, 2, 4, 6))
        n2 = sum(p[i] or p[i + 1] for i in (0, 2, 4, 6))
        if transitions == 1 and 2 <= min(n1, n2) <= 3:
            thinning[0, pattern] = not ((p[4] or p[5] or not p[7]) and p[6])
            thinning[1, pattern] = not ((p[0] or p[1] or not p[3]) and p[2])

        # Only air components touching the center by an edge participate in
        # its 4-connected topology; diagonal-only air is not joined by it.
        counts = []
        for ink in (True, False):
            remaining = {i for i in range(8) if bool(p[i]) == ink}
            count = 0
            while remaining:
                start = min(remaining)
                remaining.remove(start)
                component = {start}
                pending = [start]
                while pending:
                    current = pending.pop()
                    y, x = _NEIGHBORS[current]
                    neighbors = {i for i in remaining
                                 if (max(abs(y - _NEIGHBORS[i][0]),
                                         abs(x - _NEIGHBORS[i][1])) == 1 if ink
                                     else abs(y - _NEIGHBORS[i][0]) +
                                     abs(x - _NEIGHBORS[i][1]) == 1)}
                    remaining.difference_update(neighbors)
                    component.update(neighbors)
                    pending.extend(neighbors)
                if ink or component.intersection((0, 2, 4, 6)):
                    count += 1
            counts.append(count)
        additions[pattern] = counts == [1, 1]
    return thinning, additions


_THINNING, _SIMPLE_ADDITION = _lookup_tables()


def _check_deadline(deadline):
    if time.monotonic() > deadline:
        raise RuntimeError("Thin-stroke preservation exceeded its processing time limit")


def _thin(mask, iterations, deadline):
    """Retain thin centerlines without spending iterations skeletonizing wide cores."""
    result = mask.copy()
    for _ in range(iterations):
        _check_deadline(deadline)
        removed = 0
        for table in _THINNING:
            neighbors = cv2.filter2D(result, -1, _NEIGHBOR_KERNEL,
                                     borderType=cv2.BORDER_CONSTANT)
            deletion = cv2.LUT(neighbors, table)
            cv2.bitwise_and(deletion, result, dst=deletion)
            # A fixed rim makes the padded exterior an anchored air component.
            deletion[0, :] = deletion[-1, :] = 0
            deletion[:, 0] = deletion[:, -1] = 0
            removed += cv2.countNonZero(deletion)
            cv2.subtract(result, deletion, dst=result)
        if not removed:
            break
    return result


def _disk(radius):
    extent = math.ceil(radius)
    y, x = np.ogrid[-extent:extent + 1, -extent:extent + 1]
    return (x * x + y * y <= radius * radius + 1e-9).astype(np.uint8)


def _grow_without_connections(original, candidates, radius, deadline):
    """Add only digital simple points; never merge ink, close a loop, or erase air.

    Four parity classes have no adjacent pixels, so each class can be updated
    simultaneously without invalidating its local topology decisions. Working
    only on proposed additions avoids repeated full-canvas topology scans.
    """
    result = original.copy()
    y, x = np.nonzero(candidates)
    phases = []
    for parity in range(4):
        selected = ((y % 2) * 2 + x % 2) == parity
        phases.append((y[selected], x[selected]))
    for _ in range(2 * math.ceil(radius) + 4):
        _check_deadline(deadline)
        added = 0
        for phase, (y, x) in enumerate(phases):
            if not y.size:
                continue
            pattern = np.zeros(y.size, dtype=np.uint8)
            for bit, (dy, dx) in enumerate(_NEIGHBORS):
                pattern |= result[y + dy, x + dx] << bit
            accepted = _SIMPLE_ADDITION[pattern] != 0
            added += int(np.count_nonzero(accepted))
            result[y[accepted], x[accepted]] = 1
            phases[phase] = (y[~accepted], x[~accepted])
        if not added:
            break
    return result


def _rasterize(root, source_width, source_height, scale, pitch, width, height):
    """Use an exact-pitch, top-left aligned canvas; ceiling padding is internal."""
    canvas = ET.Element(f"{{{_SVG}}}svg", {
        "width": str(width), "height": str(height),
        "viewBox": f"0 0 {width * pitch!r} {height * pitch!r}",
    })
    group = ET.SubElement(canvas, f"{{{_SVG}}}g", {"transform": f"scale({scale!r})"})
    root.set("width", repr(source_width))
    root.set("height", repr(source_height))
    root.set("x", "0")
    root.set("y", "0")
    group.append(root)
    try:
        result = subprocess.run(
            ["rsvg-convert", "--background-color=white", "--format=png"],
            input=ET.tostring(canvas, encoding="utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Thin-stroke SVG rasterization timed out") from None
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Could not rasterize thin strokes with rsvg-convert") from exc
    try:
        raster = cv2.imdecode(np.frombuffer(result.stdout, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    except cv2.error as exc:
        raise RuntimeError("Could not decode thin-stroke rasterization") from exc
    if raster is None or raster.shape != (height, width):
        raise RuntimeError("Could not decode full-resolution thin-stroke rasterization")
    return (raster < 128).astype(np.uint8)


def _trace_additions(mask):
    height, width = mask.shape
    pbm = f"P4\n{width} {height}\n".encode("ascii") + np.packbits(mask, axis=1).tobytes()
    try:
        result = subprocess.run(
            ["potrace", "--svg", "--turdsize", "0", "--alphamax", "0",
             "--longcurve", "--turnpolicy", "black", "--output", "-", "-"],
            input=pbm, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Thin-stroke vector tracing timed out") from None
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Could not trace preserved strokes with potrace") from exc
    try:
        traced = ET.fromstring(result.stdout)
        viewbox = [float(value) for value in traced.get("viewBox", "").split()]
    except (ET.ParseError, ValueError):
        raise RuntimeError("Potrace returned an invalid preserved-stroke SVG") from None
    if viewbox != [0.0, 0.0, float(width), float(height)]:
        raise RuntimeError("Potrace changed the preserved-stroke viewport")
    groups = [child for child in traced if child.tag.rsplit("}", 1)[-1] == "g"]
    if not groups:
        raise RuntimeError("Potrace returned no preserved-stroke geometry")
    return groups


def preserve_strokes(svg_content, dimensions, min_stroke_mm):
    """Return an independent SVG and a physical-scale preservation report.

    ``added_percent`` is added sampled area / original sampled ink area.
    ``conflict_percent`` is the percentage of initially thin centerline samples
    whose target disk remains incomplete after gap, topology, and viewport
    protection. These are geometric estimates, not slicer/printability claims.
    Input dimensions must be the resolver's unreinforced, aspect-preserving fit.
    """
    target = _number(min_stroke_mm, "min_stroke_mm", 0.15, 1.2)
    if not isinstance(dimensions, dict):
        raise ValueError("Resolved stamp dimensions are required")
    design_width = _number(dimensions.get("design_width_mm"), "design_width_mm", 1e-9, 200)
    design_height = _number(dimensions.get("design_height_mm"), "design_height_mm", 1e-9, 200)
    svg_width, svg_height = svg_dimensions(svg_content)
    for name, actual in (("svg_width_mm", svg_width), ("svg_height_mm", svg_height)):
        supplied = _number(dimensions.get(name), name, 1e-9, 1e9)
        if not math.isclose(supplied, actual, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("Resolved SVG dimensions do not match the source artwork")
    if not math.isclose(design_width / svg_width, design_height / svg_height,
                        rel_tol=1e-7, abs_tol=1e-12):
        raise ValueError("Resolved artwork dimensions must preserve the SVG aspect ratio")

    # Normal stamps use 0.025 mm; a 200 mm square is bounded to 4000² pixels
    # before a small algorithmic border. No bitmap follows the SVG's native DPI.
    pitch = max(_SAMPLE_MM, max(design_width, design_height) / _MAX_SIDE)
    width = min(_MAX_SIDE, math.ceil(design_width / pitch))
    height = min(_MAX_SIDE, math.ceil(design_height / pitch))
    radius = target / (2 * pitch)
    pad = 2 * math.ceil(radius) + 4
    source_width = svg_width * SVG_DPI / 25.4
    source_height = svg_height * SVG_DPI / 25.4
    scale = design_width / source_width
    root = ET.fromstring(svg_content)
    original_width, original_height = root.get("width"), root.get("height")
    mask = _rasterize(root, source_width, source_height, scale, pitch, width, height)
    original_area = cv2.countNonZero(mask)
    report = {
        "min_stroke_mm": target, "added_percent": 0.0, "conflict_percent": 0.0,
        "warning": "",
    }
    if not original_area:
        report["warning"] = (
            f"No foreground resolved at {pitch:g} mm sampling; source vectors are unchanged. "
            "Subpixel strokes cannot be assessed. This is not a printability guarantee."
        )
        return svg_content, report

    deadline = time.monotonic() + _ALGORITHM_SECONDS
    original = cv2.copyMakeBorder(mask, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    del mask
    distance = cv2.distanceTransform(original, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    centerline = _thin(original, 2 * math.ceil(radius) + 4, deadline)
    centerline[distance >= radius] = 0
    del distance
    centerline_count = cv2.countNonZero(centerline)
    if not centerline_count:
        report["warning"] = (
            f"No thin centerlines resolved below {target:g} mm at {pitch:g} mm sampling; "
            "source vectors are unchanged. Local widths are approximate, not a printability guarantee."
        )
        return svg_content, report

    kernel = _disk(radius)
    desired = cv2.dilate(centerline, kernel, borderType=cv2.BORDER_CONSTANT, borderValue=0)
    air = 1 - original
    distance = cv2.distanceTransform(air, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    gaps = _thin(air, 4 * math.ceil(radius) + 6, deadline)
    # Only gaps close enough for the proposed growth to threaten them matter.
    # Unthinned broad exterior cores must not be mistaken for narrow gaps.
    gaps[distance >= 2 * radius + 1] = 0
    del distance
    reserved = cv2.dilate(gaps, kernel, borderType=cv2.BORDER_CONSTANT, borderValue=0)
    del gaps
    candidates = desired & air & (1 - reserved)
    del reserved, air
    # Last fractional raster cells are never exported as additions: this keeps
    # the exact original viewport even for importers that ignore SVG clipping.
    full_width = min(width, math.floor(design_width / pitch + 1e-9))
    full_height = min(height, math.floor(design_height / pitch + 1e-9))
    candidates[:pad, :] = candidates[pad + full_height:, :] = 0
    candidates[:, :pad] = candidates[:, pad + full_width:] = 0
    grown = _grow_without_connections(original, candidates, radius, deadline)
    del candidates
    additions = grown - original
    added_area = cv2.countNonZero(additions)
    report["added_percent"] = 100.0 * added_area / original_area
    unfilled = desired & (1 - grown)
    if cv2.countNonZero(unfilled):
        blocked = cv2.dilate(unfilled, kernel, borderType=cv2.BORDER_CONSTANT, borderValue=0)
        report["conflict_percent"] = 100.0 * cv2.countNonZero(blocked & centerline) / centerline_count
    del desired, centerline, unfilled, grown
    report["warning"] = (
        f"Approximate selective widening at {pitch:g} mm sampling; original vectors are retained. "
        "Added area is relative to sampled original ink; limited thin strokes are the percentage "
        "of thin centerline samples whose target disk could not fit while protecting gaps, "
        "topology, or the original viewport. Existing gaps below the target are not repaired. "
        "Subpixel gaps and vector tracing can differ from this sampled estimate. "
        "This is not a slicer simulation or printability guarantee."
    )
    if not added_area:
        return svg_content, report

    # An overlapping one-pixel ink rim joins traced additions to the exact
    # original vector boundary despite raster sampling and polygon fitting.
    overlap = cv2.dilate(additions, np.ones((3, 3), dtype=np.uint8)) & original
    additions |= overlap
    del overlap, original
    additions = additions[pad:pad + height, pad:pad + width].copy()
    additions[full_height:, :] = 0
    additions[:, full_width:] = 0
    _check_deadline(deadline)
    traced_groups = _trace_additions(additions)

    # Keep a single SVG viewport: OpenSCAD interprets nested SVG sizing
    # differently from librsvg. Map sampled CSS coordinates into the source's
    # existing viewBox instead of nesting the untouched source document.
    output = ET.fromstring(svg_content)
    output.set("width", original_width if original_width is not None else f"{svg_width!r}mm")
    output.set("height", original_height if original_height is not None else f"{svg_height!r}mm")
    sx = sy = 1.0
    tx = ty = 0.0
    if output.get("viewBox"):
        vx, vy, vw, vh = [float(value) for value in output.get("viewBox").replace(",", " ").split()]
        sx, sy = source_width / vw, source_height / vh
        aspect = output.get("preserveAspectRatio", "xMidYMid meet").split()
        if aspect[0] == "defer":
            aspect = aspect[1:]
        if aspect[0] != "none":
            sx = sy = max(sx, sy) if "slice" in aspect else min(sx, sy)
            align = aspect[0]
            tx = (source_width - vw * sx) * (0 if "xMin" in align else 1 if "xMax" in align else .5)
            ty = (source_height - vh * sy) * (0 if "YMin" in align else 1 if "YMax" in align else .5)
        tx -= vx * sx
        ty -= vy * sy
    added_group = ET.SubElement(output, f"{{{_SVG}}}g", {
        "transform": f"matrix({pitch / scale / sx!r} 0 0 {pitch / scale / sy!r} {-tx / sx!r} {-ty / sy!r})",
        "fill": "black", "stroke": "none",
    })
    added_group.extend(traced_groups)
    return ET.tostring(output, encoding="unicode"), report
