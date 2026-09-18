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
TEMP_FILE_GLOBS = ("*.trim.mp4", "*.wm.mp4", "*.fix.mp4", "*.trimming.mp4",
                   "*.trim.mkv", "*.wm.mkv", "*.fix.mkv", "*.trimming.mkv",
                   "*.export.mp4",
                   # Left behind when an image annotation is interrupted
                   # between writing the new picture and moving it into place.
                   "*.annotating.png", "*.annotating.jpg", "*.annotating.jpeg")


async def communicate_with_timeout(
    proc: asyncio.subprocess.Process, timeout: float,
) -> tuple[Optional[bytes], Optional[bytes]]:
    """Collect a finite media command, reaping it on timeout or cancellation.

    Keep draining its pipes during cleanup. Waiting only for process exit can
    deadlock when buffered stdout or stderr has filled the pipe transport.
    Recording processes have their own graceful finalization and must not use
    this helper.
    """
    communication = asyncio.create_task(proc.communicate())
    try:
        return await asyncio.wait_for(asyncio.shield(communication), timeout)
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await communication


async def probe_media(path: Path) -> Optional[dict]:
    """Probe *path* with ffprobe.

    Returns ``{"width", "height", "duration", "vcodec", "audio_streams"}``
    or ``None`` when ffprobe fails or the file has no video stream.
    """
    meta, _ = await probe_media_detailed(path)
    return meta


async def probe_media_detailed(path: Path) -> tuple[Optional[dict], str]:
    """Probe *path*, returning the metadata and why it failed.

    Same result as :func:`probe_media`, plus ffprobe's own explanation when
    the probe comes back empty. Vice used to run ffprobe with ``-v quiet``
    and discard stderr, so a clip that could not be read produced a log line
    saying only that JSON parsing failed. That cost #154 three round trips
    with the reporter, so the reason is kept and logged now.

    Duration prefers the container (format) value over the stream value:
    fragmented MP4, which gpu-screen-recorder writes for replay clips, has
    no per-stream duration tag, so reading only the stream field reports 0
    for perfectly healthy files.
    """
    stderr = b""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await communicate_with_timeout(proc, timeout=15)
        data = json.loads(stdout)
    except FileNotFoundError:
        reason = "ffprobe not found, install ffmpeg to read clip metadata"
        log.error("%s", reason)
        return None, reason
    except asyncio.TimeoutError:
        reason = "ffprobe timed out after 15s"
        log.warning("Could not read %s: %s", path.name, reason)
        return None, reason
    except Exception as exc:
        reason = _ffprobe_reason(stderr, path) or str(exc)
        log.warning("Could not read %s: %s", path.name, reason)
        return None, reason

    if proc.returncode != 0 or not data:
        reason = _ffprobe_reason(stderr, path) or f"ffprobe exited {proc.returncode}"
        log.warning("Could not read %s: %s", path.name, reason)
        return None, reason

    video = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if video is None:
        reason = "the file has no video stream"
        log.warning("Could not read %s: %s", path.name, reason)
        return None, reason

    duration = _parse_duration(data.get("format", {}).get("duration"))
    if duration <= 0:
        duration = _parse_duration(video.get("duration"))
    audio_streams = sum(
        1 for s in data.get("streams", []) if s.get("codec_type") == "audio"
    )
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "duration": duration,
        "vcodec": (video.get("codec_name") or "").lower(),
        "audio_streams": audio_streams,
    }, ""


def _ffprobe_reason(stderr: Optional[bytes], path: Optional[Path] = None) -> str:
    """The most useful line of ffprobe's stderr, short enough for a toast.

    ffprobe prefixes its message with the full path it was given, which is
    redundant next to a log line that already names the file and too long
    for the UI, so it comes off.
    """
    if not stderr:
        return ""
    lines = [
        line.strip()
        for line in stderr.decode("utf-8", "replace").splitlines()
        if line.strip()
    ]
    if not lines:
        return ""
    reason = lines[-1]
    if path:
        prefix = f"{path}: "
        if reason.startswith(prefix):
            reason = reason[len(prefix):]
    return reason[:300]


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
