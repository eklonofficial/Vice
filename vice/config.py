"""Vice configuration — reads/writes ~/.config/vice/config.toml."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from dataclasses import dataclass, field, fields, asdict
from typing import Optional

log = logging.getLogger("vice.config")

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None  # type: ignore[assignment]

import tomli_w

from .runtime import actual_home_dir, resolve_path

CONFIG_DIR = actual_home_dir() / ".config" / "vice"
CONFIG_PATH = CONFIG_DIR / "config.toml"
CLIP_DURATION_MIN = 5
CLIP_DURATION_MAX = 1800
BUFFER_DURATION_MAX = 1800

# ── Hotkey combinations ──────────────────────────────────────────────────────
# A hotkey is a "+"-joined evdev string: zero or more modifiers followed by one
# main key, e.g. "KEY_LEFTALT+KEY_F9". A bare key ("KEY_F9") is just the
# zero-modifier case, so configs written before combos existed keep working.

# Right/left modifier variants collapse to the left name so either physical key
# (e.g. either Alt) triggers the same combo.
MODIFIER_CANON = {
    "KEY_RIGHTCTRL": "KEY_LEFTCTRL",
    "KEY_RIGHTALT": "KEY_LEFTALT",
    "KEY_RIGHTSHIFT": "KEY_LEFTSHIFT",
    "KEY_RIGHTMETA": "KEY_LEFTMETA",
}
# Every evdev name we treat as a modifier (canonical + right-hand variants).
MODIFIER_KEYS = set(MODIFIER_CANON) | set(MODIFIER_CANON.values())
# Stable emit order so press order never changes the stored/ matched string.
MODIFIER_ORDER = {
    "KEY_LEFTCTRL": 0,
    "KEY_LEFTALT": 1,
    "KEY_LEFTSHIFT": 2,
    "KEY_LEFTMETA": 3,
}


def normalize_combo(combo: str) -> str:
    """Canonicalize a hotkey string: collapse right→left modifiers, order them
    Ctrl/Alt/Shift/Meta, and put the single main key last.

    Malformed input (empty, only modifiers, more than one main key) is returned
    trimmed and otherwise untouched so callers that validate strictly can still
    flag it, while lenient callers degrade gracefully.
    """
    tokens = [t.strip() for t in str(combo or "").split("+") if t.strip()]
    if not tokens:
        return ""
    mods: list[str] = []
    mains: list[str] = []
    for tok in tokens:
        canon = MODIFIER_CANON.get(tok, tok)
        if canon in MODIFIER_ORDER:
            if canon not in mods:
                mods.append(canon)
        else:
            mains.append(tok)
    if len(mains) != 1:
        # Not a well-formed combo — hand it back as-is for the caller to judge.
        return "+".join(tokens)
    mods.sort(key=lambda m: MODIFIER_ORDER[m])
    return "+".join(mods + mains)


@dataclass
class RecordingConfig:
    # How many seconds to keep in the rolling buffer.
    buffer_duration: int = 120
    # How many seconds to save when you hit the clip hotkey.
    clip_duration: int = 20
    fps: int = 60
    # None = backend default capture target. Otherwise a backend-specific display/output id.
    display: Optional[str] = None
    # Unified capture source selector. Takes priority over `display` when set.
    # Format: "display:<id>" for a monitor, "window:<id>" for a specific window,
    # "window:focused" for the currently focused window, or None for auto.
    capture_source: Optional[str] = None
    # None = auto-detect from display. E.g. "1920x1080".
    resolution: Optional[str] = None
    # "auto" | "h264_nvenc" | "hevc_nvenc" | "av1_nvenc" | "h264_vaapi" | "hevc_vaapi" | "av1_vaapi" | "libx264" | "libx265" | "copy"
    encoder: str = "auto"
    # ffmpeg -crf equivalent; lower = better quality. Used only for libx264/libx265.
    crf: int = 23
    # "auto" | "gsr" | "wf-recorder" | "ffmpeg"
    backend: str = "auto"
    # Include desktop audio in clips.
    capture_audio: bool = True
    # Loudness applied to new clips when they are saved: 1.0 = unchanged,
    # 0.5 = half, up to 2.0. Ignored when separate audio_tracks are set.
    desktop_volume: float = 1.0
    microphone_volume: float = 1.0
    # Include microphone input in clips/session recordings.
    capture_microphone: bool = False
    # Which microphone to capture when capture_microphone is on.
    # "default_input" follows the system default; "device:<name>" pins a
    # specific input (same ids as gsr_audio_source).
    microphone_source: str = "default_input"
    # How to handle mic capture when wf-recorder cannot combine desktop + mic.
    # "prompt" | "backend_fallback" | "mic_only"
    wf_microphone_strategy: str = "prompt"
    # Burn the "Clipped with Vice" watermark into exported clips.
    # Disabled by default to avoid encoding spikes while gaming.
    apply_watermark: bool = False
    # PulseAudio/PipeWire sink name. "default" works for most setups.
    audio_sink: str = "default"
    # Optional extra flags appended to gpu-screen-recorder commands.
    # Example: "-k hevc -bm cbr -q 20000 -fm cfr"
    gsr_args: str = ""
    # gpu-screen-recorder desktop audio source. Examples:
    # default_output, device:alsa_output.pci.monitor, app:firefox, app-inverse:firefox
    gsr_audio_source: str = "default_output"
    # Where gpu-screen-recorder keeps the replay buffer. "ram" is fastest but
    # long buffers get expensive; "auto" switches to disk above 10 minutes.
    # "auto" | "ram" | "disk"
    gsr_replay_storage: str = "auto"
    # Clip container: "mp4" or "mkv". mkv survives crashes better and is the
    # base for multi-track audio, but Discord/browser embeds need mp4.
    # Applies to the gpu-screen-recorder backend.
    container: str = "mp4"
    # Record these audio sources as separate tracks instead of mixing them
    # (gpu-screen-recorder backend). Each entry is one track, same ids as
    # gsr_audio_source. Example: ["default_output", "default_input",
    # "app:Discord"]. Empty = mix into a single track (the default).
    audio_tracks: list[str] = field(default_factory=list)
    # When separate tracks are configured, also record an extra track 1 that
    # mixes every source. Players, Discord, and share embeds play track 1, so
    # this keeps shared clips complete while the separates stay editable.
    audio_tracks_mix_first: bool = False


@dataclass
class HotkeyClipPreset:
    # evdev key name and the duration this key saves from the rolling buffer.
    key: str = ""
    duration: int = 60


@dataclass
class HotkeyConfig:
    # evdev key name. Run `vice list-keys` to discover names.
    clip: str = "KEY_F9"
    # Optional: toggle continuous recording on/off.
    toggle: Optional[str] = None
    # Additional clip hotkeys with their own durations.
    clip_presets: list[HotkeyClipPreset] = field(default_factory=list)


@dataclass
class OutputConfig:
    directory: str = str(actual_home_dir() / "Videos" / "Vice")
    filename_format: str = "vice_%Y%m%d_%H%M%S.mp4"
    # Append the detected game to clip filenames (Vice_Clip_4_Overwatch-2.mp4).
    # Uses the same curated games list as Discord Rich Presence; clips save
    # untagged when no known game is focused or the compositor is unsupported.
    tag_clips_with_game: bool = True
    # Auto-file each clip into a per-game playlist when a known game is focused.
    # Independent of filename tagging; uses the same detection.
    auto_playlist_by_game: bool = True
    # Override the Vice_Clip_N[_Game] clip filename. Supports $n, $date, $time
    # and $game; empty keeps the default naming. Session recordings are
    # unaffected. e.g. "clip_$date_$time" -> clip_2026-07-19_1600.mp4
    clip_name_template: str = ""


@dataclass
class SharingConfig:
    enabled: bool = True
    port: int = 8765
    # Port for the public share-only server. Defaults to sharing.port + 1.
    public_port: Optional[int] = None
    # Expose via a Cloudflare quick tunnel (requires the cloudflared binary).
    cloudflare_tunnel: bool = True
    # Override the public base URL shown in share links (e.g. if behind reverse proxy).
    base_url: Optional[str] = None
    # Accent color for share-page embeds (Discord sidebar strip etc.).
    # Synced from the UI theme; must be a #rrggbb hex value.
    embed_color: str = "#0099ff"


@dataclass
class DiscordCustomGame:
    # Display name shown in the Discord activity card (e.g. "My Game").
    name: str = ""
    # Substrings matched (case-insensitive) against the active window's process
    # name and class. First hit wins. E.g. ["mygame.exe", "MyGame"].
    matches: list[str] = field(default_factory=list)


@dataclass
class DiscordConfig:
    # Default ON for new configs; only shows when Discord is running and a known game is focused.
    enabled: bool = True
    # Override the default Vice Discord application ID (for users who want
    # custom app branding / icons in their activity card).
    client_id_override: Optional[str] = None
    # Keep the activity card up while the matched game's process is running,
    # not only while its window is focused.
    persist_while_running: bool = True
    # User-managed game additions on top of the bundled games.json database.
    custom_games: list[DiscordCustomGame] = field(default_factory=list)


@dataclass
class UpdatesConfig:
    # Ask GitHub once a day whether a newer release exists. Nothing is sent
    # about the user or their clips; turning this off stops the request.
    check_on_start: bool = True


@dataclass
class NotificationsConfig:
    # Loudness of the clip and session tones, 0.0 to 1.0. 0 plays nothing at
    # all rather than playing silence, so no audio player is spawned.
    sound_volume: float = 1.0


@dataclass
class Config:
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    sharing: SharingConfig = field(default_factory=SharingConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    updates: UpdatesConfig = field(default_factory=UpdatesConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)


def _merge(defaults: dict, overrides: dict) -> dict:
    """Recursively merge overrides into defaults."""
    result = dict(defaults)
    for k, v in overrides.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


def normalize_clip_presets(raw, *, strict: bool = False) -> list[HotkeyClipPreset]:
    """Parse clip preset rows from TOML/API data.

    Manual config edits should not crash daemon startup, so invalid rows are
    ignored unless strict=True is requested by the settings API.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        if strict:
            raise ValueError("hotkeys.clip_presets must be a list")
        return []

    presets: list[HotkeyClipPreset] = []
    for idx, item in enumerate(raw, start=1):
        if isinstance(item, HotkeyClipPreset):
            key = item.key
            duration_raw = item.duration
        elif isinstance(item, dict):
            key = item.get("key", "")
            duration_raw = item.get("duration", "")
        else:
            if strict:
                raise ValueError(f"clip preset #{idx} must be an object")
            continue

        key = normalize_combo(str(key or "").strip())
        try:
            if isinstance(duration_raw, bool):
                raise ValueError
            duration = int(duration_raw)
        except (TypeError, ValueError):
            if strict:
                raise ValueError(f"clip preset #{idx} duration must be a number") from None
            continue

        if not key:
            if strict:
                raise ValueError(f"clip preset #{idx} needs a key")
            continue
        if duration < CLIP_DURATION_MIN or duration > CLIP_DURATION_MAX:
            if strict:
                raise ValueError(
                    f"clip preset #{idx} duration must be between "
                    f"{CLIP_DURATION_MIN} and {CLIP_DURATION_MAX} seconds"
                )
            continue
        presets.append(HotkeyClipPreset(key=key, duration=duration))
    return presets


def validate_hotkeys(hotkeys: HotkeyConfig) -> None:
    seen: set[str] = set()
    primary = normalize_combo((hotkeys.clip or "").strip())
    if primary:
        seen.add(primary)

    for preset in hotkeys.clip_presets:
        key = normalize_combo((preset.key or "").strip())
        if not key:
            raise ValueError("clip preset keys cannot be empty")
        if preset.duration < CLIP_DURATION_MIN or preset.duration > CLIP_DURATION_MAX:
            raise ValueError(
                f"clip preset duration for {key} must be between "
                f"{CLIP_DURATION_MIN} and {CLIP_DURATION_MAX} seconds"
            )
        if key in seen:
            raise ValueError(f"duplicate clip hotkey: {key}")
        seen.add(key)


def effective_clip_bindings(cfg: Config) -> list[tuple[str, int]]:
    bindings: list[tuple[str, int]] = []
    seen: set[str] = set()

    primary = normalize_combo((cfg.hotkeys.clip or "").strip())
    if primary:
        bindings.append((primary, int(cfg.recording.clip_duration)))
        seen.add(primary)

    for preset in cfg.hotkeys.clip_presets:
        key = normalize_combo((preset.key or "").strip())
        if not key or key in seen:
            continue
        bindings.append((key, int(preset.duration)))
        seen.add(key)
    return bindings


def ensure_buffer_covers_clip_presets(cfg: Config) -> None:
    durations = [int(cfg.recording.clip_duration)]
    durations.extend(int(p.duration) for p in cfg.hotkeys.clip_presets)
    cfg.recording.buffer_duration = max(int(cfg.recording.buffer_duration), max(durations))


def clamp_recording_limits(cfg: Config) -> None:
    """Keep durations inside supported bounds regardless of where the config
    came from (hand-edited TOML, old versions, the settings API)."""
    rc = cfg.recording

    def _clamped(value, fallback: int, low: int, high: int, name: str) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            log.warning("recording.%s=%r is not a number — using %d", name, value, fallback)
            return fallback
        bounded = max(low, min(number, high))
        if bounded != number:
            log.warning("recording.%s=%d is out of range — clamped to %d", name, number, bounded)
        return bounded

    rc.clip_duration = _clamped(
        rc.clip_duration, 20, CLIP_DURATION_MIN, CLIP_DURATION_MAX, "clip_duration"
    )
    rc.buffer_duration = _clamped(
        rc.buffer_duration, 120, rc.clip_duration, BUFFER_DURATION_MAX, "buffer_duration"
    )

    storage = (getattr(rc, "gsr_replay_storage", "") or "auto").strip().lower()
    if storage not in {"auto", "ram", "disk"}:
        log.warning("recording.gsr_replay_storage=%r is unknown — using auto", storage)
        storage = "auto"
    rc.gsr_replay_storage = storage

    for name in ("desktop_volume", "microphone_volume"):
        try:
            volume = float(getattr(rc, name, 1.0))
        except (TypeError, ValueError):
            log.warning("recording.%s=%r is not a number — using 1.0", name, getattr(rc, name))
            volume = 1.0
        setattr(rc, name, max(0.0, min(volume, 2.0)))


def _known_keys(cls, data: dict) -> dict:
    """Drop keys the dataclass does not define, with a warning.

    A config written by a newer Vice (or a typo) must degrade to defaults,
    not crash the daemon at startup: 1.2.x died on sight of the recording
    keys 1.3.0 added.
    """
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        log.warning(
            "Ignoring unknown config keys in [%s]: %s",
            cls.__name__, ", ".join(unknown),
        )
    return {k: v for k, v in data.items() if k in known}


def load() -> Config:
    """Load config from disk, filling in defaults for any missing keys."""
    if not CONFIG_PATH.exists():
        cfg = Config()
        save(cfg)
        return cfg

    with CONFIG_PATH.open("rb") as fh:
        if tomllib is None:
            raise RuntimeError(
                "tomllib/tomli is required for Python < 3.11. Install tomli: pip install tomli"
            )
        raw = tomllib.load(fh)

    def _nested_asdict(obj) -> dict:
        if hasattr(obj, "__dataclass_fields__"):
            return {k: _nested_asdict(v) for k, v in asdict(obj).items()}
        return obj

    defaults = _nested_asdict(Config())
    merged = _merge(defaults, raw)
    output = dict(merged.get("output", {}))
    output["directory"] = str(resolve_path(output.get("directory", OutputConfig().directory)))

    discord_raw = dict(merged.get("discord", {}))
    custom_games_raw = discord_raw.pop("custom_games", []) or []
    custom_games = [
        DiscordCustomGame(
            name=str(g.get("name", "")),
            matches=[str(m) for m in (g.get("matches") or [])],
        )
        for g in custom_games_raw
        if isinstance(g, dict)
    ]
    hotkeys_raw = dict(merged.get("hotkeys", {}))
    hotkeys_raw["clip_presets"] = normalize_clip_presets(
        hotkeys_raw.get("clip_presets", []),
        strict=False,
    )

    cfg = Config(
        recording=RecordingConfig(**_known_keys(RecordingConfig, merged.get("recording", {}))),
        hotkeys=HotkeyConfig(**_known_keys(HotkeyConfig, hotkeys_raw)),
        output=OutputConfig(**_known_keys(OutputConfig, output)),
        sharing=SharingConfig(**_known_keys(SharingConfig, merged.get("sharing", {}))),
        discord=DiscordConfig(**_known_keys(DiscordConfig, discord_raw), custom_games=custom_games),
        updates=UpdatesConfig(**_known_keys(UpdatesConfig, merged.get("updates", {}))),
        notifications=NotificationsConfig(
            **_known_keys(NotificationsConfig, merged.get("notifications", {}))
        ),
    )
    ensure_buffer_covers_clip_presets(cfg)
    clamp_recording_limits(cfg)
    return cfg


def save(cfg: Config) -> None:
    """Persist config to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    from dataclasses import asdict as _asdict

    def _clean(d):
        """Convert None to sentinel string so tomli_w can handle it."""
        if isinstance(d, dict):
            return {k: _clean(v) for k, v in d.items()}
        return d

    data = _clean(_asdict(cfg))
    # Remove None values — TOML doesn't have null; omitting is cleaner.
    def _drop_none(d):
        if isinstance(d, dict):
            return {k: _drop_none(v) for k, v in d.items() if v is not None}
        return d

    with CONFIG_PATH.open("wb") as fh:
        tomli_w.dump(_drop_none(data), fh)
