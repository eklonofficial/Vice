"""Native Qt lifecycle regressions.

Run these tests with the Qt pywebview dependencies:

    VICE_RUN_NATIVE_QT_TESTS=1 python -m unittest discover -s tests -p test_tray_native.py

They are opt-in because importing QtWebEngine can abort the interpreter when a
host has no usable Qt platform plugin; that cannot be expressed as a normal
unittest skip from inside the process. The child uses Qt's offscreen platform
to provide a real native window and a deterministic absence of a tray host.
"""

from __future__ import annotations

import fcntl
import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


_NATIVE_QT_SCRIPT = r"""
import fcntl
import logging
import sys
from pathlib import Path

import webview

from vice.app import _close_window_after_bridge
from vice.tray import WindowTrayController


root = Path(sys.argv[1])
lock_path = root / "vice-app.pid"
lock_handle = lock_path.open("a+")
fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


class API:
    def __init__(self):
        self.controller = None

    def keep_running(self):
        if self.controller._tray_available:
            raise RuntimeError("the native test requires a desktop without a tray host")
        print("NO_TRAY", flush=True)
        self.controller.keep_running()


api = API()
html = '''
<!doctype html>
<html>
  <body>
    <script>
      window.addEventListener('pywebviewready', async () => {
        await window.pywebview.api.keep_running();
      });
    </script>
  </body>
</html>
'''
win = webview.create_window("Vice native lifecycle test", html=html, js_api=api)
controller = WindowTrayController(
    win=win,
    socket_path=root / "vice-app.sock",
    icon_paths=(),
    shutdown_daemon=lambda: None,
    close_window=lambda: _close_window_after_bridge(win),
    logger=logging.getLogger("vice-native-test"),
)
api.controller = controller
controller.start()
webview.start(gui="qt", debug=False, private_mode=True)
print("EXITED", flush=True)
"""


def _qt_test_available() -> bool:
    if os.environ.get("VICE_RUN_NATIVE_QT_TESTS") != "1":
        return False
    try:
        return all(
            importlib.util.find_spec(module) is not None
            for module in ("webview", "PyQt6.QtWebEngineWidgets", "qtpy")
        )
    except ModuleNotFoundError:
        return False


@unittest.skipUnless(
    _qt_test_available(),
    "set VICE_RUN_NATIVE_QT_TESTS=1 with pywebview Qt installed",
)
class NativeQtLifecycleTests(unittest.TestCase):
    def test_keep_running_without_tray_returns_before_native_window_closes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env.pop("DISPLAY", None)
            env.pop("DBUS_SESSION_BUS_ADDRESS", None)
            env["QT_API"] = "pyqt6"
            env["QT_QPA_PLATFORM"] = "offscreen"
            env["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"
            result = subprocess.run(
                [sys.executable, "-c", textwrap.dedent(_NATIVE_QT_SCRIPT), tmp],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

            self.assertEqual(
                result.returncode,
                0,
                f"native Qt child failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn("NO_TRAY", result.stdout)
            self.assertIn("EXITED", result.stdout)

            lock_path = Path(tmp) / "vice-app.pid"
            with lock_path.open("a+") as lock_handle:
                try:
                    fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    self.fail(f"native window exit left the app lock held: {exc}")


if __name__ == "__main__":
    unittest.main()
