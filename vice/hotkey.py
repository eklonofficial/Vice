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
from typing import Callable, Coroutine

import evdev
from evdev import InputDevice, categorize, ecodes

from .config import MODIFIER_CANON, MODIFIER_KEYS, normalize_combo

log = logging.getLogger("vice.hotkey")

# A callback type: async def handler() -> None
AsyncCallback = Callable[[], Coroutine]

# Seconds within which a second press counts as a double-tap.
DOUBLE_TAP_WINDOW = 0.35

# How often the supervisor rescans /dev/input for plugged/unplugged
# keyboards. Scanning is a handful of open/ioctl/close calls; 3 s keeps
# hotkeys working within a blink of replugging a keyboard.
RESCAN_INTERVAL = 3.0


class HotkeyListener:
    def __init__(self) -> None:
        self._bindings: dict[str, list[AsyncCallback]] = {}
        self._double_bindings: dict[str, list[AsyncCallback]] = {}
        # One (device, listener task) per device path, supervised for
        # hotplug. The device handle is kept so stop()/reaping can close
        # it even when the task never got to run.
        self._listeners: dict[str, tuple[InputDevice, asyncio.Task]] = {}
        self._supervisor: asyncio.Task | None = None
        self._running = False
        # Per-key pending single-tap timer tasks
        self._pending: dict[str, asyncio.Task] = {}
        # Modifier keys currently held down (canonical names, e.g. KEY_LEFTALT),
        # so a press like Alt+F9 can be matched as one combo.
        self._held_mods: set[str] = set()
        self.available = False
        # Optional: called with the new availability whenever it changes
        # (e.g. last keyboard unplugged, or one plugged back in).
        self.on_availability_change: Callable[[bool], None] | None = None

    def on(self, key_name: str, callback: AsyncCallback) -> None:
        """
        Register an async callback for a single-tap of key_name.
        Fires after DOUBLE_TAP_WINDOW if no second press is detected.
        Multiple callbacks per key are supported.

        key_name may be a combo like "KEY_LEFTALT+KEY_F9"; it is normalized so
        registration and live matching share one canonical form.
        """
        self._bindings.setdefault(normalize_combo(key_name), []).append(callback)

    def on_double(self, key_name: str, callback: AsyncCallback) -> None:
        """
        Register an async callback for a double-tap of key_name.
        Fires immediately on the second press within DOUBLE_TAP_WINDOW.
        Multiple callbacks per key are supported.

        key_name may be a combo like "KEY_LEFTALT+KEY_F9".
        """
        self._double_bindings.setdefault(normalize_combo(key_name), []).append(callback)

    def clear_bindings(self) -> None:
        """Remove all hotkey bindings and cancel pending single-tap timers."""
        self._bindings.clear()
        self._double_bindings.clear()
        for t in self._pending.values():
            t.cancel()
        self._pending.clear()

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
        for t in self._pending.values():
            t.cancel()
        self._pending.clear()
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

    def _set_available(self, value: bool) -> None:
        if value == self.available:
            return
        self.available = value
        if value:
            log.info("Keyboard available, hotkeys active")
        else:
            log.warning("All keyboards disconnected, hotkeys inactive until one reappears")
        if self.on_availability_change:
            try:
                self.on_availability_change(value)
            except Exception:
                log.exception("Hotkey availability callback raised")

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
                    if key_name in MODIFIER_KEYS:
                        # Track held modifiers so the next main-key press can be
                        # matched as a combo. Modifiers never fire on their own.
                        canon = MODIFIER_CANON.get(key_name, key_name)
                        if key_event.keystate == key_event.key_down:
                            self._held_mods.add(canon)
                        elif key_event.keystate == key_event.key_up:
                            self._held_mods.discard(canon)
                        continue
                    # key_down = 1
                    if key_event.keystate != key_event.key_down:
                        continue
                    await self._handle_press(self._combo_for(key_name))
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

    def _combo_for(self, key_name: str) -> str:
        """Build the canonical combo string for a main-key press, folding in any
        modifiers currently held down."""
        if not self._held_mods:
            return key_name
        return normalize_combo("+".join((*self._held_mods, key_name)))

    async def _handle_press(self, key_name: str) -> None:
        has_single = bool(self._bindings.get(key_name))
        has_double = bool(self._double_bindings.get(key_name))

        # If neither single nor double bindings, nothing to do
        if not has_single and not has_double:
            return

        # If there's a pending single-tap timer for this key, cancel it,
        # this is the second press, so fire double-tap callbacks instead.
        if key_name in self._pending:
            self._pending.pop(key_name).cancel()
            if has_double:
                for cb in self._double_bindings[key_name]:
                    asyncio.create_task(_safe_call(cb, key_name))
            return

        if not has_double:
            # No double-tap binding, fire single immediately.
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
