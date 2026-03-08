"""
Vice — Linux game clip recorder daemon + CLI.

Commands:
  vice start          Start the daemon (recorder + hotkey listener + share server)
  vice ui             Open the web UI in the default browser
  vice clip           Manually save a clip right now (daemon must be running)
  vice stop           Stop the daemon
  vice status         Show daemon status and recent clips
  vice config         Print the current config path and contents
  vice list-keys      Show available hotkey names (KEY_*)
  vice open-config    Open config in $EDITOR
  vice uninstall      Remove Vice cleanly (service, config, optionally clips)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from . import __version__
from .config import load as load_config, save as save_config, CONFIG_PATH, CONFIG_DIR
from .hotkey import HotkeyListener, list_available_keys
from .recorder import create_recorder
from .share import ShareServer
from . import audio

log = logging.getLogger("vice")

PID_FILE    = Path("/tmp/vice/vice.pid")
SOCKET_FILE = Path("/tmp/vice/vice.sock")


# ──────────────────────────────────────────────────────────────────────────────
# Daemon
# ──────────────────────────────────────────────────────────────────────────────

class ViceDaemon:
    def __init__(self) -> None:
        self.cfg      = load_config()
        self.recorder = create_recorder(self.cfg)
        self.hotkeys  = HotkeyListener()
        self.share:   Optional[ShareServer] = None
        self._clip_lock  = asyncio.Lock()
        self._clip_count = 0
        # Session recording state
        self._session_active   = False
        self._session_path:    Optional[Path] = None
        self._session_highlights: list[dict] = []  # {time, label, color}

    async def run(self) -> None:
        Path("/tmp/vice").mkdir(parents=True, exist_ok=True)
        Path(self.cfg.output.directory).mkdir(parents=True, exist_ok=True)

        # Share server (web UI + REST API + WebSocket)
        if self.cfg.sharing.enabled:
            self.share = ShareServer(self.cfg)
            self.share.trigger_clip_cb = self._handle_clip_hotkey
            self.share.get_status_cb   = self._get_status
            await self.share.start()

        # Recorder callback — fires for both normal clips and session clips
        def _on_clip_saved(path: Path) -> None:
            self._clip_count += 1
            click.echo(f"\n[Vice] Clip saved: {path}")
            if self.share:
                # Session clips are added to the share server inside _stop_session;
                # only add here for regular replay-buffer clips (not sessions).
                if not path.name.startswith("Vice_Session_"):
                    url = self.share.add_clip(path)
                    click.echo(f"[Vice] Share URL:  {url}\n")
                asyncio.create_task(
                    self.share.broadcast({
                        "type": "status", "recording": True,
                        "backend": self.recorder.name,
                        "session_active": self._session_active,
                    })
                )

        self.recorder.on_clip_saved(_on_clip_saved)

        # Hotkeys
        clip_key = self.cfg.hotkeys.clip
        if clip_key:
            # Single tap → save clip (or add session highlight)
            self.hotkeys.on(clip_key, self._handle_clip_hotkey)
            # Double tap → toggle session recording
            self.hotkeys.on_double(clip_key, self._handle_session_toggle)

        PID_FILE.write_text(str(os.getpid()))

        server = await asyncio.start_unix_server(
            self._handle_ipc, path=str(SOCKET_FILE)
        )

        await self.hotkeys.start()
        await self.recorder.start()

        if self.share:
            asyncio.create_task(
                self.share.broadcast({
                    "type": "status", "recording": True,
                    "backend": self.recorder.name,
                })
            )

        click.echo(f"[Vice {__version__}] Recording started.")
        click.echo(f"  Backend   : {self.recorder.name}")
        click.echo(f"  Clip key  : {clip_key or '(none)'}")
        click.echo(f"  Output    : {self.cfg.output.directory}")
        if self.share and self.share.base_url():
            click.echo(f"  UI + Share: {self.share.base_url()}/")
        click.echo("Press Ctrl-C to stop.\n")

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        loop.add_signal_handler(signal.SIGINT,  stop_event.set)

        await stop_event.wait()
        await self._shutdown(server)

    def _get_status(self) -> dict:
        return {
            "recording":      True,
            "backend":        self.recorder.name,
            "clips":          self._clip_count,
            "session_active": self._session_active,
        }

    async def _shutdown(self, server) -> None:
        click.echo("\n[Vice] Shutting down…")
        if self.share:
            await self.share.broadcast({"type": "status", "recording": False, "backend": ""})
        server.close()
        await self.recorder.stop()
        await self.hotkeys.stop()
        if self.share:
            await self.share.stop()
        for p in (PID_FILE, SOCKET_FILE):
            if p.exists():
                p.unlink()
        click.echo("[Vice] Stopped.")

    async def _handle_clip_hotkey(self) -> None:
        if self._session_active:
            # During a session, single tap = add a highlight at current timestamp
            elapsed = self.recorder.session_elapsed()
            label   = f"Highlight {len(self._session_highlights) + 1}" if self._session_highlights else "Highlight"
            color   = "#f59e0b"
            entry   = {"time": round(elapsed, 3), "label": label, "color": color}
            self._session_highlights.append(entry)
            click.echo(f"[Vice] Session highlight at {elapsed:.1f}s", err=True)
            audio.play_highlight()
            if self.share:
                asyncio.create_task(
                    self.share.broadcast({
                        "type": "session_highlight",
                        "time": entry["time"],
                        "label": entry["label"],
                        "color": entry["color"],
                    })
                )
        else:
            async with self._clip_lock:
                click.echo("[Vice] Clip triggered!", err=True)
                audio.play_clip()
                await self.recorder.save_clip()

    async def _handle_session_toggle(self) -> None:
        if self._session_active:
            await self._stop_session()
        else:
            await self._start_session()

    async def _start_session(self) -> None:
        click.echo("[Vice] Starting session recording…", err=True)
        self._session_highlights = []
        path = await self.recorder.start_session()
        if path is None:
            click.echo("[Vice] Session recording failed to start", err=True)
            return
        self._session_active = True
        self._session_path   = path
        audio.play_session_start()
        click.echo(f"[Vice] Session recording started → {path}", err=True)
        if self.share:
            asyncio.create_task(
                self.share.broadcast({
                    "type": "session_start",
                    "path": str(path),
                })
            )

    async def _stop_session(self) -> None:
        click.echo("[Vice] Stopping session recording…", err=True)
        self._session_active = False
        slug_before_stop = self._session_path.stem if self._session_path else None
        path = await self.recorder.stop_session()
        self._session_path = None

        audio.play_session_end()
        if path and self.share:
            slug = path.stem
            url  = self.share.add_clip(path)
            click.echo(f"[Vice] Session clip saved: {path}", err=True)
            click.echo(f"[Vice] Share URL: {url}", err=True)
            # Persist the highlights that were collected during the session
            if self._session_highlights:
                from .share import HIGHLIGHTS_DIR, _save_highlights
                HIGHLIGHTS_DIR.mkdir(parents=True, exist_ok=True)
                # Assign IDs
                hl_with_ids = [
                    {**h, "id": str(i + 1)}
                    for i, h in enumerate(self._session_highlights)
                ]
                _save_highlights(slug, hl_with_ids)
                click.echo(
                    f"[Vice] {len(hl_with_ids)} highlight(s) saved for {slug}", err=True
                )
            self._session_highlights = []

        if self.share:
            asyncio.create_task(
                self.share.broadcast({
                    "type": "session_stop",
                })
            )

    async def _handle_ipc(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            cmd = raw.decode().strip()
            if cmd == "clip":
                asyncio.create_task(self._handle_clip_hotkey())
                writer.write(b"ok\n")
            elif cmd == "stop":
                writer.write(b"ok\n")
                await writer.drain()
                os.kill(os.getpid(), signal.SIGTERM)
            elif cmd == "status":
                writer.write(json.dumps({
                    "running":        True,
                    "backend":        self.recorder.name,
                    "clips":          self._clip_count,
                    "output":         self.cfg.output.directory,
                    "share_url":      self.share.base_url() if self.share else None,
                    "session_active": self._session_active,
                }).encode() + b"\n")
            elif cmd == "url":
                url = self.share.base_url() if self.share else ""
                writer.write((url or "").encode() + b"\n")
            else:
                writer.write(b"unknown command\n")
            await writer.drain()
        except Exception as exc:
            log.debug("IPC error: %s", exc)
        finally:
            writer.close()


# ──────────────────────────────────────────────────────────────────────────────
# IPC client
# ──────────────────────────────────────────────────────────────────────────────

async def _ipc(command: str, timeout: float = 5.0) -> Optional[str]:
    if not SOCKET_FILE.exists():
        return None
    try:
        reader, writer = await asyncio.open_unix_connection(str(SOCKET_FILE))
        writer.write(command.encode() + b"\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.readline(), timeout=timeout)
        writer.close()
        return response.decode().strip()
    except Exception as exc:
        log.debug("IPC failed: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="vice")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Vice — Linux game clip recorder (Medal.tv for Linux)."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command()
@click.option("--debug", is_flag=True, help="Enable verbose logging.")
@click.option("--open-ui/--no-open-ui", default=True,
              help="Open the web UI in the browser on start.")
def start(debug: bool, open_ui: bool) -> None:
    """Start the Vice recording daemon."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if SOCKET_FILE.exists():
        click.echo("Vice is already running. Use `vice stop` or `vice status`.", err=True)
        sys.exit(1)

    daemon = ViceDaemon()

    if open_ui and daemon.cfg.sharing.enabled:
        port = daemon.cfg.sharing.port
        from threading import Timer
        def _open():
            subprocess.Popen(
                ["xdg-open", f"http://localhost:{port}/"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        Timer(1.5, _open).start()

    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass


@cli.command()
def ui() -> None:
    """Open the Vice web UI in your browser."""
    raw = asyncio.run(_ipc("url"))
    if raw and raw.startswith("http"):
        url = raw
    else:
        cfg = load_config()
        url = f"http://localhost:{cfg.sharing.port}/"
        if not raw:
            click.echo("Daemon may not be running — opening default port anyway.")
    subprocess.Popen(
        ["xdg-open", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    click.echo(f"Opening {url}")


@cli.command()
def clip() -> None:
    """Save a clip right now (daemon must be running)."""
    resp = asyncio.run(_ipc("clip"))
    if resp is None:
        click.echo("Vice is not running. Start it with `vice start`.", err=True)
        sys.exit(1)
    click.echo("Clip triggered!")


@cli.command()
def stop() -> None:
    """Stop the Vice daemon."""
    resp = asyncio.run(_ipc("stop"))
    if resp is None:
        click.echo("Vice is not running.", err=True)
        sys.exit(1)
    click.echo("Stopped.")


@cli.command()
def status() -> None:
    """Show daemon status."""
    raw = asyncio.run(_ipc("status"))
    if raw is None:
        click.echo("Vice is not running.")
        return
    try:
        info = json.loads(raw)
        click.echo(f"Status   : {'running' if info['running'] else 'stopped'}")
        click.echo(f"Backend  : {info['backend']}")
        click.echo(f"Clips    : {info['clips']}")
        click.echo(f"Output   : {info['output']}")
        if info.get("share_url"):
            click.echo(f"UI URL   : {info['share_url']}/")
    except Exception:
        click.echo(raw)


@cli.command("config")
def show_config() -> None:
    """Print the config file path and its contents."""
    click.echo(f"Config: {CONFIG_PATH}\n")
    if CONFIG_PATH.exists():
        click.echo(CONFIG_PATH.read_text())
    else:
        click.echo("(no config file yet — will be created on first `vice start`)")


@cli.command("open-config")
def open_config() -> None:
    """Open the config file in $EDITOR."""
    if not CONFIG_PATH.exists():
        from .config import Config
        save_config(Config())
        click.echo(f"Created default config at {CONFIG_PATH}")
    editor = os.environ.get("EDITOR", "nano")
    os.execlp(editor, editor, str(CONFIG_PATH))


@cli.command("list-keys")
@click.option("--filter", "filt", default="", help="Filter by substring.")
def list_keys(filt: str) -> None:
    """List available hotkey names for use in config."""
    keys = list_available_keys()
    if filt:
        keys = [k for k in keys if filt.upper() in k]
    for k in keys:
        click.echo(k)


@cli.command()
def clips() -> None:
    """List saved clips in the output directory."""
    cfg = load_config()
    out_dir = Path(cfg.output.directory)
    if not out_dir.exists():
        click.echo("No clips directory found.")
        return
    files = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        click.echo("No clips saved yet.")
        return
    for f in files:
        size_mb = f.stat().st_size / (1024 * 1024)
        click.echo(f"{f.name}  ({size_mb:.1f} MB)")


@cli.command()
@click.option("--yes", "-y", is_flag=True, help="Skip all confirmation prompts.")
def uninstall(yes: bool) -> None:
    """Remove Vice cleanly — config, service, and optionally clips."""
    click.echo("Vice uninstaller\n")

    # 1. Stop daemon
    if SOCKET_FILE.exists():
        click.echo("Stopping daemon…")
        asyncio.run(_ipc("stop"))

    # 2. Disable systemd user service
    service = Path.home() / ".config" / "systemd" / "user" / "vice.service"
    if service.exists():
        if yes or click.confirm("Disable and remove the systemd user service?", default=True):
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", "vice"],
                capture_output=True,
            )
            service.unlink()
            click.echo("  Removed systemd service.")

    # 3. Remove config
    if CONFIG_DIR.exists():
        if yes or click.confirm(f"Remove config directory {CONFIG_DIR}?", default=False):
            shutil.rmtree(CONFIG_DIR)
            click.echo(f"  Removed {CONFIG_DIR}.")

    # 4. Offer to remove clips
    try:
        cfg = load_config() if CONFIG_PATH.exists() else None
        clips_dir = Path(cfg.output.directory) if cfg else Path.home() / "Videos" / "Vice"
    except Exception:
        clips_dir = Path.home() / "Videos" / "Vice"

    if clips_dir.exists():
        n = len(list(clips_dir.glob("*.mp4")))
        if n > 0 and (yes or click.confirm(
            f"Delete {n} saved clip(s) in {clips_dir}?", default=False
        )):
            shutil.rmtree(clips_dir)
            click.echo(f"  Deleted {n} clip(s).")

    # 5. Uninstall the Python package
    click.echo("\nUninstalling Python package…")
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "vice", "-y"])

    click.echo("\nVice has been removed. Goodbye!")
