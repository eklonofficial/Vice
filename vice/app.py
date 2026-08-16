"""
Vice desktop app: opens the web UI in a native pywebview window.

Launched via `vice-app` (app icon, launcher, or command line).

Behaviour:
  • Starts the Vice daemon subprocess if it isn't already running.
  • Waits for the HTTP server to be ready, then opens a native window.
  • Exposes a JS API so the UI can call vice.quit() to stop the daemon
    and close the window cleanly.
  • Closing the window without vice.quit() keeps recording running.
  • Sending SIGTERM to vice-app (for example: killall vice-app) now
    forwards a clean stop request to the daemon before exit.
  • Re-launching vice-app when the daemon is already running just opens
    a new window connected to the existing session.

Falls back to xdg-open (browser) if pywebview is not installed.
Errors are logged to ~/.local/share/vice/vice-app.log when running
without a terminal (e.g. from the app launcher).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from . import __version__
from .runtime import actual_home_dir, normalize_runtime_environment

SOCKET_FILE = Path("/tmp/vice/vice.sock")
PID_FILE    = Path("/tmp/vice/vice.pid")
APP_LOCK_FILE = Path("/tmp/vice/vice-app.pid")
WINDOW_TITLE = "Vice"
LOG_FILE = actual_home_dir() / ".local" / "share" / "vice" / "vice-app.log"
DEBUG_LOG_FILE = actual_home_dir() / ".local" / "share" / "vice" / "vice-debug.log"
DAEMON_LOG_FILE = actual_home_dir() / ".local" / "share" / "vice" / "vice.log"
DAEMON_STDERR_LOG_FILE = actual_home_dir() / ".local" / "share" / "vice" / "vice-daemon-stderr.log"
# vice-app's own stderr (Qt/Chromium messages), captured by the compositor
# watcher so launcher-context failures are diagnosable. Truncated per launch.
APP_STDERR_LOG_FILE = actual_home_dir() / ".local" / "share" / "vice" / "vice-app-stderr.log"

DEBUG_MODE = False  # toggled by main() when --debug is on the command line.


# ── logging ───────────────────────────────────────────────────────────────────

def _setup_logging(debug: bool = False) -> None:
    """Log to file when stdout is not a TTY (i.e. launched from app menu).

    In debug mode: add a second verbose file handler at ~/.local/share/vice/
    vice-debug.log, capturing DEBUG-level logs from every logger, including
    JS bridge calls and the clipboard subprocess trace.
    """
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(LOG_FILE),
    ]
    if sys.stdout.isatty() or debug:
        handlers.append(logging.StreamHandler(sys.stderr))
    if debug:
        dbg = logging.FileHandler(DEBUG_LOG_FILE, mode="w")  # truncate each run
        dbg.setLevel(logging.DEBUG)
        dbg.setFormatter(logging.Formatter(
            "%(asctime)s [%(threadName)s] %(levelname)s %(name)s "
            "%(filename)s:%(lineno)d | %(message)s"
        ))
        handlers.append(dbg)
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [vice-app] %(levelname)s: %(message)s",
        handlers=handlers,
    )


log = logging.getLogger("vice-app")


def _handle_app_terminate(signum: int, _frame) -> None:
    """Stop daemon when vice-app is terminated externally."""
    log.info("Received signal %s, stopping daemon before exit", signum)
    try:
        _stop_daemon()
    finally:
        raise SystemExit(0)


# ── helpers ───────────────────────────────────────────────────────────────────

def _vice_cmd() -> list[str]:
    """Return the command to run the vice daemon.

    Tries (in order):
      1. Absolute ~/.local/bin/vice  (covers both pip-user and venv symlink)
      2. shutil.which("vice")        (works if PATH is set correctly)
      3. sys.executable -m vice.main (fallback using same Python as vice-app)
    """
    user_bin = actual_home_dir() / ".local" / "bin" / "vice"
    if user_bin.exists():
        return [str(user_bin)]
    found = shutil.which("vice")
    if found:
        return [found]
    # Last resort: run as a module with the same Python interpreter
    return [sys.executable, "-m", "vice.main"]


def _daemon_responds(timeout: float = 1.0) -> bool:
    """Return True when the Unix socket accepts an IPC request."""
    return _daemon_status(timeout=timeout) is not None


def _daemon_status(timeout: float = 1.0) -> dict | None:
    """Return daemon IPC status JSON, or None when the socket is unusable."""
    if not SOCKET_FILE.exists():
        return None

    async def _probe() -> dict | None:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(SOCKET_FILE)),
                timeout=timeout,
            )
            writer.write(b"status\n")
            await writer.drain()
            resp = await asyncio.wait_for(reader.readline(), timeout=timeout)
            writer.close()
            await writer.wait_closed()
            if not resp:
                return None
            import json
            return json.loads(resp)
        except Exception:
            return None

    return asyncio.run(_probe())


def _start_daemon() -> None:
    """Launch the daemon as a detached background process (no-op if running)."""
    normalize_runtime_environment()

    if SOCKET_FILE.exists():
        if _daemon_responds():
            log.info("Daemon already running (socket is responsive)")
            return
        log.warning("Found stale daemon socket at %s; removing it", SOCKET_FILE)
        try:
            SOCKET_FILE.unlink()
        except OSError as exc:
            log.error("Could not remove stale socket %s: %s", SOCKET_FILE, exc)
            raise
    cmd = _vice_cmd() + ["start", "--no-open-ui"]
    log.info("Starting daemon: %s", " ".join(cmd))
    # Route the daemon's stdout/stderr to a file so import-time crashes (which
    # happen before the daemon's logging is initialised, leaving vice.log empty)
    # are still recoverable for the launch error dialog. Truncated each launch
    # so the file always reflects the most recent attempt.
    DAEMON_STDERR_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    stderr_fd = open(DAEMON_STDERR_LOG_FILE, "w")
    try:
        subprocess.Popen(
            cmd,
            env=os.environ.copy(),
            stdout=stderr_fd,
            stderr=stderr_fd,
            start_new_session=True,   # detach from our process group
        )
    except Exception as exc:
        log.error("Failed to start daemon: %s", exc)
        raise
    finally:
        stderr_fd.close()  # parent's copy; child has its own dup'd fd


def _stop_daemon() -> None:
    """Ask the daemon to shut down via IPC."""
    if not SOCKET_FILE.exists():
        return
    try:
        async def _send():
            reader, writer = await asyncio.open_unix_connection(str(SOCKET_FILE))
            writer.write(b"stop\n")
            await writer.drain()
            writer.close()
        asyncio.run(_send())
    except Exception as exc:
        log.debug("Stop IPC error: %s", exc)


def _wait_for_daemon_exit(timeout: float = 10.0) -> bool:
    """Wait for the running daemon to fully exit. Returns True if it did.

    Cloudflared tunnel teardown can take several seconds, so we poll the
    PID file (cleaned up only after full shutdown) plus the IPC socket.
    Force-kills the process via SIGKILL if it doesn't exit by `timeout`.
    """
    deadline = time.monotonic() + timeout
    pid: int | None = None
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        pid = None

    while time.monotonic() < deadline:
        # Daemon writes its PID at startup and unlinks PID_FILE + SOCKET_FILE on exit.
        if not PID_FILE.exists() and not SOCKET_FILE.exists():
            return True
        if pid is not None:
            try:
                os.kill(pid, 0)  # signal 0 = "is process alive?"
            except ProcessLookupError:
                # Process is gone; let any final socket cleanup happen, then succeed.
                time.sleep(0.05)
                return True
            except PermissionError:
                pass  # alive but we can't signal it
        time.sleep(0.1)

    if pid is not None:
        log.warning("Daemon (pid=%s) did not exit in %.1fs, sending SIGKILL", pid, timeout)
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError) as exc:
            log.warning("SIGKILL on pid=%s failed: %s", pid, exc)
        # Best-effort: give the kernel a moment, then clean lingering files.
        time.sleep(0.3)
    for path in (PID_FILE, SOCKET_FILE):
        path.unlink(missing_ok=True)
    return True


def _wait_for_server(url: str, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1) as resp:
                status = getattr(resp, "status", 200)
                if 200 <= status < 400:
                    return True
        except HTTPError as exc:
            if 200 <= exc.code < 400:
                return True
            log.debug("Server probe failed for %s with HTTP %s", url, exc.code)
            time.sleep(0.25)
        except URLError:
            time.sleep(0.25)
        except Exception:
            time.sleep(0.25)
    return False


def _status_is_ready(status: dict | None) -> bool:
    return bool(status and status.get("ready") is True)


def _wait_for_ready_server(default_url: str, timeout: float = 20.0) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _daemon_status(timeout=0.5)
        url = _server_url_from_status(status, default_url)
        if _status_is_ready(status) and _wait_for_server(url, timeout=1.0):
            return url
        time.sleep(0.25)
    return None


def _server_url_from_status(status: dict | None, fallback_url: str) -> str:
    raw = (status or {}).get("local_url")
    if not raw or not isinstance(raw, str):
        return fallback_url
    return raw.rstrip("/") + "/"


def _tail_text_file(path: Path, lines: int = 20) -> str:
    try:
        content = path.read_text(errors="replace").splitlines()
    except Exception:
        return ""
    if not content:
        return ""
    return "\n".join(content[-lines:])


# Known daemon crash signatures mapped to something a user can act on. The
# raw log tail is still shown underneath; this just says what it means.
_STARTUP_DIAGNOSES: tuple[tuple[str, str], ...] = (
    (
        "failed to load opengl",
        "gpu-screen-recorder could not create an OpenGL context.\n"
        "That usually means the GPU has no supported hardware video encoder "
        "(Apple Silicon is not supported), the Mesa or GPU drivers are missing, "
        "or the daemon is running outside your graphical session.\n"
        "Run 'vice doctor', then try 'gpu-screen-recorder -w screen -o /tmp/test.mp4' "
        "by hand to see the raw error.",
    ),
    (
        "no encoder found",
        "gpu-screen-recorder found no usable hardware video encoder.\n"
        "Vice needs NVENC (NVIDIA) or VA-API (AMD/Intel). Check that the GPU "
        "driver and its VA-API package are installed, then run 'vice doctor'.",
    ),
)


def _diagnose_startup_failure(log_text: str) -> str | None:
    """Plain-English cause for a known crash signature in the daemon log."""
    haystack = log_text.lower()
    for signature, explanation in _STARTUP_DIAGNOSES:
        if signature in haystack:
            return explanation
    return None


def _startup_failure_detail(url: str) -> str:
    status = _daemon_status(timeout=0.5)
    lines: list[str] = []

    if status is None:
        lines.append("Daemon IPC socket did not become ready.")
    else:
        server_url = _server_url_from_status(status, url)
        lines.append(f"Daemon IPC responded but HTTP UI is unavailable at {server_url}")

    daemon_tail = _tail_text_file(DAEMON_LOG_FILE, lines=20)
    if daemon_tail:
        lines.append(f"Recent daemon log:\n{daemon_tail}")
    else:
        lines.append(f"No daemon log output was found at {DAEMON_LOG_FILE}")

    # vice.log is only populated after the daemon's logging.basicConfig runs.
    # If the daemon crashed during Python import (missing module, syntax error,
    # etc.) the formatted log will be empty, fall back to raw stderr so the
    # dialog still surfaces the actual traceback.
    if not daemon_tail:
        try:
            stderr_text = DAEMON_STDERR_LOG_FILE.read_text(errors="replace").strip()
            if stderr_text:
                tail = "\n".join(stderr_text.splitlines()[-40:])
                lines.append(f"Daemon stderr (pre-logging crash):\n{tail}")
        except FileNotFoundError:
            pass

    diagnosis = _diagnose_startup_failure("\n".join(lines))
    if diagnosis:
        lines.insert(0, diagnosis)

    return "\n\n".join(lines)


def _clear_stale_socket() -> None:
    if not SOCKET_FILE.exists():
        return
    if _daemon_responds():
        return
    log.warning("Removing stale daemon socket at %s", SOCKET_FILE)
    SOCKET_FILE.unlink(missing_ok=True)


def _ensure_server(default_url: str, startup_timeout: float = 20.0) -> str | None:
    status = _daemon_status()
    if status is not None:
        url = _server_url_from_status(status, default_url)
        if _wait_for_server(url, timeout=2.0):
            # Self-heal package upgrades: a daemon launched before the upgrade
            # has stale Python code in memory (Python can't hot-reload), so
            # serving the new HTML through the old route table breaks the UI.
            daemon_version = (status or {}).get("version")
            if daemon_version and daemon_version != __version__:
                log.warning(
                    "Running daemon is v%s but this launcher is v%s, restarting daemon to pick up upgraded code",
                    daemon_version, __version__,
                )
                _stop_daemon()
                _wait_for_daemon_exit(timeout=10.0)
                _clear_stale_socket()
                # Fall through to _start_daemon() below.
            else:
                ready_url = _wait_for_ready_server(url, timeout=startup_timeout)
                if ready_url:
                    log.info("Daemon already running (IPC + HTTP healthy)")
                    return ready_url
                log.warning("Daemon HTTP responded but recorder did not become ready; restarting daemon")
                _stop_daemon()
                _wait_for_daemon_exit(timeout=10.0)
                _clear_stale_socket()
        else:
            log.warning("Daemon IPC responded but UI server did not (%s); restarting daemon", url)
            _stop_daemon()
            _wait_for_daemon_exit(timeout=10.0)
            _clear_stale_socket()
    else:
        _clear_stale_socket()

    _start_daemon()

    ready_url = _wait_for_ready_server(default_url, timeout=startup_timeout)
    if ready_url:
        return ready_url

    status = _daemon_status()
    url = _server_url_from_status(status, default_url)
    if url != default_url:
        ready_url = _wait_for_ready_server(url, timeout=2.0)
        if ready_url:
            return ready_url

    if status is not None:
        log.error("Daemon IPC is alive but HTTP UI is unavailable at %s", url)
    return None


# ── entry point ───────────────────────────────────────────────────────────────

_app_lock_handle = None


def _claim_app_lock() -> bool:
    """Take the single-window lock, or report that another window has it.

    Two Vice windows drive the same daemon over the same WebSocket, so
    settings saved in one silently disagree with what the other is showing
    and both run the effects probe against each other (#121). The lock is an
    flock rather than a pid comparison so it cannot be left behind by a
    crash: the kernel drops it when the process dies.
    """
    global _app_lock_handle
    try:
        APP_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        handle = open(APP_LOCK_FILE, "a+")
    except OSError as exc:
        # Never let a lock problem stop the window opening. A duplicate
        # window is annoying; no window at all is a broken application.
        log.warning("Could not open the single-window lock %s: %s", APP_LOCK_FILE, exc)
        return True
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    except Exception as exc:
        log.warning("Could not take the single-window lock: %s", exc)
        handle.close()
        return True
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
    except OSError as exc:
        log.debug("Could not record the pid in %s: %s", APP_LOCK_FILE, exc)
    _app_lock_handle = handle  # held open for the life of the process
    return True


def _raise_existing_window() -> bool:
    """Bring the window that already exists to the front."""
    if not shutil.which("wmctrl"):
        log.info("wmctrl is not installed, so the existing Vice window cannot be raised")
        return False
    try:
        result = subprocess.run(
            ["wmctrl", "-a", WINDOW_TITLE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("Could not raise the existing Vice window: %s", exc)
        return False
    return result.returncode == 0


def main() -> None:
    global DEBUG_MODE
    debug = "--debug" in sys.argv[1:]
    DEBUG_MODE = debug

    normalize_runtime_environment()
    _setup_logging(debug=debug)
    signal.signal(signal.SIGTERM, _handle_app_terminate)
    signal.signal(signal.SIGINT, _handle_app_terminate)
    log.info("vice-app starting (python=%s, debug=%s)", sys.executable, debug)

    if not _claim_app_lock():
        log.info("A Vice window is already open, raising it instead of opening another")
        if not _raise_existing_window():
            log.info("Could not raise it, so nothing to do here")
        sys.exit(0)

    try:
        from .config import load as load_config
        cfg  = load_config()
        port = cfg.sharing.port
    except Exception as exc:
        log.error("Failed to load config: %s", exc)
        port = 8765

    url = f"http://127.0.0.1:{port}/"

    try:
        server_url = _ensure_server(url)
    except Exception:
        # Error already logged; show a user-visible message and exit.
        detail = _startup_failure_detail(url)
        log.error("Startup diagnostics:\n%s", detail)
        _show_error(
            "Vice could not start the recording daemon.\n\n"
            f"{detail}\n\n"
            f"Check the log for details:\n{LOG_FILE}"
        )
        sys.exit(1)

    log.info("Waiting for server at %s", url)
    if not server_url:
        log.error("Server did not start within 20 s")
        detail = _startup_failure_detail(url)
        log.error("Startup diagnostics:\n%s", detail)
        _show_error(
            "Vice started but the UI server did not respond.\n\n"
            f"{detail}\n\n"
            f"Check the log for details:\n{LOG_FILE}"
        )
        sys.exit(1)

    log.info("Server ready at %s, opening window", server_url)
    try:
        import webview  # type: ignore[import]
        _run_webview(server_url)
        log.info("Window closed")
    except ImportError:
        log.warning("pywebview not installed, falling back to browser")
        subprocess.Popen(
            ["xdg-open", server_url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.error("pywebview crashed: %s", exc, exc_info=True)
        # Fall back to browser so the user isn't left with nothing
        log.warning("Falling back to browser")
        subprocess.Popen(
            ["xdg-open", server_url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _show_error(message: str) -> None:
    """Show a visible error: a GTK dialog if possible, otherwise print."""
    log.error("UI error: %s", message)
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
        diag = Gtk.MessageDialog(
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text="Vice Error",
            secondary_text=message,
        )
        diag.run()
        diag.destroy()
    except Exception:
        print(f"[vice-app] ERROR: {message}", file=sys.stderr)


# ── pywebview window ──────────────────────────────────────────────────────────

def _is_nvidia() -> bool:
    return Path("/proc/driver/nvidia/version").exists()


# Driver series from which Chromium's Vulkan path is dependable. Below this,
# Vulkan rendering segfaulted on some builds (#82) and GL is used instead.
_VULKAN_MIN_DRIVER = 550


def _nvidia_driver_major() -> "int | None":
    """Major version of the loaded NVIDIA driver, or None when unreadable."""
    try:
        text = Path("/proc/driver/nvidia/version").read_text()
    except OSError:
        return None
    match = re.search(r"(\d+)\.\d+", text)
    return int(match.group(1)) if match else None


def _hardware_video_decode_enabled() -> bool:
    """ui.hardware_video_decode, read defensively: this runs before anything
    else is up, and a broken config must not stop the window from opening."""
    try:
        from .config import load as load_config
        return bool(load_config().ui.hardware_video_decode)
    except Exception as exc:
        log.debug("Could not read ui.hardware_video_decode: %s", exc)
        return False


def _prepare_webview_environment() -> None:
    """Set environment for a stable QtWebEngine (Chromium) session.

    Must run before QtWebEngine initialises, Chromium reads
    QTWEBENGINE_CHROMIUM_FLAGS once at startup.

    Why each setting is needed:
      • QT_QPA_PLATFORM=xcb (NVIDIA + Wayland): Chromium's native-Wayland
        GBM path is flaky on NVIDIA, the same machine can accept GBM on
        one launch and reject it on the next, producing a black window.
        The XWayland GL path is stable. Set VICE_WEBVIEW_PLATFORM to
        override (e.g. "wayland").
      • --disable-accelerated-video-decode / --disable-gpu-memory-buffer-
        video-frames: Chromium's hardware video decode is broken on many
        Linux GPU/driver combos and renders <video> as a black or grey
        rectangle while the rest of the UI works. Clips are short, local
        files; software decode is cheap and always correct. High-resolution
        AV1 and HEVC are the exception, the CPU cannot keep up and the
        preview stutters (#140), so ui.hardware_video_decode drops both
        flags for machines where the GPU path does work.
      • --autoplay-policy: clip previews start without a click.
      • --disable-features=Vulkan (NVIDIA below driver 550): QtWebEngine
        cannot initialise GBM on NVIDIA and falls back to Vulkan, which
        segfaulted on the driver series current at #82. Blocking it on
        every NVIDIA machine removed the only working GPU path on modern
        drivers and forced software compositing, so the block now applies
        only to the old series.
      • --disable-gpu-compositing (only with VICE_WEBVIEW_SOFTWARE=1):
        the last-resort mode for the current run. When Chromium has no
        compositing path (black window + "dma_buf acquisition failure"
        spam), _watch_for_compositor_failure relaunches the app once with
        this set. The failure is intermittent, so nothing is persisted,
        every fresh launch tries the GPU first.

    QtWebEngine can also refuse GPU compositing without reporting it at
    all: no error, no black window, it just adds --disable-gpu-compositing
    to its own renderers and draws every frame on the CPU. On an NVIDIA
    RTX 4060 with driver 610.43.02 and Qt 6.11 that is the only outcome
    available, and a bare QWebEngineView reproduces it with no pywebview
    and no Vice flags involved. Measured as making no difference there:
    --enable-features=Vulkan, --use-angle=gl, --use-angle=vulkan,
    --ignore-gpu-blocklist, --render-node-override, QT_XCB_GL_INTEGRATION,
    QSG_RHI_BACKEND, and the native Wayland Qt platform (--use-gl=desktop
    just segfaults). Electron apps on the same machine do composite on the
    GPU, so this is QtWebEngine's integration rather than the driver.
    Nothing here can fix it, so the UI measures its own frame rate instead
    and turns the expensive effects down when they are not affordable
    (see ui/scripts/perf.js).

    Users can replace the flags by setting QTWEBENGINE_CHROMIUM_FLAGS
    themselves, or append extra flags via VICE_WEBVIEW_FLAGS.
    """
    # Qt requires a UTF-8 locale; a "C"/POSIX locale makes it switch with
    # loud warnings and has preceded renderer crashes (systemd services
    # often start with no locale at all).
    locale_value = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    if locale_value in ("", "C", "POSIX"):
        os.environ["LC_ALL"] = "C.UTF-8"
        os.environ["LANG"] = "C.UTF-8"

    # When stderr is not a TTY (app-launcher starts), Qt sends its log
    # messages to journald instead of fd 2, which blinded the compositor
    # watcher exactly and only for launcher runs (the black-window failure
    # self-healed from a terminal but not from the app menu). Force Qt to
    # always log to stderr so detection works in every launch context.
    os.environ.setdefault("QT_LOGGING_TO_CONSOLE", "1")

    # Leftover from v1.2.2, which pinned software compositing here.
    (actual_home_dir() / ".local" / "share" / "vice" / "webview-state.json").unlink(missing_ok=True)

    if _is_nvidia() and os.environ.get("WAYLAND_DISPLAY"):
        platform = os.environ.get("VICE_WEBVIEW_PLATFORM", "xcb")
        os.environ["QT_QPA_PLATFORM"] = platform
        log.info("NVIDIA on Wayland, using Qt platform %r for the window", platform)

    if "QTWEBENGINE_CHROMIUM_FLAGS" in os.environ:
        return  # user override, leave it alone
    flags = ["--autoplay-policy=no-user-gesture-required"]
    if _hardware_video_decode_enabled():
        log.info("Hardware video decode enabled by config, clip previews may render black")
    else:
        flags += [
            "--disable-accelerated-video-decode",
            "--disable-gpu-memory-buffer-video-frames",
        ]
    if _is_nvidia():
        major = _nvidia_driver_major()
        if major is not None and major < _VULKAN_MIN_DRIVER:
            log.info("NVIDIA driver %d predates dependable Vulkan, blocking it", major)
            flags.append("--disable-features=Vulkan")
        if os.environ.get("VICE_WEBVIEW_SOFTWARE") == "1":
            flags.append("--disable-gpu-compositing")
    extra = os.environ.get("VICE_WEBVIEW_FLAGS", "").strip()
    if extra:
        flags.append(extra)
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)
    log.info("QTWEBENGINE_CHROMIUM_FLAGS=%s", os.environ["QTWEBENGINE_CHROMIUM_FLAGS"])


# "GBM is not supported with the current configuration. Fallback to Vulkan
# rendering in Chromium." is Chromium announcing a fallback, not a failure,
# QtWebEngine prints it on every NVIDIA start and the window then renders
# fine over Vulkan. Treating it as fatal condemned healthy machines to
# software compositing for the whole run. Only the markers below mean the
# window is actually black, and they repeat for as long as it is.
_COMPOSITOR_FAILURE_IMMEDIATE: tuple[bytes, ...] = ()
_COMPOSITOR_FAILURE_MARKERS = (
    b"Compositor returned null texture",
    b"dma_buf acquisition failure",
)
_COMPOSITOR_FAILURE_THRESHOLD = 8


def _compositor_failure_hit(line: bytes, hits: int) -> tuple[int, bool]:
    """Return (updated_hits, should_relaunch) for one stderr line."""
    if any(marker in line for marker in _COMPOSITOR_FAILURE_IMMEDIATE):
        return hits, True
    if any(marker in line for marker in _COMPOSITOR_FAILURE_MARKERS):
        hits += 1
        return hits, hits >= _COMPOSITOR_FAILURE_THRESHOLD
    return hits, False


def _relaunch_with_software_compositing() -> None:
    log.error(
        "GPU compositing failed on this launch (Chromium rejected GBM), "
        "relaunching with software compositing for this run"
    )
    os.environ["VICE_WEBVIEW_SOFTWARE"] = "1"
    # Drop the flags we set so the relaunched process rebuilds them.
    os.environ.pop("QTWEBENGINE_CHROMIUM_FLAGS", None)
    argv0 = sys.argv[0]
    if Path(argv0).exists() and os.access(argv0, os.X_OK):
        os.execv(argv0, sys.argv)
    os.execv(sys.executable, [sys.executable, "-m", "vice.app"] + sys.argv[1:])


def _watch_for_compositor_failure() -> None:
    """Tee this process's stderr through a pipe and watch for Chromium's
    black-window signature; relaunch in software-compositing mode when it
    appears. Qt/Chromium write these messages to fd 2 (QT_LOGGING_TO_CONSOLE
    is forced so this holds even without a TTY); the process neither
    crashes nor reports the failure through any Qt API. Lines are also
    appended to vice-app-stderr.log so launcher runs stay diagnosable."""
    real_stderr = os.dup(2)
    read_fd, write_fd = os.pipe()
    os.dup2(write_fd, 2)
    os.close(write_fd)

    try:
        APP_STDERR_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        capture = open(APP_STDERR_LOG_FILE, "wb", buffering=0)
    except OSError:
        capture = None

    def _pump() -> None:
        hits = 0
        with os.fdopen(read_fd, "rb") as pipe_reader:
            for line in pipe_reader:
                try:
                    os.write(real_stderr, line)
                except OSError:
                    pass
                if capture is not None:
                    try:
                        capture.write(line)
                    except OSError:
                        pass
                hits, relaunch = _compositor_failure_hit(line, hits)
                if relaunch:
                    _relaunch_with_software_compositing()

    threading.Thread(target=_pump, name="compositor-watch", daemon=True).start()


def _patch_pywebview_qt_permissions() -> None:
    """Work around pywebview 6.x + PyQt6 6.11 enum-vs-int incompatibility.

    pywebview's Qt backend (`webview/platforms/qt.py:304`) calls
    `self.setFeaturePermission(url, feature, 2)` with a raw int for the
    permission policy. PyQt5 accepted ints; PyQt6 6.11 raises TypeError
    and the process SIGABRTs the first time any permission-gated API
    (clipboard, notifications, media-devices probe) is touched. Coerce
    the int to the proper enum so pywebview's callback works.
    """
    try:
        from PyQt6.QtWebEngineCore import QWebEnginePage
    except ImportError:
        return
    orig = QWebEnginePage.setFeaturePermission
    if getattr(orig, "_vice_patched", False):
        return

    def _patched(self, origin, feature, policy):
        if isinstance(policy, int):
            policy = QWebEnginePage.PermissionPolicy(policy)
        if isinstance(feature, int):
            feature = QWebEnginePage.Feature(feature)
        return orig(self, origin, feature, policy)

    _patched._vice_patched = True  # type: ignore[attr-defined]
    QWebEnginePage.setFeaturePermission = _patched


def _run_webview(url: str) -> None:
    _prepare_webview_environment()
    import webview  # type: ignore[import]

    class _API:
        """Methods exposed to JavaScript as window.pywebview.api.*"""

        def __init__(self) -> None:
            self._win: webview.Window | None = None

        def _bind(self, win: "webview.Window") -> None:
            self._win = win

        def quit_app(self) -> None:
            """Stop the daemon and close the window."""
            _stop_daemon()
            if self._win:
                self._win.destroy()

        def keep_running(self) -> None:
            """Close the window but keep the daemon recording."""
            if self._win:
                self._win.destroy()

        def open_url(self, url: str) -> None:
            """Open a URL in the system's default browser via xdg-open."""
            import subprocess as _sp
            try:
                _sp.Popen(
                    ["xdg-open", url],
                    stdout=_sp.DEVNULL,
                    stderr=_sp.DEVNULL,
                )
            except Exception as exc:
                log.debug("Could not open %s in a browser: %s", url, exc)

        def log_debug(self, msg: str) -> None:
            """Forward a debug message from JS into the Python log."""
            try:
                log.debug("js: %s", str(msg)[:500])
            except Exception:
                # Logging the failure to log would be the same failure.
                pass

        def copy_to_clipboard(self, text: str) -> bool:
            """Copy `text` to the system clipboard via wl-copy / xclip / xsel.

            Invoked from JS as window.pywebview.api.copy_to_clipboard(text).
            QtWebEngine's in-page Clipboard API is unreliable (and has been
            seen to crash the render process) on http:// origins, so we bypass
            it here. Every attempt is logged at DEBUG level; enable --debug
            to capture the trace in ~/.local/share/vice/vice-debug.log.
            """
            import subprocess as _sp
            payload = (text or "").encode("utf-8")
            preview = (text or "")[:80].replace("\n", "\\n")
            log.debug("copy_to_clipboard: len=%d preview=%r", len(text or ""), preview)
            attempts = (["wl-copy"],
                        ["xclip", "-selection", "clipboard"],
                        ["xsel", "--clipboard", "--input"])
            for cmd in attempts:
                p = None
                try:
                    p = _sp.Popen(cmd, stdin=_sp.PIPE,
                                  stdout=_sp.DEVNULL, stderr=_sp.PIPE)
                    _, stderr = p.communicate(input=payload, timeout=2.0)
                    log.debug("copy_to_clipboard: %s rc=%s stderr=%r",
                              cmd[0], p.returncode,
                              (stderr or b"").decode(errors="replace")[:200])
                    if p.returncode == 0:
                        return True
                except FileNotFoundError:
                    log.debug("copy_to_clipboard: %s not installed", cmd[0])
                except _sp.TimeoutExpired:
                    log.warning("copy_to_clipboard: %s hung, killing", cmd[0])
                    if p is not None:
                        try: p.kill()
                        except Exception: pass
                except Exception as exc:
                    log.warning("copy_to_clipboard: %s raised %s", cmd[0], exc)
            log.warning("copy_to_clipboard: no backend succeeded")
            return False

    api = _API()

    # Pass ?native=1 so the JS can show the Quit/Minimize pill immediately,
    # pywebview's own window.pywebview is only injected after DOMContentLoaded,
    # which is too late for the initial render.
    sep = "&" if "?" in url else "?"
    native_url = f"{url}{sep}native=1"
    if os.environ.get("VICE_WEBVIEW_SOFTWARE") == "1":
        # The UI drops backdrop blurs and ambient effects in software
        # mode, they are what makes software compositing feel laggy.
        native_url += "&sw=1"
    win = webview.create_window(
        title=WINDOW_TITLE,
        url=native_url,
        js_api=api,
        width=1280,
        height=820,
        min_size=(900, 600),
        background_color="#080b12",
        text_select=False,
        zoomable=False,
    )
    api._bind(win)

    # Pick the fastest available pywebview backend. QtWebEngine (Chromium) is
    # GPU-accelerated and sidesteps WebKit2GTK's software-compositing issues
    # on NVIDIA + Wayland entirely. GTK/WebKit2GTK is the fallback.
    # pywebview's Qt backend imports `qtpy` (a Qt-binding shim) plus the
    # PyQt6 QtWebEngine bindings, both must be present.
    def _enable_gtk_workarounds() -> None:
        # WebKit2GTK + Wayland + NVIDIA crashes with "Error 71 (Protocol error)".
        # XWayland is the safe path. These vars are harmless on other setups.
        os.environ.setdefault("WEBKIT_DISABLE_SANDBOX", "1")
        os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
        os.environ.setdefault("GDK_BACKEND", "x11")

    try:
        import PyQt6.QtWebEngineWidgets  # noqa: F401, probe
        import qtpy                      # noqa: F401, pywebview's Qt shim
        os.environ.setdefault("QT_API", "pyqt6")  # pin qtpy to PyQt6
        _patch_pywebview_qt_permissions()
        gui = "qt"
        log.info("Using QtWebEngine (Chromium) backend")
        if _is_nvidia() and "--disable-gpu-compositing" not in os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", ""):
            _watch_for_compositor_failure()
    except ImportError as exc:
        gui = None  # pywebview's Linux default: GTK/WebKit2GTK
        _enable_gtk_workarounds()
        log.info(
            "Qt backend unavailable (%s), falling back to GTK WebKit on XWayland. "
            "For full GPU acceleration install python-pyqt6-webengine + python-qtpy.",
            exc,
        )

    try:
        webview.start(gui=gui, debug=False, private_mode=False)
    except Exception:
        log.exception("webview.start raised, backend=%s", gui)
        if gui != "qt":
            raise
        # Qt died before opening a window, retry once on GTK/WebKit2GTK
        # so the user still gets a native window instead of nothing.
        log.warning("Retrying with the GTK WebKit backend")
        _enable_gtk_workarounds()
        webview.start(gui=None, debug=False, private_mode=False)
    log.info("Window closed")


if __name__ == "__main__":
    main()
