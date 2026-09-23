import asyncio
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from vice.editor import Source, build_export_cmd, validate_project
from vice.media import probe_media


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
