"""Active-window detection: adapters for X11, Hyprland and Sway.

Each adapter shells out to the compositor's CLI/IPC and returns
{"process": str, "class": str, "pid": int} or None. On other Wayland
sessions (KDE Plasma/KWin, GNOME/Mutter) where DISPLAY is set, we fall back
to the X11 adapter via XWayland, which resolves any focused XWayland window.
That covers most games (Steam/Proton, Lutris). Focused native-Wayland
windows yield no result on those compositors, so detection returns None.
"""

from __future__ import annotations

import json
import logging
import os
import re
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


def read_steam_app_id(pid) -> Optional[str]:
    """Steam's app id for a running process, read from its environment.

    Several Source games ship the same launcher binary, so the process name
    cannot tell Team Fortress 2 from Half-Life 2 (#162). Steam sets SteamAppId
    on every game it launches, which names the game exactly.

    Returns None for anything unusable, so a caller falls back to matching on
    the process name exactly as before.
    """
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError as exc:
        log.debug("Cannot read the environment of pid %s: %s", pid, exc)
        return None

    fallback = None
    for entry in raw.split(b"\0"):
        name, sep, value = entry.partition(b"=")
        if not sep or name not in (b"SteamAppId", b"SteamGameId"):
            continue
        app_id = value.decode("utf-8", "replace").strip()
        if not app_id.isdigit() or app_id == "0":
            continue
        if name == b"SteamAppId":
            return app_id
        fallback = fallback or app_id
    return fallback


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


def _active_window_id_x11() -> Optional[str]:
    wid = _run(["xdotool", "getactivewindow"]).strip()
    return wid or None


def _active_window_geometry_hyprland() -> Optional[tuple[int, int]]:
    out = _run(["hyprctl", "activewindow", "-j"])
    if not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    size = data.get("size") or []
    if len(size) == 2:
        try:
            w, h = int(size[0]), int(size[1])
            if w > 0 and h > 0:
                return (w, h)
        except (TypeError, ValueError):
            pass
    return None


def _active_window_geometry_sway() -> Optional[tuple[int, int]]:
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
    rect = leaf.get("rect") or {}
    try:
        w, h = int(rect.get("width") or 0), int(rect.get("height") or 0)
        if w > 0 and h > 0:
            return (w, h)
    except (TypeError, ValueError):
        pass
    return None


def _window_geometry_by_id_x11(wid: str) -> Optional[tuple[int, int]]:
    out = _run(["xdotool", "getwindowgeometry", "--shell", wid])
    width = height = 0
    for line in out.splitlines():
        if line.startswith("WIDTH="):
            width = int(line.split("=", 1)[1] or 0)
        elif line.startswith("HEIGHT="):
            height = int(line.split("=", 1)[1] or 0)
    return (width, height) if width > 0 and height > 0 else None


def _active_window_geometry_x11() -> Optional[tuple[int, int]]:
    wid = _run(["xdotool", "getactivewindow"]).strip()
    return _window_geometry_by_id_x11(wid) if wid else None


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
    non-X11 adapters, Hyprland and Sway report focus natively."""
    if _current_adapter() is not _get_active_window_x11:
        return []
    try:
        windows = _candidate_windows_wmctrl()
        if windows:
            return windows
        return _candidate_windows_xdotool()
    except Exception as exc:
        log.debug("candidate window scan raised: %s", exc)
        return []


# ─── pointer monitor ────────────────────────────────────────────────────────
# Which monitor the pointer sits on, named the way the capture backends name
# it (DP-1, HDMI-A-1) so the result can be handed straight to
# gpu-screen-recorder's -w or matched against xrandr's output list.

def _monitor_at(point: tuple[int, int], rects: list[dict]) -> Optional[str]:
    x, y = point
    for r in rects:
        if r["x"] <= x < r["x"] + r["w"] and r["y"] <= y < r["y"] + r["h"]:
            return r["name"]
    return None


def _pointer_display_hyprland() -> Optional[str]:
    monitors_raw = _run(["hyprctl", "monitors", "-j"])
    if not monitors_raw:
        return None
    try:
        monitors = json.loads(monitors_raw)
    except json.JSONDecodeError:
        return None

    # x/y and the cursor position are logical coordinates, but width/height are
    # the raw mode, so a scaled monitor needs dividing through to match.
    rects = []
    for m in monitors:
        if not m.get("name"):
            continue
        try:
            scale = float(m.get("scale") or 1.0) or 1.0
        except (TypeError, ValueError):
            scale = 1.0
        rects.append({
            "name": str(m["name"]),
            "x": int(m.get("x") or 0),
            "y": int(m.get("y") or 0),
            "w": round(int(m.get("width") or 0) / scale),
            "h": round(int(m.get("height") or 0) / scale),
        })
    cursor_raw = _run(["hyprctl", "cursorpos", "-j"])
    if cursor_raw:
        try:
            pos = json.loads(cursor_raw)
            hit = _monitor_at((int(pos["x"]), int(pos["y"])), rects)
            if hit:
                return hit
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
    # cursorpos fails while the pointer is over a locked/DPMS-off screen;
    # the focused monitor is the same one in every case that matters here.
    for m in monitors:
        if m.get("focused") and m.get("name"):
            return str(m["name"])
    return None


def _pointer_display_sway() -> Optional[str]:
    """Sway has no cursor-position IPC, but it moves focus to the output the
    pointer enters, so the focused output is the pointer's output."""
    out = _run(["swaymsg", "-t", "get_outputs", "-r"])
    if not out:
        return None
    try:
        outputs = json.loads(out)
    except json.JSONDecodeError:
        return None
    for o in outputs:
        if o.get("focused") and o.get("name"):
            return str(o["name"])
    return None


def _parse_xdotool_mouselocation(raw: str) -> Optional[tuple[int, int]]:
    values: dict[str, int] = {}
    for line in raw.splitlines():
        key, _, value = line.partition("=")
        if key in ("X", "Y"):
            try:
                values[key] = int(value)
            except ValueError:
                return None
    if "X" in values and "Y" in values:
        return values["X"], values["Y"]
    return None


def _parse_xrandr_monitor_rects(raw: str) -> list[dict]:
    rects: list[dict] = []
    for line in raw.splitlines():
        # " 0: +*DP-1 3440/800x1440/340+0+0  DP-1"
        parts = line.split()
        if len(parts) < 4 or not parts[0].rstrip(":").isdigit():
            continue
        geometry = parts[2]
        match = re.match(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)$", geometry)
        if not match:
            continue
        w, h, x, y = (int(g) for g in match.groups())
        rects.append({"name": parts[-1], "x": x, "y": y, "w": w, "h": h})
    return rects


def _pointer_display_x11() -> Optional[str]:
    location = _run(["xdotool", "getmouselocation", "--shell"])
    point = _parse_xdotool_mouselocation(location) if location else None
    if point is None:
        return None
    rects = _parse_xrandr_monitor_rects(_run(["xrandr", "--listactivemonitors"]))
    return _monitor_at(point, rects)


def pointer_display() -> Optional[str]:
    """Name of the monitor the pointer is on, or None when it cannot be
    determined (unsupported compositor, missing tools)."""
    adapter = _current_adapter()
    if adapter is _get_active_window_hyprland:
        resolver = _pointer_display_hyprland
    elif adapter is _get_active_window_sway:
        resolver = _pointer_display_sway
    elif adapter is _get_active_window_x11 and not os.environ.get("WAYLAND_DISPLAY"):
        # Under XWayland the X pointer only tracks the real one while it is
        # over an X surface, so this is X11 sessions only.
        resolver = _pointer_display_x11
    else:
        return None
    try:
        return resolver()
    except Exception as exc:
        log.debug("pointer_display resolver raised: %s", exc)
        return None


def pointer_display_supported() -> bool:
    """For the settings UI. Whether follow-the-pointer capture can work on the
    running session."""
    adapter = _current_adapter()
    if adapter in (_get_active_window_hyprland, _get_active_window_sway):
        return True
    return adapter is _get_active_window_x11 and not os.environ.get("WAYLAND_DISPLAY")


def detection_tools_status() -> dict:
    """Which X11 window-detection tools are installed, for doctor and logs."""
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


# Resolved on first use and kept once found. Deliberately not resolved at import
# time: the daemon can reach this module before the session has exported
# DISPLAY, because the unit is wanted by default.target as well as
# graphical-session.target (#139) and wait_for_display() is satisfied by
# WAYLAND_DISPLAY on its own. Deciding once, that early, left KDE and GNOME
# Wayland sessions with no window detection at all for the life of the process,
# which is why restarting the daemon by hand appeared to fix game tagging
# (#176). A failed detection is never cached, so a display arriving late is
# picked up on the next call.
_ADAPTER: Optional[Callable[[], Optional[ActiveWindow]]] = None


def _current_adapter() -> Optional[Callable[[], Optional[ActiveWindow]]]:
    global _ADAPTER
    if _ADAPTER is None:
        _ADAPTER = _detect_compositor_adapter()
    return _ADAPTER

_GEOMETRY_ADAPTERS: dict = {
    _get_active_window_hyprland: _active_window_geometry_hyprland,
    _get_active_window_sway: _active_window_geometry_sway,
    _get_active_window_x11: _active_window_geometry_x11,
}


def get_active_window() -> Optional[ActiveWindow]:
    """Return the currently focused window, or None on unsupported compositors
    or when no focused window can be determined."""
    adapter = _current_adapter()
    if adapter is None:
        return None
    try:
        return adapter()
    except Exception as exc:
        log.debug("active_window adapter raised: %s", exc)
        return None


def get_active_window_geometry() -> Optional[tuple[int, int]]:
    """Pixel (width, height) of the currently focused window, or None when
    unsupported or undetectable. Used by window_capture to size gpu-screen-
    recorder's `-s` flag, which it requires for `-w focused`."""
    fn = _GEOMETRY_ADAPTERS.get(_ADAPTER)
    if fn is None:
        return None
    try:
        return fn()
    except Exception as exc:
        log.debug("active_window geometry adapter raised: %s", exc)
        return None


def get_focused_window_id() -> Optional[str]:
    """A stable id for the currently focused window, suitable for gpu-screen-
    recorder's `-w <window_id>` mode, which pins capture to that window
    instead of following focus around like `-w focused` does. X11/XWayland
    only (GSR's per-window capture doesn't support native-Wayland windows);
    None on Hyprland/Sway sessions with no XWayland fallback in play."""
    if _ADAPTER is not _get_active_window_x11:
        return None
    try:
        return _active_window_id_x11()
    except Exception as exc:
        log.debug("active_window id lookup raised: %s", exc)
        return None


def get_window_geometry(window_id: str) -> Optional[tuple[int, int]]:
    """Pixel (width, height) of a specific window by id, same id space as
    get_focused_window_id(), so this works even after focus has moved
    elsewhere. Used to size gpu-screen-recorder's `-s` flag for a pinned
    window_capture target."""
    if _ADAPTER is not _get_active_window_x11 or not window_id:
        return None
    try:
        return _window_geometry_by_id_x11(window_id)
    except Exception as exc:
        log.debug("active_window geometry-by-id lookup raised: %s", exc)
        return None


def supported_compositor() -> bool:
    """For UI display, whether v1 supports the running compositor."""
    return _current_adapter() is not None


def uses_x11_adapter() -> bool:
    """Whether detection goes through xdotool/xprop (X11 or XWayland)."""
    return _current_adapter() is _get_active_window_x11


def adapter_name() -> str:
    """Which adapter detection is using, for logs and doctor."""
    return {
        _get_active_window_hyprland: "hyprland",
        _get_active_window_sway:     "sway",
        _get_active_window_x11:      "x11",
    }.get(_current_adapter(), "none")
