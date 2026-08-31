import json
import tempfile
import unittest
from pathlib import Path

from vice.editor import (
    EditorProjectStore,
    Source,
    build_export_cmd,
    canvas_for,
    default_export_name,
    parse_progress,
    project_extent,
    sanitize_export_name,
    text_file_contents,
    validate_project,
)

SRC = {
    "Clip_A": Source(Path("/v/Clip_A.mp4"), 30.0, 1920, 1080, True),
    "Clip_B": Source(Path("/v/Clip_B.mp4"), 20.0, 2560, 1440, True),
    "Clip_C": Source(Path("/v/Clip_C.mp4"), 10.0, 1920, 1080, False),
}

TRACKS = [
    {"id": "T1", "type": "text", "label": "T1"},
    {"id": "V2", "type": "video", "label": "V2"},
    {"id": "V1", "type": "video", "label": "V1"},
    {"id": "A1", "type": "audio", "label": "A1"},
]


def proj(items, tracks=None):
    return {"version": 1, "tracks": tracks or TRACKS, "items": items}


def clip(iid, track, cid, start, dur, offset=0, **extra):
    return dict({"id": iid, "kind": "clip", "trackId": track, "clipId": cid,
                 "start": start, "dur": dur, "offset": offset}, **extra)


class ValidateProjectTests(unittest.TestCase):
    def test_valid_project_normalizes(self) -> None:
        p, errors = validate_project(proj([
            clip("i1", "V1", "Clip_A", 0, 10.12345, junk="x"),
            {"id": "i2", "kind": "text", "trackId": "T1", "start": 1, "dur": 3,
             "text": "GG", "font": "display", "size": 64, "weight": 700,
             "color": "#ffffff", "x": 50, "y": 18},
        ]), SRC)
        self.assertEqual(errors, [])
        self.assertEqual(p["items"][1]["dur"], 10.123)
        self.assertNotIn("junk", p["items"][1])
        self.assertEqual(project_extent(p), 10.123)

    def test_rejects_overlap_bad_track_and_kind_mismatch(self) -> None:
        _, errors = validate_project(proj([
            clip("i1", "V1", "Clip_A", 0, 10),
            clip("i2", "V1", "Clip_B", 5, 10),
            clip("i3", "A1", "Clip_A", 0, 5),
            clip("i4", "NOPE", "Clip_A", 0, 5),
        ]), SRC)
        self.assertTrue(any("overlaps" in e for e in errors))
        self.assertTrue(any("does not belong" in e for e in errors))
        self.assertTrue(any("unknown kind or track" in e for e in errors))

    def test_offset_cannot_exceed_source_duration(self) -> None:
        _, errors = validate_project(proj([
            clip("i1", "V1", "Clip_C", 0, 8, offset=5),
        ]), SRC)
        self.assertTrue(any("more of Clip_C" in e for e in errors))

    def test_missing_clip_is_an_error(self) -> None:
        _, errors = validate_project(proj([
            clip("i1", "V1", "Gone", 0, 5),
        ]), SRC)
        self.assertTrue(any("Gone is missing" in e for e in errors))

    def test_transition_length_clamped(self) -> None:
        p, errors = validate_project(proj([
            clip("i1", "V1", "Clip_A", 0, 10),
            clip("i2", "V1", "Clip_B", 10, 2, trans={"fx": "crossfade", "len": 9}),
            clip("i3", "V1", "Clip_A", 12, 4, offset=12,
                 trans={"fx": "nope", "len": 1}),
        ]), SRC)
        self.assertEqual(errors, [])
        self.assertEqual(p["items"][1]["trans"], {"fx": "crossfade", "len": 1.6})
        self.assertNotIn("trans", p["items"][2])

    def test_track_invariants(self) -> None:
        _, errors = validate_project(proj(
            [clip("i1", "V1", "Clip_A", 0, 5)],
            tracks=[{"id": "V1", "type": "video", "label": "V1"}],
        ), SRC)
        self.assertTrue(any("exactly one text track" in e for e in errors))

        _, errors = validate_project(proj([], tracks=[
            {"id": "T1", "type": "text", "label": "T1"},
            {"id": "A1", "type": "audio", "label": "A1"},
        ]), SRC)
        self.assertTrue(any("at least one video track" in e for e in errors))

    def test_empty_timeline_is_an_error(self) -> None:
        _, errors = validate_project(proj([]), SRC)
        self.assertIn("timeline is empty", errors)

    def test_text_styling_falls_back_instead_of_failing(self) -> None:
        p, errors = validate_project(proj([
            clip("i1", "V1", "Clip_A", 0, 5),
            {"id": "i2", "kind": "text", "trackId": "T1", "start": 0, "dur": 2,
             "text": "hi", "font": "comic-sans", "size": "big",
             "color": "red", "x": 400, "y": -3},
        ]), SRC)
        self.assertEqual(errors, [])
        t = p["items"][0]
        self.assertEqual((t["font"], t["size"], t["color"]), ("display", 64, "#f2f5fa"))
        self.assertEqual((t["x"], t["y"]), (100.0, 0.0))


class CanvasTests(unittest.TestCase):
    def test_canvas_follows_first_main_track_clip(self) -> None:
        p, _ = validate_project(proj([
            clip("i1", "V2", "Clip_A", 0, 5),
            clip("i2", "V1", "Clip_B", 2, 5),
        ]), SRC)
        self.assertEqual(canvas_for(p, SRC), (2560, 1440, 60))

    def test_canvas_defaults_to_1080p(self) -> None:
        p, _ = validate_project(proj([
            {"id": "i1", "kind": "text", "trackId": "T1", "start": 0, "dur": 2,
             "text": "hi"},
        ]), SRC)
        self.assertEqual(canvas_for(p, SRC), (1920, 1080, 60))


class GraphBuilderTests(unittest.TestCase):
    def build(self, items, **kw):
        p, errors = validate_project(proj(items), SRC)
        self.assertEqual(errors, [])
        return build_export_cmd(p, SRC, Path("/out/.x.export.mp4"),
                                fonts=Path("/f"), text_dir=Path("/tx"), **kw)

    def graph(self, cmd):
        return cmd[cmd.index("-filter_complex") + 1]

    def test_single_clip_golden(self) -> None:
        cmd = self.build([clip("i1", "V1", "Clip_A", 0, 10, offset=2)])
        self.assertEqual(cmd, [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
            "-progress", "pipe:1",
            "-i", "/v/Clip_A.mp4",
            "-filter_complex",
            "[0:v]trim=start=2:end=12,setpts=PTS-STARTPTS,fps=60,"
            "scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,settb=AVTB,"
            "format=yuv420p[s0];"
            "anullsrc=r=48000:cl=stereo,atrim=0:10[ab];"
            "[0:a:0]atrim=start=2:end=12,asetpts=PTS-STARTPTS,"
            "aformat=sample_rates=48000:channel_layouts=stereo,"
            "adelay=0:all=1[a0];"
            "[ab][a0]amix=inputs=2:duration=longest:normalize=0,"
            "atrim=0:10,asetpts=PTS-STARTPTS[aout]",
            "-map", "[s0]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            "-t", "10",
            "-f", "mp4",
            "-y", "/out/.x.export.mp4",
        ])

    def test_crossfade_extends_left_and_sets_offset(self) -> None:
        g = self.graph(self.build([
            clip("i1", "V1", "Clip_A", 0, 6, offset=2),
            clip("i2", "V1", "Clip_B", 6, 5,
                 trans={"fx": "crossfade", "len": 0.6}),
        ]))
        self.assertIn("[0:v]trim=start=2:end=8.6,", g)
        self.assertIn("xfade=transition=fade:duration=0.6:offset=6[x2]", g)

    def test_crossfade_without_source_tail_freezes_last_frame(self) -> None:
        g = self.graph(self.build([
            clip("i1", "V1", "Clip_C", 0, 10),
            clip("i2", "V1", "Clip_B", 10, 5, trans={"fx": "slide", "len": 1}),
        ]))
        self.assertIn("tpad=stop_mode=clone:stop_duration=1[s0]", g)
        self.assertIn("xfade=transition=slideleft:duration=1:offset=10", g)

    def test_dipaccent_uses_color_fades_and_concat(self) -> None:
        g = self.graph(self.build([
            clip("i1", "V1", "Clip_A", 0, 6),
            clip("i2", "V1", "Clip_B", 6, 5,
                 trans={"fx": "dipaccent", "len": 0.8}),
        ], accent="#33adff"))
        self.assertIn("fade=t=out:st=5.6:d=0.4:color=0x33adff", g)
        self.assertIn("fade=t=in:st=0:d=0.4:color=0x33adff", g)
        self.assertIn("concat=n=2:v=1:a=0", g)
        self.assertNotIn("xfade", g)

    def test_gap_becomes_black_and_tail_is_padded(self) -> None:
        g = self.graph(self.build([
            clip("i1", "V1", "Clip_A", 2, 5),
            clip("i2", "V2", "Clip_B", 0, 12),
        ]))
        self.assertIn("color=c=black:s=1920x1080:r=60:d=2,settb=AVTB,"
                      "format=yuv420p[s0]", g)
        self.assertIn("tpad=stop_mode=add:stop_duration=5", g)

    def test_lead_transition_xfades_against_black(self) -> None:
        # A transition with nothing before it still has to move: slide slid
        # in the preview but exported as a plain fade before this.
        g = self.graph(self.build([
            clip("i1", "V1", "Clip_A", 0, 6, trans={"fx": "slide", "len": 0.8}),
        ]))
        self.assertIn("color=c=black:s=1920x1080:r=60:d=0.8", g)
        self.assertIn("xfade=transition=slideleft:duration=0.8:offset=0", g)

    def test_lead_dipaccent_uses_a_colour_fade(self) -> None:
        g = self.graph(self.build([
            clip("i1", "V1", "Clip_A", 0, 6, trans={"fx": "dipaccent", "len": 0.6}),
        ], accent="#33adff"))
        self.assertIn("fade=t=in:st=0:d=0.6:color=0x33adff", g)
        self.assertNotIn("xfade", g)

    def test_muted_and_detached_audio(self) -> None:
        g = self.graph(self.build([
            clip("i1", "V1", "Clip_A", 0, 6, offset=2, muted=True),
            {"id": "i2", "kind": "audio", "trackId": "A1", "clipId": "Clip_A",
             "start": 8, "dur": 4, "offset": 2},
        ]))
        self.assertEqual(g.count("[0:a:0]"), 1)
        self.assertIn("adelay=8000:all=1[a0]", g)
        self.assertIn("[0:v]trim=start=2:end=8,", g)

    def test_source_without_audio_is_skipped(self) -> None:
        g = self.graph(self.build([clip("i1", "V1", "Clip_C", 0, 8)]))
        self.assertNotIn("[0:a:0]", g)
        self.assertIn("[ab]anull[aout]", g)

    def test_two_track_slide_golden(self) -> None:
        # Every video track is the same chain: segments, gap fillers and real
        # xfade transitions. Upper tracks fill gaps transparently and are
        # composited with overlay, so a clip on V2 gets the same transitions
        # the program track has always had.
        g = self.graph(self.build([
            clip("i1", "V1", "Clip_A", 0, 20),
            clip("i2", "V2", "Clip_B", 3, 5),
            clip("i3", "V2", "Clip_C", 8, 5, trans={"fx": "slide", "len": 0.8}),
        ]))
        seg = ("setpts=PTS-STARTPTS,fps=60,"
               "scale=1920:1080:force_original_aspect_ratio=decrease,"
               "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,settb=AVTB,")
        self.assertEqual(g, ";".join([
            f"[2:v]trim=start=0:end=20,{seg}format=yuv420p[s0]",
            "color=c=black@0:s=1920x1080:r=60:d=3,settb=AVTB,format=yuva420p[s1]",
            f"[0:v]trim=start=0:end=5.8,{seg}format=yuva420p[s2]",
            "[s1][s2]concat=n=2:v=1:a=0[c3]",
            f"[1:v]trim=start=0:end=5,{seg}format=yuva420p[s4]",
            "[c3][s4]xfade=transition=slideleft:duration=0.8:offset=8[x5]",
            "[s0][x5]overlay=eof_action=pass[o6]",
            "anullsrc=r=48000:cl=stereo,atrim=0:20[ab]",
            "[2:a:0]atrim=start=0:end=20,asetpts=PTS-STARTPTS,"
            "aformat=sample_rates=48000:channel_layouts=stereo,adelay=0:all=1[a0]",
            "[0:a:0]atrim=start=0:end=5,asetpts=PTS-STARTPTS,"
            "aformat=sample_rates=48000:channel_layouts=stereo,adelay=3000:all=1[a1]",
            "[ab][a0][a1]amix=inputs=3:duration=longest:normalize=0,"
            "atrim=0:20,asetpts=PTS-STARTPTS[aout]",
        ]))

    def test_upper_track_gaps_are_transparent_and_bottom_stays_black(self) -> None:
        g = self.graph(self.build([
            clip("i1", "V1", "Clip_A", 2, 18),
            clip("i2", "V2", "Clip_B", 8, 6, offset=1),
        ]))
        self.assertIn("color=c=black:s=1920x1080:r=60:d=2,settb=AVTB,"
                      "format=yuv420p", g)
        self.assertIn("color=c=black@0:s=1920x1080:r=60:d=8,settb=AVTB,"
                      "format=yuva420p", g)
        # Alpha does the masking, so the overlay needs no enable window and
        # nothing is shifted with setpts.
        self.assertIn("overlay=eof_action=pass[", g)
        self.assertNotIn("overlay=eof_action=pass:enable", g)
        self.assertNotIn("setpts=PTS+", g)

    def test_bottom_only_project_never_uses_alpha(self) -> None:
        g = self.graph(self.build([
            clip("i1", "V1", "Clip_A", 2, 5),
            clip("i2", "V1", "Clip_B", 7, 5, trans={"fx": "crossfade", "len": 1}),
        ]))
        self.assertNotIn("yuva420p", g)
        self.assertNotIn("black@0", g)
        self.assertNotIn("overlay", g)

    def test_upper_lead_dip_repairs_alpha_but_crossfade_does_not(self) -> None:
        # xfade dips through opaque black, which would punch a hole in an
        # upper track when the outgoing side is a transparent gap.
        g = self.graph(self.build([
            clip("i1", "V1", "Clip_A", 0, 20),
            clip("i2", "V2", "Clip_B", 0, 6, trans={"fx": "fadeblack", "len": 0.8}),
        ]))
        self.assertIn("xfade=transition=fadeblack:duration=0.8:offset=0,"
                      "fade=t=in:st=0:d=0.8:alpha=1", g)

        g = self.graph(self.build([
            clip("i1", "V1", "Clip_A", 0, 20),
            clip("i2", "V2", "Clip_B", 0, 6, trans={"fx": "crossfade", "len": 0.8}),
        ]))
        self.assertNotIn("alpha=1", g)

    def test_upper_dip_between_two_clips_stays_opaque(self) -> None:
        # A dip between two clips on the same track is meant to hide the
        # tracks below, so no alpha repair there.
        g = self.graph(self.build([
            clip("i1", "V1", "Clip_A", 0, 20),
            clip("i2", "V2", "Clip_B", 0, 5),
            clip("i3", "V2", "Clip_C", 5, 5, trans={"fx": "fadewhite", "len": 1}),
        ]))
        self.assertIn("xfade=transition=fadewhite:duration=1:offset=5[", g)
        self.assertNotIn("alpha=1", g)

    def test_three_tracks_composite_with_the_top_one_last(self) -> None:
        tracks = [
            {"id": "T1", "type": "text", "label": "T1"},
            {"id": "V3", "type": "video", "label": "V3"},
            {"id": "V2", "type": "video", "label": "V2"},
            {"id": "V1", "type": "video", "label": "V1"},
            {"id": "A1", "type": "audio", "label": "A1"},
        ]
        p, errors = validate_project(proj([
            clip("i1", "V1", "Clip_A", 0, 20),
            clip("i2", "V2", "Clip_B", 0, 6),
            clip("i3", "V3", "Clip_C", 0, 6),
        ], tracks=tracks), SRC)
        self.assertEqual(errors, [])
        cmd = build_export_cmd(p, SRC, Path("/out/.x.export.mp4"),
                               fonts=Path("/f"), text_dir=Path("/tx"))
        g = cmd[cmd.index("-filter_complex") + 1]
        overlays = [ln for ln in g.split(";") if "overlay=" in ln]
        self.assertEqual(len(overlays), 2)
        # The second overlay consumes the first one's output, so the
        # top-listed track wins.
        self.assertIn(overlays[0].split("[")[-1].rstrip("]"), overlays[1])

    def test_discord_optimized_caps_bitrate_to_fit_20mb(self) -> None:
        # Duration alone drives the bitrate budget, so build raw (unvalidated)
        # projects rather than routing through the 30s-capped SRC fixture.
        def raw_build(dur: float) -> list[str]:
            return build_export_cmd(proj([clip("i1", "V1", "Clip_A", 0, dur)]), SRC,
                                    Path("/out/.x.export.mp4"), fonts=Path("/f"),
                                    text_dir=Path("/tx"), discord_optimized=True)
        short = raw_build(10)
        long = raw_build(200)
        self.assertNotIn("-crf", short)
        self.assertIn("-b:v", short)
        self.assertIn("-maxrate", short)
        short_kbps = int(short[short.index("-b:v") + 1].rstrip("k"))
        long_kbps = int(long[long.index("-b:v") + 1].rstrip("k"))
        self.assertGreater(short_kbps, long_kbps)
        self.assertGreaterEqual(long_kbps, 150)  # DISCORD_MIN_VIDEO_KBPS floor

    def test_empty_upper_track_emits_no_overlay(self) -> None:
        g = self.graph(self.build([clip("i1", "V1", "Clip_A", 0, 10)]))
        self.assertNotIn("overlay", g)

    def test_drawtext_fontfile_textfile_and_window(self) -> None:
        g = self.graph(self.build([
            clip("i1", "V1", "Clip_A", 0, 10),
            {"id": "i2", "kind": "text", "trackId": "T1", "start": 1, "dur": 3,
             "text": "GG", "font": "display", "size": 64, "weight": 700,
             "color": "#ffffff", "x": 50, "y": 18},
        ]))
        self.assertIn("drawtext=expansion=none:fontfile='/f/Geist-Bold.ttf':"
                      "textfile='/tx/text_i2.txt':fontsize=64:"
                      "fontcolor=0xffffff:", g)
        self.assertIn("x=(w*50/100)-(text_w/2):y=(h*18/100)-(text_h/2):"
                      "enable='between(t,1,4)'", g)

    def test_text_files_carry_raw_text(self) -> None:
        p, _ = validate_project(proj([
            clip("i1", "V1", "Clip_A", 0, 5),
            {"id": "i2", "kind": "text", "trackId": "T1", "start": 0, "dur": 2,
             "text": "it's 100%: fine, ok"},
        ]), SRC)
        files = text_file_contents(p, Path("/tx"))
        self.assertEqual(files, {Path("/tx/text_i2.txt"): "it's 100%: fine, ok"})


class NamingTests(unittest.TestCase):
    def test_sanitize_export_name(self) -> None:
        self.assertEqual(sanitize_export_name("my-edit"), "my-edit.mp4")
        self.assertEqual(sanitize_export_name("my-edit.MP4"), "my-edit.mp4")
        self.assertEqual(sanitize_export_name("../../evil"), "evil.mp4")
        self.assertEqual(sanitize_export_name("has space"), "has-space.mp4")
        self.assertIsNone(sanitize_export_name("  "))
        self.assertIsNone(sanitize_export_name("..."))

    def test_default_export_name_skips_existing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self.assertEqual(default_export_name(out), "Vice_Edit_1.mp4")
            (out / "Vice_Edit_1.mp4").touch()
            (out / "Vice_Edit_2.mkv").touch()
            self.assertEqual(default_export_name(out), "Vice_Edit_3.mp4")


class ProjectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.store = EditorProjectStore(Path(self.tmpdir.name) / "project.json")

    def test_round_trip_and_corrupt_file(self) -> None:
        self.assertIsNone(self.store.load())
        project = proj([clip("i1", "V1", "Clip_A", 0, 5)])
        self.store.save(project)
        self.assertEqual(self.store.load(), project)

        self.store.path.write_text("{broken")
        self.assertIsNone(self.store.load())

    def test_rename_and_delete_hooks(self) -> None:
        self.store.save(proj([
            clip("i1", "V1", "Clip_A", 0, 5),
            clip("i2", "V1", "Clip_B", 5, 5),
            {"id": "i3", "kind": "audio", "trackId": "A1", "clipId": "Clip_A",
             "start": 0, "dur": 5, "offset": 0},
        ]))
        self.assertTrue(self.store.on_clip_renamed("Clip_A", "Best_Clip"))
        ids = [i.get("clipId") for i in self.store.load()["items"]]
        self.assertEqual(ids, ["Best_Clip", "Clip_B", "Best_Clip"])
        self.assertFalse(self.store.on_clip_renamed("Clip_A", "Nope"))

        self.assertTrue(self.store.on_clip_deleted("Best_Clip"))
        items = self.store.load()["items"]
        self.assertEqual([i["id"] for i in items], ["i2"])
        self.assertFalse(self.store.on_clip_deleted("Best_Clip"))


class ProgressTests(unittest.TestCase):
    def test_parse_progress(self) -> None:
        self.assertEqual(parse_progress("out_time_us=5000000", 10.0), 0.5)
        self.assertEqual(parse_progress("out_time_us=99000000", 10.0), 1.0)
        self.assertEqual(parse_progress("progress=end", 10.0), 1.0)
        self.assertIsNone(parse_progress("progress=continue", 10.0))
        self.assertIsNone(parse_progress("frame=42", 10.0))
        self.assertIsNone(parse_progress("out_time_us=nan", 10.0))


if __name__ == "__main__":
    unittest.main()
