"""WASAPI audio capture for the Windows backend.

ffmpeg on Windows cannot record what the speakers are playing: dshow has no
loopback, and there is no WASAPI input device in mainline ffmpeg. So Python
records each source itself (the `soundcard` package speaks WASAPI loopback)
and serves it to ffmpeg as raw PCM over a loopback TCP connection:

    ffmpeg ... -f s16le -ar 48000 -ac 2 -i tcp://127.0.0.1:PORT

Microphones go through the same path rather than dshow, so every audio input
shares one clock and one start time, and ffmpeg's stdin stays free for a
graceful "q".

Two properties matter more than anything else here:

  * The stream must never fall behind the wall clock. WASAPI loopback delivers
    nothing at all while nothing is playing, and soundcard only pads that
    silence approximately (2.3 s of samples over 3 s measured). ffmpeg
    timestamps raw PCM by counting samples, so any shortfall is audio that
    slides earlier and earlier against the video. The capture loop pads with
    silence to wherever the wall clock says it should be, and trims if the
    device clock runs fast.
  * A slow or stuck ffmpeg must never stall capture, so every connection has
    its own bounded queue and sender thread, and drops audio rather than block.

Source ids match the Linux ones, so config files carry over:
    default_output             what the default speakers play
    default_input              the default microphone
    device:<id>.monitor        a specific output, recorded by loopback
    device:<id>                a specific microphone
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time
import warnings
from typing import Optional

log = logging.getLogger("vice.win_audio")


SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_FRAME = 2 * CHANNELS  # s16le stereo
BLOCK_FRAMES = 480              # 10 ms

# Allowed drift from the wall clock before the stream is corrected. Small
# enough to be inaudible as sync error, large enough that ordinary scheduling
# jitter never triggers a correction.
DRIFT_TOLERANCE_FRAMES = SAMPLE_RATE // 25  # 40 ms

# How much audio a connection may buffer before it starts dropping (5 s).
MAX_QUEUED_BLOCKS = 500

PCM_INPUT_ARGS = ["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ch_layout", "stereo"]

MONITOR_SUFFIX = ".monitor"


def _soundcard():
    import soundcard  # imported lazily: it loads COM, and Linux never needs it
    # soundcard warns on stderr whenever WASAPI flags a gap. The wall-clock
    # correction below already repairs gaps, and a daemon's stderr is a log
    # file. Added after the import, because soundcard installs its own
    # "always" filter for these when it loads.
    warnings.filterwarnings("ignore", message="data discontinuity in recording")
    return soundcard


class _ComThread:
    """COM initialised for the current thread, for as long as it is held.

    soundcard only initialises COM on the thread that first imports it.
    Every other thread gets CO_E_NOTINITIALIZED (0x800401F0), so capture
    failed whenever the Settings audio list, running on a worker thread,
    happened to import soundcard before the capture thread did.
    """

    _RPC_E_CHANGED_MODE = 0x80010106

    def __enter__(self) -> "_ComThread":
        import ctypes
        # soundcard must be imported first: its import initialises COM on the
        # importing thread and treats S_FALSE ("already initialised") as a
        # fatal error, so initialising here first broke the import itself.
        _soundcard()
        self._ole32 = ctypes.WinDLL("ole32")
        hr = self._ole32.CoInitializeEx(None, 0) & 0xFFFFFFFF  # COINIT_MULTITHREADED
        # S_OK or S_FALSE must be balanced by CoUninitialize. A thread that is
        # already in another apartment is initialised anyway and is left alone.
        self._owned = hr in (0, 1)
        if not self._owned and hr != self._RPC_E_CHANGED_MODE:
            log.debug("CoInitializeEx returned 0x%08x", hr)
        return self

    def __exit__(self, *exc) -> None:
        if self._owned:
            self._ole32.CoUninitialize()


def list_audio_devices() -> dict:
    """{"outputs": [...], "inputs": [...]}, each {"id", "name"}."""
    with _ComThread():
        sc = _soundcard()
        outputs = [{"id": f"device:{s.id}{MONITOR_SUFFIX}", "name": s.name} for s in sc.all_speakers()]
        inputs = [{"id": f"device:{m.id}", "name": m.name}
                  for m in sc.all_microphones(include_loopback=False)]
    return {"outputs": outputs, "inputs": inputs}


def list_audio_sources() -> dict:
    """The Settings audio source list, in the shape list_gsr_audio_sources
    returns on Linux."""
    sources = [
        {"id": "default_output", "label": "Default output", "kind": "monitor"},
        {"id": "default_input", "label": "Default input", "kind": "input"},
    ]
    try:
        devices = list_audio_devices()
    except Exception as exc:
        return {"sources": sources, "warning": f"Could not list audio devices: {exc}"}
    for d in devices["outputs"]:
        sources.append({"id": d["id"], "label": f"Output: {d['name']}", "kind": "monitor"})
    for d in devices["inputs"]:
        sources.append({"id": d["id"], "label": f"Microphone: {d['name']}", "kind": "input"})
    return {"sources": sources, "warning": None}


def _open_device(source_id: str):
    """(soundcard microphone object, is_loopback) for a source id."""
    sc = _soundcard()
    if source_id == "default_output":
        speaker = sc.default_speaker()
        return sc.get_microphone(id=str(speaker.id), include_loopback=True), True
    if source_id == "default_input":
        return sc.default_microphone(), False
    if source_id.startswith("device:"):
        ident = source_id.split(":", 1)[1]
        if ident.endswith(MONITOR_SUFFIX):
            ident = ident[: -len(MONITOR_SUFFIX)]
            return sc.get_microphone(id=ident, include_loopback=True), True
        return sc.get_microphone(id=ident, include_loopback=False), False
    raise ValueError(f"Audio source {source_id!r} is not available on Windows")


def to_stereo_s16(block, mono: bool = False) -> bytes:
    """float32 frames x channels -> interleaved s16le stereo.

    A single-channel mic is duplicated to both sides; `mono` also folds a
    stereo mic whose signal sits on one channel into the centre (#146).
    """
    import numpy as np
    data = np.asarray(block, dtype="float32")
    if data.ndim == 1:
        data = data[:, None]
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] > 2:
        data = data[:, :2]
    if mono:
        centre = data.mean(axis=1, keepdims=True)
        data = np.repeat(centre, 2, axis=1)
    return (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class WallClock:
    """Keeps a sample stream aligned to real time.

    feed() is told how many frames just arrived and returns how many silent
    frames to insert before them (positive) or how many of them to drop
    (negative) so the running total matches elapsed time.
    """

    def __init__(self, now: Optional[float] = None) -> None:
        self._start = time.perf_counter() if now is None else now
        self._written = 0

    def feed(self, frames: int, now: Optional[float] = None) -> int:
        now = time.perf_counter() if now is None else now
        expected = int((now - self._start) * SAMPLE_RATE)
        gap = expected - (self._written + frames)
        if gap > DRIFT_TOLERANCE_FRAMES:
            self._written += gap + frames
            return gap
        if gap < -DRIFT_TOLERANCE_FRAMES:
            # Device clock is ahead of the wall clock: drop the excess, but
            # never more than this block.
            drop = min(frames, -gap)
            self._written += frames - drop
            return -drop
        self._written += frames
        return 0


class PcmConnection:
    """One ffmpeg input: a one-shot TCP listener, then a sender thread.

    The stream is anchored to `epoch`, a wall-clock time (time.time()) that
    the video timestamps are measured from too. ffmpeg connects some time
    after the epoch, so the stream opens with that much silence and sample 0
    lands on the epoch. Without it, audio started when ffmpeg opened its
    inputs and video when ddagrab finished initialising, about 130 ms later,
    and every clip had its sound that far behind the picture.
    """

    def __init__(self, epoch: Optional[float] = None) -> None:
        self._epoch = epoch
        self._server = socket.create_server(("127.0.0.1", 0))
        self._server.settimeout(30.0)
        self.port = self._server.getsockname()[1]
        self._queue: queue.Queue[Optional[bytes]] = queue.Queue(maxsize=MAX_QUEUED_BLOCKS)
        self._conn: Optional[socket.socket] = None
        self._connected = threading.Event()
        self._closed = threading.Event()
        self.dropped_blocks = 0
        self._thread = threading.Thread(target=self._run, name=f"pcm-{self.port}", daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"tcp://127.0.0.1:{self.port}"

    @property
    def connected(self) -> bool:
        return self._connected.is_set() and not self._closed.is_set()

    def push(self, data: bytes) -> None:
        if not self._connected.is_set() or self._closed.is_set():
            return
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            self.dropped_blocks += 1

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        for sock in (self._conn, self._server):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _run(self) -> None:
        try:
            conn, _ = self._server.accept()
        except OSError:
            self.close()
            return
        finally:
            try:
                self._server.close()
            except OSError:
                pass
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._conn = conn
        lead = 0.0 if self._epoch is None else time.time() - self._epoch
        self._connected.set()
        if 0.0 < lead < 30.0:
            try:
                conn.sendall(b"\0" * (int(lead * SAMPLE_RATE) * BYTES_PER_FRAME))
            except OSError:
                self.close()
                return
        try:
            while not self._closed.is_set():
                data = self._queue.get()
                if data is None:
                    break
                conn.sendall(data)
        except OSError as exc:
            # ffmpeg exited or restarted; the source carries on regardless.
            log.debug("Audio connection %d ended: %s", self.port, exc)
        finally:
            self.close()


class AudioSource:
    """One WASAPI capture thread, fanned out to any number of connections."""

    def __init__(self, source_id: str, mono: bool = False) -> None:
        self.source_id = source_id
        self.mono = mono
        self.error: Optional[str] = None
        self._connections: list[PcmConnection] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._started = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, timeout: float = 5.0) -> None:
        """Start capturing. Raises RuntimeError if the device will not open."""
        self._thread = threading.Thread(target=self._run, name=f"audio-{self.source_id}", daemon=True)
        self._thread.start()
        self._started.wait(timeout)
        if self.error:
            raise RuntimeError(self.error)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            conns, self._connections = self._connections, []
        for conn in conns:
            conn.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def connect(self, epoch: Optional[float] = None) -> PcmConnection:
        """A new ffmpeg input fed from this source, anchored to `epoch`."""
        conn = PcmConnection(epoch)
        with self._lock:
            self._connections = [c for c in self._connections if not c._closed.is_set()]
            self._connections.append(conn)
        return conn

    def _run(self) -> None:
        with _ComThread():
            self._capture()
        if not self._stop.is_set():
            # Capture ended on its own: the device went away (a headset
            # unplugged, an output disabled). Closing the connections is what
            # tells anyone the source died; left open, ffmpeg just waited on
            # a silent input, stopped writing, and still looked healthy.
            with self._lock:
                conns, self._connections = self._connections, []
            for conn in conns:
                conn.close()

    def _capture(self) -> None:
        try:
            device, loopback = _open_device(self.source_id)
            recorder = device.recorder(samplerate=SAMPLE_RATE, channels=None, blocksize=BLOCK_FRAMES)
            recorder.__enter__()
        except Exception as exc:
            self.error = f"Could not open audio source {self.source_id}: {exc}"
            log.error("%s", self.error)
            self._started.set()
            return
        log.info("Capturing audio from %s (%s)", getattr(device, "name", self.source_id),
                 "loopback" if loopback else "input")
        self._started.set()
        clock = WallClock()
        silence_block = b"\0" * (BLOCK_FRAMES * BYTES_PER_FRAME)
        try:
            while not self._stop.is_set():
                block = recorder.record(numframes=BLOCK_FRAMES)
                pcm = to_stereo_s16(block, self.mono)
                correction = clock.feed(len(pcm) // BYTES_PER_FRAME)
                if correction > 0:
                    pad = correction * BYTES_PER_FRAME
                    chunks = [silence_block] * (pad // len(silence_block))
                    chunks.append(b"\0" * (pad % len(silence_block)))
                    self._fan_out(b"".join(chunks))
                elif correction < 0:
                    pcm = pcm[: len(pcm) + correction * BYTES_PER_FRAME]
                if pcm:
                    self._fan_out(pcm)
        except Exception:
            log.exception("Audio capture from %s stopped", self.source_id)
            self.error = f"Audio capture from {self.source_id} stopped unexpectedly."
        finally:
            try:
                recorder.__exit__(None, None, None)
            except Exception:
                pass

    def _fan_out(self, data: bytes) -> None:
        with self._lock:
            conns = list(self._connections)
        for conn in conns:
            conn.push(data)


class AudioHub:
    """The set of sources the recorder is currently using, by id, so a
    session running beside the replay buffer shares the same captures."""

    def __init__(self) -> None:
        self._sources: dict[tuple[str, bool], AudioSource] = {}

    def source(self, source_id: str, mono: bool = False) -> AudioSource:
        key = (source_id, mono)
        src = self._sources.get(key)
        if src is None or src.error:
            src = AudioSource(source_id, mono)
            src.start()
            self._sources[key] = src
        return src

    def stop(self) -> None:
        for src in self._sources.values():
            src.stop()
        self._sources.clear()
