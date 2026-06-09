"""Shared ffprobe helpers for clip files.

Used by both the recorder (clip finalization, trimming) and the share
server (metadata, thumbnails). Kept in one place so duration handling
behaves identically everywhere.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from pathlib import Path
from typing import Optional

log = logging.getLogger("vice.media")

# Suffix patterns for temp files written during in-place edits
# (trim / watermark / remux). Leftovers mean a previous run was
# interrupted mid-edit; they are safe to delete at daemon startup.
TEMP_FILE_GLOBS = ("*.trim.mp4", "*.wm.mp4", "*.fix.mp4")


async def probe_media(path: Path) -> Optional[dict]:
    """Probe *path* with ffprobe.

    Returns ``{"width", "height", "duration"}`` or ``None`` when ffprobe
    fails or the file has no video stream.

    Duration prefers the container (format) value over the stream value:
    fragmented MP4 — which gpu-screen-recorder writes for replay clips —
    has no per-stream duration tag, so reading only the stream field
    reports 0 for perfectly healthy files.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        data = json.loads(stdout)
    except FileNotFoundError:
        log.error("ffprobe not found — install ffmpeg to read clip metadata")
        return None
    except Exception as exc:
        log.debug("ffprobe failed for %s: %s", path.name, exc)
        return None

    video = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if video is None:
        return None

    duration = _parse_duration(data.get("format", {}).get("duration"))
    if duration <= 0:
        duration = _parse_duration(video.get("duration"))
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "duration": duration,
    }


def _parse_duration(raw) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


async def get_duration(path: Path) -> float:
    """Duration of *path* in seconds, or 0.0 when it cannot be read."""
    meta = await probe_media(path)
    return meta["duration"] if meta else 0.0


def cleanup_temp_files(directory: Path) -> None:
    """Delete leftover in-place-edit temp files from interrupted runs."""
    if not directory.is_dir():
        return
    for pattern in TEMP_FILE_GLOBS:
        for stale in directory.glob(pattern):
            try:
                stale.unlink()
                log.info("Removed stale temp file %s", stale.name)
            except OSError as exc:
                log.warning("Could not remove stale temp file %s: %s", stale, exc)
