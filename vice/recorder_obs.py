"""OBS Studio as a capture backend, through its built-in WebSocket server.

For the games ddagrab cannot see (true exclusive fullscreen, some old DX9
titles), OBS's Game Capture hooks the game itself. Vice drives OBS's replay
buffer over obs-websocket v5, which ships with OBS 28 and later:

    SaveReplayBuffer -> ReplayBufferSaved(savedReplayPath) -> Vice moves the
    file into its library and runs the usual trim, volume and watermark steps.

OBS owns everything about the capture (sources, encoder, buffer length); Vice
only asks it to save, and names, files and shares what comes back. Only the
parts of the protocol this needs are implemented.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

import aiohttp

from .config import Config
from .recorder import (
    Recorder,
    _apply_volume_mix,
    _apply_watermark,
    _next_clip_path,
    _next_session_path,
    _trim_to_last_n_seconds,
    _wait_for_finalized_clip,
)
from .runtime import resolve_path

log = logging.getLogger("vice.recorder")

OP_HELLO = 0
OP_IDENTIFY = 1
OP_IDENTIFIED = 2
OP_EVENT = 5
OP_REQUEST = 6
OP_REQUEST_RESPONSE = 7

# EventSubscription bits: General and Outputs (replay buffer, recording).
SUBSCRIBE = (1 << 0) | (1 << 6)

REQUEST_TIMEOUT = 10.0
SAVE_TIMEOUT = 30.0


class OBSError(RuntimeError):
    pass


def auth_response(password: str, salt: str, challenge: str) -> str:
    """obs-websocket v5 authentication string for Identify."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class OBSClient:
    """A single obs-websocket connection: requests with replies, and events."""

    def __init__(self, host: str, port: int, password: str = "") -> None:
        self.url = f"ws://{host}:{port}"
        self.password = password
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._reader: Optional[asyncio.Task] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._waiters: list[tuple[str, asyncio.Future]] = []
        self.closed = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed and not self.closed.is_set()

    async def connect(self, timeout: float = 5.0) -> None:
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(self.url, timeout=timeout, max_msg_size=0)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            await self._session.close()
            self._session = None
            raise OBSError(
                f"Could not reach OBS at {self.url}. Is OBS running with its WebSocket "
                "server on (Tools, WebSocket Server Settings)?"
            ) from exc
        hello = await self._receive(timeout)
        if hello.get("op") != OP_HELLO:
            raise OBSError(f"OBS sent {hello.get('op')} instead of Hello")
        identify: dict = {"rpcVersion": 1, "eventSubscriptions": SUBSCRIBE}
        auth = (hello.get("d") or {}).get("authentication")
        if auth:
            if not self.password:
                await self.close()
                raise OBSError("OBS wants a WebSocket password. Enter it in Settings, Recording.")
            identify["authentication"] = auth_response(self.password, auth["salt"], auth["challenge"])
        await self._ws.send_json({"op": OP_IDENTIFY, "d": identify})
        try:
            identified = await self._receive(timeout)
        except OBSError:
            await self.close()
            raise OBSError("OBS closed the connection. Check the WebSocket password in Settings.")
        if identified.get("op") != OP_IDENTIFIED:
            await self.close()
            raise OBSError("OBS did not accept Vice's connection.")
        self.closed.clear()
        self._reader = asyncio.create_task(self._read_loop())

    async def _receive(self, timeout: float) -> dict:
        assert self._ws is not None
        try:
            msg = await asyncio.wait_for(self._ws.receive(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise OBSError("OBS did not answer in time") from exc
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise OBSError(f"OBS closed the connection ({msg.type.name})")
        return msg.json()

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = msg.json()
                op, d = data.get("op"), data.get("d") or {}
                if op == OP_REQUEST_RESPONSE:
                    fut = self._pending.pop(d.get("requestId", ""), None)
                    if fut and not fut.done():
                        fut.set_result(d)
                elif op == OP_EVENT:
                    self._dispatch_event(d.get("eventType", ""), d.get("eventData") or {})
        except Exception as exc:  # the connection is gone either way
            log.debug("OBS connection ended: %s", exc)
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(OBSError("OBS closed the connection"))
            self._pending.clear()
            for _, fut in self._waiters:
                if not fut.done():
                    fut.set_exception(OBSError("OBS closed the connection"))
            self._waiters.clear()

    def _dispatch_event(self, event_type: str, data: dict) -> None:
        keep = []
        for wanted, fut in self._waiters:
            if wanted == event_type and not fut.done():
                fut.set_result(data)
            elif not fut.done():
                keep.append((wanted, fut))
        self._waiters = keep

    def expect(self, event_type: str) -> asyncio.Future:
        """A future for the next event of this type. Create it before sending
        the request that causes the event, or a fast OBS can beat it."""
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append((event_type, fut))
        return fut

    async def request(self, request_type: str, data: Optional[dict] = None) -> dict:
        if not self.connected or self._ws is None:
            raise OBSError("Not connected to OBS")
        request_id = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        payload: dict = {"requestType": request_type, "requestId": request_id}
        if data:
            payload["requestData"] = data
        await self._ws.send_json({"op": OP_REQUEST, "d": payload})
        try:
            reply = await asyncio.wait_for(fut, timeout=REQUEST_TIMEOUT)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise OBSError(f"OBS did not answer {request_type}") from exc
        status = reply.get("requestStatus") or {}
        if not status.get("result"):
            comment = status.get("comment") or f"code {status.get('code')}"
            raise OBSError(f"OBS refused {request_type}: {comment}")
        return reply.get("responseData") or {}

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            self._reader = None
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()
        self._ws = None
        self._session = None
        self.closed.set()


class OBSRecorder(Recorder):
    """Replay buffer and sessions through OBS Studio."""

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        self._client: Optional[OBSClient] = None
        self._out_dir = resolve_path(cfg.output.directory)
        # Only a replay buffer Vice started is Vice's to stop.
        self._started_buffer = False
        self._buffer_active = False

    @property
    def name(self) -> str:
        return "obs"

    def is_healthy(self) -> bool:
        return self._running and self._client is not None and self._client.connected

    def _new_client(self) -> OBSClient:
        rc = self.cfg.recording
        return OBSClient(getattr(rc, "obs_host", "127.0.0.1") or "127.0.0.1",
                         int(getattr(rc, "obs_port", 4455) or 4455),
                         getattr(rc, "obs_password", "") or "")

    async def start(self) -> None:
        self._running = True
        client = self._new_client()
        try:
            await client.connect()
            status = await client.request("GetReplayBufferStatus")
            if not status.get("outputActive"):
                try:
                    await client.request("StartReplayBuffer")
                except OBSError as exc:
                    raise OBSError(
                        "OBS would not start its replay buffer. Turn it on in OBS under "
                        f"Settings, Output, Replay Buffer. ({exc})"
                    ) from exc
                self._started_buffer = True
            self._buffer_active = True
        except Exception:
            self._running = False
            await client.close()
            raise
        self._client = client
        log.info("Connected to OBS at %s, replay buffer running", client.url)

    async def stop(self) -> None:
        self._running = False
        client, self._client = self._client, None
        if client is None:
            return
        if self._started_buffer and client.connected:
            try:
                await client.request("StopReplayBuffer")
            except OBSError as exc:
                log.debug("Could not stop the OBS replay buffer: %s", exc)
        self._started_buffer = False
        await client.close()

    async def _adopt(self, source: Path, target: Path) -> Path:
        """Move a file OBS wrote into Vice's library under Vice's name."""
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, str(source), str(target))
        return target

    async def save_clip(self, duration: Optional[int] = None) -> Optional[Path]:
        self.last_clip_error = ""
        client = self._client
        if client is None or not client.connected:
            self.last_clip_error = "Vice lost its connection to OBS. Is OBS still running?"
            log.error("Not connected to OBS")
            return None
        clip_duration = int(duration or self.cfg.recording.clip_duration)
        saved = client.expect("ReplayBufferSaved")
        try:
            await client.request("SaveReplayBuffer")
            data = await asyncio.wait_for(saved, timeout=SAVE_TIMEOUT)
        except (OBSError, asyncio.TimeoutError) as exc:
            saved.cancel()
            self.last_clip_error = f"OBS did not save the replay: {exc or 'timed out'}"
            log.error("%s", self.last_clip_error)
            return None
        source = Path(str(data.get("savedReplayPath") or ""))
        if not source.is_file():
            self.last_clip_error = f"OBS reported a replay at {source}, but it is not there."
            log.error("%s", self.last_clip_error)
            return None
        if not await _wait_for_finalized_clip(source):
            self.last_clip_error = f"OBS wrote {source.name}, but it cannot be read."
            log.error("%s", self.last_clip_error)
            return None
        target = _next_clip_path(self._out_dir, ext=source.suffix.lstrip(".") or "mp4",
                                 tag=await self._clip_tag(), template=self._clip_name_template())
        path = await self._adopt(source, target)
        trimmed = await _trim_to_last_n_seconds(path, clip_duration)
        await _apply_volume_mix(trimmed, self.cfg.recording)
        if self.cfg.recording.apply_watermark:
            await _apply_watermark(trimmed)
        log.info("Clip saved: %s", trimmed)
        self._emit(trimmed)
        return trimmed

    async def start_session(self) -> Optional[Path]:
        if self._session_active:
            log.warning("Session already active")
            return None
        if self._client is None or not self._client.connected:
            log.error("Cannot start a session: not connected to OBS")
            return None
        try:
            await self._client.request("StartRecord")
        except OBSError as exc:
            log.error("OBS would not start recording: %s", exc)
            return None
        # OBS picks the file name; this is where Vice will file it.
        self._session_path = _next_session_path(self._out_dir)
        self._session_active = True
        self._session_start = time.time()
        return self._session_path

    async def stop_session(self) -> Optional[Path]:
        if not self._session_active or self._client is None:
            log.warning("No active session to stop")
            return None
        self._session_active = False
        planned, self._session_path = self._session_path, None
        try:
            data = await self._client.request("StopRecord")
        except OBSError as exc:
            log.error("OBS would not stop recording: %s", exc)
            return None
        source = Path(str(data.get("outputPath") or ""))
        if not source.is_file() or planned is None:
            log.error("OBS reported its recording at %s, but it is not there", source)
            return None
        await _wait_for_finalized_clip(source)
        path = await self._adopt(source, planned.with_suffix(source.suffix or ".mp4"))
        if self.cfg.recording.apply_watermark:
            await _apply_watermark(path)
        log.info("Session clip saved: %s", path)
        self._emit(path)
        return path
