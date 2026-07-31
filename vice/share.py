"""
Vice share server — HTTP server that powers:
  • A local control UI/server  (/ → UI, /api/*, /ws, media)
  • A public share-only server  (/c/{slug}, /v/{slug}, /t/{slug})

WebSocket event types (server → client):
  {"type": "clip_saved",   "clip":  <clip_json>}
  {"type": "clip_deleted", "slug":  "..."}
  {"type": "status",       "recording": bool, "backend": "..."}
  {"type": "tunnel_url",   "url":   "https://..."}
  {"type": "tunnel_error", "error": "..."}
  {"type": "playlists_changed", "playlists": [<playlist_json>]}
  {"type": "export_progress", "job_id": "...", "progress": 0.42}
  {"type": "export_done",  "job_id": "...", "path": "...", "clip": <clip_json>|null}
  {"type": "export_error", "job_id": "...", "error": "...", "canceled": bool}
  {"type": "editor_project_changed"}
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import shutil
import socket
import subprocess
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Coroutine, Optional

from importlib.resources import files as _pkg_files

from aiohttp import WSMsgType, web

from . import __version__
from .editor import (EditorProjectStore, ExportBusy, ExportManager, Source,
                     build_export_cmd, default_export_name, project_extent,
                     sanitize_export_name, text_file_contents,
                     validate_project)
from .media import probe_media
from .playlists import PlaylistStore, build_tag_index
from .recorder import KEEP_ALL_STREAMS, list_display_options, list_gsr_audio_sources, list_capture_sources
from .runtime import actual_home_dir, resolve_path

log = logging.getLogger("vice.share")
UI_VERSION_TOKEN = "__VICE_VERSION__"


def _resolve_ui_index() -> Path | None:
    """Resolve the web UI index path across installed and source checkouts."""
    candidates: list[Path] = []

    # Preferred path for packaged installs.
    try:
        ui = _pkg_files("vice") / "ui" / "index.html"
        candidates.append(Path(str(ui)))
    except Exception as exc:
        log.debug("importlib.resources lookup for UI index failed: %s", exc)

    # Fallback for source checkouts / direct execution.
    candidates.append(Path(__file__).resolve().parent / "ui" / "index.html")

    for cand in candidates:
        try:
            if cand.exists() and cand.is_file():
                return cand
        except OSError:
            continue
    return None


# UI assets shipped inside the package (bundled, no network needed).
_UI_ASSET_KINDS = {"fonts", "styles", "scripts"}
_UI_CONTENT_TYPES = {
    "fonts":   "font/woff2",
    "styles":  "text/css; charset=utf-8",
    "scripts": "application/javascript; charset=utf-8",
}


_UI_ASSET_REV: Optional[str] = None


def _ui_asset_rev() -> str:
    """Cache key for the bundled UI assets: the version plus a fingerprint of
    the shipped files. Assets are served immutable for a year, so without the
    fingerprint a rebuild that keeps the same version (every test build during
    development) would keep serving the previous build's scripts."""
    global _UI_ASSET_REV
    if _UI_ASSET_REV is not None:
        return _UI_ASSET_REV
    latest = 0
    for kind in _UI_ASSET_KINDS:
        base = _resolve_ui_asset_dir(kind)
        if not base:
            continue
        try:
            for asset in base.iterdir():
                if asset.is_file():
                    st = asset.stat()
                    latest = max(latest, st.st_mtime_ns ^ st.st_size)
        except OSError:
            continue
    _UI_ASSET_REV = f"{__version__}-{latest & 0xffffffff:08x}" if latest else __version__
    return _UI_ASSET_REV


def _resolve_ui_asset_dir(kind: str) -> Path | None:
    if kind not in _UI_ASSET_KINDS:
        return None
    candidates: list[Path] = []
    try:
        candidates.append(Path(str(_pkg_files("vice") / "ui" / kind)))
    except Exception as exc:
        log.debug("importlib.resources lookup for UI dir %s failed: %s", kind, exc)
    candidates.append(Path(__file__).resolve().parent / "ui" / kind)
    for cand in candidates:
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return None


def _resolve_ui_asset(kind: str, name: str) -> Path | None:
    """Resolve a bundled UI asset (only allows known kinds + simple filenames)."""
    if kind not in _UI_ASSET_KINDS:
        return None
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    candidates: list[Path] = []
    try:
        f = _pkg_files("vice") / "ui" / kind / name
        candidates.append(Path(str(f)))
    except Exception as exc:
        log.debug("importlib.resources lookup for UI asset %s/%s failed: %s", kind, name, exc)
    candidates.append(Path(__file__).resolve().parent / "ui" / kind / name)
    for cand in candidates:
        try:
            if cand.exists() and cand.is_file():
                return cand
        except OSError:
            continue
    return None

# Thumbnails go in the cache dir — separate from the clip files.
THUMB_DIR      = actual_home_dir() / ".cache" / "vice" / "thumbs"
# H.264 preview copies of clips the native WebEngine can't decode (H.265).
PROXY_DIR      = actual_home_dir() / ".cache" / "vice" / "proxies"
# Scratch space for editor export jobs (drawtext sidecar files).
EXPORT_WORK_DIR = actual_home_dir() / ".cache" / "vice" / "exports"
HIGHLIGHTS_DIR = actual_home_dir() / ".local" / "share" / "vice" / "highlights"


def _load_highlights(slug: str) -> list:
    f = HIGHLIGHTS_DIR / f"{slug}.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text())
    except Exception as exc:
        log.warning("Highlights file %s is unreadable: %s", f.name, exc)
        return []


def _save_highlights(slug: str, highlights: list) -> None:
    HIGHLIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    (HIGHLIGHTS_DIR / f"{slug}.json").write_text(json.dumps(highlights))


# In-app view counts per slug. Like playlist membership, the counters are
# migrated on rename and dropped on delete so a reused clip number never
# inherits another clip's history.
VIEWS_PATH = actual_home_dir() / ".local" / "share" / "vice" / "views.json"


def _load_views() -> dict[str, int]:
    if not VIEWS_PATH.exists():
        return {}
    try:
        return {str(k): int(v) for k, v in json.loads(VIEWS_PATH.read_text()).items()}
    except Exception as exc:
        log.warning("Views file %s is unreadable: %s", VIEWS_PATH, exc)
        return {}


def _save_views(views: dict[str, int]) -> None:
    # Write-and-rename so a crash mid-write can't truncate the whole file and
    # lose every count (a single JSON file, unlike per-clip highlights).
    VIEWS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = VIEWS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(views))
    tmp.replace(VIEWS_PATH)


# Small bag of UI state that must outlive the web view. The native window's
# localStorage does not reliably survive restarts on every QtWebEngine build,
# which made the first-run tutorial reappear every launch.
APP_STATE_PATH = actual_home_dir() / ".local" / "share" / "vice" / "ui_state.json"


def _load_app_state() -> dict:
    if not APP_STATE_PATH.exists():
        return {}
    try:
        data = json.loads(APP_STATE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("UI state file %s is unreadable: %s", APP_STATE_PATH, exc)
        return {}


def _save_app_state(state: dict) -> None:
    APP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    APP_STATE_PATH.write_text(json.dumps(state))


def _thumb_path(path: Path) -> Path:
    """Return cache path unique to this clip file content/version."""
    try:
        st = path.stat()
        key = f"{path.stem}_{st.st_size}_{st.st_mtime_ns}"
    except OSError:
        key = path.stem
    return THUMB_DIR / f"{key}.jpg"


def _purge_slug_thumbs(slug: str) -> None:
    """Remove any cached thumbs for a slug (legacy + versioned variants)."""
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    for t in THUMB_DIR.glob(f"{slug}*.jpg"):
        t.unlink(missing_ok=True)


def _proxy_path(path: Path) -> Path:
    """Cache path for a clip's H.264 preview, keyed by file identity so a trim
    or a reused clip number naturally invalidates it (same idea as _thumb_path)."""
    try:
        st = path.stat()
        key = f"{path.stem}_{st.st_size}_{st.st_mtime_ns}"
    except OSError:
        key = path.stem
    return PROXY_DIR / f"{key}.mp4"


def _purge_slug_proxies(slug: str) -> None:
    """Remove any cached preview proxies for a slug (all file versions)."""
    PROXY_DIR.mkdir(parents=True, exist_ok=True)
    for p in PROXY_DIR.glob(f"{slug}*.mp4"):
        p.unlink(missing_ok=True)


# WebEngine plays these without help; anything else gets an H.264 preview proxy.
_WEB_PLAYABLE_VCODECS = {"h264", "avc1", "vp8", "vp9", "av1"}


async def _make_preview_proxy(path: Path, vcodec: str) -> Optional[Path]:
    """Return an H.264 copy of *path* for in-app playback, transcoding once and
    caching it. Returns None when the source is already web-playable or the
    transcode fails, so the caller can just serve the original."""
    if vcodec and vcodec in _WEB_PLAYABLE_VCODECS:
        return None
    proxy = _proxy_path(path)
    if proxy.exists() and proxy.stat().st_size > 0:
        return proxy

    PROXY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = proxy.with_suffix(".mp4.tmp")
    # Same duration and fps as the source so trim in/out points map 1:1 to the
    # original file, which is what the trim endpoint actually cuts.
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-map", "0:v:0?", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        # The temp name ends in .tmp, so name the container explicitly.
        "-f", "mp4",
        "-y", str(tmp),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode != 0 or not tmp.exists():
            log.warning("preview proxy for %s failed: %s", path.name,
                        (stderr or b"").decode(errors="replace")[:200])
            tmp.unlink(missing_ok=True)
            return None
    except (asyncio.TimeoutError, OSError) as exc:
        log.warning("preview proxy for %s errored: %s", path.name, exc)
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(proxy)
    return proxy


# ── helpers ──────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


_PROBE_DEFAULTS = {"width": 1920, "height": 1080, "duration": 0, "vcodec": ""}


async def _remux_moov(path: Path) -> bool:
    """Try to recover `path` via `ffmpeg -c copy -movflags +faststart`.

    Used for clips whose MP4 container is damaged (no moov atom) — happens
    when the encoder was killed mid-finalize. The original file is only
    replaced when the remuxed copy probes as a sane video of comparable
    size; a remux that produces a near-empty file means the input was
    *not* a simple moov-atom problem, and replacing would destroy data
    (this used to truncate healthy clips to 0.02 s). Returns True when
    the remuxed file replaced the original.
    """
    tmp = path.with_suffix(path.suffix + ".fix.mp4")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(path), *KEEP_ALL_STREAMS, "-c", "copy",
            "-movflags", "+faststart", str(tmp),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=60)
        if proc.returncode == 0 and tmp.exists():
            remuxed = await probe_media(tmp)
            orig_size = path.stat().st_size
            if (
                remuxed
                and remuxed["duration"] > 0
                and tmp.stat().st_size >= orig_size * 0.5
            ):
                tmp.replace(path)
                return True
            log.warning(
                "Remux of %s produced an invalid or truncated file "
                "(duration=%.2fs, %d → %d bytes) — keeping the original",
                path.name,
                remuxed["duration"] if remuxed else 0.0,
                orig_size,
                tmp.stat().st_size,
            )
    except Exception as exc:
        log.warning("Remux of %s failed: %s", path.name, exc)
    try:
        tmp.unlink(missing_ok=True)
    except Exception:
        pass
    return False


async def _ffprobe(path: Path) -> dict:
    """Return {"width", "height", "duration"} via ffprobe.

    If the file cannot be probed at all, its container is probably missing
    the moov atom — try one (validated, non-destructive) remux + re-probe
    before giving up.
    """
    meta = await probe_media(path)
    if meta and meta["duration"] > 0:
        return meta
    if path.suffix.lower() != ".mp4":
        # The remux below repairs MP4 moov atoms; other containers are
        # served as-is.
        return meta or dict(_PROBE_DEFAULTS)
    log.warning("ffprobe cannot read %s — attempting moov remux", path.name)
    if await _remux_moov(path):
        meta = await probe_media(path)
        log.info(
            "Remuxed %s — duration now %.2fs",
            path.name, meta["duration"] if meta else 0.0,
        )
    return meta or dict(_PROBE_DEFAULTS)


async def _make_thumb(path: Path, duration: float = 0.0) -> Path:
    """Lazily generate a 640px-wide JPEG thumbnail stored in THUMB_DIR.

    Short clips (< 1 s) used to come back blank because `-ss 0.75` seeks
    past EOF and `-vf thumbnail` needs a 100-frame lookahead. Now we seek
    to `min(duration/2, 0.75)` and use a plain scale filter, which works
    on sub-second clips too.
    """
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    thumb = _thumb_path(path)
    if thumb.exists():
        return thumb
    if duration and duration > 0:
        seek_ts = min(duration / 2.0, 0.75)
    else:
        seek_ts = 0.0
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{seek_ts:.3f}",
            "-i", str(path),
            "-frames:v", "1",
            "-vf", "scale=640:-2",
            "-q:v", "4",
            str(thumb),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=20)
    except Exception as exc:
        log.debug("Thumbnail generation failed for %s: %s", path.name, exc)
    return thumb


# OpenGraph only, no twitter:card. twitter:player must point at an
# embeddable HTML page, not a raw video file, and Discord does not iframe
# arbitrary players anyway: when a player card is present and unusable,
# Discord renders no embed at all (issues #77, #100). Plain og:video with
# a direct file URL is the pattern working self-hosted sharers use.
_EMBED_PAGE = """\
<!DOCTYPE html>
<html><head>
  <meta charset="utf-8">
  <meta name="theme-color"              content="{color}">
  <meta property="og:site_name"         content="Vice">
  <meta property="og:type"              content="video.other">
  <meta property="og:url"               content="{page_url}">
  <meta property="og:title"             content="{title}">
  <meta property="og:description"       content="Clipped with Vice on Linux">
  <meta property="og:video"             content="{video_url}">
  <meta property="og:video:url"         content="{video_url}">
  <meta property="og:video:secure_url"  content="{video_url}">
  <meta property="og:video:type"        content="{video_type}">
  <meta property="og:video:width"       content="{width}">
  <meta property="og:video:height"      content="{height}">
  <meta property="og:image"             content="{thumb_url}">
  <title>{title}</title>
  <style>
    body{{margin:0;background:#000;display:flex;align-items:center;
         justify-content:center;min-height:100vh}}
    video{{max-width:100%;max-height:100vh}}
  </style>
</head>
<body>
  <video src="{video_url}" controls autoplay muted loop></video>
</body></html>
"""


# ── share server ─────────────────────────────────────────────────────────────

class ShareServer:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._local_app = web.Application()
        self._public_app = web.Application()
        self._local_runner: Optional[web.AppRunner] = None
        self._local_site: Optional[web.TCPSite] = None
        self._public_runner: Optional[web.AppRunner] = None
        self._public_site: Optional[web.TCPSite] = None
        self._legacy_public_site: Optional[web.TCPSite] = None

        # slug → Path  (populated from disk on start + runtime additions)
        self._clips: dict[str, Path] = {}
        # slug → {width, height, duration}
        self._meta:  dict[str, dict] = {}

        self.playlists = PlaylistStore()
        self._views = _load_views()
        self.editor_project = EditorProjectStore()
        self._exports = ExportManager(self.broadcast)

        # One lock per proxy path so two opens of the same H.265 clip don't
        # transcode it twice.
        self._proxy_locks: dict[str, asyncio.Lock] = {}

        self._tunnel_proc: Optional[asyncio.subprocess.Process] = None
        self._tunnel_url:  Optional[str] = None
        self._local_base_url: Optional[str] = None
        self._public_bind_url: Optional[str] = None
        self._legacy_public_bind_url: Optional[str] = None

        # Connected WebSocket clients
        self._ws_clients: set[web.WebSocketResponse] = set()

        # Injected by ViceDaemon so /api/trigger works
        self.trigger_clip_cb: Optional[Callable[[], Coroutine]] = None
        # Injected so the Settings "Check now" button can skip the daily wait.
        self.check_update_cb: Optional[Callable[[], Coroutine]] = None
        # Injected so /api/status can report live state
        self.get_status_cb: Optional[Callable[[], dict]] = None
        # Injected so config changes can be applied without restart when possible.
        self.apply_config_cb: Optional[Callable[[], Coroutine]] = None

        self._setup_local_routes()
        self._setup_public_routes()

    # ── routes ───────────────────────────────────────────────────────────────

    def _setup_local_routes(self) -> None:
        r = self._local_app.router

        # Web UI
        r.add_get("/",            self._ui)
        for _kind in _UI_ASSET_KINDS:
            r.add_get(f"/{_kind}/{{name}}",
                      lambda req, k=_kind: self._ui_asset(req, kind=k))

        # Discord embed pages
        r.add_get("/c/{slug}",    self._embed_page)

        # Media
        r.add_get("/v/{slug}",    self._video)
        r.add_get("/t/{slug}",    self._thumb)

        # REST
        r.add_get("/api/clips",              self._api_clips)
        r.add_get("/api/clips/{slug}",       self._api_clip_info)
        r.add_delete("/api/clips/{slug}",    self._api_delete)
        r.add_post("/api/clips/{slug}/trim",              self._api_trim)
        r.add_post("/api/clips/{slug}/rename",            self._api_rename)
        r.add_post("/api/clips/{slug}/reveal",            self._api_reveal)
        r.add_post("/api/clips/{slug}/open",              self._api_open)
        r.add_post("/api/clips/{slug}/copy-file",         self._api_copy_file)
        r.add_get("/api/app-state",                       self._api_get_app_state)
        r.add_post("/api/app-state",                      self._api_set_app_state)
        r.add_post("/api/clips/{slug}/view",              self._api_view)
        r.add_get("/api/clips/{slug}/highlights",         self._api_get_highlights)
        r.add_post("/api/clips/{slug}/highlights",        self._api_add_highlight)
        r.add_patch("/api/clips/{slug}/highlights/{hid}", self._api_patch_highlight)
        r.add_delete("/api/clips/{slug}/highlights/{hid}",self._api_del_highlight)
        r.add_get("/api/editor/project",              self._api_editor_get_project)
        r.add_post("/api/editor/project",             self._api_editor_save_project)
        r.add_post("/api/editor/export",              self._api_editor_export)
        r.add_post("/api/editor/export/{jid}/cancel", self._api_editor_export_cancel)
        r.add_get("/api/playlists",            self._api_playlists)
        r.add_post("/api/playlists",           self._api_create_playlist)
        r.add_patch("/api/playlists/{pid}",    self._api_patch_playlist)
        r.add_delete("/api/playlists/{pid}",   self._api_delete_playlist)
        r.add_post("/api/playlists/{pid}/clips",           self._api_playlist_add_clip)
        r.add_delete("/api/playlists/{pid}/clips/{slug}",  self._api_playlist_remove_clip)
        r.add_get("/api/config",               self._api_get_config)
        r.add_get("/api/displays",             self._api_get_displays)
        r.add_get("/api/capture-sources",      self._api_get_capture_sources)
        r.add_get("/api/audio-sources",        self._api_get_audio_sources)
        r.add_post("/api/config",              self._api_set_config)
        r.add_get("/api/status",               self._api_status)
        r.add_post("/api/update/check",         self._api_check_update)
        r.add_post("/api/trigger",             self._api_trigger)
        r.add_post("/api/quit",                self._api_quit)
        r.add_post("/api/uninstall",           self._api_uninstall)

        # WebSocket
        r.add_get("/ws", self._ws_handler)

    def _setup_public_routes(self) -> None:
        r = self._public_app.router
        r.add_get("/c/{slug}", self._embed_page)
        r.add_get("/v/{slug}", self._video)
        r.add_get("/t/{slug}", self._thumb)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        # Pre-populate from output dir
        out_dir = resolve_path(self.cfg.output.directory)
        if out_dir.exists():
            media = list(out_dir.glob("*.mp4")) + list(out_dir.glob("*.mkv"))
            for clip in sorted(media, key=lambda p: p.stat().st_mtime):
                self._clips[clip.stem] = clip
        self.playlists.backfill(
            set(self._clips),
            build_tag_index(self.cfg.discord.custom_games),
            seed_auto=self.cfg.output.auto_playlist_by_game,
        )
        # View counts persist like highlights: they are only dropped when a
        # clip is deleted (in _api_delete) or its number is reused by a new
        # recording (in add_clip). They are never purged against the startup
        # scan, which wiped valid counts when the output dir was slow to mount.

        local_port = self.cfg.sharing.port
        public_port = self.cfg.sharing.public_port or (local_port + 1)
        public_host = _local_ip()

        self._local_runner = web.AppRunner(self._local_app, access_log=None)
        await self._local_runner.setup()
        self._local_site = web.TCPSite(self._local_runner, "127.0.0.1", local_port)
        await self._local_site.start()

        self._public_runner = web.AppRunner(self._public_app, access_log=None)
        await self._public_runner.setup()
        self._public_site = web.TCPSite(self._public_runner, "0.0.0.0", public_port)
        await self._public_site.start()

        self._local_base_url = f"http://127.0.0.1:{local_port}"
        self._public_bind_url = f"http://{public_host}:{public_port}"
        log.info("Vice local control UI: %s", self._local_base_url)
        log.info("Vice public share server: %s", self._public_bind_url)
        if self.cfg.sharing.base_url:
            log.info("Vice public base URL override: %s", self.cfg.sharing.base_url)
        elif public_host not in {"127.0.0.1", "0.0.0.0"}:
            try:
                self._legacy_public_site = web.TCPSite(self._public_runner, public_host, local_port)
                await self._legacy_public_site.start()
                self._legacy_public_bind_url = f"http://{public_host}:{local_port}"
                log.info(
                    "Vice legacy share compatibility URL: %s",
                    self._legacy_public_bind_url,
                )
            except OSError as exc:
                log.warning(
                    "Failed to enable legacy share compatibility on %s:%d: %s",
                    public_host,
                    local_port,
                    exc,
                )

        if self.cfg.sharing.cloudflare_tunnel:
            await self._start_tunnel(public_port)

    async def stop(self) -> None:
        for ws in list(self._ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        if self._tunnel_proc:
            try:
                self._tunnel_proc.terminate()
                await asyncio.wait_for(self._tunnel_proc.wait(), timeout=5)
            except Exception:
                pass
        if self._local_runner:
            await self._local_runner.cleanup()
        if self._public_runner:
            await self._public_runner.cleanup()

    # ── public helpers (called by ViceDaemon) ─────────────────────────────────

    def add_clip(self, path: Path, game: Optional[str] = None) -> str:
        """Register a new clip and return its share URL."""
        slug = path.stem
        self._clips[slug] = path
        self._meta.pop(slug, None)
        # A fresh recording under a reused clip number must not inherit the
        # old clip's view count.
        if self._views.pop(slug, None) is not None:
            _save_views(self._views)
        if (game and self.cfg.output.auto_playlist_by_game
                and self.playlists.record_auto(game, slug)):
            asyncio.create_task(self._broadcast_playlists())
        asyncio.create_task(self.broadcast({
            "type": "clip_saved",
            "clip": self._clip_json(slug, path, {}),
        }))
        asyncio.create_task(self._broadcast_clip(slug, path))
        return f"{self.public_base_url()}/c/{slug}"

    def local_base_url(self) -> Optional[str]:
        return self._local_base_url

    def public_base_url(self) -> Optional[str]:
        if self.cfg.sharing.base_url:
            return self.cfg.sharing.base_url.rstrip("/")
        return (self._tunnel_url or self._public_bind_url or "").rstrip("/") or None

    def public_is_reachable(self) -> bool:
        """Whether share links work outside the local network. False means we
        fell back to a LAN address because there is no tunnel (#105)."""
        return bool(self.cfg.sharing.base_url or self._tunnel_url)

    async def broadcast(self, msg: dict) -> None:
        if not self._ws_clients:
            return
        text = json.dumps(msg)
        dead: set[web.WebSocketResponse] = set()
        for ws in self._ws_clients:
            try:
                await ws.send_str(text)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    # ── internal broadcast helpers ────────────────────────────────────────────

    async def _broadcast_playlists(self) -> None:
        await self.broadcast({
            "type": "playlists_changed",
            "playlists": self.playlists.list_playlists(),
        })

    async def _broadcast_clip(self, slug: str, path: Path) -> None:
        meta = await self._get_meta(slug, path)
        if not _thumb_path(path).exists():
            await _make_thumb(path, duration=meta.get("duration", 0))
        await self.broadcast({"type": "clip_saved", "clip": self._clip_json(slug, path, meta)})

    async def _get_meta(self, slug: str, path: Path) -> dict:
        if slug not in self._meta:
            self._meta[slug] = await _ffprobe(path)
        return self._meta[slug]

    def _clip_json(self, slug: str, path: Path, meta: dict) -> dict:
        public_base = self.public_base_url() or self.local_base_url() or ""
        try:
            st = path.stat()
            size = st.st_size
            mtime_ns = st.st_mtime_ns
            created_at = datetime.fromtimestamp(st.st_mtime).isoformat()
        except OSError:
            size, mtime_ns, created_at = 0, 0, ""

        thumb_rev = f"{size}-{mtime_ns}"
        thumb_url = f"/t/{slug}?v={thumb_rev}" if _thumb_path(path).exists() else None
        return {
            "slug":       slug,
            "name":       path.name,
            "size":       size,
            "created_at": created_at,
            "game":       self.playlists.game_for(slug),
            "views":      self._views.get(slug, 0),
            "duration":   meta.get("duration", 0),
            "width":      meta.get("width",    0),
            "height":     meta.get("height",   0),
            # Lets the UI request an H.264 preview proxy for codecs the native
            # WebEngine can't decode (H.265).
            "vcodec":     meta.get("vcodec",   ""),
            # Keep share links public, but serve media via local relative URLs
            # so the app UI never fetches video through an external tunnel.
            "share_url":  f"{public_base}/c/{slug}",
            "share_is_public": self.public_is_reachable(),
            # Cache-bust media URLs by clip file identity: deleted clip numbers
            # get reused (Vice_Clip_5 can name a brand-new file), and a trim
            # rewrites the file under the same slug — without the version the
            # browser may play a cached older video for the clip it shows.
            "video_url":  f"/v/{slug}?v={thumb_rev}",
            "thumb_url":  thumb_url,
        }

    # ── route handlers ────────────────────────────────────────────────────────

    async def _ui(self, _: web.Request) -> web.Response:
        ui_index = _resolve_ui_index()
        if not ui_index:
            log.error("Vice UI not found (missing vice/ui/index.html)")
            return web.Response(
                text="<h1>Vice UI not found</h1><p>Reinstall Vice from this checkout or AUR package.</p>",
                content_type="text/html",
                status=500,
            )

        try:
            content = ui_index.read_text(encoding="utf-8")
            # Asset URLs are versioned by content, the visible version string
            # stays the plain version.
            content = content.replace(f"?v={UI_VERSION_TOKEN}", f"?v={_ui_asset_rev()}")
            content = content.replace(UI_VERSION_TOKEN, __version__)
            return web.Response(
                text=content,
                content_type="text/html",
                # The page itself must never be cached, or a stale copy keeps
                # pointing at the previous build's assets.
                headers={"Cache-Control": "no-store"},
            )
        except Exception as exc:
            log.error("Failed reading UI file %s: %s", ui_index, exc)
            return web.Response(
                text="<h1>Vice UI failed to load</h1><p>Check vice logs for details.</p>",
                content_type="text/html",
                status=500,
            )

    async def _ui_asset(self, req: web.Request, *, kind: str) -> web.Response:
        path = _resolve_ui_asset(kind, req.match_info["name"])
        if not path:
            raise web.HTTPNotFound()
        return web.FileResponse(
            path,
            headers={
                "Content-Type": _UI_CONTENT_TYPES[kind],
                "Cache-Control": "public, max-age=31536000, immutable",
            },
        )

    async def _embed_page(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()
        meta = await self._get_meta(slug, path)
        # cloudflared terminates TLS and forwards plain HTTP, so req.scheme
        # is "http" even when the visitor came in over https. Discord and
        # other scrapers reject non-https og:video URLs, which breaks
        # embeds for tunnel links (issue #100).
        scheme = req.headers.get("X-Forwarded-Proto", req.scheme)
        base = f"{scheme}://{req.host}"
        # Direct file URL with the real container suffix; some unfurlers
        # sniff the extension. _video strips it back off.
        suffix = path.suffix.lower() or ".mp4"
        html = _EMBED_PAGE.format(
            title=f"Vice clip — {slug}",
            page_url=f"{base}/c/{slug}",
            video_url=f"{base}/v/{slug}{suffix}",
            video_type="video/x-matroska" if suffix == ".mkv" else "video/mp4",
            thumb_url=f"{base}/t/{slug}",
            width=meta.get("width", 1920),
            height=meta.get("height", 1080),
            color=self._embed_color(),
        )
        return web.Response(text=html, content_type="text/html")

    def _embed_color(self) -> str:
        """Validated embed accent color (guards against HTML injection)."""
        color = getattr(self.cfg.sharing, "embed_color", "") or ""
        if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            return color
        return "#0099ff"

    async def _video(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if path is None and slug.lower().endswith((".mp4", ".mkv")):
            # Embed pages link the file with its container suffix. Exact
            # match first so slugs that themselves contain dots keep working.
            path = self._clips.get(slug.rsplit(".", 1)[0])
        if not path or not path.exists():
            raise web.HTTPNotFound()

        # The UI asks for proxy=1 when the clip's codec (H.265) can't play in
        # the native WebEngine. Serve a cached H.264 copy instead; the original
        # is never touched. Falls through to the source if it's already
        # web-playable or the transcode fails.
        if req.query.get("proxy") == "1":
            served = await self._serve_preview_proxy(slug, path)
            if served is not None:
                return served

        # no-cache = revalidate before reuse. Slugs are not stable identities
        # (clip numbers get reused after deletes, trims rewrite in place), so
        # a cached response may belong to a different video than the slug
        # currently names.
        content_type = ("video/x-matroska" if path.suffix.lower() == ".mkv"
                        else "video/mp4")
        return web.FileResponse(
            path,
            headers={
                "Content-Type": content_type,
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-cache",
            },
        )

    async def _serve_preview_proxy(self, slug: str, path: Path):
        """Return a FileResponse for the clip's H.264 preview proxy, or None to
        fall back to serving the original."""
        proxy_key = str(_proxy_path(path))
        lock = self._proxy_locks.setdefault(proxy_key, asyncio.Lock())
        async with lock:
            meta = await self._get_meta(slug, path)
            proxy = await _make_preview_proxy(path, meta.get("vcodec", ""))
        if proxy is None or not proxy.exists():
            return None
        return web.FileResponse(
            proxy,
            headers={
                "Content-Type": "video/mp4",
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-cache",
            },
        )

    async def _thumb(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()
        meta = await self._get_meta(slug, path)
        t = await _make_thumb(path, duration=meta.get("duration", 0))
        if not t.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(t, headers={"Content-Type": "image/jpeg"})

    # ── REST handlers ─────────────────────────────────────────────────────────

    async def _api_clips(self, _: web.Request) -> web.Response:
        items = [
            (slug, path) for slug, path in sorted(
                self._clips.items(),
                key=lambda kv: kv[1].stat().st_mtime if kv[1].exists() else 0,
                reverse=True,
            )
            if path.exists()
        ]
        metas = {slug: await self._get_meta(slug, path) for slug, path in items}

        sem = asyncio.Semaphore(3)
        async def _ensure(slug: str, path: Path) -> None:
            if _thumb_path(path).exists():
                return
            async with sem:
                await _make_thumb(path, duration=metas[slug].get("duration", 0))
        await asyncio.gather(
            *[_ensure(slug, path) for slug, path in items],
            return_exceptions=True,
        )

        result = [self._clip_json(slug, path, metas[slug]) for slug, path in items]
        return web.json_response({"clips": result})

    async def _api_clip_info(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()
        meta = await self._get_meta(slug, path)
        return web.json_response(self._clip_json(slug, path, meta))

    async def _api_delete(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.pop(slug, None)
        if path and path.exists():
            path.unlink()
        _purge_slug_thumbs(slug)
        _purge_slug_proxies(slug)
        (HIGHLIGHTS_DIR / f"{slug}.json").unlink(missing_ok=True)
        self._meta.pop(slug, None)
        if self._views.pop(slug, None) is not None:
            _save_views(self._views)
        if self.playlists.on_clip_deleted(slug):
            await self._broadcast_playlists()
        if self.editor_project.on_clip_deleted(slug):
            await self.broadcast({"type": "editor_project_changed"})
        await self.broadcast({"type": "clip_deleted", "slug": slug})
        return web.json_response({"ok": True})

    async def _api_trim(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()

        body  = await req.json()
        start = float(body.get("start", 0))
        end   = float(body.get("end",   0))
        if end <= start:
            return web.json_response({"ok": False, "error": "end must be after start"})

        ext = path.suffix.lstrip(".") or "mp4"
        tmp = path.with_suffix(f".trimming.{ext}")
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", str(start), "-i", str(path),
            "-t",  str(end - start),
            *KEEP_ALL_STREAMS,
            "-c",  "copy",
            *(["-movflags", "+faststart"] if ext == "mp4" else []),
            "-y",  str(tmp),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                tmp.unlink(missing_ok=True)
                return web.json_response({"ok": False, "error": stderr.decode()[:300]})
        except asyncio.TimeoutError:
            tmp.unlink(missing_ok=True)
            return web.json_response({"ok": False, "error": "ffmpeg timed out"})

        tmp.replace(path)
        # Clear cached thumbnail and metadata so they regenerate on next access
        _purge_slug_thumbs(slug)
        _purge_slug_proxies(slug)
        self._meta.pop(slug, None)
        asyncio.create_task(self._broadcast_clip(slug, path))
        return web.json_response({"ok": True, "slug": slug})

    async def _api_rename(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()

        body     = await req.json()
        new_name = body.get("name", "").strip()
        if not new_name:
            return web.json_response({"ok": False, "error": "name is required"})

        # Sanitise — no path separators; keep the clip's own container.
        ext = path.suffix.lower() or ".mp4"
        new_name = new_name.replace("/", "").replace("\\", "").replace("\0", "")
        if " " in new_name:
            return web.json_response({"ok": False, "error": "Clip name cannot contain spaces"})
        if not new_name.lower().endswith(ext):
            new_name += ext

        new_path = path.parent / new_name
        if new_path.exists() and new_path != path:
            return web.json_response({"ok": False, "error": "A clip with that name already exists"})

        path.rename(new_path)
        new_slug = new_path.stem

        # Update internal state
        self._clips.pop(slug, None)
        self._clips[new_slug] = new_path
        _purge_slug_thumbs(slug)
        _purge_slug_proxies(slug)
        self._meta.pop(slug, None)

        # Rename highlights file if it exists
        old_hl = HIGHLIGHTS_DIR / f"{slug}.json"
        if old_hl.exists():
            HIGHLIGHTS_DIR.mkdir(parents=True, exist_ok=True)
            old_hl.rename(HIGHLIGHTS_DIR / f"{new_slug}.json")

        # Playlist membership, the view counter and the editor project follow
        # the clip
        if self.playlists.on_clip_renamed(slug, new_slug):
            await self._broadcast_playlists()
        if self.editor_project.on_clip_renamed(slug, new_slug):
            await self.broadcast({"type": "editor_project_changed"})
        if slug in self._views:
            self._views[new_slug] = self._views.pop(slug)
            _save_views(self._views)

        # Tell the UI: old card gone, new card appears
        await self.broadcast({"type": "clip_deleted", "slug": slug})
        asyncio.create_task(self._broadcast_clip(new_slug, new_path))
        return web.json_response({"ok": True, "slug": new_slug})

    async def _api_reveal(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()
        # Open the clip's parent directory in the system file manager
        asyncio.create_task(asyncio.create_subprocess_exec(
            "xdg-open", str(path.parent),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        ))
        return web.json_response({"ok": True})

    async def _api_get_app_state(self, _: web.Request) -> web.Response:
        return web.json_response(_load_app_state())

    async def _api_set_app_state(self, req: web.Request) -> web.Response:
        try:
            body = await req.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "expected an object"}, status=400)
        state = _load_app_state()
        state.update(body)
        _save_app_state(state)
        return web.json_response({"ok": True})

    async def _api_copy_file(self, req: web.Request) -> web.Response:
        """Put the clip file itself on the clipboard so it can be pasted
        straight into Discord instead of shared as a link (#117)."""
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()

        uri = path.resolve().as_uri()
        # Chromium and Electron read pasted files from text/uri-list. Both
        # tools keep owning the selection in the background after forking,
        # so the clipboard survives this request returning.
        if shutil.which("wl-copy"):
            cmd = ["wl-copy", "--type", "text/uri-list"]
        elif shutil.which("xclip"):
            cmd = ["xclip", "-selection", "clipboard", "-t", "text/uri-list"]
        else:
            return web.json_response({
                "ok": False,
                "error": "Copying files needs wl-clipboard (Wayland) or xclip (X11).",
            })

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            proc.stdin.write(f"{uri}\r\n".encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception as exc:
            log.warning("Copying %s to the clipboard failed: %s", path.name, exc)
            return web.json_response({"ok": False, "error": "Could not reach the clipboard."})
        return web.json_response({"ok": True})

    async def _api_open(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()
        # Playback escape hatch for WebEngine builds that cannot decode the
        # clip (PyPI wheels ship without H.264 support): hand the file to the
        # system default video player.
        asyncio.create_task(asyncio.create_subprocess_exec(
            "xdg-open", str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        ))
        return web.json_response({"ok": True})

    async def _api_playlists(self, _: web.Request) -> web.Response:
        return web.json_response({"playlists": self.playlists.list_playlists()})

    async def _api_create_playlist(self, req: web.Request) -> web.Response:
        body = await req.json()
        try:
            playlist = self.playlists.create_custom(
                name=str(body.get("name", "")),
                emoji=str(body.get("emoji", "") or ""),
                color1=str(body.get("color1", "") or ""),
                color2=str(body.get("color2", "") or ""),
            )
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        await self._broadcast_playlists()
        return web.json_response({"ok": True, "playlist": playlist})

    async def _api_patch_playlist(self, req: web.Request) -> web.Response:
        pid = req.match_info["pid"]
        body = await req.json()
        try:
            playlist = self.playlists.update_playlist(pid, body)
        except KeyError:
            raise web.HTTPNotFound()
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        await self._broadcast_playlists()
        return web.json_response({"ok": True, "playlist": playlist})

    async def _api_delete_playlist(self, req: web.Request) -> web.Response:
        pid = req.match_info["pid"]
        try:
            self.playlists.delete(pid)
        except KeyError:
            raise web.HTTPNotFound()
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        await self._broadcast_playlists()
        return web.json_response({"ok": True})

    async def _api_playlist_add_clip(self, req: web.Request) -> web.Response:
        pid = req.match_info["pid"]
        body = await req.json()
        slug = str(body.get("slug", ""))
        if slug not in self._clips:
            raise web.HTTPNotFound()
        try:
            playlist = self.playlists.add_clip(pid, slug)
        except KeyError:
            raise web.HTTPNotFound()
        await self._broadcast_playlists()
        return web.json_response({"ok": True, "playlist": playlist})

    async def _api_playlist_remove_clip(self, req: web.Request) -> web.Response:
        pid = req.match_info["pid"]
        slug = req.match_info["slug"]
        try:
            self.playlists.remove_clip(pid, slug)
        except KeyError:
            raise web.HTTPNotFound()
        await self._broadcast_playlists()
        return web.json_response({"ok": True})

    async def _api_view(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        if slug not in self._clips:
            raise web.HTTPNotFound()
        self._views[slug] = self._views.get(slug, 0) + 1
        _save_views(self._views)
        return web.json_response({"ok": True, "views": self._views[slug]})

    async def _api_get_highlights(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        return web.json_response({"highlights": _load_highlights(slug)})

    async def _api_add_highlight(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        body  = await req.json()
        time_ = round(float(body.get("time", 0)), 3)
        label = (body.get("label") or "Highlight").strip() or "Highlight"
        color = body.get("color") or "#f59e0b"
        hl = _load_highlights(slug)
        next_id = str(max((int(h["id"]) for h in hl if str(h.get("id","")).isdigit()), default=0) + 1)
        entry = {"id": next_id, "time": time_, "label": label, "color": color}
        hl.append(entry)
        hl.sort(key=lambda h: h["time"])
        _save_highlights(slug, hl)
        return web.json_response({"ok": True, "highlight": entry})

    async def _api_patch_highlight(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        hid  = req.match_info["hid"]
        body  = await req.json()
        hl = _load_highlights(slug)
        for h in hl:
            if str(h.get("id")) == hid:
                if "label" in body:
                    h["label"] = (body["label"] or "Highlight").strip() or "Highlight"
                if "color" in body:
                    h["color"] = body["color"]
                if "time" in body:
                    try:
                        h["time"] = round(max(0.0, float(body["time"])), 3)
                    except (TypeError, ValueError):
                        pass
                hl.sort(key=lambda x: float(x.get("time", 0)))
                _save_highlights(slug, hl)
                return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "highlight not found"})

    async def _api_del_highlight(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        hid  = req.match_info["hid"]
        hl = [h for h in _load_highlights(slug) if str(h.get("id")) != hid]
        _save_highlights(slug, hl)
        return web.json_response({"ok": True})

    # ── editor ───────────────────────────────────────────────────────────────

    async def _api_editor_get_project(self, _: web.Request) -> web.Response:
        project = self.editor_project.load()
        missing: list[str] = []
        if project:
            refs = {it.get("clipId") for it in project.get("items", [])
                    if isinstance(it, dict) and it.get("clipId")}
            missing = sorted(c for c in refs if c not in self._clips)
        return web.json_response({"project": project, "missing": missing})

    async def _api_editor_save_project(self, req: web.Request) -> web.Response:
        # Autosave is lenient on purpose: items may reference clips that no
        # longer exist and still get persisted, so a slow-mounting output dir
        # can't eat the project.
        try:
            body = await req.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if (not isinstance(body, dict)
                or not isinstance(body.get("tracks"), list)
                or not isinstance(body.get("items"), list)):
            return web.json_response({"ok": False, "error": "expected a project"},
                                     status=400)
        self.editor_project.save({"version": 1, "tracks": body["tracks"],
                                  "items": body["items"]})
        return web.json_response({"ok": True})

    async def _editor_sources(self, project: dict) -> dict[str, Source]:
        sources: dict[str, Source] = {}
        for it in project.get("items", []):
            cid = it.get("clipId") if isinstance(it, dict) else None
            if not cid or cid in sources:
                continue
            path = self._clips.get(cid)
            if not path or not path.exists():
                continue
            meta = await self._get_meta(cid, path)
            sources[cid] = Source(
                path=path,
                duration=meta.get("duration", 0),
                width=meta.get("width", 0),
                height=meta.get("height", 0),
                has_audio=meta.get("audio_streams", 0) > 0,
            )
        return sources

    async def _api_editor_export(self, req: web.Request) -> web.Response:
        try:
            body = await req.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        raw = body.get("project")
        if not isinstance(raw, dict):
            return web.json_response({"ok": False, "error": "expected a project"},
                                     status=400)
        if self._exports.busy:
            return web.json_response(
                {"ok": False, "error": "an export is already running"}, status=409)

        sources = await self._editor_sources(raw)
        project, errors = validate_project(raw, sources)
        if errors:
            return web.json_response({"ok": False, "errors": errors}, status=400)

        out_dir = resolve_path(self.cfg.output.directory)
        location = body.get("location", "library")
        if location == "videos":
            dest = actual_home_dir() / "Videos"
        elif location == "custom":
            custom = str(body.get("path", "")).strip()
            if not custom:
                return web.json_response(
                    {"ok": False, "error": "a folder is required"}, status=400)
            dest = resolve_path(custom)
        else:
            location, dest = "library", out_dir
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return web.json_response(
                {"ok": False, "error": f"cannot use that folder: {exc}"}, status=400)

        requested = body.get("filename")
        if requested:
            name = sanitize_export_name(str(requested))
            if not name:
                return web.json_response(
                    {"ok": False, "error": "that name will not work as a file"},
                    status=400)
        else:
            name = default_export_name(dest)
        final = dest / name
        if final.exists():
            return web.json_response(
                {"ok": False, "error": "a file with that name already exists"},
                status=400)

        job_id = f"exp-{int(datetime.now().timestamp() * 1000)}"
        work = EXPORT_WORK_DIR / job_id
        work.mkdir(parents=True, exist_ok=True)
        for path, text in text_file_contents(project, work).items():
            path.write_text(text)

        tmp = dest / f".{final.stem}.export.mp4"
        cmd = build_export_cmd(project, sources, tmp,
                               accent=str(body.get("accent", "")) or "#0099ff",
                               text_dir=work)
        add_to_library = bool(body.get("add_to_library"))

        async def on_done(path: Path) -> Optional[dict]:
            target = path
            if location != "library" and add_to_library:
                copy = out_dir / name
                if copy.exists():
                    copy = out_dir / default_export_name(out_dir)
                out_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, copy)
                target = copy
            elif location != "library":
                return None
            self.add_clip(target)
            meta = await self._get_meta(target.stem, target)
            return self._clip_json(target.stem, target, meta)

        def cleanup() -> None:
            shutil.rmtree(work, ignore_errors=True)

        try:
            self._exports.start(job_id, cmd, project_extent(project), tmp, final,
                                on_done=on_done, cleanup=cleanup)
        except ExportBusy:
            cleanup()
            return web.json_response(
                {"ok": False, "error": "an export is already running"}, status=409)
        log.info("Editor export %s started: %s", job_id, final)
        return web.json_response({"ok": True, "job_id": job_id, "path": str(final)})

    async def _api_editor_export_cancel(self, req: web.Request) -> web.Response:
        if await self._exports.cancel(req.match_info["jid"]):
            return web.json_response({"ok": True})
        raise web.HTTPNotFound()

    async def _api_uninstall(self, _: web.Request) -> web.Response:
        """Launch a detached uninstall process, then exit the daemon cleanly."""
        import os
        import signal as _sig
        import subprocess as _sp
        import sys

        # Use a shell subprocess in a *new session* so it survives after we
        # send SIGTERM to this daemon process.  The `sleep 2` delay lets the
        # daemon finish shutting down (and the Unix socket disappear) before
        # the uninstall script tries to stop it via IPC — avoiding a deadlock
        # where the uninstall blocks waiting to talk to a daemon that is
        # waiting for the uninstall to finish.
        exe = sys.executable.replace("'", r"\'")
        cmd = f"sleep 2 && '{exe}' -m vice.main uninstall --yes"
        try:
            _sp.Popen(
                ["bash", "-c", cmd],
                start_new_session=True,
                stdin=_sp.DEVNULL,
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
        except Exception as exc:
            log.error("Failed to launch uninstall subprocess: %s", exc)

        # Stop the daemon after giving the HTTP response time to reach the client.
        async def _exit() -> None:
            await asyncio.sleep(0.4)
            os.kill(os.getpid(), _sig.SIGTERM)

        asyncio.create_task(_exit())
        return web.json_response({"ok": True})

    async def _api_get_config(self, _: web.Request) -> web.Response:
        from .config import load as load_cfg
        return web.json_response(asdict(load_cfg()))

    async def _api_get_displays(self, req: web.Request) -> web.Response:
        backend = (req.query.get("backend") or self.cfg.recording.backend or "auto").strip() or "auto"
        # Enumeration shells out (with timeouts); keep it off the event loop.
        payload = await asyncio.to_thread(list_display_options, backend)
        payload["selected"] = self.cfg.recording.display
        return web.json_response(payload)

    async def _api_get_capture_sources(self, req: web.Request) -> web.Response:
        """Return both monitors and application windows as capture sources."""
        backend = (req.query.get("backend") or self.cfg.recording.backend or "auto").strip() or "auto"
        payload = await asyncio.to_thread(list_capture_sources, backend)
        payload["selected"] = self.cfg.recording.capture_source or self.cfg.recording.display
        return web.json_response(payload)

    async def _api_get_audio_sources(self, _: web.Request) -> web.Response:
        payload = await asyncio.to_thread(list_gsr_audio_sources)
        payload["selected"] = self.cfg.recording.gsr_audio_source
        return web.json_response(payload)

    async def _api_set_config(self, req: web.Request) -> web.Response:
        from .config import (
            Config, RecordingConfig, HotkeyConfig, OutputConfig, SharingConfig,
            DiscordConfig, DiscordCustomGame, UpdatesConfig, NotificationsConfig,
            clamp_recording_limits, ensure_buffer_covers_clip_presets,
            normalize_clip_presets, normalize_combo,
            validate_hotkeys,
            load as load_cfg, save as save_cfg,
        )

        body = await req.json()

        def _merge(base: dict, over: dict) -> dict:
            for k, v in over.items():
                if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                    _merge(base[k], v)
                elif k in base:
                    base[k] = v
            return base

        # Merge onto the persisted config so partial saves never depend on
        # transient in-memory rollback state.
        persisted_cfg = load_cfg()
        merged = _merge(asdict(persisted_cfg), body)

        discord_raw = dict(merged.get("discord", {}))
        custom_games_raw = discord_raw.pop("custom_games", []) or []
        discord_custom_games = [
            DiscordCustomGame(
                name=str(g.get("name", "")),
                matches=[str(m) for m in (g.get("matches") or [])],
            )
            for g in custom_games_raw
            if isinstance(g, dict)
        ]
        hotkeys_raw = dict(merged.get("hotkeys", {}))
        if hotkeys_raw.get("clip"):
            hotkeys_raw["clip"] = normalize_combo(str(hotkeys_raw["clip"]).strip())
        try:
            hotkeys_raw["clip_presets"] = normalize_clip_presets(
                hotkeys_raw.get("clip_presets", []),
                strict=True,
            )
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

        new_cfg = Config(
            recording=RecordingConfig(**{
                k: v for k, v in merged["recording"].items()
                if k in RecordingConfig.__dataclass_fields__
            }),
            hotkeys=HotkeyConfig(**{
                k: v for k, v in hotkeys_raw.items()
                if k in HotkeyConfig.__dataclass_fields__
            }),
            output=OutputConfig(**{
                k: v for k, v in merged["output"].items()
                if k in OutputConfig.__dataclass_fields__
            }),
            sharing=SharingConfig(**{
                k: v for k, v in merged["sharing"].items()
                if k in SharingConfig.__dataclass_fields__
            }),
            discord=DiscordConfig(
                **{k: v for k, v in discord_raw.items()
                   if k in DiscordConfig.__dataclass_fields__ and k != "custom_games"},
                custom_games=discord_custom_games,
            ),
            updates=UpdatesConfig(**{
                k: v for k, v in merged.get("updates", {}).items()
                if k in UpdatesConfig.__dataclass_fields__
            }),
            notifications=NotificationsConfig(**{
                k: v for k, v in merged.get("notifications", {}).items()
                if k in NotificationsConfig.__dataclass_fields__
            }),
        )
        try:
            validate_hotkeys(new_cfg.hotkeys)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        ensure_buffer_covers_clip_presets(new_cfg)
        clamp_recording_limits(new_cfg)

        old_cfg = copy.deepcopy(self.cfg)
        # embed_color is read per-request, so changing it (the UI syncs it
        # on theme switches) must not demand a daemon restart.
        old_sharing = copy.deepcopy(old_cfg.sharing)
        new_sharing = copy.deepcopy(new_cfg.sharing)
        old_sharing.embed_color = new_sharing.embed_color = ""
        restart_required = (
            old_sharing != new_sharing
            or old_cfg.recording.gsr_args != new_cfg.recording.gsr_args
        )

        # Apply live (some settings still require daemon restart, e.g. recorder backend).
        for field in ("recording", "hotkeys", "output", "sharing", "discord"):
            setattr(self.cfg, field, getattr(new_cfg, field))

        apply_error: str | None = None
        if self.apply_config_cb:
            try:
                await self.apply_config_cb()
            except Exception as exc:
                # Keep runtime state stable and reject invalid live changes.
                for field in ("recording", "hotkeys", "output", "sharing", "discord"):
                    setattr(self.cfg, field, getattr(old_cfg, field))

                try:
                    await self.apply_config_cb()
                except Exception as rollback_exc:
                    log.warning("Rollback apply failed: %s", rollback_exc)

                apply_error = str(exc) or exc.__class__.__name__
                log.warning("Live config apply failed; settings saved for next restart: %s", exc)

        # Always persist validated settings, even when live apply fails.
        # This keeps restart-intended config changes from being lost.
        save_cfg(new_cfg)

        if apply_error:
            return web.json_response({
                "ok": True,
                "applied": False,
                "restart_required": True,
                "warning": "Settings saved for next restart. Restart Vice to apply them.",
                "error": apply_error,
            })

        payload = {"ok": True, "applied": True, "restart_required": restart_required}
        if restart_required:
            payload["warning"] = "Some sharing settings require a full app restart to take effect."
        return web.json_response(payload)

    async def _api_status(self, _: web.Request) -> web.Response:
        extra = self.get_status_cb() if self.get_status_cb else {}
        public_url = self.public_base_url()
        return web.json_response({
            "running":  True,
            "version":  __version__,
            "clips":    len(self._clips),
            "local_url": self.local_base_url(),
            "public_url": public_url,
            "base_url": public_url,
            "public_is_tunnel": self.public_is_reachable(),
            **extra,
        })

    async def _api_trigger(self, _: web.Request) -> web.Response:
        if self.trigger_clip_cb:
            asyncio.create_task(self.trigger_clip_cb())
        return web.json_response({"ok": True})

    async def _api_check_update(self, _: web.Request) -> web.Response:
        """Check now, ignoring the daily interval. Returns the newer release
        or null; a failed check is reported as null, never as an error."""
        if not self.check_update_cb:
            return web.json_response({"ok": True, "update": None})
        try:
            found = await self.check_update_cb()
        except Exception as exc:
            log.debug("Manual update check failed: %s", exc)
            found = None
        return web.json_response({"ok": True, "update": found})

    async def _api_quit(self, _: web.Request) -> web.Response:
        """Stop the daemon (browser-mode quit — native window uses pywebview API)."""
        import os, signal as _sig
        response = web.json_response({"ok": True})
        asyncio.get_event_loop().call_later(0.2, lambda: os.kill(os.getpid(), _sig.SIGTERM))
        return response

    # ── WebSocket ─────────────────────────────────────────────────────────────

    async def _ws_handler(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self._ws_clients.add(ws)
        try:
            async for msg in ws:
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            self._ws_clients.discard(ws)
        return ws

    # ── Tunnel (Cloudflare quick tunnel) ──────────────────────────────────────
    #
    # cloudflared is the only supported tunnel. There used to be an SSH
    # fallback via serveo.net, but it produced broken links (serveo prints
    # promotional URLs that got parsed as the tunnel address), is operated
    # by an unaccountable third party, and silently man-in-the-middles all
    # traffic. Failing loudly with an install hint is strictly better.

    async def _start_tunnel(self, port: int) -> None:
        if not shutil.which("cloudflared"):
            await self._tunnel_failed(
                "cloudflared is not installed. Install it to get public share "
                "links (https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/), "
                "or turn off the public tunnel in Settings."
            )
            return
        log.info("Starting Cloudflare Tunnel on port %d", port)
        try:
            self._tunnel_proc = await asyncio.create_subprocess_exec(
                "cloudflared", "tunnel", "--url", f"http://localhost:{port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            await self._tunnel_failed(f"cloudflared failed to start: {exc}")
            return
        asyncio.create_task(self._read_cloudflare_url())

    async def _tunnel_failed(self, reason: str) -> None:
        log.error("Public share tunnel unavailable: %s", reason)
        self._tunnel_url = None
        await self.broadcast({"type": "tunnel_error", "error": reason})

    async def _read_cloudflare_url(self) -> None:
        assert self._tunnel_proc and self._tunnel_proc.stdout
        proc = self._tunnel_proc
        async for raw in proc.stdout:
            # Quick tunnels always live at <random>.trycloudflare.com. Match
            # that exactly: cloudflared's startup banner contains other
            # *.cloudflare.com links (docs, downloads) that must never be
            # mistaken for the tunnel address (issue #100). Keep draining
            # stdout after the URL so process exit is still detected.
            if self._tunnel_url is not None:
                continue
            m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", raw.decode(errors="replace"))
            if m:
                self._tunnel_url = m.group(0)
                log.info("Cloudflare Tunnel URL: %s", self._tunnel_url)
                await self.broadcast({"type": "tunnel_url", "url": self._tunnel_url})
        # stdout closed: cloudflared exited. If that happened before a URL
        # was ever printed, surface it instead of leaving the UI waiting.
        if self._tunnel_url is None and proc is self._tunnel_proc:
            rc = proc.returncode if proc.returncode is not None else await proc.wait()
            await self._tunnel_failed(
                f"cloudflared exited (code {rc}) before providing a tunnel URL. "
                "Check your network or run it manually to see the error."
            )
