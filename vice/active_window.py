"""Active-window detection — adapters for X11, Hyprland, Sway.

Each adapter shells out to the compositor's CLI/IPC and returns
{"process": str, "class": str, "pid": int} or None. On other Wayland
sessions (KDE Plasma/KWin, GNOME/Mutter) where DISPLAY is set, we fall back
to the X11 adapter via XWayland, which resolves any focused XWayland window
— that covers most games (Steam/Proton, Lutris). Focused native-Wayland
windows yield no result on those compositors, so detection returns None.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

ActiveWindow = dict  # {"process": str, "class": str, "pid": int}


def _read_proc_comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text(errors="replace").strip()
    except Exception:
        return ""


def _run(cmd: list[str], timeout: float = 1.0) -> str:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return ""
        return result.stdout
    except Exception:
        return ""


# ─── Hyprland ───────────────────────────────────────────────────────────────

def _get_active_window_hyprland() -> Optional[ActiveWindow]:
    out = _run(["hyprctl", "activewindow", "-j"])
    if not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    pid = int(data.get("pid") or 0)
    cls = str(data.get("class") or "")
    proc = _read_proc_comm(pid) if pid else ""
    if not (cls or proc):
        return None
    return {"process": proc, "class": cls, "pid": pid}


# ─── Sway ───────────────────────────────────────────────────────────────────

def _walk_sway_tree(node: dict) -> Optional[dict]:
    """Depth-first search for the focused leaf node."""
    if node.get("focused") and not node.get("nodes") and not node.get("floating_nodes"):
        return node
    for child in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
        hit = _walk_sway_tree(child)
        if hit:
            return hit
    return None


def _get_active_window_sway() -> Optional[ActiveWindow]:
    out = _run(["swaymsg", "-t", "get_tree"])
    if not out:
        return None
    try:
        tree = json.loads(out)
    except json.JSONDecodeError:
        return None
    leaf = _walk_sway_tree(tree)
    if not leaf:
        return None
    pid = int(leaf.get("pid") or 0)
    cls = str(
        leaf.get("app_id")
        or (leaf.get("window_properties") or {}).get("class")
        or ""
    )
    proc = _read_proc_comm(pid) if pid else ""
    if not (cls or proc):
        return None
    return {"process": proc, "class": cls, "pid": pid}


# ─── X11 ────────────────────────────────────────────────────────────────────

def _get_active_window_x11() -> Optional[ActiveWindow]:
    wid = _run(["xdotool", "getactivewindow"]).strip()
    if not wid:
        return None
    pid_text = _run(["xdotool", "getwindowpid", wid]).strip()
    try:
        pid = int(pid_text) if pid_text else 0
    except ValueError:
        pid = 0
    proc = _read_proc_comm(pid) if pid else ""
    cls = ""
    wmclass = _run(["xprop", "-id", wid, "WM_CLASS"]).strip()
    # wmclass looks like:  WM_CLASS(STRING) = "firefox", "firefox"
    if "=" in wmclass:
        rhs = wmclass.split("=", 1)[1].strip()
        # Take the second of the two quoted names if both present
        parts = [p.strip().strip('"') for p in rhs.split(",")]
        if parts:
            cls = parts[-1] or parts[0]
    if not (cls or proc):
        return None
    return {"process": proc, "class": cls, "pid": pid}


def _candidate_windows_wmctrl() -> list[ActiveWindow]:
    out = _run(["wmctrl", "-lpx"], timeout=2.0)
    windows: list[ActiveWindow] = []
    for line in out.splitlines():
        # 0x03a00003  0 1234   steam_app_123.steam_app_123  host Title
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[2])
        except ValueError:
            continue
        cls = parts[3].split(".")[-1]
        proc = _read_proc_comm(pid) if pid else ""
        if cls or proc:
            windows.append({"process": proc, "class": cls, "pid": pid})
    return windows


def _candidate_windows_xdotool(cap: int = 30) -> list[ActiveWindow]:
    out = _run(["xdotool", "search", "--onlyvisible", "--class", ""], timeout=2.0)
    windows: list[ActiveWindow] = []
    for wid in out.split()[:cap]:
        pid_text = _run(["xdotool", "getwindowpid", wid]).strip()
        try:
            pid = int(pid_text) if pid_text else 0
        except ValueError:
            pid = 0
        cls = ""
        wmclass = _run(["xprop", "-id", wid, "WM_CLASS"]).strip()
        if "=" in wmclass:
            rhs = wmclass.split("=", 1)[1].strip()
            parts = [p.strip().strip('"') for p in rhs.split(",")]
            if parts:
                cls = parts[-1] or parts[0]
        proc = _read_proc_comm(pid) if pid else ""
        if cls or proc:
            windows.append({"process": proc, "class": cls, "pid": pid})
    return windows


def list_candidate_windows() -> list[ActiveWindow]:
    """All visible X clients with process/class info. Fallback for
    compositors where the focused window can't be read reliably (KWin only
    partially mirrors focus into XWayland's EWMH properties, #102). Empty on
    non-X11 adapters — Hyprland and Sway report focus natively."""
    if _ADAPTER is not _get_active_window_x11:
        return []
    try:
        windows = _candidate_windows_wmctrl()
        if windows:
            return windows
        return _candidate_windows_xdotool()
    except Exception as exc:
        log.debug("candidate window scan raised: %s", exc)
        return []


# ─── Window enumeration for capture source selection ──────────────────────

# System-level window classes/titles to exclude from capture-eligible lists.
_SYSTEM_WINDOW_CLASSES = frozenset({
    "panel", "dock", "notification", "desktop_window", "desktop",
    "xfce4-panel", "plasmashell", "kdeconnect", "tray",
    "Conky", "polybar", "eww-bar", "waybar",
})


def _is_system_window(cls: str, title: str) -> bool:
    """Return True if the window looks like a system UI element, not an app."""
    low_cls = (cls or "").lower()
    low_title = (title or "").lower()
    if low_cls in _SYSTEM_WINDOW_CLASSES:
        return True
    if any(s in low_cls for s in ("panel", "dock", "notification", "tray", "bar")):
        return True
    if low_title in ("desktop", "xfce4-panel", "plasmashell"):
        return True
    return False


def _list_capture_windows_x11() -> list[dict]:
    """List visible X11 windows via wmctrl, enriched with titles."""
    windows: list[dict] = []
    out = _run(["wmctrl", "-lpx"], timeout=2.0)
    if not out:
        # Fallback to xdotool
        return _list_capture_windows_xdotool()
    for line in out.splitlines():
        # Format: 0xID  desktop  PID  WM_CLASS  Title
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        wid = parts[0]
        try:
            pid = int(parts[2])
        except ValueError:
            continue
        wm_class = parts[3].split(".")[-1]
        title = parts[4].strip()
        proc = _read_proc_comm(pid) if pid else ""
        if not (wm_class or proc):
            continue
        if _is_system_window(wm_class, title):
            continue
        app = proc or wm_class
        windows.append({
            "id": wid,
            "title": title or app,
            "app": app,
            "pid": pid,
        })
    return windows


def _list_capture_windows_xdotool(cap: int = 50) -> list[dict]:
    """List visible X11 windows via xdotool (fallback when wmctrl is absent)."""
    windows: list[dict] = []
    out = _run(["xdotool", "search", "--onlyvisible", "--class", ""], timeout=2.0)
    if not out:
        return windows
    for wid in out.split()[:cap]:
        pid_text = _run(["xdotool", "getwindowpid", wid]).strip()
        try:
            pid = int(pid_text) if pid_text else 0
        except ValueError:
            pid = 0
        # Get WM_CLASS
        cls = ""
        wmclass = _run(["xprop", "-id", wid, "WM_CLASS"]).strip()
        if "=" in wmclass:
            rhs = wmclass.split("=", 1)[1].strip()
            parts = [p.strip().strip('"') for p in rhs.split(",")]
            if parts:
                cls = parts[-1] or parts[0]
        # Get window title
        title = ""
        name_prop = _run(["xprop", "-id", wid, "WM_NAME"]).strip()
        if "=" in name_prop:
            title = name_prop.split("=", 1)[1].strip().strip('"')
        if not title:
            title = _run(["xdotool", "getwindowname", wid]).strip()
        proc = _read_proc_comm(pid) if pid else ""
        if not (cls or proc):
            continue
        if _is_system_window(cls, title):
            continue
        app = proc or cls
        windows.append({
            "id": wid,
            "title": title or app,
            "app": app,
            "pid": pid,
        })
    return windows


def _list_capture_windows_hyprland() -> list[dict]:
    """List all visible Hyprland clients."""
    out = _run(["hyprctl", "clients", "-j"], timeout=2.0)
    if not out:
        return []
    try:
        clients = json.loads(out)
    except json.JSONDecodeError:
        return []
    if not isinstance(clients, list):
        return []
    windows: list[dict] = []
    for client in clients:
        if client.get("hidden"):
            continue
        addr = client.get("address", "")
        title = str(client.get("title") or "")
        cls = str(client.get("class") or "")
        pid = int(client.get("pid") or 0)
        size = client.get("size", [0, 0])
        proc = _read_proc_comm(pid) if pid else ""
        if not (cls or proc or title):
            continue
        if _is_system_window(cls, title):
            continue
        app = proc or cls
        windows.append({
            "id": f"hyprland:{addr}" if addr else f"pid:{pid}",
            "title": title or app,
            "app": app,
            "pid": pid,
            "width": int(size[0]) if len(size) > 0 else 0,
            "height": int(size[1]) if len(size) > 1 else 0,
        })
    return windows


def _walk_sway_tree_all(node: dict, results: list[dict]) -> None:
    """Collect all visible leaf nodes from the Sway tree."""
    is_leaf = not node.get("nodes") and not node.get("floating_nodes")
    if is_leaf:
        app_id = node.get("app_id") or ""
        win_class = (node.get("window_properties") or {}).get("class") or ""
        name = node.get("name") or ""
        pid = int(node.get("pid") or 0)
        rect = node.get("rect", {})
        if app_id or win_class or name:
            cls = app_id or win_class
            if not _is_system_window(cls, name):
                results.append({
                    "id": f"sway:{node.get('id', '')}",
                    "title": name or cls or app_id,
                    "app": _read_proc_comm(pid) if pid else cls,
                    "pid": pid,
                    "width": int(rect.get("width", 0)),
                    "height": int(rect.get("height", 0)),
                })
    for child in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
        _walk_sway_tree_all(child, results)


def _list_capture_windows_sway() -> list[dict]:
    """List all visible Sway containers."""
    out = _run(["swaymsg", "-t", "get_tree"], timeout=2.0)
    if not out:
        return []
    try:
        tree = json.loads(out)
    except json.JSONDecodeError:
        return []
    windows: list[dict] = []
    _walk_sway_tree_all(tree, windows)
    return windows


def list_capture_windows() -> list[dict]:
    """Return capture-eligible windows for the current compositor.

    Each entry: {"id": str, "title": str, "app": str, "pid": int,
                 "width": int (optional), "height": int (optional)}

    The "id" value is a compositor-specific identifier that can be passed
    to the recording backend to target that specific window.
    """
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return _list_capture_windows_hyprland()
    if os.environ.get("SWAYSOCK"):
        return _list_capture_windows_sway()
    # X11 or XWayland
    if _ADAPTER is _get_active_window_x11 or os.environ.get("DISPLAY"):
        try:
            windows = _list_capture_windows_x11()
            if windows:
                return windows
        except Exception as exc:
            log.debug("X11 window enumeration raised: %s", exc)
    return []


def detection_tools_status() -> dict:
    """Which X11 window-detection tools are installed — for doctor and logs."""
    import shutil
    return {tool: bool(shutil.which(tool)) for tool in ("xdotool", "xprop", "wmctrl")}


# ─── compositor detection (one-shot at import time) ─────────────────────────

def _detect_compositor_adapter() -> Optional[Callable[[], Optional[ActiveWindow]]]:
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return _get_active_window_hyprland
    if os.environ.get("SWAYSOCK"):
        return _get_active_window_sway
    if os.environ.get("XDG_SESSION_TYPE") == "x11" or (
        os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
    ):
        return _get_active_window_x11
    # Other Wayland compositors (KDE/KWin, GNOME/Mutter): no native IPC adapter,
    # but if XWayland is up we can still read focused X clients (most games).
    if os.environ.get("DISPLAY"):
        return _get_active_window_x11
    return None


_ADAPTER: Optional[Callable[[], Optional[ActiveWindow]]] = _detect_compositor_adapter()


def get_active_window() -> Optional[ActiveWindow]:
    """Return the currently focused window, or None on unsupported compositors
    or when no focused window can be determined."""
    if _ADAPTER is None:
        return None
    try:
        return _ADAPTER()
    except Exception as exc:
        log.debug("active_window adapter raised: %s", exc)
        return None


def supported_compositor() -> bool:
    """For UI display — whether v1 supports the running compositor."""
    return _ADAPTER is not None


def uses_x11_adapter() -> bool:
    """Whether detection goes through xdotool/xprop (X11 or XWayland)."""
    return _ADAPTER is _get_active_window_x11
