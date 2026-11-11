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
Another sound pack (vice/sounds/<pack>) can stand in for the tones.
"""

from __future__ import annotations

import array
import asyncio
import io
import logging
import math
import os
import shutil
import struct
import sys
import wave
from importlib.resources import files as _pkg_files
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlsplit

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

# Sound packs: "classic" is the synthesised tones above. Another pack is a
# folder of WAVs under vice/sounds, 48 kHz stereo 16-bit like the tones (#163),
# named after the sounds. A sound a pack does not have uses one it does
# (_PACK_STANDINS), then the classic tone.
CLASSIC = "classic"
SOUND_PACKS = (CLASSIC, "futuristic")
# Marking a highlight is the same moment as saving a clip (#300).
_PACK_STANDINS = {"highlight": "clip"}


def normalize_pack(pack: object) -> str:
    value = str(pack or "").strip().lower()
    return value if value in SOUND_PACKS else CLASSIC


def _pack_frames(pack: str, name: str) -> Optional[bytes]:
    """The pack's 16-bit frames for *name*, or None when it has none."""
    folder = _pkg_files("vice") / "sounds" / pack
    for candidate in (name, _PACK_STANDINS.get(name)):
        if not candidate:
            continue
        source = folder / f"{candidate}.wav"
        try:
            with wave.open(io.BytesIO(source.read_bytes())) as w:
                if (w.getsampwidth(), w.getnchannels(), w.getframerate()) != (2, _CHANNELS, _SR):
                    log.warning("Sound pack file %s is not 48 kHz stereo 16-bit", source)
                    return None
                return w.readframes(w.getnframes())
        except FileNotFoundError:
            continue
        except (OSError, wave.Error, EOFError) as exc:
            log.warning("Sound pack file %s cannot be read: %s", source, exc)
            return None
    return None


def _scaled_wav(frames: bytes, level: float) -> bytes:
    samples = array.array("h")
    samples.frombytes(frames)
    if sys.byteorder == "big":
        samples.byteswap()
    if level < 1.0:
        samples = array.array("h", (int(v * level) for v in samples))
    if sys.byteorder == "big":
        samples.byteswap()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(_CHANNELS)
        w.setsampwidth(2)
        w.setframerate(_SR)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


# Built per volume rather than once at import, so the setting takes effect
# without a daemon restart. Synthesis is pure Python, so the result is cached.
_wav_cache: dict[tuple[str, int], bytes] = {}


def _clamp_volume(volume: float) -> float:
    try:
        return max(0.0, min(1.0, float(volume)))
    except (TypeError, ValueError):
        return 1.0


_wav_cache_packs: dict[tuple[str, str, int], bytes] = {}


def _wav_for(name: str, volume: float, pack: str = CLASSIC) -> bytes:
    level = _clamp_volume(volume)
    if pack != CLASSIC:
        key3 = (pack, name, int(round(level * 100)))
        wav = _wav_cache_packs.get(key3)
        if wav is None:
            frames = _pack_frames(pack, name)
            if frames is not None:
                wav = _scaled_wav(frames, level)
                _wav_cache_packs[key3] = wav
        if wav is not None:
            return wav
    key = (name, int(round(level * 100)))
    wav = _wav_cache.get(key)
    if wav is None:
        wav = _make_wav(*_SPECS[name], amplitude=_BASE_AMPLITUDE * level)
        _wav_cache[key] = wav
    return wav


# ── Playback ───────────────────────────────────────────────────────────────────

# Stable temp paths so we never accumulate files
_TMP_DIR = Path("/tmp/vice")


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


def _pasted_path(value: str) -> str:
    """The path in a settings value, which is usually pasted: file managers
    copy a location quoted or as a file:// URI (#297)."""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].strip()
    if text.lower().startswith("file://"):
        text = unquote(urlsplit(text).path)
    return text


def resolve_custom_sound(custom: Optional[str]) -> Optional[Path]:
    """A usable path for a user-supplied sound, or None to use the tone.

    Anything unset, missing, empty or unreadable falls back, because a
    mistyped path must never turn into silence: the sound is how you know
    the clip landed.
    """
    if not custom or not str(custom).strip():
        return None
    path = Path(os.path.expanduser(_pasted_path(str(custom))))
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


async def _run_player(player: str, sound: Path) -> Optional[str]:
    """Play *sound*. None when it played, otherwise why it did not."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *_player_cmd(player, sound),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return str(exc)
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        # A custom file can legitimately be longer than a tone, but not so
        # long that it stacks up player processes.
        try:
            proc.kill()
        except ProcessLookupError as exc:
            log.debug("Notification player had already exited: %s", exc)
        return None
    if proc.returncode == 0:
        return None
    return (stderr or b"").decode(errors="replace").strip()[:200] or f"exit code {proc.returncode}"


async def _play(name: str, volume: float, custom: Optional[str] = None,
                pack: str = CLASSIC) -> None:
    player = _find_player()
    if not player:
        log.debug("No audio player found (paplay/aplay/ffplay); skipping notification")
        return

    sound = resolve_custom_sound(custom)
    if sound is not None:
        problem = await _run_player(player, sound)
        if problem is None:
            return
        log.warning("Notification sound %s would not play (%s), using the built-in tone",
                    sound, problem)

    pack = normalize_pack(pack)
    wav_data = _wav_for(name, volume, pack)
    # One file per sound and volume, written only when it is not already
    # there: two clips in quick succession used to rewrite a single shared
    # path while the previous player still had it open.
    level = int(round(_clamp_volume(volume) * 100))
    tone = _TMP_DIR / (f"snd_{name}_{level}.wav" if pack == CLASSIC
                       else f"snd_{pack}_{name}_{level}.wav")
    try:
        _TMP_DIR.mkdir(parents=True, exist_ok=True)
        if not tone.exists() or tone.stat().st_size != len(wav_data):
            tone.write_bytes(wav_data)
    except OSError as exc:
        log.debug("Failed to write notification WAV: %s", exc)
        return

    problem = await _run_player(player, tone)
    if problem is not None:
        log.debug("Audio playback error: %s", problem)


# ── Public helpers (fire-and-forget, safe to call from any async context) ──────

def _fire(name: str, volume: float, custom: Optional[str] = None, pack: str = CLASSIC) -> None:
    # At zero, play nothing rather than playing silence: no temp file, no
    # player process, no device wake-up.
    if _clamp_volume(volume) <= 0.0:
        return
    asyncio.create_task(_play(name, volume, custom, pack))


def play_clip(volume: float = 1.0, custom: Optional[str] = None,
              pack: str = CLASSIC) -> None:
    """Fire-and-forget: play the clip-saved notification sound."""
    _fire("clip", volume, custom, pack)


def play_clip_failed(volume: float = 1.0, custom: Optional[str] = None,
                     pack: str = CLASSIC) -> None:
    """Fire-and-forget: play the clip-failed notification sound."""
    _fire("clip_failed", volume, custom, pack)


def play_session_start(volume: float = 1.0, custom: Optional[str] = None,
                       pack: str = CLASSIC) -> None:
    """Fire-and-forget: play the session-started notification sound."""
    _fire("session_start", volume, custom, pack)


def play_session_end(volume: float = 1.0, custom: Optional[str] = None,
                     pack: str = CLASSIC) -> None:
    """Fire-and-forget: play the session-ended notification sound."""
    _fire("session_end", volume, custom, pack)


def play_highlight(volume: float = 1.0, custom: Optional[str] = None,
                   pack: str = CLASSIC) -> None:
    """Fire-and-forget: play the session-highlight marker sound."""
    _fire("highlight", volume, custom, pack)


def play_screenshot(volume: float = 1.0, custom: Optional[str] = None,
                    pack: str = CLASSIC) -> None:
    """Fire-and-forget: play the screenshot-taken notification sound."""
    _fire("screenshot", volume, custom, pack)


def preview(name: str, volume: float, pack: str) -> None:
    """Play one sound of a pack, for choosing one in Settings."""
    if name in _SPECS:
        _fire(name, volume, None, normalize_pack(pack))
