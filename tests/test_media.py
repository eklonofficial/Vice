import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vice.media import cleanup_temp_files, get_duration, probe_media

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def _make_video(path: Path, *, fragmented: bool, duration: float = 1.0) -> None:
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=128x72:rate=10",
    ]
    if fragmented:
        # Fragmented MP4 has no per-stream duration tags — this is what
        # gpu-screen-recorder writes for replay clips.
        cmd += ["-movflags", "frag_keyframe+empty_moov"]
    cmd += ["-y", str(path)]
    subprocess.run(cmd, check=True, timeout=60)


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg/ffprobe not installed")
class ProbeMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_reads_duration_of_regular_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            _make_video(clip, fragmented=False)
            meta = await probe_media(clip)

        self.assertIsNotNone(meta)
        self.assertEqual(meta["width"], 128)
        self.assertEqual(meta["height"], 72)
        self.assertGreater(meta["duration"], 0.5)

    async def test_probe_reads_duration_of_fragmented_mp4(self) -> None:
        """Regression test for #81: fragmented MP4 has no stream duration
        tag, and probing only the stream field reported 0 for healthy
        clips — triggering bogus timeouts and a destructive remux."""
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            _make_video(clip, fragmented=True)
            meta = await probe_media(clip)
            duration = await get_duration(clip)

        self.assertIsNotNone(meta)
        self.assertGreater(meta["duration"], 0.5)
        self.assertGreater(duration, 0.5)

    async def test_probe_returns_none_for_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            junk = Path(tmp) / "junk.mp4"
            junk.write_bytes(b"this is not a video")
            meta = await probe_media(junk)

        self.assertIsNone(meta)


class CleanupTempFilesTests(unittest.TestCase):
    def test_removes_edit_leftovers_and_keeps_clips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            keep = out / "Vice_Clip_1.mp4"
            keep.write_bytes(b"clip")
            stale = [
                out / "Vice_Clip_1.trim.mp4",
                out / "Vice_Clip_2.wm.mp4",
                out / "Replay_x.mp4.fix.mp4",
            ]
            for p in stale:
                p.write_bytes(b"partial")

            cleanup_temp_files(out)

            self.assertTrue(keep.exists())
            for p in stale:
                self.assertFalse(p.exists(), p.name)

    def test_missing_directory_is_a_noop(self) -> None:
        cleanup_temp_files(Path("/nonexistent/vice-test-dir"))


class RemuxGuardTests(unittest.IsolatedAsyncioTestCase):
    """_remux_moov must never replace a clip with a worse file."""

    async def test_remux_keeps_original_when_output_is_truncated(self) -> None:
        from vice.share import _remux_moov

        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "clip.mp4"
            original.write_bytes(b"x" * 10_000)

            class _Proc:
                returncode = 0

                async def wait(self):
                    return 0

            async def _fake_exec(*cmd, **_kwargs):
                # ffmpeg "succeeds" but emits a near-empty file, like the
                # 0.02 s remuxes reported in #81.
                Path(cmd[-1]).write_bytes(b"tiny")
                return _Proc()

            async def _fake_probe(_: Path):
                return {"width": 128, "height": 72, "duration": 0.02}

            with mock.patch(
                "vice.share.asyncio.create_subprocess_exec", new=_fake_exec
            ):
                with mock.patch("vice.share.probe_media", new=_fake_probe):
                    replaced = await _remux_moov(original)

            self.assertFalse(replaced)
            self.assertEqual(original.read_bytes(), b"x" * 10_000)
            self.assertEqual(list(Path(tmp).glob("*.fix.mp4")), [])

    async def test_remux_replaces_original_when_output_is_sane(self) -> None:
        from vice.share import _remux_moov

        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "clip.mp4"
            original.write_bytes(b"x" * 10_000)

            class _Proc:
                returncode = 0

                async def wait(self):
                    return 0

            async def _fake_exec(*cmd, **_kwargs):
                Path(cmd[-1]).write_bytes(b"y" * 9_800)
                return _Proc()

            async def _fake_probe(_: Path):
                return {"width": 128, "height": 72, "duration": 14.9}

            with mock.patch(
                "vice.share.asyncio.create_subprocess_exec", new=_fake_exec
            ):
                with mock.patch("vice.share.probe_media", new=_fake_probe):
                    replaced = await _remux_moov(original)

            self.assertTrue(replaced)
            self.assertEqual(original.read_bytes(), b"y" * 9_800)


if __name__ == "__main__":
    unittest.main()
