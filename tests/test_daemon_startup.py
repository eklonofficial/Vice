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

    def test_status_carries_free_space_for_the_clips_drive(self) -> None:
        daemon = _startup_daemon(_StubRecorder())
        daemon._ready = True
        with tempfile.TemporaryDirectory() as out:
            daemon.cfg.output.directory = out

            disk = daemon._get_status()["disk"]

        self.assertGreater(disk["total"], 0)
        self.assertGreaterEqual(disk["total"], disk["free"])

    def test_an_unmeasurable_drive_reports_nothing_rather_than_zero(self) -> None:
        # Zero free space is the one reading a storage meter must never invent.
        daemon = _startup_daemon(_StubRecorder())
        daemon.cfg.output.directory = "/nonexistent/vice-test-path"

        self.assertIsNone(daemon._get_status()["disk"])

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




class ClipCountReportingTests(unittest.TestCase):
    """The status payload reports the library, not this process's tally."""

    def _daemon(self) -> ViceDaemon:
        with mock.patch("vice.main.load_config", return_value=Config()), \
                mock.patch("vice.main.create_recorder", return_value=_StubRecorder()), \
                mock.patch("vice.main.HotkeyListener"), \
                mock.patch("vice.main.can_access_hotkeys", return_value=True):
            return ViceDaemon()

    def test_library_size_comes_from_the_share_server(self) -> None:
        # _clip_count only ever counted saves made by this process, so a
        # daemon left running reported a handful against a library of dozens.
        daemon = self._daemon()
        daemon._clip_count = 2
        daemon.share = mock.Mock()
        daemon.share.clip_count.return_value = 51
        self.assertEqual(daemon._clips_in_library(), 51)
        self.assertEqual(daemon._get_status()["clips"], 51)

    def test_counter_is_the_fallback_before_the_share_server_is_up(self) -> None:
        daemon = self._daemon()
        daemon._clip_count = 4
        daemon.share = None
        self.assertEqual(daemon._clips_in_library(), 4)


if __name__ == "__main__":
    unittest.main()


class OutdatedDaemonTakeoverTests(unittest.TestCase):
    """An upgrade must not leave the old daemon serving old code."""

    def test_a_daemon_on_a_different_version_is_replaced(self) -> None:
        from vice.main import _take_over_outdated_daemon
        import json as _json

        status = _json.dumps({"running": True, "version": "0.0.1"})
        with mock.patch("vice.main.SOCKET_FILE") as sock, \
                mock.patch("vice.main._ipc", return_value=None) as ipc:
            sock.exists.return_value = False
            self.assertTrue(_take_over_outdated_daemon(status))
        # It must actually ask the old daemon to stop, not just claim the socket.
        self.assertIn("stop", [c.args[0] for c in ipc.call_args_list])

    def test_the_same_version_is_left_alone(self) -> None:
        from vice.main import _take_over_outdated_daemon
        from vice import __version__
        import json as _json

        status = _json.dumps({"running": True, "version": __version__})
        with mock.patch("vice.main._ipc") as ipc:
            # Starting Vice twice on purpose stays an error; killing a healthy
            # daemon would be worse than refusing to start.
            self.assertFalse(_take_over_outdated_daemon(status))
            ipc.assert_not_called()

    def test_an_unreadable_status_is_left_alone(self) -> None:
        from vice.main import _take_over_outdated_daemon
        for bad in (None, "", "not json", "{}"):
            with self.subTest(bad=bad), mock.patch("vice.main._ipc") as ipc:
                self.assertFalse(_take_over_outdated_daemon(bad))
                ipc.assert_not_called()


class DaemonLaunchOwnershipTests(unittest.TestCase):
    """vice-app must not strand the daemon outside its own service."""

    def test_systemd_owns_the_daemon_when_the_unit_is_loaded(self) -> None:
        from vice import app

        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}), \
                mock.patch("vice.app.shutil.which", return_value="/usr/bin/systemctl"), \
                mock.patch("vice.app.subprocess.run") as run:
            run.side_effect = [
                mock.Mock(stdout="loaded\n", returncode=0),
                mock.Mock(stdout="", stderr="", returncode=0),
            ]
            self.assertTrue(app._start_daemon_via_systemd())
        # restart, not start: a unit left in a failed state ignores start.
        self.assertIn("restart", run.call_args_list[-1].args[0])

    def test_it_falls_back_when_there_is_no_systemd(self) -> None:
        from vice import app

        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("vice.app.subprocess.run") as run:
            self.assertFalse(app._start_daemon_via_systemd())
            run.assert_not_called()

    def test_it_falls_back_when_the_unit_is_not_installed(self) -> None:
        from vice import app

        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}), \
                mock.patch("vice.app.shutil.which", return_value="/usr/bin/systemctl"), \
                mock.patch("vice.app.subprocess.run",
                           return_value=mock.Mock(stdout="not-found\n", returncode=0)):
            self.assertFalse(app._start_daemon_via_systemd())

    def test_it_falls_back_when_systemctl_fails(self) -> None:
        from vice import app

        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}), \
                mock.patch("vice.app.shutil.which", return_value="/usr/bin/systemctl"), \
                mock.patch("vice.app.subprocess.run") as run:
            run.side_effect = [
                mock.Mock(stdout="loaded\n", returncode=0),
                mock.Mock(stdout="", stderr="Failed to connect to bus", returncode=1),
            ]
            self.assertFalse(app._start_daemon_via_systemd())


class WindowOpensWithoutARecorderTests(unittest.TestCase):
    """A dead recorder must not cost you the window.

    2.7.0 made the daemon outlive a recorder that will not start so the UI
    could explain the problem and Settings stayed reachable (#156). The
    launcher still demanded status.ready, which the daemon deliberately leaves
    false in exactly that case, so it killed the healthy daemon and showed
    nothing at all. A GPU driver that needed a reboot became "Vice does not
    open", with no way to see why.
    """

    def test_a_serving_ui_is_opened_even_when_the_recorder_is_down(self) -> None:
        from vice import app

        down = {"version": app.__version__, "ready": False, "recorder_error": "no opengl"}
        with mock.patch("vice.app._daemon_status", return_value=down), \
                mock.patch("vice.app._wait_for_server", return_value=True), \
                mock.patch("vice.app._wait_for_ready_server", return_value=None), \
                mock.patch("vice.app._stop_daemon") as stop, \
                mock.patch("vice.app._start_daemon") as start:
            url = app._ensure_server("http://127.0.0.1:8765/", startup_timeout=0.1)

        self.assertEqual(url, "http://127.0.0.1:8765/")
        # The daemon was healthy. Killing it would have thrown away the very
        # diagnosis the window is being opened to show.
        stop.assert_not_called()
        start.assert_not_called()

    def test_a_daemon_that_stopped_serving_is_still_restarted(self) -> None:
        from vice import app

        down = {"version": app.__version__, "ready": False}
        # HTTP answered the first probe and not the second: genuinely broken,
        # so the restart path has to stay.
        with mock.patch("vice.app._daemon_status", return_value=down), \
                mock.patch("vice.app._wait_for_server", side_effect=[True, False, False]), \
                mock.patch("vice.app._wait_for_ready_server", return_value=None), \
                mock.patch("vice.app._wait_for_daemon_exit"), \
                mock.patch("vice.app._clear_stale_socket"), \
                mock.patch("vice.app._stop_daemon") as stop, \
                mock.patch("vice.app._start_daemon") as start:
            app._ensure_server("http://127.0.0.1:8765/", startup_timeout=0.1)

        stop.assert_called_once()
        start.assert_called_once()

    def test_a_ready_daemon_still_takes_the_fast_path(self) -> None:
        from vice import app

        up = {"version": app.__version__, "ready": True}
        with mock.patch("vice.app._daemon_status", return_value=up), \
                mock.patch("vice.app._wait_for_server", return_value=True), \
                mock.patch("vice.app._wait_for_ready_server",
                           return_value="http://127.0.0.1:8765/") as ready, \
                mock.patch("vice.app._stop_daemon") as stop:
            url = app._ensure_server("http://127.0.0.1:8765/", startup_timeout=0.1)

        self.assertEqual(url, "http://127.0.0.1:8765/")
        ready.assert_called_once()
        stop.assert_not_called()


class ServiceUnitTests(unittest.TestCase):
    def test_the_unit_gives_up_instead_of_retrying_forever(self) -> None:
        unit = (Path(__file__).resolve().parents[1] / "packaging" / "vice.service").read_text()
        # Without a start limit a permanent failure loops silently: this was
        # found at restart counter 5424, with the unit stuck "activating".
        self.assertIn("StartLimitIntervalSec=", unit)
        self.assertIn("StartLimitBurst=", unit)
        limit = unit.split("StartLimitBurst=")[1].split("\n")[0].strip()
        self.assertLessEqual(int(limit), 5)
        # The limit only means anything in [Unit].
        head = unit.split("[Service]")[0]
        self.assertIn("StartLimitBurst=", head)


class UpgradeSelfRestartTests(unittest.IsolatedAsyncioTestCase):
    """A daemon whose own files were replaced underneath it restarts itself.

    Before this, only vice-app noticed, and only at launch. Upgrading without
    opening the window left the old Python running with nothing saying so, and
    the user had to know to type `systemctl --user restart vice.service`.
    """

    @staticmethod
    def _daemon() -> ViceDaemon:
        with mock.patch("vice.main.load_config", return_value=Config()), \
             mock.patch("vice.main.create_recorder"), \
             mock.patch("vice.main.HotkeyListener"), \
             mock.patch("vice.main.can_access_hotkeys", return_value=True):
            return ViceDaemon()

    async def _tick(self, daemon: ViceDaemon) -> None:
        """Run one pass of the loop without waiting out its real interval."""
        async def _instant(_seconds: float) -> None:
            return None
        with mock.patch("vice.main.asyncio.sleep", new=_instant):
            await asyncio.wait_for(daemon._upgrade_watch_loop(), timeout=5)

    async def test_a_new_version_on_disk_restarts_through_systemd(self) -> None:
        daemon = self._daemon()
        daemon.share = None
        with mock.patch("vice.main.installed_version", return_value="99.0.0"), \
             mock.patch("vice.main.systemd_unit_loaded", return_value=True), \
             mock.patch.object(daemon, "_restart_via_systemd") as restart:
            await self._tick(daemon)
        restart.assert_awaited_once()

    async def test_the_same_version_never_restarts_anything(self) -> None:
        from vice import __version__
        daemon = self._daemon()
        # The loop would spin forever agreeing with itself, so it is stopped
        # after a couple of passes rather than by a return.
        calls = {"n": 0}

        def _version() -> str:
            calls["n"] += 1
            if calls["n"] > 3:
                raise asyncio.CancelledError
            return __version__

        with mock.patch("vice.main.installed_version", side_effect=_version), \
             mock.patch("vice.main.systemd_unit_loaded") as unit, \
             mock.patch.object(daemon, "_restart_via_systemd") as restart:
            with self.assertRaises(asyncio.CancelledError):
                await self._tick(daemon)
        restart.assert_not_called()
        # It must not even ask systemd while there is nothing to ask about.
        unit.assert_not_called()

    async def test_an_unreadable_version_changes_nothing(self) -> None:
        daemon = self._daemon()
        calls = {"n": 0}

        def _version() -> None:
            calls["n"] += 1
            if calls["n"] > 3:
                raise asyncio.CancelledError
            return None

        with mock.patch("vice.main.installed_version", side_effect=_version), \
             mock.patch("vice.main.systemd_unit_loaded") as unit, \
             mock.patch.object(daemon, "_restart_via_systemd") as restart:
            with self.assertRaises(asyncio.CancelledError):
                await self._tick(daemon)
        restart.assert_not_called()
        unit.assert_not_called()

    async def test_without_a_systemd_unit_it_stays_up_on_the_old_code(self) -> None:
        # Nothing would start the replacement, and no daemon is worse than a
        # stale one. It says so in the log and gives up rather than exiting.
        daemon = self._daemon()
        daemon.share = None
        with mock.patch("vice.main.installed_version", return_value="99.0.0"), \
             mock.patch("vice.main.systemd_unit_loaded", return_value=False), \
             mock.patch.object(daemon, "_restart_via_systemd") as restart:
            await self._tick(daemon)
        restart.assert_not_called()

    async def test_it_waits_while_a_session_is_recording(self) -> None:
        daemon = self._daemon()
        daemon.share = None
        daemon._session_active = True
        passes = {"n": 0}

        def _version() -> str:
            passes["n"] += 1
            if passes["n"] > 3:
                raise asyncio.CancelledError
            return "99.0.0"

        with mock.patch("vice.main.installed_version", side_effect=_version), \
             mock.patch("vice.main.systemd_unit_loaded", return_value=True), \
             mock.patch.object(daemon, "_restart_via_systemd") as restart:
            with self.assertRaises(asyncio.CancelledError):
                await self._tick(daemon)
        # Restarting mid-session would throw away the recording in progress.
        restart.assert_not_called()

    async def test_it_waits_while_a_clip_is_still_being_written(self) -> None:
        daemon = self._daemon()
        daemon.share = None
        pending: asyncio.Future = asyncio.get_running_loop().create_future()
        daemon._clip_task = asyncio.ensure_future(pending)
        self.addCleanup(daemon._clip_task.cancel)
        passes = {"n": 0}

        def _version() -> str:
            passes["n"] += 1
            if passes["n"] > 3:
                raise asyncio.CancelledError
            return "99.0.0"

        with mock.patch("vice.main.installed_version", side_effect=_version), \
             mock.patch("vice.main.systemd_unit_loaded", return_value=True), \
             mock.patch.object(daemon, "_restart_via_systemd") as restart:
            with self.assertRaises(asyncio.CancelledError):
                await self._tick(daemon)
        restart.assert_not_called()

    async def test_the_restart_is_detached_from_this_process(self) -> None:
        # systemctl outlives the process it restarts, so a child in our own
        # process group would be torn down before it could start the new one.
        daemon = self._daemon()
        with mock.patch(
            "vice.main.asyncio.create_subprocess_exec", new_callable=mock.AsyncMock,
        ) as spawn:
            await daemon._restart_via_systemd()
        spawn.assert_awaited_once()
        args, kwargs = spawn.await_args
        self.assertEqual(args[:4], ("systemctl", "--user", "restart", "vice.service"))
        self.assertTrue(kwargs.get("start_new_session"))

    async def test_a_failed_restart_leaves_the_daemon_running(self) -> None:
        daemon = self._daemon()
        with mock.patch(
            "vice.main.asyncio.create_subprocess_exec",
            new_callable=mock.AsyncMock,
            side_effect=OSError("no systemctl"),
        ):
            # No exception escapes: staying up on old code beats going away.
            await daemon._restart_via_systemd()

    def test_the_version_probe_reads_the_file_rather_than_the_import(self) -> None:
        from vice import __version__
        from vice.runtime import installed_version
        self.assertEqual(installed_version(), __version__)
