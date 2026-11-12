"""
Per-clip audio track settings and the copy of a clip that gets sent.

The viewer's Audio tracks list switches each recorded source on or off and
sets its level. Those settings are kept beside the clip, never written into
it, so a source switched off can be switched back on. Whatever leaves the
machine (a share link, the Discord embed, a copied file) is a copy with one
audio track mixed from the sources that are on, so the ones switched off are
not in it at all.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
from pathlib import Path
from typing import Optional

from .recorder import MIX_TRACK_TITLE, _AUDIO_ENCODERS
from .runtime import actual_home_dir

log = logging.getLogger("vice.track_mix")

TRACK_MIX_DIR = actual_home_dir() / ".local" / "share" / "vice" / "track_mix"

MAX_VOLUME = 2.0

# What one track is set to: switched on, and its level from 0 to MAX_VOLUME.
Setting = tuple[bool, float]
AS_RECORDED: Setting = (True, 1.0)


# ── settings store ───────────────────────────────────────────────────────────

def sidecar_path(slug: str) -> Path:
    return TRACK_MIX_DIR / f"{slug}.json"


def load(slug: str) -> dict[int, Setting]:
    """A clip's track settings by audio track index; tracks left as recorded
    are not listed."""
    try:
        data = json.loads(sidecar_path(slug).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Cannot read the audio track settings of %s: %s", slug, exc)
        return {}
    tracks = data.get("tracks") if isinstance(data, dict) else None
    if not isinstance(tracks, dict):
        return {}
    out: dict[int, Setting] = {}
    for key, value in tracks.items():
        try:
            index = int(key)
            enabled = value["enabled"]
            volume = float(value["volume"])
        except (KeyError, TypeError, ValueError):
            continue
        if index >= 0 and type(enabled) is bool and math.isfinite(volume):
            out[index] = (enabled, min(max(volume, 0.0), MAX_VOLUME))
    return out


def save(slug: str, settings: dict[int, Setting]) -> None:
    """Keep *settings*, dropping tracks left as recorded; nothing left removes
    the file."""
    changed = {i: s for i, s in settings.items() if not as_recorded(s)}
    path = sidecar_path(slug)
    if not changed:
        path.unlink(missing_ok=True)
        return
    TRACK_MIX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"version": 1, "tracks": {
            str(i): {"enabled": e, "volume": round(v, 3)} for i, (e, v) in sorted(changed.items())
        }}, fh)
    tmp.replace(path)


def move(old_slug: str, new_slug: str) -> None:
    try:
        sidecar_path(old_slug).replace(sidecar_path(new_slug))
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not move the audio track settings of %s: %s", old_slug, exc)


def copy(slug: str, to_slug: str) -> None:
    """A copy of the clip (a trim kept as a copy) keeps its settings: the
    tracks are the same."""
    try:
        shutil.copyfile(sidecar_path(slug), sidecar_path(to_slug))
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not copy the audio track settings of %s: %s", slug, exc)


def delete(slug: str) -> None:
    try:
        sidecar_path(slug).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not delete the audio track settings of %s: %s", slug, exc)


def as_recorded(setting: Setting) -> bool:
    enabled, volume = setting
    return enabled and abs(volume - 1.0) <= 0.005


def to_json(settings: dict[int, Setting]) -> dict[str, dict]:
    return {str(i): {"enabled": e, "volume": v} for i, (e, v) in sorted(settings.items())}


# ── what gets sent ───────────────────────────────────────────────────────────

def send_levels(titles: list[str], parts: Optional[list[bool]],
                settings: dict[int, Setting]) -> Optional[dict[int, float]]:
    """The tracks to mix into the copy that gets sent, with their levels, or
    None when the clip itself can go as it is.

    The clip goes as it is when nothing has been changed and its first track
    is the whole recording, because that is all a player or Discord plays.
    Otherwise the copy mixes the sources that are on. A track another one
    already holds (the combined track, an app inside a desktop track) is
    left out of the mix so nothing is heard twice.
    """
    changed = any(not as_recorded(settings.get(i, AS_RECORDED)) for i in range(len(titles)))
    combined = {i for i, title in enumerate(titles)
                if parts is not None and not parts[i] and title == MIX_TRACK_TITLE}
    sources = [i for i in range(len(titles))
               if i not in combined and (parts is None or parts[i])]
    whole_first = parts is None or bool(combined) or len(sources) < 2
    if not changed and whole_first:
        return None
    levels: dict[int, float] = {}
    for i in sources:
        enabled, volume = settings.get(i, AS_RECORDED)
        if enabled and volume > 0:
            levels[i] = volume
    return levels


def levels_key(levels: dict[int, float]) -> str:
    """Short digest naming one mix, so a changed setting makes a new copy."""
    canonical = json.dumps({str(i): round(v, 3) for i, v in sorted(levels.items())})
    return hashlib.sha1(canonical.encode()).hexdigest()[:10]


def mix_cmd(path: Path, tmp: Path, levels: dict[int, float],
            codecs: list[tuple[str, int]]) -> list[str]:
    """ffmpeg for the copy that gets sent: video stream-copied and one audio
    track mixed from *levels*, in the clip's own audio codec at the highest
    bitrate of what goes into it. No sources means no audio."""
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path)]
    sources = sorted(i for i in levels if 0 <= i < len(codecs))
    if sources:
        chains = []
        for i in sources:
            gain = "" if abs(levels[i] - 1.0) <= 0.005 else f",volume={levels[i]:g}"
            chains.append(f"[0:a:{i}]aformat=channel_layouts=stereo{gain}[s{i}]")
        inputs = "".join(f"[s{i}]" for i in sources)
        chains.append(f"{inputs}amix=inputs={len(sources)}:normalize=0:duration=longest[mix]"
                      if len(sources) > 1 else f"{inputs}anull[mix]")
        args += ["-filter_complex", ";".join(chains)]
    args += ["-map", "0:v?"]
    if sources:
        name = codecs[sources[0]][0]
        rate = max(codecs[i][1] for i in sources)
        args += ["-map", "[mix]", "-c:a", _AUDIO_ENCODERS.get(name, "aac")]
        if rate:
            args += ["-b:a", str(rate)]
        args += ["-metadata:s:a:0", f"title={MIX_TRACK_TITLE}",
                 "-metadata:s:a:0", f"handler_name={MIX_TRACK_TITLE}"]
    else:
        args += ["-an"]
    args += ["-c:v", "copy"]
    if path.suffix.lower() == ".mp4":
        args += ["-movflags", "+faststart"]
    return args + ["-y", str(tmp)]
