"""Where Vice keeps things, and how it talks to the desktop, per platform.

Everything that differs between Linux and Windows but is not capture or
hotkeys lives here, so the rest of the code asks one question ("where is the
data directory", "open this folder") instead of branching on the OS itself.

On Linux every function returns exactly what the code used before this module
existed: ~/.config/vice, ~/.local/share/vice, ~/.cache/vice and /tmp/vice. A
change here must never move a Linux user's files.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("vice.platform")

IS_WINDOWS = sys.platform == "win32"


# ── Directories ──────────────────────────────────────────────────────────────

def home_dir() -> Path:
    """The current user's real home directory.

    On Linux $HOME is not trusted, because systemd units and some launchers
    hand it over unexpanded; the password database is. Windows has no such
    problem and no pwd module.
    """
    if not IS_WINDOWS:
        try:
            import pwd
            return Path(pwd.getpwuid(os.getuid()).pw_dir)
        except Exception:
            pass
    return Path(os.path.expanduser("~"))


def _windows_env_dir(var: str, fallback: Path) -> Path:
    value = os.environ.get(var, "").strip()
    return Path(value) if value else fallback


def config_dir() -> Path:
    """config.toml lives here."""
    if IS_WINDOWS:
        return _windows_env_dir("APPDATA", home_dir() / "AppData" / "Roaming") / "Vice"
    return home_dir() / ".config" / "vice"


def data_dir() -> Path:
    """Logs, playlists, view counts and other state worth keeping."""
    if IS_WINDOWS:
        return _windows_env_dir("LOCALAPPDATA", home_dir() / "AppData" / "Local") / "Vice"
    return home_dir() / ".local" / "share" / "vice"


def cache_dir() -> Path:
    """Thumbnails, preview proxies and export scratch space."""
    if IS_WINDOWS:
        return data_dir() / "cache"
    return home_dir() / ".cache" / "vice"


def runtime_dir() -> Path:
    """Pid file, IPC endpoint, capture registry and the segment ring.

    Deliberately not per-user on Linux: /tmp/vice is what every existing
    install, test and packaged unit already expects.
    """
    if IS_WINDOWS:
        return Path(tempfile.gettempdir()) / "vice"
    return Path("/tmp/vice")


def videos_dir() -> Path:
    if IS_WINDOWS:
        from . import win32
        found = win32.known_folder("videos")
        if found:
            return found
    return home_dir() / "Videos"


def pictures_dir() -> Path:
    if IS_WINDOWS:
        from . import win32
        found = win32.known_folder("pictures")
        if found:
            return found
    return home_dir() / "Pictures"


# ── Subprocesses ─────────────────────────────────────────────────────────────

# CREATE_NO_WINDOW. Without it every ffmpeg/ffprobe the daemon starts flashes
# a console window when the daemon itself runs under pythonw.
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008


def no_window_kwargs() -> dict:
    """Extra subprocess kwargs so a child never opens a console window."""
    if IS_WINDOWS:
        return {"creationflags": _CREATE_NO_WINDOW}
    return {}


def new_group_kwargs() -> dict:
    """Start a child as the leader of its own group, so it and its helpers
    can be stopped together. The POSIX spelling is a new session."""
    if IS_WINDOWS:
        return {"creationflags": _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def detached_kwargs() -> dict:
    """Start a child that outlives this process and has no console."""
    if IS_WINDOWS:
        return {"creationflags": _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


_CREATE_NEW_CONSOLE = 0x00000010


def hide_child_consoles() -> None:
    """Stop console windows flashing up for every ffmpeg/ffprobe on Windows.

    A console program started by a process that has no console of its own
    (pythonw, a login autostart, the app window) gets a brand-new console
    window. The daemon starts dozens of short ffprobe runs, so without this
    the desktop flickers with them. Rather than threading a flag through
    every call site, Popen itself defaults to CREATE_NO_WINDOW here, which
    also covers asyncio's subprocesses because they are built on Popen.

    A process that has a console already shares it with its children, so
    nothing flashes and nothing is changed.
    """
    if not IS_WINDOWS:
        return
    import ctypes
    if ctypes.windll.kernel32.GetConsoleWindow():
        return
    original = subprocess.Popen.__init__
    if getattr(original, "_vice_no_window", False):
        return

    def __init__(self, *args, **kwargs):
        flags = kwargs.get("creationflags", 0) or 0
        if not flags & (_DETACHED_PROCESS | _CREATE_NEW_CONSOLE):
            kwargs["creationflags"] = flags | _CREATE_NO_WINDOW
        original(self, *args, **kwargs)

    __init__._vice_no_window = True  # type: ignore[attr-defined]
    subprocess.Popen.__init__ = __init__  # type: ignore[method-assign]


# ── Opening things ───────────────────────────────────────────────────────────

def open_path(target: str | os.PathLike) -> bool:
    """Open a file, folder or URL with the user's default handler.

    Returns whether a handler was started, never whether it succeeded, which
    is all any of these mechanisms can tell us.
    """
    text = os.fspath(target)
    try:
        if IS_WINDOWS:
            os.startfile(text)  # type: ignore[attr-defined]
            return True
        subprocess.Popen(
            ["xdg-open", text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, ValueError) as exc:
        log.debug("Could not open %s: %s", text, exc)
        return False


def reveal_path(path: str | os.PathLike) -> bool:
    """Show a file in the file manager. Windows can select the file itself;
    xdg-open can only open its folder, which is what Linux has always done."""
    target = Path(path)
    if IS_WINDOWS:
        try:
            if target.exists() and target.is_file():
                subprocess.Popen(["explorer", f"/select,{target}"], **no_window_kwargs())
                return True
        except OSError as exc:
            log.debug("Could not reveal %s: %s", target, exc)
        return open_path(target.parent)
    return open_path(target.parent)


# ── Clipboard ────────────────────────────────────────────────────────────────

def set_clipboard_text(text: str) -> bool:
    if IS_WINDOWS:
        from . import win32
        return win32.set_clipboard_text(text)
    payload = (text or "").encode("utf-8")
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            proc = subprocess.run(cmd, input=payload, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=2.0)
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("%s failed: %s", cmd[0], exc)
            continue
        if proc.returncode == 0:
            return True
    return False
