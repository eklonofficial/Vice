import asyncio
import unittest
from unittest import mock

from evdev import InputEvent, ecodes

from vice import main as main_mod
from vice.config import (Config, HotkeyClipPreset, HotkeyConfig, normalize_combo,
                         validate_hotkeys)
from vice.hotkey import HotkeyListener


def _key_event(name: str, value: int) -> InputEvent:
    """Build an evdev EV_KEY event by key name (value: 1=down, 0=up)."""
    return InputEvent(0, 0, ecodes.EV_KEY, ecodes.ecodes[name], value)


class _FakeDevice:
    """Stands in for evdev.InputDevice."""

    def __init__(self, path: str, *, dies: bool = False, events=None) -> None:
        self.path = path
        self.name = f"fake-{path}"
        self.closed = False
        self._dies = dies
        self._events = list(events or [])

    async def async_read_loop(self):
        if self._dies:
            raise OSError(19, "No such device")
        for ev in self._events:
            yield ev
        while True:
            await asyncio.sleep(3600)
            yield  # pragma: no cover, never reached

    def close(self) -> None:
        self.closed = True


class HotkeyHotplugTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_reattaches_after_keyboard_replug(self) -> None:
        """Regression test for #65: a disconnected keyboard must not leave
        the listener stuck until a daemon restart."""
        dying = _FakeDevice("/dev/input/event2", dies=True)
        replug = _FakeDevice("/dev/input/event5")
        # Scan results over time: device present, gone, replugged.
        scans = [[dying], [], [replug]]

        def _fake_find(skip_paths=None):
            batch = scans.pop(0) if scans else []
            skip = skip_paths or set()
            return [d for d in batch if d.path not in skip]

        availability: list[bool] = []
        listener = HotkeyListener()
        listener.on_availability_change = availability.append

        with mock.patch("vice.hotkey._find_keyboards", side_effect=_fake_find):
            with mock.patch("vice.hotkey.RESCAN_INTERVAL", 0.03):
                await listener.start()
                self.assertTrue(listener.available)

                # Wait until the supervisor has reaped the dead device and
                # attached the replugged one.
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if replug.path in listener._listeners and listener.available:
                        break
                self.assertIn(replug.path, listener._listeners)
                self.assertNotIn(dying.path, listener._listeners)
                self.assertTrue(listener.available)
                await listener.stop()

        # Availability flapped: down when the keyboard died, up on replug.
        self.assertIn(False, availability)
        self.assertEqual(availability[-1], True)
        self.assertTrue(dying.closed)

    async def test_start_without_keyboards_keeps_watching(self) -> None:
        keyboard = _FakeDevice("/dev/input/event3")
        scans = [[], [keyboard]]

        def _fake_find(skip_paths=None):
            batch = scans.pop(0) if scans else []
            skip = skip_paths or set()
            return [d for d in batch if d.path not in skip]

        listener = HotkeyListener()
        with mock.patch("vice.hotkey._find_keyboards", side_effect=_fake_find):
            with mock.patch("vice.hotkey.RESCAN_INTERVAL", 0.03):
                await listener.start()
                self.assertFalse(listener.available)

                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if listener.available:
                        break
                self.assertTrue(listener.available)
                await listener.stop()

    async def test_stop_cancels_supervisor_and_listeners(self) -> None:
        keyboard = _FakeDevice("/dev/input/event4")

        def _fake_find(skip_paths=None):
            skip = skip_paths or set()
            return [keyboard] if keyboard.path not in skip else []

        listener = HotkeyListener()
        with mock.patch("vice.hotkey._find_keyboards", side_effect=_fake_find):
            await listener.start()
            supervisor = listener._supervisor
            await listener.stop()

        self.assertEqual(listener._listeners, {})
        self.assertIsNone(listener._supervisor)
        self.assertTrue(supervisor.cancelled() or supervisor.done())
        self.assertTrue(keyboard.closed)


class ComboDispatchTests(unittest.IsolatedAsyncioTestCase):
    """Issue #101: hold a modifier so Alt+F9 is its own binding."""

    async def _run_events(self, binding: str, events) -> int:
        fired = 0

        async def cb() -> None:
            nonlocal fired
            fired += 1

        device = _FakeDevice("/dev/input/event9", events=events)
        listener = HotkeyListener()
        listener.on(binding, cb)

        def _fake_find(skip_paths=None):
            skip = skip_paths or set()
            return [device] if device.path not in skip else []

        with mock.patch("vice.hotkey._find_keyboards", side_effect=_fake_find):
            await listener.start()
            for _ in range(50):
                await asyncio.sleep(0.01)
                if fired:
                    break
            await listener.stop()
        return fired

    async def test_modifier_plus_key_fires_combo(self) -> None:
        events = [_key_event("KEY_LEFTALT", 1), _key_event("KEY_F9", 1)]
        self.assertEqual(await self._run_events("KEY_LEFTALT+KEY_F9", events), 1)

    async def test_right_modifier_matches_left_binding(self) -> None:
        events = [_key_event("KEY_RIGHTALT", 1), _key_event("KEY_F9", 1)]
        self.assertEqual(await self._run_events("KEY_LEFTALT+KEY_F9", events), 1)

    async def test_press_order_independent(self) -> None:
        events = [
            _key_event("KEY_LEFTSHIFT", 1),
            _key_event("KEY_LEFTCTRL", 1),
            _key_event("KEY_F9", 1),
        ]
        self.assertEqual(
            await self._run_events("KEY_LEFTCTRL+KEY_LEFTSHIFT+KEY_F9", events), 1
        )

    async def test_bare_key_still_fires(self) -> None:
        # Regression guard: single-key configs keep working untouched.
        events = [_key_event("KEY_F9", 1)]
        self.assertEqual(await self._run_events("KEY_F9", events), 1)

    async def test_modifier_held_does_not_fire_bare_binding(self) -> None:
        # Alt+F9 must not trigger a plain F9 binding, they are distinct.
        events = [_key_event("KEY_LEFTALT", 1), _key_event("KEY_F9", 1)]
        self.assertEqual(await self._run_events("KEY_F9", events), 0)

    async def test_modifier_release_clears_held_state(self) -> None:
        # Release Alt before F9 → plain F9 fires.
        events = [
            _key_event("KEY_LEFTALT", 1),
            _key_event("KEY_LEFTALT", 0),
            _key_event("KEY_F9", 1),
        ]
        self.assertEqual(await self._run_events("KEY_F9", events), 1)


class NormalizeComboTests(unittest.TestCase):
    def test_bare_key_unchanged(self) -> None:
        self.assertEqual(normalize_combo("KEY_F9"), "KEY_F9")

    def test_modifier_order_canonical(self) -> None:
        self.assertEqual(
            normalize_combo("KEY_F9+KEY_LEFTSHIFT+KEY_LEFTCTRL"),
            "KEY_LEFTCTRL+KEY_LEFTSHIFT+KEY_F9",
        )

    def test_right_modifier_folds_to_left(self) -> None:
        self.assertEqual(
            normalize_combo("KEY_RIGHTALT+KEY_F9"), "KEY_LEFTALT+KEY_F9"
        )

    def test_duplicate_modifier_collapsed(self) -> None:
        self.assertEqual(
            normalize_combo("KEY_LEFTALT+KEY_RIGHTALT+KEY_F9"),
            "KEY_LEFTALT+KEY_F9",
        )

    def test_empty_is_empty(self) -> None:
        self.assertEqual(normalize_combo(""), "")

    def test_validate_hotkeys_dedups_reordered_combos(self) -> None:
        hk = HotkeyConfig(
            clip="KEY_LEFTALT+KEY_F9",
            clip_presets=[HotkeyClipPreset(key="KEY_F9+KEY_LEFTALT", duration=30)],
        )
        with self.assertRaises(ValueError):
            validate_hotkeys(hk)


class FocusedAppSuppressionTests(unittest.IsolatedAsyncioTestCase):
    """#130: some games clip on the same keys, so Vice has to stand down while
    they are focused. Only the keyboard path is affected."""

    @staticmethod
    def _daemon(matches: list[str]) -> main_mod.ViceDaemon:
        cfg = Config(hotkeys=HotkeyConfig(clip="KEY_F9", disable_while_focused=matches))
        with mock.patch("vice.main.load_config", return_value=cfg), \
             mock.patch("vice.main.create_recorder"), \
             mock.patch("vice.main.HotkeyListener"), \
             mock.patch("vice.main.can_access_hotkeys", return_value=True):
            return main_mod.ViceDaemon()

    async def test_no_matches_skips_window_detection_entirely(self) -> None:
        daemon = self._daemon([])
        with mock.patch("vice.active_window.get_active_window") as detect:
            self.assertFalse(await daemon._hotkeys_suppressed())
        detect.assert_not_called()

    async def test_focused_match_suppresses(self) -> None:
        daemon = self._daemon(["ggst.exe"])
        win = {"process": "GGST.exe", "class": "steam_app_1384160", "pid": 7}
        with mock.patch("vice.active_window.get_active_window", return_value=win):
            self.assertTrue(await daemon._hotkeys_suppressed())

    async def test_matching_is_case_insensitive_and_checks_the_class(self) -> None:
        daemon = self._daemon(["STEAM_APP_1384160"])
        win = {"process": "wine", "class": "steam_app_1384160", "pid": 7}
        with mock.patch("vice.active_window.get_active_window", return_value=win):
            self.assertTrue(await daemon._hotkeys_suppressed())

    async def test_other_app_focused_does_not_suppress(self) -> None:
        daemon = self._daemon(["ggst.exe"])
        win = {"process": "firefox", "class": "firefox", "pid": 7}
        with mock.patch("vice.active_window.get_active_window", return_value=win):
            self.assertFalse(await daemon._hotkeys_suppressed())

    async def test_detection_failure_leaves_hotkeys_working(self) -> None:
        daemon = self._daemon(["ggst.exe"])
        with mock.patch("vice.active_window.get_active_window", side_effect=OSError):
            self.assertFalse(await daemon._hotkeys_suppressed())


if __name__ == "__main__":
    unittest.main()
