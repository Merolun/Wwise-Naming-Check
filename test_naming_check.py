# Copyright (c) 2026 Kevin PANCHOUT
# SPDX-License-Identifier: MIT

"""Integration tests for naming_check against a real headless Wwise WAAPI server."""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from waapi import CannotConnectToWaapiException, WaapiClient

import naming_check

WWISE_CONSOLE = Path(
    os.environ.get(
        "WWISE_CONSOLE",
        r"C:\Program Files (x86)\Audiokinetic\Wwise2024.1.4.8780"
        r"\Authoring\x64\Release\bin\WwiseConsole.exe",
    )
)
# Dedicated ports so a Wwise Authoring already open on 8080/8090 is not disturbed.
TEST_WAMP_PORT = 8095
TEST_HTTP_PORT = 8096
TEST_URL = f"ws://127.0.0.1:{TEST_WAMP_PORT}/waapi"
SERVER_START_TIMEOUT_S = 60
CONNECT_RETRY_INTERVAL_S = 0.5
# Port 1 is never a WAAPI server: the connection is refused immediately.
UNREACHABLE_URL = "ws://127.0.0.1:1/waapi"
NO_PROJECT_WAMP_PORT = 8097
NO_PROJECT_HTTP_PORT = 8098
NO_PROJECT_URL = f"ws://127.0.0.1:{NO_PROJECT_WAMP_PORT}/waapi"
DEFAULT_WU = "\\Actor-Mixer Hierarchy\\Default Work Unit"
MUSIC_WU = "\\Interactive Music Hierarchy\\Default Work Unit"
CONVENTION = naming_check.Convention(
    project="ADN", types=("SFX", "AMB", "MUS"), music_type="MUS"
)
FILLED_CONVENTION_FILE = "project = ADN\ntypes = SFX, AMB, MUS\nmusic_type = MUS\n"

_server = None
_temp_dir = None
_project_root = Path()


def setUpModule():
    global _server, _temp_dir, _project_root
    if not WWISE_CONSOLE.is_file():
        raise unittest.SkipTest(f"WwiseConsole not found: {WWISE_CONSOLE}")
    _temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    _project_root = Path(_temp_dir.name) / "NamingTest"
    project = _project_root / "NamingTest.wproj"
    subprocess.run(
        [WWISE_CONSOLE, "create-new-project", project, "--quiet"], check=True
    )
    _server = subprocess.Popen(
        [
            WWISE_CONSOLE,
            "waapi-server",
            project,
            "--wamp-port",
            str(TEST_WAMP_PORT),
            "--http-port",
            str(TEST_HTTP_PORT),
            "--no-source-control",
            "--quiet",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def tearDownModule():
    if _server is not None:
        _server.terminate()
        _server.wait()
    if _temp_dir is not None:
        _temp_dir.cleanup()


def _connect(url=TEST_URL):
    deadline = time.monotonic() + SERVER_START_TIMEOUT_S
    while True:
        try:
            return WaapiClient(url, allow_exception=True)
        except CannotConnectToWaapiException:
            if time.monotonic() > deadline:
                raise
            time.sleep(CONNECT_RETRY_INTERVAL_S)


class WwiseTestCase(unittest.TestCase):
    def setUp(self):
        self.client = _connect()
        self.addCleanup(lambda: self.client.disconnect())
        self.addCleanup(self._delete_all, "Sound")
        self.addCleanup(self._delete_all, "MusicSegment")

    def _music_track(self, name):
        segment = self.client.call(
            "ak.wwise.core.object.create",
            {"parent": MUSIC_WU, "type": "MusicSegment", "name": f"Segment {name}"},
        )["id"]
        self.client.call(
            "ak.wwise.core.object.create",
            {"parent": segment, "type": "MusicTrack", "name": name},
        )

    def _sound(self, name, **props):
        args = {"parent": DEFAULT_WU, "type": "Sound", "name": name}
        args.update({f"@{key}": value for key, value in props.items()})
        return self.client.call("ak.wwise.core.object.create", args)["id"]

    def _sound_names(self):
        sounds = self.client.call(
            "ak.wwise.core.object.get",
            {"waql": "$ from type Sound"},
            options={"return": ["name"]},
        )["return"]
        return {sound["name"] for sound in sounds}

    def _delete_all(self, object_type):
        objects = self.client.call(
            "ak.wwise.core.object.get",
            {"waql": f"$ from type {object_type}"},
            options={"return": ["id"]},
        )["return"]
        for obj in objects:
            self.client.call("ak.wwise.core.object.delete", {"object": obj["id"]})

    def _with_test_client_disconnected(self, action):
        # waapi-client cannot run two clients at once in one process.
        self.client.disconnect()
        try:
            return action()
        finally:
            self.client = _connect()


class RunTest(WwiseTestCase):
    def test_audit_lists_non_compliant_sfx_sounds_with_their_reason(self):
        self._sound("ADN_SFX_Laser_PassBy_04")
        self._sound("ADN_SFX_RatsScream_Death_02_Mono")
        self._sound("my_sound")
        self._sound("ADN_FOO_Laser_PassBy")
        self._sound("ADN_SFX_laser_PassBy")
        self._sound("ADN_SFX_Laser")
        self._sound("my_voice_line", IsVoice=True)

        summary = naming_check.run(self.client, CONVENTION)

        self.assertEqual(summary.analyzed, 6)
        self.assertEqual(summary.compliant, 3)
        self.assertEqual(
            {issue.name: issue.reason for issue in summary.issues},
            {
                "my_sound": "does not start with project code 'ADN'",
                "ADN_FOO_Laser_PassBy": "missing or unknown type (allowed: SFX, AMB, MUS)",
                "ADN_SFX_laser_PassBy": "category missing or not CamelCase",
            },
        )

    def test_audit_plans_certain_fixes_without_renaming(self):
        names = [
            "SFX_Laser_PassBy_01",
            "adn_sfx_Laser_PassBy_02",
            "ADN SFX-Laser_PassBy 03",
            "ADN_SFX_laser_passBy_04",
            "sfx-laser_passBy 05",
            "my_sound",
            "ADN_SFX_Rats Scream_Death_02",
            "ADN__SFX_laser_PassBy_06",
            "_SFX_Laser_PassBy_07",
            "ADN_SFX_laser_PassBy_",
        ]
        for name in names:
            self._sound(name)

        summary = naming_check.run(self.client, CONVENTION)

        self.assertEqual(
            {issue.name: issue.fix for issue in summary.issues},
            {
                "SFX_Laser_PassBy_01": "ADN_SFX_Laser_PassBy_01",
                "adn_sfx_Laser_PassBy_02": "ADN_SFX_Laser_PassBy_02",
                "ADN SFX-Laser_PassBy 03": "ADN_SFX_Laser_PassBy_03",
                "ADN_SFX_laser_passBy_04": "ADN_SFX_Laser_passBy_04",
                "sfx-laser_passBy 05": "ADN_SFX_Laser_passBy_05",
                "my_sound": None,
                "ADN_SFX_Rats Scream_Death_02": None,
                "ADN__SFX_laser_PassBy_06": None,
                "_SFX_Laser_PassBy_07": None,
                "ADN_SFX_laser_PassBy_": None,
            },
        )
        self.assertEqual(self._sound_names(), set(names))

    def test_apply_renames_fixable_names_and_reports_conflicts(self):
        self._sound("SFX_Laser_PassBy_01")
        self._sound("SFX_Laser_PassBy_02")
        self._sound("ADN_SFX_Laser_PassBy_02")
        self._sound("adn_sfx_Laser_PassBy_03")
        self._sound("my_sound")

        summary = naming_check.run(self.client, CONVENTION, apply=True)

        self.assertEqual(summary.renamed, 2)
        self.assertEqual(
            [(f.issue.name, f.issue.fix) for f in summary.failures],
            [("SFX_Laser_PassBy_02", "ADN_SFX_Laser_PassBy_02")],
        )
        self.assertIn("already contains", summary.failures[0].reason)
        self.assertEqual(summary.remaining, 2)
        self.assertEqual(
            self._sound_names(),
            {
                "ADN_SFX_Laser_PassBy_01",
                "SFX_Laser_PassBy_02",
                "ADN_SFX_Laser_PassBy_02",
                "ADN_SFX_Laser_PassBy_03",
                "my_sound",
            },
        )

    def test_one_undo_reverts_every_fix(self):
        self._sound("SFX_Laser_PassBy_01")
        self._sound("ADN_SFX_laser_passBy_02")

        naming_check.run(self.client, CONVENTION, apply=True)
        self.client.call("ak.wwise.core.undo.undo")

        self.assertEqual(
            self._sound_names(), {"SFX_Laser_PassBy_01", "ADN_SFX_laser_passBy_02"}
        )

    def test_second_apply_changes_nothing(self):
        self._sound("SFX_Laser_PassBy_01")
        self._sound("my_sound")
        naming_check.run(self.client, CONVENTION, apply=True)

        summary = naming_check.run(self.client, CONVENTION, apply=True)

        self.assertEqual(summary.renamed, 0)
        self.assertEqual(summary.remaining, 1)

    def test_client_without_allow_exception_still_reports_rejected_fixes(self):
        self._sound("SFX_Laser_PassBy_02")
        self._sound("ADN_SFX_Laser_PassBy_02")

        def run_with_default_client():
            client = WaapiClient(TEST_URL)
            try:
                return naming_check.run(client, CONVENTION, apply=True)
            finally:
                client.disconnect()

        summary = self._with_test_client_disconnected(run_with_default_client)

        self.assertEqual(summary.renamed, 0)
        self.assertEqual(len(summary.failures), 1)

    def test_apply_never_saves_the_project(self):
        work_unit = _project_root / "Actor-Mixer Hierarchy" / "Default Work Unit.wwu"
        before = work_unit.read_bytes()
        self._sound("SFX_Laser_PassBy_01")

        naming_check.run(self.client, CONVENTION, apply=True)

        self.assertEqual(self._sound_names(), {"ADN_SFX_Laser_PassBy_01"})
        self.assertEqual(work_unit.read_bytes(), before)

    def test_music_tracks_are_audited_and_get_the_music_prefix(self):
        self._music_track("Battle_Intro_01")
        self._music_track("ADN_MUS_Battle_Intro_02")
        self._sound("Battle_Intro_01")
        self._sound("ADN_MUS_Battle_Intro_03")

        summary = naming_check.run(self.client, CONVENTION)

        self.assertEqual(summary.analyzed, 4)
        self.assertEqual(
            {(issue.object_type, issue.name, issue.fix) for issue in summary.issues},
            {
                ("MusicTrack", "Battle_Intro_01", "ADN_MUS_Battle_Intro_01"),
                ("Sound", "Battle_Intro_01", None),
            },
        )


class MainTest(WwiseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(shutil.rmtree, _project_root / "script", True)

    def _write_convention(self, text):
        script_dir = _project_root / "script"
        script_dir.mkdir(exist_ok=True)
        (script_dir / naming_check.CONVENTION_FILE_NAME).write_text(
            text, encoding="utf-8"
        )

    def _main(self, argv):
        return self._with_test_client_disconnected(lambda: naming_check.main(argv))

    def test_exits_zero_when_every_name_is_compliant(self):
        self._write_convention(FILLED_CONVENTION_FILE)
        self._sound("ADN_SFX_Laser_PassBy_04")

        self.assertEqual(self._main(["--url", TEST_URL]), 0)

    def test_exits_one_when_a_name_is_not_compliant(self):
        self._write_convention(FILLED_CONVENTION_FILE)
        self._sound("my_sound")

        self.assertEqual(self._main(["--url", TEST_URL]), 1)

    def test_apply_exits_zero_when_every_name_gets_fixed(self):
        self._write_convention(FILLED_CONVENTION_FILE)
        self._sound("SFX_Laser_PassBy_01")

        self.assertEqual(self._main(["--url", TEST_URL, "--apply"]), 0)

    def test_apply_exits_one_when_a_name_needs_a_manual_fix(self):
        self._write_convention(FILLED_CONVENTION_FILE)
        self._sound("SFX_Laser_PassBy_01")
        self._sound("my_sound")

        self.assertEqual(self._main(["--url", TEST_URL, "--apply"]), 1)

    def test_first_run_creates_convention_in_project_script_folder(self):
        exit_code = self._main(["--url", TEST_URL])

        self.assertEqual(exit_code, 2)
        convention_file = _project_root / "script" / naming_check.CONVENTION_FILE_NAME
        self.assertTrue(convention_file.is_file())


class NoProjectTest(unittest.TestCase):
    """Wwise open without a project: requests about the project are rejected."""

    @classmethod
    def setUpClass(cls):
        cls.server = subprocess.Popen(
            [
                WWISE_CONSOLE,
                "waapi-server",
                "--wamp-port",
                str(NO_PROJECT_WAMP_PORT),
                "--http-port",
                str(NO_PROJECT_HTTP_PORT),
                "--quiet",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _connect(NO_PROJECT_URL).disconnect()

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait()

    def test_rejected_request_exits_with_error_and_wwise_message(self):
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = naming_check.main(["--url", NO_PROJECT_URL])

        self.assertEqual(exit_code, 2)
        self.assertIn("No project is loaded.", stderr.getvalue())
        self.assertNotIn("ApplicationError", stderr.getvalue())


class CliTest(unittest.TestCase):
    def test_unreachable_wwise_exits_with_error(self):
        exit_code = naming_check.main(["--url", UNREACHABLE_URL])

        self.assertEqual(exit_code, 2)

    def test_script_run_from_command_line_reports_and_exits(self):
        script = Path(naming_check.__file__)

        result = subprocess.run(
            [sys.executable, script, "--url", UNREACHABLE_URL],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot connect to Wwise", result.stderr)


if __name__ == "__main__":
    unittest.main()
