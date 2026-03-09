"""
Vice hotkey listener — uses Linux evdev to read global keyboard events.

evdev reads directly from /dev/input/event* kernel devices, bypassing the
display server entirely. This means hotkeys work on:
  • X11 (any WM/DE)
  • Wayland (Hyprland, GNOME, KDE, sway — any compositor)
  • Even TTY sessions

Requirement: the running user must be in the `input` group, or Vice must
run with appropriate permissions. The installer script handles this.

Usage:
    listener = HotkeyListener(cfg)
    listener.on("KEY_F9", my_async_callback)
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
import os
from typing import Callable, Coroutine, Optional

import evdev
from evdev import InputDevice, categorize, ecodes

log = logging.getLogger("vice.hotkey")

# A callback type: async def handler() -> None
AsyncCallback = Callable[[], Coroutine]

# Seconds within which a second press counts as a double-tap.
DOUBLE_TAP_WINDOW = 0.35


class HotkeyListener:
    def __init__(self) -> None:
        self._bindings: dict[str, list[AsyncCallback]] = {}
        self._double_bindings: dict[str, list[AsyncCallback]] = {}
        self._tasks: list[asyncio.Task] = []
        self._running = False
        # Per-key pending single-tap timer tasks
        self._pending: dict[str, asyncio.Task] = {}

    def on(self, key_name: str, callback: AsyncCallback) -> None:
        """
        Register an async callback for a single-tap of key_name.
        Fires after DOUBLE_TAP_WINDOW if no second press is detected.
        Multiple callbacks per key are supported.
        """
        self._bindings.setdefault(key_name, []).append(callback)

    def on_double(self, key_name: str, callback: AsyncCallback) -> None:
        """
        Register an async callback for a double-tap of key_name.
        Fires immediately on the second press within DOUBLE_TAP_WINDOW.
        Multiple callbacks per key are supported.
        """
        self._double_bindings.setdefault(key_name, []).append(callback)

    def clear_bindings(self) -> None:
        """Remove all hotkey bindings and cancel pending single-tap timers."""
        self._bindings.clear()
        self._double_bindings.clear()
        for t in self._pending.values():
            t.cancel()
        self._pending.clear()

    async def start(self) -> None:
        """Discover keyboards and spawn a listener task per device."""
        keyboards = _find_keyboards()
        if not keyboards:
            log.warning(
                "No keyboard devices found in /dev/input/. "
                "Make sure your user is in the 'input' group: "
                "sudo usermod -aG input $USER  (then log out/in)"
            )
            return

        log.info("Listening for hotkeys on %d device(s)", len(keyboards))
        self._running = True
        for dev in keyboards:
            task = asyncio.create_task(self._listen(dev))
            self._tasks.append(task)

    async def stop(self) -> None:
        self._running = False
        for t in self._pending.values():
            t.cancel()
        self._pending.clear()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _listen(self, dev: InputDevice) -> None:
        log.debug("Listening on %s (%s)", dev.path, dev.name)
        try:
            async for event in dev.async_read_loop():
                if not self._running:
                    break
                if event.type != ecodes.EV_KEY:
                    continue
                key_event = categorize(event)
                # key_down = 1
                if key_event.keystate != key_event.key_down:
                    continue
                pressed = key_event.keycode
                if isinstance(pressed, str):
                    pressed = [pressed]
                for key_name in pressed:
                    await self._handle_press(key_name)
        except OSError as exc:
            log.warning("Device %s disconnected: %s", dev.path, exc)
        except asyncio.CancelledError:
            pass

    async def _handle_press(self, key_name: str) -> None:
        has_single = bool(self._bindings.get(key_name))
        has_double = bool(self._double_bindings.get(key_name))

        # If neither single nor double bindings, nothing to do
        if not has_single and not has_double:
            return

        # If there's a pending single-tap timer for this key, cancel it —
        # this is the second press, so fire double-tap callbacks instead.
        if key_name in self._pending:
            self._pending.pop(key_name).cancel()
            if has_double:
                for cb in self._double_bindings[key_name]:
                    asyncio.create_task(_safe_call(cb, key_name))
            return

        if not has_double:
            # No double-tap binding — fire single immediately.
            if has_single:
                for cb in self._bindings[key_name]:
                    asyncio.create_task(_safe_call(cb, key_name))
            return

        # Has a double-tap binding: start a wait window.
        # If it expires without a second press, fire single-tap callbacks.
        async def _wait_and_fire():
            try:
                await asyncio.sleep(DOUBLE_TAP_WINDOW)
            except asyncio.CancelledError:
                return
            self._pending.pop(key_name, None)
            if has_single:
                for cb in self._bindings[key_name]:
                    asyncio.create_task(_safe_call(cb, key_name))

        task = asyncio.create_task(_wait_and_fire())
        self._pending[key_name] = task


async def _safe_call(cb: AsyncCallback, key_name: str) -> None:
    try:
        await cb()
    except Exception:
        log.exception("Hotkey callback for %s raised an exception", key_name)


def _find_keyboards() -> list[InputDevice]:
    """Return all readable /dev/input devices that have key capabilities."""
    devices: list[InputDevice] = []
    for path in evdev.list_devices():
        try:
            dev = InputDevice(path)
            caps = dev.capabilities()
            if ecodes.EV_KEY in caps:
                # Require that the device has at least some normal keys
                keys = caps[ecodes.EV_KEY]
                if ecodes.KEY_A in keys or ecodes.KEY_SPACE in keys:
                    devices.append(dev)
        except (PermissionError, OSError):
            # Not readable — user not in input group, or not a keyboard
            pass
    return devices


def list_available_keys() -> list[str]:
    """Return all KEY_* names evdev knows about (for documentation/config help)."""
    return sorted(k for k in ecodes.bytype[ecodes.EV_KEY].values() if k.startswith("KEY_"))
