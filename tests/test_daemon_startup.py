"""Daemon startup resilience and clip game tagging.

Two behaviours that used to fail quietly:

  * A recorder that would not start took the share server down with it, so
    the app said "the UI server did not respond" and there was no way to
    reach Settings and pick an encoder that works (#156).
  * Clip tagging only ever asked for the focused window, which comes back
    empty on KDE and GNOME under Wayland, so clips were never tagged and
    never landed in an auto playlist (#152).
  * A clip directory on a drive that had been unmounted killed the daemon
    before the log existed, so the reporter's vice.log stopped mid-startup
    with nothing after it (#142).
"""

import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vice.config import Config, DiscordConfig, OutputConfig
from vice.main import ViceDaemon


class _StubRecorder:
    """Enough recorder for the startup path. Fails on start when told to."""

    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.name = "gpu-screen-recorder"
        self.started = 0
        self.stopped = 0
        self.cpu_fallback = False

    async def start(self) -> None:
        self.started += 1
        if self.error:
            raise RuntimeError(self.error)

    async def stop(self) -> None:
        self.stopped += 1

    def is_healthy(self) -> bool:
        return self.error is None


def _startup_daemon(recorder: _StubRecorder, *, share=None) -> ViceDaemon:
    daemon = ViceDaemon.__new__(ViceDaemon)
    daemon.cfg = Config()
    daemon.recorder = recorder
    daemon.share = share
    daemon._ready = False
    daemon._recorder_error = ""
    daemon._session_active = False
    daemon.hotkeys_available = True
    daemon._clip_count = 0
    daemon._update = None
    return daemon


class RecorderStartupFailureTests(unittest.IsolatedAsyncioTestCase):
    def test_status_reports_the_failure_instead_of_claiming_to_record(self) -> None:
        daemon = _startup_daemon(_StubRecorder())
        daemon._ready = False
        daemon._recorder_error = "gsr error: Could not open video codec"

        status = daemon._get_status()

        # "recording" was hardcoded True, so a dead recorder still showed a
        # live chip in the UI.
        self.assertFalse(status["recording"])
        self.assertFalse(status["ready"])
        self.assertIn("Could not open video codec", status["recorder_error"])

    def test_status_is_clean_once_the_recorder_is_up(self) -> None:
        daemon = _startup_daemon(_StubRecorder())
        daemon._ready = True

        status = daemon._get_status()

        self.assertTrue(status["recording"])
        self.assertEqual(status["recorder_error"], "")
        self.assertFalse(status["cpu_fallback"])

    def test_status_surfaces_cpu_fallback(self) -> None:
        recorder = _StubRecorder()
        recorder.cpu_fallback = True
        daemon = _startup_daemon(recorder)
        daemon._ready = True

        self.assertTrue(daemon._get_status()["cpu_fallback"])

    async def test_watchdog_recovery_clears_the_error(self) -> None:
        recorder = _StubRecorder(error="gsr error: Could not open video codec")
        daemon = _startup_daemon(recorder)
        daemon._recorder_error = recorder.error
        daemon._config_apply_lock = asyncio.Lock()
        daemon._clip_lock = asyncio.Lock()

        # The driver comes back; the watchdog's restart now succeeds.
        recorder.error = None
        async with daemon._config_apply_lock:
            async with daemon._clip_lock:
                await daemon.recorder.stop()
                await daemon.recorder.start()
        daemon._ready = True
        daemon._recorder_error = ""

        status = daemon._get_status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["recorder_error"], "")


class OutputDirectoryFailureTests(unittest.TestCase):
    """An unusable clip directory has to reach the user, not the void (#142)."""

    def test_writable_directory_reports_no_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ViceDaemon._output_dir_problem(Path(tmp) / "clips"), "")

    def test_unwritable_directory_names_itself_and_the_config(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root writes anywhere")
        with tempfile.TemporaryDirectory() as tmp:
            locked = Path(tmp) / "locked"
            locked.mkdir()
            os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)
            try:
                problem = ViceDaemon._output_dir_problem(locked / "clips")
            finally:
                os.chmod(locked, stat.S_IRWXU)

        self.assertIn(str(locked), problem)
        self.assertIn("output.directory", problem)

    def test_status_carries_the_directory_problem(self) -> None:
        daemon = _startup_daemon(_StubRecorder())
        daemon._ready = False
        daemon._recorder_error = ViceDaemon._output_dir_problem(
            Path("/proc/vice-cannot-exist/clips")
        )

        status = daemon._get_status()

        self.assertFalse(status["recording"])
        self.assertIn("not writable", status["recorder_error"])


class ClipGameTagFallbackTests(unittest.TestCase):
    def _daemon(self) -> ViceDaemon:
        daemon = ViceDaemon.__new__(ViceDaemon)
        daemon.cfg = Config(
            output=OutputConfig(tag_clips_with_game=True),
            discord=DiscordConfig(),
        )
        daemon._last_clip_game = None
        return daemon

    def test_focused_window_still_wins(self) -> None:
        daemon = self._daemon()
        with mock.patch(
            "vice.active_window.get_active_window",
            return_value={"process": "cs2", "class": "cs2", "pid": 1},
        ):
            with mock.patch.object(daemon, "_scan_visible_for_game") as scan:
                self.assertEqual(daemon._clip_game_tag(), "Counter-Strike 2")

        # No reason to scan every window when focus already answered.
        scan.assert_not_called()

    def test_scans_visible_windows_when_focus_is_empty(self) -> None:
        daemon = self._daemon()
        with mock.patch("vice.active_window.get_active_window", return_value=None):
            with mock.patch(
                "vice.active_window.list_candidate_windows",
                return_value=[
                    {"process": "steam", "class": "steam", "pid": 1},
                    {"process": "overwatch.exe", "class": "overwatch.exe", "pid": 2},
                ],
            ):
                self.assertEqual(daemon._clip_game_tag(), "Overwatch 2")

    def test_scans_when_the_focused_window_is_not_a_game(self) -> None:
        # Alt-tabbing to Discord to clip must still tag the game behind it.
        daemon = self._daemon()
        with mock.patch(
            "vice.active_window.get_active_window",
            return_value={"process": "Discord", "class": "discord", "pid": 1},
        ):
            with mock.patch(
                "vice.active_window.list_candidate_windows",
                return_value=[{"process": "cs2", "class": "cs2", "pid": 2}],
            ):
                self.assertEqual(daemon._clip_game_tag(), "Counter-Strike 2")

    def test_no_game_anywhere_tags_nothing(self) -> None:
        daemon = self._daemon()
        with mock.patch("vice.active_window.get_active_window", return_value=None):
            with mock.patch("vice.active_window.list_candidate_windows", return_value=[]):
                self.assertIsNone(daemon._clip_game_tag())

    def test_detection_runs_even_with_tagging_off(self) -> None:
        # Auto playlists depend on it, so the lookup must not be skipped.
        daemon = self._daemon()
        daemon.cfg.output.tag_clips_with_game = False
        with mock.patch("vice.active_window.get_active_window", return_value=None):
            with mock.patch(
                "vice.active_window.list_candidate_windows",
                return_value=[{"process": "cs2", "class": "cs2", "pid": 2}],
            ):
                self.assertIsNone(daemon._clip_game_tag())

        self.assertEqual(daemon._last_clip_game, "Counter-Strike 2")


if __name__ == "__main__":
    unittest.main()
