# Copyright (c) 2026 Kevin PANCHOUT
# SPDX-License-Identifier: MIT

"""Tests for loading the per-project naming convention file (no Wwise needed)."""

import tempfile
import unittest
from pathlib import Path

import naming_check

FILLED_CONVENTION = """\
project = ADN
types = SFX, AMB, MUS
music_type = MUS
"""


class LoadConventionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.script_dir = Path(temp.name) / "script"
        self.script_dir.mkdir()
        self.file = self.script_dir / "naming_convention.ini"

    def test_first_run_creates_template_and_reports_its_path(self):
        with self.assertRaises(naming_check.ConventionError) as raised:
            naming_check.load_convention(self.script_dir)

        self.assertTrue(self.file.is_file())
        self.assertIn(str(self.file), str(raised.exception))

    def test_template_is_rejected_until_filled_naming_the_setting(self):
        with self.assertRaises(naming_check.ConventionError):
            naming_check.load_convention(self.script_dir)

        with self.assertRaises(naming_check.ConventionError) as raised:
            naming_check.load_convention(self.script_dir)

        self.assertIn("project", str(raised.exception))

    def test_empty_value_is_rejected_naming_the_setting(self):
        self.file.write_text("project = ADN\ntypes =\n", encoding="utf-8")

        with self.assertRaises(naming_check.ConventionError) as raised:
            naming_check.load_convention(self.script_dir)

        self.assertIn("types", str(raised.exception))

    def test_missing_setting_is_rejected_naming_it(self):
        self.file.write_text("project = ADN\n", encoding="utf-8")

        with self.assertRaises(naming_check.ConventionError) as raised:
            naming_check.load_convention(self.script_dir)

        self.assertIn("types", str(raised.exception))

    def test_filled_file_loads(self):
        self.file.write_text(FILLED_CONVENTION, encoding="utf-8")

        convention = naming_check.load_convention(self.script_dir)

        self.assertEqual(convention.project, "ADN")
        self.assertEqual(convention.types, ("SFX", "AMB", "MUS"))
        self.assertEqual(convention.music_type, "MUS")

    def test_music_type_must_be_one_of_the_types(self):
        self.file.write_text(
            "project = ADN\ntypes = SFX, AMB\nmusic_type = MUS\n", encoding="utf-8"
        )

        with self.assertRaises(naming_check.ConventionError) as raised:
            naming_check.load_convention(self.script_dir)

        self.assertIn("music_type", str(raised.exception))

    def _assert_rejected(self, text, setting):
        self.file.write_text(text, encoding="utf-8")

        with self.assertRaises(naming_check.ConventionError) as raised:
            naming_check.load_convention(self.script_dir)

        self.assertIn(setting, str(raised.exception))
        self.assertIn(str(self.file), str(raised.exception))

    def test_line_without_equal_sign_is_rejected(self):
        self._assert_rejected(
            "project = ADN\ntypes SFX\nmusic_type = SFX\n", "naming_convention.ini"
        )

    def test_duplicated_setting_is_rejected(self):
        self._assert_rejected(
            "project = ADN\nproject = XYZ\ntypes = SFX\nmusic_type = SFX\n", "project"
        )

    def test_percent_sign_is_rejected_as_invalid_project(self):
        self._assert_rejected(
            "project = A%B\ntypes = SFX\nmusic_type = SFX\n", "project"
        )

    def test_project_with_underscore_is_rejected(self):
        self._assert_rejected(
            "project = AD_N\ntypes = SFX\nmusic_type = SFX\n", "project"
        )

    def test_empty_type_in_list_is_rejected(self):
        self._assert_rejected(
            "project = ADN\ntypes = SFX,, AMB\nmusic_type = SFX\n", "types"
        )

    def test_trailing_comma_in_types_is_rejected(self):
        self._assert_rejected(
            "project = ADN\ntypes = SFX, AMB,\nmusic_type = SFX\n", "types"
        )

    def test_duplicated_type_is_rejected(self):
        self._assert_rejected(
            "project = ADN\ntypes = SFX, SFX\nmusic_type = SFX\n", "types"
        )

    def test_type_with_space_is_rejected(self):
        self._assert_rejected(
            "project = ADN\ntypes = S FX, AMB\nmusic_type = AMB\n", "types"
        )

    def test_template_asks_for_music_type(self):
        with self.assertRaises(naming_check.ConventionError):
            naming_check.load_convention(self.script_dir)

        self.assertIn("music_type =", self.file.read_text(encoding="utf-8"))


class ConventionTest(unittest.TestCase):
    def test_music_type_outside_types_is_rejected(self):
        with self.assertRaises(ValueError) as raised:
            naming_check.Convention("ADN", ("SFX", "AMB"), "MUS")

        self.assertIn("music_type", str(raised.exception))

    def test_project_with_underscore_is_rejected(self):
        with self.assertRaises(ValueError) as raised:
            naming_check.Convention("AD_N", ("SFX",), "SFX")

        self.assertIn("project", str(raised.exception))

    def test_empty_types_are_rejected(self):
        with self.assertRaises(ValueError) as raised:
            naming_check.Convention("ADN", (), "SFX")

        self.assertIn("types", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
