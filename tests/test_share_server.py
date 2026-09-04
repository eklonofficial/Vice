import asyncio
import json
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vice import __version__
from vice.config import Config, HotkeyConfig, OutputConfig, RecordingConfig, SharingConfig

try:
    from aiohttp import ClientSession
    from vice.share import ShareServer
except ModuleNotFoundError:
    ClientSession = None
    ShareServer = None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _stub_ffprobe(_: Path) -> dict:
    return {"width": 1920, "height": 1080, "duration": 4.2}


class _JsonRequest:
    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


@unittest.skipUnless(ShareServer is not None and ClientSession is not None, "aiohttp is not installed")
class ShareServerSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        root = Path(self.tmpdir.name)
        self.output_dir = root / "clips"
        self.output_dir.mkdir()
        self.thumb_dir = root / "thumbs"
        self.thumb_dir.mkdir()
        self.highlights_dir = root / "highlights"
        self.highlights_dir.mkdir()

        self.clip_path = self.output_dir / "test_clip.mp4"
        self.clip_path.write_bytes(b"not-a-real-mp4")

        self.thumb_path = self.thumb_dir / "test_clip.jpg"
        self.thumb_path.write_bytes(b"jpeg")

        self.local_port = _free_port()
        self.public_port = _free_port()
        while self.public_port == self.local_port:
            self.public_port = _free_port()

        async def _stub_make_thumb(_: Path, duration: float = 0.0) -> Path:
            return self.thumb_path

        self.triggered = asyncio.Event()

        self.patchers = [
            mock.patch("vice.share._local_ip", return_value="127.0.0.1"),
            mock.patch("vice.share.THUMB_DIR", self.thumb_dir),
            mock.patch("vice.share.HIGHLIGHTS_DIR", self.highlights_dir),
            mock.patch("vice.playlists.PLAYLISTS_PATH", root / "playlists.json"),
            mock.patch("vice.share.VIEWS_PATH", root / "views.json"),
            mock.patch("vice.share._ffprobe", new=_stub_ffprobe),
            mock.patch("vice.share._make_thumb", new=_stub_make_thumb),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        cfg = Config(
            output=OutputConfig(directory=str(self.output_dir)),
            sharing=SharingConfig(
                port=self.local_port,
                public_port=self.public_port,
                cloudflare_tunnel=False,
            ),
        )
        self.server = ShareServer(cfg)

        async def _trigger() -> None:
            self.triggered.set()

        self.server.trigger_clip_cb = _trigger
        self.server.get_status_cb = lambda: {"recording": True, "backend": "test"}

        await self.server.start()
        self.server.add_clip(self.clip_path)
        self.client = ClientSession()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.server.stop()

    async def test_local_control_server_exposes_ui_api_and_ws(self) -> None:
        local_base = self.server.local_base_url()
        self.assertEqual(local_base, f"http://127.0.0.1:{self.local_port}")

        async with self.client.get(f"{local_base}/api/clips") as resp:
            self.assertEqual(resp.status, 200)
            payload = await resp.json()
        self.assertEqual(payload["clips"][0]["slug"], "test_clip")
        self.assertEqual(
            payload["clips"][0]["share_url"],
            f"http://127.0.0.1:{self.public_port}/c/test_clip",
        )

        async with self.client.get(f"{local_base}/api/status") as resp:
            self.assertEqual(resp.status, 200)
            status = await resp.json()
        self.assertEqual(status["local_url"], local_base)
        self.assertEqual(status["public_url"], f"http://127.0.0.1:{self.public_port}")

        async with self.client.post(f"{local_base}/api/trigger") as resp:
            self.assertEqual(resp.status, 200)
        await asyncio.wait_for(self.triggered.wait(), timeout=1.0)

        with mock.patch(
            "vice.share.list_display_options",
            return_value={
                "backend": "gsr",
                "displays": [{"id": "DP-1", "label": "DP-1"}],
                "warning": None,
            },
        ):
            async with self.client.get(f"{local_base}/api/displays?backend=gsr") as resp:
                self.assertEqual(resp.status, 200)
                displays = await resp.json()
        self.assertEqual(displays["backend"], "gsr")
        self.assertEqual(displays["displays"][0]["id"], "DP-1")

        ws = await self.client.ws_connect(f"ws://127.0.0.1:{self.local_port}/ws")
        await ws.close()

    async def test_public_server_only_serves_share_routes(self) -> None:
        public_base = f"http://127.0.0.1:{self.public_port}"

        async with self.client.get(f"{public_base}/c/test_clip") as resp:
            self.assertEqual(resp.status, 200)
            html = await resp.text()
        self.assertIn(f"{public_base}/v/test_clip", html)

        async with self.client.get(f"{public_base}/v/test_clip") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "video/mp4")

        async with self.client.get(f"{public_base}/t/test_clip") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "image/jpeg")

    async def test_video_urls_are_versioned_and_uncacheable(self) -> None:
        # Slugs are not stable identities: deleted clip numbers get reused and
        # trims rewrite the file in place. A cached /v/ response can therefore
        # play a different video than the clip the UI shows.
        local_base = self.server.local_base_url()
        async with self.client.get(f"{local_base}/api/clips") as resp:
            payload = await resp.json()

        st = self.clip_path.stat()
        self.assertEqual(
            payload["clips"][0]["video_url"],
            f"/v/test_clip?v={st.st_size}-{st.st_mtime_ns}",
        )

        public_base = f"http://127.0.0.1:{self.public_port}"
        async with self.client.get(f"{public_base}/v/test_clip") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Cache-Control"), "no-cache")

    async def test_mkv_clips_are_listed_and_served(self) -> None:
        mkv = self.output_dir / "mkv_clip.mkv"
        mkv.write_bytes(b"not-a-real-mkv")
        self.server.add_clip(mkv)

        local_base = self.server.local_base_url()
        async with self.client.get(f"{local_base}/api/clips") as resp:
            payload = await resp.json()
        slugs = {c["slug"] for c in payload["clips"]}
        self.assertIn("mkv_clip", slugs)

        # The Content-Type must match the actual container: claiming
        # video/mp4 for Matroska confuses the browser's codec detection.
        public_base = f"http://127.0.0.1:{self.public_port}"
        async with self.client.get(f"{public_base}/v/mkv_clip") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "video/x-matroska")

    async def test_open_endpoint_hands_clip_to_system_player(self) -> None:
        local_base = self.server.local_base_url()
        with mock.patch(
            "vice.share.asyncio.create_subprocess_exec", new_callable=mock.AsyncMock
        ) as spawn:
            async with self.client.post(f"{local_base}/api/clips/test_clip/open") as resp:
                self.assertEqual(resp.status, 200)
                self.assertTrue((await resp.json())["ok"])
            await asyncio.sleep(0)  # let the fire-and-forget task run
            spawn.assert_called_once_with(
                "xdg-open", str(self.clip_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

            async with self.client.post(f"{local_base}/api/clips/missing/open") as resp:
                self.assertEqual(resp.status, 404)

        # Local control route only: the public server must never expose it.
        public_base = f"http://127.0.0.1:{self.public_port}"
        async with self.client.post(f"{public_base}/api/clips/test_clip/open") as resp:
            self.assertEqual(resp.status, 404)

    async def test_embed_page_carries_theme_color_and_video_metadata(self) -> None:
        public_base = self.server.public_base_url()
        async with self.client.get(f"{public_base}/c/test_clip") as resp:
            self.assertEqual(resp.status, 200)
            html = await resp.text()

        self.assertIn('name="theme-color"', html)
        self.assertIn('content="#0099ff"', html)
        self.assertIn(f'property="og:url"               content="{public_base}/c/test_clip"', html)
        self.assertIn(f'content="{public_base}/v/test_clip.mp4"', html)
        self.assertIn('property="og:video:type"        content="video/mp4"', html)
        # twitter:player must be an embeddable HTML page, not a raw file;
        # Discord renders no embed at all when the player card is unusable
        # (issues #77, #100). Video embeds ride on OpenGraph alone.
        self.assertNotIn("twitter:", html)

    async def test_video_route_accepts_container_suffix(self) -> None:
        # Embed pages link /v/<slug>.mp4 so unfurlers see a file extension.
        public_base = self.server.public_base_url()
        async with self.client.get(f"{public_base}/v/test_clip.mp4") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "video/mp4")

        async with self.client.get(f"{public_base}/v/missing.mp4") as resp:
            self.assertEqual(resp.status, 404)

    async def test_embed_page_honors_forwarded_proto(self) -> None:
        # Tunneled requests arrive as plain HTTP with X-Forwarded-Proto set
        # by cloudflared; embed URLs must use the visitor's scheme or
        # Discord rejects the video (issue #100).
        public_base = self.server.public_base_url()
        headers = {"X-Forwarded-Proto": "https", "Host": "clip.trycloudflare.com"}
        async with self.client.get(f"{public_base}/c/test_clip", headers=headers) as resp:
            self.assertEqual(resp.status, 200)
            html = await resp.text()

        self.assertIn('content="https://clip.trycloudflare.com/v/test_clip.mp4"', html)
        self.assertNotIn("http://clip.trycloudflare.com", html)

        # Plain LAN requests keep working without the header.
        async with self.client.get(f"{public_base}/c/test_clip") as resp:
            html = await resp.text()
        self.assertIn(f'content="{public_base}/v/test_clip.mp4"', html)

    async def test_embed_color_rejects_non_hex_values(self) -> None:
        self.server.cfg.sharing.embed_color = "<script>alert(1)</script>"
        public_base = self.server.public_base_url()
        async with self.client.get(f"{public_base}/c/test_clip") as resp:
            html = await resp.text()

        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn('content="#0099ff"', html)

    async def test_public_server_blocks_privileged_routes_and_mutation(self) -> None:
        public_base = f"http://127.0.0.1:{self.public_port}"

        async with self.client.get(f"{public_base}/") as resp:
            self.assertEqual(resp.status, 404)

        async with self.client.get(f"{public_base}/api/clips") as resp:
            self.assertEqual(resp.status, 404)

        async with self.client.get(f"{public_base}/api/playlists") as resp:
            self.assertEqual(resp.status, 404)

        async with self.client.post(f"{public_base}/api/trigger") as resp:
            self.assertEqual(resp.status, 404)

        async with self.client.get(f"{public_base}/ws") as resp:
            self.assertEqual(resp.status, 404)

        async with self.client.delete(f"{public_base}/api/clips/test_clip") as resp:
            self.assertEqual(resp.status, 404)

        self.assertTrue(self.clip_path.exists())


@unittest.skipUnless(ShareServer is not None and ClientSession is not None, "aiohttp is not installed")
class ClipCountTests(unittest.IsolatedAsyncioTestCase):
    """status.clips is the library size, not this process's save tally."""

    async def asyncSetUp(self) -> None:
        self.root = tempfile.TemporaryDirectory()
        root = Path(self.root.name)
        self.output_dir = root / "clips"
        self.output_dir.mkdir()
        cfg = Config(
            output=OutputConfig(directory=str(self.output_dir)),
            sharing=SharingConfig(
                port=_free_port(), public_port=_free_port(), cloudflare_tunnel=False
            ),
        )
        self.server = ShareServer(cfg)
        await self.server.start()
        for name in ("a", "b", "c"):
            path = self.output_dir / f"{name}.mp4"
            path.write_bytes(b"0" * 16)
            self.server.add_clip(path)
        self.client = ClientSession()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.server.stop()
        self.root.cleanup()

    async def test_clip_count_matches_the_library(self) -> None:
        self.assertEqual(self.server.clip_count(), 3)

    async def test_a_daemon_status_callback_cannot_hide_the_real_count(self) -> None:
        # The daemon merges its own dict over this payload. It used to send a
        # counter that only ever counted saves made by the running process, so
        # a daemon that had been up a while reported a handful against a
        # library of dozens (both in the UI and in `vice status`).
        self.server.get_status_cb = lambda: {"recording": True, "backend": "test"}
        async with self.client.get(
            f"{self.server.local_base_url()}/api/status"
        ) as resp:
            payload = await resp.json()
        self.assertEqual(payload["clips"], 3)

        async with self.client.get(
            f"{self.server.local_base_url()}/api/clips"
        ) as resp:
            listed = await resp.json()
        self.assertEqual(payload["clips"], len(listed["clips"]))


class PlaylistApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        root = Path(self.tmpdir.name)
        self.output_dir = root / "clips"
        self.output_dir.mkdir()
        self.thumb_dir = root / "thumbs"
        self.thumb_dir.mkdir()
        self.highlights_dir = root / "highlights"
        self.highlights_dir.mkdir()

        self.clip_path = self.output_dir / "Vice_Clip_1_Minecraft.mp4"
        self.clip_path.write_bytes(b"not-a-real-mp4")
        self.plain_path = self.output_dir / "Vice_Clip_2.mp4"
        self.plain_path.write_bytes(b"not-a-real-mp4")

        self.thumb_path = self.thumb_dir / "thumb.jpg"
        self.thumb_path.write_bytes(b"jpeg")

        self.local_port = _free_port()
        self.public_port = _free_port()
        while self.public_port == self.local_port:
            self.public_port = _free_port()

        async def _stub_make_thumb(_: Path, duration: float = 0.0) -> Path:
            return self.thumb_path

        self.patchers = [
            mock.patch("vice.share._local_ip", return_value="127.0.0.1"),
            mock.patch("vice.share.THUMB_DIR", self.thumb_dir),
            mock.patch("vice.share.HIGHLIGHTS_DIR", self.highlights_dir),
            mock.patch("vice.playlists.PLAYLISTS_PATH", root / "playlists.json"),
            mock.patch("vice.share.VIEWS_PATH", root / "views.json"),
            mock.patch("vice.share._ffprobe", new=_stub_ffprobe),
            mock.patch("vice.share._make_thumb", new=_stub_make_thumb),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        cfg = Config(
            output=OutputConfig(directory=str(self.output_dir)),
            sharing=SharingConfig(
                port=self.local_port,
                public_port=self.public_port,
                cloudflare_tunnel=False,
            ),
        )
        self.server = ShareServer(cfg)
        await self.server.start()
        self.client = ClientSession()
        self.base = self.server.local_base_url()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.server.stop()

    async def _playlists(self) -> list:
        async with self.client.get(f"{self.base}/api/playlists") as resp:
            self.assertEqual(resp.status, 200)
            return (await resp.json())["playlists"]

    async def test_startup_backfill_seeds_auto_playlists_from_filename_tags(self) -> None:
        playlists = await self._playlists()
        auto = [p for p in playlists if p["kind"] == "auto"]
        self.assertEqual(len(auto), 1)
        self.assertEqual(auto[0]["id"], "auto:minecraft")
        self.assertEqual(auto[0]["name"], "Minecraft")
        self.assertEqual(auto[0]["clip_slugs"], ["Vice_Clip_1_Minecraft"])

        async with self.client.get(f"{self.base}/api/clips") as resp:
            clips = (await resp.json())["clips"]
        games = {c["slug"]: c["game"] for c in clips}
        self.assertEqual(games["Vice_Clip_1_Minecraft"], "Minecraft")
        self.assertIsNone(games["Vice_Clip_2"])

    async def test_custom_playlist_crud_and_membership(self) -> None:
        async with self.client.post(f"{self.base}/api/playlists", json={
            "name": "Best of 2026", "emoji": "🔥",
            "color1": "#8b5cf6", "color2": "#3b0a74",
        }) as resp:
            self.assertEqual(resp.status, 200)
            playlist = (await resp.json())["playlist"]
        pid = playlist["id"]
        self.assertEqual(playlist["kind"], "custom")
        self.assertEqual(playlist["emoji"], "🔥")

        async with self.client.post(f"{self.base}/api/playlists/{pid}/clips",
                                    json={"slug": "Vice_Clip_2"}) as resp:
            self.assertEqual(resp.status, 200)
        async with self.client.post(f"{self.base}/api/playlists/{pid}/clips",
                                    json={"slug": "nonexistent"}) as resp:
            self.assertEqual(resp.status, 404)

        async with self.client.patch(f"{self.base}/api/playlists/{pid}",
                                     json={"name": "Bangers"}) as resp:
            self.assertEqual(resp.status, 200)

        playlists = await self._playlists()
        custom = [p for p in playlists if p["id"] == pid][0]
        self.assertEqual(custom["name"], "Bangers")
        self.assertEqual(custom["clip_slugs"], ["Vice_Clip_2"])

        async with self.client.delete(
                f"{self.base}/api/playlists/{pid}/clips/Vice_Clip_2") as resp:
            self.assertEqual(resp.status, 200)
        async with self.client.delete(f"{self.base}/api/playlists/{pid}") as resp:
            self.assertEqual(resp.status, 200)
        self.assertNotIn(pid, [p["id"] for p in await self._playlists()])

    async def test_auto_playlists_can_be_edited_and_deleted(self) -> None:
        async with self.client.patch(f"{self.base}/api/playlists/auto:minecraft",
                                     json={"name": "MC", "emoji": "⛏️"}) as resp:
            self.assertEqual(resp.status, 200)
            edited = (await resp.json())["playlist"]
        self.assertEqual(edited["name"], "MC")
        self.assertEqual(edited["kind"], "auto")

        async with self.client.delete(f"{self.base}/api/playlists/auto:minecraft") as resp:
            self.assertEqual(resp.status, 200)
        self.assertNotIn("auto:minecraft", [p["id"] for p in await self._playlists()])

        async with self.client.patch(f"{self.base}/api/playlists/missing",
                                     json={"name": "X"}) as resp:
            self.assertEqual(resp.status, 404)

    async def test_saved_clip_with_detected_game_lands_in_auto_playlist(self) -> None:
        new_clip = self.output_dir / "Vice_Clip_3.mp4"
        new_clip.write_bytes(b"not-a-real-mp4")
        self.server.add_clip(new_clip, game="Overwatch 2")
        await asyncio.sleep(0)

        playlists = await self._playlists()
        ow = [p for p in playlists if p["id"] == "auto:overwatch-2"]
        self.assertEqual(len(ow), 1)
        self.assertEqual(ow[0]["game"], "Overwatch 2")
        self.assertEqual(ow[0]["clip_slugs"], ["Vice_Clip_3"])

    async def test_rename_migrates_membership_and_delete_prunes_it(self) -> None:
        async with self.client.post(
                f"{self.base}/api/clips/Vice_Clip_1_Minecraft/rename",
                json={"name": "epic_dig"}) as resp:
            self.assertEqual(resp.status, 200)
            self.assertTrue((await resp.json())["ok"])

        playlists = await self._playlists()
        auto = [p for p in playlists if p["id"] == "auto:minecraft"][0]
        self.assertEqual(auto["clip_slugs"], ["epic_dig"])

        async with self.client.get(f"{self.base}/api/clips") as resp:
            clips = (await resp.json())["clips"]
        games = {c["slug"]: c["game"] for c in clips}
        self.assertEqual(games["epic_dig"], "Minecraft")

        async with self.client.delete(f"{self.base}/api/clips/epic_dig") as resp:
            self.assertEqual(resp.status, 200)
        self.assertNotIn("auto:minecraft", [p["id"] for p in await self._playlists()])

    async def _clip(self, slug: str) -> dict:
        async with self.client.get(f"{self.base}/api/clips") as resp:
            clips = (await resp.json())["clips"]
        return {c["slug"]: c for c in clips}[slug]

    async def test_rename_normalises_spaces_and_apostrophes(self) -> None:
        """#138: these used to be rejected outright, or land on disk and break
        every link and button on the card."""
        async with self.client.post(f"{self.base}/api/clips/Vice_Clip_2/rename",
                                    json={"name": "Insane wallbang"}) as resp:
            payload = await resp.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["slug"], "Insane-wallbang")
        self.assertEqual(payload["name"], "Insane-wallbang.mp4")
        self.assertTrue((self.output_dir / "Insane-wallbang.mp4").exists())

        async with self.client.post(f"{self.base}/api/clips/Insane-wallbang/rename",
                                    json={"name": "Bob's clip"}) as resp:
            payload = await resp.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["slug"], "Bobs-clip")

    async def test_rename_rejects_a_name_with_nothing_usable_left(self) -> None:
        async with self.client.post(f"{self.base}/api/clips/Vice_Clip_2/rename",
                                    json={"name": "???"}) as resp:
            payload = await resp.json()
        self.assertFalse(payload["ok"])
        self.assertTrue((self.output_dir / "Vice_Clip_2.mp4").exists())

    async def test_clip_urls_are_percent_encoded(self) -> None:
        """A slug is a filename, so a clip renamed outside Vice can still hold
        characters that would truncate the share link (#138)."""
        awkward = self.output_dir / "Bob's clip.mp4"
        awkward.write_bytes(b"not-a-real-mp4")
        self.server.add_clip(awkward)
        await asyncio.sleep(0)

        clip = await self._clip("Bob's clip")
        for key in ("share_url", "video_url", "thumb_url"):
            url = clip.get(key)
            if url:
                self.assertNotIn(" ", url, key)
                self.assertNotIn("'", url, key)
        self.assertIn("Bob%27s%20clip", clip["share_url"])
        self.assertIn("Bob%27s%20clip", clip["video_url"])

        # The embed page the share link points at has to render, and its
        # og:video must stay a single unbroken URL.
        async with self.client.get(f"{self.base}/c/Bob's clip") as resp:
            self.assertEqual(resp.status, 200)
            page = await resp.text()
        self.assertIn('content="http://127.0.0.1', page)
        self.assertIn("Bob%27s%20clip.mp4", page)
        self.assertNotIn("Bob's clip.mp4", page)

    async def test_view_counter_increments_and_follows_the_clip(self) -> None:
        for _ in range(2):
            async with self.client.post(f"{self.base}/api/clips/Vice_Clip_2/view") as resp:
                self.assertEqual(resp.status, 200)
                payload = await resp.json()
        self.assertEqual(payload["views"], 2)

        async with self.client.get(f"{self.base}/api/clips") as resp:
            clips = (await resp.json())["clips"]
        self.assertEqual({c["slug"]: c["views"] for c in clips}["Vice_Clip_2"], 2)

        async with self.client.post(f"{self.base}/api/clips/missing/view") as resp:
            self.assertEqual(resp.status, 404)

        async with self.client.post(f"{self.base}/api/clips/Vice_Clip_2/rename",
                                    json={"name": "watched"}) as resp:
            self.assertTrue((await resp.json())["ok"])
        async with self.client.get(f"{self.base}/api/clips") as resp:
            clips = (await resp.json())["clips"]
        self.assertEqual({c["slug"]: c["views"] for c in clips}["watched"], 2)

        # Deleting drops the counter so a reused clip number starts clean
        async with self.client.delete(f"{self.base}/api/clips/watched") as resp:
            self.assertEqual(resp.status, 200)
        self.assertNotIn("watched", self.server._views)

    async def test_playlist_mutations_broadcast_snapshots(self) -> None:
        messages: list[dict] = []

        async def _fake_broadcast(msg: dict) -> None:
            messages.append(msg)

        with mock.patch.object(self.server, "broadcast", side_effect=_fake_broadcast):
            async with self.client.post(f"{self.base}/api/playlists",
                                        json={"name": "Fails"}) as resp:
                self.assertEqual(resp.status, 200)

        changed = [m for m in messages if m["type"] == "playlists_changed"]
        self.assertEqual(len(changed), 1)
        self.assertIn("Fails", [p["name"] for p in changed[0]["playlists"]])


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class ShareServerBaseUrlTests(unittest.TestCase):
    def test_configured_public_base_url_beats_tunnel_and_bind_url(self) -> None:
        cfg = Config(
            sharing=SharingConfig(
                base_url="https://clips.example.com/",
                port=8765,
                public_port=8766,
                cloudflare_tunnel=False,
            )
        )
        server = ShareServer(cfg)
        server._tunnel_url = "https://ignored.trycloudflare.com"
        server._public_bind_url = "http://127.0.0.1:8766"

        self.assertEqual(server.public_base_url(), "https://clips.example.com")

    def test_lan_fallback_is_not_reported_as_publicly_reachable(self) -> None:
        """A LAN address looks like a working share link until a friend tries
        to open it, which is what confused the reporter of #105."""
        cfg = Config(sharing=SharingConfig(port=8765, public_port=8766))
        server = ShareServer(cfg)
        server._public_bind_url = "http://192.168.1.20:8766"

        self.assertEqual(server.public_base_url(), "http://192.168.1.20:8766")
        self.assertFalse(server.public_is_reachable())

        server._tunnel_url = "https://abc.trycloudflare.com"
        self.assertTrue(server.public_is_reachable())


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class ShareServerCopyFileTests(unittest.IsolatedAsyncioTestCase):
    """#117: copy the clip file itself so it can be pasted into Discord."""

    async def test_copies_a_file_uri_as_uri_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "Vice_Clip_1.mp4"
            clip.write_bytes(b"data")
            server = ShareServer(Config())
            server._clips = {"Vice_Clip_1": clip}

            spawned: dict = {}

            async def _fake_exec(*cmd, **kwargs):
                spawned["cmd"] = list(cmd)
                proc = mock.MagicMock()
                proc.stdin = mock.MagicMock()
                proc.stdin.drain = mock.AsyncMock()
                spawned["proc"] = proc
                return proc

            req = mock.MagicMock()
            req.match_info = {"slug": "Vice_Clip_1"}
            with mock.patch("vice.share.shutil.which", side_effect=lambda t: t == "wl-copy"):
                with mock.patch("asyncio.create_subprocess_exec", new=_fake_exec):
                    resp = await server._api_copy_file(req)

            self.assertEqual(json.loads(resp.text)["ok"], True)
            self.assertEqual(spawned["cmd"], ["wl-copy", "--type", "text/uri-list"])
            written = spawned["proc"].stdin.write.call_args[0][0].decode()
            self.assertEqual(written.strip(), clip.resolve().as_uri())

    async def test_falls_back_to_xclip_on_x11(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "Vice_Clip_1.mp4"
            clip.write_bytes(b"data")
            server = ShareServer(Config())
            server._clips = {"Vice_Clip_1": clip}

            spawned: dict = {}

            async def _fake_exec(*cmd, **kwargs):
                spawned["cmd"] = list(cmd)
                proc = mock.MagicMock()
                proc.stdin = mock.MagicMock()
                proc.stdin.drain = mock.AsyncMock()
                return proc

            req = mock.MagicMock()
            req.match_info = {"slug": "Vice_Clip_1"}
            with mock.patch("vice.share.shutil.which", side_effect=lambda t: t == "xclip"):
                with mock.patch("asyncio.create_subprocess_exec", new=_fake_exec):
                    await server._api_copy_file(req)

            self.assertEqual(
                spawned["cmd"],
                ["xclip", "-selection", "clipboard", "-t", "text/uri-list"],
            )

    async def test_reports_a_fixable_error_without_a_clipboard_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "Vice_Clip_1.mp4"
            clip.write_bytes(b"data")
            server = ShareServer(Config())
            server._clips = {"Vice_Clip_1": clip}

            req = mock.MagicMock()
            req.match_info = {"slug": "Vice_Clip_1"}
            with mock.patch("vice.share.shutil.which", return_value=None):
                resp = await server._api_copy_file(req)

            body = json.loads(resp.text)
            self.assertFalse(body["ok"])
            self.assertIn("wl-clipboard", body["error"])


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class PreviewProxyTests(unittest.IsolatedAsyncioTestCase):
    """H.265 clips can't decode in the native WebEngine, so the daemon hands
    the viewer/trim an H.264 preview proxy instead."""

    @staticmethod
    def _vcodec(path: Path) -> str:
        return subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True,
        ).stdout.strip()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
    async def test_non_h264_source_gets_a_cached_h264_proxy(self) -> None:
        import vice.share as share_mod
        from vice.media import probe_media
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # mpeg4 stands in for HEVC: not web-playable, and needs no libx265.
            src = root / "Vice_Clip_1.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=2",
                 "-c:v", "mpeg4", "-y", str(src)], check=True,
            )
            meta = await probe_media(src)
            self.assertEqual(meta["vcodec"], "mpeg4")

            with mock.patch.object(share_mod, "PROXY_DIR", root / "proxies"):
                proxy = await share_mod._make_preview_proxy(src, meta["vcodec"])
                self.assertIsNotNone(proxy)
                self.assertEqual(self._vcodec(proxy), "h264")

                # Second call reuses the cache instead of transcoding again.
                mtime = proxy.stat().st_mtime_ns
                again = await share_mod._make_preview_proxy(src, meta["vcodec"])
                self.assertEqual(again, proxy)
                self.assertEqual(again.stat().st_mtime_ns, mtime)

    async def test_h264_source_never_gets_a_proxy(self) -> None:
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(share_mod, "PROXY_DIR", Path(tmp) / "proxies"):
                proxy = await share_mod._make_preview_proxy(Path(tmp) / "x.mp4", "h264")
                self.assertIsNone(proxy)

    def test_purge_removes_cached_proxy(self) -> None:
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            proxy_dir = Path(tmp) / "proxies"
            proxy_dir.mkdir()
            (proxy_dir / "Vice_Clip_9_123_456.mp4").write_bytes(b"x")
            with mock.patch.object(share_mod, "PROXY_DIR", proxy_dir):
                share_mod._purge_slug_proxies("Vice_Clip_9")
            self.assertEqual(list(proxy_dir.glob("*.mp4")), [])


class DiscordCopyTests(unittest.IsolatedAsyncioTestCase):
    """The share sheet drags a copy sized for Discord's upload cap."""

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    def _recording_ffmpeg(self, captured: list) -> "callable":
        """Stand-in for ffmpeg that records the argv and writes the output."""
        async def fake_exec(*cmd, **_kwargs):
            captured.append(list(cmd))
            Path(cmd[-1]).write_bytes(b"encoded")
            return self._FakeProc()
        return fake_exec

    @staticmethod
    def _kbps(cmd: list) -> int:
        return int(cmd[cmd.index("-b:v") + 1].rstrip("k"))

    async def test_an_mp4_under_the_cap_is_dragged_as_is(self) -> None:
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "Vice_Clip_1.mp4"
            src.write_bytes(b"small")
            out = await share_mod._make_discord_copy(src, 10)
            self.assertEqual(out, src)
            self.assertFalse((root / share_mod.DISCORD_SUBDIR).exists())

    async def test_the_copy_lands_beside_the_clip(self) -> None:
        """A sandboxed drop target (Discord ships as a Flatpak) can only read
        the folders it was granted, and the clip's own is one of them. A copy
        in ~/.cache dropped as a path it cannot open, which Discord reported
        as an empty file."""
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "clips" / "Vice_Clip_1.mkv"
            src.parent.mkdir()
            src.write_bytes(b"not-a-real-mkv")
            with mock.patch("asyncio.create_subprocess_exec",
                            self._recording_ffmpeg([])):
                out = await share_mod._make_discord_copy(src, 10)
            self.assertEqual(out.parent, src.parent / share_mod.DISCORD_SUBDIR)

    async def test_bitrate_budget_shrinks_with_duration_and_audio_tracks(self) -> None:
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # .mkv, so the "already small enough" shortcut never applies and
            # every case actually goes through the budget.
            src = root / "Vice_Clip_1.mkv"
            src.write_bytes(b"not-a-real-mkv")
            captured: list = []
            with mock.patch("asyncio.create_subprocess_exec",
                            self._recording_ffmpeg(captured)):
                for duration, tracks in ((10, 1), (200, 1), (10, 2)):
                    share_mod._purge_clip_discord(src)
                    out = await share_mod._make_discord_copy(src, duration, tracks)
                    self.assertIsNotNone(out)

        short, long, two_tracks = (self._kbps(c) for c in captured)
        self.assertGreater(short, long)
        self.assertEqual(short - two_tracks, share_mod.DISCORD_AUDIO_KBPS)
        self.assertGreaterEqual(long, share_mod.DISCORD_MIN_VIDEO_KBPS)
        # The budget has to fit the cap, not just be smaller than the source.
        self.assertLess(short * 10 / 8192, share_mod.DISCORD_MAX_MB)

    async def test_the_copy_is_cached_and_transcoded_once(self) -> None:
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "Vice_Clip_1.mkv"
            src.write_bytes(b"not-a-real-mkv")
            captured: list = []
            with mock.patch("asyncio.create_subprocess_exec",
                            self._recording_ffmpeg(captured)):
                first = await share_mod._make_discord_copy(src, 10)
                again = await share_mod._make_discord_copy(src, 10)
            self.assertEqual(first, again)
            self.assertEqual(len(captured), 1)

    def test_purge_removes_the_cached_copy(self) -> None:
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "Vice_Clip_9.mp4"
            copies = Path(tmp) / share_mod.DISCORD_SUBDIR
            copies.mkdir()
            (copies / "Vice_Clip_9_123_456.mp4").write_bytes(b"x")
            (copies / "Vice_Clip_8_123_456.mp4").write_bytes(b"x")
            share_mod._purge_clip_discord(clip)
            self.assertEqual([p.name for p in copies.glob("*.mp4")],
                             ["Vice_Clip_8_123_456.mp4"])


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class TrimKeyframeTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for #172. A stream copy that starts between keyframes
    produces a track with nothing to decode from: it probes fine, tolerant
    players show it, and the app window stays black with no error."""

    KEYFRAME_EVERY = 300  # frames, so a 5 s source has keyframes only at 0 and 5

    @staticmethod
    def _keyframe_count(path: Path) -> int:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
             "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True,
        ).stdout
        return len([ln for ln in out.splitlines() if ln.strip()])

    @staticmethod
    def _duration(path: Path) -> float:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True,
        ).stdout.strip()
        return float(out) if out else 0.0

    def _long_gop_source(self, root: Path) -> Path:
        src = root / "Vice_Clip_1.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=60:duration=6",
             "-c:v", "libx264", "-preset", "ultrafast",
             "-g", str(self.KEYFRAME_EVERY), "-keyint_min", str(self.KEYFRAME_EVERY),
             "-sc_threshold", "0", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", "-y", str(src)], check=True,
        )
        return src

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
    async def test_a_copy_between_keyframes_is_rejected(self) -> None:
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = self._long_gop_source(root)
            cut = root / "cut.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-ss", "2.0", "-i", str(src), "-t", "1.0",
                 "-c", "copy", "-y", str(cut)], check=True,
            )
            # Assert the probe answered before trusting its verdict: an
            # unanswerable probe is "" too, and a bare assertNotEqual on that
            # fails with nothing to read.
            self.assertIsNotNone(await share_mod._first_video_packet(cut),
                                 "the packet probe has to answer at all")
            problem = await share_mod._trim_result_problem(cut)
            self.assertNotEqual(problem, "", f"{cut.name} should have been rejected")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
    async def test_a_clean_copy_is_left_alone(self) -> None:
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            src = self._long_gop_source(Path(tmp))
            problem = await share_mod._trim_result_problem(src)
            self.assertEqual(problem, "", f"an untouched recording is not a problem: {problem}")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
    async def test_a_healthy_copy_on_an_edit_list_is_rejected(self) -> None:
        """The check this replaced asked ffprobe for the frame at timestamp
        zero. A stream copy expresses its in-point with a leading edit list and
        timestamps below zero, so ffprobe answered with nothing and every trim
        re-encoded, which is how H.265 clips came back as H.264."""
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = self._long_gop_source(root)
            cut = root / "cut.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-ss", "2.0", "-i", str(src), "-t", "1.0",
                 "-c", "copy", "-y", str(cut)], check=True,
            )
            packet = await share_mod._first_video_packet(cut)
            self.assertIsNotNone(packet, "the packet probe has to answer at all")
            pts, flags = packet
            if pts < 0:
                self.assertIn("K", flags, "ffmpeg kept the leading keyframe")
                self.assertEqual(
                    await share_mod._trim_result_problem(cut),
                    "starts on an edit list before zero",
                )

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
    async def test_an_unplayable_result_leaves_the_original_alone(self) -> None:
        """A trim Vice cannot decode must not reach the user's clip. Silently
        replacing it is what left people with a black frame and no reason."""
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = self._long_gop_source(root)
            before = src.read_bytes()
            cfg = Config()
            cfg.output.directory = str(root)
            server = ShareServer(cfg)
            server._clips["Vice_Clip_1"] = src

            req = mock.MagicMock()
            req.match_info = {"slug": "Vice_Clip_1"}
            req.json = mock.AsyncMock(return_value={"start": 2.0, "end": 4.0})
            with mock.patch.object(share_mod, "_purge_slug_thumbs"), \
                 mock.patch.object(share_mod, "_purge_slug_proxies"), \
                 mock.patch.object(share_mod, "_trim_result_problem",
                                   new=mock.AsyncMock(return_value="starts between keyframes")), \
                 mock.patch.object(share_mod, "_trim_encoder_candidates",
                                   return_value=["libx264"]), \
                 mock.patch.object(server, "_broadcast_clip", new=mock.AsyncMock()):
                resp = await server._api_trim(req)

            body = json.loads(resp.text)
            self.assertFalse(body["ok"])
            self.assertIn("starts between keyframes", body["error"])
            self.assertEqual(src.read_bytes(), before)
            self.assertFalse(list(root.glob("*.trimming.*")))


    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
    async def test_trim_between_keyframes_still_yields_a_playable_exact_cut(self) -> None:
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = self._long_gop_source(root)
            cfg = Config()
            cfg.output.directory = str(root)
            server = ShareServer(cfg)
            server._clips["Vice_Clip_1"] = src

            req = mock.MagicMock()
            req.match_info = {"slug": "Vice_Clip_1"}
            req.json = mock.AsyncMock(return_value={"start": 2.0, "end": 4.0})
            with mock.patch.object(share_mod, "_purge_slug_thumbs"), \
                 mock.patch.object(share_mod, "_purge_slug_proxies"), \
                 mock.patch.object(server, "_broadcast_clip", new=mock.AsyncMock()):
                resp = await server._api_trim(req)

            self.assertTrue(json.loads(resp.text)["ok"])
            # Exact length the user asked for, and something to decode from.
            self.assertAlmostEqual(self._duration(src), 2.0, delta=0.2)
            self.assertGreaterEqual(self._keyframe_count(src), 1)



@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class TrimEncoderTests(unittest.IsolatedAsyncioTestCase):
    """#172: a trim that has to re-encode used to hardcode libx264, so every
    H.265 recording came back as H.264 whatever the user set Video codec to."""

    def setUp(self) -> None:
        import vice.share as share_mod
        share_mod._trim_encoder_cache.clear()
        self.addCleanup(share_mod._trim_encoder_cache.clear)

    def _candidates(self, vcodec: str, installed: set, nvidia: bool) -> list:
        import vice.share as share_mod
        with mock.patch.object(share_mod, "_available_encoders", return_value=installed), \
             mock.patch.object(share_mod, "_is_nvidia", return_value=nvidia):
            return share_mod._trim_encoder_candidates(vcodec)

    ALL = {"hevc_nvenc", "hevc_vaapi", "libx265", "libx264",
           "av1_nvenc", "av1_vaapi", "libsvtav1", "h264_nvenc", "h264_vaapi"}

    def test_the_clips_own_codec_comes_first(self) -> None:
        self.assertEqual(
            self._candidates("hevc", self.ALL, nvidia=True),
            ["hevc_nvenc", "hevc_vaapi", "libx265", "libx264"],
        )

    def test_av1_and_unknown_codecs_have_their_own_rows(self) -> None:
        self.assertEqual(
            self._candidates("av1", self.ALL, nvidia=True),
            ["av1_nvenc", "av1_vaapi", "libsvtav1", "libx264"],
        )
        self.assertEqual(
            self._candidates("", self.ALL, nvidia=True),
            ["h264_nvenc", "h264_vaapi", "libx264"],
        )

    def test_nvenc_is_dropped_without_an_nvidia_card(self) -> None:
        self.assertEqual(
            self._candidates("hevc", self.ALL, nvidia=False),
            ["hevc_vaapi", "libx265", "libx264"],
        )

    def test_software_only_ffmpeg_behaves_the_way_it_always_has(self) -> None:
        self.assertEqual(self._candidates("hevc", {"libx264"}, nvidia=True), ["libx264"])

    def test_an_unanswerable_probe_still_offers_the_whole_row(self) -> None:
        # ffmpeg -encoders failing is no opinion, not "nothing is available".
        self.assertEqual(
            self._candidates("hevc", set(), nvidia=True),
            ["hevc_nvenc", "hevc_vaapi", "libx265", "libx264"],
        )

    def test_the_encoder_that_worked_is_tried_first_next_time(self) -> None:
        import vice.share as share_mod
        self._candidates("hevc", self.ALL, nvidia=True)
        share_mod._remember_trim_encoder("hevc", "libx265")
        self.assertEqual(
            share_mod._trim_encoder_candidates("hevc"),
            ["libx265", "hevc_nvenc", "hevc_vaapi", "libx264"],
        )

    def test_each_encoder_gets_the_arguments_its_scale_needs(self) -> None:
        import vice.share as share_mod
        pre, out = share_mod._trim_encoder_args("hevc_vaapi")
        self.assertEqual(pre, ["-vaapi_device", "/dev/dri/renderD128"])
        self.assertIn("format=nv12,hwupload", out)
        self.assertEqual(share_mod._trim_encoder_args("hevc_nvenc")[1][:2], ["-c:v", "hevc_nvenc"])
        # Audio survives whichever encoder wins.
        for encoder in ("hevc_nvenc", "hevc_vaapi", "libx265", "libx264", "libsvtav1"):
            self.assertIn("-c:a", share_mod._trim_encoder_args(encoder)[1])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
    async def test_a_trim_keeps_the_clips_codec(self) -> None:
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "Vice_Clip_1.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=6",
                 "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=none",
                 "-g", "300", "-keyint_min", "300", "-tag:v", "hev1",
                 "-pix_fmt", "yuv420p", "-y", str(src)], check=True,
            )
            cfg = Config()
            cfg.output.directory = str(root)
            server = ShareServer(cfg)
            server._clips["Vice_Clip_1"] = src

            req = mock.MagicMock()
            req.match_info = {"slug": "Vice_Clip_1"}
            req.json = mock.AsyncMock(return_value={"start": 2.0, "end": 4.0})
            # Software only, so the assertion does not depend on the machine.
            with mock.patch.object(share_mod, "_purge_slug_thumbs"), \
                 mock.patch.object(share_mod, "_purge_slug_proxies"), \
                 mock.patch.object(share_mod, "_available_encoders",
                                   return_value={"libx265", "libx264"}), \
                 mock.patch.object(share_mod, "_is_nvidia", return_value=False), \
                 mock.patch.object(server, "_broadcast_clip", new=mock.AsyncMock()):
                resp = await server._api_trim(req)

            self.assertTrue(json.loads(resp.text)["ok"], resp.text)
            codec = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(src)],
                capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(codec, "hevc")

@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class AutoPlaylistToggleTests(unittest.IsolatedAsyncioTestCase):
    """A detected game files the clip into a per-game auto playlist, unless the
    user turned that off."""

    def _server(self, tmp: str, enabled: bool) -> "ShareServer":
        cfg = Config(output=OutputConfig(directory=tmp, auto_playlist_by_game=enabled))
        server = ShareServer(cfg)
        # PlaylistStore loads in its constructor, so repointing the path is not
        # enough on its own: the developer's real playlists are already in
        # memory and leak into the assertions.
        server.playlists.path = Path(tmp) / "playlists.json"
        server.playlists.load()
        return server

    async def test_auto_playlist_created_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "Vice_Clip_1.mp4"
            clip.write_bytes(b"x")
            server = self._server(tmp, enabled=True)
            server.add_clip(clip, game="Outer Wilds")
            await asyncio.sleep(0)

            auto = [p for p in server.playlists.list_playlists() if p["kind"] == "auto"]
            self.assertEqual(len(auto), 1)
            self.assertEqual(auto[0]["id"], "auto:outer-wilds")
            self.assertIn("Vice_Clip_1", auto[0]["clip_slugs"])

    async def test_no_auto_playlist_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "Vice_Clip_1.mp4"
            clip.write_bytes(b"x")
            server = self._server(tmp, enabled=False)
            server.add_clip(clip, game="Outer Wilds")
            await asyncio.sleep(0)

            auto = [p for p in server.playlists.list_playlists() if p["kind"] == "auto"]
            self.assertEqual(auto, [])


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class ShareServerViewPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """View counts must survive a restart as reliably as highlights, even if
    the output dir is slow to appear. A startup scan used to purge them."""

    def _make_server(self, output_dir: Path, views_path: Path):
        cfg = Config(
            output=OutputConfig(directory=str(output_dir)),
            sharing=SharingConfig(port=_free_port(), public_port=_free_port(),
                                  cloudflare_tunnel=False),
        )
        return ShareServer(cfg)

    async def test_counts_survive_a_restart_with_an_empty_scan(self) -> None:
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            views_path = root / "views.json"
            views_path.write_text(json.dumps({"Vice_Clip_5": 7, "old_clip": 2}))
            missing_output = root / "not_mounted_yet"  # dir does not exist

            with mock.patch.object(share_mod, "VIEWS_PATH", views_path):
                server = self._make_server(missing_output, views_path)
                await server.start()
                try:
                    # Nothing was scanned, but no count was thrown away.
                    self.assertEqual(server._views.get("Vice_Clip_5"), 7)
                    self.assertEqual(server._views.get("old_clip"), 2)
                    self.assertEqual(
                        json.loads(views_path.read_text()),
                        {"Vice_Clip_5": 7, "old_clip": 2},
                    )
                finally:
                    await server.stop()

    async def test_reused_clip_number_starts_fresh(self) -> None:
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "clips"
            output.mkdir()
            views_path = root / "views.json"
            views_path.write_text(json.dumps({"Vice_Clip_5": 7}))

            with mock.patch.object(share_mod, "VIEWS_PATH", views_path):
                server = self._make_server(output, views_path)
                await server.start()
                try:
                    # A brand-new recording reuses the number 5.
                    new_clip = output / "Vice_Clip_5.mp4"
                    new_clip.write_bytes(b"x")
                    server.add_clip(new_clip)

                    self.assertNotIn("Vice_Clip_5", server._views)
                    self.assertEqual(json.loads(views_path.read_text()), {})
                finally:
                    await server.stop()


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class ShareServerAppStateTests(unittest.IsolatedAsyncioTestCase):
    """The tutorial-seen flag lives server-side so a native webview storage
    reset does not make the first-run tutorial reappear."""

    async def test_state_round_trips_across_a_fresh_server(self) -> None:
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "ui_state.json"
            with mock.patch.object(share_mod, "APP_STATE_PATH", state_path):
                server = ShareServer(Config())
                self.assertEqual(json.loads((await server._api_get_app_state(mock.MagicMock())).text), {})

                req = mock.MagicMock()
                req.json = mock.AsyncMock(return_value={"tutorial_seen": True})
                saved = await server._api_set_app_state(req)
                self.assertTrue(json.loads(saved.text)["ok"])

                # A brand-new server (fresh client, wiped localStorage) still sees it.
                fresh = ShareServer(Config())
                state = json.loads((await fresh._api_get_app_state(mock.MagicMock())).text)
                self.assertTrue(state["tutorial_seen"])

    async def test_set_rejects_non_object_bodies(self) -> None:
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(share_mod, "APP_STATE_PATH", Path(tmp) / "s.json"):
                server = ShareServer(Config())
                req = mock.MagicMock()
                req.json = mock.AsyncMock(return_value=["nope"])
                resp = await server._api_set_app_state(req)
                self.assertEqual(resp.status, 400)


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class ShareServerUiVersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ui_response_injects_current_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ui_path = Path(tmp) / "index.html"
            ui_path.write_text(
                '<link href="/styles/base.css?v=__VICE_VERSION__">'
                '<script src="/scripts/settings.js?v=__VICE_VERSION__"></script>'
                "<div>Version __VICE_VERSION__</div>",
                encoding="utf-8",
            )
            server = ShareServer(Config())

            with mock.patch("vice.share._resolve_ui_index", return_value=ui_path):
                response = await server._ui(mock.Mock())

        self.assertEqual(response.status, 200)
        self.assertIn(__version__, response.text)
        self.assertNotIn("__VICE_VERSION__", response.text)
        # The visible version stays plain; asset URLs carry a content
        # fingerprint so a rebuild at the same version still busts the
        # year-long immutable cache.
        self.assertIn(">Version 2.4.0<".replace("2.4.0", __version__), response.text)
        self.assertRegex(response.text, r"/scripts/settings\.js\?v=[\w.\-]+")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    async def test_ui_asset_rev_changes_when_a_file_changes(self) -> None:
        import vice.share as share_mod
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp) / "scripts"
            assets.mkdir()
            target = assets / "editor-core.js"
            target.write_text("one")

            with mock.patch.object(share_mod, "_resolve_ui_asset_dir",
                                   side_effect=lambda k: assets if k == "scripts" else None):
                share_mod._UI_ASSET_REV = None
                first = share_mod._ui_asset_rev()
                # Cached for the process lifetime.
                self.assertEqual(first, share_mod._ui_asset_rev())

                target.write_text("two, a different length")
                share_mod._UI_ASSET_REV = None
                self.assertNotEqual(first, share_mod._ui_asset_rev())
            share_mod._UI_ASSET_REV = None


@unittest.skipUnless(ShareServer is not None and ClientSession is not None, "aiohttp is not installed")
class ShareServerLegacyUrlCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        root = Path(self.tmpdir.name)
        self.output_dir = root / "clips"
        self.output_dir.mkdir()
        self.thumb_dir = root / "thumbs"
        self.thumb_dir.mkdir()
        self.highlights_dir = root / "highlights"
        self.highlights_dir.mkdir()

        self.clip_path = self.output_dir / "legacy_clip.mp4"
        self.clip_path.write_bytes(b"not-a-real-mp4")

        self.thumb_path = self.thumb_dir / "legacy_clip.jpg"
        self.thumb_path.write_bytes(b"jpeg")

        self.local_port = _free_port()
        self.public_port = _free_port()
        while self.public_port == self.local_port:
            self.public_port = _free_port()

        async def _stub_make_thumb(_: Path, duration: float = 0.0) -> Path:
            return self.thumb_path

        self.patchers = [
            mock.patch("vice.share._local_ip", return_value="127.0.0.2"),
            mock.patch("vice.share.THUMB_DIR", self.thumb_dir),
            mock.patch("vice.share.HIGHLIGHTS_DIR", self.highlights_dir),
            mock.patch("vice.playlists.PLAYLISTS_PATH", root / "playlists.json"),
            mock.patch("vice.share.VIEWS_PATH", root / "views.json"),
            mock.patch("vice.share._ffprobe", new=_stub_ffprobe),
            mock.patch("vice.share._make_thumb", new=_stub_make_thumb),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        cfg = Config(
            output=OutputConfig(directory=str(self.output_dir)),
            sharing=SharingConfig(
                port=self.local_port,
                public_port=self.public_port,
                cloudflare_tunnel=False,
            ),
        )
        self.server = ShareServer(cfg)

        await self.server.start()
        self.server.add_clip(self.clip_path)
        self.client = ClientSession()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.server.stop()

    async def test_legacy_pre_v1_0_12_share_urls_still_resolve(self) -> None:
        legacy_base = f"http://127.0.0.2:{self.local_port}"

        async with self.client.get(f"{legacy_base}/c/legacy_clip") as resp:
            self.assertEqual(resp.status, 200)
            html = await resp.text()
        self.assertIn(f"{legacy_base}/v/legacy_clip", html)

        async with self.client.get(f"{legacy_base}/v/legacy_clip") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "video/mp4")

        async with self.client.get(f"{legacy_base}/t/legacy_clip") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "image/jpeg")

    async def test_legacy_origin_still_blocks_ui_and_api_routes(self) -> None:
        legacy_base = f"http://127.0.0.2:{self.local_port}"

        async with self.client.get(f"{legacy_base}/") as resp:
            self.assertEqual(resp.status, 404)

        async with self.client.get(f"{legacy_base}/api/clips") as resp:
            self.assertEqual(resp.status, 404)


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class ShareServerDisplayApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_get_displays_returns_backend_options_and_selected_value(self) -> None:
        server = ShareServer(Config(recording=RecordingConfig(display="DP-1", backend="auto")))
        request = mock.Mock(query={"backend": "gsr"})

        with mock.patch(
            "vice.share.list_display_options",
            return_value={
                "backend": "gsr",
                "displays": [{"id": "DP-1", "label": "DP-1"}],
                "warning": None,
            },
        ):
            response = await server._api_get_displays(request)

        payload = json.loads(response.text)
        self.assertEqual(payload["backend"], "gsr")
        self.assertEqual(payload["selected"], "DP-1")
        self.assertEqual(payload["displays"][0]["id"], "DP-1")

    async def test_api_get_audio_sources_returns_gsr_sources_and_selected_value(self) -> None:
        server = ShareServer(Config(recording=RecordingConfig(gsr_audio_source="app:Firefox")))
        request = mock.Mock()

        with mock.patch(
            "vice.share.list_gsr_audio_sources",
            return_value={
                "sources": [{"id": "app:Firefox", "label": "Application: Firefox"}],
                "warning": None,
            },
        ):
            response = await server._api_get_audio_sources(request)

        payload = json.loads(response.text)
        self.assertEqual(payload["selected"], "app:Firefox")
        self.assertEqual(payload["sources"][0]["id"], "app:Firefox")


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class ShareServerConfigApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_set_config_saves_clip_presets_and_grows_buffer(self) -> None:
        server = ShareServer(
            Config(
                recording=RecordingConfig(buffer_duration=60, clip_duration=15),
                hotkeys=HotkeyConfig(clip="KEY_F9"),
            )
        )
        request = _JsonRequest({
            "hotkeys": {
                "clip_presets": [{"key": "KEY_F6", "duration": 120}],
            },
        })

        with mock.patch("vice.config.load", return_value=server.cfg):
            with mock.patch("vice.config.save") as save_mock:
                response = await server._api_set_config(request)

        payload = json.loads(response.text)
        saved_cfg = save_mock.call_args.args[0]
        self.assertTrue(payload["ok"])
        self.assertEqual(saved_cfg.hotkeys.clip_presets[0].key, "KEY_F6")
        self.assertEqual(saved_cfg.hotkeys.clip_presets[0].duration, 120)
        self.assertEqual(saved_cfg.recording.buffer_duration, 120)

    async def test_api_set_config_clamps_oversized_durations(self) -> None:
        server = ShareServer(Config(recording=RecordingConfig()))
        request = _JsonRequest({
            "recording": {"buffer_duration": 999999, "clip_duration": 99999},
        })

        with mock.patch("vice.config.load", return_value=server.cfg):
            with mock.patch("vice.config.save") as save_mock:
                response = await server._api_set_config(request)

        payload = json.loads(response.text)
        saved_cfg = save_mock.call_args.args[0]
        self.assertTrue(payload["ok"])
        self.assertEqual(saved_cfg.recording.buffer_duration, 1800)
        self.assertEqual(saved_cfg.recording.clip_duration, 1800)

    async def test_api_set_config_rejects_duplicate_clip_hotkeys(self) -> None:
        server = ShareServer(Config(hotkeys=HotkeyConfig(clip="KEY_F9")))
        request = _JsonRequest({
            "hotkeys": {
                "clip": "KEY_F9",
                "clip_presets": [{"key": "KEY_F9", "duration": 60}],
            },
        })

        with mock.patch("vice.config.load", return_value=server.cfg):
            with mock.patch("vice.config.save") as save_mock:
                response = await server._api_set_config(request)

        payload = json.loads(response.text)
        self.assertEqual(response.status, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("duplicate clip hotkey", payload["error"])
        save_mock.assert_not_called()


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class ShareServerClipBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_clip_broadcasts_immediately_before_metadata_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clip_path = Path(tmp) / "clip.mp4"
            clip_path.write_bytes(b"clip")
            server = ShareServer(Config())
            messages: list[dict] = []

            async def _fake_broadcast(msg: dict) -> None:
                messages.append(msg)

            async def _slow_meta(_slug: str, _path: Path) -> dict:
                await asyncio.sleep(0.05)
                return {"width": 1920, "height": 1080, "duration": 6.5}

            async def _fake_thumb(_path: Path, duration: float = 0.0) -> Path:
                return Path(tmp) / "thumb.jpg"

            with mock.patch.object(server, "broadcast", side_effect=_fake_broadcast):
                with mock.patch.object(server, "_get_meta", side_effect=_slow_meta):
                    with mock.patch("vice.share._make_thumb", new=_fake_thumb):
                        server.add_clip(clip_path)
                        await asyncio.sleep(0)
                        self.assertTrue(messages)
                        self.assertEqual(messages[0]["type"], "clip_saved")
                        self.assertEqual(messages[0]["clip"]["duration"], 0)

                        await asyncio.sleep(0.06)

            self.assertEqual(messages[-1]["type"], "clip_saved")
            self.assertEqual(messages[-1]["clip"]["duration"], 6.5)


@unittest.skipUnless(ShareServer is not None and ClientSession is not None, "aiohttp is not installed")
class EditorApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        root = Path(self.tmpdir.name)
        self.output_dir = root / "clips"
        self.output_dir.mkdir()
        self.thumb_path = root / "thumb.jpg"
        self.thumb_path.write_bytes(b"jpeg")

        self.local_port = _free_port()
        self.public_port = _free_port()
        while self.public_port == self.local_port:
            self.public_port = _free_port()

        async def _stub_make_thumb(_: Path, duration: float = 0.0) -> Path:
            return self.thumb_path

        self.patchers = [
            mock.patch("vice.share._local_ip", return_value="127.0.0.1"),
            mock.patch("vice.share.THUMB_DIR", root / "thumbs"),
            mock.patch("vice.share.HIGHLIGHTS_DIR", root / "highlights"),
            mock.patch("vice.playlists.PLAYLISTS_PATH", root / "playlists.json"),
            mock.patch("vice.share.VIEWS_PATH", root / "views.json"),
            mock.patch("vice.share.EXPORT_WORK_DIR", root / "exports"),
            mock.patch("vice.share._make_thumb", new=_stub_make_thumb),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        cfg = Config(
            output=OutputConfig(directory=str(self.output_dir)),
            sharing=SharingConfig(
                port=self.local_port,
                public_port=self.public_port,
                cloudflare_tunnel=False,
            ),
        )
        self.server = ShareServer(cfg)
        self.server.editor_project.path = root / "editor_project.json"
        await self.server.start()
        self.client = ClientSession()
        self.base = self.server.local_base_url()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.server.stop()

    def _project(self, items: list) -> dict:
        return {
            "version": 1,
            "tracks": [
                {"id": "T1", "type": "text", "label": "T1"},
                {"id": "V1", "type": "video", "label": "V1"},
                {"id": "A1", "type": "audio", "label": "A1"},
            ],
            "items": items,
        }

    def _make_clip(self, name: str, seconds: float = 2.0) -> Path:
        path = self.output_dir / name
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=30:duration={seconds}",
             "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
             "-y", str(path)], check=True,
        )
        self.server.add_clip(path)
        return path

    async def test_project_round_trip_and_missing_clips(self) -> None:
        async with self.client.get(f"{self.base}/api/editor/project") as resp:
            payload = await resp.json()
        self.assertIsNone(payload["project"])

        project = self._project([
            {"id": "i1", "kind": "clip", "trackId": "V1", "clipId": "ghost",
             "start": 0, "dur": 5, "offset": 0},
        ])
        async with self.client.post(f"{self.base}/api/editor/project",
                                    json=project) as resp:
            self.assertEqual(resp.status, 200)

        # Autosave keeps items whose clip vanished; the GET reports them.
        async with self.client.get(f"{self.base}/api/editor/project") as resp:
            payload = await resp.json()
        self.assertEqual(payload["project"]["items"][0]["clipId"], "ghost")
        self.assertEqual(payload["missing"], ["ghost"])

        async with self.client.post(f"{self.base}/api/editor/project",
                                    json={"tracks": "nope"}) as resp:
            self.assertEqual(resp.status, 400)

    async def test_rename_and_delete_migrate_the_project(self) -> None:
        clip = self.output_dir / "Vice_Clip_1.mp4"
        clip.write_bytes(b"not-a-real-mp4")
        self.server.add_clip(clip)
        self.server.editor_project.save(self._project([
            {"id": "i1", "kind": "clip", "trackId": "V1",
             "clipId": "Vice_Clip_1", "start": 0, "dur": 2, "offset": 0},
        ]))

        with mock.patch("vice.share._ffprobe", new=_stub_ffprobe):
            async with self.client.post(
                    f"{self.base}/api/clips/Vice_Clip_1/rename",
                    json={"name": "keeper"}) as resp:
                self.assertTrue((await resp.json())["ok"])
        items = self.server.editor_project.load()["items"]
        self.assertEqual(items[0]["clipId"], "keeper")

        async with self.client.delete(f"{self.base}/api/clips/keeper") as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(self.server.editor_project.load()["items"], [])

    async def test_export_rejects_invalid_projects(self) -> None:
        async with self.client.post(f"{self.base}/api/editor/export", json={
            "project": self._project([
                {"id": "i1", "kind": "clip", "trackId": "V1", "clipId": "ghost",
                 "start": 0, "dur": 5, "offset": 0},
            ]),
        }) as resp:
            self.assertEqual(resp.status, 400)
            payload = await resp.json()
        self.assertTrue(any("ghost is missing" in e for e in payload["errors"]))

        async with self.client.post(f"{self.base}/api/editor/export",
                                    json={"project": []}) as resp:
            self.assertEqual(resp.status, 400)

    async def test_export_returns_409_while_busy(self) -> None:
        pending: asyncio.Future = asyncio.get_running_loop().create_future()
        with mock.patch.object(self.server._exports, "_task", asyncio.ensure_future(pending)):
            async with self.client.post(f"{self.base}/api/editor/export", json={
                "project": self._project([]),
            }) as resp:
                self.assertEqual(resp.status, 409)
        pending.cancel()

    async def test_cancel_of_unknown_job_is_404(self) -> None:
        async with self.client.post(
                f"{self.base}/api/editor/export/exp-nope/cancel") as resp:
            self.assertEqual(resp.status, 404)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "ffmpeg not installed")
    async def test_export_renders_registers_and_reports_progress(self) -> None:
        self._make_clip("Vice_Clip_1.mp4")
        ws = await self.client.ws_connect(f"ws://127.0.0.1:{self.local_port}/ws")

        async with self.client.post(f"{self.base}/api/editor/export", json={
            "project": self._project([
                {"id": "i1", "kind": "clip", "trackId": "V1",
                 "clipId": "Vice_Clip_1", "start": 0, "dur": 1.5, "offset": 0.2},
                {"id": "i2", "kind": "text", "trackId": "T1", "start": 0,
                 "dur": 1, "text": "hi", "font": "display", "size": 40,
                 "weight": 700, "color": "#ffffff", "x": 50, "y": 20},
            ]),
            "filename": "my-edit",
        }) as resp:
            self.assertEqual(resp.status, 200)
            payload = await resp.json()
        self.assertTrue(payload["ok"])
        final = Path(payload["path"])
        self.assertEqual(final, self.output_dir / "my-edit.mp4")

        done = None
        for _ in range(50):
            msg = await asyncio.wait_for(ws.receive_json(), timeout=30)
            if msg["type"] == "export_done":
                done = msg
                break
            self.assertNotEqual(msg["type"], "export_error", msg)
        await ws.close()

        self.assertIsNotNone(done)
        self.assertEqual(done["clip"]["slug"], "my-edit")
        self.assertTrue(final.exists())
        self.assertIn("my-edit", self.server._clips)
        self.assertFalse((self.output_dir / ".my-edit.export.mp4").exists())

        # The temp work dir is cleaned up once the job ends.
        work_root = Path(self.tmpdir.name) / "exports"
        self.assertEqual(list(work_root.glob("*/")), [])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    async def test_export_to_custom_path_with_add_to_library(self) -> None:
        self._make_clip("Vice_Clip_1.mp4")
        dest = Path(self.tmpdir.name) / "elsewhere"
        ws = await self.client.ws_connect(f"ws://127.0.0.1:{self.local_port}/ws")

        async with self.client.post(f"{self.base}/api/editor/export", json={
            "project": self._project([
                {"id": "i1", "kind": "clip", "trackId": "V1",
                 "clipId": "Vice_Clip_1", "start": 0, "dur": 1, "offset": 0},
            ]),
            "location": "custom", "path": str(dest), "add_to_library": True,
        }) as resp:
            self.assertEqual(resp.status, 200)
            payload = await resp.json()

        done = None
        for _ in range(50):
            msg = await asyncio.wait_for(ws.receive_json(), timeout=30)
            if msg["type"] == "export_done":
                done = msg
                break
        await ws.close()

        self.assertTrue(Path(payload["path"]).exists())
        self.assertEqual(Path(payload["path"]).parent, dest)
        self.assertIsNotNone(done["clip"])
        self.assertTrue((self.output_dir / done["clip"]["name"]).exists())


class ExportManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_terminates_and_reports(self) -> None:
        from vice.editor import ExportManager
        messages: list[dict] = []

        async def _collect(msg: dict) -> None:
            messages.append(msg)

        with tempfile.TemporaryDirectory() as tmp:
            mgr = ExportManager(_collect)
            tmp_path = Path(tmp) / ".x.export.mp4"
            mgr.start("exp-1", ["sleep", "30"], 10.0, tmp_path,
                      Path(tmp) / "x.mp4")
            await asyncio.sleep(0.05)
            self.assertTrue(mgr.busy)
            self.assertTrue(await mgr.cancel("exp-1"))
            await asyncio.wait_for(mgr._task, timeout=5)

        self.assertEqual(messages[-1]["type"], "export_error")
        self.assertTrue(messages[-1]["canceled"])
        self.assertFalse(await mgr.cancel("exp-1"))

    async def test_failed_command_reports_stderr(self) -> None:
        from vice.editor import ExportManager
        messages: list[dict] = []

        async def _collect(msg: dict) -> None:
            messages.append(msg)

        with tempfile.TemporaryDirectory() as tmp:
            mgr = ExportManager(_collect)
            mgr.start("exp-2", ["ffmpeg", "-hide_banner", "-not-a-flag"],
                      10.0, Path(tmp) / ".x.export.mp4", Path(tmp) / "x.mp4")
            await asyncio.wait_for(mgr._task, timeout=10)

        self.assertEqual(messages[-1]["type"], "export_error")
        self.assertFalse(messages[-1]["canceled"])
        self.assertTrue(messages[-1]["error"])


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class UpdateCheckApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_route_reports_the_result_and_swallows_failures(self) -> None:
        server = ShareServer(Config())

        async def _found():
            return {"version": "2.5.0", "url": "https://x", "notes": []}

        server.check_update_cb = _found
        resp = await server._api_check_update(mock.MagicMock())
        self.assertEqual(json.loads(resp.text)["update"]["version"], "2.5.0")

        async def _boom():
            raise OSError("no route to host")

        server.check_update_cb = _boom
        resp = await server._api_check_update(mock.MagicMock())
        # A failed check is "nothing to report", never an error for the user.
        self.assertEqual(json.loads(resp.text), {"ok": True, "update": None})

    async def test_no_callback_is_not_an_error(self) -> None:
        server = ShareServer(Config())
        resp = await server._api_check_update(mock.MagicMock())
        self.assertEqual(json.loads(resp.text), {"ok": True, "update": None})

    async def test_update_check_is_configurable_and_defaults_on(self) -> None:
        from vice.config import UpdatesConfig
        self.assertTrue(UpdatesConfig().check_on_start)

        server = ShareServer(Config())
        request = _JsonRequest({"updates": {"check_on_start": False}})
        with mock.patch("vice.config.load", return_value=server.cfg):
            with mock.patch("vice.config.save") as save_mock:
                response = await server._api_set_config(request)

        self.assertTrue(json.loads(response.text)["ok"])
        self.assertFalse(save_mock.call_args.args[0].updates.check_on_start)


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class ShareServerTunnelTests(unittest.IsolatedAsyncioTestCase):
    """The serveo SSH fallback is gone: cloudflared or a clear error."""

    def _server(self) -> "ShareServer":
        return ShareServer(Config(sharing=SharingConfig(cloudflare_tunnel=True)))

    async def test_missing_cloudflared_broadcasts_tunnel_error(self) -> None:
        server = self._server()
        server.broadcast = mock.AsyncMock()

        with mock.patch("vice.share.shutil.which", return_value=None):
            with mock.patch(
                "vice.share.asyncio.create_subprocess_exec",
                new=mock.AsyncMock(),
            ) as exec_mock:
                await server._start_tunnel(8766)

        exec_mock.assert_not_awaited()
        msg = server.broadcast.await_args.args[0]
        self.assertEqual(msg["type"], "tunnel_error")
        self.assertIn("cloudflared", msg["error"])

    async def test_no_serveo_fallback_remains(self) -> None:
        self.assertFalse(hasattr(ShareServer, "_read_serveo_url"))

        # Even with ssh available, nothing must be spawned when
        # cloudflared is missing.
        server = self._server()
        server.broadcast = mock.AsyncMock()
        with mock.patch(
            "vice.share.shutil.which",
            side_effect=lambda name: "/usr/bin/ssh" if name == "ssh" else None,
        ):
            with mock.patch(
                "vice.share.asyncio.create_subprocess_exec",
                new=mock.AsyncMock(),
            ) as exec_mock:
                await server._start_tunnel(8766)

        exec_mock.assert_not_awaited()

    @staticmethod
    def _stdout_lines(lines: list) -> object:
        class _Stdout:
            def __init__(self) -> None:
                self._it = iter(lines)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration

        return _Stdout()

    async def test_banner_docs_url_is_not_the_tunnel_url(self) -> None:
        # cloudflared prints *.cloudflare.com docs links in its startup
        # banner; only the *.trycloudflare.com address is the tunnel
        # (issue #100).
        server = self._server()
        server.broadcast = mock.AsyncMock()

        proc = mock.Mock()
        proc.stdout = self._stdout_lines([
            b"2026-06-12T00:00:00Z INF Thank you for trying Cloudflare Tunnel. "
            b"Doing so, even in the recommended way, requires a Cloudflare account. "
            b"https://developers.cloudflare.com/cloudflare-one/connections/connect-apps\n",
            b"2026-06-12T00:00:01Z INF Requesting new quick Tunnel on trycloudflare.com...\n",
            b"2026-06-12T00:00:02Z INF |  https://brave-owl-clip.trycloudflare.com  |\n",
        ])
        proc.returncode = None
        server._tunnel_proc = proc

        await server._read_cloudflare_url()

        self.assertEqual(server._tunnel_url, "https://brave-owl-clip.trycloudflare.com")
        server.broadcast.assert_awaited_once_with(
            {"type": "tunnel_url", "url": "https://brave-owl-clip.trycloudflare.com"}
        )

    async def test_cloudflare_api_host_is_not_the_tunnel_url(self) -> None:
        # cloudflared's own API endpoint is https://api.trycloudflare.com, and
        # taking that as the tunnel pointed every share link at Cloudflare's
        # API, which answers "Method Not Allowed" (issue #143).
        server = self._server()
        server.broadcast = mock.AsyncMock()

        proc = mock.Mock()
        proc.stdout = self._stdout_lines([
            b"2026-08-06T00:00:00Z INF Requesting new quick Tunnel on trycloudflare.com...\n",
            b"2026-08-06T00:00:01Z ERR Failed to request quick Tunnel "
            b"error=\"POST https://api.trycloudflare.com/tunnel failed\"\n",
            b"2026-08-06T00:00:02Z INF |  https://brave-owl-clip.trycloudflare.com  |\n",
        ])
        proc.returncode = None
        server._tunnel_proc = proc

        await server._read_cloudflare_url()

        self.assertEqual(server._tunnel_url, "https://brave-owl-clip.trycloudflare.com")

    def test_quick_tunnel_url_picking(self) -> None:
        from vice.share import _quick_tunnel_url

        self.assertIsNone(_quick_tunnel_url("INF Requesting new quick Tunnel on trycloudflare.com..."))
        self.assertIsNone(_quick_tunnel_url("POST https://api.trycloudflare.com/tunnel"))
        self.assertIsNone(_quick_tunnel_url("https://developers.cloudflare.com/argo-tunnel"))
        self.assertEqual(
            _quick_tunnel_url("|  https://sensitive-clock-remote-tie.trycloudflare.com  |"),
            "https://sensitive-clock-remote-tie.trycloudflare.com",
        )
        # A real address on the same line as the API host still wins.
        self.assertEqual(
            _quick_tunnel_url("https://api.trycloudflare.com then https://foo-bar.trycloudflare.com"),
            "https://foo-bar.trycloudflare.com",
        )
        # A hyphenless host is a worse guess, not a rejected one, so a naming
        # change at Cloudflare cannot break sharing outright.
        self.assertEqual(
            _quick_tunnel_url("https://singleword.trycloudflare.com"),
            "https://singleword.trycloudflare.com",
        )

    async def test_first_tunnel_url_is_kept(self) -> None:
        # Later banner or metrics lines must not overwrite the address.
        server = self._server()
        server.broadcast = mock.AsyncMock()

        proc = mock.Mock()
        proc.stdout = self._stdout_lines([
            b"INF |  https://brave-owl-clip.trycloudflare.com  |\n",
            b"INF Thank you for trying Cloudflare Tunnel. "
            b"https://developers.cloudflare.com/cloudflare-one/connections/connect-apps\n",
            b"INF another https://stale-other-name.trycloudflare.com mention\n",
        ])
        proc.returncode = None
        server._tunnel_proc = proc

        await server._read_cloudflare_url()

        self.assertEqual(server._tunnel_url, "https://brave-owl-clip.trycloudflare.com")
        server.broadcast.assert_awaited_once()

    async def test_cloudflared_exit_without_url_reports_error(self) -> None:
        server = self._server()
        server.broadcast = mock.AsyncMock()

        class _Stdout:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        proc = mock.Mock()
        proc.stdout = _Stdout()
        proc.returncode = 1
        server._tunnel_proc = proc

        await server._read_cloudflare_url()

        msg = server.broadcast.await_args.args[0]
        self.assertEqual(msg["type"], "tunnel_error")
        self.assertIn("exited", msg["error"])


@unittest.skipUnless(ShareServer is not None and ClientSession is not None, "aiohttp is not installed")
class ImageApiTests(unittest.IsolatedAsyncioTestCase):
    """The Images section end to end: list, rename, annotate, delete (#171).

    Real PNGs from ffmpeg rather than stub bytes, because annotate replaces the
    file and the index has to follow it, and a stub would hide a path bug.
    """

    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        root = Path(self.tmpdir.name)

        self.clip_dir = root / "clips"
        self.clip_dir.mkdir()
        self.image_dir = root / "pictures"
        self.image_dir.mkdir()
        self.thumb_dir = root / "thumbs"
        self.thumb_dir.mkdir()

        self.local_port = _free_port()
        self.public_port = _free_port()
        while self.public_port == self.local_port:
            self.public_port = _free_port()

        thumb = self.thumb_dir / "stub.jpg"
        thumb.write_bytes(b"jpeg")

        async def _stub_make_thumb(_: Path, duration: float = 0.0) -> Path:
            return thumb

        self.patchers = [
            mock.patch("vice.share._local_ip", return_value="127.0.0.1"),
            mock.patch("vice.share.THUMB_DIR", self.thumb_dir),
            mock.patch("vice.share.HIGHLIGHTS_DIR", root / "highlights"),
            mock.patch("vice.playlists.PLAYLISTS_PATH", root / "playlists.json"),
            mock.patch("vice.share.VIEWS_PATH", root / "views.json"),
            mock.patch("vice.share._make_thumb", new=_stub_make_thumb),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        cfg = Config(
            output=OutputConfig(
                directory=str(self.clip_dir),
                image_directory=str(self.image_dir),
            ),
            sharing=SharingConfig(
                port=self.local_port,
                public_port=self.public_port,
                cloudflare_tunnel=False,
            ),
        )
        self.server = ShareServer(cfg)
        self.server.playlists.path = root / "playlists.json"
        self.server.playlists.load()
        await self.server.start()
        self.client = ClientSession()
        self.base = self.server.local_base_url()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.server.stop()

    def _png(self, name: str, size: str = "64x48") -> Path:
        path = self.image_dir / name
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", f"color=c=red:size={size}",
             "-frames:v", "1", "-y", str(path)], check=True,
        )
        return path

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    async def test_an_image_is_listed_renamed_and_deleted(self) -> None:
        shot = self._png("Vice_Shot_1.png")
        self.server.add_image(shot, game="Deadlock")
        await asyncio.sleep(0)

        async with self.client.get(f"{self.base}/api/images") as resp:
            self.assertEqual(resp.status, 200)
            listed = (await resp.json())["images"]
        self.assertEqual([i["slug"] for i in listed], ["Vice_Shot_1"])
        self.assertEqual(listed[0]["width"], 64)
        self.assertEqual(listed[0]["height"], 48)
        self.assertEqual(listed[0]["game"], "Deadlock")
        # No sharing for images, so nothing in the payload offers one.
        self.assertNotIn("share_url", listed[0])

        async with self.client.get(f"{self.base}/i/Vice_Shot_1") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Content-Type"], "image/png")

        async with self.client.post(
            f"{self.base}/api/images/Vice_Shot_1/rename", json={"name": "Nice shot"}
        ) as resp:
            renamed = await resp.json()
        self.assertEqual(renamed["slug"], "Nice-shot")
        self.assertTrue((self.image_dir / "Nice-shot.png").exists())
        self.assertFalse(shot.exists())

        async with self.client.delete(f"{self.base}/api/images/Nice-shot") as resp:
            self.assertEqual(resp.status, 200)
        self.assertFalse((self.image_dir / "Nice-shot.png").exists())
        self.assertEqual(self.server.image_count(), 0)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    async def test_annotating_replaces_the_file_in_place(self) -> None:
        shot = self._png("Vice_Shot_1.png")
        self.server.add_image(shot)
        await asyncio.sleep(0)
        before = shot.read_bytes()

        # A different picture of the same size, so a byte comparison proves the
        # write landed rather than proving the sizes differ.
        replacement = self.image_dir / "scratch.png"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=blue:size=64x48",
             "-frames:v", "1", "-y", str(replacement)], check=True,
        )
        payload = replacement.read_bytes()
        replacement.unlink()

        async with self.client.post(
            f"{self.base}/api/images/Vice_Shot_1/annotate",
            data=payload,
            headers={"Content-Type": "image/png"},
        ) as resp:
            self.assertEqual(resp.status, 200)
            updated = await resp.json()

        self.assertEqual(updated["slug"], "Vice_Shot_1")
        self.assertEqual(shot.read_bytes(), payload)
        self.assertNotEqual(shot.read_bytes(), before)
        # No .annotating leftover, and no second copy under a new name.
        self.assertEqual(sorted(p.name for p in self.image_dir.iterdir()), ["Vice_Shot_1.png"])

    async def test_annotate_refuses_a_body_that_is_not_a_png(self) -> None:
        shot = self.image_dir / "Vice_Shot_1.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\nstub")
        self.server.add_image(shot)
        await asyncio.sleep(0)

        async with self.client.post(
            f"{self.base}/api/images/Vice_Shot_1/annotate",
            data=b"<html>not a png</html>",
            headers={"Content-Type": "image/png"},
        ) as resp:
            self.assertEqual(resp.status, 400)
        self.assertEqual(shot.read_bytes(), b"\x89PNG\r\n\x1a\nstub")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    async def test_a_frame_grabbed_from_a_clip_becomes_an_image(self) -> None:
        clip = self.clip_dir / "Vice_Clip_1.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30:duration=3",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-y", str(clip)], check=True,
        )
        self.server.add_clip(clip, game="Deadlock")
        await asyncio.sleep(0)

        with mock.patch("vice.share.copy_image_to_clipboard", return_value=(True, "")):
            async with self.client.post(
                f"{self.base}/api/clips/Vice_Clip_1/frame", json={"time": 1.5}
            ) as resp:
                self.assertEqual(resp.status, 200)
                payload = await resp.json()

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["copied"])
        self.assertEqual(payload["width"], 160)
        self.assertEqual(payload["height"], 120)
        # The still inherits the clip's game, so it files itself the same way.
        self.assertEqual(payload["game"], "Deadlock")
        self.assertTrue((self.image_dir / f"{payload['slug']}.png").exists())

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    async def test_a_failed_copy_does_not_fail_the_frame_grab(self) -> None:
        clip = self.clip_dir / "Vice_Clip_1.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30:duration=2",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-y", str(clip)], check=True,
        )
        self.server.add_clip(clip)
        await asyncio.sleep(0)

        with mock.patch(
            "vice.share.copy_image_to_clipboard", return_value=(False, "no clipboard tool"),
        ):
            async with self.client.post(
                f"{self.base}/api/clips/Vice_Clip_1/frame", json={"time": 0.5}
            ) as resp:
                payload = await resp.json()

        # The picture is on disk either way. Only the paste would have failed.
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["copied"])
        self.assertEqual(payload["copy_error"], "no clipboard tool")
        self.assertTrue((self.image_dir / f"{payload['slug']}.png").exists())

    async def test_the_public_server_has_no_route_to_an_image(self) -> None:
        shot = self.image_dir / "Vice_Shot_1.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\nstub")
        self.server.add_image(shot)
        await asyncio.sleep(0)

        public = f"http://127.0.0.1:{self.public_port}"
        for path in ("/i/Vice_Shot_1", "/it/Vice_Shot_1", "/api/images"):
            async with self.client.get(f"{public}{path}") as resp:
                self.assertEqual(resp.status, 404, f"{path} is reachable from outside")

    async def test_images_and_clips_share_one_playlist(self) -> None:
        clip = self.clip_dir / "Vice_Clip_1.mp4"
        clip.write_bytes(b"x")
        shot = self.image_dir / "Vice_Shot_1.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\nstub")
        self.server.add_clip(clip, game="Outer Wilds")
        self.server.add_image(shot, game="Outer Wilds")
        await asyncio.sleep(0)

        auto = [p for p in self.server.playlists.list_playlists() if p["kind"] == "auto"]
        self.assertEqual(len(auto), 1, "one game, one playlist, both kinds in it")
        self.assertIn("Vice_Clip_1", auto[0]["clip_slugs"])
        self.assertIn("img:Vice_Shot_1", auto[0]["clip_slugs"])

    async def test_a_restart_does_not_empty_playlists_of_their_images(self) -> None:
        # backfill drops membership for anything outside the set it is given,
        # so handing it the clips alone would quietly delete every screenshot
        # from every playlist on the next start.
        shot = self.image_dir / "Vice_Shot_1.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\nstub")
        self.server.add_image(shot, game="Outer Wilds")
        await asyncio.sleep(0)

        self.server.rescan_images()
        self.server.playlists.backfill(
            set(self.server._clips) | {f"img:{s}" for s in self.server._images},
            {},
            seed_auto=True,
        )
        auto = [p for p in self.server.playlists.list_playlists() if p["kind"] == "auto"]
        self.assertEqual(len(auto), 1)
        self.assertIn("img:Vice_Shot_1", auto[0]["clip_slugs"])
