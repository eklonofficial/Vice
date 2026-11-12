"""What kind of machine and install Vice is running on.

Nothing changes behaviour on this yet. It is reported in the status payload
and by `vice doctor` so a later UI can, for example, switch to a
controller-friendly layout on a Steam Deck without new plumbing.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from .runtime import in_vice_flatpak

# Valve's board names: Jupiter is the LCD Steam Deck, Galileo the OLED one.
_STEAM_DECK_PRODUCTS = {"jupiter", "galileo"}
_IMMUTABLE_IDS = {"steamos", "bazzite", "silverblue", "kinoite", "bluefin", "aurora"}

# Read from the root so tests can point at a fixture tree.
_ROOT = Path("/")


def _read(path: str) -> str:
    try:
        return (_ROOT / path.lstrip("/")).read_text(errors="replace").strip()
    except OSError:
        return ""


def _os_release() -> dict[str, str]:
    """The host's os-release. Inside a Flatpak /etc is the runtime's, and the
    host's is mounted at /run/host."""
    text = _read("/run/host/os-release") if in_vice_flatpak() else ""
    text = text or _read("/etc/os-release") or _read("/usr/lib/os-release")
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip().strip('"')
    return values


def _device() -> str:
    vendor = _read("/sys/class/dmi/id/board_vendor").lower()
    product = (_read("/sys/class/dmi/id/product_name")
               or _read("/sys/class/dmi/id/board_name")).lower()
    if "valve" in vendor and product in _STEAM_DECK_PRODUCTS:
        return "steamdeck"
    return "desktop"


def _immutable(os_id: str, like: str) -> bool:
    if os_id in _IMMUTABLE_IDS or any(part in _IMMUTABLE_IDS for part in like.split()):
        return True
    # rpm-ostree and other ostree systems mark themselves with this file.
    return (_ROOT / "run/ostree-booted").exists()


def _session() -> str:
    if os.environ.get("GAMESCOPE_WAYLAND_DISPLAY") or "gamescope" in (
        os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    ):
        return "gamescope"
    return os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("XDG_SESSION_TYPE", "")


@lru_cache(maxsize=1)
def detect_platform(packaging: Optional[Callable[[], str]] = None) -> dict:
    """device (steamdeck or desktop), os_id, variant, immutable, packaging
    (flatpak, aur, script, nix or unknown) and session (gamescope in Steam
    Deck Game Mode, else the desktop name)."""
    release = _os_release()
    os_id = release.get("ID", "").lower()
    if in_vice_flatpak():
        method = "flatpak"
    elif packaging is not None:
        method = packaging()
    else:
        method = "unknown"
    return {
        "device": _device(),
        "os_id": os_id,
        "variant": release.get("VARIANT_ID", "").lower(),
        "immutable": _immutable(os_id, release.get("ID_LIKE", "").lower()),
        "packaging": method,
        "session": _session(),
    }
