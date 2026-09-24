"""
Vice hotkey listener: uses Linux evdev to read global keyboard events.

evdev reads directly from /dev/input/event* kernel devices, bypassing the
display server entirely. This means hotkeys work on:
  • X11 (any WM/DE)
  • Wayland (Hyprland, GNOME, KDE, sway, any compositor)
  • Even TTY sessions

Requirement: the running user must have read access to /dev/input/event*
(typically via the packaged udev rule that tags input devices with uaccess).
If that rule is missing, hotkeys will not trigger.

Usage:
    listener = HotkeyListener(cfg)
    listener.on("KEY_F9", my_async_callback)            # single key
    listener.on("KEY_LEFTALT+KEY_F9", my_callback)      # or a combo
    listener.on_double("KEY_F9", my_double_tap_callback)
    await listener.start()
    ...
    await listener.stop()

Double-tap: two presses of the same key within DOUBLE_TAP_WINDOW seconds.
Single-tap callbacks fire after DOUBLE_TAP_WINDOW has elapsed with no
second press, so there is a small delay on single-tap equal to that window.
"""

from __future__ import annotations

import asyncio
import logging

import evdev
from evdev import InputDevice, categorize, ecodes

# DOUBLE_TAP_WINDOW, AsyncCallback and _safe_call used to live here; kept
# importable from this module.
from .hotkey_dispatch import DOUBLE_TAP_WINDOW, AsyncCallback, HotkeyDispatcher, _safe_call  # noqa: F401

log = logging.getLogger("vice.hotkey")

# How often the supervisor rescans /dev/input for plugged/unplugged
# keyboards. Scanning is a handful of open/ioctl/close calls; 3 s keeps
# hotkeys working within a blink of replugging a keyboard.
RESCAN_INTERVAL = 3.0


class HotkeyListener(HotkeyDispatcher):
    def __init__(self) -> None:
        super().__init__()
        # One (device, listener task) per device path, supervised for
        # hotplug. The device handle is kept so stop()/reaping can close
        # it even when the task never got to run.
        self._listeners: dict[str, tuple[InputDevice, asyncio.Task]] = {}
        self._supervisor: asyncio.Task | None = None

    async def start(self) -> None:
        """Discover keyboards, then keep watching for hotplug events.

        Listener tasks die when their device disappears (keyboard
        unplugged, errno 19); the supervisor reaps them and attaches to
        new devices, so hotkeys survive unplug/replug without a daemon
        restart.
        """
        self._running = True
        self._attach_new_keyboards(initial=True)
        if not self._listeners:
            log.warning(
                "No keyboard devices found in /dev/input/. "
                "Ensure the udev uaccess rule is installed, then run: "
                "sudo udevadm control --reload && sudo udevadm trigger. "
                "Vice keeps watching for keyboards every %.0f s.",
                RESCAN_INTERVAL,
            )
        self._supervisor = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        self._running = False
        if self._supervisor:
            self._supervisor.cancel()
            self._supervisor = None
        self._cancel_pending()
        tasks = []
        for dev, task in self._listeners.values():
            task.cancel()
            tasks.append(task)
            _close_quietly(dev)
        await asyncio.gather(*tasks, return_exceptions=True)
        self._listeners.clear()

    async def _supervise(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(RESCAN_INTERVAL)
                # Reap listeners whose device died.
                for path, (dev, task) in list(self._listeners.items()):
                    if task.done():
                        _close_quietly(dev)
                        del self._listeners[path]
                self._attach_new_keyboards()
        except asyncio.CancelledError:
            pass

    def _attach_new_keyboards(self, initial: bool = False) -> None:
        for dev in _find_keyboards(skip_paths=set(self._listeners)):
            log.info(
                "Listening for hotkeys on %s (%s)%s",
                dev.path, dev.name, "" if initial else " [hotplug]",
            )
            self._listeners[dev.path] = (dev, asyncio.create_task(self._listen(dev)))
        self._set_available(bool(self._listeners))

    async def _listen(self, dev: InputDevice) -> None:
        log.debug("Listening on %s (%s)", dev.path, dev.name)
        try:
            async for event in dev.async_read_loop():
                if not self._running:
                    break
                if event.type != ecodes.EV_KEY:
                    continue
                key_event = categorize(event)
                pressed = key_event.keycode
                if isinstance(pressed, str):
                    pressed = [pressed]
                for key_name in pressed:
                    await self._key_event(key_name, key_event.keystate)
        except OSError as exc:
            log.warning(
                "Device %s disconnected: %s, will reattach when it returns",
                dev.path, exc,
            )
        except asyncio.CancelledError:
            pass
        finally:
            # Drop held-modifier state so an unplug mid-chord can't leave a
            # phantom modifier stuck on.
            self._held_mods.clear()
            _close_quietly(dev)


def _close_quietly(dev: InputDevice) -> None:
    try:
        dev.close()
    except Exception as exc:
        log.debug("Could not close %s: %s", getattr(dev, "path", dev), exc)


def _find_keyboards(skip_paths: set[str] | None = None) -> list[InputDevice]:
    """Return readable /dev/input keyboards, skipping already-known paths."""
    skip = skip_paths or set()
    devices: list[InputDevice] = []
    for path in evdev.list_devices():
        if path in skip:
            continue
        try:
            dev = InputDevice(path)
        except (PermissionError, OSError):
            # Not readable, user not in input group, or device vanished.
            continue
        try:
            # Require that the device has at least some normal keys.
            keys = dev.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_A in keys or ecodes.KEY_SPACE in keys:
                devices.append(dev)
                continue
        except OSError:
            pass
        _close_quietly(dev)
    return devices


def can_access_hotkeys() -> bool:
    """Return True when at least one keyboard input device is readable."""
    keyboards = _find_keyboards()
    for dev in keyboards:
        _close_quietly(dev)
    return bool(keyboards)

def list_available_keys() -> list[str]:
    """Return all KEY_* names evdev knows about (for documentation/config help)."""
    return sorted(k for k in ecodes.bytype[ecodes.EV_KEY].values() if k.startswith("KEY_"))
