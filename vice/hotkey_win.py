"""
Vice hotkey listener for Windows: a low-level keyboard hook (WH_KEYBOARD_LL).

The hook sees every key on the desktop before any window does, including
games in fullscreen, which is the Windows equivalent of reading evdev below
the display server. It never consumes a key: every event is passed on with
CallNextHookEx, so F9 still reaches the game, the same as on Linux.
RegisterHotKey was not used for exactly that reason, it swallows the key.

Keys are reported in evdev names so config files, combos and the settings
UI are identical on both platforms. The main keyboard block is mapped by
scan code rather than virtual key, because a scan code is a physical
position: the browser's KeyboardEvent.code, which the settings UI captures
from, is positional too, so "the key left of S" is KEY_A on QWERTY and
AZERTY alike. Keys whose meaning does not depend on layout (F-keys,
navigation, numpad, modifiers, media) are mapped by virtual key.

The hook runs on its own thread with a message pump, as Windows requires,
and hands each event to the asyncio loop with call_soon_threadsafe.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Optional

from .hotkey_dispatch import KEY_DOWN, KEY_HOLD, KEY_UP, HotkeyDispatcher

log = logging.getLogger("vice.hotkey")

# ── Key tables ───────────────────────────────────────────────────────────────

# Scan code set 1 (what the hook reports) for the main block. Below 0x59 a
# set-1 scan code is the Linux keycode number itself, which is why these line
# up with evdev without translation.
_SCAN_TO_KEY: dict[int, str] = {
    0x02: "KEY_1", 0x03: "KEY_2", 0x04: "KEY_3", 0x05: "KEY_4", 0x06: "KEY_5",
    0x07: "KEY_6", 0x08: "KEY_7", 0x09: "KEY_8", 0x0A: "KEY_9", 0x0B: "KEY_0",
    0x0C: "KEY_MINUS", 0x0D: "KEY_EQUAL",
    0x10: "KEY_Q", 0x11: "KEY_W", 0x12: "KEY_E", 0x13: "KEY_R", 0x14: "KEY_T",
    0x15: "KEY_Y", 0x16: "KEY_U", 0x17: "KEY_I", 0x18: "KEY_O", 0x19: "KEY_P",
    0x1A: "KEY_LEFTBRACE", 0x1B: "KEY_RIGHTBRACE",
    0x1E: "KEY_A", 0x1F: "KEY_S", 0x20: "KEY_D", 0x21: "KEY_F", 0x22: "KEY_G",
    0x23: "KEY_H", 0x24: "KEY_J", 0x25: "KEY_K", 0x26: "KEY_L",
    0x27: "KEY_SEMICOLON", 0x28: "KEY_APOSTROPHE", 0x29: "KEY_GRAVE",
    0x2B: "KEY_BACKSLASH",
    0x2C: "KEY_Z", 0x2D: "KEY_X", 0x2E: "KEY_C", 0x2F: "KEY_V", 0x30: "KEY_B",
    0x31: "KEY_N", 0x32: "KEY_M", 0x33: "KEY_COMMA", 0x34: "KEY_DOT",
    0x35: "KEY_SLASH", 0x56: "KEY_102ND",
}

# Virtual keys whose meaning does not move with the layout.
_VK_TO_KEY: dict[int, str] = {
    0x08: "KEY_BACKSPACE", 0x09: "KEY_TAB", 0x13: "KEY_PAUSE", 0x14: "KEY_CAPSLOCK",
    0x1B: "KEY_ESC", 0x20: "KEY_SPACE", 0x2C: "KEY_SYSRQ",
    0x5D: "KEY_COMPOSE", 0x90: "KEY_NUMLOCK", 0x91: "KEY_SCROLLLOCK",
    # Modifiers. The hook reports the sided virtual keys.
    0xA0: "KEY_LEFTSHIFT", 0xA1: "KEY_RIGHTSHIFT",
    0xA2: "KEY_LEFTCTRL", 0xA3: "KEY_RIGHTCTRL",
    0xA4: "KEY_LEFTALT", 0xA5: "KEY_RIGHTALT",
    0x5B: "KEY_LEFTMETA", 0x5C: "KEY_RIGHTMETA",
    # Numpad with NumLock on.
    0x60: "KEY_KP0", 0x61: "KEY_KP1", 0x62: "KEY_KP2", 0x63: "KEY_KP3", 0x64: "KEY_KP4",
    0x65: "KEY_KP5", 0x66: "KEY_KP6", 0x67: "KEY_KP7", 0x68: "KEY_KP8", 0x69: "KEY_KP9",
    0x6A: "KEY_KPASTERISK", 0x6B: "KEY_KPPLUS", 0x6D: "KEY_KPMINUS",
    0x6E: "KEY_KPDOT", 0x6F: "KEY_KPSLASH",
    # Media.
    0xAD: "KEY_MUTE", 0xAE: "KEY_VOLUMEDOWN", 0xAF: "KEY_VOLUMEUP",
    0xB0: "KEY_NEXTSONG", 0xB1: "KEY_PREVIOUSSONG", 0xB2: "KEY_STOPCD", 0xB3: "KEY_PLAYPAUSE",
}
_VK_TO_KEY.update({0x70 + i: f"KEY_F{i + 1}" for i in range(24)})  # F1-F24

# Navigation keys exist twice: the dedicated block (extended flag set) and
# the numpad with NumLock off (no extended flag). The browser calls the
# latter Numpad7 and so on whatever NumLock says, so they map to KEY_KP*.
_VK_NAV: dict[int, tuple[str, str]] = {
    0x2D: ("KEY_INSERT", "KEY_KP0"),
    0x23: ("KEY_END", "KEY_KP1"),
    0x28: ("KEY_DOWN", "KEY_KP2"),
    0x22: ("KEY_PAGEDOWN", "KEY_KP3"),
    0x25: ("KEY_LEFT", "KEY_KP4"),
    0x0C: ("KEY_KP5", "KEY_KP5"),  # VK_CLEAR, numpad 5 with NumLock off
    0x27: ("KEY_RIGHT", "KEY_KP6"),
    0x24: ("KEY_HOME", "KEY_KP7"),
    0x26: ("KEY_UP", "KEY_KP8"),
    0x21: ("KEY_PAGEUP", "KEY_KP9"),
    0x2E: ("KEY_DELETE", "KEY_KPDOT"),
    0x0D: ("KEY_KPENTER", "KEY_ENTER"),  # extended Enter is the numpad one
}

_LLKHF_EXTENDED = 0x01


def key_name_for(vk: int, scan: int, extended: bool) -> Optional[str]:
    """The evdev name for one hook event, or None for a key Vice ignores."""
    nav = _VK_NAV.get(vk)
    if nav:
        return nav[0] if extended else nav[1]
    named = _VK_TO_KEY.get(vk)
    if named:
        return named
    if not extended:
        return _SCAN_TO_KEY.get(scan)
    return None


def list_available_keys() -> list[str]:
    """Every key name this listener can report."""
    names = set(_SCAN_TO_KEY.values()) | set(_VK_TO_KEY.values())
    for pair in _VK_NAV.values():
        names.update(pair)
    return sorted(names)


# ── Hook plumbing ────────────────────────────────────────────────────────────

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_TIMER = 0x0113
WM_QUIT = 0x0012
HC_ACTION = 0

# Windows silently unhooks a low-level hook whose callback runs past
# LowLevelHooksTimeout, and says nothing. A GIL stall at the wrong moment
# could do that, so the hook is quietly reinstalled this often.
REHOOK_INTERVAL_MS = 5 * 60 * 1000


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


_LRESULT = ctypes.c_ssize_t
_HOOKPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = _LRESULT
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
    user32.SetTimer.restype = ctypes.c_size_t
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    return user32, kernel32


class HotkeyListener(HotkeyDispatcher):
    def __init__(self) -> None:
        super().__init__()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._hook_ready = threading.Event()
        self._hook_ok = False
        # Keys currently down, so the hook's auto-repeat keydowns are
        # reported as holds instead of fresh presses.
        self._down: set[str] = set()

    async def start(self) -> None:
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._hook_ready.clear()
        self._thread = threading.Thread(target=self._pump, name="vice-hotkeys", daemon=True)
        self._thread.start()
        await asyncio.to_thread(self._hook_ready.wait, 5.0)
        self._set_available(self._hook_ok)
        if not self._hook_ok:
            log.error("Could not install the keyboard hook, hotkeys will not work")

    async def stop(self) -> None:
        self._running = False
        self._cancel_pending()
        if self._thread and self._thread_id:
            user32, _ = _user32()
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            await asyncio.to_thread(self._thread.join, 2.0)
        self._thread = None
        self._thread_id = 0
        self._down.clear()
        self._held_mods.clear()

    # Runs on the hook thread.
    def _pump(self) -> None:
        user32, kernel32 = _user32()
        self._thread_id = kernel32.GetCurrentThreadId()
        hook = wintypes.HHOOK()

        @_HOOKPROC
        def _callback(n_code, w_param, l_param):
            if n_code == HC_ACTION:
                try:
                    info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    self._on_raw(int(w_param), info.vkCode, info.scanCode, info.flags)
                except Exception:
                    # Never let an exception escape into the hook chain.
                    log.exception("Keyboard hook callback failed")
            return user32.CallNextHookEx(hook, n_code, w_param, l_param)

        module = kernel32.GetModuleHandleW(None)

        def _install() -> bool:
            handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _callback, module, 0)
            if not handle:
                log.error("SetWindowsHookExW failed: error %d", ctypes.get_last_error())
                return False
            hook.value = handle
            return True

        self._hook_ok = _install()
        self._hook_ready.set()
        if not self._hook_ok:
            return
        log.info("Listening for hotkeys with a low-level keyboard hook")
        user32.SetTimer(None, 0, REHOOK_INTERVAL_MS, None)

        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_TIMER:
                    user32.UnhookWindowsHookEx(hook)
                    if not _install():
                        self._report_available(False)
                        break
        finally:
            if hook:
                user32.UnhookWindowsHookEx(hook)

    def _report_available(self, value: bool) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._set_available, value)

    def _on_raw(self, message: int, vk: int, scan: int, flags: int) -> None:
        """Hook thread: translate and forward. Must stay fast, Windows drops
        a hook that makes the whole desktop wait on it."""
        if not self._running or self._loop is None:
            return
        name = key_name_for(vk, scan, bool(flags & _LLKHF_EXTENDED))
        if not name:
            return
        if message in (WM_KEYDOWN, WM_SYSKEYDOWN):
            state = KEY_HOLD if name in self._down else KEY_DOWN
            self._down.add(name)
        elif message in (WM_KEYUP, WM_SYSKEYUP):
            self._down.discard(name)
            state = KEY_UP
        else:
            return
        self._loop.call_soon_threadsafe(self._dispatch, name, state)

    def _dispatch(self, name: str, state: int) -> None:
        asyncio.ensure_future(self._key_event(name, state))


def can_access_hotkeys() -> bool:
    """A low-level hook needs no permission on Windows."""
    return True
