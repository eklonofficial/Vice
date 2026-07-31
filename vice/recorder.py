"""
Vice recorder — manages the continuous capture buffer and clip extraction.

Backend priority (auto mode):
  1. gpu-screen-recorder (gsr)  — best: native replay buffer, NVIDIA NVENC, Wayland + X11
  2. wf-recorder                — good: Wayland (wlroots, GNOME portal, KDE portal)
  3. ffmpeg x11grab             — fallback: X11 only

Environment detection
---------------------
* Wayland  : $WAYLAND_DISPLAY is set
* Hyprland : $HYPRLAND_INSTANCE_SIGNATURE is set (subset of Wayland)
* GNOME    : $XDG_CURRENT_DESKTOP contains "GNOME"
* KDE      : $XDG_CURRENT_DESKTOP contains "KDE"
* NVIDIA   : /proc/driver/nvidia/version exists or nvidia-smi succeeds
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable, List, Optional

from .config import Config
from .media import get_duration as _get_duration
from .runtime import recover_wayland_display, resolve_path

log = logging.getLogger("vice.recorder")

# ──────────────────────────────────────────────────────────────────────────────
# Environment helpers
# ──────────────────────────────────────────────────────────────────────────────

def _is_wayland() -> bool:
    return recover_wayland_display()


def _is_x11() -> bool:
    return bool(os.environ.get("DISPLAY")) and not _is_wayland()


def _is_nvidia() -> bool:
    if Path("/proc/driver/nvidia/version").exists():
        return True
    return shutil.which("nvidia-smi") is not None and _run_ok(["nvidia-smi", "-L"])


def _run_ok(cmd: list[str]) -> bool:
    try:
        subprocess.run(cmd, capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def _has(tool: str) -> bool:
    return shutil.which(tool) is not None


def _combine_process_output(*chunks) -> str:
    parts: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        if isinstance(chunk, bytes):
            text = chunk.decode(errors="replace")
        else:
            text = str(chunk)
        text = text.strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def _run_command_capture(cmd: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, _combine_process_output(proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as exc:
        return 124, _combine_process_output(exc.stdout, exc.stderr)
    except Exception as exc:
        return 1, str(exc)


def _wf_runtime_error(raw: str) -> Optional[str]:
    lowered = raw.lower()
    if "compositor doesn't support wlr-screencopy-unstable-v1" in lowered:
        return "compositor doesn't support wlr-screencopy-unstable-v1"
    if "failed to start screencopy" in lowered:
        return "failed to start screencopy"
    if "xdg-desktop-portal" in lowered and "failed" in lowered:
        return "failed to use xdg-desktop-portal screencast"
    return None


@lru_cache(maxsize=1)
def _wf_help_text() -> str:
    if not _has("wf-recorder"):
        return ""
    outputs: list[str] = []
    for args in (["wf-recorder", "--help"], ["wf-recorder", "-h"]):
        _, out = _run_command_capture(args, timeout=2.0)
        if out:
            outputs.append(out)
        if outputs:
            break
    return "\n".join(outputs)


def _wf_supports_flag(flag: str) -> bool:
    return flag in _wf_help_text()


def _wf_list_outputs() -> tuple[list[dict], Optional[str]]:
    if not _has("wf-recorder"):
        return [], None

    _, out = _run_command_capture(["wf-recorder", "-L"], timeout=3.0)
    runtime_error = _wf_runtime_error(out)
    if runtime_error:
        return [], runtime_error

    lowered = out.lower()
    if (
        "invalid option -- 'l'" in lowered
        or "unsupported command line argument" in lowered
        or "unrecognized option" in lowered
    ):
        return [], "installed wf-recorder does not support output listing (-L)"

    return _parse_wf_display_lines(out), None


def _extra_gsr_args(raw: str) -> list[str]:
    """Parse user-provided gpu-screen-recorder CLI args."""
    if not raw.strip():
        return []

    # Allow env var + tilde expansion so users can reference shell-style values.
    expanded = os.path.expanduser(os.path.expandvars(raw))
    monitor = _desktop_audio_source("default")
    expanded = expanded.replace("$(pactl get-default-sink).monitor", monitor)
    expanded = expanded.replace("(pactl get-default-sink).monitor", monitor)

    try:
        args = shlex.split(expanded)
    except ValueError as exc:
        log.warning("Invalid recording.gsr_args ignored: %s", exc)
        return []

    # Convenience placeholder for desktop monitor source.
    replaced: list[str] = []
    for arg in args:
        val = arg.replace("{default_sink_monitor}", monitor)
        val = val.replace("$(pactl get-default-sink).monitor", monitor)
        val = val.replace("(pactl get-default-sink).monitor", monitor)
        replaced.append(val)
    return replaced




def _gsr_has_any_flag(args: list[str], *flags: str) -> bool:
    for arg in args:
        for f in flags:
            if arg == f or arg.startswith(f + "="):
                return True
    return False


def _gsr_codec_for_encoder(encoder: str) -> Optional[str]:
    if encoder == "auto":
        return None
    if encoder in {"h264", "h264_nvenc", "h264_vaapi", "libx264"}:
        return "h264"
    if encoder in {"hevc", "hevc_nvenc", "hevc_vaapi", "libx265"}:
        return "hevc"
    if encoder in {"av1", "av1_nvenc", "av1_vaapi", "libaom-av1", "libsvtav1"}:
        return "av1"
    return None


@lru_cache(maxsize=None)
def _gsr_help_text() -> str:
    if not _has("gpu-screen-recorder"):
        return ""
    _, out = _run_command_capture(["gpu-screen-recorder", "--help"], timeout=3.0)
    return out


def _gsr_supports_flag(flag: str) -> bool:
    return flag in _gsr_help_text()


def _gsr_wants_disk_replay(rc) -> bool:
    storage = (getattr(rc, "gsr_replay_storage", "") or "auto").strip().lower()
    if storage == "disk":
        return True
    try:
        buffer_duration = int(rc.buffer_duration)
    except (TypeError, ValueError):
        return False
    return storage == "auto" and buffer_duration > 600


def _classify_gsr_source(source: str) -> str:
    """Rough kind of a GSR -a value: "monitor" (desktop audio), "input"
    (microphone), "app", or "unknown"."""
    value = (source or "").strip()
    if value == "default_output":
        return "monitor"
    if value == "default_input":
        return "input"
    if value.startswith(("app:", "app-inverse:")):
        return "app"
    if value.startswith("device:"):
        return "monitor" if value.endswith(".monitor") else "input"
    return "unknown"


def _container(rc) -> str:
    """Validated clip container ("mp4" or "mkv")."""
    value = (getattr(rc, "container", "") or "mp4").strip().lower()
    if value not in {"mp4", "mkv"}:
        log.warning("Unknown recording.container=%r — using mp4", value)
        return "mp4"
    return value


def _gsr_audio_args(rc, *, split_for_volume: bool = True) -> list[str]:
    """GSR audio flags: one -a per configured track, or one mixed input.

    gpu-screen-recorder records each -a flag as its own audio track;
    sources joined with "|" inside one flag are mixed together.
    split_for_volume=False keeps a single mixed track even when the volume
    sliders are active (session recordings get no save-time mix pass).
    """
    tracks = [
        str(t).strip()
        for t in (getattr(rc, "audio_tracks", None) or [])
        if str(t).strip()
    ]
    if not _captures_desktop_audio(rc):
        # The desktop-audio toggle silences desktop/app sources but must not
        # take the microphone down with them (#110).
        kept: list[str] = []
        for track in tracks:
            parts = [
                p for p in track.split("|")
                if p and _classify_gsr_source(p) == "input"
            ]
            if parts:
                kept.append("|".join(parts))
        tracks = kept
    if tracks:
        if _captures_microphone(rc):
            mic = _gsr_mic_source(rc)
            if not any(mic in track.split("|") for track in tracks):
                tracks.append(mic)
        if getattr(rc, "audio_tracks_mix_first", False) and len(tracks) > 1:
            mix: list[str] = []
            for track in tracks:
                for part in track.split("|"):
                    if part and part not in mix:
                        mix.append(part)
            tracks.insert(0, "|".join(mix))
        args: list[str] = []
        for track in tracks:
            args += ["-a", track]
        return args
    if (
        split_for_volume
        and _captures_desktop_audio(rc)
        and _captures_microphone(rc)
        and _volume_mix_wanted(rc)
    ):
        # Record desktop and mic as separate tracks so the save-time volume
        # pass can balance them before mixing down.
        desktop_source = (getattr(rc, "gsr_audio_source", "") or "default_output").strip() or "default_output"
        _warn_if_desktop_source_is_mic(desktop_source)
        return ["-a", desktop_source, "-a", _gsr_mic_source(rc)]
    merged = _gsr_audio_input(rc)
    return ["-a", merged] if merged else []


def _gsr_resolution_args(rc, extra: list[str]) -> list[str]:
    """GSR `-s WxH` flag for the configured output resolution, if any."""
    resolution = (getattr(rc, "resolution", None) or "").strip()
    if not resolution or _gsr_has_any_flag(extra, "-s"):
        return []
    if not re.fullmatch(r"\d+x\d+", resolution):
        log.warning(
            "Ignoring recording.resolution=%r — expected WIDTHxHEIGHT (e.g. 1920x1080)",
            resolution,
        )
        return []
    return ["-s", resolution]


def _gsr_sanitize_args(args: list[str], blocked_flags: set[str]) -> list[str]:
    """Drop flags that Vice manages internally to avoid conflicting values."""
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in blocked_flags:
            i += 2
            continue
        if any(arg.startswith(f + "=") for f in blocked_flags):
            i += 1
            continue
        out.append(arg)
        i += 1
    return out


def _selected_display_id(rc) -> Optional[str]:
    value = getattr(rc, "display", None)
    if value is None:
        return None
    selected = str(value).strip()
    return selected or None


def _detect_x11_resolution() -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["xdpyinfo"], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            if "dimensions:" in line:
                return line.split()[1]  # e.g. "1920x1080"
    except Exception:
        pass
    return None


def _detect_window_resolution(rc) -> Optional[str]:
    """If a window capture source is selected, return its resolution as WxH."""
    source = getattr(rc, "capture_source", None)
    if not source or not source.startswith("window:"):
        return None
    window_id = source[len("window:"):]
    if window_id == "focused":
        return None  # focused window changes; can't pre-detect
    # Try xwininfo (X11 window IDs)
    geom = _x11_window_geometry(window_id)
    if geom:
        return f"{geom[2]}x{geom[3]}"
    return None


def _unquote_gsr_ident(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].strip()
    return value


def _parse_gsr_display_lines(raw: str) -> list[dict]:
    displays: list[dict] = []
    generic_targets = {
        "window",
        "focused",
        "screen",
        "screen-direct-force",
        "portal",
    }
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        line = line.lstrip("-*• ").strip()
        lowered = line.lower()
        # Filter GSR diagnostic noise — `--list-capture-options` is known to
        # print error strings (e.g. "gsr error: for_each_active_monitor_output_drm
        # failed, ...") on systems where DRM enumeration fails. They share stdout
        # with the real options because _run_command_capture merges stdout+stderr.
        if (
            lowered.startswith("gsr error")
            or lowered.startswith("error:")
            or "for_each_active_monitor" in lowered
            or "failed to open" in lowered
        ):
            continue
        if not line or lowered.startswith("monitor") or lowered in generic_targets:
            continue
        ident = line
        label = line
        if "|" in line:
            head, tail = line.split("|", 1)
            ident = _unquote_gsr_ident(head)
            tail = tail.strip()
            label = f"{ident} ({tail})" if tail else ident
        elif match := re.match(r'^"([^"]+)"\s*(.*)$', line):
            ident = match.group(1).strip()
            detail = match.group(2).strip()
            label = f"{ident} {detail}".strip()
        elif ":" in line:
            head, _ = line.split(":", 1)
            if head and " " not in head:
                ident = _unquote_gsr_ident(head)
        else:
            ident = _unquote_gsr_ident(line.split()[0])
            label = ident if ident != line else label
        if ident and ident.lower() not in generic_targets:
            displays.append({"id": ident, "label": label})
    return displays


def _parse_wf_display_lines(raw: str) -> list[dict]:
    displays: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        line = line.lstrip("-*• ").strip()
        if not line:
            continue
        ident = line
        if ":" in line:
            head, _ = line.split(":", 1)
            if head and " " not in head:
                ident = head.strip()
        else:
            ident = line.split()[0]
        displays.append({"id": ident, "label": line})
    return displays


def _parse_xrandr_display_lines(raw: str) -> list[dict]:
    displays: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("Monitors:"):
            continue
        match = re.match(
            r"^\d+:\s+([+*]*)(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)\s+(\S+)$",
            line,
        )
        if not match:
            continue
        flags, _, width, height, x, y, ident = match.groups()
        displays.append(
            {
                "id": ident,
                "label": f"{ident} ({width}x{height})",
                "width": int(width),
                "height": int(height),
                "x": int(x),
                "y": int(y),
                "primary": "*" in flags,
            }
        )
    return displays


def _display_options(backend: str) -> list[dict]:
    if backend == "gsr":
        if not _has("gpu-screen-recorder"):
            return []
        for cmd in (
            ["gpu-screen-recorder", "--list-monitors"],
            ["gpu-screen-recorder", "--list-capture-options"],
        ):
            code, out = _run_command_capture(cmd, timeout=3.0)
            if code == 0 and out:
                displays = _parse_gsr_display_lines(out)
                if displays:
                    return displays
            lowered = out.lower()
            if "--list-capture-options" in cmd and (
                "unrecognized option" in lowered
                or "unknown option" in lowered
                or "invalid option" in lowered
            ):
                continue
        return []

    if backend == "wf-recorder":
        displays, _ = _wf_list_outputs()
        return displays

    if backend == "ffmpeg":
        if not _has("xrandr"):
            return []
        try:
            out = subprocess.check_output(
                ["xrandr", "--listactivemonitors"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return []
        return _parse_xrandr_display_lines(out)

    return []


def resolve_display_backend(preferred: str = "auto") -> str:
    if preferred == "gsr" or (preferred == "auto" and _has("gpu-screen-recorder")):
        return "gsr"
    if preferred == "wf-recorder" or (preferred == "auto" and _is_wayland() and _has("wf-recorder")):
        return "wf-recorder"
    if preferred == "ffmpeg" or _is_x11():
        return "ffmpeg"
    return preferred


def list_display_options(preferred: str = "auto") -> dict:
    backend = resolve_display_backend(preferred)
    warning = None
    if backend == "wf-recorder":
        displays, warning = _wf_list_outputs()
    else:
        displays = _display_options(backend)
    if preferred in {"gsr", "wf-recorder", "ffmpeg"} and not displays:
        warning = warning or f"Could not list displays for {backend}."
    return {"backend": backend, "displays": displays, "warning": warning}


def _parse_gsr_audio_lines(raw: str, prefix: str) -> list[dict]:
    sources: list[dict] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        value = line.strip().lstrip("-*• ").strip()
        if not value:
            continue
        lowered = value.lower()
        if (
            lowered.startswith("gsr error")
            or lowered.startswith("error:")
            or lowered.startswith("warning:")
            or "failed to" in lowered
        ):
            continue
        # GSR lists devices as "name|Human description"; application
        # entries are bare names. Only the name is a valid -a value.
        name, _, description = value.partition("|")
        name = name.strip()
        description = description.strip()
        if not name:
            continue
        if name in {"default_output", "default_input"}:
            continue  # covered by the hardcoded entries with friendly labels
        source_id = name if name.startswith(("device:", "app:", "app-inverse:")) else f"{prefix}:{name}"
        if source_id in seen:
            continue
        seen.add(source_id)
        label_prefix = "Application" if prefix == "app" else "Device"
        label_value = description or (source_id.split(":", 1)[1] if ":" in source_id else source_id)
        sources.append({
            "id": source_id,
            "label": f"{label_prefix}: {label_value}",
            "kind": _classify_gsr_source(source_id),
        })
    return sources


def list_gsr_audio_sources() -> dict:
    sources = [
        {"id": "default_output", "label": "Default output", "kind": "monitor"},
        {"id": "default_input", "label": "Default input", "kind": "input"},
    ]
    warning = None
    if not _has("gpu-screen-recorder"):
        return {"sources": sources, "warning": "gpu-screen-recorder is not installed."}

    code, out = _run_command_capture(["gpu-screen-recorder", "--list-audio-devices"], timeout=3.0)
    if code == 0:
        sources.extend(_parse_gsr_audio_lines(out, "device"))
    elif out:
        warning = out.splitlines()[-1].strip()

    code, out = _run_command_capture(["gpu-screen-recorder", "--list-application-audio"], timeout=3.0)
    if code == 0:
        apps = _parse_gsr_audio_lines(out, "app")
        sources.extend(apps)
        for app in apps:
            name = app["id"].split(":", 1)[1]
            sources.append({"id": f"app-inverse:{name}", "label": f"All except: {name}", "kind": "app"})
    elif out and not warning:
        warning = out.splitlines()[-1].strip()

    deduped: list[dict] = []
    seen: set[str] = set()
    for source in sources:
        source_id = str(source.get("id") or "")
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        deduped.append(source)
    return {"sources": deduped, "warning": warning}


def list_gsr_windows() -> list[dict]:
    """Parse gpu-screen-recorder --list-capture-options for window entries.

    Returns [{"id": str, "label": str, "kind": "window"}] for each
    capture-eligible application window GSR can see.
    """
    if not _has("gpu-screen-recorder"):
        return []
    for cmd in (
        ["gpu-screen-recorder", "--list-capture-options"],
        ["gpu-screen-recorder", "--list-windows"],
    ):
        code, out = _run_command_capture(cmd, timeout=3.0)
        if code != 0 or not out:
            continue
        lowered = out.lower()
        if "unrecognized option" in lowered or "unknown option" in lowered:
            continue
        windows: list[dict] = []
        seen: set[str] = set()
        # GSR lists windows as lines that look like identifiers with titles.
        # Known generic targets that are NOT specific windows:
        generic_targets = {
            "window", "focused", "screen", "screen-direct-force",
            "portal", "monitor",
        }
        for line in out.splitlines():
            value = line.strip().lstrip("-*• ").strip()
            if not value:
                continue
            low = value.lower()
            if (
                low.startswith("gsr error") or low.startswith("error:")
                or "for_each_active_monitor" in low or "failed to" in low
            ):
                continue
            if low in generic_targets or low.startswith("monitor"):
                continue
            # Parse "identifier|Description" or bare identifier
            ident = value
            label = value
            if "|" in value:
                head, tail = value.split("|", 1)
                ident = _unquote_gsr_ident(head)
                tail = tail.strip()
                label = f"{ident} — {tail}" if tail else ident
            elif match := re.match(r'^"([^"]+)"\s*(.*)$', value):
                ident = match.group(1).strip()
                detail = match.group(2).strip()
                label = f"{ident} — {detail}".strip() if detail else ident
            if not ident or ident.lower() in generic_targets or ident in seen:
                continue
            seen.add(ident)
            windows.append({"id": ident, "label": label, "kind": "window"})
        if windows:
            return windows
    return []


def list_capture_sources(preferred: str = "auto") -> dict:
    """Return monitors AND application windows as a unified source list.

    Response: {
        "backend": str,
        "sources": [{"id": str, "label": str, "kind": "monitor"|"window"}],
        "warning": str|None
    }
    """
    backend = resolve_display_backend(preferred)
    warning = None

    # ── Monitors (existing logic) ──
    monitor_result = list_display_options(preferred)
    monitors = [
        {**m, "kind": "monitor"}
        for m in (monitor_result.get("displays") or [])
    ]
    if monitor_result.get("warning"):
        warning = monitor_result["warning"]

    # ── Application windows ──
    windows: list[dict] = []
    if backend == "gsr":
        windows = list_gsr_windows()
    else:
        # For wf-recorder and ffmpeg, enumerate windows via compositor IPC.
        # This runs in a thread because it shells out.
        try:
            from .active_window import list_capture_windows
            raw_windows = list_capture_windows()
            windows = [
                {
                    "id": w["id"],
                    "label": f"{w.get('app', '')} — {w.get('title', '')}".strip(" —"),
                    "kind": "window",
                }
                for w in raw_windows
            ]
        except Exception as exc:
            log.debug("Window enumeration failed: %s", exc)

    sources = monitors + windows
    return {"backend": backend, "sources": sources, "warning": warning}


def _resolve_capture_source(rc, backend: str) -> Optional[dict]:
    """Resolve the unified capture_source config value.

    capture_source format: "display:<id>" | "window:<id>" | "window:focused" | None
    Falls back to the legacy `display` field when capture_source is not set.
    """
    source = getattr(rc, "capture_source", None)
    if not source:
        # Legacy fallback: display field = monitor only
        return _resolve_display_option(rc, backend)

    if source == "window:focused":
        return {"id": "focused", "label": "Focused window", "kind": "window"}

    if source.startswith("window:"):
        window_id = source[len("window:"):]
        return {"id": window_id, "label": window_id, "kind": "window"}

    if source.startswith("display:"):
        display_id = source[len("display:"):]
        # Look up in the display options for a label
        for opt in _display_options(backend):
            if str(opt.get("id", "")) == display_id:
                return {**opt, "kind": "monitor"}
        return {"id": display_id, "label": display_id, "kind": "monitor"}

    return None


def _resolve_display_option(rc, backend: str) -> Optional[dict]:
    selected = _selected_display_id(rc)
    if not selected:
        return None
    normalized_selected = selected.split("|", 1)[0].strip()
    for opt in _display_options(backend):
        ident = str(opt.get("id") or "").strip()
        label = str(opt.get("label") or "").strip()
        if selected == ident or selected == label or normalized_selected == ident:
            return opt
    log.warning("Configured display %r is unavailable for backend %s; using auto capture", selected, backend)
    return None


def _default_gsr_capture_target() -> str:
    return "screen"


def _gsr_capture_target(rc) -> str:
    """Determine the -w value for gpu-screen-recorder.

    Checks the unified capture_source first, then falls back to the legacy
    display field, then defaults to "screen".
    """
    resolved = _resolve_capture_source(rc, "gsr")
    if resolved:
        return str(resolved["id"])
    return _default_gsr_capture_target()


def _wf_capture_target(rc) -> Optional[str]:
    """Determine the output (display) for wf-recorder.

    Note: wf-recorder does not support per-window capture natively.
    When a window source is selected, this returns None (auto) and the caller
    should warn the user or fall back to GSR.
    """
    resolved = _resolve_capture_source(rc, "wf-recorder")
    if resolved and resolved.get("kind") == "window":
        # wf-recorder cannot capture individual windows — return None to signal
        # the caller that this needs special handling.
        return None
    if resolved:
        return str(resolved["id"])
    return None


def _resolution_scale_filter(resolution: Optional[str]) -> Optional[str]:
    if not resolution:
        return None
    if "x" not in resolution:
        return None
    width, height = resolution.lower().split("x", 1)
    if not width.isdigit() or not height.isdigit():
        return None
    return f"scale={width}:{height}"


def _merge_ffmpeg_filters(flags: list[str], extra_filter: Optional[str]) -> list[str]:
    if not extra_filter:
        return flags
    out: list[str] = []
    i = 0
    merged = False
    while i < len(flags):
        arg = flags[i]
        if arg == "-vf" and i + 1 < len(flags):
            out += ["-vf", f"{extra_filter},{flags[i + 1]}"]
            merged = True
            i += 2
            continue
        out.append(arg)
        i += 1
    if not merged:
        return ["-vf", extra_filter, *out]
    return out


def _x11_window_geometry(window_id: str) -> Optional[tuple[int, int, int, int]]:
    """Get (x, y, width, height) for an X11 window via xwininfo."""
    try:
        out = subprocess.check_output(
            ["xwininfo", "-id", window_id],
            text=True, stderr=subprocess.DEVNULL, timeout=2.0,
        )
    except Exception:
        return None
    x = y = w = h = 0
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Absolute upper-left X:"):
            try: x = int(line.split(":")[-1].strip())
            except ValueError: pass
        elif line.startswith("Absolute upper-left Y:"):
            try: y = int(line.split(":")[-1].strip())
            except ValueError: pass
        elif line.startswith("Width:"):
            try: w = int(line.split(":")[-1].strip())
            except ValueError: pass
        elif line.startswith("Height:"):
            try: h = int(line.split(":")[-1].strip())
            except ValueError: pass
    if w > 0 and h > 0:
        return (x, y, w, h)
    return None


def _ffmpeg_x11_input_args(rc) -> tuple[list[str], Optional[str]]:
    display = os.environ.get("DISPLAY", ":0")

    # Check unified capture_source first
    resolved = _resolve_capture_source(rc, "ffmpeg")
    if resolved and resolved.get("kind") == "window":
        # Capture a specific window by its geometry
        window_id = resolved["id"]
        geom = _x11_window_geometry(window_id)
        if geom:
            x, y, w, h = geom
            video_size = f"{w}x{h}"
            input_display = f"{display}+{x},{y}"
            return (
                ["-f", "x11grab", "-framerate", str(rc.fps),
                 "-video_size", video_size, "-i", input_display],
                _resolution_scale_filter(rc.resolution),
            )
        log.warning("Could not get geometry for window %s; falling back to full display", window_id)

    if resolved and resolved.get("kind") == "monitor":
        # Use the resolved monitor directly
        display_id = resolved["id"]
        for opt in _display_options("ffmpeg"):
            if str(opt.get("id", "")) == display_id and "width" in opt:
                video_size = f"{opt['width']}x{opt['height']}"
                input_display = f"{display}+{opt['x']},{opt['y']}"
                return (
                    ["-f", "x11grab", "-framerate", str(rc.fps),
                     "-video_size", video_size, "-i", input_display],
                    _resolution_scale_filter(rc.resolution),
                )

    # Legacy display field fallback
    selected = _resolve_display_option(rc, "ffmpeg")
    if selected:
        video_size = f"{selected['width']}x{selected['height']}"
        input_display = f"{display}+{selected['x']},{selected['y']}"
        return (
            ["-f", "x11grab", "-framerate", str(rc.fps), "-video_size", video_size, "-i", input_display],
            _resolution_scale_filter(rc.resolution),
        )

    res = rc.resolution or _detect_x11_resolution()
    args = ["-f", "x11grab", "-framerate", str(rc.fps)]
    if res:
        args += ["-s", res]
    args += ["-i", display]
    return args, None

def _pactl_audio_source(kind: str, preferred: str = "default") -> str:
    """Resolve a Pulse/PipeWire source name via pactl.

    kind="desktop": the default sink's monitor source, so clips contain
    system/game audio. Leaving this to "default" can make ffmpeg/wf-recorder
    record the default *input* (microphone) on some setups.
    kind="microphone": the default input source.

    Falls back to `preferred` (logged at debug) when pactl is missing or
    fails — gpu-screen-recorder resolves "default" itself, so this only
    degrades the ffmpeg/wf-recorder paths.
    """
    if preferred and preferred != "default":
        return preferred
    if not _has("pactl"):
        log.debug("pactl not found; using %r for %s audio", preferred, kind)
        return preferred

    get_cmd = "get-default-sink" if kind == "desktop" else "get-default-source"
    try:
        name = subprocess.check_output(
            ["pactl", get_cmd], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if name:
            return f"{name}.monitor" if kind == "desktop" else name
    except Exception as exc:
        log.debug("pactl %s failed: %s", get_cmd, exc)

    # Fallback: walk the source list for a (non-)monitor entry.
    try:
        out = subprocess.check_output(
            ["pactl", "list", "short", "sources"], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            cols = re.split(r"\s+", line.strip())
            if len(cols) > 1 and (cols[1].endswith(".monitor") == (kind == "desktop")):
                return cols[1]
    except Exception as exc:
        log.debug("pactl list sources failed: %s", exc)

    return preferred


def _desktop_audio_source(preferred: str) -> str:
    return _pactl_audio_source("desktop", preferred)


def _microphone_audio_source(preferred: str = "default") -> str:
    return _pactl_audio_source("microphone", preferred)


def _captures_desktop_audio(rc) -> bool:
    return bool(rc.capture_audio)


def _captures_microphone(rc) -> bool:
    return bool(rc.capture_microphone)


def _gsr_mic_source(rc) -> str:
    """Configured microphone as a GSR -a value ("default_input" or "device:…")."""
    return (getattr(rc, "microphone_source", "") or "default_input").strip() or "default_input"


def _pactl_mic_preferred(rc) -> str:
    """Configured microphone as a pactl source name for wf-recorder/ffmpeg."""
    source = _gsr_mic_source(rc)
    if source == "default_input":
        return "default"
    if source.startswith("device:"):
        return source.split(":", 1)[1]
    return source


def _desktop_volume(rc) -> float:
    try:
        return max(0.0, min(float(getattr(rc, "desktop_volume", 1.0)), 2.0))
    except (TypeError, ValueError):
        return 1.0


def _mic_volume(rc) -> float:
    try:
        return max(0.0, min(float(getattr(rc, "microphone_volume", 1.0)), 2.0))
    except (TypeError, ValueError):
        return 1.0


def _volume_mix_wanted(rc) -> bool:
    """Whether clips need a save-time volume pass. Off at the defaults so the
    recording pipeline stays byte-identical for everyone who never touches
    the sliders. Separate audio_tracks keep full control instead."""
    if getattr(rc, "audio_tracks", None):
        return False
    return abs(_desktop_volume(rc) - 1.0) > 0.01 or abs(_mic_volume(rc) - 1.0) > 0.01


_warned_desktop_sources: set[str] = set()


def _warn_if_desktop_source_is_mic(desktop_source: str) -> None:
    parts = [p for p in desktop_source.split("|") if p]
    if not parts or any(_classify_gsr_source(p) != "input" for p in parts):
        return
    if desktop_source in _warned_desktop_sources:
        return
    _warned_desktop_sources.add(desktop_source)
    log.warning(
        "Desktop audio source %r is a microphone input — clips will have no "
        "desktop audio. Pick a monitor source under Settings → Audio.",
        desktop_source,
    )


def _gsr_audio_input(rc) -> Optional[str]:
    desktop = _captures_desktop_audio(rc)
    mic = _captures_microphone(rc)
    desktop_source = (getattr(rc, "gsr_audio_source", "") or "default_output").strip() or "default_output"
    if desktop:
        _warn_if_desktop_source_is_mic(desktop_source)
    if desktop and mic:
        mic_source = _gsr_mic_source(rc)
        parts = [p for p in desktop_source.split("|") if p]
        if mic_source not in parts:
            parts.append(mic_source)
        return "|".join(parts)
    if desktop:
        return desktop_source
    if mic:
        return _gsr_mic_source(rc)
    return None


def _wf_audio_device(rc) -> Optional[str]:
    desktop = _captures_desktop_audio(rc)
    mic = _captures_microphone(rc)
    if desktop and mic:
        if rc.wf_microphone_strategy == "mic_only":
            return _microphone_audio_source(_pactl_mic_preferred(rc))
        return _desktop_audio_source(rc.audio_sink)
    if desktop:
        return _desktop_audio_source(rc.audio_sink)
    if mic:
        return _microphone_audio_source(_pactl_mic_preferred(rc))
    return None


def _ffmpeg_audio_input_args(rc) -> list[str]:
    args: list[str] = []
    if _captures_desktop_audio(rc):
        args += ["-f", "pulse", "-i", _desktop_audio_source(rc.audio_sink)]
    if _captures_microphone(rc):
        args += ["-f", "pulse", "-i", _microphone_audio_source(_pactl_mic_preferred(rc))]
    return args


def _ffmpeg_audio_output_args(rc) -> list[str]:
    desktop = _captures_desktop_audio(rc)
    mic = _captures_microphone(rc)
    if not desktop and not mic:
        return []
    if desktop and mic:
        return [
            "-filter_complex", "[1:a][2:a]amix=inputs=2:normalize=0[aout]",
            "-map", "0:v",
            "-map", "[aout]",
            "-c:a", "aac",
            "-b:a", "128k",
        ]
    return ["-c:a", "aac", "-b:a", "128k"]


def _gsr_monitor_listing_line(line: str) -> bool:
    value = line.strip()
    if not value:
        return False
    if re.match(r'^"[^"]+"\s+\(\d+x\d+[+-]\d+[+-]\d+\)$', value):
        return True
    if re.match(r"^\S+\|\d+x\d+", value):
        return True
    return False


def _gsr_progress_line(line: str) -> bool:
    """GSR prints a throughput line every second. Keeping those would push the
    one line that explains a death straight out of the tail."""
    return bool(re.match(r"^\s*(update|damage)\s+fps:", line, re.IGNORECASE))


def _gsr_runtime_error(raw: str) -> Optional[str]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for line in reversed(lines):
        lowered = line.lower()
        if lowered.startswith(("gsr error:", "error:", "gpu-screen-recorder:")):
            return line
        if "failed to" in lowered:
            return line
        if "invalid" in lowered and (
            "capture" in lowered or "target" in lowered or "-w" in lowered
        ):
            return line

    for line in reversed(lines):
        lowered = line.lower()
        if lowered.startswith(("available", "monitors")):
            continue
        if _gsr_monitor_listing_line(line):
            continue
        return line
    return None


def _summarize_process_error(program: str, returncode: Optional[int], stderr_text: str) -> str:
    if program == "wf-recorder":
        runtime_error = _wf_runtime_error(stderr_text)
        if runtime_error:
            return runtime_error
    if program == "gpu-screen-recorder":
        runtime_error = _gsr_runtime_error(stderr_text)
        if runtime_error:
            return runtime_error

    lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
    if lines:
        return lines[-1]
    if returncode is not None:
        return f"exit code {returncode}"
    return "unknown error"


async def _read_stream_text(stream) -> str:
    if stream is None:
        return ""
    try:
        data = await asyncio.wait_for(stream.read(), timeout=1.0)
    except Exception:
        return ""
    return _combine_process_output(data)


async def _spawn_capture(cmd: list[str], stderr) -> asyncio.subprocess.Process:
    """Start a capture process as its own process-group leader.

    gpu-screen-recorder forks a privileged gsr-kms-server helper. Signalling
    GSR alone leaves that helper running, and because it inherited GSR's
    stderr it holds the write end of our pipe open forever, so asyncio never
    tears the transport down. That leaked one process and one fd per restart
    until the daemon hit EMFILE and stopped clipping (#129). Own group means
    the helper can be reaped with the recorder.
    """
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=stderr,
        start_new_session=True,
    )


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Signal a capture process's whole group, helpers included.

    An empty group is the ordinary outcome once everything has exited, so
    ProcessLookupError is not an error here.
    """
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def _terminate_group(proc: asyncio.subprocess.Process, timeout: float = 5.0) -> None:
    """Ask a capture process group to stop, then make sure it did.

    The SIGKILL still reaches surviving members after the leader has been
    reaped, which is exactly the case that used to strand gsr-kms-server.
    """
    _signal_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    except ProcessLookupError:
        pass
    except Exception as exc:
        log.warning("Error while stopping capture process: %s", exc)
    _signal_group(proc, signal.SIGKILL)
    try:
        await proc.wait()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Encoder selection
# ──────────────────────────────────────────────────────────────────────────────

def _available_encoders() -> set[str]:
    """Return the set of ffmpeg video encoders available on this system."""
    try:
        out = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-encoders"], stderr=subprocess.DEVNULL, text=True
        )
        return {line.split()[1] for line in out.splitlines() if line.startswith(" V")}
    except Exception:
        return set()


def choose_encoder(preferred: str) -> str:
    """
    Resolve 'auto' or validate a user-specified encoder.
    Returns the ffmpeg encoder name to use.
    """
    if preferred != "auto":
        return preferred

    enc = _available_encoders()
    if _is_nvidia():
        if "h264_nvenc" in enc:
            log.info("NVIDIA GPU detected → using h264_nvenc")
            return "h264_nvenc"
    # AMD/Intel VAAPI (Wayland/Mesa)
    if "h264_vaapi" in enc and _is_wayland():
        log.info("VAAPI available → using h264_vaapi")
        return "h264_vaapi"
    log.info("Falling back to software encoder libx264")
    return "libx264"


def _encoder_flags(encoder: str, crf: int) -> list[str]:
    """Return ffmpeg flags for a given encoder."""
    if encoder in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
        # NVENC: use CQ mode (similar to CRF) and tuning for low-latency
        return ["-c:v", encoder, "-rc", "vbr", "-cq", str(crf), "-preset", "p4", "-tune", "hq"]
    if encoder in ("h264_vaapi", "hevc_vaapi", "av1_vaapi"):
        return ["-vf", "format=nv12,hwupload", "-c:v", encoder, "-qp", str(crf)]
    # libx264 / libx265 software
    return ["-c:v", encoder, "-crf", str(crf), "-preset", "fast"]


# ──────────────────────────────────────────────────────────────────────────────
# Abstract base
# ──────────────────────────────────────────────────────────────────────────────

class Recorder(ABC):
    """Base class for recording backends."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._running = False
        self._clip_callbacks: list[Callable[[Path], None]] = []
        # Optional sync callback returning the focused game's name (or None);
        # used to tag clip filenames. Runs in a thread (it shells out).
        self.clip_tag_cb: Optional[Callable[[], Optional[str]]] = None
        # Session recording state (shared across all backends)
        self._session_active = False
        self._session_proc: Optional[asyncio.subprocess.Process] = None
        self._session_path: Optional[Path] = None
        self._session_start: float = 0.0
        self._session_program = ""

    def on_clip_saved(self, cb: Callable[[Path], None]) -> None:
        """Register a callback invoked with the clip Path once it's ready."""
        self._clip_callbacks.append(cb)

    def last_output(self) -> str:
        """Recent output from the capture process, for diagnosing a death.
        Backends that do not capture stderr return nothing."""
        return ""

    async def _clip_tag(self) -> Optional[str]:
        """Sanitized filename tag for the clip being saved, or None."""
        if not self.clip_tag_cb:
            return None
        try:
            tag = await asyncio.to_thread(self.clip_tag_cb)
        except Exception:
            log.exception("Clip tag callback raised")
            return None
        if not tag:
            return None
        tag = re.sub(r"[^A-Za-z0-9]+", "-", tag).strip("-")
        return tag[:48] or None

    def _clip_name_template(self) -> str:
        return (getattr(self.cfg.output, "clip_name_template", "") or "").strip()

    def _emit(self, path: Path) -> None:
        for cb in self._clip_callbacks:
            try:
                cb(path)
            except Exception:
                log.exception("Clip callback raised")

    def is_healthy(self) -> bool:
        """Whether capture is believed to be live right now."""
        return self._running

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def save_clip(self, duration: Optional[int] = None) -> Optional[Path]:
        """
        Trigger saving the requested number of seconds, or the configured
        default clip duration when duration is omitted.
        Returns the saved path, or None on failure.
        """
        ...

    @property
    def name(self) -> str:
        return type(self).__name__

    # ── Session recording (shared implementation) ──────────────────────────

    def session_elapsed(self) -> float:
        """Return seconds elapsed since the session started (0 if not active)."""
        if not self._session_active:
            return 0.0
        return time.time() - self._session_start

    async def start_session(self) -> Optional[Path]:
        """
        Begin a continuous session recording directly to a file.
        Returns the output path, or None on failure.
        Session recording uses ffmpeg regardless of the replay-buffer backend
        so that we get a single contiguous output file to stamp highlights into.
        """
        if self._session_active:
            log.warning("Session already active")
            return None

        out_dir = resolve_path(self.cfg.output.directory)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = _next_session_path(out_dir)

        cmd = self._build_session_cmd(out_path)
        if cmd is None:
            log.error("Cannot build session recording command for this environment")
            return None

        log.info("Starting session recording: %s", " ".join(cmd))
        stderr_target = (
            asyncio.subprocess.PIPE
            if cmd and cmd[0] in {"wf-recorder", "ffmpeg"}
            else asyncio.subprocess.DEVNULL
        )
        try:
            self._session_proc = await _spawn_capture(cmd, stderr_target)
        except Exception as exc:
            log.error("Failed to start session recording: %s", exc)
            return None

        self._session_program = cmd[0]
        try:
            await asyncio.wait_for(self._session_proc.wait(), timeout=1.0)
            stderr_text = await _read_stream_text(self._session_proc.stderr)
            detail = _summarize_process_error(
                self._session_program,
                self._session_proc.returncode,
                stderr_text,
            )
            log.error("Session recorder failed to start: %s", detail)
            # Session recording uses GSR on Wayland, so the same helper can
            # be left behind here (#129).
            _signal_group(self._session_proc, signal.SIGKILL)
            self._session_proc = None
            self._session_program = ""
            return None
        except asyncio.TimeoutError:
            pass

        self._session_active = True
        self._session_path = out_path
        self._session_start = time.time()
        return out_path

    async def stop_session(self) -> Optional[Path]:
        """
        Stop the active session recording, apply the watermark, and emit the
        clip via the normal on_clip_saved callbacks.
        Returns the saved path, or None on failure.
        """
        if not self._session_active or not self._session_proc:
            log.warning("No active session to stop")
            return None

        path = self._session_path
        proc = self._session_proc
        program = self._session_program
        self._session_active = False
        self._session_proc = None
        self._session_path = None
        self._session_program = ""

        # Ask the recorder to stop gracefully, then reap anything it forked.
        # The SIGKILL only lands after the leader has exited, so it can never
        # cut a container short.
        await _terminate_group(proc, timeout=8)

        stderr_text = await _read_stream_text(proc.stderr)
        if not path or not path.exists():
            detail = _summarize_process_error(program, proc.returncode, stderr_text)
            log.error("Session file not found after stop: %s (%s)", path, detail)
            return None

        if self.cfg.recording.apply_watermark:
            await _apply_watermark(path)
        log.info("Session clip saved: %s", path)
        self._emit(path)
        return path

    def _build_session_cmd(self, out_path: Path) -> Optional[list[str]]:
        """Build a direct-to-file ffmpeg command for session recording."""
        rc = self.cfg.recording
        encoder = choose_encoder(rc.encoder)

        if _is_wayland():
            # Prefer gpu-screen-recorder on Wayland (especially smoother on NVIDIA).
            if _has("gpu-screen-recorder"):
                return self._gsr_session_cmd(out_path, rc)

            # Fallback: wf-recorder direct-to-file on Wayland.
            if _has("wf-recorder"):
                cmd = ["wf-recorder"]
                if _wf_supports_flag("--force-yuv"):
                    cmd.append("--force-yuv")
                cmd += ["-f", str(out_path)]
                if rc.capture_audio:
                    cmd += [f"--audio={_desktop_audio_source(rc.audio_sink)}"]
                if encoder in ("h264_nvenc", "hevc_nvenc"):
                    cmd += ["-c", encoder]
                elif encoder == "h264_vaapi":
                    cmd += ["-c", "h264_vaapi", "-d", "/dev/dri/renderD128"]
                else:
                    cmd += ["-c", "libx264"]
                return cmd

            # Last resort on XWayland sessions.
            if os.environ.get("DISPLAY") and _has("ffmpeg"):
                return self._ffmpeg_session_cmd(out_path, encoder, rc)
            return None

        if _is_x11() and _has("ffmpeg"):
            return self._ffmpeg_session_cmd(out_path, encoder, rc)

        return None

    @staticmethod
    def _gsr_session_cmd(out_path: Path, rc) -> list[str]:
        extra = _gsr_sanitize_args(_extra_gsr_args(rc.gsr_args), {"-o", "-r"})
        cmd = ["gpu-screen-recorder"]

        if not _gsr_has_any_flag(extra, "-w"):
            cmd += ["-w", _gsr_capture_target(rc)]
        if not _gsr_has_any_flag(extra, "-f"):
            cmd += ["-f", str(rc.fps)]
        cmd += _gsr_resolution_args(rc, extra)
        if not _gsr_has_any_flag(extra, "-c"):
            cmd += ["-c", "mp4"]
        codec = _gsr_codec_for_encoder(rc.encoder)
        if codec and not _gsr_has_any_flag(extra, "-k"):
            cmd += ["-k", codec]
        if not _gsr_has_any_flag(extra, "-a"):
            cmd += _gsr_audio_args(rc, split_for_volume=False)

        cmd += extra
        cmd += ["-o", str(out_path)]
        return cmd

    @staticmethod
    def _ffmpeg_session_cmd(out_path: Path, encoder: str, rc) -> list[str]:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
        input_args, extra_filter = _ffmpeg_x11_input_args(rc)
        cmd += input_args
        cmd += _ffmpeg_audio_input_args(rc)
        cmd += _merge_ffmpeg_filters(_encoder_flags(encoder, rc.crf), extra_filter)
        cmd += _ffmpeg_audio_output_args(rc)
        cmd += ["-y", str(out_path)]
        return cmd


# ──────────────────────────────────────────────────────────────────────────────
# Clip trimming helper (used by GSR backend)
# ──────────────────────────────────────────────────────────────────────────────

def _media_file_names(out_dir: Path) -> set[str]:
    """Names of clip media files (mp4 + mkv) currently in out_dir."""
    return {f.name for f in out_dir.glob("*.mp4")} | {f.name for f in out_dir.glob("*.mkv")}


def _gsr_replay_candidates(current: set[str], baseline: set[str]) -> set[str]:
    """New media files that could be a replay GSR just flushed.

    GSR names its replay files itself (date-based); anything Vice creates —
    sequential clips, session recordings, in-place-edit temp files — can
    never be the flushed replay, so those names are never claimed even if
    they appear in the output directory mid-save.
    """
    return {
        name
        for name in current - baseline
        if not name.startswith(("Vice_Clip_", "Vice_Session_"))
        and not any(t in name for t in (".trim.", ".trimming.", ".wm.", ".fix."))
    }


def _next_numbered_path(out_dir: Path, stem: str, ext: str, tag: Optional[str] = None) -> Path:
    """Next available <stem>_N[_Tag].<ext> path. Numbering counts every
    container and tag variant so tagged clips never collide."""
    max_n = 0
    pattern = re.compile(rf"^{stem}_(\d+)(?:_.+)?\.(?:mp4|mkv)$")
    for f in out_dir.glob(f"{stem}_*"):
        m = pattern.match(f.name)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    suffix = f"_{tag}" if tag else ""
    return out_dir / f"{stem}_{max_n + 1}{suffix}.{ext}"


def _sanitize_clip_name(name: str) -> str:
    """Strip anything that cannot live in a filename, then tidy the separators
    an unresolved token leaves behind."""
    name = re.sub(r"[/\\\x00-\x1f]", "", name)
    name = re.sub(r"[_-]{2,}", lambda m: m.group(0)[0], name)
    return name.strip("_- .")


def _render_clip_name(template: str, n: int, game: Optional[str], now: datetime) -> str:
    values = {
        "$n":    str(n),
        "$date": now.strftime("%Y-%m-%d"),
        "$time": now.strftime("%H%M"),
        "$game": game or "",
    }
    rendered = template
    for token, value in values.items():
        rendered = rendered.replace(token, value)
    return _sanitize_clip_name(rendered)


def _next_templated_path(
    out_dir: Path, template: str, ext: str, tag: Optional[str], now: datetime
) -> Optional[Path]:
    """Path for a user-defined clip name, or None if the template is unusable.

    $n continues the highest number already on disk for this template. Without
    $n a template can repeat, so a numeric suffix is added to avoid clobbering
    an existing clip."""
    template = (template or "").strip()
    if not template:
        return None

    n = 1
    if "$n" in template:
        # Build a matcher from the template so numbering survives a change to
        # anything around $n. The marker is a private-use codepoint: it lives
        # through sanitizing and cannot occur in a real filename.
        placeholder = ""
        literal = _render_clip_name(template.replace("$n", placeholder), 0, tag, now)
        if placeholder not in literal:
            return None
        pattern = re.compile(
            "^" + r"(\d+)".join(re.escape(p) for p in literal.split(placeholder))
            + r"\.(?:mp4|mkv)$"
        )
        max_n = 0
        for f in out_dir.glob("*"):
            m = pattern.match(f.name)
            if m:
                max_n = max(max_n, int(m.group(1)))
        n = max_n + 1

    name = _render_clip_name(template, n, tag, now)
    if not name:
        return None

    candidate = out_dir / f"{name}.{ext}"
    if not candidate.exists():
        return candidate
    for extra in range(2, 1000):
        candidate = out_dir / f"{name}_{extra}.{ext}"
        if not candidate.exists():
            return candidate
    return None


def _next_clip_path(
    out_dir: Path,
    ext: str = "mp4",
    tag: Optional[str] = None,
    template: str = "",
) -> Path:
    """Return the next available clip path in out_dir. Uses the user's filename
    template when set, otherwise Vice_Clip_N[_Game].<ext>."""
    if template:
        path = _next_templated_path(out_dir, template, ext, tag, datetime.now())
        if path is not None:
            return path
        log.warning(
            "Clip name template %r produced no usable filename — using default naming",
            template,
        )
    return _next_numbered_path(out_dir, "Vice_Clip", ext, tag)


def _next_session_path(out_dir: Path) -> Path:
    """Return the next available Vice_Session_N.mp4 path in out_dir."""
    return _next_numbered_path(out_dir, "Vice_Session", "mp4")


# Without an explicit map, ffmpeg keeps only one audio stream per output, so
# any pass over a clip recorded with separate desktop and mic tracks silently
# threw the mic away (#119). The "?" makes each map optional for clips that
# have no audio at all. The watermark pass filters video, which needs exactly
# one video input, so it pins the first video stream instead of all of them.
KEEP_ALL_STREAMS = ["-map", "0:v?", "-map", "0:a?"]
KEEP_ALL_AUDIO = ["-map", "0:v:0?", "-map", "0:a?"]


async def _trim_to_last_n_seconds(path: Path, seconds: int) -> Path:
    """Trim a clip to its last `seconds` seconds in-place. Returns the path."""
    total = await _get_duration(path)
    if total <= 0 or total <= seconds:
        return path  # already short enough

    start = total - seconds
    ext = path.suffix.lstrip(".") or "mp4"
    tmp = path.with_suffix(f".trim.{ext}")
    faststart = ["-movflags", "+faststart"] if ext == "mp4" else []

    def _copy_trim_cmd() -> list[str]:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", str(start), "-i", str(path),
            "-t", str(seconds),
            *KEEP_ALL_STREAMS,
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            *faststart,
            "-y", str(tmp),
        ]

    def _reencode_trim_cmd() -> list[str]:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", str(start), "-i", str(path),
            "-t", str(seconds),
            *KEEP_ALL_STREAMS,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            *faststart,
            "-y", str(tmp),
        ]

    async def _run_trim(cmd: list[str], timeout: int) -> tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            err = stderr.decode() if stderr else ""
            return proc.returncode == 0, err
        except asyncio.TimeoutError:
            return False, "trim command timed out"

    ok, err = await _run_trim(_copy_trim_cmd(), 60)
    if not ok:
        log.warning("ffmpeg copy trim failed, retrying with re-encode: %s", err)
        ok, err = await _run_trim(_reencode_trim_cmd(), 120)
        if not ok:
            log.error("ffmpeg trim failed: %s", err)
            return path

    if not tmp.exists():
        log.error("ffmpeg trim did not produce output file")
        return path

    # Replace original with trimmed version
    tmp.replace(path)
    return path


async def _wait_for_finalized_clip(
    path: Path,
    *,
    stable_polls: int = 3,
    poll_interval: float = 0.25,
    inactivity_timeout: float = 15.0,
    max_wait: float = 600.0,
) -> bool:
    """Wait until a replay clip stops changing and ffprobe can read it.

    The timeout is based on write inactivity, not total elapsed time:
    flushing a long replay buffer can legitimately take minutes on slow
    storage, and a fixed deadline used to abandon clips that were still
    being written (and would have finished fine). We only give up when
    the file has stopped growing for `inactivity_timeout` seconds and
    still cannot be probed, or after `max_wait` as a hard safety cap.
    """
    deadline = time.monotonic() + max_wait
    last_sig: tuple[int, int] | None = None
    last_change = time.monotonic()
    stable = 0

    while time.monotonic() < deadline:
        try:
            st = path.stat()
            sig = (st.st_size, st.st_mtime_ns)
        except OSError:
            sig = None

        if sig != last_sig:
            stable = 0
            last_sig = sig
            last_change = time.monotonic()
        elif sig is not None and sig[0] > 0:
            stable += 1
        else:
            stable = 0

        if stable >= stable_polls and await _get_duration(path) > 0:
            return True

        if time.monotonic() - last_change > inactivity_timeout:
            return False

        await asyncio.sleep(poll_interval)

    return False


_WATERMARK = (
    "drawtext=text='Clipped with Vice'"
    ":x=w-tw-12:y=h-th-12"
    ":fontsize=17"
    ":fontcolor=white@0.55"
    ":shadowcolor=black@0.7:shadowx=1:shadowy=1"
    ":box=1:boxcolor=black@0.25:boxborderw=7"
)


async def _apply_watermark(path: Path) -> None:
    """Burn the Vice watermark into *path* in-place (re-encodes with libx264)."""
    ext = path.suffix.lstrip(".") or "mp4"
    tmp = path.with_suffix(f".wm.{ext}")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-vf", _WATERMARK,
        *KEEP_ALL_AUDIO,
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "copy",
        *(["-movflags", "+faststart"] if ext == "mp4" else []),
        "-y", str(tmp),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            log.warning("watermark encode failed: %s", stderr.decode())
            tmp.unlink(missing_ok=True)
            return
    except asyncio.TimeoutError:
        log.warning("watermark encode timed out")
        tmp.unlink(missing_ok=True)
        return
    tmp.replace(path)


async def _count_audio_streams(path: Path) -> int:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return 0
        return len([line for line in out.decode().splitlines() if line.strip()])
    except (asyncio.TimeoutError, OSError):
        return 0


def _volume_mix_cmd(path: Path, tmp: Path, streams: int, dv: float, mv: float) -> list[str]:
    ext = path.suffix.lstrip(".") or "mp4"
    audio_codec = ["-c:a", "libopus", "-b:a", "128k"] if ext == "mkv" else ["-c:a", "aac", "-b:a", "160k"]
    faststart = ["-movflags", "+faststart"] if ext == "mp4" else []
    if streams >= 2:
        filter_graph = (
            f"[0:a:0]volume={dv}[a0];[0:a:1]volume={mv}[a1];"
            f"[a0][a1]amix=inputs=2:normalize=0[aout]"
        )
        mapping = ["-filter_complex", filter_graph, "-map", "0:v", "-map", "[aout]"]
    else:
        mapping = ["-af", f"volume={dv}"]
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        *mapping,
        "-c:v", "copy",
        *audio_codec,
        *faststart,
        "-y", str(tmp),
    ]


async def _apply_volume_mix(path: Path, rc) -> None:
    """Balance desktop vs mic loudness in-place. Video is stream-copied, only
    audio re-encodes. Any failure leaves the clip untouched."""
    if not _volume_mix_wanted(rc):
        return
    if [t for t in (getattr(rc, "audio_tracks", None) or []) if str(t).strip()]:
        # User-defined tracks are never split for volume, and the mix graph
        # below only handles the first two, so leave them alone.
        return
    dv, mv = _desktop_volume(rc), _mic_volume(rc)
    streams = await _count_audio_streams(path)
    if streams == 0:
        return
    if streams == 1:
        if _captures_desktop_audio(rc) and _captures_microphone(rc):
            # Both sources share one track (clip recorded before the volume
            # change took effect); can't balance them separately.
            log.debug("Skipping volume mix: single mixed audio track in %s", path.name)
            return
        dv = dv if _captures_desktop_audio(rc) else mv

    ext = path.suffix.lstrip(".") or "mp4"
    tmp = path.with_suffix(f".mix.{ext}")
    cmd = _volume_mix_cmd(path, tmp, streams, dv, mv)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            log.warning("volume mix failed, keeping clip as recorded: %s", stderr.decode())
            tmp.unlink(missing_ok=True)
            return
    except asyncio.TimeoutError:
        log.warning("volume mix timed out, keeping clip as recorded")
        tmp.unlink(missing_ok=True)
        return
    tmp.replace(path)


# ──────────────────────────────────────────────────────────────────────────────
# gpu-screen-recorder backend
# ──────────────────────────────────────────────────────────────────────────────

class GSRRecorder(Recorder):
    """
    Uses gpu-screen-recorder (https://git.dec05eba.com/gpu-screen-recorder).
    Supports: NVIDIA (NVENC), AMD (VAAPI), Intel (VAAPI), Wayland KMS, X11.
    Replay-buffer mode: sends SIGUSR1 to flush the buffer to a file.
    """

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._out_dir = resolve_path(cfg.output.directory)
        self._watch_task: Optional[asyncio.Task] = None
        # Kept so an unexpected death can say why instead of just "died".
        self._stderr_tail: deque[str] = deque(maxlen=12)

    @property
    def name(self) -> str:
        return "gpu-screen-recorder"

    def is_healthy(self) -> bool:
        return self._running and self._proc is not None and self._proc.returncode is None

    def _build_cmd(self) -> list[str]:
        rc = self.cfg.recording
        extra = _gsr_sanitize_args(_extra_gsr_args(rc.gsr_args), {"-o"})
        cmd = ["gpu-screen-recorder"]

        # Allow manual overrides through recording.gsr_args.
        if not _gsr_has_any_flag(extra, "-w"):
            cmd += ["-w", _gsr_capture_target(rc)]

        if not _gsr_has_any_flag(extra, "-f"):
            cmd += ["-f", str(rc.fps)]

        cmd += _gsr_resolution_args(rc, extra)

        if not _gsr_has_any_flag(extra, "-r"):
            cmd += ["-r", str(rc.buffer_duration)]

        if not _gsr_has_any_flag(extra, "-replay-storage") and _gsr_wants_disk_replay(rc):
            if _gsr_supports_flag("-replay-storage"):
                cmd += ["-replay-storage", "disk"]
            else:
                log.info(
                    "This gpu-screen-recorder build has no -replay-storage flag; "
                    "keeping the replay buffer in RAM"
                )

        if not _gsr_has_any_flag(extra, "-c"):
            cmd += ["-c", _container(rc)]
        codec = _gsr_codec_for_encoder(rc.encoder)
        if codec and not _gsr_has_any_flag(extra, "-k"):
            cmd += ["-k", codec]

        if not _gsr_has_any_flag(extra, "-a"):
            cmd += _gsr_audio_args(rc)

        cmd += extra

        # Output directory (gsr writes files here on SIGUSR1)
        self._out_dir.mkdir(parents=True, exist_ok=True)
        cmd += ["-o", str(self._out_dir)]
        return cmd

    async def start(self) -> None:
        cmd = self._build_cmd()
        log.info("Starting GSR: %s", " ".join(cmd))
        self._running = True

        self._stderr_tail.clear()
        self._proc = await _spawn_capture(cmd, asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=1.0)
            stderr_text = await _read_stream_text(self._proc.stderr)
            detail = _summarize_process_error(
                "gpu-screen-recorder",
                self._proc.returncode,
                stderr_text,
            )
            # GSR may have forked its helper before giving up.
            _signal_group(self._proc, signal.SIGKILL)
            self._proc = None
            self._running = False
            raise RuntimeError(f"gpu-screen-recorder failed to start: {detail}")
        except asyncio.TimeoutError:
            pass
        self._watch_task = asyncio.create_task(self._stderr_reader())

    async def _stderr_reader(self) -> None:
        assert self._proc and self._proc.stderr
        async for line in self._proc.stderr:
            text = line.decode(errors="replace").rstrip()
            if text and not _gsr_progress_line(text):
                self._stderr_tail.append(text)
            log.debug("gsr: %s", text)

    def last_output(self) -> str:
        """GSR's last non-routine stderr, for explaining an unexpected death.
        Empty when it said nothing, which is the honest answer for a process
        that was simply killed."""
        return "\n".join(self._stderr_tail)

    async def stop(self) -> None:
        self._running = False
        if self._proc:
            proc = self._proc
            self._proc = None
            await _terminate_group(proc)
        if self._watch_task:
            self._watch_task.cancel()
            self._watch_task = None

    async def save_clip(self, duration: Optional[int] = None) -> Optional[Path]:
        if not self._proc or self._proc.returncode is not None:
            log.error("GSR process is not running")
            return None
        clip_duration = int(duration or self.cfg.recording.clip_duration)

        # Snapshot the directory immediately before triggering the flush.
        # Diffing against a baseline captured at recorder start misattributed
        # files that appeared in between (a finished session recording, a clip
        # renamed in the UI, a late flush from a timed-out save) as the new
        # replay — the wrong clip then got renamed, trimmed, and shown.
        baseline = _media_file_names(self._out_dir)

        log.info("Sending SIGUSR1 to GSR (pid=%d) to save replay", self._proc.pid)
        try:
            os.kill(self._proc.pid, signal.SIGUSR1)
        except ProcessLookupError:
            log.error("GSR process not found")
            return None

        def _mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0

        # Wait for the new file to appear (GSR creates it almost immediately
        # after SIGUSR1), then wait for GSR to finish writing it.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            current = _media_file_names(self._out_dir)
            new = _gsr_replay_candidates(current, baseline)
            if new:
                newest = max((self._out_dir / n for n in new), key=_mtime)
                if not await _wait_for_finalized_clip(newest):
                    log.error(
                        "GSR clip %s stopped being written but is unreadable — "
                        "leaving the file in place for inspection",
                        newest,
                    )
                    return None
                # Rename GSR's auto-generated filename to a sequential
                # Vice_Clip_N name, tagged with the focused game if known.
                seq_path = _next_clip_path(
                    self._out_dir,
                    ext=newest.suffix.lstrip(".") or "mp4",
                    tag=await self._clip_tag(),
                    template=self._clip_name_template(),
                )
                newest.rename(seq_path)
                newest = seq_path
                # GSR saves the entire buffer; trim to the requested clip duration.
                trimmed = await _trim_to_last_n_seconds(newest, clip_duration)
                await _apply_volume_mix(trimmed, self.cfg.recording)
                if self.cfg.recording.apply_watermark:
                    await _apply_watermark(trimmed)
                log.info("Clip saved: %s", trimmed)
                self._emit(trimmed)
                return trimmed

        log.error("Timed out waiting for GSR to write clip")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Segment-based backend (wf-recorder or ffmpeg x11grab)
# ──────────────────────────────────────────────────────────────────────────────

SEGMENT_DURATION = 30  # seconds per segment
MAX_SEGMENTS = 20      # 20 × 30 s = 10 min max buffer


class SegmentRecorder(Recorder):
    """
    Rolling-segment recording: records 30-second chunks in a temp directory,
    keeping the most recent MAX_SEGMENTS. On clip request, concatenates the
    segments covering the last `clip_duration` seconds using ffmpeg.

    This backend works with any capture tool that writes to a file:
    wf-recorder (Wayland) or ffmpeg -f x11grab (X11).
    """

    def __init__(self, cfg: Config, use_wf_recorder: bool) -> None:
        super().__init__(cfg)
        self._use_wf = use_wf_recorder
        self._seg_dir = Path("/tmp/vice/segs")
        self._seg_dir.mkdir(parents=True, exist_ok=True)
        self._seg_index = 0
        self._segments: list[tuple[float, Path]] = []  # (start_time, path)
        self._loop_task: Optional[asyncio.Task] = None
        self._current_proc: Optional[asyncio.subprocess.Process] = None
        self._encoder = choose_encoder(cfg.recording.encoder)
        self._out_dir = resolve_path(cfg.output.directory)
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self._last_segment_error: Optional[str] = None

    @property
    def name(self) -> str:
        return "wf-recorder" if self._use_wf else "ffmpeg"

    def is_healthy(self) -> bool:
        return self._running and self._loop_task is not None and not self._loop_task.done()

    # ── Capture commands ──────────────────────────────────────────────────────

    def _wf_recorder_cmd(self, out: Path) -> list[str]:
        rc = self.cfg.recording
        cmd = ["wf-recorder"]
        if _wf_supports_flag("--force-yuv"):
            cmd.append("--force-yuv")
        cmd += ["-f", str(out)]
        if rc.resolution:
            # wf-recorder geometry flag
            pass  # resolution is auto by default; geometry can be set with -g
        target = _wf_capture_target(rc)
        if target:
            cmd += ["-o", target]
        audio_device = _wf_audio_device(rc)
        if audio_device:
            cmd += [f"--audio={audio_device}"]
        # Use ffmpeg codec flags via wf-recorder's -c option
        codec = self._encoder
        if codec in ("h264_nvenc", "hevc_nvenc"):
            cmd += ["-c", codec]
        elif codec == "h264_vaapi":
            cmd += ["-c", "h264_vaapi", "-d", "/dev/dri/renderD128"]
        else:
            cmd += ["-c", "libx264"]
        return cmd

    def _ffmpeg_x11_cmd(self, out: Path) -> list[str]:
        rc = self.cfg.recording
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
        input_args, extra_filter = _ffmpeg_x11_input_args(rc)
        cmd += input_args
        cmd += _ffmpeg_audio_input_args(rc)

        enc_flags = _merge_ffmpeg_filters(_encoder_flags(self._encoder, rc.crf), extra_filter)
        cmd += enc_flags

        cmd += _ffmpeg_audio_output_args(rc)

        cmd += ["-y", str(out)]
        return cmd

    @staticmethod
    def _detect_x11_resolution() -> Optional[str]:
        return _detect_x11_resolution()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        # Warn if window capture is requested on a backend that doesn't support it
        source = getattr(self.cfg.recording, "capture_source", None)
        if source and source.startswith("window:") and self._use_wf:
            log.warning(
                "Window capture source %r is not supported by wf-recorder. "
                "Recording full display instead. Install gpu-screen-recorder "
                "for per-window capture on Wayland.",
                source,
            )
        log.info(
            "Starting segment recorder (backend=%s, encoder=%s)",
            "wf-recorder" if self._use_wf else "ffmpeg-x11grab",
            self._encoder,
        )
        self._running = True
        self._last_segment_error = None
        self._loop_task = asyncio.create_task(self._record_loop())
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if self._last_segment_error:
                await self.stop()
                raise RuntimeError(f"{self.name} failed to start: {self._last_segment_error}")
            if self._loop_task and self._loop_task.done():
                exc = self._loop_task.exception()
                await self.stop()
                detail = str(exc) if exc else "recorder loop exited during startup"
                raise RuntimeError(f"{self.name} failed to start: {detail}")

    async def stop(self) -> None:
        self._running = False
        if self._current_proc:
            proc = self._current_proc
            self._current_proc = None
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
            except Exception as exc:
                log.warning("Error while stopping segment recorder process: %s", exc)
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None

    async def _record_loop(self) -> None:
        while self._running:
            idx = self._seg_index % MAX_SEGMENTS
            seg_path = self._seg_dir / f"seg{idx:04d}.mp4"
            # Remove old file at this slot before overwriting
            if seg_path.exists():
                seg_path.unlink()

            start_ts = time.time()

            if self._use_wf:
                cmd = self._wf_recorder_cmd(seg_path)
            else:
                cmd = self._ffmpeg_x11_cmd(seg_path)

            log.debug("Segment %d: %s", self._seg_index, " ".join(cmd))

            timed_out = False
            stderr_text = ""
            try:
                self._current_proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE if self._use_wf else asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(
                    self._current_proc.wait(), timeout=SEGMENT_DURATION
                )
            except asyncio.TimeoutError:
                timed_out = True
                # Normal: segment duration elapsed, kill and move on
                if self._current_proc:
                    self._current_proc.terminate()
                    try:
                        await asyncio.wait_for(self._current_proc.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        try:
                            self._current_proc.kill()
                        except ProcessLookupError:
                            pass
            except asyncio.CancelledError:
                if self._current_proc:
                    try:
                        self._current_proc.terminate()
                    except ProcessLookupError:
                        pass
                return
            except Exception as exc:
                log.error("Recorder error: %s", exc)
                await asyncio.sleep(1)
                continue

            proc = self._current_proc
            self._current_proc = None
            if proc:
                stderr_text = await _read_stream_text(proc.stderr)

            if seg_path.exists():
                self._segments.append((start_ts, seg_path))
                # Prune old slots that have been overwritten
                self._segments = [
                    (t, p) for t, p in self._segments if p.exists()
                ]
                self._last_segment_error = None
            else:
                detail = _summarize_process_error(
                    "wf-recorder" if self._use_wf else "ffmpeg",
                    proc.returncode if proc else None,
                    stderr_text,
                )
                if detail or not timed_out:
                    self._last_segment_error = detail
                    log.error("Segment recorder did not produce output: %s", detail)
                    if self._seg_index == 0:
                        self._running = False
                        return

            self._seg_index += 1

    # ── Clip extraction ───────────────────────────────────────────────────────

    async def save_clip(self, duration: Optional[int] = None) -> Optional[Path]:
        rc = self.cfg.recording
        clip_duration = int(duration or rc.clip_duration)
        now = time.time()
        clip_start = now - clip_duration

        # Also stop the current segment so we capture up to "now"
        if self._current_proc and self._current_proc.returncode is None:
            self._current_proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(self._current_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._current_proc.kill()

        # Refresh segment list
        self._segments = [
            (t, p) for t, p in self._segments if p.exists()
        ]
        if not self._segments:
            if self._last_segment_error:
                log.error("No segments available to clip from (%s)", self._last_segment_error)
            else:
                log.error("No segments available to clip from")
            return None

        # Find segments that overlap the desired clip window
        relevant = [
            (t, p) for t, p in sorted(self._segments)
            if t + SEGMENT_DURATION >= clip_start
        ]
        if not relevant:
            relevant = [self._segments[-1]]

        log.info(
            "Clipping from %d segment(s), target window: last %d s",
            len(relevant),
            clip_duration,
        )

        # Write concat list for ffmpeg
        concat_list = Path("/tmp/vice/concat.txt")
        with concat_list.open("w") as fh:
            for _, seg in relevant:
                fh.write(f"file '{seg}'\n")

        # Calculate offset into the first segment
        first_ts = relevant[0][0]
        skip = max(0.0, clip_start - first_ts)

        out_path = _next_clip_path(
            self._out_dir,
            tag=await self._clip_tag(),
            template=self._clip_name_template(),
        )

        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-ss", str(skip),
            "-t", str(clip_duration),
            *KEEP_ALL_STREAMS,
            "-c:v", "copy",
            "-c:a", "copy",
            "-movflags", "+faststart",
            "-y", str(out_path),
        ]

        log.info("Extracting clip: %s", " ".join(ffmpeg_cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                log.error("ffmpeg clip error: %s", stderr.decode())
                return None
        except asyncio.TimeoutError:
            log.error("ffmpeg timed out during clip extraction")
            return None

        if self.cfg.recording.apply_watermark:
            await _apply_watermark(out_path)
        log.info("Clip saved: %s", out_path)
        self._emit(out_path)
        return out_path


# ──────────────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────────────

def _wf_requires_compat_backend(cfg: Config) -> bool:
    rc = cfg.recording
    return bool(rc.capture_audio and rc.capture_microphone and rc.wf_microphone_strategy == "backend_fallback")


def _wf_requires_user_choice(cfg: Config) -> bool:
    rc = cfg.recording
    return bool(rc.capture_audio and rc.capture_microphone and rc.wf_microphone_strategy == "prompt")


def _create_wf_compatible_recorder(cfg: Config) -> Recorder:
    if _has("gpu-screen-recorder"):
        log.info("wf-recorder mic mode requested compatible backend; using gpu-screen-recorder")
        return GSRRecorder(cfg)
    if _is_x11() and _has("ffmpeg"):
        log.info("wf-recorder mic mode requested compatible backend; using ffmpeg x11grab")
        return SegmentRecorder(cfg, use_wf_recorder=False)
    raise RuntimeError(
        "Microphone capture with desktop audio is not supported by wf-recorder on this system. "
        "Install gpu-screen-recorder, switch backend, or choose mic-only mode."
    )


def create_recorder(cfg: Config) -> Recorder:
    """
    Instantiate the best available recorder for this system.
    Respects cfg.recording.backend if not 'auto'.
    """
    pref = cfg.recording.backend
    on_wayland = _is_wayland()
    on_x11 = _is_x11()
    has_gsr = _has("gpu-screen-recorder")
    has_wf = _has("wf-recorder")
    has_ffmpeg = _has("ffmpeg")

    if pref in ("wf-recorder", "ffmpeg"):
        if _container(cfg.recording) != "mp4":
            log.warning(
                "recording.container=%s applies to the gpu-screen-recorder "
                "backend; %s clips stay mp4",
                _container(cfg.recording), pref,
            )
        if getattr(cfg.recording, "audio_tracks", None):
            log.warning(
                "recording.audio_tracks applies to the gpu-screen-recorder "
                "backend; %s records a single mixed track", pref,
            )

    if pref == "wf-recorder" and not on_wayland and not on_x11:
        # wf-recorder is the only backend whose selection actually depends on
        # on_wayland downstream (auto + gsr return GSRRecorder regardless;
        # ffmpeg is X11-explicit). Briefly retry Wayland detection in case a
        # packaged launch raced desktop-session startup env exports.
        for _ in range(5):
            time.sleep(0.2)
            on_wayland = _is_wayland()
            on_x11 = _is_x11()
            if on_wayland or on_x11:
                break

    if pref == "gsr":
        if not has_gsr:
            raise RuntimeError(
                "gpu-screen-recorder is selected as the backend, but it is not installed or not on PATH."
            )
        log.info("Selected backend: gpu-screen-recorder")
        return GSRRecorder(cfg)

    if pref == "wf-recorder":
        if not on_wayland:
            raise RuntimeError("wf-recorder requires a Wayland session.")
        if _wf_requires_user_choice(cfg):
            raise RuntimeError(
                "wf-recorder cannot combine desktop audio and microphone until you choose a compatibility mode."
            )
        if _wf_requires_compat_backend(cfg):
            return _create_wf_compatible_recorder(cfg)
        if not has_wf:
            raise RuntimeError(
                "wf-recorder is selected as the backend, but it is not installed or not on PATH."
            )
        log.info("Selected backend: wf-recorder (Wayland segment mode)")
        return SegmentRecorder(cfg, use_wf_recorder=True)

    if pref == "ffmpeg":
        if not on_x11:
            raise RuntimeError("ffmpeg x11grab requires an X11 session.")
        if not has_ffmpeg:
            raise RuntimeError(
                "No supported screen-capture backend found.\n"
                "Install gpu-screen-recorder, wf-recorder, or ffmpeg."
            )
        log.info("Selected backend: ffmpeg x11grab")
        return SegmentRecorder(cfg, use_wf_recorder=False)

    if pref == "auto":
        if has_gsr:
            if not on_wayland and not on_x11:
                log.warning(
                    "Display server env vars not detected (WAYLAND_DISPLAY and DISPLAY both unset). "
                    "GSR will try its own session detection, but if recording fails see "
                    "README troubleshooting for systemd-launched daemons on Hyprland/Sway."
                )
            log.info("Selected backend: gpu-screen-recorder")
            return GSRRecorder(cfg)
        raise RuntimeError(
            "gpu-screen-recorder is required by Vice's auto-backend mode but is not "
            "installed or not on PATH. Rerun ./install.sh, or set "
            "recording.backend in ~/.config/vice/config.toml to 'wf-recorder' or "
            "'ffmpeg' if you specifically need an alternate backend."
        )

    raise RuntimeError(
        f"Invalid recording.backend value: {pref!r}. "
        "Must be one of: 'auto', 'gsr', 'wf-recorder', 'ffmpeg'."
    )
