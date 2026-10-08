from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from vice import app as vice_app
from vice.tray import WindowTrayController, _ActivationServer, request_window_activation


class _Events:
    class _Event:
        def __iadd__(self, callback):
            return self

    def __init__(self) -> None:
        self.before_show = self._Event()
        self.closing = self._Event()
        self.closed = self._Event()


class _Window:
    def __init__(self) -> None:
        self.events = _Events()
        self.hidden = 0
        self.destroyed = 0
        self.shown = 0
        self.restored = 0
        self.native = object()

    def hide(self) -> None:
        self.hidden += 1

    def destroy(self) -> None:
        self.destroyed += 1

    def show(self) -> None:
        self.shown += 1

    def restore(self) -> None:
        self.restored += 1


def _controller(win: _Window, socket_path: Path) -> WindowTrayController:
    return WindowTrayController(
        win=win,
        socket_path=socket_path,
        icon_paths=(),
        shutdown_daemon=mock.Mock(),
        close_window=win.destroy,
        logger=mock.Mock(),
    )


class ActivationTests(unittest.TestCase):
    def test_activation_server_forwards_show_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "vice-app.sock"
            shown = threading.Event()
            server = _ActivationServer(socket_path, shown.set, mock.Mock())
            server.start()
            try:
                self.assertTrue(request_window_activation(socket_path))
                self.assertTrue(shown.wait(1.5))
            finally:
                server.close()

    def test_activation_returns_false_without_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "vice-app.sock"
            self.assertFalse(request_window_activation(socket_path, timeout=0.05))


class LifecycleTests(unittest.TestCase):
    def test_close_hides_only_when_tray_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            win = _Window()
            controller = _controller(win, Path(tmp) / "vice-app.sock")
            controller._tray_available = True
            self.assertFalse(controller._on_closing())
            self.assertEqual(win.hidden, 1)
            self.assertEqual(win.destroyed, 0)

    def test_close_is_unchanged_without_tray_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            win = _Window()
            controller = _controller(win, Path(tmp) / "vice-app.sock")
            self.assertIsNone(controller._on_closing())
            self.assertEqual(win.hidden, 0)

    def test_minimize_preserves_upstream_behavior_without_tray(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            win = _Window()
            controller = _controller(win, Path(tmp) / "vice-app.sock")
            controller.keep_running()
            self.assertEqual(win.destroyed, 1)
            self.assertEqual(win.hidden, 0)

    def test_minimize_hides_when_tray_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            win = _Window()
            controller = _controller(win, Path(tmp) / "vice-app.sock")
            controller._tray_available = True
            controller.keep_running()
            self.assertEqual(win.hidden, 1)
            self.assertEqual(win.destroyed, 0)

    def test_shutdown_uses_injected_application_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            win = _Window()
            shutdown = mock.Mock()
            controller = WindowTrayController(
                win=win,
                socket_path=Path(tmp) / "vice-app.sock",
                icon_paths=(),
                shutdown_daemon=shutdown,
                close_window=win.destroy,
                logger=mock.Mock(),
            )
            controller._quitting = True
            controller._shutdown_worker()
            shutdown.assert_called_once_with()
            self.assertEqual(win.destroyed, 1)

    def test_shutdown_failure_keeps_window_open_and_allows_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            win = _Window()
            shutdown = mock.Mock(side_effect=RuntimeError("still shutting down"))
            logger = mock.Mock()
            controller = WindowTrayController(
                win=win,
                socket_path=Path(tmp) / "vice-app.sock",
                icon_paths=(),
                shutdown_daemon=shutdown,
                close_window=win.destroy,
                logger=logger,
            )
            controller._quitting = True

            controller._shutdown_worker()

            shutdown.assert_called_once_with()
            self.assertEqual(win.destroyed, 0)
            self.assertFalse(controller._quitting)
            logger.exception.assert_called_once_with(
                "Could not fully stop Vice during quit"
            )

    def test_translated_labels_are_dispatched_without_recreating_tray(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            win = _Window()
            controller = _controller(win, Path(tmp) / "vice-app.sock")
            controller._dispatcher = mock.Mock()
            controller.set_labels("Ouvrir Vice", "Quitter Vice")
            self.assertEqual(controller._open_label, "Ouvrir Vice")
            self.assertEqual(controller._quit_label, "Quitter Vice")
            controller._dispatcher.labels_requested.emit.assert_called_once_with(
                "Ouvrir Vice", "Quitter Vice"
            )

    def test_backend_change_clears_previous_tray_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            win = _Window()
            controller = _controller(win, Path(tmp) / "vice-app.sock")
            old_tray = mock.Mock()
            old_native = object()
            controller._tray = old_tray
            controller._tray_available = True
            controller._native = old_native
            controller._dispatcher = object()
            controller._clear_tray()
            old_tray.hide.assert_called_once_with()
            self.assertFalse(controller._tray_available)
            self.assertIsNone(controller._tray)
            self.assertIsNone(controller._dispatcher)
            self.assertIsNone(controller._native)


class ShutdownPolicyTests(unittest.TestCase):
    def test_systemd_owned_daemon_stops_unit_then_ipc_and_waits(self) -> None:
        result = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(
            vice_app, "_systemd_unit_available", return_value=True
        ), mock.patch.object(
            vice_app.subprocess, "run", return_value=result
        ) as run, mock.patch.object(
            vice_app, "_stop_daemon"
        ) as stop, mock.patch.object(
            vice_app, "_wait_for_daemon_exit", return_value=True
        ) as wait:
            vice_app._stop_daemon_completely(timeout=0.25)

        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            ["systemctl", "--user", "stop", "vice.service"],
        )
        stop.assert_called_once_with()
        wait.assert_called_once_with(timeout=0.25)

    def test_direct_daemon_stops_without_systemctl(self) -> None:
        with mock.patch.object(
            vice_app, "_systemd_unit_available", return_value=False
        ), mock.patch.object(
            vice_app.subprocess, "run"
        ) as run, mock.patch.object(
            vice_app, "_stop_daemon"
        ) as stop, mock.patch.object(
            vice_app, "_wait_for_daemon_exit", return_value=True
        ) as wait:
            vice_app._stop_daemon_completely(timeout=0.25)

        run.assert_not_called()
        stop.assert_called_once_with()
        wait.assert_called_once_with(timeout=0.25)

    def test_shutdown_timeout_raises(self) -> None:
        with mock.patch.object(
            vice_app, "_systemd_unit_available", return_value=False
        ), mock.patch.object(
            vice_app, "_stop_daemon"
        ), mock.patch.object(
            vice_app, "_wait_for_daemon_exit", return_value=False
        ):
            with self.assertRaisesRegex(RuntimeError, "did not finish shutting down"):
                vice_app._stop_daemon_completely(timeout=0.25)


if __name__ == "__main__":
    unittest.main()
