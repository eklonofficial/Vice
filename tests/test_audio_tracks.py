import asyncio
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

try:
    import _sandbox  # noqa: F401  keeps every store out of the real home
except ModuleNotFoundError:
    from tests import _sandbox  # noqa: F401

from vice.editor import Source, build_export_cmd, validate_project
from vice.media import probe_media

try:
    from vice import share as _share
except ModuleNotFoundError:
    _share = None


def project(stream=1, volume=0.5, muted=False):
    return {'version': 1, 'tracks': [
        {'id': 'T1', 'type': 'text'}, {'id': 'V1', 'type': 'video'},
        {'id': 'A1', 'type': 'audio'}], 'items': [
        {'id': 'v', 'kind': 'clip', 'trackId': 'V1', 'clipId': 'clip',
         'start': 0, 'dur': 1, 'offset': 0, 'muted': True},
        {'id': 'a', 'kind': 'audio', 'trackId': 'A1', 'clipId': 'clip',
         'start': 0, 'dur': 1, 'offset': 0, 'audioStream': stream,
         'volume': volume, 'muted': muted}]}


class AudioProjectTests(unittest.TestCase):
    def setUp(self):
        self.sources = {'clip': Source(Path('clip.mkv'), 2, 128, 72, True, audio_streams=3)}

    def test_selected_stream_gain_and_mute_survive_validation(self):
        normalized, errors = validate_project(project(), self.sources)
        self.assertEqual(errors, [])
        audio = next(i for i in normalized['items'] if i['kind'] == 'audio')
        self.assertEqual((audio['audioStream'], audio['volume'], audio['muted']), (1, 0.5, False))
        graph = build_export_cmd(normalized, self.sources, Path('out.mp4'))
        graph = graph[graph.index('-filter_complex') + 1]
        self.assertIn('[0:a:1]', graph)
        self.assertIn('volume=0.5', graph)
        self.assertNotIn('[0:a:0]', graph)

    def test_invalid_streams_and_nonfinite_gains_are_rejected(self):
        for value in (-1, 3, 0.5, True, '1'):
            with self.subTest(stream=value):
                self.assertTrue(validate_project(project(stream=value), self.sources)[1])
        for value in (-0.1, 1.1, float('nan'), float('inf'), True, 'bad'):
            with self.subTest(volume=value):
                self.assertTrue(validate_project(project(volume=value), self.sources)[1])

    def test_muted_stream_does_not_contribute_to_export(self):
        normalized, errors = validate_project(project(muted=True), self.sources)
        self.assertEqual(errors, [])
        cmd = build_export_cmd(normalized, self.sources, Path('out.mp4'))
        self.assertNotIn('[0:a:', cmd[cmd.index('-filter_complex') + 1])

    def test_old_audio_items_still_select_first_stream_at_full_volume(self):
        raw = project()
        for key in ('audioStream', 'volume', 'muted'):
            raw['items'][1].pop(key)
        normalized, errors = validate_project(raw, self.sources)
        self.assertEqual(errors, [])
        cmd = build_export_cmd(normalized, self.sources, Path('out.mp4'))
        graph = cmd[cmd.index('-filter_complex') + 1]
        self.assertIn('[0:a:0]', graph)
        self.assertNotIn('volume=', graph)


def make_source(path):
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=size=128x72:rate=10:duration=2',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
                    '-f', 'lavfi', '-i', 'sine=frequency=880:duration=2',
                    '-f', 'lavfi', '-i', 'sine=frequency=1320:duration=2',
                    '-map', '0:v', '-map', '1:a', '-map', '2:a', '-map', '3:a',
                    '-c:v', 'ffv1', '-threads', '1', '-c:a', 'pcm_s16le',
                    '-metadata:s:a:0', 'title=Combined', '-metadata:s:a:1', 'title=Game',
                    '-metadata:s:a:2', 'title=Microphone', str(path)], check=True, timeout=15)


def tone_levels(path):
    raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path),
                                   '-ss', '0.1', '-t', '0.5', '-map', '0:a:0',
                                   '-ac', '1', '-ar', '8000', '-f', 'f32le', '-'], timeout=10)
    samples = struct.unpack('<' + 'f' * (len(raw) // 4), raw)
    return [abs(sum(x * complex(math.cos(2*math.pi*f*i/8000),
                               math.sin(2*math.pi*f*i/8000))
                    for i, x in enumerate(samples))) / len(samples)
            for f in (440, 880, 1320)]


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not installed')
class RealAudioTracksTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_and_export_retain_the_selected_recorded_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'clip.mkv'
            make_source(source)
            meta = await probe_media(source)
            self.assertEqual(meta['audio_streams'], 3)
            self.assertEqual([t['title'] for t in meta['audio_tracks']], ['Combined', 'Game', 'Microphone'])
            sources = {'clip': Source(source, 2, 128, 72, True, audio_streams=3)}
            normalized, errors = validate_project(project(stream=1, volume=0.5), sources)
            self.assertEqual(errors, [])
            output = root / 'out.mp4'
            subprocess.run(build_export_cmd(normalized, sources, output), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
            levels = tone_levels(output)
            self.assertGreater(levels[1], 20 * max(levels[0], levels[2]))
            self.assertGreater(levels[1], 0.015)
            self.assertLess(levels[1], 0.04)


    async def test_delayed_stream_keeps_its_leading_silence_in_preview_and_export(self):
        from unittest import mock
        from vice import share
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'delayed.mkv'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'color=size=128x72:rate=10:duration=2', '-itsoffset', '0.4',
                            '-f', 'lavfi', '-i', 'sine=frequency=880:duration=1.6',
                            '-map', '0:v', '-map', '1:a', '-c:v', 'ffv1', '-threads', '1',
                            '-c:a', 'pcm_s16le', str(source)], check=True, timeout=10)
            sources = {'clip': Source(source, 2, 128, 72, True, audio_streams=1)}
            normalized, errors = validate_project(project(stream=0, volume=1), sources)
            self.assertEqual(errors, [])
            output = root / 'out.mp4'
            subprocess.run(build_export_cmd(normalized, sources, output), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
            with mock.patch.object(share, 'PROXY_DIR', root / 'cache'):
                preview = await share._make_audio_preview(source, 0)
            for path in (preview, output):
                for start, silent in ((0, True), (0.5, False)):
                    raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path),
                        '-ss', str(start), '-t', '0.2', '-map', '0:a:0', '-ac', '1', '-f', 'f32le', '-'], timeout=10)
                    samples = struct.unpack('<' + 'f' * (len(raw) // 4), raw)
                    peak = max(abs(x) for x in samples)
                    if silent:
                        self.assertLess(peak, 0.001, path.name)
                    else:
                        self.assertGreater(peak, 0.05, path.name)


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not installed')
class AudioPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer
        from unittest import mock
        from vice import share
        from vice.config import Config, OutputConfig, SharingConfig
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'clip.mkv'
        make_source(self.source)
        for target, value in (
            ('vice.share.PROXY_DIR', self.root / 'proxies'),
            ('vice.share.THUMB_DIR', self.root / 'thumbs'),
            ('vice.share.VIEWS_PATH', self.root / 'views.json'),
            ('vice.playlists.PLAYLISTS_PATH', self.root / 'playlists.json'),
            ('vice.editor.PROJECT_PATH', self.root / 'project.json'),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = share.ShareServer(Config(output=OutputConfig(directory=str(self.root)),
                                              sharing=SharingConfig(cloudflare_tunnel=False)))
        self.server._clips = {'clip': self.source}
        self.client = TestClient(TestServer(self.server._local_app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.addAsyncCleanup(self.server.stop)

    async def test_preview_selects_stream_and_supports_ranges_and_cache_reuse(self):
        from unittest import mock
        from vice import share
        replies = await asyncio.gather(self.client.get('/api/clips/clip/audio/2'),
                                       self.client.get('/api/clips/clip/audio/2'))
        contents = []
        for response in replies:
            self.assertEqual(response.status, 200)
            contents.append(await response.read())
        self.assertEqual(contents[0], contents[1])
        preview = self.root / 'audio.m4a'
        preview.write_bytes(contents[0])
        levels = tone_levels(preview)
        self.assertGreater(levels[2], 20 * max(levels[:2]))
        self.assertEqual(len(list((self.root / 'proxies').iterdir())), 1)
        with mock.patch.object(share, '_make_audio_preview') as encoder:
            response = await self.client.get('/api/clips/clip/audio/2', headers={'Range': 'bytes=0-99'})
            self.assertEqual(response.status, 206)
            self.assertEqual(await response.read(), contents[0][:100])
            encoder.assert_not_called()
        self.assertEqual(len((await (await self.client.get('/api/clips/clip')).json())['audio_tracks']), 3)

    async def test_invalid_or_missing_tracks_are_rejected(self):
        for path in ('clip/audio/-1', 'clip/audio/3', 'clip/audio/1.0', 'missing/audio/0'):
            response = await self.client.get('/api/clips/' + path)
            self.assertEqual(response.status, 404, path)
        self.server._proxy_stopping = True
        self.assertEqual((await self.client.get('/api/clips/clip/audio/0')).status, 503)

    async def test_audio_route_is_not_exposed_by_public_server(self):
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.server._public_app)) as public:
            self.assertEqual((await public.get('/api/clips/clip/audio/1')).status, 404)

    async def test_canceled_audio_conversion_reaps_child_and_removes_partial_file(self):
        import sys
        from unittest import mock
        from vice import share
        real_spawn = asyncio.create_subprocess_exec
        ready = asyncio.Event()
        child = None
        original = self.source.read_bytes()

        async def spawn(*args, **kwargs):
            nonlocal child
            child = await real_spawn(sys.executable, '-c', 'import time; time.sleep(60)', **kwargs)
            ready.set()
            return child

        with mock.patch.object(asyncio, 'create_subprocess_exec', spawn):
            task = asyncio.create_task(share._make_audio_preview(self.source, 1))
            await asyncio.wait_for(ready.wait(), 2)
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        self.assertIsNotNone(child.returncode)
        self.assertEqual(list((self.root / 'proxies').iterdir()), [])
        self.assertEqual(self.source.read_bytes(), original)


def _tone(path, stream, freq):
    """Strength of one frequency in one audio stream."""
    raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path),
                                   '-ss', '0.3', '-t', '0.5', '-map', f'0:a:{stream}',
                                   '-ac', '1', '-ar', '8000', '-f', 'f32le', '-'], timeout=10)
    samples = struct.unpack('<' + 'f' * (len(raw) // 4), raw)
    return abs(sum(x * complex(math.cos(2*math.pi*freq*i/8000), math.sin(2*math.pi*freq*i/8000))
                   for i, x in enumerate(samples))) / len(samples)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg not installed')
class TrackVolumeTests(unittest.IsolatedAsyncioTestCase):
    """#251: separate tracks were recorded at full level with no way to
    balance them, so a call and a mic disappeared under game audio."""

    def _rc(self, volumes, mix_first=True):
        from vice.config import RecordingConfig
        rc = RecordingConfig()
        rc.audio_tracks = ['default_output', 'device:usb-mic']
        rc.audio_tracks_mix_first = mix_first
        rc.capture_microphone = False
        rc.audio_track_volumes = volumes
        return rc

    def _clip(self, root, tracks):
        path = root / 'clip.mp4'
        inputs, maps = [], ['-map', '0:v']
        for n, expr in enumerate(tracks, start=1):
            inputs += ['-f', 'lavfi', '-i', f'aevalsrc={expr}:s=48000:d=2,aformat=channel_layouts=stereo']
            maps += ['-map', f'{n}:a']
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=128x72:rate=10:duration=2',
                        *inputs, *maps, '-c:v', 'libx264', '-preset', 'ultrafast',
                        '-c:a', 'aac', '-b:a', '128k', '-shortest', str(path)], check=True, timeout=30)
        return path

    async def test_defaults_leave_the_clip_alone(self):
        from vice.recorder import _apply_track_volumes
        with tempfile.TemporaryDirectory() as directory:
            path = self._clip(Path(directory), ['0.2*sin(2*PI*440*t)'] * 3)
            before = path.read_bytes()
            await _apply_track_volumes(path, self._rc({}))
            await _apply_track_volumes(path, self._rc({'default_output': 1.0}))
            self.assertEqual(path.read_bytes(), before)

    async def test_levels_apply_per_track_and_the_combined_track_follows(self):
        from vice.recorder import _apply_track_volumes
        with tempfile.TemporaryDirectory() as directory:
            path = self._clip(Path(directory), [
                '0.2*sin(2*PI*440*t)+0.2*sin(2*PI*880*t)',
                '0.2*sin(2*PI*440*t)',
                '0.2*sin(2*PI*880*t)',
            ])
            before = [_tone(path, 0, 440), _tone(path, 0, 880),
                      _tone(path, 1, 440), _tone(path, 2, 880)]
            await _apply_track_volumes(path, self._rc({'default_output': 0.5, 'device:usb-mic': 2.0}))
            after = [_tone(path, 0, 440), _tone(path, 0, 880),
                     _tone(path, 1, 440), _tone(path, 2, 880)]
            for was, now, gain in zip(before, after, (0.5, 2.0, 0.5, 2.0)):
                self.assertAlmostEqual(now / was, gain, delta=0.1 * gain)
            streams = subprocess.check_output(
                ['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'csv=p=0',
                 str(path)], text=True).split()
            self.assertEqual(streams, ['video', 'audio', 'audio', 'audio'])

    async def test_a_clip_that_does_not_match_the_tracks_is_left_alone(self):
        from vice.recorder import _apply_track_volumes
        with tempfile.TemporaryDirectory() as directory:
            path = self._clip(Path(directory), ['0.2*sin(2*PI*440*t)'] * 2)
            before = path.read_bytes()
            await _apply_track_volumes(path, self._rc({'device:usb-mic': 2.0}))
            self.assertEqual(path.read_bytes(), before)


class TrackVolumeGraphTests(unittest.TestCase):
    def test_an_app_the_monitor_already_carries_is_not_mixed_twice(self):
        from pathlib import Path as P
        from vice.recorder import _track_volume_cmd
        cmd = _track_volume_cmd(P('c.mp4'), P('t.mp4'),
                                ['default_output', 'default_output', 'app:Discord'], True,
                                [1.0, 1.0, 1.5], [('opus', 96000)] * 3)
        graph = cmd[cmd.index('-filter_complex') + 1]
        self.assertIn('[0:a:2]aformat=channel_layouts=stereo,volume=1.5[v2]', graph)
        # Only the app changed, and the monitor already carries it, so the
        # combined track would come out the same: it is copied, not rebuilt.
        self.assertNotIn('amix', graph)
        self.assertEqual(cmd[cmd.index('-c:a:0') + 1], 'copy')
        self.assertEqual(cmd[cmd.index('-c:a:1') + 1], 'copy')
        self.assertEqual(cmd[cmd.index('-c:a:2') + 1], 'libopus')
        self.assertEqual(cmd[cmd.index('-b:a:2') + 1], '96000')


class TrackVolumeRestartTests(unittest.TestCase):
    def test_moving_a_track_volume_does_not_restart_the_recorder(self):
        """A restart empties the replay buffer, and the volumes only apply
        when a clip is saved."""
        from types import SimpleNamespace
        from vice.config import Config
        from vice.main import ViceDaemon
        cfg = Config()
        cfg.recording.audio_tracks = ['default_output', 'app:Discord']
        stub = SimpleNamespace(cfg=cfg)
        before = ViceDaemon._recording_signature(stub)
        cfg.recording.audio_track_volumes = {'app:Discord': 1.5}
        self.assertEqual(ViceDaemon._recording_signature(stub), before)
        cfg.recording.audio_tracks = ['default_output']
        self.assertNotEqual(ViceDaemon._recording_signature(stub), before)


def _apps_rc(tracks, mix_first=False, mic=True):
    from vice.config import RecordingConfig
    rc = RecordingConfig()
    rc.capture_audio = True
    rc.capture_microphone = mic
    rc.audio_tracks = list(tracks)
    rc.audio_tracks_mix_first = mix_first
    return rc


class EverythingElseTests(unittest.TestCase):
    """Recording Desktop next to an app's own track put the app on both, so
    muting its track in the editor left a call or music in the clip.
    Everything else records every app without a track of its own."""

    def test_it_records_every_app_except_the_ones_with_their_own_track(self):
        from vice.recorder import _gsr_audio_args
        args = _gsr_audio_args(_apps_rc(['everything_else', 'app:Spotify', 'app:vesktop']))
        self.assertEqual(args, ['-a', 'default_output|default_input',
                                '-a', 'app-inverse:Spotify|app-inverse:vesktop',
                                '-a', 'app:Spotify', '-a', 'app:vesktop',
                                '-a', 'default_input'])

    def test_it_always_comes_with_the_combined_track(self):
        """As track 1, which is all players and share links play, it would
        leave out every app with a track of its own."""
        from vice.recorder import _track_titles
        rc = _apps_rc(['everything_else', 'app:Spotify'], mix_first=False)
        self.assertEqual(_track_titles(rc), ['All audio', 'Everything else', 'Spotify', 'Microphone'])
        self.assertEqual(_track_titles(_apps_rc(['default_output', 'app:Spotify'], mix_first=False)),
                         ['Desktop', 'Spotify', 'Microphone'])

    def test_gsr_is_never_handed_the_vice_only_name(self):
        from vice.recorder import _gsr_audio_args
        for tracks in (['everything_else'], ['everything_else|default_input', 'app:Discord'],
                       ['app:Discord', 'everything_else']):
            for mix_first in (False, True):
                with self.subTest(tracks=tracks, mix_first=mix_first):
                    args = _gsr_audio_args(_apps_rc(tracks, mix_first))
                    self.assertFalse(any('everything_else' in a for a in args), args)

    def test_with_no_app_tracks_it_is_the_desktop(self):
        from vice.recorder import _gsr_audio_args
        self.assertEqual(_gsr_audio_args(_apps_rc(['everything_else'], mic=False)),
                         ['-a', 'default_output'])

    def test_apps_are_named_once_whatever_their_case(self):
        from vice.recorder import _gsr_audio_args
        args = _gsr_audio_args(_apps_rc(['everything_else', 'app:Discord', 'app:discord'], mic=False))
        self.assertEqual(args[3], 'app-inverse:Discord')

    def test_the_combined_track_records_the_desktop_not_the_apps(self):
        """GSR refuses app: and app-inverse: on one track, and the desktop
        already holds every app."""
        from vice.recorder import _gsr_audio_args
        args = _gsr_audio_args(_apps_rc(['everything_else', 'app:Spotify'], mix_first=True))
        self.assertEqual(args[:2], ['-a', 'default_output|default_input'])
        for i in range(1, len(args), 2):
            parts = args[i].split('|')
            self.assertFalse(any(p.startswith('app:') for p in parts)
                             and any(p.startswith('app-inverse:') for p in parts), args[i])

    def test_it_is_dropped_with_desktop_audio_off(self):
        from vice.recorder import _gsr_audio_args
        rc = _apps_rc(['everything_else', 'app:Spotify'])
        rc.capture_audio = False
        with self.assertLogs('vice.recorder', 'WARNING'):
            self.assertEqual(_gsr_audio_args(rc), ['-a', 'default_input'])

    def test_a_volume_change_rebuilds_the_combined_track_from_every_part(self):
        from vice.recorder import _separate_track_layout, _track_volume_cmd
        layout, mixed = _separate_track_layout(
            _apps_rc(['everything_else', 'app:Spotify'], mix_first=True))
        cmd = _track_volume_cmd(Path('c.mp4'), Path('t.mp4'), layout, mixed,
                                [1.0, 1.0, 0.5, 1.0], [('aac', 0)] * 4)
        graph = cmd[cmd.index('-filter_complex') + 1]
        # Spotify is not on the Everything else track, so the combined track
        # is rebuilt with it rather than left out as a desktop app would be.
        self.assertIn('[m1][m2][m3]amix=inputs=3', graph)

    def test_the_source_list_offers_it(self):
        from unittest import mock
        from vice.recorder import list_gsr_audio_sources
        with mock.patch('vice.recorder._has', return_value=True), \
             mock.patch('vice.recorder._run_command_capture', return_value=(0, 'Spotify\n')):
            sources = list_gsr_audio_sources()['sources']
        kinds = {s['id']: s['kind'] for s in sources}
        self.assertEqual(kinds['everything_else'], 'others')


class TrackTitleTests(unittest.TestCase):
    def test_tracks_are_named_after_their_sources(self):
        from vice.recorder import _track_titles
        self.assertEqual(
            _track_titles(_apps_rc(['everything_else', 'app:Spotify', 'app:vesktop'], True)),
            ['All audio', 'Everything else', 'Spotify', 'vesktop', 'Microphone'])
        self.assertEqual(
            _track_titles(_apps_rc(['default_output|default_input', 'app-inverse:Discord',
                                    'device:alsa_output.usb.monitor'], mic=False)),
            ['Desktop + Microphone', 'Everything except Discord', 'alsa_output.usb'])
        self.assertEqual(_track_titles(_apps_rc([])), [])

    def test_every_part_starts_on_when_each_app_is_on_one_track(self):
        from vice.recorder import _track_titles, audio_track_parts
        rc = _apps_rc(['everything_else', 'app:Spotify', 'app:vesktop'], True)
        self.assertEqual(audio_track_parts(rc, _track_titles(rc)),
                         [False, True, True, True, True])

    def test_an_app_the_desktop_track_holds_is_not_a_part(self):
        from vice.recorder import _track_titles, audio_track_parts
        rc = _apps_rc(['default_output', 'app:Discord'], True)
        self.assertEqual(audio_track_parts(rc, _track_titles(rc)),
                         [False, True, False, True])

    def test_a_clip_recorded_some_other_way_has_no_parts(self):
        from vice.recorder import audio_track_parts
        rc = _apps_rc(['everything_else', 'app:Spotify'])
        self.assertIsNone(audio_track_parts(rc, ['Desktop', 'Discord', 'Microphone']))
        self.assertIsNone(audio_track_parts(rc, ['', '', '']))
        self.assertIsNone(audio_track_parts(_apps_rc([]), ['']))

    def test_everything_else_says_what_the_tracks_hold_whatever_the_settings(self):
        """Automatic app tracks change from one start to the next, so their
        clips are read by their titles."""
        from vice.recorder import audio_track_parts
        rc = _apps_rc(['everything_else', 'app:Spotify'])
        self.assertEqual(
            audio_track_parts(rc, ['All audio', 'Everything else', 'Discord', 'Microphone']),
            [False, True, True, True])
        self.assertIsNone(audio_track_parts(rc, ['Everything else', 'Desktop', 'Discord']))

    def test_generic_handler_names_are_not_titles(self):
        from vice.media import _track_title
        for handler in ('SoundHandler', 'Core Media Audio', 'GPAC ISO Audio Handler',
                        'L-SMASH Audio Handler', 'ISO Media file produced by Google Inc.'):
            with self.subTest(handler=handler):
                self.assertEqual(_track_title({'handler_name': handler}), '')
        self.assertEqual(_track_title({'handler_name': 'Spotify'}), 'Spotify')
        self.assertEqual(_track_title({'name': 'Spotify', 'handler_name': 'SoundHandler'}), 'Spotify')
        self.assertEqual(_track_title({'title': 'Mic', 'handler_name': 'Spotify'}), 'Mic')

    @unittest.skipIf(_share is None, 'share server dependencies not installed')
    def test_the_clip_list_marks_the_parts(self):
        from types import SimpleNamespace
        rc = _apps_rc(['everything_else', 'app:Spotify'])
        stub = SimpleNamespace(cfg=SimpleNamespace(recording=rc))
        stub._audio_parts = lambda tracks: _share.ShareServer._audio_parts(stub, tracks)
        tracks = [{'index': i, 'title': t} for i, t in
                  enumerate(['Everything else', 'Spotify', 'Microphone'])]
        out = _share.ShareServer._audio_tracks_json(stub, tracks)
        self.assertEqual([t['part'] for t in out], [True, True, True])
        tracks[0]['title'] = 'Desktop'
        out = _share.ShareServer._audio_tracks_json(stub, tracks)
        self.assertFalse(any('part' in t for t in out))
        tracks = [{'index': i, 'title': t} for i, t in
                  enumerate(['All audio', 'Everything else', 'Spotify'])]
        out = _share.ShareServer._audio_tracks_json(stub, tracks)
        self.assertEqual([(t['part'], t['combined']) for t in out],
                         [(False, True), (True, False), (True, False)])


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg not installed')
class LabelTracksTests(unittest.IsolatedAsyncioTestCase):
    def _clip(self, path, tracks):
        inputs, maps = [], ['-map', '0:v']
        for n in range(1, tracks + 1):
            inputs += ['-f', 'lavfi', '-i', f'sine=frequency={220 * n}:duration=1']
            maps += ['-map', f'{n}:a']
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'color=size=128x72:rate=10:duration=1', *inputs, *maps,
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac',
                        str(path)], check=True, timeout=30)
        return path

    async def test_titles_are_written_and_survive_a_trim(self):
        from vice.recorder import _label_audio_tracks
        rc = _apps_rc(['everything_else', 'app:Spotify'])
        expected = ['All audio', 'Everything else', 'Spotify', 'Microphone']
        for ext in ('mp4', 'mkv'):
            with self.subTest(container=ext), tempfile.TemporaryDirectory() as directory:
                path = self._clip(Path(directory) / f'clip.{ext}', 4)
                await _label_audio_tracks(path, rc)
                meta = await probe_media(path)
                self.assertEqual([t['title'] for t in meta['audio_tracks']], expected)
                # A trim remuxes the clip, which drops an MP4 title.
                subprocess.run(['ffmpeg', '-v', 'error', '-ss', '0.2', '-i', str(path),
                                '-map', '0', '-c', 'copy', str(path.with_suffix(f'.t.{ext}'))],
                               check=True, timeout=15)
                meta = await probe_media(path.with_suffix(f'.t.{ext}'))
                self.assertEqual([t['title'] for t in meta['audio_tracks']], expected)
                self.assertEqual(sorted(p.name for p in Path(directory).iterdir()),
                                 sorted([f'clip.{ext}', f'clip.t.{ext}']))

    async def test_a_clip_that_does_not_match_the_tracks_is_left_alone(self):
        from vice.recorder import _label_audio_tracks
        with tempfile.TemporaryDirectory() as directory:
            path = self._clip(Path(directory) / 'clip.mp4', 2)
            before = path.read_bytes()
            await _label_audio_tracks(path, _apps_rc(['everything_else', 'app:Spotify']))
            await _label_audio_tracks(path, _apps_rc([]))
            self.assertEqual(path.read_bytes(), before)

    async def test_the_users_own_audio_flags_win(self):
        from vice.recorder import _label_audio_tracks
        rc = _apps_rc(['everything_else', 'app:Spotify'])
        rc.gsr_args = '-a default_output -a default_output -a app:Spotify -a default_input'
        with tempfile.TemporaryDirectory() as directory:
            path = self._clip(Path(directory) / 'clip.mp4', 4)
            before = path.read_bytes()
            await _label_audio_tracks(path, rc)
            self.assertEqual(path.read_bytes(), before)


class AutoAppTracksTests(unittest.TestCase):
    """Every app heard gets a track of its own, with nothing to list by hand."""

    def setUp(self):
        from vice import recorder
        self.recorder = recorder
        recorder.AUDIO_APPS_PATH.unlink(missing_ok=True)
        self.addCleanup(recorder.AUDIO_APPS_PATH.unlink, missing_ok=True)

    def _detect(self, listing):
        from unittest import mock
        with mock.patch.object(self.recorder, '_gsr_prefix', return_value=['gpu-screen-recorder']), \
             mock.patch.object(self.recorder, '_run_command_capture', return_value=(0, listing)):
            return self.recorder._detect_app_tracks()

    def test_apps_playing_now_get_tracks_and_are_remembered(self):
        self.assertEqual(self._detect('vesktop\nSpotify\npaplay\n'),
                         ['everything_else', 'app:Spotify', 'app:vesktop'])
        # Discord started after the recorder last did: still separated, and
        # the apps heard before keep theirs.
        self.assertEqual(self._detect('Garry\'s Mod\n'),
                         ['everything_else', "app:Garry's Mod", 'app:Spotify', 'app:vesktop'])
        self.assertEqual(self.recorder.remembered_audio_apps(), ["Garry's Mod", 'Spotify', 'vesktop'])

    def test_one_track_per_app_whatever_the_case(self):
        self._detect('Spotify\n')
        self.assertEqual(self._detect('spotify\n'), ['everything_else', 'app:Spotify'])

    def test_old_apps_are_forgotten_and_the_count_is_capped(self):
        import json, time
        now = time.time()
        apps = {f'App{n:02d}': now - n for n in range(15)}
        apps['Ancient'] = now - 31 * 24 * 3600
        self.recorder.AUDIO_APPS_PATH.write_text(json.dumps({'version': 1, 'apps': apps}))
        names = self.recorder.remembered_audio_apps()
        self.assertEqual(len(names), self.recorder.AUTO_APP_LIMIT)
        self.assertNotIn('Ancient', names)
        self.assertIn('App00', names)
        self.assertNotIn('App14', names)

    def test_the_recorder_gets_every_app_a_combined_track_and_the_mic(self):
        from vice.recorder import _effective_rc, _gsr_audio_args, _track_titles
        rc = _apps_rc([])
        rc.auto_app_tracks = True
        eff = _effective_rc(rc, ['everything_else', 'app:Spotify', 'app:vesktop'])
        self.assertEqual(_gsr_audio_args(eff), [
            '-a', 'default_output|default_input',
            '-a', 'app-inverse:Spotify|app-inverse:vesktop',
            '-a', 'app:Spotify', '-a', 'app:vesktop', '-a', 'default_input'])
        self.assertEqual(_track_titles(eff),
                         ['All audio', 'Everything else', 'Spotify', 'vesktop', 'Microphone'])
        # The user's own settings are left as they were.
        self.assertEqual(rc.audio_tracks, [])
        self.assertFalse(rc.audio_tracks_mix_first)

    def test_hand_picked_tracks_are_untouched_when_it_is_off(self):
        from vice.recorder import _effective_rc
        rc = _apps_rc(['default_output'])
        self.assertIs(_effective_rc(rc, ['everything_else', 'app:Spotify']), rc)


class TrackMixStoreTests(unittest.TestCase):
    """The viewer's track settings are kept beside the clip, not written into it."""

    def setUp(self):
        from vice import track_mix
        self.tm = track_mix
        for slug in ('clip', 'renamed', 'copy'):
            track_mix.delete(slug)

    def test_only_changed_tracks_are_kept_and_volumes_are_clamped(self):
        self.tm.save('clip', {0: (True, 1.0), 2: (False, 1.0), 3: (True, 1.5)})
        self.assertEqual(self.tm.load('clip'), {2: (False, 1.0), 3: (True, 1.5)})
        self.tm.save('clip', {0: (True, 1.0)})
        self.assertFalse(self.tm.sidecar_path('clip').exists())
        self.tm.TRACK_MIX_DIR.mkdir(parents=True, exist_ok=True)
        self.tm.sidecar_path('clip').write_text(
            '{"tracks": {"1": {"enabled": true, "volume": 9}, "x": {}, "2": {"enabled": "no", "volume": 1}}}')
        self.assertEqual(self.tm.load('clip'), {1: (True, 2.0)})

    def test_settings_follow_the_clip(self):
        self.tm.save('clip', {1: (False, 1.0)})
        self.tm.copy('clip', 'copy')
        self.tm.move('clip', 'renamed')
        self.assertEqual(self.tm.load('renamed'), {1: (False, 1.0)})
        self.assertEqual(self.tm.load('copy'), {1: (False, 1.0)})
        self.assertEqual(self.tm.load('clip'), {})
        self.tm.delete('renamed')
        self.assertEqual(self.tm.load('renamed'), {})


class SendLevelsTests(unittest.TestCase):
    TITLES = ['All audio', 'Everything else', 'Spotify', 'vesktop', 'Microphone']
    PARTS = [False, True, True, True, True]

    def _levels(self, settings, titles=TITLES, parts=PARTS):
        from vice.track_mix import send_levels
        return send_levels(titles, parts, settings)

    def test_a_clip_left_as_recorded_with_a_combined_track_goes_as_it_is(self):
        self.assertIsNone(self._levels({}))
        self.assertIsNone(self._levels({2: (True, 1.0)}))

    def test_a_switched_off_source_is_left_out_and_levels_apply(self):
        self.assertEqual(self._levels({2: (False, 1.0), 3: (True, 1.5)}),
                         {1: 1.0, 3: 1.5, 4: 1.0})

    def test_a_clip_without_the_combined_track_is_always_mixed(self):
        """Its first track is only Everything else, which is all a player or
        Discord plays."""
        self.assertEqual(self._levels({}, self.TITLES[1:], self.PARTS[1:]),
                         {0: 1.0, 1: 1.0, 2: 1.0, 3: 1.0})

    def test_a_track_another_one_holds_is_not_mixed_in_twice(self):
        self.assertEqual(self._levels({2: (False, 1.0)}, ['All audio', 'Desktop', 'Discord', 'Microphone'],
                                      [False, True, False, True]), {1: 1.0, 3: 1.0})

    def test_unknown_tracks_go_as_they_are_until_changed(self):
        self.assertIsNone(self._levels({}, ['', ''], None))
        self.assertEqual(self._levels({1: (False, 1.0)}, ['', ''], None), {0: 1.0})

    def test_the_mix_has_one_audio_track_and_copies_the_video(self):
        from vice.track_mix import mix_cmd
        cmd = mix_cmd(Path('c.mp4'), Path('t.mp4'), {1: 1.0, 3: 1.5}, [('aac', 128000)] * 5)
        graph = cmd[cmd.index('-filter_complex') + 1]
        self.assertIn('[0:a:3]aformat=channel_layouts=stereo,volume=1.5[s3]', graph)
        self.assertIn('[s1][s3]amix=inputs=2', graph)
        self.assertNotIn('0:a:2', graph)
        self.assertEqual([cmd[i + 1] for i, a in enumerate(cmd) if a == '-map'], ['0:v?', '[mix]'])
        self.assertEqual(cmd[cmd.index('-c:v') + 1], 'copy')
        self.assertIn('-an', mix_cmd(Path('c.mp4'), Path('t.mp4'), {}, [('aac', 0)]))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg not installed')
@unittest.skipIf(_share is None, 'share server dependencies not installed')
class SendWithTrackSettingsTests(unittest.IsolatedAsyncioTestCase):
    """What is sent (share link, Discord, copied file) leaves out the tracks
    switched off in the viewer, and the clip itself keeps them."""

    async def asyncSetUp(self):
        import socket
        from aiohttp import ClientSession
        from vice import track_mix
        from vice.config import Config, OutputConfig, SharingConfig
        from vice.recorder import _label_audio_tracks

        def port():
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                return sock.getsockname()[1]

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        clips = Path(self.tmp.name)
        self.cfg = Config(output=OutputConfig(directory=str(clips)),
                          sharing=SharingConfig(port=port(), public_port=port(), cloudflare_tunnel=False))
        self.path = clips / 'clip.mp4'
        exprs = ['0.1*sin(2*PI*440*t)+0.1*sin(2*PI*880*t)', '0.2*sin(2*PI*440*t)', '0.2*sin(2*PI*880*t)']
        inputs, maps = [], ['-map', '0:v']
        for n, expr in enumerate(exprs, start=1):
            inputs += ['-f', 'lavfi', '-i', f'aevalsrc={expr}:s=48000:d=2,aformat=channel_layouts=stereo']
            maps += ['-map', f'{n}:a']
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=128x72:rate=10:duration=2',
                        *inputs, *maps, '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '10',
                        '-c:a', 'aac', '-b:a', '128k', '-shortest', str(self.path)], check=True, timeout=30)
        await _label_audio_tracks(self.path, _apps_rc(['everything_else', 'app:Spotify'], True, mic=False))
        track_mix.delete('clip')
        self.addCleanup(track_mix.delete, 'clip')
        self.server = _share.ShareServer(self.cfg)
        await self.server.start()
        self.server.add_clip(self.path)
        self.client = ClientSession()
        self.local = f'http://127.0.0.1:{self.cfg.sharing.port}'

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.stop()

    async def _set(self, tracks):
        async with self.client.post(f'{self.local}/api/clips/clip/track-mix', json={'tracks': tracks}) as resp:
            return resp.status, await resp.json()

    async def _public_video(self):
        token = self.server.share_url('clip').rsplit('/', 1)[1]
        out = Path(self.tmp.name) / 'downloaded.mp4'
        url = f'http://127.0.0.1:{self.cfg.sharing.public_port}/v/{token}'
        async with self.client.get(url) as resp:
            self.assertEqual(resp.status, 200)
            out.write_bytes(await resp.read())
        return out

    async def test_spotify_switched_off_is_not_in_what_gets_sent(self):
        before = self.path.read_bytes()
        status, body = await self._set([{'index': 2, 'enabled': False, 'volume': 1}])
        self.assertEqual((status, body['track_mix']), (200, {'2': {'enabled': False, 'volume': 1.0}}))
        async with self.client.get(f'{self.local}/api/clips/clip') as resp:
            self.assertEqual((await resp.json())['track_mix'], {'2': {'enabled': False, 'volume': 1.0}})

        sent = await self._public_video()
        meta = await probe_media(sent)
        self.assertEqual(meta['audio_streams'], 1)
        self.assertGreater(_tone(sent, 0, 440), 0.02)
        self.assertLess(_tone(sent, 0, 880), 0.002)
        # Discord gets the same, and the clip itself keeps every track.
        discord = await self.server._prepare_discord_copy('clip', self.path)
        self.assertLess(_tone(discord, 0, 880), 0.002)
        self.assertEqual(self.path.read_bytes(), before)

    async def test_switching_it_back_on_sends_everything_again(self):
        await self._set([{'index': 2, 'enabled': False, 'volume': 1}])
        await self._public_video()
        await self._set([{'index': 2, 'enabled': True, 'volume': 1}])
        sent = await self._public_video()
        self.assertEqual(sent.read_bytes(), self.path.read_bytes())
        # The copy made for the old settings is cleared away.
        sends = self.path.parent / _share.SEND_SUBDIR
        self.assertEqual(list(sends.iterdir()) if sends.exists() else [], [])

    async def test_bad_settings_are_refused(self):
        for tracks in ([{'index': 9, 'enabled': False, 'volume': 1}],
                       [{'index': 1, 'enabled': 'no', 'volume': 1}],
                       [{'index': 1, 'enabled': True, 'volume': 3}]):
            status, _ = await self._set(tracks)
            self.assertEqual(status, 400)
