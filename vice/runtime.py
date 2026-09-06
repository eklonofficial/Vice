"""Runtime helpers for robust daemon startup under launchers and user services."""

from __future__ import annotations

import logging
import fcntl
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import time
from pathlib import Path

log = logging.getLogger("vice.runtime")
RUNTIME_ENV_KEYS = (
    "HOME",
    "XDG_RUNTIME_DIR",
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
)


def daemon_is_running(socket_file: Path, pid_file: Path) -> bool:
    """Conservatively detect an owner, including a busy or stopping daemon."""
    try:
        pid = int(pid_file.read_text().strip())
    except (FileNotFoundError, ValueError):
        pid = 0
    except OSError as exc:
        log.warning("Cannot inspect daemon PID file %s: %s", pid_file, exc)
        return True
    if pid > 0:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            pass
        except PermissionError:
            return True

    # A status timeout says nothing about ownership. A listening Unix socket
    # can accept connections while its daemon is busy finalizing a clip.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(str(socket_file))
        except (FileNotFoundError, ConnectionRefusedError):
            return False
        except OSError as exc:
            log.debug("Cannot rule out a daemon at %s: %s", socket_file, exc)
        return True


def claim_daemon_lock(socket_file: Path, timeout: float = 0.0):
    """Hold the returned file open for the daemon's whole lifetime."""
    lock_file = socket_file.with_suffix(".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("a+")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Vice is already starting or running.") from exc
                time.sleep(0.05)
    except BaseException:
        handle.close()
        raise


def actual_home_dir() -> Path:
    """Return the current user's real home directory without trusting $HOME."""
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path(os.path.expanduser("~"))


def _needs_shell_expansion(value: str | None) -> bool:
    if not value:
        return True
    return "${" in value or "$(" in value


def runtime_env_snapshot() -> dict[str, str]:
    return {key: os.environ.get(key, "") for key in RUNTIME_ENV_KEYS}


def user_systemd_env_snapshot() -> dict[str, str]:
    """Return relevant graphical-session vars exported by the user systemd manager."""
    if shutil.which("systemctl") is None:
        return {}

    try:
        out = subprocess.check_output(
            ["systemctl", "--user", "show-environment"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except Exception:
        return {}

    values: dict[str, str] = {}
    wanted = set(RUNTIME_ENV_KEYS) - {"HOME"}
    for line in out.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in wanted and value:
            values[key] = value
    return values


def load_user_systemd_env() -> None:
    """Hydrate graphical session vars from the user systemd manager when needed."""
    for key, value in user_systemd_env_snapshot().items():
        if not os.environ.get(key) or _needs_shell_expansion(os.environ.get(key)):
            os.environ[key] = value


def has_display() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def running_under_systemd() -> bool:
    """Whether this process was started by systemd, which sets both of these
    for its services. Running from a terminal must never pay the session
    wait."""
    return bool(os.environ.get("INVOCATION_ID") or os.environ.get("JOURNAL_STREAM"))


def wait_for_display(timeout: float = 60.0, interval: float = 2.0) -> bool:
    """Block until a display shows up in the environment, or give up.

    The service is wanted by default.target so it survives compositors that
    never activate graphical-session.target (#139), and default.target can be
    reached before the compositor has exported anything. Returns whether a
    display was found; the caller carries on either way.
    """
    if has_display():
        return True
    deadline = time.monotonic() + timeout
    log.info("No display in the environment yet, waiting up to %.0fs for the session", timeout)
    while time.monotonic() < deadline:
        time.sleep(interval)
        load_user_systemd_env()
        if has_display():
            log.info("Session is up (WAYLAND_DISPLAY=%r DISPLAY=%r)",
                     os.environ.get("WAYLAND_DISPLAY", ""), os.environ.get("DISPLAY", ""))
            return True
    log.warning("No display appeared within %.0fs, starting anyway", timeout)
    return False


def _wayland_runtime_dir_candidates() -> list[Path]:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    candidates: list[Path] = []
    seen: set[Path] = set()

    for raw_path in (
        runtime_dir,
        f"/run/user/{os.getuid()}",
        f"/tmp/wayland-{os.getuid()}",
    ):
        if not raw_path or _needs_shell_expansion(raw_path):
            continue
        candidate = Path(raw_path)
        if candidate in seen:
            continue
        seen.add(candidate)
        candidates.append(candidate)

    return candidates


def recover_wayland_display() -> bool:
    """Recover Wayland env vars from a socket when launchers omit them."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return True

    for runtime_dir in _wayland_runtime_dir_candidates():
        if not runtime_dir.exists():
            continue

        for candidate in sorted(runtime_dir.glob("wayland-*")):
            try:
                mode = candidate.stat().st_mode
            except OSError:
                continue

            if not stat.S_ISSOCK(mode):
                continue

            os.environ["WAYLAND_DISPLAY"] = candidate.name
            os.environ["XDG_RUNTIME_DIR"] = str(runtime_dir)
            log.info(
                "Recovered Wayland display from socket: %s/%s",
                runtime_dir,
                candidate.name,
            )
            return True

    return False


def normalize_runtime_environment() -> None:
    """Repair common broken service env vars before Vice touches config or capture."""
    real_home = str(actual_home_dir())
    runtime_dir = f"/run/user/{os.getuid()}"
    log.debug("Runtime env before normalization: %s", runtime_env_snapshot())

    if _needs_shell_expansion(os.environ.get("HOME")):
        os.environ["HOME"] = real_home

    if _needs_shell_expansion(os.environ.get("XDG_RUNTIME_DIR")):
        os.environ["XDG_RUNTIME_DIR"] = runtime_dir

    if (
        not os.environ.get("WAYLAND_DISPLAY")
        and not os.environ.get("DISPLAY")
    ) or _needs_shell_expansion(os.environ.get("XDG_RUNTIME_DIR")):
        load_user_systemd_env()

    if _needs_shell_expansion(os.environ.get("HOME")):
        os.environ["HOME"] = real_home

    if _needs_shell_expansion(os.environ.get("XDG_RUNTIME_DIR")):
        os.environ["XDG_RUNTIME_DIR"] = runtime_dir

    if not os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        recover_wayland_display()

    log.debug("Runtime env after normalization: %s", runtime_env_snapshot())


def resolve_path(path_like: str | Path) -> Path:
    """Expand home-directory placeholders in config-driven filesystem paths."""
    text = os.fspath(path_like)
    home = str(actual_home_dir())

    if text.startswith("~"):
        text = text.replace("~", home, 1)

    text = text.replace("${HOME}", home).replace("$HOME", home)
    text = os.path.expandvars(text)
    return Path(text)


def systemd_unit_loaded(unit: str = "vice.service") -> bool:
    """Whether this user's systemd has the unit loaded.

    Everything here is a probe: any failure means "no systemd", and every
    caller treats that as "do nothing" rather than as an error.
    """
    if not os.environ.get("XDG_RUNTIME_DIR"):
        return False
    if not shutil.which("systemctl"):
        return False
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p", "LoadState", "--value"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("systemctl probe failed: %s", exc)
        return False
    return out.stdout.strip() == "loaded"


def installed_version() -> str | None:
    """The version sitting on disk right now, which is not necessarily the one
    this process is running.

    Read out of the file rather than imported: the module is already in
    memory, and Python cannot reload it. Parsing beats importing here because
    a half-written file during an upgrade must not execute.

    Returns None when it cannot be read, and every caller treats that as "no
    opinion" so a failed read can never change behaviour.
    """
    try:
        source = (Path(__file__).resolve().parent / "__init__.py").read_text(encoding="utf-8")
    except OSError as exc:
        log.debug("Could not read the installed version: %s", exc)
        return None
    match = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", source, re.MULTILINE)
    if not match:
        log.debug("No __version__ found in the installed package")
        return None
    return match.group(1)
