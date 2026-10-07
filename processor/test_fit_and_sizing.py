"""Full-bleed artwork fits and nozzle-driven body sizing."""

import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

import numpy as np

from nozzle_sizing import size_for_nozzle
from print_preview import build_print_preview, nozzle_detail
from stamp_geometry import MARGIN_MM, MOUTH_MM, ink_radius_mm, resolve_dimensions, scad_source


# 2:1 landscape rectangle filling its whole viewport.
SOLID = ('<svg xmlns="http://www.w3.org/2000/svg" width="40pt" height="20pt" '
         'viewBox="0 0 40 20"><path d="M0 0H40V20H0Z" fill="black"/></svg>')
# A disc touching all four viewport edges: its bounding-box corners hold no ink.
DISC = ('<svg xmlns="http://www.w3.org/2000/svg" width="30pt" height="30pt" '
        'viewBox="0 0 30 30"><circle cx="15" cy="15" r="15" fill="black"/></svg>')
# Twenty 1-unit bars separated by 1-unit gaps: every feature and gap is 1/39
# of the width, so a nozzle's line width sets a sharp minimum physical size.
# Tall bars keep the disk nozzle's corner rounding well below 1% of their area.
BARS = ('<svg xmlns="http://www.w3.org/2000/svg" width="39pt" height="100pt" viewBox="0 0 39 100">'
        + ''.join(f'<rect x="{2 * i}" y="0" width="1" height="100" fill="black"/>' for i in range(20))
        + '</svg>')
# An 8:1 diamond: its sharp tips are the farthest ink from the center.
DIAMOND = ('<svg xmlns="http://www.w3.org/2000/svg" width="80pt" height="10pt" '
           'viewBox="0 0 80 10"><path d="M0 5L40 0L80 5L40 10Z" fill="black"/></svg>')
PT = 25.4 / 72
INCIRCLE_40 = 20 * math.cos(math.pi / 256)


def ui_value(mm):
    """The number a size input holds after the UI writes it back (displayMm)."""
    return float(f"{mm:.3f}")


def settings(**overrides):
    base = {"width_mm": 36, "height_mm": None, "total_height_mm": 20,
            "body_shape": "auto", "thicken": "0", "fit": "margin"}
    return {**base, **overrides}


class ArtworkFitTests(unittest.TestCase):
    def test_margin_fit_is_unchanged(self):
        d = resolve_dimensions(SOLID, 50, 30, 20, "rectangular", "0")
        inset = MARGIN_MM + MOUTH_MM
        self.assertEqual(d["fit"], "margin")
        self.assertFalse(d["clip"])
        self.assertAlmostEqual(d["scale"], min((50 - 2 * inset) / (40 * PT), (30 - 2 * inset) / (20 * PT)))

    def test_bleed_meets_the_edge_on_the_tighter_axis(self):
        d = resolve_dimensions(SOLID, 50, 30, 20, "rectangular", "0", "bleed")
        self.assertTrue(d["clip"])
        self.assertAlmostEqual(d["design_width_mm"], 50)
        self.assertAlmostEqual(d["design_height_mm"], 25)

    def test_auto_bleed_body_matches_the_artwork_on_all_edges(self):
        d = resolve_dimensions(SOLID, 50, None, 20, "auto", "auto", "bleed")
        self.assertAlmostEqual(d["height_mm"], 25)
        self.assertLessEqual(d["design_width_mm"], d["width_mm"])
        self.assertLessEqual(d["design_height_mm"], d["height_mm"])
        self.assertAlmostEqual(d["design_width_mm"], 50)
        self.assertAlmostEqual(d["design_height_mm"], 25)

    def test_fill_covers_the_face_and_rejects_oversized_overflow(self):
        d = resolve_dimensions(SOLID, 30, 30, 20, "rectangular", "0", "fill")
        self.assertAlmostEqual(d["design_width_mm"], 60)
        self.assertAlmostEqual(d["design_height_mm"], 30)
        with self.assertRaisesRegex(ValueError, "Fill would enlarge"):
            resolve_dimensions(SOLID, 12, 150, 20, "rectangular", "0", "fill")

    def test_round_bleed_fits_the_ink_not_its_bounding_box(self):
        margin = resolve_dimensions(DISC, 40, None, 20, "round", "0")
        bleed = resolve_dimensions(DISC, 40, None, 20, "round", "0", "bleed")
        # Corner fitting would leave the disc near 40 / sqrt(2) mm.
        self.assertLess(margin["design_width_mm"], 27)
        self.assertLessEqual(bleed["design_width_mm"], 40 * math.cos(math.pi / 256))
        self.assertGreater(bleed["design_width_mm"], 39.8)

    def test_round_bleed_keeps_pointed_tips_inside_the_circle(self):
        d = resolve_dimensions(DIAMOND, 40, None, 20, "round", "0", "bleed")
        # The design keeps the artwork's aspect exactly; tips stay inside the body.
        self.assertAlmostEqual(d["design_width_mm"] / d["design_height_mm"], 8, places=9)
        self.assertLessEqual(d["design_width_mm"] / 2, INCIRCLE_40)
        self.assertGreater(d["design_width_mm"] / 2, INCIRCLE_40 * 0.995)
        build_print_preview(DIAMOND, d, "raised")

    def test_round_bleed_measures_from_the_ink_center_like_openscad(self):
        # OpenSCAD's import(center=true) centers the shapes, not the viewport.
        corner = ('<svg xmlns="http://www.w3.org/2000/svg" width="60pt" height="60pt" '
                  'viewBox="0 0 60 60"><circle cx="10" cy="10" r="5" fill="black"/></svg>')
        self.assertAlmostEqual(ink_radius_mm(corner), 5 * PT, delta=5 * PT * 0.02)
        with self.assertRaisesRegex(ValueError, "Full bleed would enlarge the artwork canvas"):
            resolve_dimensions(corner, 40, None, 20, "round", "0", "bleed")

    def test_bleed_at_the_size_limit_absorbs_float_excess(self):
        # 207 pt: (width in mm) * (200 / width in mm) rounds to 200.00000000000003.
        bar = ('<svg xmlns="http://www.w3.org/2000/svg" width="207pt" height="60pt" '
               'viewBox="0 0 207 60"><path d="M0 0H207V60H0Z" fill="black"/></svg>')
        for height, shape in ((None, "auto"), (60, "rectangular")):
            d = resolve_dimensions(bar, 200, height, 20, shape, "0", "bleed")
            self.assertLessEqual(d["design_width_mm"], 200)
            self.assertAlmostEqual(d["design_width_mm"], 200, places=9)
        build_print_preview(bar, d, "raised")

    def test_round_fill_spans_the_diameter_on_the_shorter_axis(self):
        d = resolve_dimensions(SOLID, 30, None, 20, "round", "0", "fill")
        self.assertAlmostEqual(d["design_height_mm"], 30)
        self.assertAlmostEqual(d["design_width_mm"], 60)

    def test_cli_passes_the_fit_to_the_model(self):
        script = Path(__file__).with_name("stamp_geometry.py")
        with tempfile.TemporaryDirectory() as directory:
            svg, scad = Path(directory) / "art.svg", Path(directory) / "art.scad"
            svg.write_text(SOLID)
            for extra, clip in (([], "false"), (["bleed"], "true")):
                subprocess.run([sys.executable, str(script), str(svg), str(scad), "50", "30", "20",
                                "rectangular", "raised", "0", *extra],
                               check=True, capture_output=True, timeout=60)
                self.assertIn(f"clip_to_body = {clip};", scad.read_text())

    def test_artwork_finer_than_a_sample_counts_as_lost(self):
        faint = ('<svg xmlns="http://www.w3.org/2000/svg" width="100pt" height="100pt" '
                 'viewBox="0 0 1000 1000"><rect x="0" y="500" width="1000" height="0.01" fill="black"/></svg>')
        d = resolve_dimensions(faint, 36, None, 20, "auto", "0", "bleed")
        self.assertEqual(nozzle_detail(faint, d, "raised", 0.4), (100.0, 0.0))
        for nozzle in build_print_preview(faint, d, "raised")["previews"]:
            self.assertEqual(nozzle["lost_percent"], 100.0)

    def test_fill_preview_counts_only_the_visible_face(self):
        d = resolve_dimensions(SOLID, 30, 30, 20, "rectangular", "0", "fill")
        preview = build_print_preview(SOLID, d, "raised")
        for nozzle in preview["previews"]:
            # The trimmed face is one solid square: nothing is lost or added.
            self.assertLess(nozzle["lost_percent"], 0.5)
            self.assertEqual(nozzle["gained_percent"], 0)

    def test_invalid_fit_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "fit must be"):
            resolve_dimensions(SOLID, 36, None, 20, "auto", "0", "edge")

    def test_raised_bleed_export_stays_within_the_body(self):
        app = '/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD'
        openscad = os.environ.get('OPENSCAD') or (app if Path(app).is_file() else shutil.which('openscad'))
        if not openscad:
            self.skipTest('OpenSCAD is required for physical export verification')
        # Auto reinforcement offsets the artwork past the edge; the clip trims it.
        d = resolve_dimensions(SOLID, 30, 30, 20, "rectangular", "auto", "fill")
        self.assertGreater(d["thicken_mm"], 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            svg, scad, model = root / 'art.svg', root / 'stamp.scad', root / 'stamp.3mf'
            svg.write_text(SOLID)
            scad.write_text(scad_source(svg, d, 'raised'))
            subprocess.run([openscad, '-o', str(model), str(scad)],
                           check=True, capture_output=True, timeout=60)
            with zipfile.ZipFile(model) as archive:
                tree = ET.fromstring(archive.read('3D/3dmodel.model'))
        ns = {'m': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
        vertices = np.array([[float(v.get(axis)) for axis in ('x', 'y', 'z')]
                             for v in tree.findall('.//m:vertex', ns)])
        np.testing.assert_allclose(vertices.max(0) - vertices.min(0), [30, 30, 20], atol=1e-6)
        # The relief covers the whole face: every top vertex lies on the body outline.
        top = vertices[np.isclose(vertices[:, 2], vertices[:, 2].max())]
        self.assertTrue(np.all(np.isclose(np.abs(top[:, :2]), 15)))


class NozzleSizingTests(unittest.TestCase):
    def test_minimum_size_passes_and_a_smaller_size_fails(self):
        result = size_for_nozzle(BARS, settings(width_mm=14, fit="bleed"), "raised", 0.4, 1)
        self.assertFalse(result["current"]["passes"])
        self.assertTrue(result["achievable"])
        minimum = result["minimum"]
        self.assertTrue(minimum["passes"])
        width = minimum["dimensions"]["width_mm"]
        self.assertEqual(width, ui_value(width))
        # Bars are 1/39 of the width and must hold a 0.42 mm line: 9 samples
        # at 0.05 mm, plus up to one sample of raster misalignment.
        self.assertGreater(width, 0.42 * 39)
        self.assertLess(width, 0.5 * 39 * 1.05)
        smaller = resolve_dimensions(BARS, width * 0.9, None, 20, "auto", "0", "bleed")
        lost, gained = nozzle_detail(BARS, smaller, "raised", 0.4)
        self.assertTrue(lost > 1 or gained > 1)

    def test_rectangular_bodies_keep_their_proportions_and_only_grow(self):
        result = size_for_nozzle(BARS, settings(width_mm=14, height_mm=28, body_shape="rectangular",
                                                fit="bleed"), "raised", 0.4, 2)
        self.assertFalse(result["current"]["passes"])
        minimum = result["minimum"]["dimensions"]
        self.assertGreater(minimum["width_mm"], 14)
        self.assertGreater(minimum["height_mm"], 28)
        self.assertAlmostEqual(minimum["height_mm"] / minimum["width_mm"], 2, delta=0.01)
        # Comparing at the values the UI writes back reproduces the reported numbers.
        applied = resolve_dimensions(BARS, ui_value(minimum["width_mm"]), ui_value(minimum["height_mm"]),
                                     20, "rectangular", "0", "bleed")
        self.assertEqual(nozzle_detail(BARS, applied, "raised", 0.4),
                         (result["minimum"]["lost_percent"], result["minimum"]["gained_percent"]))

    def test_a_failing_size_is_never_shrunk(self):
        # Only a size just below the 200 mm height limit exists above this one.
        bar = ('<svg xmlns="http://www.w3.org/2000/svg" width="19.97mm" height="200mm" viewBox="0 0 1997 20000">'
               '<rect x="979.5" y="0" width="42" height="20000" fill="black"/></svg>')
        result = size_for_nozzle(bar, settings(width_mm=19.95, fit="bleed"), "raised", 0.4, 5)
        self.assertFalse(result["current"]["passes"])
        reported = result["minimum" if result["achievable"] else "largest"]["dimensions"]
        self.assertGreaterEqual(reported["width_mm"], 19.95)

    def test_a_pass_below_a_failing_top_size_is_found(self):
        # Sub-sample hairlines join the pattern only at large sizes, so 200 mm
        # fails while mid sizes pass.
        art = ('<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" viewBox="0 0 1000 1000">'
               '<rect x="100" y="100" width="390" height="150" fill="black"/>'
               '<rect x="510" y="100" width="390" height="150" fill="black"/>'
               + ''.join(f'<rect x="100" y="{y}" width="800" height="0.5" fill="black"/>'
                         for y in range(350, 950, 60)) + '</svg>')
        result = size_for_nozzle(art, settings(width_mm=12), "raised", 0.2, 1)
        self.assertFalse(result["current"]["passes"])
        self.assertTrue(result["achievable"])
        self.assertGreater(result["minimum"]["dimensions"]["width_mm"], 12)
        self.assertTrue(result["minimum"]["passes"])

    def test_sizing_survives_when_every_smaller_size_passes(self):
        result = size_for_nozzle(DISC, settings(width_mm=20, height_mm=47, body_shape="rectangular"),
                                 "raised", 0.4, 2)
        self.assertTrue(result["current"]["passes"])
        self.assertLessEqual(result["minimum"]["dimensions"]["width_mm"], 20)
        self.assertGreaterEqual(result["minimum"]["dimensions"]["height_mm"], 12)

    def test_passing_size_is_kept_and_reports_the_minimum(self):
        result = size_for_nozzle(BARS, settings(width_mm=60, fit="bleed"), "raised", 0.2, 1)
        self.assertTrue(result["current"]["passes"])
        self.assertTrue(result["minimum"]["passes"])
        self.assertLess(result["minimum"]["dimensions"]["width_mm"], 60)

    def test_unreachable_detail_reports_the_largest_size(self):
        # Hairlines 1/1000 of the width cannot reach 0.84 mm within 200 mm.
        hair = ('<svg xmlns="http://www.w3.org/2000/svg" width="1000pt" height="200pt" '
                'viewBox="0 0 1000 200"><rect x="0" y="0" width="1000" height="1" fill="black"/>'
                '<rect x="0" y="199" width="1000" height="1" fill="black"/></svg>')
        result = size_for_nozzle(hair, settings(width_mm=36, fit="bleed"), "raised", 0.8, 5)
        self.assertFalse(result["achievable"])
        self.assertAlmostEqual(result["largest"]["dimensions"]["width_mm"], 200)

    def test_rejects_unknown_nozzles_and_allowances(self):
        with self.assertRaisesRegex(ValueError, "nozzle_mm"):
            size_for_nozzle(BARS, settings(), "raised", 0.5, 1)
        with self.assertRaisesRegex(ValueError, "tolerance_percent"):
            size_for_nozzle(BARS, settings(), "raised", 0.4, True)


if __name__ == '__main__':
    unittest.main()
