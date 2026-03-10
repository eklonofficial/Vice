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
import shutil
import signal
import stat
import subprocess
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from .config import Config

log = logging.getLogger("vice.recorder")

# ──────────────────────────────────────────────────────────────────────────────
# Environment helpers
# ──────────────────────────────────────────────────────────────────────────────

def _is_wayland() -> bool:
    if os.environ.get("WAYLAND_DISPLAY"):
        return True

    runtime_dir = Path(
        os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    )
    if not runtime_dir.exists():
        return False

    for candidate in sorted(runtime_dir.glob("wayland-*")):
        try:
            mode = candidate.stat().st_mode
        except OSError:
            continue

        if stat.S_ISSOCK(mode):
            os.environ["WAYLAND_DISPLAY"] = candidate.name
            os.environ.setdefault("XDG_RUNTIME_DIR", str(runtime_dir))
            log.info(
                "Detected Wayland socket fallback: %s/%s",
                runtime_dir,
                candidate.name,
            )
            return True

    return False


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


def _desktop_audio_source(preferred: str) -> str:
    """
    Resolve a Pulse/PipeWire source name that captures desktop output audio.

    When users leave audio_sink as "default", ffmpeg/wf-recorder may record
    the current default *input* source (microphone) on some setups. We prefer
    the default sink's monitor source so clips contain system/game audio.
    """
    if preferred and preferred != "default":
        return preferred

    if not _has("pactl"):
        return preferred

    try:
        sink = subprocess.check_output(
            ["pactl", "get-default-sink"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if sink:
            return f"{sink}.monitor"
    except Exception:
        pass

    try:
        out = subprocess.check_output(
            ["pactl", "list", "short", "sources"], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            cols = re.split(r"\s+", line.strip())
            if len(cols) > 1 and cols[1].endswith(".monitor"):
                return cols[1]
    except Exception:
        pass

    return preferred


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
    if encoder in ("h264_nvenc", "hevc_nvenc"):
        # NVENC: use CQ mode (similar to CRF) and tuning for low-latency
        return ["-c:v", encoder, "-rc", "vbr", "-cq", str(crf), "-preset", "p4", "-tune", "hq"]
    if encoder == "h264_vaapi":
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
        # Session recording state (shared across all backends)
        self._session_active = False
        self._session_proc: Optional[asyncio.subprocess.Process] = None
        self._session_path: Optional[Path] = None
        self._session_start: float = 0.0

    def on_clip_saved(self, cb: Callable[[Path], None]) -> None:
        """Register a callback invoked with the clip Path once it's ready."""
        self._clip_callbacks.append(cb)

    def _emit(self, path: Path) -> None:
        for cb in self._clip_callbacks:
            try:
                cb(path)
            except Exception:
                log.exception("Clip callback raised")

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def save_clip(self) -> Optional[Path]:
        """
        Trigger saving the last `cfg.recording.clip_duration` seconds.
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

        out_dir = Path(self.cfg.output.directory)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = _next_session_path(out_dir)

        cmd = self._build_session_cmd(out_path)
        if cmd is None:
            log.error("Cannot build session recording command for this environment")
            return None

        log.info("Starting session recording: %s", " ".join(cmd))
        try:
            self._session_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as exc:
            log.error("Failed to start session recording: %s", exc)
            return None

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
        self._session_active = False
        self._session_proc = None
        self._session_path = None

        # Ask ffmpeg/wf-recorder to stop gracefully
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=8)
        except asyncio.TimeoutError:
            proc.kill()
        except Exception as exc:
            log.warning("Session stop signal error: %s", exc)

        if not path or not path.exists():
            log.error("Session file not found after stop: %s", path)
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
                cmd = ["wf-recorder", "--force-yuv", "-f", str(out_path)]
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
        cmd = ["gpu-screen-recorder"]
        cmd += ["-w", "screen" if _is_wayland() else os.environ.get("DISPLAY", ":0")]
        cmd += ["-f", str(rc.fps)]
        cmd += ["-c", "mp4"]
        if rc.capture_audio:
            cmd += ["-a", "default_output"]
        cmd += ["-o", str(out_path)]
        return cmd

    @staticmethod
    def _ffmpeg_session_cmd(out_path: Path, encoder: str, rc) -> list[str]:
        display = os.environ.get("DISPLAY", ":0")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
        cmd += ["-f", "x11grab", "-framerate", str(rc.fps)]
        res = rc.resolution
        if not res:
            # Try to auto-detect
            try:
                import subprocess as _sp
                out = _sp.check_output(["xdpyinfo"], text=True, stderr=_sp.DEVNULL)
                for line in out.splitlines():
                    if "dimensions:" in line:
                        res = line.split()[1]
                        break
            except Exception:
                pass
        if res:
            cmd += ["-s", res]
        cmd += ["-i", display]
        if rc.capture_audio:
            cmd += ["-f", "pulse", "-i", _desktop_audio_source(rc.audio_sink)]
        cmd += _encoder_flags(encoder, rc.crf)
        if rc.capture_audio:
            cmd += ["-c:a", "aac", "-b:a", "128k"]
        cmd += ["-y", str(out_path)]
        return cmd


# ──────────────────────────────────────────────────────────────────────────────
# Clip trimming helper (used by GSR backend)
# ──────────────────────────────────────────────────────────────────────────────

def _next_clip_path(out_dir: Path) -> Path:
    """Return the next available Vice_Clip_N.mp4 path in out_dir."""
    max_n = 0
    for f in out_dir.glob("Vice_Clip_*.mp4"):
        m = re.match(r"^Vice_Clip_(\d+)\.mp4$", f.name)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return out_dir / f"Vice_Clip_{max_n + 1}.mp4"


def _next_session_path(out_dir: Path) -> Path:
    """Return the next available Vice_Session_N.mp4 path in out_dir."""
    max_n = 0
    for f in out_dir.glob("Vice_Session_*.mp4"):
        m = re.match(r"^Vice_Session_(\d+)\.mp4$", f.name)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return out_dir / f"Vice_Session_{max_n + 1}.mp4"


async def _get_duration(path: Path) -> float:
    """Return the duration of a video file in seconds via ffprobe."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        import json
        for s in json.loads(stdout).get("streams", []):
            if s.get("codec_type") == "video":
                return float(s.get("duration", 0))
    except Exception:
        pass
    return 0.0


async def _trim_to_last_n_seconds(path: Path, seconds: int) -> Path:
    """Trim a clip to its last `seconds` seconds in-place. Returns the path."""
    total = await _get_duration(path)
    if total <= 0 or total <= seconds:
        return path  # already short enough

    start = total - seconds
    tmp = path.with_suffix(".trim.mp4")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", str(start), "-i", str(path),
        "-t", str(seconds), "-c", "copy", "-movflags", "+faststart",
        "-y", str(tmp),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            log.error("ffmpeg trim failed: %s", stderr.decode())
            return path  # return original on failure
    except asyncio.TimeoutError:
        log.error("ffmpeg trim timed out")
        return path

    # Replace original with trimmed version
    tmp.replace(path)
    return path


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
    tmp = path.with_suffix(".wm.mp4")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-vf", _WATERMARK,
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "copy",
        "-movflags", "+faststart",
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
        self._out_dir = Path(cfg.output.directory)
        self._watch_task: Optional[asyncio.Task] = None
        self._seen_files: set[str] = set()

    def _build_cmd(self) -> list[str]:
        rc = self.cfg.recording
        cmd = ["gpu-screen-recorder"]

        # Window/screen target
        if _is_wayland():
            # On Wayland, 'screen' captures all outputs; specific output names also work.
            cmd += ["-w", "screen"]
        else:
            display = os.environ.get("DISPLAY", ":0")
            cmd += ["-w", display]

        # Frame rate
        cmd += ["-f", str(rc.fps)]

        # Replay buffer duration
        cmd += ["-r", str(rc.buffer_duration)]

        # Output directory (gsr writes files here on SIGUSR1)
        self._out_dir.mkdir(parents=True, exist_ok=True)
        cmd += ["-o", str(self._out_dir)]

        # Container / codec
        cmd += ["-c", "mp4"]

        # Audio
        if rc.capture_audio:
            # 'default_output' captures desktop audio via PipeWire/PulseAudio
            cmd += ["-a", "default_output"]

        # Quality — gsr uses its own quality flags
        # (encoder selection is automatic in gsr based on detected GPU)

        return cmd

    async def start(self) -> None:
        cmd = self._build_cmd()
        log.info("Starting GSR: %s", " ".join(cmd))
        self._running = True

        # Track existing files so we can detect newly saved clips
        self._seen_files = {f.name for f in self._out_dir.glob("*.mp4")}

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._watch_task = asyncio.create_task(self._stderr_reader())

    async def _stderr_reader(self) -> None:
        assert self._proc and self._proc.stderr
        async for line in self._proc.stderr:
            log.debug("gsr: %s", line.decode().rstrip())

    async def stop(self) -> None:
        self._running = False
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                self._proc.kill()
        if self._watch_task:
            self._watch_task.cancel()

    async def save_clip(self) -> Optional[Path]:
        if not self._proc or self._proc.returncode is not None:
            log.error("GSR process is not running")
            return None

        log.info("Sending SIGUSR1 to GSR (pid=%d) to save replay", self._proc.pid)
        try:
            os.kill(self._proc.pid, signal.SIGUSR1)
        except ProcessLookupError:
            log.error("GSR process not found")
            return None

        # Wait for the new file to appear (up to 10 s)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            current = {f.name for f in self._out_dir.glob("*.mp4")}
            new = current - self._seen_files
            if new:
                newest = max(
                    (self._out_dir / n for n in new),
                    key=lambda p: p.stat().st_mtime,
                )
                self._seen_files = current
                # Rename GSR's auto-generated filename to sequential Vice_Clip_N name.
                seq_path = _next_clip_path(self._out_dir)
                newest.rename(seq_path)
                newest = seq_path
                self._seen_files = {f.name for f in self._out_dir.glob("*.mp4")}
                # GSR saves the entire buffer; trim to the requested clip duration.
                trimmed = await _trim_to_last_n_seconds(newest, self.cfg.recording.clip_duration)
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
        self._out_dir = Path(cfg.output.directory)
        self._out_dir.mkdir(parents=True, exist_ok=True)

    # ── Capture commands ──────────────────────────────────────────────────────

    def _wf_recorder_cmd(self, out: Path) -> list[str]:
        rc = self.cfg.recording
        cmd = ["wf-recorder", "--force-yuv", "-f", str(out)]
        if rc.resolution:
            # wf-recorder geometry flag
            pass  # resolution is auto by default; geometry can be set with -g
        if rc.capture_audio:
            cmd += [f"--audio={_desktop_audio_source(rc.audio_sink)}"]
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
        display = os.environ.get("DISPLAY", ":0")
        res = rc.resolution or self._detect_x11_resolution()

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
        cmd += ["-f", "x11grab", "-framerate", str(rc.fps)]
        if res:
            cmd += ["-s", res]
        cmd += ["-i", display]

        if rc.capture_audio:
            cmd += ["-f", "pulse", "-i", _desktop_audio_source(rc.audio_sink)]

        enc_flags = _encoder_flags(self._encoder, rc.crf)
        cmd += enc_flags

        if rc.capture_audio:
            cmd += ["-c:a", "aac", "-b:a", "128k"]

        cmd += ["-y", str(out)]
        return cmd

    @staticmethod
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

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        log.info(
            "Starting segment recorder (backend=%s, encoder=%s)",
            "wf-recorder" if self._use_wf else "ffmpeg-x11grab",
            self._encoder,
        )
        self._running = True
        self._loop_task = asyncio.create_task(self._record_loop())

    async def stop(self) -> None:
        self._running = False
        if self._current_proc:
            try:
                self._current_proc.terminate()
                await asyncio.wait_for(self._current_proc.wait(), timeout=5)
            except Exception:
                pass
        if self._loop_task:
            self._loop_task.cancel()

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

            try:
                self._current_proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(
                    self._current_proc.wait(), timeout=SEGMENT_DURATION
                )
            except asyncio.TimeoutError:
                # Normal: segment duration elapsed, kill and move on
                if self._current_proc:
                    self._current_proc.terminate()
                    try:
                        await asyncio.wait_for(self._current_proc.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        self._current_proc.kill()
            except asyncio.CancelledError:
                if self._current_proc:
                    self._current_proc.terminate()
                return
            except Exception as exc:
                log.error("Recorder error: %s", exc)
                await asyncio.sleep(1)
                continue

            if seg_path.exists():
                self._segments.append((start_ts, seg_path))
                # Prune old slots that have been overwritten
                self._segments = [
                    (t, p) for t, p in self._segments if p.exists()
                ]

            self._seg_index += 1

    # ── Clip extraction ───────────────────────────────────────────────────────

    async def save_clip(self) -> Optional[Path]:
        rc = self.cfg.recording
        now = time.time()
        clip_start = now - rc.clip_duration

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
            rc.clip_duration,
        )

        # Write concat list for ffmpeg
        concat_list = Path("/tmp/vice/concat.txt")
        with concat_list.open("w") as fh:
            for _, seg in relevant:
                fh.write(f"file '{seg}'\n")

        # Calculate offset into the first segment
        first_ts = relevant[0][0]
        skip = max(0.0, clip_start - first_ts)

        out_path = _next_clip_path(self._out_dir)

        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-ss", str(skip),
            "-t", str(rc.clip_duration),
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

def create_recorder(cfg: Config) -> Recorder:
    """
    Instantiate the best available recorder for this system.
    Respects cfg.recording.backend if not 'auto'.
    """
    pref = cfg.recording.backend

    if pref != "ffmpeg" and not _is_wayland() and not _is_x11():
        # Some packaged launches can race desktop-session startup env exports.
        # Briefly retry Wayland/X11 detection before giving up.
        for _ in range(5):
            time.sleep(0.2)
            if _is_wayland() or _is_x11():
                break

    if pref == "gsr" or (pref == "auto" and _has("gpu-screen-recorder")):
        log.info("Selected backend: gpu-screen-recorder")
        return GSRRecorder(cfg)

    if pref == "wf-recorder" or (pref == "auto" and _is_wayland() and _has("wf-recorder")):
        log.info("Selected backend: wf-recorder (Wayland segment mode)")
        return SegmentRecorder(cfg, use_wf_recorder=True)

    if _is_x11() or pref == "ffmpeg":
        if not _has("ffmpeg"):
            raise RuntimeError(
                "No supported screen-capture backend found.\n"
                "Install gpu-screen-recorder, wf-recorder, or ffmpeg."
            )
        log.info("Selected backend: ffmpeg x11grab")
        return SegmentRecorder(cfg, use_wf_recorder=False)

    raise RuntimeError(
        "Cannot determine display server (WAYLAND_DISPLAY and DISPLAY are both unset, "
        "and no Wayland socket was found in XDG_RUNTIME_DIR). "
        "Are you running in a graphical session?"
    )
