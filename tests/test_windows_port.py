"""Tests for the Windows port.

Most of this is pure logic (command building, segment choice, key mapping,
the OBS protocol) and runs everywhere, so a Linux CI run still catches a
regression in it. Anything that needs Win32 itself is skipped off Windows,
and anything that touches the real desktop (injected keys, screen capture)
only runs with VICE_LIVE_TESTS=1.
"""

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import WSMsgType, web

from vice import oscompat as plat
from vice import recorder_win as rw
from vice.config import Config
from vice.editor import _escape_filter_path

IS_WINDOWS = sys.platform == "win32"
LIVE = os.environ.get("VICE_LIVE_TESTS") == "1"
FFMPEG = shutil.which("ffmpeg")


# ── platform paths ───────────────────────────────────────────────────────────

class PlatformPathTests(unittest.TestCase):
    @unittest.skipIf(IS_WINDOWS, "Linux layout")
    def test_linux_paths_are_exactly_what_they_always_were(self) -> None:
        home = plat.home_dir()
        self.assertEqual(plat.config_dir(), home / ".config" / "vice")
        self.assertEqual(plat.data_dir(), home / ".local" / "share" / "vice")
        self.assertEqual(plat.cache_dir(), home / ".cache" / "vice")
        self.assertEqual(plat.runtime_dir(), Path("/tmp/vice"))
        self.assertEqual(plat.videos_dir(), home / "Videos")
        self.assertEqual(plat.new_group_kwargs(), {"start_new_session": True})
        self.assertEqual(plat.no_window_kwargs(), {})

    @unittest.skipUnless(IS_WINDOWS, "Windows layout")
    def test_windows_paths_follow_appdata(self) -> None:
        with mock.patch.dict(os.environ, {"APPDATA": r"C:\R", "LOCALAPPDATA": r"C:\L"}):
            self.assertEqual(plat.config_dir(), Path(r"C:\R\Vice"))
            self.assertEqual(plat.data_dir(), Path(r"C:\L\Vice"))
            self.assertEqual(plat.cache_dir(), Path(r"C:\L\Vice\cache"))
        self.assertEqual(plat.runtime_dir(), Path(tempfile.gettempdir()) / "vice")
        self.assertTrue(plat.videos_dir().is_absolute())


# ── hotkeys ──────────────────────────────────────────────────────────────────

try:
    from vice import hotkey_win
except (ImportError, ValueError, AttributeError):
    # Off Windows: ctypes has no WINFUNCTYPE, and on some Pythons no wintypes.
    hotkey_win = None


@unittest.skipIf(hotkey_win is None, "ctypes.wintypes unavailable")
class WindowsKeyMappingTests(unittest.TestCase):
    def test_function_and_modifier_keys_map_to_evdev_names(self) -> None:
        k = hotkey_win.key_name_for
        self.assertEqual(k(0x78, 0x43, False), "KEY_F9")
        self.assertEqual(k(0x87, 0x00, False), "KEY_F24")
        self.assertEqual(k(0xA4, 0x38, False), "KEY_LEFTALT")
        self.assertEqual(k(0xA5, 0x38, True), "KEY_RIGHTALT")
        self.assertEqual(k(0xA2, 0x1D, False), "KEY_LEFTCTRL")
        self.assertEqual(k(0x2C, 0x37, True), "KEY_SYSRQ")

    def test_letters_follow_the_physical_key_not_the_layout(self) -> None:
        # On AZERTY the key left of S sends VK 'Q' but is still scan code 0x1E,
        # and the settings UI (KeyboardEvent.code) calls that position KeyA.
        self.assertEqual(hotkey_win.key_name_for(ord("Q"), 0x1E, False), "KEY_A")
        self.assertEqual(hotkey_win.key_name_for(ord("A"), 0x1E, False), "KEY_A")

    def test_numpad_and_navigation_are_told_apart_by_the_extended_flag(self) -> None:
        k = hotkey_win.key_name_for
        self.assertEqual(k(0x24, 0x47, True), "KEY_HOME")
        self.assertEqual(k(0x24, 0x47, False), "KEY_KP7")
        self.assertEqual(k(0x0D, 0x1C, False), "KEY_ENTER")
        self.assertEqual(k(0x0D, 0x1C, True), "KEY_KPENTER")

    def test_every_key_the_settings_ui_can_capture_is_reportable(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "ui-src" / "lib" / "hotkeyCapture.ts").read_text(
            encoding="utf-8")
        import re
        ui_names = set(re.findall(r"'(KEY_[A-Z0-9]+)'", source))
        ui_names |= {f"KEY_F{i}" for i in range(1, 25)}
        ui_names |= {f"KEY_{c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}
        missing = sorted(ui_names - set(hotkey_win.list_available_keys()))
        self.assertEqual(missing, [], "keys the UI can bind but Windows never reports")


@unittest.skipUnless(IS_WINDOWS and LIVE, "injects real key presses; set VICE_LIVE_TESTS=1")
class WindowsHookLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_combo_and_double_tap(self) -> None:
        import ctypes
        user32 = ctypes.windll.user32

        def tap(vk: int, scan: int = 0) -> None:
            user32.keybd_event(vk, scan, 0, 0)
            user32.keybd_event(vk, scan, 2, 0)

        hits: list[str] = []

        def record(name):
            async def cb():
                hits.append(name)
            return cb

        listener = hotkey_win.HotkeyListener()
        listener.on("KEY_F24", record("single"))
        listener.on("KEY_LEFTCTRL+KEY_F23", record("combo"))
        listener.on_double("KEY_F22", record("double"))
        await listener.start()
        try:
            self.assertTrue(listener.available)
            tap(0x87)
            user32.keybd_event(0xA2, 0x1D, 0, 0)
            tap(0x86)
            user32.keybd_event(0xA2, 0x1D, 2, 0)
            tap(0x85)
            await asyncio.sleep(0.1)
            tap(0x85)
            await asyncio.sleep(0.6)
        finally:
            await listener.stop()
        self.assertEqual(hits, ["single", "combo", "double"])


# ── audio feed ───────────────────────────────────────────────────────────────

class WallClockTests(unittest.TestCase):
    def setUp(self) -> None:
        from vice import win_audio
        self.wa = win_audio

    def test_silence_is_inserted_when_the_device_falls_behind(self) -> None:
        clock = self.wa.WallClock(now=0.0)
        # One second has passed but only 10 ms of audio arrived: WASAPI
        # loopback delivers nothing at all while nothing plays.
        pad = clock.feed(480, now=1.0)
        self.assertEqual(pad, 48000 - 480)

    def test_small_jitter_is_left_alone(self) -> None:
        clock = self.wa.WallClock(now=0.0)
        self.assertEqual(clock.feed(480, now=0.02), 0)

    def test_a_fast_device_clock_is_trimmed(self) -> None:
        clock = self.wa.WallClock(now=0.0)
        # A second of audio after half a second of time.
        self.assertLess(clock.feed(48000, now=0.5), 0)

    def test_stream_stays_on_the_wall_clock_over_time(self) -> None:
        clock = self.wa.WallClock(now=0.0)
        written = 0
        t = 0.0
        for i in range(3000):  # 30 s of 10 ms blocks, with 1 s gaps now and then
            t += 0.010 + (1.0 if i % 500 == 0 else 0.0)
            correction = clock.feed(480, now=t)
            written += 480 + correction
        self.assertLess(abs(written - t * 48000), self.wa.DRIFT_TOLERANCE_FRAMES + 480)

    @unittest.skipUnless(IS_WINDOWS and LIVE, "opens real audio devices; set VICE_LIVE_TESTS=1")
    def test_capture_works_on_any_thread_whichever_imported_soundcard(self) -> None:
        # soundcard only set COM up on the thread that imported it, so capture
        # failed with 0x800401F0 once the Settings list had imported it first.
        import threading
        results: list[str] = []

        def listing() -> None:
            results.append(str(len(self.wa.list_audio_sources()["sources"])))

        def capture() -> None:
            src = self.wa.AudioSource("default_output")
            try:
                src.start()
                results.append("ok")
            except RuntimeError as exc:
                results.append(str(exc))
            finally:
                src.stop()

        for step in (listing, capture, listing, capture):
            thread = threading.Thread(target=step)
            thread.start()
            thread.join(30)
        self.assertEqual(results[1::2], ["ok", "ok"])

    @unittest.skipUnless(IS_WINDOWS, "needs numpy, installed with soundcard on Windows")
    def test_mono_and_single_channel_become_centred_stereo(self) -> None:
        import numpy as np
        mono = np.array([[0.5], [-0.5]], dtype="float32")
        pcm = self.wa.to_stereo_s16(mono)
        self.assertEqual(np.frombuffer(pcm, "<i2").tolist(), [16383, 16383, -16383, -16383])
        left_only = np.array([[1.0, 0.0]], dtype="float32")
        pcm = self.wa.to_stereo_s16(left_only, mono=True)
        self.assertEqual(np.frombuffer(pcm, "<i2").tolist(), [16383, 16383])

    def test_a_connection_opens_with_silence_back_to_its_epoch(self) -> None:
        # ffmpeg connects some time after the epoch the video is timed from;
        # the gap must arrive as silence or the sound lands late.
        conn = self.wa.PcmConnection(epoch=time.time() - 0.25)
        import socket
        with socket.create_connection(("127.0.0.1", conn.port), timeout=5) as client:
            client.settimeout(5)
            got = b""
            deadline = time.time() + 5
            want = int(0.25 * self.wa.SAMPLE_RATE) * self.wa.BYTES_PER_FRAME
            while len(got) < want and time.time() < deadline:
                got += client.recv(65536)
        conn.close()
        self.assertGreaterEqual(len(got), want)
        self.assertEqual(set(got[:want]), {0})


# ── ffmpeg command building ──────────────────────────────────────────────────

OUT_INTEL = {"adapter": 0, "output": 0, "vendor": "intel", "gpu": "Iris", "device": r"\\.\DISPLAY1",
             "x": 0, "y": 0, "width": 2560, "height": 1600, "primary": True}
OUT_NVIDIA = {"adapter": 1, "output": 0, "vendor": "nvidia", "gpu": "RTX", "device": r"\\.\DISPLAY5",
              "x": 2560, "y": 0, "width": 1920, "height": 1080, "primary": False}
ALL_ENCODERS = ["h264_nvenc", "hevc_nvenc", "av1_nvenc", "h264_qsv", "hevc_qsv", "av1_qsv",
                "h264_amf", "hevc_amf", "av1_amf", "libx264", "libx265", "libsvtav1"]


class EncoderPlanTests(unittest.TestCase):
    def plans(self, out, vendors, encoder="auto"):
        rc = Config().recording
        rc.encoder = encoder
        return [(p.encoder, p.zero_copy) for p in rw.encoder_plans(rc, out, vendors, ALL_ENCODERS)]

    def test_the_monitors_own_gpu_is_tried_first_without_a_copy(self) -> None:
        plans = self.plans(OUT_INTEL, ["intel", "nvidia"])
        self.assertEqual(plans[0], ("h264_qsv", True))
        self.assertIn(("h264_nvenc", False), plans)
        self.assertEqual(plans[-1], ("libx264", False))

    def test_a_hybrid_laptops_dgpu_monitor_can_fall_back_to_the_igpu(self) -> None:
        # The case measured on an RTX 3050 Ti + Iris Xe: NVENC refused to open
        # on an older driver, and QSV fed downloaded frames was what worked.
        plans = self.plans(OUT_NVIDIA, ["intel", "nvidia"])
        self.assertEqual(plans[0], ("h264_nvenc", True))
        self.assertIn(("h264_qsv", False), plans)
        self.assertLess(plans.index(("h264_qsv", False)), plans.index(("libx264", False)))

    def test_an_absent_vendor_is_never_tried(self) -> None:
        plans = self.plans(OUT_INTEL, ["intel"])
        self.assertFalse([p for p in plans if "nvenc" in p[0] or "amf" in p[0]])

    def test_a_chosen_encoder_goes_first_and_sets_the_codec(self) -> None:
        plans = self.plans(OUT_INTEL, ["intel", "nvidia"], encoder="hevc_nvenc")
        self.assertEqual(plans[0], ("hevc_nvenc", False))
        self.assertIn(("hevc_qsv", True), plans)

    def test_hevc_setting_asks_for_hevc_from_every_vendor(self) -> None:
        plans = self.plans(OUT_INTEL, ["intel"], encoder="libx265")
        self.assertEqual(plans[0], ("libx265", False))
        self.assertIn(("hevc_qsv", True), plans)


class CaptureCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rc = Config().recording
        self.plan = rw.EncoderPlan("h264_qsv", True, ("-init_hw_device", "qsv=qs@dda"),
                                   ("hwmap=derive_device=qsv", "format=qsv"))

    def test_ddagrab_is_a_filter_source_on_the_right_adapter(self) -> None:
        cmd = rw.build_capture_cmd(self.rc, OUT_NVIDIA, self.plan, [], [], ["out.ts"])
        self.assertIn("d3d11va=dda:1", cmd)
        self.assertEqual(cmd[cmd.index("-filter_hw_device") + 1], "dda")
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertTrue(graph.startswith("ddagrab=output_idx=0:framerate=60"))
        self.assertNotIn("-f lavfi", " ".join(cmd))

    def test_video_is_timed_from_the_same_epoch_as_the_audio(self) -> None:
        cmd = rw.build_capture_cmd(self.rc, OUT_INTEL, self.plan, [], [], ["o.ts"], epoch_us=123)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("setpts=(RTCTIME-123)/(TB*1000000)", graph)

    def test_desktop_and_mic_mix_into_one_track_by_default(self) -> None:
        self.rc.capture_microphone = True
        tracks = rw.audio_tracks(self.rc)
        self.assertEqual(tracks, [["default_output", "default_input"]])
        cmd = rw.build_capture_cmd(self.rc, OUT_INTEL, self.plan,
                                   ["tcp://127.0.0.1:1", "tcp://127.0.0.1:2"], tracks, ["o.ts"])
        self.assertEqual(cmd.count("-i"), 2)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("[0:a][1:a]amix=inputs=2", graph)
        self.assertIn("-c:a", cmd)

    def test_volume_sliders_split_desktop_and_mic_like_linux(self) -> None:
        self.rc.capture_microphone = True
        self.rc.microphone_volume = 0.5
        self.assertEqual(rw.audio_tracks(self.rc), [["default_output"], ["default_input"]])

    def test_per_app_audio_is_dropped_on_windows(self) -> None:
        self.rc.audio_tracks = ["default_output", "app:Discord"]
        with self.assertLogs("vice.recorder", "WARNING"):
            self.assertEqual(rw.audio_tracks(self.rc), [["default_output"]])

    def test_segments_keep_running_timestamps(self) -> None:
        args = rw.segment_output_args(Path("segs"), self.rc)
        self.assertNotIn("-reset_timestamps", args)
        self.assertIn("mpegts", args)

    def test_qsv_makes_every_keyframe_an_idr(self) -> None:
        args = rw.encoder_args(self.plan, self.rc, 60)
        self.assertEqual(args[args.index("-g") + 1], "120")
        self.assertIn("-idr_interval", args)
        self.assertNotIn("-force_key_frames", args)

    def test_concat_uses_forward_slashes(self) -> None:
        url = rw.concat_input([Path("C:/a/seg1.ts"), Path("C:/a/seg2.ts")])
        self.assertEqual(url, "concat:C:/a/seg1.ts|C:/a/seg2.ts")

    def test_display_ids_match_however_they_are_typed(self) -> None:
        for typed in ("DISPLAY5", r"\\.\DISPLAY5", "display5", r"\.\DISPLAY5"):
            self.assertEqual(rw.display_key(typed), "DISPLAY5")

    def test_probe_failure_names_the_driver_problem(self) -> None:
        stderr = (
            "[h264_nvenc @ 0x1] Driver does not support the required nvenc API version.\n"
            "[out#0/null @ 0x2] Nothing was written into output file\n"
        )
        self.assertIn("Driver does not support", rw._probe_failure_reason(stderr))


class SegmentSelectionTests(unittest.TestCase):
    def test_length_is_read_from_the_clock_not_assumed(self) -> None:
        # Encoders do not honour the GOP exactly: a 4 s segment where 2 s was
        # asked for must count as 4 s, not as one more file.
        # Segment lengths here: b 4 s, c 2 s, d 2 s, e 0.5 s (still being written).
        files = [(100.0, 1, Path("a")), (104.0, 1, Path("b")), (106.0, 1, Path("c")),
                 (108.0, 1, Path("d")), (108.5, 1, Path("e"))]
        # 3 s is covered by c+d+e (4.5 s); one segment more is kept as margin.
        self.assertEqual(rw.select_segments(files, 3), [Path("b"), Path("c"), Path("d"), Path("e")])
        # 1 s needs d+e, plus the margin; b's 4 s is neither needed nor taken.
        self.assertEqual(rw.select_segments(files, 1), [Path("c"), Path("d"), Path("e")])

    def test_an_empty_just_opened_segment_is_skipped(self) -> None:
        files = [(1.0, 5, Path("a")), (3.0, 5, Path("b")), (3.1, 0, Path("c"))]
        self.assertNotIn(Path("c"), rw.select_segments(files, 60))

    def test_asking_for_more_than_exists_returns_everything(self) -> None:
        files = [(1.0, 5, Path("a")), (3.0, 5, Path("b"))]
        self.assertEqual(rw.select_segments(files, 600), [Path("a"), Path("b")])
        self.assertEqual(rw.select_segments([], 10), [])


# ── editor paths ─────────────────────────────────────────────────────────────

class FilterPathEscapingTests(unittest.TestCase):
    def test_drive_colon_and_quotes_are_escaped_for_both_passes(self) -> None:
        # Option pass gives C\:\\a\'b; the graph pass escapes each \ and ' again.
        self.assertEqual(_escape_filter_path(r"C:\a'b"), r"C\\:\\\\a\\\'b")

    @unittest.skipUnless(FFMPEG, "ffmpeg not installed")
    def test_ffmpeg_reads_the_paths_back_exactly(self) -> None:
        # The directory name is as hostile as a filtergraph gets.
        font = Path(__file__).resolve().parents[1] / "vice" / "fonts" / "Geist-Bold.ttf"
        with tempfile.TemporaryDirectory(prefix="vice it's [x], y; z ") as tmp:
            text = Path(tmp) / "t.txt"
            text.write_text("Héllo: 'quoted'", encoding="utf-8")
            graph = (f"color=c=black:s=64x64:d=1[b];[b]drawtext=fontfile={_escape_filter_path(str(font))}"
                     f":textfile={_escape_filter_path(str(text))}[v]")
            result = subprocess.run(
                ["ffmpeg", "-v", "error", "-filter_complex", graph, "-map", "[v]",
                 "-frames:v", "1", "-f", "null", "-"],
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


# ── daemon IPC ───────────────────────────────────────────────────────────────

@unittest.skipUnless(IS_WINDOWS, "the TCP transport is Windows-only; Linux keeps its Unix socket")
class WindowsIpcTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_token_is_required_and_the_protocol_is_unchanged(self) -> None:
        from vice import runtime

        async def handler(reader, writer):
            line = await reader.readline()
            writer.write(b"echo " + line)
            await writer.drain()
            writer.close()

        with tempfile.TemporaryDirectory() as tmp:
            sock = Path(tmp) / "vice.sock"
            server = await runtime.start_ipc_server(handler, sock)
            try:
                reader, writer = await runtime.open_ipc_connection(sock)
                writer.write(b"status\n")
                await writer.drain()
                self.assertEqual(await reader.readline(), b"echo status\n")
                writer.close()

                # Without the token the daemon hangs up before reading anything.
                port = json.loads(sock.read_text())["port"]
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"stop\n")
                await writer.drain()
                self.assertEqual(await reader.read(), b"")
                writer.close()

                self.assertTrue(runtime.daemon_is_running(sock, Path(tmp) / "missing.pid"))
            finally:
                server.close()
                await server.wait_closed()

    async def test_a_crashed_daemons_endpoint_is_seen_as_dead_quickly(self) -> None:
        # Windows takes ~2 s to refuse a loopback connection to a closed port,
        # so a short probe timed out and a crash left Vice unable to start.
        import socket as _socket
        from vice import runtime
        with _socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead_port = s.getsockname()[1]
        dead = subprocess.Popen([sys.executable, "-c", ""])
        dead.wait()
        with tempfile.TemporaryDirectory() as tmp:
            sock, pid = Path(tmp) / "vice.sock", Path(tmp) / "vice.pid"
            sock.write_text(json.dumps({"port": dead_port, "token": "x", "pid": dead.pid}))
            pid.write_text(str(dead.pid))
            started = time.perf_counter()
            self.assertFalse(runtime.daemon_is_running(sock, pid))
            with self.assertRaises(ConnectionRefusedError):
                await runtime.open_ipc_connection(sock)
            self.assertLess(time.perf_counter() - started, 1.0)

    async def test_an_unreadable_endpoint_looks_like_a_dead_socket(self) -> None:
        from vice import runtime
        with tempfile.TemporaryDirectory() as tmp:
            sock = Path(tmp) / "vice.sock"
            with self.assertRaises(FileNotFoundError):
                await runtime.open_ipc_connection(sock)
            sock.write_text("garbage")
            with self.assertRaises(ConnectionRefusedError):
                await runtime.open_ipc_connection(sock)
            self.assertFalse(runtime.daemon_is_running(sock, Path(tmp) / "missing.pid"))


# ── Win32 glue ───────────────────────────────────────────────────────────────

@unittest.skipUnless(IS_WINDOWS, "Win32 only")
class Win32Tests(unittest.TestCase):
    def test_hdrop_is_a_dropfiles_block_of_wide_paths(self) -> None:
        from vice.win32 import hdrop_payload
        data = hdrop_payload([r"C:\a.mp4", r"C:\b.mp4"])
        self.assertEqual(int.from_bytes(data[0:4], "little"), 20)
        self.assertEqual(int.from_bytes(data[16:20], "little"), 1)
        self.assertEqual(data[20:].decode("utf-16-le"), "C:\\a.mp4\0C:\\b.mp4\0\0")

    def test_dxgi_reports_attached_outputs(self) -> None:
        from vice.win32 import dxgi_outputs
        outputs = dxgi_outputs()
        for out in outputs:
            self.assertGreater(out["width"], 0)
            self.assertTrue(out["device"].startswith("\\\\.\\DISPLAY"))

    def test_os_kill_is_never_used_to_probe_a_pid(self) -> None:
        # On Windows os.kill(pid, 0) is TerminateProcess, not a probe.
        from vice import runtime
        with mock.patch("vice.runtime.os.kill") as kill:
            self.assertTrue(runtime.pid_is_alive(os.getpid()))
        kill.assert_not_called()


# ── OBS ──────────────────────────────────────────────────────────────────────

class FakeOBS:
    """Just enough obs-websocket v5 to drive OBSRecorder: auth, requests,
    and ReplayBufferSaved / StopRecord outputs pointing at real files."""

    def __init__(self, workdir: Path, password: str = "") -> None:
        self.workdir = workdir
        self.password = password
        self.salt, self.challenge = "c2FsdA==", "Y2hhbGxlbmdl"
        self.replay_active = False
        self.requests: list[str] = []
        self.runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._ws)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    def _expected_auth(self) -> str:
        secret = base64.b64encode(hashlib.sha256((self.password + self.salt).encode()).digest()).decode()
        return base64.b64encode(hashlib.sha256((secret + self.challenge).encode()).digest()).decode()

    async def _ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        hello = {"obsWebSocketVersion": "5.5.0", "rpcVersion": 1}
        if self.password:
            hello["authentication"] = {"salt": self.salt, "challenge": self.challenge}
        await ws.send_json({"op": 0, "d": hello})
        identify = await ws.receive_json()
        if self.password and identify["d"].get("authentication") != self._expected_auth():
            await ws.close(code=4009)
            return ws
        await ws.send_json({"op": 2, "d": {"negotiatedRpcVersion": 1}})
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            d = json.loads(msg.data)["d"]
            kind = d["requestType"]
            self.requests.append(kind)
            data: dict = {}
            if kind == "GetReplayBufferStatus":
                data = {"outputActive": self.replay_active}
            elif kind == "StartReplayBuffer":
                self.replay_active = True
            elif kind == "StopRecord":
                data = {"outputPath": str(self._write("recording.mp4"))}
            await ws.send_json({"op": 7, "d": {
                "requestType": kind, "requestId": d["requestId"],
                "requestStatus": {"result": True, "code": 100}, "responseData": data}})
            if kind == "SaveReplayBuffer":
                path = self._write("Replay 2026-09-17.mp4")
                await ws.send_json({"op": 5, "d": {
                    "eventType": "ReplayBufferSaved", "eventIntent": 64,
                    "eventData": {"savedReplayPath": str(path)}}})
        return ws

    def _write(self, name: str) -> Path:
        path = self.workdir / name
        path.write_bytes(b"not really a video")
        return path


class OBSRecorderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "obs").mkdir()
        (root / "library").mkdir()
        self.obs = FakeOBS(root / "obs", password="hunter2")
        await self.obs.start()
        self.addAsyncCleanup(self.obs.stop)
        self.cfg = Config()
        self.cfg.output.directory = str(root / "library")
        self.cfg.recording.obs_port = self.obs.port
        self.cfg.recording.obs_password = "hunter2"
        # The fake files are not media, so the media passes are stubbed.
        for name in ("_wait_for_finalized_clip",):
            patcher = mock.patch(f"vice.recorder_obs.{name}", mock.AsyncMock(return_value=True))
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("_trim_to_last_n_seconds",):
            patcher = mock.patch(f"vice.recorder_obs.{name}", mock.AsyncMock(side_effect=lambda p, s: p))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("vice.recorder_obs._apply_volume_mix", mock.AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_auth_string_matches_the_obs_websocket_spec(self) -> None:
        from vice.recorder_obs import auth_response
        secret = base64.b64encode(hashlib.sha256(b"pwsalt").digest()).decode()
        want = base64.b64encode(hashlib.sha256((secret + "chal").encode()).digest()).decode()
        self.assertEqual(auth_response("pw", "salt", "chal"), want)

    async def test_saving_a_clip_files_obss_replay_into_the_library(self) -> None:
        from vice.recorder_obs import OBSRecorder
        rec = OBSRecorder(self.cfg)
        saved: list[Path] = []
        rec.on_clip_saved(saved.append)
        await rec.start()
        try:
            self.assertTrue(rec.is_healthy())
            self.assertIn("StartReplayBuffer", self.obs.requests)
            path = await rec.save_clip(10)
        finally:
            await rec.stop()
        self.assertIsNotNone(path, rec.last_clip_error)
        self.assertEqual(path.parent, Path(self.cfg.output.directory))
        self.assertTrue(path.name.startswith("Vice_Clip_"))
        self.assertTrue(path.exists())
        self.assertEqual(saved, [path])
        # Vice started the buffer, so Vice stops it again.
        self.assertIn("StopReplayBuffer", self.obs.requests)

    async def test_a_buffer_the_user_started_is_left_running(self) -> None:
        from vice.recorder_obs import OBSRecorder
        self.obs.replay_active = True
        rec = OBSRecorder(self.cfg)
        await rec.start()
        await rec.stop()
        self.assertNotIn("StartReplayBuffer", self.obs.requests)
        self.assertNotIn("StopReplayBuffer", self.obs.requests)

    async def test_sessions_go_through_obs_recording(self) -> None:
        from vice.recorder_obs import OBSRecorder
        rec = OBSRecorder(self.cfg)
        await rec.start()
        try:
            planned = await rec.start_session()
            self.assertIsNotNone(planned)
            path = await rec.stop_session()
        finally:
            await rec.stop()
        self.assertTrue(path.name.startswith("Vice_Session_"))
        self.assertTrue(path.exists())

    async def test_a_wrong_password_is_reported_plainly(self) -> None:
        from vice.recorder_obs import OBSRecorder, OBSError
        self.cfg.recording.obs_password = "wrong"
        rec = OBSRecorder(self.cfg)
        with self.assertRaises(OBSError) as ctx:
            await rec.start()
        self.assertIn("password", str(ctx.exception).lower())
        self.assertFalse(rec.is_healthy())

    async def test_obs_not_running_is_reported_plainly(self) -> None:
        from vice.recorder_obs import OBSRecorder, OBSError
        await self.obs.stop()
        rec = OBSRecorder(self.cfg)
        with self.assertRaises(OBSError) as ctx:
            await rec.start()
        self.assertIn("Could not reach OBS", str(ctx.exception))


# ── installer ────────────────────────────────────────────────────────────────

class InstallScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (Path(__file__).resolve().parents[1] / "install.ps1").read_bytes()
        cls.script = cls.raw.decode("ascii")

    def test_is_plain_ascii(self) -> None:
        # Windows PowerShell 5.1 reads a script with no BOM in the ANSI code
        # page; one non-ASCII character can stop it parsing.
        self.assertTrue(all(b in (9, 10, 13) or 32 <= b < 127 for b in self.raw))

    def test_installs_what_vice_needs_through_winget(self) -> None:
        for package in ("Gyan.FFmpeg", "Python.Python.3.12"):
            self.assertIn(package, self.script)
        self.assertIn("ddagrab", self.script)

    def test_cloudflared_needs_no_administrator(self) -> None:
        # winget's cloudflared is a machine-wide MSI that asks for admin rights;
        # the standalone exe goes in Vice's own bin folder instead.
        self.assertNotIn("Cloudflare.cloudflared", self.script)
        self.assertIn("releases/latest/download/cloudflared-windows-", self.script)

    def test_native_programs_never_abort_the_script_through_stderr(self) -> None:
        # With ErrorActionPreference Stop, 5.1 turns native stderr into a
        # terminating error; "Vice is not running." once aborted -Uninstall.
        import re
        direct = re.findall(r"^\s*& \$(VenvPython|python|ViceExe) ", self.script, re.M)
        self.assertEqual(direct, [])
        # And --flags go through as an array: "--" is PowerShell's own
        # end-of-parameters marker when passed loose to a function.
        self.assertNotRegex(self.script, r"Invoke-Native \S+ --")

    def test_the_vice_command_runs_the_installed_package(self) -> None:
        # python -m would put the current directory first on sys.path.
        self.assertIn("Scripts\\vice.exe", self.script)
        self.assertIn("-Uninstall", self.script)


# ── live capture ─────────────────────────────────────────────────────────────

@unittest.skipUnless(IS_WINDOWS and LIVE and FFMPEG, "records the screen; set VICE_LIVE_TESTS=1")
class LiveCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_clip_is_saved_with_video_and_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config()
            cfg.output.directory = tmp
            cfg.recording.buffer_duration = 20
            rec = rw.DDAGrabRecorder(cfg)
            await rec.start()
            try:
                await asyncio.sleep(6)
                path = await rec.save_clip(4)
            finally:
                await rec.stop()
            self.assertIsNotNone(path, rec.last_clip_error)
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                capture_output=True, text=True,
            ).stdout.split()
            self.assertIn("video", probe)
            self.assertIn("audio", probe)


if __name__ == "__main__":
    unittest.main()
