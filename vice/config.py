"""Vice configuration — reads/writes ~/.config/vice/config.toml."""

from __future__ import annotations

import sys
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None  # type: ignore[assignment]

import tomli_w

CONFIG_DIR = Path.home() / ".config" / "vice"
CONFIG_PATH = CONFIG_DIR / "config.toml"


@dataclass
class RecordingConfig:
    # How many seconds to keep in the rolling buffer.
    buffer_duration: int = 120
    # How many seconds to save when you hit the clip hotkey.
    clip_duration: int = 15
    fps: int = 60
    # None = auto-detect from display. E.g. "1920x1080".
    resolution: Optional[str] = None
    # "auto" | "h264_nvenc" | "hevc_nvenc" | "libx264" | "libx265" | "h264_vaapi" | "copy"
    encoder: str = "auto"
    # ffmpeg -crf equivalent; lower = better quality. Used only for libx264/libx265.
    crf: int = 23
    # "auto" | "gsr" | "wf-recorder" | "ffmpeg"
    backend: str = "auto"
    # Include desktop audio in clips.
    capture_audio: bool = True
    # Burn the "Clipped with Vice" watermark into exported clips.
    # Disabled by default to avoid encoding spikes while gaming.
    apply_watermark: bool = False
    # PulseAudio/PipeWire sink name. "default" works for most setups.
    audio_sink: str = "default"


@dataclass
class HotkeyConfig:
    # evdev key name. Run `vice list-keys` to discover names.
    clip: str = "KEY_F9"
    # Optional: toggle continuous recording on/off.
    toggle: Optional[str] = None


@dataclass
class OutputConfig:
    directory: str = str(Path.home() / "Videos" / "Vice")
    filename_format: str = "vice_%Y%m%d_%H%M%S.mp4"


@dataclass
class SharingConfig:
    enabled: bool = True
    port: int = 8765
    # Expose via a public tunnel (cloudflared if available, SSH/serveo as fallback).
    cloudflare_tunnel: bool = True
    # Override the public base URL shown in share links (e.g. if behind reverse proxy).
    base_url: Optional[str] = None


@dataclass
class Config:
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    sharing: SharingConfig = field(default_factory=SharingConfig)


def _merge(defaults: dict, overrides: dict) -> dict:
    """Recursively merge overrides into defaults."""
    result = dict(defaults)
    for k, v in overrides.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


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

    return Config(
        recording=RecordingConfig(**merged.get("recording", {})),
        hotkeys=HotkeyConfig(**merged.get("hotkeys", {})),
        output=OutputConfig(**merged.get("output", {})),
        sharing=SharingConfig(**merged.get("sharing", {})),
    )


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
