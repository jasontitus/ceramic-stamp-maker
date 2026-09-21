"""Physical stroke/topology contracts and SVG-to-OpenSCAD interoperability."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

import cv2
import numpy as np

from print_preview import _rasterize
from stamp_geometry import RELIEF_MM, resolve_dimensions, scad_source
from stroke_preservation import preserve_strokes


# Point units deliberately differ from viewBox units: nested SVG wrappers can
# look correct in browsers while OpenSCAD drops or incorrectly scales the art.
ARTWORK = '''<svg xmlns="http://www.w3.org/2000/svg" width="40pt" height="10pt" viewBox="0 0 40 10">
<path d="M1 1.95H15V2.05H1Z M1 6.5H15V7.5H1Z
M19.95 2H20.05V8H19.95Z M20.17 2H20.27V8H20.17Z
M32.05 5A2.05 2.05 0 1 0 27.95 5A2.05 2.05 0 1 0 32.05 5Z
M31.95 5A1.95 1.95 0 1 1 28.05 5A1.95 1.95 0 1 1 31.95 5Z" fill="black"/>
</svg>'''


class StrokePreservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dimensions = resolve_dimensions(ARTWORK, 43.4, 13.4, 20, 'rectangular', '0')
        cls.preserved, cls.report = preserve_strokes(ARTWORK, cls.dimensions, 0.46)

    def raster(self, svg):
        d = self.dimensions
        return _rasterize(svg, d['width_mm'], d['height_mm'],
                          d['design_width_mm'], d['design_height_mm'], d['body_shape'])[0]

    def region(self, mask, x0, y0, x1, y1):
        # The resolver fits this fixture at 1 mm per viewBox unit, margin 1.7 mm.
        return mask[round((y0 + 1.7) / .05):round((y1 + 1.7) / .05),
                    round((x0 + 1.7) / .05):round((x1 + 1.7) / .05)]

    def test_widening_retains_original_ink_wide_strokes_and_gaps(self):
        before, after = self.raster(ARTWORK), self.raster(self.preserved)
        self.assertFalse(np.any((before != 0) & (after == 0)))
        self.assertGreater(np.count_nonzero(self.region(after, 2, 1, 14, 3)),
                           2 * np.count_nonzero(self.region(before, 2, 1, 14, 3)))
        np.testing.assert_array_equal(self.region(before, 2, 6, 14, 8),
                                      self.region(after, 2, 6, 14, 8))
        self.assertFalse(np.any(self.region(after, 29.5, 4.5, 30.5, 5.5)))
        self.assertEqual(cv2.connectedComponents((before != 0).astype(np.uint8))[0],
                         cv2.connectedComponents((after != 0).astype(np.uint8))[0])
        self.assertGreater(self.report['conflict_percent'], 0)

    def test_export_contains_relief_at_the_preview_scale(self):
        app = '/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD'
        openscad = os.environ.get('OPENSCAD') or (app if Path(app).is_file() else shutil.which('openscad'))
        if not openscad:
            self.skipTest('OpenSCAD is required for physical export verification')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            svg, scad, model = root / 'art.svg', root / 'stamp.scad', root / 'stamp.3mf'
            svg.write_text(self.preserved)
            scad.write_text(scad_source(svg, self.dimensions, 'raised'))
            subprocess.run([openscad, '-o', str(model), str(scad)],
                           check=True, capture_output=True, timeout=60)
            with zipfile.ZipFile(model) as archive:
                tree = ET.fromstring(archive.read('3D/3dmodel.model'))
            ns = {'m': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
            vertices = np.array([[float(v.get(axis)) for axis in ('x', 'y', 'z')]
                                 for v in tree.findall('.//m:vertex', ns)])
            faces = np.array([[int(t.get(axis)) for axis in ('v1', 'v2', 'v3')]
                              for t in tree.findall('.//m:triangle', ns)])
        np.testing.assert_allclose(vertices.max(0) - vertices.min(0), [43.4, 13.4, 20], atol=1e-6)
        triangles = vertices[faces]
        volume = abs(np.einsum('ij,ij->i', triangles[:, 0],
                              np.cross(triangles[:, 1], triangles[:, 2])).sum()) / 6
        relief_volume = volume - 43.4 * 13.4 * (20 - RELIEF_MM)
        expected = np.count_nonzero(self.raster(self.preserved)) * .05 ** 2 * RELIEF_MM
        self.assertAlmostEqual(relief_volume, expected, delta=expected * .04)

    def test_width_and_height_follow_the_artwork_axes(self):
        """Width mm must be the artwork's X extent, height its Y extent.

        A portrait artwork must yield a taller-than-wide body: the preview
        raster and the exported model share the SVG's axes, so treating width
        as the long side (or swapping the pair for tall art) silently rotates
        the printed stamp 90 degrees relative to what the user sees.
        """
        portrait = ('<svg xmlns="http://www.w3.org/2000/svg" width="30pt" height="45pt" '
                    'viewBox="0 0 30 45"><path d="M0 0H30V45H0Z" fill="black"/></svg>')
        dimensions = resolve_dimensions(portrait, 36, None, 22, 'auto', '0')
        svg_width, svg_height = 30, 45
        inset = 1.5 + 0.2 + dimensions['thicken_mm']
        expected_scale = min((36 - 2 * inset) / svg_width, (dimensions['height_mm'] - 2 * inset) / svg_height)
        self.assertAlmostEqual(dimensions['design_width_mm'], svg_width * expected_scale)
        self.assertAlmostEqual(dimensions['design_height_mm'], svg_height * expected_scale)
        self.assertAlmostEqual(dimensions['design_width_mm'] / dimensions['design_height_mm'],
                               svg_width / svg_height, places=9)
        # 36 mm is the narrow side here: the body must be taller than it is wide.
        self.assertGreater(dimensions['height_mm'], dimensions['width_mm'])
        self.assertAlmostEqual(dimensions['width_mm'], 36)

        # Requesting the same artwork as an explicit rectangle keeps the axes:
        # a square request is square, and 36 wide x 60 tall stays 36 x 60.
        rectangle = resolve_dimensions(portrait, 36, 60, 22, 'rectangular', '0')
        self.assertAlmostEqual(rectangle['width_mm'], 36)
        self.assertAlmostEqual(rectangle['height_mm'], 60)
        self.assertLess(rectangle['design_width_mm'], rectangle['design_height_mm'])


if __name__ == '__main__':
    unittest.main()
