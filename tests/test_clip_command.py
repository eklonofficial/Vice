"""`vice clip <seconds>` for Stream Deck buttons and scripts (#285)."""

import asyncio
import unittest
from unittest import mock

try:
    import _sandbox  # noqa: F401  keeps every store out of the real home
except ModuleNotFoundError:
    from tests import _sandbox  # noqa: F401

from click.testing import CliRunner

from vice import main as main_mod
from vice.config import Config, RecordingConfig
from vice.main import ViceDaemon


def _daemon(buffer: int = 120) -> ViceDaemon:
    daemon = ViceDaemon.__new__(ViceDaemon)
    daemon.cfg = Config(recording=RecordingConfig(buffer_duration=buffer, clip_duration=20))
    daemon._handle_clip_hotkey = mock.AsyncMock()
    return daemon


class ClipIpcTests(unittest.IsolatedAsyncioTestCase):
    async def _send(self, daemon, line: bytes) -> bytes:
        reader = asyncio.StreamReader()
        reader.feed_data(line)
        reader.feed_eof()
        writer = mock.Mock()
        writer.drain = mock.AsyncMock()
        await daemon._handle_ipc(reader, writer)
        await asyncio.sleep(0)
        return b"".join(call.args[0] for call in writer.write.call_args_list)

    async def test_bare_clip_uses_the_clip_key_duration(self) -> None:
        daemon = _daemon()
        self.assertEqual(await self._send(daemon, b"clip\n"), b"ok\n")
        daemon._handle_clip_hotkey.assert_awaited_once_with(None)

    async def test_a_duration_is_passed_on(self) -> None:
        daemon = _daemon()
        self.assertEqual(await self._send(daemon, b"clip 30\n"), b"ok 30\n")
        daemon._handle_clip_hotkey.assert_awaited_once_with(30)

    async def test_longer_than_the_buffer_saves_the_buffer(self) -> None:
        daemon = _daemon(buffer=60)
        self.assertEqual(await self._send(daemon, b"clip 300\n"), b"ok 60\n")
        daemon._handle_clip_hotkey.assert_awaited_once_with(60)

    async def test_bad_durations_are_refused(self) -> None:
        for line in (b"clip 0\n", b"clip 3\n", b"clip -5\n", b"clip 2.5\n", b"clip soon\n"):
            daemon = _daemon()
            reply = await self._send(daemon, line)
            self.assertTrue(reply.startswith(b"error:"), (line, reply))
            daemon._handle_clip_hotkey.assert_not_awaited()


class ClipCliTests(unittest.TestCase):
    def _run(self, args, reply):
        sent = []

        async def fake_ipc(command, timeout=5.0):
            sent.append(command)
            return reply

        with mock.patch.object(main_mod, "_ipc", side_effect=fake_ipc), \
             mock.patch.object(main_mod, "normalize_runtime_environment"):
            result = CliRunner().invoke(main_mod.cli, ["clip", *args])
        return result, sent

    def test_bare_clip_is_unchanged(self) -> None:
        result, sent = self._run([], "ok")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(sent, ["clip"])
        self.assertIn("Clip triggered!", result.output)

    def test_seconds_as_argument_or_option(self) -> None:
        for args in (["30"], ["--duration", "30"], ["-d", "30"]):
            result, sent = self._run(args, "ok 30")
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(sent, ["clip 30"])
            self.assertIn("last 30 s", result.output)

    def test_the_saved_length_is_what_is_reported(self) -> None:
        result, _ = self._run(["300"], "ok 120")
        self.assertIn("last 120 s", result.output)

    def test_a_refusal_fails(self) -> None:
        result, _ = self._run(["3"], "error: a clip is at least 5 seconds")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("at least 5 seconds", result.output)

    def test_an_older_daemon_is_not_reported_as_success(self) -> None:
        result, _ = self._run(["30"], "unknown command")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("older", result.output)

    def test_not_running(self) -> None:
        result, _ = self._run([], None)
        self.assertEqual(result.exit_code, 1)

    def test_two_different_durations_are_a_usage_error(self) -> None:
        result, sent = self._run(["30", "--duration", "60"], "ok 30")
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
