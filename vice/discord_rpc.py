"""Discord IPC client, raw protocol, no third-party deps.

Discord exposes a Unix socket at $XDG_RUNTIME_DIR/discord-ipc-{0..9} (also
/tmp/discord-ipc-N as a fallback). The wire protocol is length-prefixed JSON
frames. We only need HANDSHAKE (op=0) and SET_ACTIVITY (cmd inside op=1).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

# Placeholder. Andrew creates a Discord application at
# https://discord.com/developers/applications, uploads the Vice icon as the
# `vice_logo` art asset, and pastes the Application ID here before tagging
# a release. Until then, users with a `client_id_override` in config can
# still use their own Discord app.
DEFAULT_CLIENT_ID = "1496646444726354031"

_OP_HANDSHAKE = 0
_OP_FRAME = 1
_OP_CLOSE = 2
_OP_PING = 3
_OP_PONG = 4
_DEFAULT_TIMEOUT = 2.0
_MAX_FRAME_BYTES = 64 * 1024


def _socket_paths() -> list[Path]:
    bases: list[Path] = []
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        runtime_path = Path(runtime)
        bases.append(runtime_path)
        # Some Flatpak Discords nest the socket under the app dir.
        bases += [
            runtime_path / "app" / "com.discordapp.Discord",
            runtime_path / "app" / "com.discordapp.DiscordCanary",
            runtime_path / "app" / "com.discordapp.DiscordPTB",
            runtime_path / "app" / "dev.vencord.Vesktop",
            runtime_path / "snap.discord",
        ]
        app_dir = runtime_path / "app"
        try:
            bases.extend(sorted(p for p in app_dir.iterdir() if p.is_dir()))
        except OSError:
            pass
    bases.append(Path("/tmp"))
    paths: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path in seen:
            return
        seen.add(path)
        paths.append(path)

    for base in bases:
        try:
            for path in sorted(base.glob("discord-ipc-*")):
                add(path)
        except OSError:
            pass
        for n in range(10):
            add(base / f"discord-ipc-{n}")
    return paths


class DiscordRPC:
    def __init__(self, client_id: str, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self.client_id = client_id
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._last_payload: dict | None = None

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> bool:
        """Try every candidate socket path. Returns True on first success."""
        if self.is_connected:
            return True
        if not self.client_id:
            log.debug("DiscordRPC: no client_id configured; skipping connect")
            return False
        for path in _socket_paths():
            try:
                if not path.exists():
                    continue
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(str(path)),
                    timeout=self.timeout,
                )
                self._reader, self._writer = reader, writer
                await self._send(_OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
                op, payload = await self._recv_response()
                if op != _OP_FRAME or payload.get("evt") != "READY":
                    raise ConnectionError(f"unexpected handshake response: op={op} payload={payload}")
                log.info("Discord RPC connected via %s", path)
                return True
            except Exception as exc:
                log.debug("DiscordRPC: connect to %s failed: %s", path, exc)
                await self._reset_streams()
                continue
        log.debug("DiscordRPC: no Discord socket reachable (is Discord running?)")
        return False

    async def set_activity(self, activity: dict | None) -> bool:
        """Send SET_ACTIVITY. activity=None clears the card."""
        async with self._lock:
            if not self.is_connected:
                return False
            payload = {
                "cmd": "SET_ACTIVITY",
                "args": {"pid": os.getpid(), "activity": activity},
                "nonce": str(uuid.uuid4()),
            }
            try:
                await self._send(_OP_FRAME, payload)
                # Discord acks with op=1 frame; drain it so it doesn't accumulate.
                op, ack = await self._recv_response()
                if op != _OP_FRAME:
                    raise ConnectionError(f"unexpected activity ack op={op}")
                if ack.get("cmd") not in {None, "SET_ACTIVITY"}:
                    raise ConnectionError(f"unexpected activity ack: {ack}")
                self._last_payload = activity
                return True
            except Exception as exc:
                detail = str(exc) or exc.__class__.__name__
                log.warning("DiscordRPC: set_activity failed (%s); marking disconnected", detail)
                await self._reset_streams()
                return False

    async def close(self) -> None:
        async with self._lock:
            if not self.is_connected:
                return
            try:
                await self._send(_OP_CLOSE, {})
            except Exception as exc:
                log.debug("Discord did not accept the close frame: %s", exc)
            await self._reset_streams()

    # ─── frame I/O ───────────────────────────────────────────────────────────

    async def _send(self, op: int, payload: dict) -> None:
        if not self._writer:
            raise ConnectionError("not connected")
        body = json.dumps(payload).encode("utf-8")
        header = struct.pack("<II", op, len(body))
        self._writer.write(header + body)
        await asyncio.wait_for(self._writer.drain(), timeout=self.timeout)

    async def _recv(self) -> tuple[int, dict]:
        if not self._reader:
            raise ConnectionError("not connected")
        header = await asyncio.wait_for(self._reader.readexactly(8), timeout=self.timeout)
        op, length = struct.unpack("<II", header)
        if length > _MAX_FRAME_BYTES:
            raise ValueError(f"Discord frame too large: {length} bytes")
        body = (
            await asyncio.wait_for(self._reader.readexactly(length), timeout=self.timeout)
            if length
            else b""
        )
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except json.JSONDecodeError:
            payload = {}
        return op, payload

    async def _recv_response(self) -> tuple[int, dict]:
        op, payload = await self._recv()
        if op == _OP_PING:
            await self._send(_OP_PONG, payload)
            op, payload = await self._recv()
        if op == _OP_CLOSE:
            raise ConnectionError(f"Discord closed IPC connection: {payload}")
        if payload.get("evt") == "ERROR":
            raise ConnectionError(f"Discord RPC error: {payload.get('data') or payload}")
        return op, payload

    async def _reset_streams(self) -> None:
        w = self._writer
        self._reader = None
        self._writer = None
        if w is not None:
            try:
                w.close()
                await asyncio.wait_for(w.wait_closed(), timeout=self.timeout)
            except Exception as exc:
                log.debug("Discord socket did not close cleanly: %s", exc)
