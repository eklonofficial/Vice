import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from vice import app, media, share
from vice.main import ViceDaemon


class WindowCloseTests(unittest.TestCase):
    def test_close_waits_until_bridge_has_delivered_its_result(self):
        replying = threading.Event()
        replied = threading.Event()
        closed = threading.Event()
        win = mock.Mock()
        win.destroy.side_effect = closed.set

        def bridge():
            app._close_window_after_bridge(win)
            replying.set()
            replied.wait(2)

        thread = threading.Thread(target=bridge)
        thread.start()
        try:
            self.assertTrue(replying.wait(1))
            self.assertFalse(closed.is_set())
            replied.set()
            thread.join(1)
            self.assertTrue(closed.wait(1))
            win.destroy.assert_called_once_with()
        finally:
            replied.set()
            thread.join(2)


class StopIPCTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_stop_waits_for_the_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "vice.sock"
            peer_closed = asyncio.get_running_loop().create_future()

            async def handle(reader, writer):
                try:
                    self.assertEqual(await reader.readline(), b"stop\n")
                    # Delay the reply to expose a client that closes after send.
                    try:
                        closed = await asyncio.wait_for(reader.read(1), 0.05) == b""
                    except asyncio.TimeoutError:
                        closed = False
                    peer_closed.set_result(closed)
                    writer.write(b"ok\n")
                    await writer.drain()
                except ConnectionError:
                    pass
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except ConnectionError:
                        pass

            server = await asyncio.start_unix_server(handle, path=str(socket_path))
            async with server:
                with mock.patch.object(app, "SOCKET_FILE", socket_path):
                    await asyncio.to_thread(app._stop_daemon)
                    self.assertFalse(await asyncio.wait_for(peer_closed, 1))

    async def test_stop_survives_a_client_disconnecting_before_acknowledgment(self):
        daemon = ViceDaemon.__new__(ViceDaemon)
        reader = asyncio.StreamReader()
        reader.feed_data(b"stop\n")
        reader.feed_eof()
        writer = mock.Mock()
        writer.drain = mock.AsyncMock(side_effect=BrokenPipeError())
        with mock.patch("vice.main.os.kill") as kill:
            await daemon._handle_ipc(reader, writer)
        kill.assert_called_once_with(os.getpid(), signal.SIGTERM)


class MediaProbeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def _interrupted_probe(self, probe, *, cancel):
        real_spawn = asyncio.create_subprocess_exec
        real_wait_for = asyncio.wait_for
        spawned = asyncio.Event()
        child = None

        async def spawn(*args, **kwargs):
            nonlocal child
            child = await real_spawn(
                sys.executable, "-c",
                "import sys, time; sys.stderr.write('x' * 200000); sys.stderr.flush(); time.sleep(60)",
                **kwargs,
            )
            spawned.set()
            return child

        async def short_wait(awaitable, timeout):
            return await real_wait_for(awaitable, 0.05)

        try:
            with mock.patch.object(asyncio, "create_subprocess_exec", spawn), \
                 mock.patch.object(asyncio, "wait_for", short_wait):
                task = asyncio.create_task(probe(Path("fixture.mp4")))
                await real_wait_for(spawned.wait(), 2)
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    await real_wait_for(task, 2)
                self.assertIsNotNone(child.returncode, "media probe child was left running")
        finally:
            if child is not None and child.returncode is None:
                child.kill()
                await child.communicate()

    async def test_metadata_timeout_reaps_child(self):
        await self._interrupted_probe(media.probe_media_detailed, cancel=False)

    async def test_metadata_cancel_reaps_child(self):
        await self._interrupted_probe(media.probe_media_detailed, cancel=True)

    async def test_packet_timeout_reaps_child(self):
        await self._interrupted_probe(share._first_video_packet, cancel=False)

    async def test_packet_cancel_reaps_child(self):
        await self._interrupted_probe(share._first_video_packet, cancel=True)

    async def test_decode_timeout_reaps_child(self):
        await self._interrupted_probe(share._decode_complaint, cancel=False)

    async def test_decode_cancel_reaps_child(self):
        await self._interrupted_probe(share._decode_complaint, cancel=True)


class PreviewLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def _interrupted_conversion(self, *, cancel):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "clip.mkv"
            source.write_bytes(b"source must survive")
            spawned = asyncio.Event()
            child = None
            real_spawn = asyncio.create_subprocess_exec

            async def spawn(*args, **kwargs):
                nonlocal child
                child = await real_spawn(
                    sys.executable, "-c",
                    "import sys, time; sys.stderr.write('x' * 200000); sys.stderr.flush(); time.sleep(60)",
                    **kwargs
                )
                spawned.set()
                return child

            try:
                with mock.patch.object(share, "PROXY_DIR", root / "proxies"), \
                     mock.patch.object(share, "_PREVIEW_TIMEOUT", 0.05, create=True), \
                     mock.patch.object(share.asyncio, "create_subprocess_exec", spawn):
                    task = asyncio.create_task(share._make_preview_proxy(source, "hevc"))
                    await asyncio.wait_for(spawned.wait(), 2)
                    if cancel:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        self.assertIsNone(await asyncio.wait_for(task, 1))
                    self.assertIsNotNone(child.returncode, "conversion child was left running")
                    self.assertEqual(list((root / "proxies").iterdir()), [])
                    self.assertEqual(source.read_bytes(), b"source must survive")
            finally:
                if child is not None and child.returncode is None:
                    child.kill()
                    await child.communicate()

    async def test_cancel_reaps_conversion(self):
        await self._interrupted_conversion(cancel=True)

    async def test_timeout_reaps_conversion(self):
        await self._interrupted_conversion(cancel=False)

    async def test_different_clips_share_one_conversion_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Avoid opening any of the user's persistent stores.
            server = share.ShareServer.__new__(share.ShareServer)
            server._proxy_lock = asyncio.Lock()
            server._proxy_tasks = set()
            server._proxy_stopping = False
            server._get_meta = mock.AsyncMock(return_value={"vcodec": "hevc"})
            entered = asyncio.Event()
            release = asyncio.Event()
            active = peak = calls = 0

            async def convert(path, codec):
                nonlocal active, peak, calls
                calls += 1
                active += 1
                peak = max(peak, active)
                entered.set()
                try:
                    await release.wait()
                    return None
                finally:
                    active -= 1

            with mock.patch.object(share, "PROXY_DIR", root), \
                 mock.patch.object(share, "_make_preview_proxy", convert):
                tasks = [asyncio.create_task(server._serve_preview_proxy(str(i), root / f"{i}.mkv"))
                         for i in range(3)]
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    await asyncio.sleep(0.03)
                    release.set()
                    await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            self.assertEqual(calls, 3)
            self.assertEqual(peak, 1, "different clips launched concurrent encoders")
            self.assertEqual(server._proxy_tasks, set())

    async def test_shutdown_cancels_active_and_queued_previews(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = share.ShareServer.__new__(share.ShareServer)
            server._proxy_lock = asyncio.Lock()
            server._proxy_tasks = set()
            server._proxy_stopping = False
            server._ws_clients = set()
            server._exports = mock.Mock(stop=mock.AsyncMock())
            server._tunnel_proc = server._local_runner = server._public_runner = None
            server._get_meta = mock.AsyncMock(return_value={"vcodec": "hevc"})
            entered = asyncio.Event()

            async def convert(path, codec):
                entered.set()
                await asyncio.Event().wait()

            with mock.patch.object(share, "PROXY_DIR", root), \
                 mock.patch.object(share, "_make_preview_proxy", convert):
                tasks = [asyncio.create_task(server._serve_preview_proxy(str(i), root / f"{i}.mkv"))
                         for i in range(2)]
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    await asyncio.wait_for(server.stop(), 1)
                    self.assertTrue(all(task.cancelled() for task in tasks))
                    self.assertEqual(server._proxy_tasks, set())
                    with self.assertRaises(share.web.HTTPServiceUnavailable):
                        await server._serve_preview_proxy("new", root / "new.mkv")
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

    async def test_cached_preview_does_not_wait_for_another_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = share.ShareServer.__new__(share.ShareServer)
            server._proxy_lock = asyncio.Lock()
            server._proxy_tasks = set()
            server._proxy_stopping = False
            server._get_meta = mock.AsyncMock()
            with mock.patch.object(share, "PROXY_DIR", root):
                source = root / "clip.mkv"
                share._proxy_path(source).write_bytes(b"cached")
                async with server._proxy_lock:
                    response = await asyncio.wait_for(server._serve_preview_proxy("clip", source), 1)
                self.assertEqual(response.status, 200)
                server._get_meta.assert_not_called()

    def test_old_preview_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "clip.mkv"
            source.write_bytes(b"source")
            st = source.stat()
            old = root / f"clip_{st.st_size}_{st.st_mtime_ns}.mp4"
            old.write_bytes(b"old ten-bit preview")
            with mock.patch.object(share, "PROXY_DIR", root):
                self.assertNotEqual(share._proxy_path(source), old)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class PreviewFormatTests(unittest.IsolatedAsyncioTestCase):
    async def test_ten_bit_source_gets_an_eight_bit_browser_preview(self):
        await self._check_format(66, 50)

    async def test_odd_dimensions_are_padded_for_browser_playback(self):
        await self._check_format(65, 49)

    async def _check_format(self, width, height):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "ten-bit.mkv"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                f"testsrc=size={width}x{height}:rate=5:duration=0.4,format=yuv444p10le",
                "-c:v", "ffv1", "-threads", "1", str(source),
            ], check=True)
            original = source.read_bytes()
            with mock.patch.object(share, "PROXY_DIR", root / "proxies"):
                proxy = await share._make_preview_proxy(source, "ffv1")
            self.assertIsNotNone(proxy)
            result = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,pix_fmt,width,height", "-of", "json", str(proxy),
            ], check=True, capture_output=True, text=True)
            stream = json.loads(result.stdout)["streams"][0]
            self.assertEqual(stream["codec_name"], "h264")
            self.assertEqual(stream["pix_fmt"], "yuv420p")
            self.assertEqual((stream["width"], stream["height"]),
                             (width + width % 2, height + height % 2))
            self.assertEqual(source.read_bytes(), original)
