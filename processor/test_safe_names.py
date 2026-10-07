"""Client-supplied names must not escape a directory or inject headers."""

import unittest

from stamp_tool import safe_filename


class SafeFilenameTests(unittest.TestCase):
    def test_ordinary_names_are_kept(self):
        self.assertEqual(safe_filename("IMG-3570-extract-1", "x"), "IMG-3570-extract-1")
        self.assertEqual(safe_filename("my stamp_v2.svg", "x"), "my stamp_v2.svg")

    def test_paths_cannot_escape(self):
        for name in ("../../etc/evil", "/tmp/evil", "..\\evil", "..", "./."):
            cleaned = safe_filename(name, "fallback")
            self.assertNotIn("/", cleaned)
            self.assertNotIn("\\", cleaned)
            self.assertFalse(cleaned.startswith("."))

    def test_header_breaking_characters_are_removed(self):
        cleaned = safe_filename('a"\r\nSet-Cookie: x=1', "fallback")
        self.assertTrue(cleaned.isascii())
        for char in '"\r\n:':
            self.assertNotIn(char, cleaned)
        self.assertTrue(safe_filename("名前", "fallback").isascii())

    def test_unusable_names_fall_back(self):
        for name in (None, 42, ["a"], "", "...", "   "):
            self.assertEqual(safe_filename(name, "fallback"), "fallback")


if __name__ == "__main__":
    unittest.main()
