"""Windows capture backend: ffmpeg ddagrab into a rolling ring of segments.

gpu-screen-recorder does not exist on Windows, so the replay buffer is built
from ffmpeg directly:

    ddagrab (Desktop Duplication, frames stay on the GPU)
      -> NVENC / QSV / AMF, or x264 when no hardware encoder will open
      -> the segment muxer, writing 2-second MPEG-TS files into a ring

MPEG-TS because a segment is readable while it is still being written and
survives the process being killed, so saving a clip never has to stop the
recorder, and a crash never corrupts the buffer. Saving concatenates the
newest segments without re-encoding, then hands the file to the same trim,
volume and watermark steps the Linux backends use.

Things learned the hard way while building this, each of which shapes the
code below:

  * ddagrab has to be a -filter_complex source with an explicit D3D11 device
    (-init_hw_device d3d11va=dda:<adapter> -filter_hw_device dda). As an
    -f lavfi input it only ever sees the first GPU's outputs, so on a hybrid
    laptop a monitor wired to the dGPU was unreachable.
  * "ffmpeg lists the encoder" means nothing. A current ffmpeg's NVENC wants
    driver 610+, and on an older driver it is listed and refuses to open. So
    every encoder path is probed with a real three-frame encode, and the
    first that works is used.
  * Frames only stay on the GPU when the encoder belongs to the GPU the
    monitor hangs off. Anything else goes through hwdownload, which costs a
    copy but works everywhere.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import shutil
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import Config
from .oscompat import runtime_dir
from .recorder import (
    KEEP_ALL_STREAMS,
    Recorder,
    _apply_volume_mix,
    _apply_watermark,
    _classify_gsr_source,
    _color_depth,
    _container,
    _extra_gsr_args,
    _gsr_audio_args,
    _gsr_codec_for_encoder,
    _has,
    _next_clip_path,
    _next_session_path,
    _read_stream_text,
    _run_command_capture,
    _selected_display_id,
    _spawn_capture,
    _summarize_process_error,
    _terminate_group,
    _trim_to_last_n_seconds,
    _unregister_capture,
    _kill_tree,
)
from .runtime import resolve_path

log = logging.getLogger("vice.recorder")

SEGMENT_SECONDS = 2

# ── ffmpeg ───────────────────────────────────────────────────────────────────

_capabilities: Optional[dict] = None


def ffmpeg_capabilities() -> dict:
    """{"version", "ddagrab", "encoders"} for the ffmpeg on PATH.

    Cached once an ffmpeg has been found. Not finding one is not cached, so
    installing ffmpeg while Vice runs is picked up at the next restart of
    the recorder instead of the next restart of Vice.
    """
    global _capabilities
    if _capabilities is not None:
        return _capabilities
    info = {"version": "", "ddagrab": False, "encoders": []}
    if not _has("ffmpeg"):
        return info
    _, out = _run_command_capture(["ffmpeg", "-hide_banner", "-version"], timeout=5.0)
    match = re.search(r"ffmpeg version (\S+)", out)
    info["version"] = match.group(1) if match else (out.splitlines()[0] if out else "")
    _, filters = _run_command_capture(["ffmpeg", "-hide_banner", "-filters"], timeout=5.0)
    info["ddagrab"] = bool(re.search(r"\bddagrab\b", filters))
    _, encoders = _run_command_capture(["ffmpeg", "-hide_banner", "-encoders"], timeout=5.0)
    wanted = re.compile(r"^(h264|hevc|av1)_(nvenc|qsv|amf)$|^(libx264|libx265|libsvtav1)$")
    names = []
    for line in encoders.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V") and wanted.match(parts[1]):
            names.append(parts[1])
    info["encoders"] = names
    if info["version"]:
        _capabilities = info
    return info


# ── Displays ─────────────────────────────────────────────────────────────────

def _outputs() -> list[dict]:
    from .win32 import dxgi_outputs
    return dxgi_outputs()


def display_key(value: str) -> str:
    """DISPLAY1, whether config.toml says DISPLAY1 or the full device path
    with its leading backslashes and dot."""
    return str(value or "").strip().lstrip("\\.").upper()


def display_label(out: dict) -> str:
    name = display_key(out["device"])
    primary = ", primary" if out.get("primary") else ""
    return f"{name} {out['width']}x{out['height']} ({out['gpu']}{primary})"


def list_display_options() -> dict:
    """The Settings display list, in the shape the Linux backends return."""
    outputs = _outputs()
    displays = [{"id": out["device"], "label": display_label(out)} for out in outputs]
    warning = None if displays else "Could not list displays through DXGI."
    return {"backend": "ffmpeg", "displays": displays, "warning": warning}


def resolve_output(rc, override: Optional[str] = None) -> Optional[dict]:
    """The DXGI output to capture: the configured one, else the primary."""
    outputs = _outputs()
    if not outputs:
        return None
    selected = (_selected_display_id(rc, override) or "").strip()
    if selected:
        for out in outputs:
            if (display_key(selected) == display_key(out["device"])
                    or selected == f"{out['adapter']}:{out['output']}"):
                return out
        log.warning("Configured display %r is not connected; capturing the primary display", selected)
    for out in outputs:
        if out.get("primary"):
            return out
    return outputs[0]


# ── Encoders ─────────────────────────────────────────────────────────────────

_VENDOR_OF_SUFFIX = {"nvenc": "nvidia", "qsv": "intel", "amf": "amd"}
_SOFTWARE = {"h264": "libx264", "hevc": "libx265", "av1": "libsvtav1"}


@dataclass(frozen=True)
class EncoderPlan:
    """How to get ddagrab's frames into one encoder."""
    encoder: str
    zero_copy: bool
    # Arguments before the filter graph (hardware devices).
    device_args: tuple[str, ...] = ()
    # Filters appended after ddagrab.
    filters: tuple[str, ...] = ()

    @property
    def hardware(self) -> bool:
        return not self.encoder.startswith("lib")

    @property
    def label(self) -> str:
        return f"{self.encoder}{'' if self.zero_copy or not self.hardware else ' (copy)'}"


def _codec_family(rc) -> str:
    """h264, hevc or av1 for the configured encoder.

    Read from the name first: gpu-screen-recorder's mapping predates QSV and
    AMF, so hevc_qsv or av1_amf came back as None and silently became H.264.
    """
    encoder = str(rc.encoder or "")
    prefix = encoder.split("_", 1)[0]
    if prefix in _SOFTWARE and "_" in encoder:
        return prefix
    family = _gsr_codec_for_encoder(encoder, "8") or "h264"
    return family if family in _SOFTWARE else "h264"


def encoder_plans(rc, out: dict, vendors: list[str], available: list[str]) -> list[EncoderPlan]:
    """Every way to encode `out` worth trying, best first.

    An explicitly chosen encoder goes first. After that: the monitor's own GPU
    without leaving it, then any other hardware encoder fed downloaded frames,
    then software, which always works.
    """
    family = _codec_family(rc)
    adapter_vendor = out.get("vendor", "other")
    plans: list[EncoderPlan] = []

    def add(plan: EncoderPlan) -> None:
        if plan.encoder in available and plan not in plans:
            plans.append(plan)

    def zero_copy(suffix: str) -> Optional[EncoderPlan]:
        name = f"{family}_{suffix}"
        if suffix == "qsv":
            return EncoderPlan(name, True, ("-init_hw_device", "qsv=qs@dda"),
                               ("hwmap=derive_device=qsv", "format=qsv"))
        return EncoderPlan(name, True)  # NVENC and AMF take D3D11 frames as they are

    def downloaded(name: str) -> EncoderPlan:
        pix = "yuv420p" if name.startswith("lib") else "nv12"
        return EncoderPlan(name, False, (), ("hwdownload", "format=bgra", f"format={pix}"))

    chosen = rc.encoder if rc.encoder in available else None
    if chosen:
        suffix = chosen.rsplit("_", 1)[-1]
        if _VENDOR_OF_SUFFIX.get(suffix) == adapter_vendor:
            add(zero_copy(suffix))
        add(downloaded(chosen))

    for suffix, vendor in _VENDOR_OF_SUFFIX.items():
        if vendor == adapter_vendor:
            plan = zero_copy(suffix)
            if plan:
                add(plan)
    for suffix, vendor in _VENDOR_OF_SUFFIX.items():
        if vendor in vendors:
            add(downloaded(f"{family}_{suffix}"))
    add(downloaded(_SOFTWARE[family]))
    if family != "h264":
        add(downloaded("libx264"))
    return plans


def encoder_args(plan: EncoderPlan, rc, fps: int) -> list[str]:
    """Quality and keyframe flags per encoder.

    The segment muxer can only split on a keyframe, so the GOP decides how
    fine-grained the ring is. -force_key_frames is deliberately not used:
    with h264_qsv it stopped the muxer splitting at all, which would have let
    one segment grow for as long as the recorder ran.
    """
    q = str(max(0, min(51, int(rc.crf))))
    gop = ["-g", str(fps * SEGMENT_SECONDS)]
    name = plan.encoder
    if name.endswith("_nvenc"):
        return ["-c:v", name, "-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", q, "-b:v", "0", *gop]
    if name.endswith("_qsv"):
        # Every I-frame an IDR, or the muxer skips the boundary.
        return ["-c:v", name, "-preset", "veryfast", "-global_quality", q, *gop,
                "-idr_interval", "0"]
    if name.endswith("_amf"):
        return ["-c:v", name, "-usage", "lowlatency", "-rc", "cqp", "-qp_i", q, "-qp_p", q, *gop]
    if name == "libsvtav1":
        return ["-c:v", name, "-preset", "10", "-crf", q, *gop]
    return ["-c:v", name, "-preset", "veryfast", "-crf", q, *gop]


# Only encoders that worked are remembered. A probe can fail because the
# capture could not start (lock screen, a UAC prompt, a display changing
# mode) rather than because of the encoder, and caching that failure left the
# watchdog unable to ever start recording again in that process.
_probe_cache: dict[tuple, bool] = {}


async def _probe_plan(plan: EncoderPlan, out: dict) -> bool:
    """Whether this plan really encodes on this machine: three frames of the
    real capture, through the real encoder, into nothing."""
    key = (plan, out["adapter"], out["output"])
    if key in _probe_cache:
        return _probe_cache[key]
    graph = _video_graph(out, plan, fps=30, label="v")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
        *_device_args(out, plan),
        "-filter_complex", graph, "-map", "[v]",
        *encoder_args(plan, _ProbeQuality(), 30),
        "-frames:v", "3", "-f", "null", "-",
    ]
    ok = False
    detail = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), timeout=15)
        ok = proc.returncode == 0
        detail = _probe_failure_reason((err or b"").decode(errors="replace"))
    except asyncio.TimeoutError:
        detail = "timed out"
        try:
            proc.kill()
        except Exception:
            pass
    except OSError as exc:
        detail = str(exc)
    if ok:
        log.info("Encoder %s works on %s", plan.label, out["device"])
    else:
        log.info("Encoder %s is unusable here: %s", plan.label, detail or "no reason given")
    if ok:
        _probe_cache[key] = True
    return ok


class _ProbeQuality:
    crf = 30


def _probe_failure_reason(stderr: str) -> str:
    """The line that says why, not ffmpeg's closing "Nothing was written".
    For NVENC that is the one naming the driver version it wants."""
    lines = [ln.split("] ", 1)[-1].strip() for ln in stderr.splitlines() if ln.strip()]
    for line in lines:
        if re.search(r"driver|not support|minimum required|cannot load|failed to|unsupported", line, re.I):
            return line
    return lines[-1] if lines else ""


def _device_args(out: dict, plan: EncoderPlan) -> list[str]:
    return ["-init_hw_device", f"d3d11va=dda:{out['adapter']}", "-filter_hw_device", "dda",
            *plan.device_args]


def _video_graph(out: dict, plan: EncoderPlan, fps: int, label: str,
                 resolution: Optional[str] = None, epoch_us: Optional[int] = None) -> str:
    chain = [f"ddagrab=output_idx={out['output']}:framerate={fps}"]
    if epoch_us is not None:
        # Stamp each frame with when it was captured, measured from the same
        # epoch the audio stream is anchored to (see win_audio.PcmConnection).
        chain.append(f"setpts=(RTCTIME-{epoch_us})/(TB*1000000)")
    filters = list(plan.filters)
    scale = _parse_resolution(resolution)
    if scale:
        if plan.zero_copy and plan.encoder.endswith("_qsv"):
            filters.append(f"scale_qsv=w={scale[0]}:h={scale[1]}")
        elif plan.zero_copy:
            # NVENC/AMF zero-copy has no D3D11 scaler to lean on, so a
            # resolution change costs the download path.
            filters = ["hwdownload", "format=bgra", f"scale={scale[0]}:{scale[1]}", "format=nv12"]
        else:
            filters.insert(len(filters) - 1, f"scale={scale[0]}:{scale[1]}")
    chain += filters
    return ",".join(chain) + f"[{label}]"


def _parse_resolution(value: Optional[str]) -> Optional[tuple[int, int]]:
    match = re.fullmatch(r"\s*(\d{2,5})\s*[xX]\s*(\d{2,5})\s*", value or "")
    if not match:
        return None
    w, h = int(match.group(1)), int(match.group(2))
    return (w - w % 2, h - h % 2)


# ── Audio layout ─────────────────────────────────────────────────────────────

def audio_tracks(rc, *, split_for_volume: bool = True) -> list[list[str]]:
    """The audio tracks to record, each a list of source ids to mix.

    Worked out by exactly the rules gpu-screen-recorder's -a flags follow on
    Linux (separate tracks, the mix-first track, splitting desktop and mic
    for the save-time volume pass), so a config behaves the same on both.
    Per-application capture has no Windows equivalent yet and is dropped.
    """
    args = _gsr_audio_args(rc, split_for_volume=split_for_volume)
    tracks: list[list[str]] = []
    for i in range(0, len(args) - 1, 2):
        if args[i] != "-a":
            continue
        parts = [p for p in args[i + 1].split("|") if p]
        kept = [p for p in parts if _classify_gsr_source(p) != "app"]
        if len(kept) != len(parts):
            log.warning("Per-application audio (%s) is not available on Windows; skipping it",
                        ", ".join(p for p in parts if p not in kept))
        if kept:
            tracks.append(kept)
    return tracks


def audio_graph(tracks: list[list[str]], first_input: int) -> tuple[list[str], list[str], list[str]]:
    """(sources in input order, filter_complex parts, -map arguments)."""
    sources: list[str] = []
    for track in tracks:
        for src in track:
            if src not in sources:
                sources.append(src)
    filters: list[str] = []
    maps: list[str] = []
    for n, track in enumerate(tracks):
        inputs = [first_input + sources.index(s) for s in track]
        if len(inputs) == 1:
            maps += ["-map", f"{inputs[0]}:a"]
            continue
        pads = "".join(f"[{i}:a]" for i in inputs)
        filters.append(f"{pads}amix=inputs={len(inputs)}:normalize=0:dropout_transition=0[a{n}]")
        maps += ["-map", f"[a{n}]"]
    return sources, filters, maps


AUDIO_CODEC_ARGS = ["-c:a", "aac", "-b:a", "160k"]


# ── Command building ─────────────────────────────────────────────────────────

def build_capture_cmd(rc, out: dict, plan: EncoderPlan, audio_urls: list[str],
                      tracks: list[list[str]], output_args: list[str],
                      epoch_us: Optional[int] = None) -> list[str]:
    """One ffmpeg command: ddagrab video, the audio inputs, and the output.

    audio_urls are in the order audio_graph() lists the sources. epoch_us is
    the wall-clock time both streams are timed from.
    """
    fps = max(1, int(rc.fps))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
           *_device_args(out, plan)]
    from .win_audio import PCM_INPUT_ARGS
    for url in audio_urls:
        cmd += ["-thread_queue_size", "4096", *PCM_INPUT_ARGS, "-i", url]
    graph = [_video_graph(out, plan, fps, "v", rc.resolution, epoch_us)]
    _, audio_filters, audio_maps = audio_graph(tracks, first_input=0)
    graph += audio_filters
    cmd += ["-filter_complex", ";".join(graph), "-map", "[v]", *audio_maps,
            *encoder_args(plan, rc, fps)]
    if audio_maps:
        cmd += AUDIO_CODEC_ARGS
    extra = _extra_gsr_args(getattr(rc, "gsr_args", "") or "")
    return cmd + extra + output_args


def segment_output_args(seg_dir: Path, rc) -> list[str]:
    ring = math.ceil(max(int(rc.buffer_duration), SEGMENT_SECONDS) / SEGMENT_SECONDS) + 2
    # Timestamps run on across segments (no -reset_timestamps), so joining
    # segments byte for byte gives one continuous stream. Re-basing each
    # segment, which the concat demuxer does, drifted the sound about 5 ms
    # per second: audio and video never end a segment at exactly the same
    # instant, and the difference piled up at every boundary.
    return ["-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
            "-segment_wrap", str(ring),
            "-segment_format", "mpegts", str(seg_dir / "seg%05d.ts")]


def concat_input(segments: list[Path]) -> str:
    """An ffmpeg concat-protocol URL: the segments read as one file."""
    return "concat:" + "|".join(p.as_posix() for p in segments)


def select_segments(files: list[tuple[float, int, Path]], seconds: int) -> list[Path]:
    """The newest segments that together cover `seconds`, oldest first.

    files are (mtime, size, path). A segment's length is read off the clock
    rather than assumed: it ends when its file was last written and begins
    when the one before it ended. Encoders do not all honour the GOP length
    to the frame (QSV skipped a whole boundary in testing), so counting files
    would come up short. Empty files are the slot the muxer has just opened,
    and are skipped. One extra segment is kept as margin; the trim that
    follows cuts the clip to the exact length.
    """
    usable = sorted((f for f in files if f[1] > 0), key=lambda f: f[0])
    if not usable:
        return []
    wanted = max(seconds, 1)
    picked = [usable[-1]]
    covered = 0.0
    for i in range(len(usable) - 1, 0, -1):
        covered += max(usable[i][0] - usable[i - 1][0], 0.0)
        picked.insert(0, usable[i - 1])
        if covered >= wanted:
            break
    return [p for _, _, p in picked]


# ── The recorder ─────────────────────────────────────────────────────────────

class DDAGrabRecorder(Recorder):
    """Replay buffer on Windows through ffmpeg ddagrab."""

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._out_dir = resolve_path(cfg.output.directory)
        self._seg_root = runtime_dir() / "segs"
        self._seg_dir = self._seg_root
        self._runs = 0
        self._stderr_task: Optional[asyncio.Task] = None
        self._stderr_tail: deque[str] = deque(maxlen=12)
        self._connections: list = []
        self._hub = None
        self._plan: Optional[EncoderPlan] = None
        self._output: Optional[dict] = None
        self._save_lock = asyncio.Lock()
        self.cpu_fallback = False
        self.codec_fallback = False

    @property
    def name(self) -> str:
        return "ffmpeg-ddagrab"

    def is_healthy(self) -> bool:
        if not (self._running and self._proc is not None and self._proc.returncode is None):
            return False
        # A dead audio capture means clips are silently missing sound; let
        # the watchdog restart everything.
        return not any(getattr(c, "_closed").is_set() for c in self._connections)

    def last_output(self) -> str:
        return "\n".join(self._stderr_tail)

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def _choose_plan(self, out: dict) -> EncoderPlan:
        from .win32 import gpu_vendors
        caps = await asyncio.to_thread(ffmpeg_capabilities)
        if not caps["version"]:
            raise RuntimeError("ffmpeg is not installed or not on PATH. Install it with: "
                               "winget install Gyan.FFmpeg")
        if not caps["ddagrab"]:
            raise RuntimeError(f"ffmpeg {caps['version']} has no ddagrab filter. "
                               "Vice needs ffmpeg 6.0 or newer.")
        vendors = await asyncio.to_thread(gpu_vendors)
        plans = encoder_plans(self.cfg.recording, out, vendors, caps["encoders"])
        for i, plan in enumerate(plans):
            if await _probe_plan(plan, out):
                wanted = self.cfg.recording.encoder
                # Only a fallback when hardware was wanted; libx264 chosen on
                # purpose must not raise the "GPU encoder would not open" banner.
                wanted_hardware = wanted == "auto" or not str(wanted).startswith("lib")
                self.cpu_fallback = (not plan.hardware and wanted_hardware
                                     and any(p.hardware for p in plans))
                self.codec_fallback = (wanted not in ("auto", plan.encoder)
                                       and plan.hardware and i > 0)
                return plan
        raise RuntimeError("No video encoder would open on this machine. "
                           "Run 'vice doctor' to see what ffmpeg reports.")

    def _open_audio(self, tracks: list[list[str]], epoch: float) -> list:
        """Start (or reuse) the captures and open one ffmpeg input per source."""
        from .win_audio import AudioHub
        sources, _, _ = audio_graph(tracks, 0)
        if not sources:
            return []
        if self._hub is None:
            self._hub = AudioHub()
        return [self._hub.source(src).connect(epoch) for src in sources]

    async def start(self) -> None:
        self._running = True
        self.cpu_fallback = False
        self.codec_fallback = False
        rc = self.cfg.recording
        if _color_depth(rc) == "10":
            log.info("10-bit recording is not available on Windows yet; recording 8-bit")
        # DXGI enumeration and opening audio devices both block, the latter
        # for up to seconds; neither may stall the daemon's event loop.
        out = await asyncio.to_thread(resolve_output, rc, self.display_override)
        if out is None:
            self._running = False
            raise RuntimeError("No display found to capture.")
        try:
            plan = await self._choose_plan(out)
        except Exception:
            self._running = False
            raise
        self._plan, self._output = plan, out

        self._seg_dir = self._fresh_segment_dir()
        self._out_dir.mkdir(parents=True, exist_ok=True)

        tracks = audio_tracks(rc)
        epoch = time.time()
        try:
            self._connections = await asyncio.to_thread(self._open_audio, tracks, epoch)
        except Exception as exc:
            self._running = False
            self._close_audio()
            raise RuntimeError(str(exc)) from exc
        cmd = build_capture_cmd(rc, out, plan, [c.url for c in self._connections], tracks,
                                segment_output_args(self._seg_dir, rc), int(epoch * 1_000_000))
        log.info("Starting ffmpeg capture of %s with %s: %s",
                 out["device"], plan.label, " ".join(cmd))
        self._stderr_tail.clear()
        self._proc = await _spawn_capture(cmd, asyncio.subprocess.PIPE,
                                          stdin=asyncio.subprocess.PIPE)
        self._stderr_task = asyncio.create_task(self._read_stderr(self._proc))
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            return
        # It exited inside two seconds, so it is not going to record.
        await asyncio.sleep(0.1)
        detail = _summarize_process_error("ffmpeg", self._proc.returncode, self.last_output())
        await self.stop()
        raise RuntimeError(f"ffmpeg failed to start: {detail}")

    def _fresh_segment_dir(self) -> Path:
        """A new directory for this run's ring.

        Never reused: a capture left over from a crashed daemon can still be
        writing into the old one, and Windows will not let a file that is
        open be deleted, so clearing it in place failed the start. Old run
        directories are removed when nothing holds them any more.
        """
        self._runs += 1
        self._seg_root.mkdir(parents=True, exist_ok=True)
        for old in self._seg_root.iterdir():
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
            else:
                try:
                    old.unlink()
                except OSError:
                    pass
        path = self._seg_root / f"{os.getpid()}-{self._runs}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _read_stderr(self, proc) -> None:
        if proc.stderr is None:
            return
        async for raw in proc.stderr:
            text = raw.decode(errors="replace").rstrip()
            if not text:
                continue
            # The segment muxer announces every file it opens.
            if "Opening '" in text and ".ts' for writing" in text:
                log.debug("ffmpeg: %s", text)
                continue
            self._stderr_tail.append(text)
            log.warning("ffmpeg: %s", text)

    def _close_audio(self) -> None:
        for conn in self._connections:
            conn.close()
        self._connections = []

    async def stop(self) -> None:
        self._running = False
        proc, self._proc = self._proc, None
        if proc is not None:
            # MPEG-TS needs no clean shutdown, so no waiting on "q" here.
            await _terminate_group(proc, timeout=3)
        if self._stderr_task:
            self._stderr_task.cancel()
            self._stderr_task = None
        self._close_audio()
        if not self._session_active and self._hub is not None:
            await asyncio.to_thread(self._hub.stop)
            self._hub = None
        if self._seg_dir != self._seg_root:
            # The buffer is gone with the process; its segments are just disk.
            shutil.rmtree(self._seg_dir, ignore_errors=True)

    # ── clips ────────────────────────────────────────────────────────────────

    def _segment_files(self) -> list[tuple[float, int, Path]]:
        files = []
        for path in self._seg_dir.glob("seg*.ts"):
            try:
                st = path.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, path))
        return files

    async def save_clip(self, duration: Optional[int] = None) -> Optional[Path]:
        self.last_clip_error = ""
        if not self.is_healthy():
            self.last_clip_error = "The recorder is not running. Check Settings for an encoder error."
            log.error("ffmpeg capture is not running")
            return None
        rc = self.cfg.recording
        clip_duration = int(duration or rc.clip_duration)
        async with self._save_lock:
            segments = select_segments(self._segment_files(), clip_duration)
            if not segments:
                self.last_clip_error = "Nothing has been recorded yet."
                log.error("No segments to clip from")
                return None

            ext = _container(rc)
            out_path = _next_clip_path(self._out_dir, ext=ext, tag=await self._clip_tag(),
                                       template=self._clip_name_template())
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                   "-f", "mpegts", "-i", concat_input(segments),
                   *KEEP_ALL_STREAMS, "-c", "copy"]
            if ext == "mp4":
                cmd += ["-movflags", "+faststart"]
            cmd += ["-y", str(out_path)]
            log.info("Saving clip from %d segment(s): %s", len(segments), " ".join(cmd))
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
            except asyncio.TimeoutError:
                self.last_clip_error = "ffmpeg took too long to save the clip."
                log.error("Clip concat timed out")
                return None
            text = (err or b"").decode(errors="replace").strip()
            if not out_path.exists() or out_path.stat().st_size == 0:
                out_path.unlink(missing_ok=True)
                self.last_clip_error = f"ffmpeg could not save the clip: {text or 'no output'}"
                log.error("Clip concat failed: %s", text)
                return None
            if proc.returncode != 0:
                # The newest segment ends mid-packet, which the concat demuxer
                # reports and then works around. A file that plays is a clip.
                log.debug("Clip concat finished with warnings: %s", text)

        trimmed = await _trim_to_last_n_seconds(out_path, clip_duration)
        await _apply_volume_mix(trimmed, rc)
        if rc.apply_watermark:
            await _apply_watermark(trimmed)
        log.info("Clip saved: %s", trimmed)
        self._emit(trimmed)
        return trimmed

    # ── sessions ─────────────────────────────────────────────────────────────

    async def start_session(self) -> Optional[Path]:
        """A second ddagrab capture straight to a file, sharing the replay
        buffer's audio captures. Desktop Duplication allows several
        duplications of one output, so this does not disturb the buffer."""
        if self._session_active:
            log.warning("Session already active")
            return None
        rc = self.cfg.recording
        out = self._output or await asyncio.to_thread(resolve_output, rc, self.display_override)
        if out is None:
            log.error("No display found to record the session from")
            return None
        try:
            plan = self._plan or await self._choose_plan(out)
        except Exception as exc:
            log.error("Cannot start a session: %s", exc)
            return None
        out_dir = resolve_path(self.cfg.output.directory)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = _next_session_path(out_dir, _container(rc))
        tracks = audio_tracks(rc, split_for_volume=False)
        epoch = time.time()
        try:
            conns = await asyncio.to_thread(self._open_audio, tracks, epoch)
        except Exception as exc:
            log.error("Cannot open audio for the session: %s", exc)
            return None
        output_args = (["-movflags", "+faststart"] if out_path.suffix == ".mp4" else []) + ["-y", str(out_path)]
        cmd = build_capture_cmd(rc, out, plan, [c.url for c in conns], tracks, output_args,
                                int(epoch * 1_000_000))
        log.info("Starting session recording: %s", " ".join(cmd))
        try:
            proc = await _spawn_capture(cmd, asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE)
        except Exception as exc:
            for c in conns:
                c.close()
            log.error("Failed to start session recording: %s", exc)
            return None
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.5)
            detail = _summarize_process_error("ffmpeg", proc.returncode,
                                              await _read_stream_text(proc.stderr))
            log.error("Session recorder failed to start: %s", detail)
            _unregister_capture(proc.pid)
            for c in conns:
                c.close()
            return None
        except asyncio.TimeoutError:
            pass
        self._session_proc = proc
        self._session_conns = conns
        self._session_program = "ffmpeg"
        self._session_active = True
        self._session_path = out_path
        self._session_start = time.time()
        return out_path

    async def stop_session(self) -> Optional[Path]:
        if not self._session_active or not self._session_proc:
            log.warning("No active session to stop")
            return None
        path, proc = self._session_path, self._session_proc
        conns = getattr(self, "_session_conns", [])
        self._session_active = False
        self._session_proc = None
        self._session_path = None
        self._session_program = ""
        # MP4 needs its index written, so ask ffmpeg to finish ("q"), and
        # only kill it if it will not.
        try:
            if proc.stdin is not None:
                proc.stdin.write(b"q")
                await proc.stdin.drain()
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=15)
        except (asyncio.TimeoutError, OSError, ConnectionError) as exc:
            log.warning("Session recorder did not finish cleanly (%s); stopping it", exc)
            await asyncio.to_thread(_kill_tree, proc.pid)
        _unregister_capture(proc.pid)
        for c in conns:
            c.close()
        self._session_conns = []
        if not path or not path.exists() or path.stat().st_size == 0:
            log.error("Session file not found after stop: %s", path)
            return None
        if self.cfg.recording.apply_watermark:
            await _apply_watermark(path)
        log.info("Session clip saved: %s", path)
        self._emit(path)
        return path


# ── Screenshots ──────────────────────────────────────────────────────────────

async def capture_screenshot(out_path: Path, rc, override: Optional[str] = None,
                             timeout: float = 20.0) -> Path:
    """One frame of the chosen display, written as an image."""
    out = resolve_output(rc, override)
    if out is None:
        raise RuntimeError("No display found to capture.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plan = EncoderPlan("png", False)
    graph = (f"ddagrab=output_idx={out['output']}:framerate=10,"
             f"hwdownload,format=bgra[v]")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *_device_args(out, plan),
           "-filter_complex", graph, "-map", "[v]", "-frames:v", "1", "-update", "1",
           "-y", str(out_path)]
    log.debug("Screenshot command: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError("ffmpeg did not return a screenshot in time.")
    except OSError as exc:
        raise RuntimeError(f"Could not run ffmpeg: {exc}") from exc
    text = (err or b"").decode(errors="replace")
    if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(_summarize_process_error("ffmpeg", proc.returncode, text)
                           or "ffmpeg wrote no image.")
    return out_path

