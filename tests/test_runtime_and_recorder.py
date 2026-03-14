import asyncio
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from vice import config as config_mod
from vice.config import Config, OutputConfig, RecordingConfig, SharingConfig
from vice.recorder import GSRRecorder, SegmentRecorder, create_recorder, _wait_for_finalized_clip
from vice.runtime import actual_home_dir, normalize_runtime_environment

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
            ]
        )
        with mock.patch.dict(os.environ, {"HOME": "${HOME}"}, clear=True):
            with mock.patch("vice.runtime.shutil.which", return_value="/usr/bin/systemctl"):
                with mock.patch("vice.runtime.subprocess.check_output", return_value=systemd_env):
                    normalize_runtime_environment()
            self.assertEqual(os.environ["HOME"], str(actual_home_dir()))
            self.assertEqual(os.environ["WAYLAND_DISPLAY"], "wayland-1")
            self.assertEqual(os.environ["XDG_RUNTIME_DIR"], f"/run/user/{os.getuid()}")


class ConfigPathResolutionTests(unittest.TestCase):
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

    def test_save_and_load_preserve_microphone_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".config" / "vice"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"

            cfg = Config(
                recording=RecordingConfig(
                    capture_microphone=True,
                    wf_microphone_strategy="backend_fallback",
                )
            )

            with mock.patch.object(config_mod, "CONFIG_DIR", config_dir):
                with mock.patch.object(config_mod, "CONFIG_PATH", config_path):
                    config_mod.save(cfg)
                    loaded = config_mod.load()

        self.assertTrue(loaded.recording.capture_microphone)
        self.assertEqual(loaded.recording.wf_microphone_strategy, "backend_fallback")


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
                    timeout=1.0,
                )
            elapsed = time.monotonic() - start
            await writer

        self.assertTrue(ready)
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertEqual(observed[-1], b"abc")

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
                self.assertEqual(seconds, 30)
                return path

            writer = asyncio.create_task(_writer())
            with mock.patch("vice.recorder.os.kill") as kill_mock:
                with mock.patch("vice.recorder._wait_for_finalized_clip", new=mock.AsyncMock(return_value=True)) as wait_mock:
                    with mock.patch("vice.recorder._trim_to_last_n_seconds", new=_trim):
                        saved = await recorder.save_clip()
            await writer

        kill_mock.assert_called_once_with(1234, mock.ANY)
        wait_mock.assert_awaited_once()
        self.assertIsNotNone(saved)
        self.assertEqual(saved.name, "Vice_Clip_1.mp4")


class RecorderAudioCommandTests(unittest.TestCase):
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
