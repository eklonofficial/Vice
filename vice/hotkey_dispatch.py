"""Platform-neutral hotkey dispatch: bindings, combos and double-tap.

Each platform's listener only has to turn its own key events into evdev key
names ("KEY_F9", "KEY_LEFTALT") and hand them to _key_event(). Everything
that decides what a press means lives here, so a combo or a double-tap
behaves the same on Linux and Windows. evdev names are the common language
because they are what config files and the settings UI already store.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Coroutine

from .config import MODIFIER_CANON, MODIFIER_KEYS, normalize_combo

log = logging.getLogger("vice.hotkey")

# A callback type: async def handler() -> None
AsyncCallback = Callable[[], Coroutine]

# Seconds within which a second press counts as a double-tap.
DOUBLE_TAP_WINDOW = 0.35

KEY_DOWN = 1
KEY_UP = 0
KEY_HOLD = 2


class HotkeyDispatcher:
    def __init__(self) -> None:
        self._bindings: dict[str, list[AsyncCallback]] = {}
        self._double_bindings: dict[str, list[AsyncCallback]] = {}
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

    def _cancel_pending(self) -> None:
        for t in self._pending.values():
            t.cancel()
        self._pending.clear()

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

    async def _key_event(self, key_name: str, state: int) -> None:
        """One key transition, as an evdev name and KEY_DOWN/KEY_UP/KEY_HOLD."""
        if key_name in MODIFIER_KEYS:
            # Track held modifiers so the next main-key press can be
            # matched as a combo. Modifiers never fire on their own.
            canon = MODIFIER_CANON.get(key_name, key_name)
            if state == KEY_DOWN:
                self._held_mods.add(canon)
            elif state == KEY_UP:
                self._held_mods.discard(canon)
            return
        if state != KEY_DOWN:
            return
        await self._handle_press(self._combo_for(key_name))

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
