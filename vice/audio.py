"""
Vice audio notifications — synthesises short WAV tones and plays them
via the first available player: paplay → aplay → ffplay.

Three sounds are pre-generated at import time:
  CLIP_SOUND     — quick two-note ascending ping (clip saved)
  SESSION_START  — three ascending tones (session recording started)
  SESSION_END    — three descending tones (session recording stopped)
  HIGHLIGHT_SOUND — soft single chime (session highlight marked)

All playback is non-blocking (asyncio task).
No external audio files needed — pure Python + stdlib wave module.
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

log = logging.getLogger("vice.audio")

# ── Tone synthesis ─────────────────────────────────────────────────────────────

_SR = 44100  # sample rate


def _tone(freq: float, duration: float, amplitude: float = 0.30) -> bytes:
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
        frames.append(max(-32767, min(32767, int(sample * 32767))))
    return struct.pack(f"<{n}h", *frames)


def _silence(duration: float) -> bytes:
    n = int(_SR * duration)
    return struct.pack(f"<{n}h", *([0] * n))


def _make_wav(*tones: tuple[float, float], gap: float = 0.012) -> bytes:
    """
    Combine one or more (frequency_hz, duration_s) tones into a WAV file
    (in-memory bytes).  A brief silence is inserted between tones.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SR)
        for idx, (freq, dur) in enumerate(tones):
            w.writeframes(_tone(freq, dur))
            if idx < len(tones) - 1:
                w.writeframes(_silence(gap))
    return buf.getvalue()


# ── Pre-generated sounds ───────────────────────────────────────────────────────
#
# Clip saved   : short ascending two-note ping (A5 → C#6)
# Session start: rising C-E-G major arpeggio   (C5 → E5 → G5)
# Session end  : falling G-E-C major arpeggio  (G5 → E5 → C5)

CLIP_SOUND    = _make_wav((880, 0.07), (1109, 0.11))
SESSION_START = _make_wav((523, 0.09), (659, 0.09), (784, 0.13))
SESSION_END   = _make_wav((784, 0.09), (659, 0.09), (523, 0.14))
HIGHLIGHT_SOUND = _make_wav((988, 0.06))


# ── Playback ───────────────────────────────────────────────────────────────────

# Stable temp paths so we never accumulate files
_TMP_DIR   = Path("/tmp/vice")
_TMP_CLIP  = _TMP_DIR / "snd_clip.wav"
_TMP_START = _TMP_DIR / "snd_session_start.wav"
_TMP_END   = _TMP_DIR / "snd_session_end.wav"
_TMP_HL    = _TMP_DIR / "snd_highlight.wav"

# Map sound bytes → stable temp path (written once, reused)
_SOUND_MAP: dict[int, Path] = {
    id(CLIP_SOUND):    _TMP_CLIP,
    id(SESSION_START): _TMP_START,
    id(SESSION_END):   _TMP_END,
    id(HIGHLIGHT_SOUND): _TMP_HL,
}


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


async def _play(wav_data: bytes) -> None:
    player = _find_player()
    if not player:
        log.debug("No audio player found (paplay/aplay/ffplay); skipping notification")
        return

    # Write to the stable temp path (create dir if needed)
    tmp = _SOUND_MAP.get(id(wav_data))
    if tmp is None:
        tmp = _TMP_DIR / "snd_tmp.wav"

    try:
        _TMP_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(wav_data)
    except Exception as exc:
        log.debug("Failed to write notification WAV: %s", exc)
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            *_player_cmd(player, tmp),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
    except Exception as exc:
        log.debug("Audio playback error: %s", exc)


# ── Public helpers (fire-and-forget, safe to call from any async context) ──────

def play_clip() -> None:
    """Fire-and-forget: play the clip-saved notification sound."""
    asyncio.create_task(_play(CLIP_SOUND))


def play_session_start() -> None:
    """Fire-and-forget: play the session-started notification sound."""
    asyncio.create_task(_play(SESSION_START))


def play_session_end() -> None:
    """Fire-and-forget: play the session-ended notification sound."""
    asyncio.create_task(_play(SESSION_END))


def play_highlight() -> None:
    """Fire-and-forget: play the session-highlight marker sound."""
    asyncio.create_task(_play(HIGHLIGHT_SOUND))
