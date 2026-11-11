import asyncio
import json
import sys
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import _sandbox  # noqa: F401  keeps every store out of the real home
except ModuleNotFoundError:
    from tests import _sandbox  # noqa: F401

from vice import keys


class _KeysDir(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(keys, "KEYS_DIR", Path(tmp.name) / "keys")
        patcher.start()
        self.addCleanup(patcher.stop)


class KeyRingTests(unittest.TestCase):
    def test_only_layout_keys_are_kept(self):
        """#209: typing in a chat box must not be recoverable from a clip."""
        ring = keys.KeyRing(60)
        for name in ("KEY_H", "KEY_I", "KEY_ENTER", "KEY_P"):
            self.assertIsNone(ring.record(name, True, at=1.0))
            self.assertIsNone(ring.record(name, False, at=1.1))
        self.assertEqual(ring.intervals(0, 10), [])

    def test_both_shifts_are_one_key(self):
        ring = keys.KeyRing(60)
        self.assertEqual(ring.record("KEY_LEFTSHIFT", True, at=1.0), ("SHIFT", True))
        self.assertIsNone(ring.record("KEY_RIGHTSHIFT", True, at=1.5))
        self.assertIsNone(ring.record("KEY_LEFTSHIFT", False, at=2.0))
        self.assertEqual(ring.record("KEY_RIGHTSHIFT", False, at=3.0), ("SHIFT", False))
        self.assertEqual(ring.intervals(0, 10), [[1.0, 3.0, "SHIFT"]])

    def test_intervals_are_clamped_to_the_clip_and_rebased(self):
        ring = keys.KeyRing(60)
        ring.record("KEY_W", True, at=8.0)
        ring.record("KEY_W", False, at=12.0)
        ring.record("KEY_SPACE", True, at=13.0)
        ring.record("KEY_SPACE", False, at=13.5)
        ring.record("KEY_D", True, at=19.0)   # still held when the clip ends
        self.assertEqual(ring.intervals(10.0, 20.0),
                         [[0.0, 2.0, "W"], [3.0, 3.5, "SPACE"], [9.0, 10.0, "D"]])

    def test_the_ring_forgets_what_is_older_than_its_horizon(self):
        ring = keys.KeyRing(5)
        ring.record("KEY_W", True, at=1.0)
        ring.record("KEY_W", False, at=2.0)
        ring.record("KEY_A", True, at=10.0)
        ring.record("KEY_A", False, at=11.0)
        self.assertEqual(ring.intervals(0, 20), [[10.0, 11.0, "A"]])


class SidecarTests(_KeysDir):
    def test_sidecar_round_trips_and_is_private(self):
        keys.save_sidecar("Clip_1", [[0.0, 1.0, "W"], [0.5, 0.7, "NOPE"]])
        mode = stat.S_IMODE(os.stat(keys.sidecar_path("Clip_1")).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(keys.load_sidecar("Clip_1"), [[0.0, 1.0, "W"]])

    def test_move_and_delete_follow_the_clip(self):
        keys.save_sidecar("a", [[0, 1, "W"]])
        keys.move_sidecar("a", "b")
        self.assertIsNone(keys.load_sidecar("a"))
        self.assertEqual(keys.load_sidecar("b"), [[0, 1, "W"]])
        keys.delete_sidecar("b")
        self.assertIsNone(keys.load_sidecar("b"))
        keys.delete_sidecar("never-existed")

    def test_cut_intervals_for_a_trim(self):
        cut = keys.cut_intervals([[0.0, 2.0, "W"], [3.0, 5.0, "A"], [6.0, 7.0, "D"]], 1.0, 4.0)
        self.assertEqual(cut, [[0.0, 1.0, "W"], [2.0, 3.0, "A"]])


class OverlayDrawTests(unittest.TestCase):
    def test_every_accent_has_a_scheme_and_the_panel_draws(self):
        try:
            import cairo
        except ImportError:
            self.skipTest("pycairo not installed")
        from vice import key_overlay
        schemes = key_overlay._accents()
        self.assertEqual(set(schemes), {"blue", "purple", "green", "red", "orange"})
        for scheme in schemes.values():
            self.assertEqual(key_overlay.ramp_for(scheme["base"])["base"], scheme["base"])
        self.assertEqual(key_overlay.ramp_for("#123456")["base"], schemes["blue"]["base"])
        w, h = key_overlay.panel_size(1.0)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        key_overlay.draw(cairo.Context(surface), schemes["green"], 1.0, {"W": 1.0})

    def test_accents_json_matches_the_ui_theme(self):
        """The overlay must wear the same colours as the window."""
        import re
        root = Path(__file__).resolve().parents[1]
        ts = (root / "ui-src" / "theme" / "accents.ts").read_text()
        data = json.loads((root / "vice" / "data" / "accents.json").read_text())
        for name, scheme in data.items():
            block = ts.split(f"  {name}: {{", 1)[1].split("},", 1)[0]
            for key, value in re.findall(r"(\w+): '(#[0-9a-f]{6})'", block):
                self.assertEqual(scheme[key], value, f"{name}.{key}")


try:
    import shutil
    import subprocess
    import vice.share as _share
    from vice.config import Config
    from vice.share import ShareServer
except ModuleNotFoundError:
    ShareServer = None


@unittest.skipUnless(ShareServer is not None and shutil.which("ffmpeg"), "aiohttp or ffmpeg missing")
class SidecarLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for target, value in ((keys, "KEYS_DIR"), (_share, "HIGHLIGHTS_DIR"),
                              (_share, "THUMB_DIR"), (_share, "PROXY_DIR")):
            patcher = mock.patch.object(target, value, self.root / value.lower())
            patcher.start()
            self.addCleanup(patcher.stop)
        clips = self.root / "clips"
        clips.mkdir()
        self.src = clips / "Clip.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=160x120:rate=30:duration=6", "-c:v", "libx264",
                        "-preset", "ultrafast", "-g", "30", "-pix_fmt", "yuv420p", str(self.src)],
                       check=True, timeout=30)
        cfg = Config()
        cfg.output.directory = str(clips)
        self.server = ShareServer(cfg)
        self.server._clips["Clip"] = self.src
        keys.save_sidecar("Clip", [[0.5, 1.5, "W"], [2.0, 5.0, "A"]])

    def _req(self, slug, body):
        req = mock.MagicMock()
        req.match_info = {"slug": slug}
        req.json = mock.AsyncMock(return_value=body)
        return req

    async def _trim(self, body):
        with mock.patch.object(_share, "_available_encoders", return_value={"libx264"}), \
             mock.patch.object(_share, "_is_nvidia", return_value=False), \
             mock.patch.object(self.server, "add_clip"), \
             mock.patch.object(self.server, "_broadcast_clip", new=mock.AsyncMock()):
            return json.loads((await self.server._api_trim(self._req("Clip", body))).text)

    async def test_a_trimmed_copy_gets_the_keys_of_its_span(self):
        body = await self._trim({"start": 1.0, "end": 3.0, "copy": True})
        self.assertTrue(body["ok"], body)
        self.assertEqual(keys.load_sidecar("Clip-trim"), [[0.0, 0.5, "W"], [1.0, 2.0, "A"]])
        self.assertEqual(keys.load_sidecar("Clip"), [[0.5, 1.5, "W"], [2.0, 5.0, "A"]])

    async def test_replacing_cuts_the_clips_own_keys(self):
        body = await self._trim({"start": 1.0, "end": 3.0})
        self.assertTrue(body["ok"], body)
        self.assertEqual(keys.load_sidecar("Clip"), [[0.0, 0.5, "W"], [1.0, 2.0, "A"]])

    async def test_rename_and_delete_carry_the_keys(self):
        with mock.patch.object(self.server, "_broadcast_clip", new=mock.AsyncMock()), \
             mock.patch.object(self.server, "broadcast", new=mock.AsyncMock()):
            await self.server._api_rename(self._req("Clip", {"name": "Renamed"}))
            self.assertEqual(keys.load_sidecar("Renamed"), [[0.5, 1.5, "W"], [2.0, 5.0, "A"]])
            self.assertIsNone(keys.load_sidecar("Clip"))
            self.assertTrue(self.server._clip_json("Renamed", self.server._clips["Renamed"], {})["keys"])
            await self.server._api_delete(self._req("Renamed", {}))
        self.assertIsNone(keys.load_sidecar("Renamed"))


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg missing")
class KeyBurnExportTests(unittest.TestCase):
    """The panel burned into an editor export lights each key while it was
    held, in timeline time, from the same drawing as the live overlay."""

    def test_held_keys_light_up_in_the_export(self):
        try:
            import cairo  # noqa: F401
        except ImportError:
            self.skipTest("pycairo missing")
        from vice.editor import KeyBurn, Source, build_export_cmd, validate_project
        from vice.key_overlay import _accents, render_burn_assets
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "Clip.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            "color=c=gray:size=640x360:rate=30:duration=4",
                            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                            "-t", "4", "-c:v", "libx264", "-preset", "ultrafast",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True, timeout=30)
            sources = {"Clip": Source(src, 4.0, 640, 360, True, 1)}
            raw = {"version": 1,
                   "tracks": [{"id": "T1", "type": "text", "label": "T1"},
                              {"id": "V1", "type": "video", "label": "V1"},
                              {"id": "A1", "type": "audio", "label": "A1"}],
                   # The clip starts 1 s into the source, at 0.5 s on the timeline.
                   "items": [{"id": "i1", "kind": "clip", "trackId": "V1", "clipId": "Clip",
                              "start": 0.5, "dur": 2.5, "offset": 1.0}]}
            project, errors = validate_project(raw, sources)
            self.assertEqual(errors, [])
            scheme = _accents()["green"]
            assets = render_burn_assets(root / "keys", scheme["base"], 360 / 1080)
            # W held from 2.0 to 2.6 s of the source: 1.5 to 2.1 s of the timeline.
            burn = KeyBurn(panel=assets["panel"], size=assets["size"], keys=assets["keys"],
                           intervals={"Clip": [[2.0, 2.6, "W"]]})
            out = root / "out.mp4"
            cmd = build_export_cmd(project, sources, out, text_dir=root, key_burn=burn)
            subprocess.run(cmd, check=True, timeout=120, capture_output=True)

            _, kx, ky = assets["keys"]["W"]
            pw, ph = assets["size"]
            px, py = round(32 * 360 / 1080), 360 - ph - round(32 * 360 / 1080)
            cx, cy = px + kx + 4, py + ky + 5

            def pixel(at):
                raw = subprocess.check_output(
                    ["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(out), "-frames:v", "1",
                     "-vf", f"format=rgb24,crop=1:1:{cx}:{cy}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
                return tuple(raw[:3])

            def near(rgb, hex_colour):
                want = tuple(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
                return max(abs(a - b) for a, b in zip(rgb, want)) <= 24

            self.assertTrue(near(pixel(0.2), "#000000"), "no panel over the gap before the clip")
            self.assertTrue(near(pixel(1.0), scheme["secondaryContainer"]), pixel(1.0))
            self.assertTrue(near(pixel(1.8), scheme["base"]), pixel(1.8))
            self.assertTrue(near(pixel(2.6), scheme["secondaryContainer"]), pixel(2.6))


@unittest.skipUnless(ShareServer is not None, "aiohttp missing")
class KeyBurnForExportTests(unittest.TestCase):
    def test_only_clips_with_keys_are_burned(self):
        try:
            import cairo  # noqa: F401
        except ImportError:
            self.skipTest("pycairo missing")
        from vice.editor import Source
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(keys, "KEYS_DIR", root / "keys"):
                sources = {"A": Source(root / "A.mp4", 5, 1920, 1080, True),
                           "B": Source(root / "B.mp4", 5, 1920, 1080, True)}
                project = {"items": [], "tracks": []}
                with mock.patch.object(_share, "canvas_for", return_value=(1920, 1080, 60)):
                    self.assertIsNone(_share._key_burn_for(project, sources, root / "w", "#8ab9f3"))
                    keys.save_sidecar("B", [[0, 1, "W"]])
                    burn = _share._key_burn_for(project, sources, root / "w", "#8ab9f3")
                self.assertEqual(burn.intervals, {"B": [[0, 1, "W"]]})
                self.assertTrue(burn.panel.exists())
                self.assertEqual(len(burn.keys), 17)


_FAKE_OVERLAY = r'''
import json, sys
log = open(sys.argv[1], "a")
print(json.dumps({"t": "ready"}), flush=True)
for line in sys.stdin:
    log.write(line); log.flush()
    message = json.loads(line)
    if message.get("t") == "move" and message.get("on") is False:
        print(json.dumps({"t": "moved", "x": 222, "y": 333}), flush=True)
    if message.get("t") == "key" and message.get("k") == "CRASH":
        sys.exit(3)
'''


class KeyOverlayHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        script = root / "fake_overlay.py"
        script.write_text(_FAKE_OVERLAY)
        self.log = root / "received.jsonl"
        self.moved: list[tuple[int, int]] = []
        from vice.overlay_host import KeyOverlayHost
        self.host = KeyOverlayHost(lambda x, y: self.moved.append((x, y)), lambda: None,
                                   command=[sys.executable, str(script), str(self.log)])
        self.addAsyncCleanup(self.host.stop)
        display = mock.patch("vice.overlay_host.has_display", return_value=True)
        display.start()
        self.addCleanup(display.stop)

    def _received(self) -> list[dict]:
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    async def _settle(self) -> None:
        for _ in range(50):
            await asyncio.sleep(0.02)

    async def test_it_starts_styled_and_placed_and_forwards_keys(self):
        place = {"position": "top-right", "x": 0, "y": 0, "scale": 1.5}
        await self.host.sync(True, place, "#8ab9f3")
        self.host.key("W", True)
        await self._settle()
        got = self._received()
        self.assertEqual(got[0], {"t": "style", "accent": "#8ab9f3"})
        self.assertEqual(got[1], {"t": "place", **place})
        self.assertEqual(got[2], {"t": "key", "k": "W", "d": True})
        self.assertTrue(self.host.state()["running"])

    async def test_a_finished_drag_is_reported(self):
        await self.host.sync(True, {"position": "bottom-left"}, "")
        self.host.set_moving(True)
        self.host.set_moving(False)
        await self._settle()
        self.assertEqual(self.moved, [(222, 333)])

    async def test_turning_it_off_stops_the_process(self):
        await self.host.sync(True, {"position": "bottom-left"}, "")
        await self.host.sync(False, {"position": "bottom-left"}, "")
        self.assertFalse(self.host.state()["running"])

    async def test_a_crash_is_restarted(self):
        await self.host.sync(True, {"position": "bottom-left"}, "")
        self.host.key("CRASH", True)
        for _ in range(200):
            await asyncio.sleep(0.02)
            if self.host.running and len([m for m in self._received() if m["t"] == "style"]) == 2:
                break
        self.assertTrue(self.host.running)
        self.assertEqual(len([m for m in self._received() if m["t"] == "style"]), 2)

    async def test_no_display_is_reported_not_retried(self):
        with mock.patch("vice.overlay_host.has_display", return_value=False):
            await self.host.sync(True, {"position": "bottom-left"}, "")
        self.assertEqual(self.host.state(), {"running": False, "error": "no-display", "moving": False})


class HiddenKeysTests(unittest.TestCase):
    """#258: a key taken off the panel is not shown, read or kept."""

    def test_a_hidden_key_is_not_recorded(self) -> None:
        ring = keys.KeyRing(60)
        ring.hide(["W"])
        self.assertIsNone(ring.record("KEY_W", True, at=1.0))
        self.assertEqual(ring.record("KEY_A", True, at=1.0), ("A", True))
        self.assertEqual([k for _, _, k in ring.intervals(0.0, 2.0)], ["A"])

    def test_hiding_forgets_what_was_already_kept(self) -> None:
        ring = keys.KeyRing(60)
        ring.record("KEY_W", True, at=1.0)
        ring.record("KEY_W", False, at=2.0)
        ring.hide(["W"])
        self.assertEqual(ring.intervals(0.0, 3.0), [])

    def test_showing_it_again_records_it(self) -> None:
        ring = keys.KeyRing(60)
        ring.hide(["W"])
        ring.hide([])
        self.assertEqual(ring.record("KEY_W", True, at=1.0), ("W", True))

    def test_a_hidden_key_is_not_drawn_and_the_rest_stay_put(self) -> None:
        from vice.key_overlay import key_rects

        full = {key: (x, y) for key, _, x, y, _, _ in key_rects(1.0)}
        kept = {key: (x, y) for key, _, x, y, _, _ in key_rects(1.0, {"W"})}
        self.assertNotIn("W", kept)
        self.assertEqual(kept, {k: v for k, v in full.items() if k != "W"})

    def test_unknown_ids_are_dropped_and_an_empty_panel_is_refused(self) -> None:
        from vice.config import Config, normalize_overlay
        from vice.keys import LAYOUT_KEYS

        cfg = Config()
        cfg.overlay.keys_hidden = ["W", "NOPE", "W"]
        normalize_overlay(cfg)
        self.assertEqual(cfg.overlay.keys_hidden, ["W"])
        cfg.overlay.keys_hidden = sorted(LAYOUT_KEYS)
        normalize_overlay(cfg)
        self.assertEqual(cfg.overlay.keys_hidden, [])



