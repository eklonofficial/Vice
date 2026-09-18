"""
Vice audio notifications: synthesises short WAV tones and plays them
via the first available player: paplay → aplay → ffplay.

Five sounds are synthesised on demand:
  clip:           quick two-note ascending ping (clip saved)
  clip_failed:    low descending pair (the clip did not save)
  session_start:  three ascending tones (session recording started)
  session_end:    three descending tones (session recording stopped)
  highlight:      soft single chime (session highlight marked)

Each is built at the requested volume (notifications.sound_volume) and
cached, so changing the setting applies immediately. Volume 0 plays nothing.

All playback is non-blocking (asyncio task).
No external audio files needed, pure Python + stdlib wave module.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import shutil
import struct
import wave
from pathlib import Path
from typing import Optional

from .oscompat import IS_WINDOWS, runtime_dir

log = logging.getLogger("vice.audio")

# ── Tone synthesis ─────────────────────────────────────────────────────────────

# Matched to the usual PipeWire graph so a notification needs neither a
# resampler nor a channel remixer. Both live in libspa-audioconvert, which is
# where pipewire-pulse was aborting when a clip was saved (#163).
_SR = 48000
_CHANNELS = 2

# Loudness at 100%. Everything scales off this, so the tones keep their
# relative balance at every setting.
_BASE_AMPLITUDE = 0.30


def _tone(freq: float, duration: float, amplitude: float = _BASE_AMPLITUDE) -> bytes:
    """
    Generate a single sine-wave tone as raw 16-bit little-endian PCM bytes.
    Applies a short linear attack and release envelope to prevent clicks.
    """
    n = int(_SR * duration)
    attack  = min(int(_SR * 0.010), n // 4)   # 10 ms attack
    release = min(int(_SR * 0.025), n // 3)   # 25 ms release

    frames: list[int] = []
    for i in range(n):
        t = i / _SR
        if i < attack:
            env = i / attack
        elif i >= n - release:
            env = (n - i) / release
        else:
            env = 1.0
        sample = amplitude * env * math.sin(2.0 * math.pi * freq * t)
        value = max(-32767, min(32767, int(sample * 32767)))
        frames.extend((value,) * _CHANNELS)
    return struct.pack(f"<{len(frames)}h", *frames)


def _silence(duration: float) -> bytes:
    n = int(_SR * duration) * _CHANNELS
    return struct.pack(f"<{n}h", *([0] * n))


def _make_wav(*tones: tuple[float, float], gap: float = 0.012,
              amplitude: float = _BASE_AMPLITUDE) -> bytes:
    """
    Combine one or more (frequency_hz, duration_s) tones into a WAV file
    (in-memory bytes).  A brief silence is inserted between tones.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(_CHANNELS)
        w.setsampwidth(2)
        w.setframerate(_SR)
        for idx, (freq, dur) in enumerate(tones):
            w.writeframes(_tone(freq, dur, amplitude))
            if idx < len(tones) - 1:
                w.writeframes(_silence(gap))
    return buf.getvalue()


# ── Sounds ─────────────────────────────────────────────────────────────────────
#
# Clip saved   : short ascending two-note ping (A5 → C#6)
# Clip failed  : low descending two-note pair  (A4 → E4)
# Session start: rising C-E-G major arpeggio   (C5 → E5 → G5)
# Session end  : falling G-E-C major arpeggio  (G5 → E5 → C5)
# Screenshot   : two clipped high ticks         (E6 → A6)
#
# The clip tone plays the moment the hotkey lands, before the save is known
# to have worked, because flushing a long buffer takes seconds. Failure needs
# its own sound or that confirmation is a lie (#154). Deliberately low and
# falling so it is unmistakable mid-game without being an alarm.

_SPECS: dict[str, tuple[tuple[float, float], ...]] = {
    "clip":          ((880, 0.07), (1109, 0.11)),
    "clip_failed":   ((440, 0.10), (330, 0.18)),
    "session_start": ((523, 0.09), (659, 0.09), (784, 0.13)),
    "session_end":   ((784, 0.09), (659, 0.09), (523, 0.14)),
    "highlight":     ((988, 0.06),),
    # Short and bright, so it lands like a shutter rather than the clip ping.
    "screenshot":    ((1318, 0.04), (1760, 0.05)),
}

# Built per volume rather than once at import, so the setting takes effect
# without a daemon restart. Synthesis is pure Python, so the result is cached.
_wav_cache: dict[tuple[str, int], bytes] = {}


def _clamp_volume(volume: float) -> float:
    try:
        return max(0.0, min(1.0, float(volume)))
    except (TypeError, ValueError):
        return 1.0


def _wav_for(name: str, volume: float) -> bytes:
    level = _clamp_volume(volume)
    key = (name, int(round(level * 100)))
    wav = _wav_cache.get(key)
    if wav is None:
        wav = _make_wav(*_SPECS[name], amplitude=_BASE_AMPLITUDE * level)
        _wav_cache[key] = wav
    return wav


# ── Playback ───────────────────────────────────────────────────────────────────

# Stable temp paths so we never accumulate files
_TMP_DIR = runtime_dir()


def _find_player() -> Optional[str]:
    for p in ("paplay", "aplay", "ffplay"):
        found = shutil.which(p)
        if found:
            return found
    return None


def _player_cmd(player: str, wav_path: Path) -> list[str]:
    if "ffplay" in player:
        return [player, "-nodisp", "-autoexit", "-loglevel", "quiet", str(wav_path)]
    return [player, str(wav_path)]


def resolve_custom_sound(custom: Optional[str]) -> Optional[Path]:
    """A usable path for a user-supplied sound, or None to use the tone.

    Anything unset, missing, empty or unreadable falls back, because a
    mistyped path must never turn into silence: the sound is how you know
    the clip landed.
    """
    if not custom or not str(custom).strip():
        return None
    path = Path(os.path.expanduser(str(custom).strip()))
    try:
        if not path.is_file():
            missing = "does not exist" if not path.exists() else "is not a file"
            log.warning("Notification sound %s %s, using the built-in tone", path, missing)
            return None
        if not os.access(path, os.R_OK):
            log.warning("Notification sound %s cannot be read, using the built-in tone", path)
            return None
        if path.stat().st_size == 0:
            log.warning("Notification sound %s is empty, using the built-in tone", path)
            return None
    except OSError as exc:
        log.warning("Notification sound %s cannot be used (%s), using the built-in tone", path, exc)
        return None
    return path


def _play_windows_blocking(wav: Optional[bytes], path: Optional[Path]) -> None:
    import winsound
    if wav is not None:
        winsound.PlaySound(wav, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
    elif path is not None:
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_NODEFAULT)


async def _decode_to_wav(path: Path) -> Optional[bytes]:
    """winsound only plays WAV, so anything else goes through ffmpeg first."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-t", "10", "-f", "wav", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        data, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (OSError, asyncio.TimeoutError) as exc:
        log.debug("Could not decode %s: %s", path, exc)
        return None
    return data if proc.returncode == 0 and data else None


async def _play_windows(name: str, volume: float, custom: Optional[str]) -> None:
    """Windows plays WAV itself, straight from memory, so no player program
    and no temp file are involved."""
    sound = resolve_custom_sound(custom)
    wav: Optional[bytes] = None
    path: Optional[Path] = None
    if sound is None:
        wav = _wav_for(name, volume)
    elif sound.suffix.lower() == ".wav":
        path = sound
    else:
        wav = await _decode_to_wav(sound)
        if wav is None:
            wav = _wav_for(name, volume)
    try:
        await asyncio.to_thread(_play_windows_blocking, wav, path)
    except Exception as exc:
        log.debug("Audio playback error: %s", exc)


async def _play(name: str, volume: float, custom: Optional[str] = None) -> None:
    if IS_WINDOWS:
        await _play_windows(name, volume, custom)
        return
    player = _find_player()
    if not player:
        log.debug("No audio player found (paplay/aplay/ffplay); skipping notification")
        return

    sound = resolve_custom_sound(custom)
    if sound is None:
        wav_data = _wav_for(name, volume)
        # One file per sound and volume, written only when it is not already
        # there: two clips in quick succession used to rewrite a single shared
        # path while the previous player still had it open.
        level = int(round(_clamp_volume(volume) * 100))
        sound = _TMP_DIR / f"snd_{name}_{level}.wav"
        try:
            _TMP_DIR.mkdir(parents=True, exist_ok=True)
            if not sound.exists() or sound.stat().st_size != len(wav_data):
                sound.write_bytes(wav_data)
        except OSError as exc:
            log.debug("Failed to write notification WAV: %s", exc)
            return

    try:
        proc = await asyncio.create_subprocess_exec(
            *_player_cmd(player, sound),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        # A custom file can legitimately be longer than a tone, but not so
        # long that it stacks up player processes.
        try:
            proc.kill()
        except Exception as exc:
            log.debug("Notification player had already exited: %s", exc)
    except Exception as exc:
        log.debug("Audio playback error: %s", exc)


# ── Public helpers (fire-and-forget, safe to call from any async context) ──────

def _fire(name: str, volume: float, custom: Optional[str] = None) -> None:
    # At zero, play nothing rather than playing silence: no temp file, no
    # player process, no device wake-up.
    if _clamp_volume(volume) <= 0.0:
        return
    asyncio.create_task(_play(name, volume, custom))


def play_clip(volume: float = 1.0, custom: Optional[str] = None) -> None:
    """Fire-and-forget: play the clip-saved notification sound."""
    _fire("clip", volume, custom)


def play_clip_failed(volume: float = 1.0, custom: Optional[str] = None) -> None:
    """Fire-and-forget: play the clip-failed notification sound."""
    _fire("clip_failed", volume, custom)


def play_session_start(volume: float = 1.0, custom: Optional[str] = None) -> None:
    """Fire-and-forget: play the session-started notification sound."""
    _fire("session_start", volume, custom)


def play_session_end(volume: float = 1.0, custom: Optional[str] = None) -> None:
    """Fire-and-forget: play the session-ended notification sound."""
    _fire("session_end", volume, custom)


def play_highlight(volume: float = 1.0, custom: Optional[str] = None) -> None:
    """Fire-and-forget: play the session-highlight marker sound."""
    _fire("highlight", volume, custom)


def play_screenshot(volume: float = 1.0, custom: Optional[str] = None) -> None:
    """Fire-and-forget: play the screenshot-taken notification sound."""
    _fire("screenshot", volume, custom)
