import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from vice import app, main
from vice.runtime import claim_daemon_lock, daemon_is_running


class DaemonOwnershipTests(unittest.TestCase):
    def test_lock_excludes_another_launch_until_the_owner_closes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vice.sock"
            with claim_daemon_lock(path):
                with self.assertRaises(RuntimeError):
                    claim_daemon_lock(path)
            with claim_daemon_lock(path):
                pass

    def test_dead_socket_can_be_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vice.sock"
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(path))
            with mock.patch.object(app, "SOCKET_FILE", path), \
                 mock.patch.object(app, "PID_FILE", Path(tmp) / "vice.pid"), \
                 mock.patch.object(app, "_daemon_responds", return_value=False):
                app._clear_stale_socket()
            self.assertFalse(path.exists())

    def test_launcher_cleanup_respects_a_daemon_that_has_only_claimed_its_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vice.sock"
            path.touch()
            with claim_daemon_lock(path), \
                 mock.patch.object(app, "SOCKET_FILE", path), \
                 mock.patch.object(app, "PID_FILE", Path(tmp) / "vice.pid"), \
                 mock.patch.object(app, "daemon_is_running", return_value=False), \
                 mock.patch.object(app, "_daemon_responds", return_value=False):
                with self.assertRaises(RuntimeError):
                    app._clear_stale_socket()
                self.assertTrue(path.exists())

    def test_invalid_pid_never_signals_a_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vice.sock"
            pid = Path(tmp) / "vice.pid"
            for value in ("-1", "0", "not a pid"):
                pid.write_text(value)
                with self.subTest(value=value), mock.patch("vice.runtime.os.kill") as kill:
                    self.assertFalse(daemon_is_running(path, pid))
                    kill.assert_not_called()

    def test_a_busy_listener_keeps_its_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vice.sock"
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(path))
                listener.listen()
                with mock.patch.object(app, "SOCKET_FILE", path), \
                     mock.patch.object(app, "PID_FILE", Path(tmp) / "vice.pid"), \
                     mock.patch.object(app, "_daemon_responds", return_value=False):
                    try:
                        app._clear_stale_socket()
                    except RuntimeError:
                        pass
                self.assertTrue(path.exists(), "an unanswered status request unlinked a live socket")

    def test_a_live_pid_prevents_socket_cleanup_during_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vice.sock"
            pid = Path(tmp) / "vice.pid"
            pid.write_text(str(os.getpid()))
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(path))
            with mock.patch.object(app, "SOCKET_FILE", path), \
                 mock.patch.object(app, "PID_FILE", pid), \
                 mock.patch.object(app, "_daemon_responds", return_value=False):
                try:
                    app._clear_stale_socket()
                except RuntimeError:
                    pass
            self.assertTrue(path.exists())

    def test_shutdown_timeout_never_kills_or_deletes_live_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid = Path(tmp) / "vice.pid"
            path = Path(tmp) / "vice.sock"
            pid.write_text(str(os.getpid()))
            path.touch()
            with mock.patch.object(app, "SOCKET_FILE", path), \
                 mock.patch.object(app, "PID_FILE", pid), \
                 mock.patch.object(app.os, "kill") as kill:
                stopped = app._wait_for_daemon_exit(timeout=0)
            self.assertFalse(stopped)
            self.assertTrue(path.exists())
            self.assertTrue(pid.exists())
            self.assertFalse(any(call.args[1] != 0 for call in kill.call_args_list))

    def test_upgrade_can_start_after_old_daemon_removes_its_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vice.sock"
            path.touch()
            daemon = mock.Mock()
            daemon.cfg.sharing.enabled = False
            daemon.run = mock.AsyncMock()

            def takeover(status):
                path.unlink()
                return True

            with mock.patch.object(main, "SOCKET_FILE", path), \
                 mock.patch.object(main, "PID_FILE", Path(tmp) / "vice.pid"), \
                 mock.patch.object(main, "normalize_runtime_environment"), \
                 mock.patch.object(main, "_setup_daemon_logging"), \
                 mock.patch.object(main, "running_under_systemd", return_value=False), \
                 mock.patch.object(main, "_ipc", return_value=json.dumps({"version": "0.0.1"})), \
                 mock.patch.object(main, "_take_over_outdated_daemon", side_effect=takeover), \
                 mock.patch.object(main, "ViceDaemon", return_value=daemon):
                result = CliRunner().invoke(main.cli, ["start", "--no-open-ui"])
            self.assertEqual(result.exit_code, 0, result.output)
            daemon.run.assert_awaited_once()

    def test_launcher_does_not_restart_after_shutdown_timeout(self):
        with mock.patch.object(app, "_daemon_status", return_value={"version": "0.0.1"}), \
             mock.patch.object(app, "_wait_for_server", return_value=True), \
             mock.patch.object(app, "_stop_daemon"), \
             mock.patch.object(app, "_wait_for_daemon_exit", return_value=False), \
             mock.patch.object(app, "_clear_stale_socket"), \
             mock.patch.object(app, "_start_daemon") as start:
            try:
                app._ensure_server("http://127.0.0.1:8765/", startup_timeout=0)
            except RuntimeError:
                pass
        start.assert_not_called()


_DAEMON_SCRIPT = r'''
import asyncio, json, os, signal, sys
from pathlib import Path
from types import SimpleNamespace
from vice import main

root = Path(sys.argv[1])
main.SOCKET_FILE = root / "vice.sock"
main.PID_FILE = root / "vice.pid"
if len(sys.argv) > 2:
    main.__version__ = sys.argv[2]
main.normalize_runtime_environment = lambda: None
main._setup_daemon_logging = lambda debug: None
main.running_under_systemd = lambda: False

class FixtureDaemon:
    def __init__(self):
        self.cfg = SimpleNamespace(sharing=SimpleNamespace(enabled=False))

    async def run(self):
        done = asyncio.Event()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, done.set)
        async def handle(reader, writer):
            try:
                command = await reader.readline()
                if command == b"status\n":
                    writer.write(json.dumps({"version": main.__version__}).encode() + b"\n")
                elif command == b"stop\n":
                    writer.write(b"ok\n")
                    done.set()
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        main.PID_FILE.write_text(str(os.getpid()))
        server = await asyncio.start_unix_server(handle, path=str(main.SOCKET_FILE))
        try:
            await done.wait()
        finally:
            server.close()
            await server.wait_closed()
            main.SOCKET_FILE.unlink(missing_ok=True)
            main.PID_FILE.unlink(missing_ok=True)

main.ViceDaemon = FixtureDaemon
main.cli(["start", "--no-open-ui"])
'''


class DaemonProcessTests(unittest.TestCase):
    def _spawn(self, root, version=None):
        args = [sys.executable, "-c", _DAEMON_SCRIPT, str(root)]
        if version:
            args.append(version)
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self._reap, proc)
        return proc

    @staticmethod
    def _reap(proc):
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()

    def _wait_for_owner(self, root, proc):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail(proc.communicate()[1].decode())
            try:
                if (root / "vice.sock").exists() and (root / "vice.pid").read_text() == str(proc.pid):
                    return
            except FileNotFoundError:
                pass
            time.sleep(0.02)
        self.fail("daemon did not claim the fixture socket")

    def test_duplicate_launch_does_not_replace_the_running_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._spawn(root)
            self._wait_for_owner(root, first)
            second = self._spawn(root)
            _, error = second.communicate(timeout=5)
            self.assertEqual(second.returncode, 1, error.decode())
            self.assertIn(b"already running", error)
            self.assertIsNone(first.poll())
            self.assertEqual((root / "vice.pid").read_text(), str(first.pid))
            self._reap(first)

    def test_upgrade_waits_for_old_process_and_claims_its_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._spawn(root, "0.0.1")
            self._wait_for_owner(root, first)
            second = self._spawn(root)
            self._wait_for_owner(root, second)
            first.communicate(timeout=3)
            self.assertEqual(first.returncode, 0)
            self.assertIsNone(second.poll())
            self._reap(second)
