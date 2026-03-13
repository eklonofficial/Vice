"""
Vice share server — HTTP server that powers:
  • The web UI  (/  →  vice/ui/index.html)
  • Discord-embed share pages  (/c/{slug})
  • Direct video/thumbnail serving  (/v/{slug}, /t/{slug})
  • REST API  (/api/*)
  • WebSocket  (/ws) for real-time UI updates

WebSocket event types (server → client):
  {"type": "clip_saved",   "clip":  <clip_json>}
  {"type": "clip_deleted", "slug":  "..."}
  {"type": "status",       "recording": bool, "backend": "..."}
  {"type": "tunnel_url",   "url":   "https://..."}
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import shutil
import socket
import subprocess
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Coroutine, Optional

from importlib.resources import files as _pkg_files

from aiohttp import WSMsgType, web

log = logging.getLogger("vice.share")


def _resolve_ui_index() -> Path | None:
    """Resolve the web UI index path across installed and source checkouts."""
    candidates: list[Path] = []

    # Preferred path for packaged installs.
    try:
        ui = _pkg_files("vice") / "ui" / "index.html"
        candidates.append(Path(str(ui)))
    except Exception:
        pass

    # Fallback for source checkouts / direct execution.
    candidates.append(Path(__file__).resolve().parent / "ui" / "index.html")

    for cand in candidates:
        try:
            if cand.exists() and cand.is_file():
                return cand
        except OSError:
            continue
    return None

# Thumbnails go in the cache dir — separate from the clip files.
THUMB_DIR      = Path.home() / ".cache" / "vice" / "thumbs"
HIGHLIGHTS_DIR = Path.home() / ".local" / "share" / "vice" / "highlights"


def _load_highlights(slug: str) -> list:
    f = HIGHLIGHTS_DIR / f"{slug}.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text())
    except Exception:
        return []


def _save_highlights(slug: str, highlights: list) -> None:
    HIGHLIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    (HIGHLIGHTS_DIR / f"{slug}.json").write_text(json.dumps(highlights))


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


async def _ffprobe(path: Path) -> dict:
    """Return {"width", "height", "duration"} via ffprobe."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        data = json.loads(stdout)
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                return {
                    "width":    s.get("width",    1920),
                    "height":   s.get("height",   1080),
                    "duration": float(s.get("duration", 0)),
                }
    except Exception:
        pass
    return {"width": 1920, "height": 1080, "duration": 0}


async def _make_thumb(path: Path) -> Path:
    """Lazily generate a 640px-wide JPEG thumbnail stored in THUMB_DIR."""
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    thumb = _thumb_path(path)
    if thumb.exists():
        return thumb
    try:
        # Seek a little after the start so we avoid intro black frames.
        # Keep -ss after -i for accurate frame selection.
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(path),
            "-ss", "0.75",
            "-frames:v", "1",
            "-vf", "thumbnail,scale=640:-2",
            "-q:v", "4",
            str(thumb),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=20)
    except Exception:
        pass
    return thumb


_EMBED_PAGE = """\
<!DOCTYPE html>
<html><head>
  <meta charset="utf-8">
  <meta property="og:type"              content="video.other">
  <meta property="og:title"             content="{title}">
  <meta property="og:description"       content="Clipped with Vice on Linux">
  <meta property="og:video"             content="{video_url}">
  <meta property="og:video:secure_url"  content="{video_url}">
  <meta property="og:video:type"        content="video/mp4">
  <meta property="og:video:width"       content="{width}">
  <meta property="og:video:height"      content="{height}">
  <meta property="og:image"             content="{thumb_url}">
  <meta name="twitter:card"             content="player">
  <meta name="twitter:player"           content="{video_url}">
  <meta name="twitter:player:width"     content="{width}">
  <meta name="twitter:player:height"    content="{height}">
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
        self._app  = web.Application()
        self._runner: Optional[web.AppRunner]  = None
        self._site:   Optional[web.TCPSite]    = None

        # slug → Path  (populated from disk on start + runtime additions)
        self._clips: dict[str, Path] = {}
        # slug → {width, height, duration}
        self._meta:  dict[str, dict] = {}

        self._tunnel_proc: Optional[asyncio.subprocess.Process] = None
        self._tunnel_url:  Optional[str] = None
        self._base_url:    Optional[str] = None

        # Connected WebSocket clients
        self._ws_clients: set[web.WebSocketResponse] = set()

        # Injected by ViceDaemon so /api/trigger works
        self.trigger_clip_cb: Optional[Callable[[], Coroutine]] = None
        # Injected so /api/status can report live state
        self.get_status_cb: Optional[Callable[[], dict]] = None
        # Injected so config changes can be applied without restart when possible.
        self.apply_config_cb: Optional[Callable[[], Coroutine]] = None

        self._setup_routes()

    # ── routes ───────────────────────────────────────────────────────────────

    def _setup_routes(self) -> None:
        r = self._app.router

        # Web UI
        r.add_get("/",            self._ui)

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
        r.add_get("/api/clips/{slug}/highlights",         self._api_get_highlights)
        r.add_post("/api/clips/{slug}/highlights",        self._api_add_highlight)
        r.add_patch("/api/clips/{slug}/highlights/{hid}", self._api_patch_highlight)
        r.add_delete("/api/clips/{slug}/highlights/{hid}",self._api_del_highlight)
        r.add_get("/api/config",               self._api_get_config)
        r.add_post("/api/config",              self._api_set_config)
        r.add_get("/api/status",               self._api_status)
        r.add_post("/api/trigger",             self._api_trigger)
        r.add_post("/api/quit",                self._api_quit)
        r.add_post("/api/uninstall",           self._api_uninstall)

        # WebSocket
        r.add_get("/ws", self._ws_handler)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        # Pre-populate from output dir
        out_dir = Path(self.cfg.output.directory)
        if out_dir.exists():
            for mp4 in sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime):
                self._clips[mp4.stem] = mp4

        port = self.cfg.sharing.port
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", port)
        await self._site.start()

        self._base_url = self.cfg.sharing.base_url or f"http://{_local_ip()}:{port}"
        log.info("Vice UI + share server: %s", self._base_url)

        if self.cfg.sharing.cloudflare_tunnel:
            await self._start_tunnel(port)

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
        if self._runner:
            await self._runner.cleanup()

    # ── public helpers (called by ViceDaemon) ─────────────────────────────────

    def add_clip(self, path: Path) -> str:
        """Register a new clip and return its share URL."""
        slug = path.stem
        self._clips[slug] = path
        asyncio.create_task(self._broadcast_clip(slug, path))
        return f"{self._tunnel_url or self._base_url}/c/{slug}"

    def base_url(self) -> Optional[str]:
        return self._tunnel_url or self._base_url

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

    async def _broadcast_clip(self, slug: str, path: Path) -> None:
        meta = await self._get_meta(slug, path)
        await self.broadcast({"type": "clip_saved", "clip": self._clip_json(slug, path, meta)})

    async def _get_meta(self, slug: str, path: Path) -> dict:
        if slug not in self._meta:
            self._meta[slug] = await _ffprobe(path)
        return self._meta[slug]

    def _clip_json(self, slug: str, path: Path, meta: dict) -> dict:
        public_base = self._tunnel_url or self._base_url
        try:
            st = path.stat()
            size = st.st_size
            mtime_ns = st.st_mtime_ns
            created_at = datetime.fromtimestamp(st.st_mtime).isoformat()
        except OSError:
            size, mtime_ns, created_at = 0, 0, ""

        thumb_rev = f"{size}-{mtime_ns}"
        return {
            "slug":       slug,
            "name":       path.name,
            "size":       size,
            "created_at": created_at,
            "duration":   meta.get("duration", 0),
            "width":      meta.get("width",    0),
            "height":     meta.get("height",   0),
            # Keep share links public, but serve media via local relative URLs
            # so the app UI never fetches video through an external tunnel.
            "share_url":  f"{public_base}/c/{slug}",
            "video_url":  f"/v/{slug}",
            # Cache-bust by clip file identity to avoid stale thumbs when slugs are reused.
            "thumb_url":  f"/t/{slug}?v={thumb_rev}",
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
            return web.Response(text=content, content_type="text/html")
        except Exception as exc:
            log.error("Failed reading UI file %s: %s", ui_index, exc)
            return web.Response(
                text="<h1>Vice UI failed to load</h1><p>Check vice logs for details.</p>",
                content_type="text/html",
                status=500,
            )

    async def _embed_page(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()
        meta = await self._get_meta(slug, path)
        base = self._tunnel_url or self._base_url
        html = _EMBED_PAGE.format(
            title=f"Vice clip — {slug}",
            video_url=f"{base}/v/{slug}",
            thumb_url=f"{base}/t/{slug}",
            width=meta.get("width", 1920),
            height=meta.get("height", 1080),
        )
        return web.Response(text=html, content_type="text/html")

    async def _video(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(
            path,
            headers={"Content-Type": "video/mp4", "Accept-Ranges": "bytes"},
        )

    async def _thumb(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()
        t = await _make_thumb(path)
        if not t.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(t, headers={"Content-Type": "image/jpeg"})

    # ── REST handlers ─────────────────────────────────────────────────────────

    async def _api_clips(self, _: web.Request) -> web.Response:
        result = []
        for slug, path in sorted(
            self._clips.items(),
            key=lambda kv: kv[1].stat().st_mtime if kv[1].exists() else 0,
            reverse=True,
        ):
            if not path.exists():
                continue
            meta = await self._get_meta(slug, path)
            result.append(self._clip_json(slug, path, meta))
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
        (HIGHLIGHTS_DIR / f"{slug}.json").unlink(missing_ok=True)
        self._meta.pop(slug, None)
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

        tmp = path.with_suffix(".trimming.mp4")
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", str(start), "-i", str(path),
            "-t",  str(end - start),
            "-c",  "copy", "-movflags", "+faststart",
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

        # Sanitise — no path separators; always .mp4
        new_name = new_name.replace("/", "").replace("\\", "").replace("\0", "")
        if " " in new_name:
            return web.json_response({"ok": False, "error": "Clip name cannot contain spaces"})
        if not new_name.lower().endswith(".mp4"):
            new_name += ".mp4"

        new_path = path.parent / new_name
        if new_path.exists() and new_path != path:
            return web.json_response({"ok": False, "error": "A clip with that name already exists"})

        path.rename(new_path)
        new_slug = new_path.stem

        # Update internal state
        self._clips.pop(slug, None)
        self._clips[new_slug] = new_path
        _purge_slug_thumbs(slug)
        self._meta.pop(slug, None)

        # Rename highlights file if it exists
        old_hl = HIGHLIGHTS_DIR / f"{slug}.json"
        if old_hl.exists():
            HIGHLIGHTS_DIR.mkdir(parents=True, exist_ok=True)
            old_hl.rename(HIGHLIGHTS_DIR / f"{new_slug}.json")

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

    async def _api_set_config(self, req: web.Request) -> web.Response:
        from .config import (
            Config, RecordingConfig, HotkeyConfig, OutputConfig, SharingConfig,
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

        new_cfg = Config(
            recording=RecordingConfig(**{
                k: v for k, v in merged["recording"].items()
                if k in RecordingConfig.__dataclass_fields__
            }),
            hotkeys=HotkeyConfig(**{
                k: v for k, v in merged["hotkeys"].items()
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
        )
        old_cfg = copy.deepcopy(self.cfg)
        restart_required = (
            old_cfg.sharing != new_cfg.sharing
            or old_cfg.recording.gsr_args != new_cfg.recording.gsr_args
        )

        # Apply live (some settings still require daemon restart, e.g. recorder backend).
        for field in ("recording", "hotkeys", "output", "sharing"):
            setattr(self.cfg, field, getattr(new_cfg, field))

        apply_error: str | None = None
        if self.apply_config_cb:
            try:
                await self.apply_config_cb()
            except Exception as exc:
                # Keep runtime state stable and reject invalid live changes.
                for field in ("recording", "hotkeys", "output", "sharing"):
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
        return web.json_response({
            "running":  True,
            "clips":    len(self._clips),
            "base_url": self.base_url(),
            **extra,
        })

    async def _api_trigger(self, _: web.Request) -> web.Response:
        if self.trigger_clip_cb:
            asyncio.create_task(self.trigger_clip_cb())
        return web.json_response({"ok": True})

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

    # ── Tunnel (Cloudflare → SSH/serveo fallback) ─────────────────────────────

    async def _start_tunnel(self, port: int) -> None:
        if shutil.which("cloudflared"):
            log.info("Starting Cloudflare Tunnel on port %d", port)
            self._tunnel_proc = await asyncio.create_subprocess_exec(
                "cloudflared", "tunnel", "--url", f"http://localhost:{port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            asyncio.create_task(self._read_cloudflare_url())
        elif shutil.which("ssh"):
            log.info("cloudflared not found; using SSH tunnel via serveo.net on port %d", port)
            self._tunnel_proc = await asyncio.create_subprocess_exec(
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ServerAliveInterval=30",
                "-o", "ExitOnForwardFailure=yes",
                "-R", f"80:localhost:{port}",
                "serveo.net",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            asyncio.create_task(self._read_serveo_url())
        else:
            log.warning("No tunnel available (install cloudflared for public share links)")

    async def _read_cloudflare_url(self) -> None:
        assert self._tunnel_proc and self._tunnel_proc.stdout
        async for raw in self._tunnel_proc.stdout:
            line = raw.decode()
            if "trycloudflare.com" in line or ".cloudflare.com" in line:
                for word in line.split():
                    if word.startswith("https://"):
                        self._tunnel_url = word.strip()
                        log.info("Cloudflare Tunnel URL: %s", self._tunnel_url)
                        await self.broadcast({"type": "tunnel_url", "url": self._tunnel_url})
                        break

    async def _read_serveo_url(self) -> None:
        assert self._tunnel_proc and self._tunnel_proc.stdout
        async for raw in self._tunnel_proc.stdout:
            line = raw.decode()
            # serveo prints: "Forwarding HTTP traffic from https://xxxx.serveo.net"
            for word in line.split():
                if word.startswith("https://") and "serveo.net" in word:
                    self._tunnel_url = word.strip()
                    log.info("serveo.net Tunnel URL: %s", self._tunnel_url)
                    await self.broadcast({"type": "tunnel_url", "url": self._tunnel_url})
                    break
