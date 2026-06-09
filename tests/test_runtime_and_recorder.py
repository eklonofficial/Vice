import asyncio
import os
import socket
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from vice import app as app_mod
from vice import config as config_mod
from vice import main as main_mod
from vice.config import Config, HotkeyClipPreset, HotkeyConfig, OutputConfig, RecordingConfig, SharingConfig
from vice.recorder import (
    GSRRecorder,
    SegmentRecorder,
    _is_wayland,
    create_recorder,
    list_display_options,
    list_gsr_audio_sources,
    _wait_for_finalized_clip,
    _encoder_flags,
)
from vice.runtime import (
    _wayland_runtime_dir_candidates,
    actual_home_dir,
    normalize_runtime_environment,
)

try:
    from vice.share import ShareServer
except ModuleNotFoundError:
    ShareServer = None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _stub_ffprobe(_: Path) -> dict:
    return {"width": 1920, "height": 1080, "duration": 4.2}


class RuntimeEnvironmentTests(unittest.TestCase):
    def test_normalize_runtime_environment_replaces_unexpanded_service_values(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"HOME": "${HOME}", "XDG_RUNTIME_DIR": "/run/user/$(id -u)"},
            clear=False,
        ):
            with mock.patch("vice.runtime.shutil.which", return_value=None):
                normalize_runtime_environment()
            self.assertEqual(os.environ["HOME"], str(actual_home_dir()))
            self.assertEqual(os.environ["XDG_RUNTIME_DIR"], f"/run/user/{os.getuid()}")

    def test_normalize_runtime_environment_loads_display_vars_from_systemd(self) -> None:
        systemd_env = "\n".join(
            [
                "WAYLAND_DISPLAY=wayland-1",
                f"XDG_RUNTIME_DIR=/run/user/{os.getuid()}",
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                "XDG_SESSION_TYPE=wayland",
            ]
        )
        with mock.patch.dict(os.environ, {"HOME": "${HOME}"}, clear=True):
            with mock.patch("vice.runtime.shutil.which", return_value="/usr/bin/systemctl"):
                with mock.patch("vice.runtime.subprocess.check_output", return_value=systemd_env):
                    normalize_runtime_environment()
            self.assertEqual(os.environ["HOME"], str(actual_home_dir()))
            self.assertEqual(os.environ["WAYLAND_DISPLAY"], "wayland-1")
            self.assertEqual(os.environ["XDG_RUNTIME_DIR"], f"/run/user/{os.getuid()}")
            self.assertEqual(os.environ["XDG_SESSION_TYPE"], "wayland")

    def test_wayland_runtime_dir_candidates_include_tmp_fallback(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}, clear=True):
            candidates = _wayland_runtime_dir_candidates()

        self.assertIn(Path(f"/run/user/{os.getuid()}"), candidates)
        self.assertIn(Path(f"/tmp/wayland-{os.getuid()}"), candidates)

    def test_normalize_runtime_environment_recovers_wayland_socket_without_systemd(self) -> None:
        runtime_dir = mock.MagicMock()
        runtime_dir.exists.return_value = True
        runtime_dir.__str__.return_value = "/tmp/vice-runtime"
        candidate = mock.MagicMock()
        candidate.name = "wayland-9"
        candidate.stat.return_value = mock.Mock(st_mode=stat.S_IFSOCK)
        runtime_dir.glob.return_value = [candidate]

        with mock.patch.dict(
            os.environ,
            {"HOME": "${HOME}", "XDG_RUNTIME_DIR": "/run/user/$(id -u)"},
            clear=True,
        ):
            with mock.patch("vice.runtime.shutil.which", return_value=None):
                with mock.patch(
                    "vice.runtime._wayland_runtime_dir_candidates",
                    return_value=[runtime_dir],
                ):
                    normalize_runtime_environment()
            self.assertEqual(os.environ["HOME"], str(actual_home_dir()))
            self.assertEqual(os.environ["WAYLAND_DISPLAY"], "wayland-9")
            self.assertEqual(os.environ["XDG_RUNTIME_DIR"], "/tmp/vice-runtime")

    def test_normalize_runtime_environment_leaves_display_unset_without_socket(self) -> None:
        runtime_dir = mock.MagicMock()
        runtime_dir.exists.return_value = True
        runtime_dir.glob.return_value = []

        with mock.patch.dict(os.environ, {"HOME": "${HOME}"}, clear=True):
            with mock.patch("vice.runtime.shutil.which", return_value=None):
                with mock.patch(
                    "vice.runtime._wayland_runtime_dir_candidates",
                    return_value=[runtime_dir],
                ):
                    normalize_runtime_environment()
            self.assertEqual(os.environ["HOME"], str(actual_home_dir()))
            self.assertNotIn("WAYLAND_DISPLAY", os.environ)
            self.assertNotIn("DISPLAY", os.environ)
            self.assertEqual(os.environ["XDG_RUNTIME_DIR"], f"/run/user/{os.getuid()}")

    def test_normalize_runtime_environment_repairs_runtime_dir_without_overwriting_valid_display(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "HOME": "${HOME}",
                "WAYLAND_DISPLAY": "wayland-3",
                "DISPLAY": ":1",
                "XDG_RUNTIME_DIR": "/run/user/$(id -u)",
            },
            clear=True,
        ):
            with mock.patch("vice.runtime.shutil.which", return_value=None):
                normalize_runtime_environment()
                self.assertEqual(os.environ["WAYLAND_DISPLAY"], "wayland-3")
                self.assertEqual(os.environ["DISPLAY"], ":1")
                self.assertEqual(os.environ["XDG_RUNTIME_DIR"], f"/run/user/{os.getuid()}")

    def test_normalize_runtime_environment_logs_before_and_after_snapshots(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": "${HOME}"}, clear=True):
            with mock.patch("vice.runtime.shutil.which", return_value=None):
                with mock.patch("vice.runtime.log.debug") as debug_mock:
                    normalize_runtime_environment()

        debug_mock.assert_any_call("Runtime env before normalization: %s", mock.ANY)
        debug_mock.assert_any_call("Runtime env after normalization: %s", mock.ANY)


class AppStartupTests(unittest.TestCase):
    def test_start_daemon_passes_normalized_environment_to_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "vice.sock"
            with mock.patch.object(app_mod, "SOCKET_FILE", socket_path):
                with mock.patch.dict(os.environ, {}, clear=True):
                    with mock.patch("vice.app._vice_cmd", return_value=["vice"]):
                        with mock.patch(
                            "vice.app.normalize_runtime_environment",
                            side_effect=lambda: os.environ.__setitem__("WAYLAND_DISPLAY", "wayland-7"),
                        ) as normalize_mock:
                            with mock.patch("vice.app.subprocess.Popen") as popen_mock:
                                app_mod._start_daemon()

        normalize_mock.assert_called_once()
        self.assertEqual(popen_mock.call_args.kwargs["env"]["WAYLAND_DISPLAY"], "wayland-7")

    def test_ensure_server_reuses_healthy_daemon_url(self) -> None:
        with mock.patch(
            "vice.app._daemon_status",
            return_value={"local_url": "http://127.0.0.1:9001", "ready": True},
        ):
            with mock.patch("vice.app._wait_for_server", return_value=True) as wait_mock:
                with mock.patch("vice.app._start_daemon") as start_mock:
                    url = app_mod._ensure_server("http://localhost:8765/")

        self.assertEqual(url, "http://127.0.0.1:9001/")
        self.assertEqual(wait_mock.call_args_list[0], mock.call("http://127.0.0.1:9001/", timeout=2.0))
        start_mock.assert_not_called()

    def test_ensure_server_waits_for_ready_daemon_after_http_responds(self) -> None:
        statuses = [
            None,
            {"local_url": "http://127.0.0.1:8765", "ready": False},
            {"local_url": "http://127.0.0.1:8765", "ready": True},
        ]
        with mock.patch("vice.app._daemon_status", side_effect=statuses):
            with mock.patch("vice.app._wait_for_server", return_value=True):
                with mock.patch("vice.app._start_daemon") as start_mock:
                    url = app_mod._ensure_server("http://127.0.0.1:8765/", startup_timeout=1.0)

        self.assertEqual(url, "http://127.0.0.1:8765/")
        start_mock.assert_called_once()

    def test_app_main_uses_ipv4_loopback_url(self) -> None:
        fake_cfg = mock.Mock()
        fake_cfg.sharing.port = 8765
        with mock.patch("vice.app.normalize_runtime_environment"):
            with mock.patch("vice.app._setup_logging"):
                with mock.patch("vice.app.signal.signal"):
                    with mock.patch("vice.config.load", return_value=fake_cfg):
                        with mock.patch("vice.app._ensure_server", return_value=None) as ensure_mock:
                            with mock.patch("vice.app._startup_failure_detail", return_value="detail") as detail_mock:
                                with mock.patch("vice.app._show_error"):
                                    with self.assertRaises(SystemExit):
                                        app_mod.main()

        ensure_mock.assert_called_once_with("http://127.0.0.1:8765/")
        detail_mock.assert_called_once_with("http://127.0.0.1:8765/")

    def test_ensure_server_restarts_when_ipc_is_alive_but_http_is_dead(self) -> None:
        with mock.patch("vice.app._daemon_status", side_effect=[
            {"local_url": "http://127.0.0.1:9001"},
            {"local_url": "http://127.0.0.1:8765", "ready": True},
        ]):
            with mock.patch("vice.app._wait_for_server", side_effect=[False, True]) as wait_mock:
                with mock.patch("vice.app._stop_daemon") as stop_mock:
                    with mock.patch("vice.app._wait_for_daemon_exit", return_value=True):
                        with mock.patch("vice.app._clear_stale_socket") as clear_mock:
                            with mock.patch("vice.app._start_daemon") as start_mock:
                                with mock.patch("vice.app.time.sleep"):
                                    url = app_mod._ensure_server("http://127.0.0.1:8765/")

        self.assertEqual(url, "http://127.0.0.1:8765/")
        stop_mock.assert_called_once()
        clear_mock.assert_called_once()
        start_mock.assert_called_once()
        self.assertEqual(wait_mock.call_args_list[0].kwargs["timeout"], 2.0)
        self.assertEqual(wait_mock.call_args_list[1].kwargs["timeout"], 1.0)

    def test_startup_failure_detail_includes_daemon_log_tail_when_ipc_never_appears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon_log = Path(tmp) / "vice.log"
            daemon_log.write_text("first line\nsecond line\n")
            with mock.patch.object(app_mod, "DAEMON_LOG_FILE", daemon_log):
                with mock.patch("vice.app._daemon_status", return_value=None):
                    detail = app_mod._startup_failure_detail("http://localhost:8765/")

        self.assertIn("Daemon IPC socket did not become ready.", detail)
        self.assertIn("second line", detail)

    def test_startup_failure_detail_reports_http_outage_when_ipc_is_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon_log = Path(tmp) / "vice.log"
            daemon_log.write_text("backend failed\n")
            with mock.patch.object(app_mod, "DAEMON_LOG_FILE", daemon_log):
                with mock.patch(
                    "vice.app._daemon_status",
                    return_value={"local_url": "http://127.0.0.1:9001"},
                ):
                    detail = app_mod._startup_failure_detail("http://localhost:8765/")

        self.assertIn("Daemon IPC responded but HTTP UI is unavailable at http://127.0.0.1:9001/", detail)
        self.assertIn("backend failed", detail)


class ConfigPathResolutionTests(unittest.TestCase):
    def test_default_config_enables_discord_rich_presence(self) -> None:
        self.assertTrue(Config().discord.enabled)

    def test_load_expands_home_placeholders_in_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".config" / "vice"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"
            config_path.write_text('[output]\ndirectory = "$HOME/Videos/Vice"\n')

            with mock.patch.object(config_mod, "CONFIG_DIR", config_dir):
                with mock.patch.object(config_mod, "CONFIG_PATH", config_path):
                    cfg = config_mod.load()

        self.assertEqual(cfg.output.directory, str(actual_home_dir() / "Videos" / "Vice"))

    def test_load_preserves_existing_discord_disabled_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".config" / "vice"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"
            config_path.write_text("[discord]\nenabled = false\n")

            with mock.patch.object(config_mod, "CONFIG_DIR", config_dir):
                with mock.patch.object(config_mod, "CONFIG_PATH", config_path):
                    cfg = config_mod.load()

        self.assertFalse(cfg.discord.enabled)

    def test_save_and_load_preserve_recording_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".config" / "vice"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"

            cfg = Config(
                recording=RecordingConfig(
                    capture_microphone=True,
                    wf_microphone_strategy="backend_fallback",
                    display="DP-1",
                    gsr_audio_source="app:firefox",
                )
            )

            with mock.patch.object(config_mod, "CONFIG_DIR", config_dir):
                with mock.patch.object(config_mod, "CONFIG_PATH", config_path):
                    config_mod.save(cfg)
                    loaded = config_mod.load()

        self.assertTrue(loaded.recording.capture_microphone)
        self.assertEqual(loaded.recording.wf_microphone_strategy, "backend_fallback")
        self.assertEqual(loaded.recording.display, "DP-1")
        self.assertEqual(loaded.recording.gsr_audio_source, "app:firefox")

    def test_save_and_load_preserve_clip_presets_and_grow_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".config" / "vice"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"

            cfg = Config(
                recording=RecordingConfig(buffer_duration=60, clip_duration=15),
                hotkeys=HotkeyConfig(
                    clip="KEY_F9",
                    clip_presets=[HotkeyClipPreset(key="KEY_F6", duration=120)],
                ),
            )

            with mock.patch.object(config_mod, "CONFIG_DIR", config_dir):
                with mock.patch.object(config_mod, "CONFIG_PATH", config_path):
                    config_mod.save(cfg)
                    loaded = config_mod.load()

        self.assertEqual(loaded.hotkeys.clip_presets[0].key, "KEY_F6")
        self.assertEqual(loaded.hotkeys.clip_presets[0].duration, 120)
        self.assertEqual(loaded.recording.buffer_duration, 120)

    def test_load_ignores_malformed_clip_presets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".config" / "vice"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"
            config_path.write_text(
                "[hotkeys]\n"
                "clip = \"KEY_F9\"\n"
                "[[hotkeys.clip_presets]]\n"
                "key = \"\"\n"
                "duration = 60\n"
                "[[hotkeys.clip_presets]]\n"
                "key = \"KEY_F6\"\n"
                "duration = 90\n"
            )

            with mock.patch.object(config_mod, "CONFIG_DIR", config_dir):
                with mock.patch.object(config_mod, "CONFIG_PATH", config_path):
                    loaded = config_mod.load()

        self.assertEqual(len(loaded.hotkeys.clip_presets), 1)
        self.assertEqual(loaded.hotkeys.clip_presets[0].key, "KEY_F6")


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class ShareServerPathResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_preloads_clips_from_resolved_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "clips"
            output_dir.mkdir()
            clip_path = output_dir / "clip.mp4"
            clip_path.write_bytes(b"not-a-real-mp4")

            local_port = _free_port()
            public_port = _free_port()
            while public_port == local_port:
                public_port = _free_port()
            cfg = Config(
                output=OutputConfig(directory="$HOME/Videos/Vice"),
                sharing=SharingConfig(
                    port=local_port,
                    public_port=public_port,
                    cloudflare_tunnel=False,
                ),
            )
            server = ShareServer(cfg)

            with mock.patch("vice.share.resolve_path", return_value=output_dir):
                with mock.patch("vice.share._local_ip", return_value="127.0.0.1"):
                    with mock.patch("vice.share._ffprobe", new=_stub_ffprobe):
                        await server.start()
                        try:
                            self.assertIn("clip", server._clips)
                        finally:
                            await server.stop()


class RecorderEnvironmentTests(unittest.TestCase):
    def test_is_wayland_delegates_to_runtime_recovery(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("vice.recorder.recover_wayland_display", return_value=True) as recover_mock:
                self.assertTrue(_is_wayland())
        recover_mock.assert_called_once()


class _FakeHotkeys:
    available = True

    def __init__(self) -> None:
        self.single: dict[str, list] = {}
        self.double: dict[str, list] = {}

    def clear_bindings(self) -> None:
        self.single.clear()
        self.double.clear()

    def on(self, key, callback) -> None:
        self.single.setdefault(key, []).append(callback)

    def on_double(self, key, callback) -> None:
        self.double.setdefault(key, []).append(callback)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class _FakeRecorder:
    def __init__(self, result=None) -> None:
        self.name = "fake"
        self._result = result
        self.save_calls = 0
        self.save_durations: list[int | None] = []
        self._cb = None

    def on_clip_saved(self, cb) -> None:
        self._cb = cb

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def save_clip(self, duration=None):
        self.save_calls += 1
        self.save_durations.append(duration)
        await asyncio.sleep(0)
        return self._result

    def session_elapsed(self) -> float:
        return 0.0


class _FakeShare:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def broadcast(self, msg: dict) -> None:
        self.messages.append(msg)


class ViceDaemonClipFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_clip_trigger_broadcasts_progress_and_error(self) -> None:
        recorder = _FakeRecorder(result=None)
        with mock.patch("vice.main.load_config", return_value=Config()):
            with mock.patch("vice.main.create_recorder", return_value=recorder):
                with mock.patch("vice.main.HotkeyListener", return_value=_FakeHotkeys()):
                    with mock.patch("vice.main.can_access_hotkeys", return_value=True):
                        daemon = main_mod.ViceDaemon()

        daemon.share = _FakeShare()

        with mock.patch("vice.main.audio.play_clip"):
            await daemon._handle_clip_hotkey()
            self.assertIsNotNone(daemon._clip_task)
            await daemon._clip_task

        self.assertEqual(recorder.save_calls, 1)
        self.assertEqual(
            [msg["type"] for msg in daemon.share.messages],
            ["clip_saving", "clip_error"],
        )

    async def test_clip_trigger_passes_requested_duration(self) -> None:
        recorder = _FakeRecorder(result=None)
        with mock.patch("vice.main.load_config", return_value=Config()):
            with mock.patch("vice.main.create_recorder", return_value=recorder):
                with mock.patch("vice.main.HotkeyListener", return_value=_FakeHotkeys()):
                    with mock.patch("vice.main.can_access_hotkeys", return_value=True):
                        daemon = main_mod.ViceDaemon()

        with mock.patch("vice.main.audio.play_clip"):
            await daemon._handle_clip_hotkey(90)
            await daemon._clip_task

        self.assertEqual(recorder.save_durations, [90])

    async def test_bind_hotkeys_registers_primary_and_preset_keys(self) -> None:
        hotkeys = _FakeHotkeys()
        cfg = Config(
            recording=RecordingConfig(clip_duration=15),
            hotkeys=HotkeyConfig(
                clip="KEY_F9",
                clip_presets=[HotkeyClipPreset(key="KEY_F6", duration=60)],
            ),
        )
        with mock.patch("vice.main.load_config", return_value=cfg):
            with mock.patch("vice.main.create_recorder", return_value=_FakeRecorder()):
                with mock.patch("vice.main.HotkeyListener", return_value=hotkeys):
                    with mock.patch("vice.main.can_access_hotkeys", return_value=True):
                        daemon = main_mod.ViceDaemon()

        daemon._bind_hotkeys()

        self.assertEqual(set(hotkeys.single), {"KEY_F9", "KEY_F6"})
        self.assertEqual(set(hotkeys.double), {"KEY_F9", "KEY_F6"})


class RecorderDurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_segment_save_clip_uses_requested_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seg = root / "seg0001.mp4"
            seg.write_bytes(b"segment")
            out = root / "out.mp4"
            recorder = SegmentRecorder(
                Config(
                    output=OutputConfig(directory=str(root)),
                    recording=RecordingConfig(clip_duration=15),
                ),
                use_wf_recorder=False,
            )
            recorder._segments = [(900.0, seg)]
            captured: dict[str, list[str]] = {}

            class _Proc:
                returncode = 0

                async def communicate(self):
                    out.write_bytes(b"clip")
                    return b"", b""

            async def _fake_exec(*cmd, **_kwargs):
                captured["cmd"] = list(cmd)
                return _Proc()

            with mock.patch("vice.recorder.time.time", return_value=1000.0):
                with mock.patch("vice.recorder._next_clip_path", return_value=out):
                    with mock.patch("vice.recorder.asyncio.create_subprocess_exec", new=_fake_exec):
                        saved = await recorder.save_clip(45)

        self.assertEqual(saved, out)
        self.assertEqual(captured["cmd"][captured["cmd"].index("-t") + 1], "45")


class RecorderStabilizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_for_finalized_clip_waits_for_last_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "raw.mp4"

            async def _writer() -> None:
                await asyncio.sleep(0.02)
                clip.write_bytes(b"a")
                await asyncio.sleep(0.08)
                clip.write_bytes(b"ab")
                await asyncio.sleep(0.08)
                clip.write_bytes(b"abc")

            observed: list[bytes] = []

            async def _fake_duration(path: Path) -> float:
                observed.append(path.read_bytes())
                return 30.0

            writer = asyncio.create_task(_writer())
            start = time.monotonic()
            with mock.patch("vice.recorder._get_duration", new=_fake_duration):
                ready = await _wait_for_finalized_clip(
                    clip,
                    stable_polls=3,
                    poll_interval=0.03,
                    inactivity_timeout=1.0,
                    max_wait=5.0,
                )
            elapsed = time.monotonic() - start
            await writer

        self.assertTrue(ready)
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertEqual(observed[-1], b"abc")

    async def test_wait_for_finalized_clip_gives_up_after_write_inactivity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "raw.mp4"
            clip.write_bytes(b"not a video")

            async def _zero_duration(_: Path) -> float:
                return 0.0

            start = time.monotonic()
            with mock.patch("vice.recorder._get_duration", new=_zero_duration):
                ready = await _wait_for_finalized_clip(
                    clip,
                    stable_polls=2,
                    poll_interval=0.02,
                    inactivity_timeout=0.15,
                    max_wait=5.0,
                )
            elapsed = time.monotonic() - start

        self.assertFalse(ready)
        self.assertLess(elapsed, 2.0)

    async def test_wait_for_finalized_clip_tolerates_slow_writes(self) -> None:
        """A file that keeps growing must not be abandoned, even when the
        total write time exceeds the inactivity timeout."""
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "raw.mp4"

            async def _writer() -> None:
                # Total write time (0.8 s) far exceeds the inactivity
                # timeout (0.3 s); only the gaps between writes count.
                data = b""
                for _ in range(16):
                    data += b"x" * 10
                    clip.write_bytes(data)
                    await asyncio.sleep(0.05)

            durations = iter([0.0] * 2)

            async def _fake_duration(_: Path) -> float:
                return next(durations, 30.0)

            writer = asyncio.create_task(_writer())
            with mock.patch("vice.recorder._get_duration", new=_fake_duration):
                ready = await _wait_for_finalized_clip(
                    clip,
                    stable_polls=2,
                    poll_interval=0.03,
                    inactivity_timeout=0.3,
                    max_wait=5.0,
                )
            await writer

        self.assertTrue(ready)

    async def test_trim_copy_command_avoids_negative_timestamps(self) -> None:
        captured: dict = {}

        async def _fake_duration(_: Path) -> float:
            return 100.0

        class _Proc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def _fake_exec(*cmd, **_kwargs):
            captured["cmd"] = list(cmd)
            return _Proc()

        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"clip")
            with mock.patch("vice.recorder._get_duration", new=_fake_duration):
                with mock.patch(
                    "vice.recorder.asyncio.create_subprocess_exec", new=_fake_exec
                ):
                    from vice.recorder import _trim_to_last_n_seconds

                    await _trim_to_last_n_seconds(clip, 30)

        cmd = captured["cmd"]
        self.assertIn("-avoid_negative_ts", cmd)
        self.assertEqual(cmd[cmd.index("-avoid_negative_ts") + 1], "make_zero")
        self.assertIn("copy", cmd)

    async def test_gsr_save_clip_waits_for_finalized_file_before_trim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            recorder = GSRRecorder(
                Config(
                    output=OutputConfig(directory=str(out_dir)),
                    recording=RecordingConfig(clip_duration=30),
                )
            )
            recorder._seen_files = set()
            recorder._proc = mock.Mock(pid=1234, returncode=None)

            raw_clip = out_dir / "gsr-auto.mp4"

            async def _writer() -> None:
                await asyncio.sleep(0.05)
                raw_clip.write_bytes(b"clip")

            async def _trim(path: Path, seconds: int) -> Path:
                self.assertEqual(seconds, 45)
                return path

            writer = asyncio.create_task(_writer())
            with mock.patch("vice.recorder.os.kill") as kill_mock:
                with mock.patch("vice.recorder._wait_for_finalized_clip", new=mock.AsyncMock(return_value=True)) as wait_mock:
                    with mock.patch("vice.recorder._trim_to_last_n_seconds", new=_trim):
                        saved = await recorder.save_clip(45)
            await writer

        kill_mock.assert_called_once_with(1234, mock.ANY)
        wait_mock.assert_awaited_once()
        self.assertIsNotNone(saved)
        self.assertEqual(saved.name, "Vice_Clip_1.mp4")


class RecorderAudioCommandTests(unittest.TestCase):
    def test_list_display_options_parses_gsr_capture_options(self) -> None:
        gsr_out = "\n".join(
            [
                "window",
                "screen",
                "DP-1|2560x1440",
                "HDMI-A-1|1920x1080",
            ]
        )

        with mock.patch("vice.recorder._has", side_effect=lambda tool: tool == "gpu-screen-recorder"):
            with mock.patch("vice.recorder._run_command_capture", return_value=(0, gsr_out)):
                info = list_display_options("gsr")

        self.assertEqual(info["backend"], "gsr")
        self.assertEqual([d["id"] for d in info["displays"]], ["DP-1", "HDMI-A-1"])
        self.assertEqual(info["displays"][0]["label"], "DP-1 (2560x1440)")

    def test_list_display_options_parses_quoted_gsr_monitors(self) -> None:
        gsr_out = "\n".join(
            [
                '"DP-4" (1920x1080+1920+0)',
                '"HDMI-A-1" (1920x1080+0+0)',
            ]
        )

        with mock.patch("vice.recorder._has", side_effect=lambda tool: tool == "gpu-screen-recorder"):
            with mock.patch("vice.recorder._run_command_capture", return_value=(0, gsr_out)):
                info = list_display_options("gsr")

        self.assertEqual(info["backend"], "gsr")
        self.assertEqual([d["id"] for d in info["displays"]], ["DP-4", "HDMI-A-1"])
        self.assertEqual(info["displays"][0]["label"], "DP-4 (1920x1080+1920+0)")

    def test_list_display_options_parses_xrandr_monitors(self) -> None:
        xrandr_out = "\n".join(
            [
                "Monitors: 2",
                " 0: +*DP-1 2560/600x1440/340+1920+0  DP-1",
                " 1: +HDMI-1 1920/520x1080/290+0+0  HDMI-1",
            ]
        )

        with mock.patch("vice.recorder._has", side_effect=lambda tool: tool == "xrandr"):
            with mock.patch("vice.recorder.subprocess.check_output", return_value=xrandr_out):
                info = list_display_options("ffmpeg")

        self.assertEqual(info["backend"], "ffmpeg")
        self.assertEqual([d["id"] for d in info["displays"]], ["DP-1", "HDMI-1"])
        self.assertEqual(info["displays"][0]["x"], 1920)
        self.assertEqual(info["displays"][0]["width"], 2560)

    def test_gsr_build_cmd_includes_desktop_and_microphone_audio(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(
                    capture_audio=True,
                    capture_microphone=True,
                ),
            )
        )

        cmd = recorder._build_cmd()

        self.assertIn("-a", cmd)
        idx = cmd.index("-a")
        self.assertEqual(cmd[idx + 1], "default_output|default_input")

    def test_gsr_build_cmd_uses_selected_audio_source(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(
                    capture_audio=True,
                    gsr_audio_source="app:firefox",
                ),
            )
        )

        cmd = recorder._build_cmd()

        self.assertEqual(cmd[cmd.index("-a") + 1], "app:firefox")

    def test_gsr_build_cmd_combines_selected_audio_source_with_microphone(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(
                    capture_audio=True,
                    capture_microphone=True,
                    gsr_audio_source="device:game.monitor",
                ),
            )
        )

        cmd = recorder._build_cmd()

        self.assertEqual(cmd[cmd.index("-a") + 1], "device:game.monitor|default_input")

    def test_gsr_build_cmd_passes_configured_resolution(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(resolution="1280x720"),
            )
        )

        cmd = recorder._build_cmd()

        self.assertEqual(cmd[cmd.index("-s") + 1], "1280x720")

    def test_gsr_build_cmd_ignores_invalid_resolution(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(resolution="720p"),
            )
        )

        cmd = recorder._build_cmd()

        self.assertNotIn("-s", cmd)

    def test_gsr_build_cmd_keeps_user_resolution_override(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(
                    resolution="1280x720",
                    gsr_args="-s 640x360",
                ),
            )
        )

        cmd = recorder._build_cmd()

        self.assertEqual(cmd.count("-s"), 1)
        self.assertEqual(cmd[cmd.index("-s") + 1], "640x360")

    def test_gsr_session_cmd_passes_configured_resolution(self) -> None:
        rc = RecordingConfig(resolution="1920x1080")

        cmd = GSRRecorder._gsr_session_cmd(Path("/tmp/vice-test/out.mp4"), rc)

        self.assertEqual(cmd[cmd.index("-s") + 1], "1920x1080")

    def test_gsr_build_cmd_maps_hevc_encoder_to_gsr_codec(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(encoder="hevc_vaapi"),
            )
        )

        cmd = recorder._build_cmd()

        self.assertEqual(cmd[cmd.index("-k") + 1], "hevc")

    def test_gsr_build_cmd_respects_user_codec_arg(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(encoder="hevc_vaapi", gsr_args="-k av1"),
            )
        )

        cmd = recorder._build_cmd()

        self.assertEqual(cmd.count("-k"), 1)
        self.assertEqual(cmd[cmd.index("-k") + 1], "av1")

    def test_gsr_session_cmd_maps_hevc_encoder_to_gsr_codec(self) -> None:
        cmd = GSRRecorder._gsr_session_cmd(
            Path("/tmp/session.mp4"),
            RecordingConfig(encoder="hevc_nvenc"),
        )

        self.assertEqual(cmd[cmd.index("-k") + 1], "hevc")

    def test_hevc_vaapi_encoder_flags(self) -> None:
        flags = _encoder_flags("hevc_vaapi", 23)

        self.assertIn("-c:v", flags)
        self.assertEqual(flags[flags.index("-c:v") + 1], "hevc_vaapi")

    def test_list_gsr_audio_sources_parses_devices_and_apps(self) -> None:
        def fake_run(cmd, timeout=5.0):
            if "--list-audio-devices" in cmd:
                return 0, "alsa_output.game.monitor\n"
            if "--list-application-audio" in cmd:
                return 0, "Firefox\nDiscord\n"
            return 1, ""

        with mock.patch("vice.recorder._has", side_effect=lambda tool: tool == "gpu-screen-recorder"):
            with mock.patch("vice.recorder._run_command_capture", side_effect=fake_run):
                payload = list_gsr_audio_sources()

        ids = [source["id"] for source in payload["sources"]]
        self.assertIn("default_output", ids)
        self.assertIn("device:alsa_output.game.monitor", ids)
        self.assertIn("app:Firefox", ids)
        self.assertIn("app-inverse:Firefox", ids)

    def test_gsr_build_cmd_defaults_to_screen_on_x11(self) -> None:
        recorder = GSRRecorder(
            Config(output=OutputConfig(directory="/tmp/vice-test"))
        )

        with mock.patch("vice.recorder._is_wayland", return_value=False):
            with mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=False):
                cmd = recorder._build_cmd()

        self.assertIn("-w", cmd)
        self.assertEqual(cmd[cmd.index("-w") + 1], "screen")

    def test_gsr_build_cmd_uses_selected_display(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(display="DP-1"),
            )
        )

        with mock.patch("vice.recorder._display_options", return_value=[{"id": "DP-1", "label": "DP-1"}]):
            cmd = recorder._build_cmd()

        self.assertIn("-w", cmd)
        self.assertEqual(cmd[cmd.index("-w") + 1], "DP-1")

    def test_gsr_build_cmd_accepts_legacy_pipe_form_display_value(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(display="DP-1|2560x1440"),
            )
        )

        with mock.patch(
            "vice.recorder._display_options",
            return_value=[{"id": "DP-1", "label": "DP-1 (2560x1440)"}],
        ):
            cmd = recorder._build_cmd()

        self.assertIn("-w", cmd)
        self.assertEqual(cmd[cmd.index("-w") + 1], "DP-1")

    def test_ffmpeg_segment_cmd_mixes_desktop_and_microphone_audio(self) -> None:
        recorder = SegmentRecorder(
            Config(
                recording=RecordingConfig(
                    capture_audio=True,
                    capture_microphone=True,
                )
            ),
            use_wf_recorder=False,
        )

        with mock.patch("vice.recorder._desktop_audio_source", return_value="desk.monitor"):
            with mock.patch("vice.recorder._microphone_audio_source", return_value="mic.input"):
                cmd = recorder._ffmpeg_x11_cmd(Path("/tmp/out.mp4"))

        self.assertIn("desk.monitor", cmd)
        self.assertIn("mic.input", cmd)
        self.assertIn("-filter_complex", cmd)
        self.assertIn("[1:a][2:a]amix=inputs=2:normalize=0[aout]", cmd)

    def test_ffmpeg_segment_cmd_uses_selected_monitor_geometry(self) -> None:
        recorder = SegmentRecorder(
            Config(
                recording=RecordingConfig(
                    display="DP-1",
                    resolution="1920x1080",
                )
            ),
            use_wf_recorder=False,
        )

        displays = [{"id": "DP-1", "label": "DP-1", "width": 2560, "height": 1440, "x": 1920, "y": 0}]
        with mock.patch("vice.recorder._display_options", return_value=displays):
            cmd = recorder._ffmpeg_x11_cmd(Path("/tmp/out.mp4"))

        self.assertIn("-video_size", cmd)
        self.assertEqual(cmd[cmd.index("-video_size") + 1], "2560x1440")
        self.assertIn("-i", cmd)
        self.assertEqual(cmd[cmd.index("-i") + 1], ":0+1920,0")
        self.assertIn("-vf", cmd)
        self.assertIn("scale=1920:1080", cmd[cmd.index("-vf") + 1])

    def test_wf_recorder_uses_microphone_only_strategy(self) -> None:
        recorder = SegmentRecorder(
            Config(
                recording=RecordingConfig(
                    capture_audio=True,
                    capture_microphone=True,
                    wf_microphone_strategy="mic_only",
                )
            ),
            use_wf_recorder=True,
        )

        with mock.patch("vice.recorder._microphone_audio_source", return_value="mic.input"):
            cmd = recorder._wf_recorder_cmd(Path("/tmp/out.mp4"))

        self.assertIn("--audio=mic.input", cmd)

    def test_wf_recorder_uses_selected_display(self) -> None:
        recorder = SegmentRecorder(
            Config(recording=RecordingConfig(display="DP-1")),
            use_wf_recorder=True,
        )

        with mock.patch("vice.recorder._display_options", return_value=[{"id": "DP-1", "label": "DP-1"}]):
            with mock.patch("vice.recorder._wf_supports_flag", return_value=False):
                cmd = recorder._wf_recorder_cmd(Path("/tmp/out.mp4"))

        self.assertIn("-o", cmd)
        self.assertEqual(cmd[cmd.index("-o") + 1], "DP-1")
        self.assertNotIn("--force-yuv", cmd)

    def test_wf_recorder_includes_force_yuv_when_supported(self) -> None:
        recorder = SegmentRecorder(
            Config(recording=RecordingConfig()),
            use_wf_recorder=True,
        )

        with mock.patch("vice.recorder._wf_supports_flag", return_value=True):
            cmd = recorder._wf_recorder_cmd(Path("/tmp/out.mp4"))

        self.assertIn("--force-yuv", cmd)

    def test_list_display_options_warns_for_legacy_wf_recorder_listing(self) -> None:
        proc = subprocess.CompletedProcess(
            ["wf-recorder", "-L"],
            1,
            "",
            "wf-recorder: invalid option -- 'L'\nUnsupported command line argument (null)\n",
        )

        with mock.patch("vice.recorder._has", side_effect=lambda tool: tool == "wf-recorder"):
            with mock.patch("vice.recorder.subprocess.run", return_value=proc):
                info = list_display_options("wf-recorder")

        self.assertEqual(info["backend"], "wf-recorder")
        self.assertEqual(info["displays"], [])
        self.assertEqual(
            info["warning"],
            "installed wf-recorder does not support output listing (-L)",
        )

    def test_create_recorder_uses_compat_backend_for_wf_microphone_mode(self) -> None:
        cfg = Config(
            recording=RecordingConfig(
                backend="wf-recorder",
                capture_audio=True,
                capture_microphone=True,
                wf_microphone_strategy="backend_fallback",
            )
        )

        with mock.patch("vice.recorder._has") as has_mock:
            with mock.patch("vice.recorder._is_wayland", return_value=True):
                with mock.patch("vice.recorder._is_x11", return_value=False):
                    has_mock.side_effect = lambda tool: tool == "gpu-screen-recorder"
                    recorder = create_recorder(cfg)

        self.assertIsInstance(recorder, GSRRecorder)

    def test_create_recorder_rejects_wf_microphone_prompt_mode(self) -> None:
        cfg = Config(
            recording=RecordingConfig(
                backend="wf-recorder",
                capture_audio=True,
                capture_microphone=True,
                wf_microphone_strategy="prompt",
            )
        )

        with mock.patch("vice.recorder._has", side_effect=lambda tool: tool == "wf-recorder"):
            with mock.patch("vice.recorder._is_wayland", return_value=True):
                with mock.patch("vice.recorder._is_x11", return_value=False):
                    with self.assertRaises(RuntimeError):
                        create_recorder(cfg)

    def test_create_recorder_reports_missing_wayland_backend(self) -> None:
        cfg = Config(recording=RecordingConfig(backend="auto"))

        with mock.patch("vice.recorder._has", return_value=False):
            with mock.patch("vice.recorder._is_wayland", return_value=True):
                with mock.patch("vice.recorder._is_x11", return_value=False):
                    with self.assertRaises(RuntimeError) as ctx:
                        create_recorder(cfg)

        self.assertIn("gpu-screen-recorder is required", str(ctx.exception))
        self.assertIn("recording.backend", str(ctx.exception))


class _FakeStream:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class _FakeProcess:
    def __init__(self, returncode: int, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stderr = _FakeStream(stderr)

    async def wait(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        return None


class RecorderSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_gsr_start_raises_when_process_exits_immediately(self) -> None:
        recorder = GSRRecorder(
            Config(output=OutputConfig(directory="/tmp/vice-test"))
        )
        proc = _FakeProcess(
            2,
            b"gpu-screen-recorder: invalid capture target DP-1|2560x1440\n",
        )

        with mock.patch(
            "vice.recorder.asyncio.create_subprocess_exec",
            new=mock.AsyncMock(return_value=proc),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await recorder.start()

        self.assertIn("gpu-screen-recorder failed to start", str(ctx.exception))

    async def test_gsr_start_reports_error_line_instead_of_monitor_listing(self) -> None:
        recorder = GSRRecorder(
            Config(output=OutputConfig(directory="/tmp/vice-test"))
        )
        proc = _FakeProcess(
            2,
            b"gpu-screen-recorder: invalid capture target :0\n"
            b"Available monitors:\n"
            b'"DP-4" (1920x1080+1920+0)\n',
        )

        with mock.patch(
            "vice.recorder.asyncio.create_subprocess_exec",
            new=mock.AsyncMock(return_value=proc),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await recorder.start()

        message = str(ctx.exception)
        self.assertIn("invalid capture target :0", message)
        self.assertNotIn('"DP-4"', message)

    async def test_segment_start_raises_when_first_segment_exits_immediately(self) -> None:
        recorder = SegmentRecorder(
            Config(output=OutputConfig(directory="/tmp/vice-test")),
            use_wf_recorder=True,
        )
        proc = _FakeProcess(
            2,
            b"wf-recorder: unknown output DP-1\n",
        )

        with mock.patch("vice.recorder._wf_supports_flag", return_value=False):
            with mock.patch(
                "vice.recorder.asyncio.create_subprocess_exec",
                new=mock.AsyncMock(return_value=proc),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    await recorder.start()

        self.assertIn("wf-recorder failed to start", str(ctx.exception))

    async def test_start_session_returns_none_when_recorder_exits_immediately(self) -> None:
        recorder = SegmentRecorder(
            Config(output=OutputConfig(directory="/tmp/vice-test")),
            use_wf_recorder=True,
        )
        proc = _FakeProcess(
            2,
            b"wf-recorder: unrecognized option '--force-yuv'\n",
        )

        with mock.patch("vice.recorder._is_wayland", return_value=True):
            with mock.patch("vice.recorder._has", side_effect=lambda tool: tool == "wf-recorder"):
                with mock.patch("vice.recorder._wf_supports_flag", return_value=False):
                    with mock.patch(
                        "vice.recorder.asyncio.create_subprocess_exec",
                        new=mock.AsyncMock(return_value=proc),
                    ):
                        path = await recorder.start_session()

        self.assertIsNone(path)
