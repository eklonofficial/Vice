"""System tray and existing-window activation for the Vice desktop app.

The tray implementation is intentionally backend-specific only at the edge:
Qt desktops use ``QSystemTrayIcon`` when a tray host is available, while
desktops/backends without tray support keep Vice's existing close behavior.
Window activation uses a small per-user Unix socket and is independent of the
compositor, so relaunching Vice can restore a hidden window on Wayland.
"""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable, Iterable


class _ActivationServer:
    """Receive requests from later ``vice-app`` launches to show the window."""

    def __init__(
        self,
        socket_path: Path,
        show_window: Callable[[], None],
        logger: Any,
    ) -> None:
        self._socket_path = socket_path
        self._show_window = show_window
        self._log = logger
        self._socket: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._socket_path.unlink(missing_ok=True)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self._socket_path))
        os.chmod(self._socket_path, 0o600)
        server.listen(4)
        server.settimeout(0.5)
        self._socket = server
        self._thread = threading.Thread(
            target=self._serve,
            name="vice-window-activation",
            daemon=True,
        )
        self._thread.start()
        self._log.info("Window activation socket ready at %s", self._socket_path)

    def _serve(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            with conn:
                try:
                    command = conn.recv(64).strip().lower()
                except OSError:
                    continue

            if command == b"show":
                try:
                    self._show_window()
                except Exception:
                    self._log.exception("Could not restore Vice window")

    def close(self) -> None:
        self._stop.set()
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        self._socket_path.unlink(missing_ok=True)


def request_window_activation(socket_path: Path, timeout: float = 0.75) -> bool:
    """Ask an existing Vice GUI process to restore its window."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(socket_path))
            sock.sendall(b"show\n")
        return True
    except OSError:
        return False


class WindowTrayController:
    """Own tray lifecycle without changing behavior on unsupported desktops."""

    def __init__(
        self,
        *,
        win: Any,
        socket_path: Path,
        icon_paths: Iterable[Path],
        shutdown_daemon: Callable[[], None],
        close_window: Callable[[], None],
        logger: Any,
    ) -> None:
        self._win = win
        self._icon_paths = tuple(icon_paths)
        self._shutdown_daemon = shutdown_daemon
        self._close_window = close_window
        self._log = logger
        self._server = _ActivationServer(socket_path, self.show_window, logger)
        self._tray_available = False
        self._tray: Any = None
        self._menu: Any = None
        self._dispatcher: Any = None
        self._native: Any = None
        self._open_action: Any = None
        self._quit_action: Any = None
        self._activated: Any = None
        self._open_label = "Open Vice"
        self._quit_label = "Quit Vice"
        self._quitting = False
        self._closed = False

    def start(self) -> None:
        """Attach public pywebview lifecycle hooks and start activation IPC."""
        try:
            self._server.start()
        except OSError as exc:
            self._log.warning("Could not create window activation socket: %s", exc)

        self._win.events.before_show += self._before_show
        self._win.events.closing += self._on_closing
        self._win.events.closed += self.close

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._clear_tray()
        self._server.close()

    def _clear_tray(self) -> None:
        """Drop tray objects when the native backend changes or closes."""
        if self._tray is not None:
            try:
                self._tray.hide()
            except Exception:
                # The Qt C++ object may already have been deleted during a
                # failed backend startup. Clearing Python state is enough.
                pass
        self._tray_available = False
        self._tray = None
        self._menu = None
        self._dispatcher = None
        self._native = None
        self._open_action = None
        self._quit_action = None
        self._activated = None

    def _before_show(self) -> None:
        """Install the Qt tray once ``window.native`` is publicly available."""
        try:
            from qtpy import QtCore
            from qtpy.QtGui import QIcon
            from qtpy.QtWidgets import QMainWindow, QMenu, QSystemTrayIcon
        except ImportError:
            self._log.debug("Qt tray integration unavailable on this webview backend")
            return

        native = self._win.native
        if native is self._native and self._tray_available:
            return
        if self._native is not None and native is not self._native:
            self._clear_tray()
        self._native = native

        if not isinstance(native, QMainWindow):
            self._log.debug(
                "Tray integration skipped for non-Qt native window %s",
                type(native).__name__,
            )
            return

        if not QSystemTrayIcon.isSystemTrayAvailable():
            # Crucial for GNOME/other sessions without a tray host: do not
            # turn the X button into a hide operation with nowhere to restore
            # the window from. Existing upstream close behavior remains intact.
            self._log.info("No system tray host detected; tray behavior disabled")
            return

        controller = self

        class _QtDispatcher(QtCore.QObject):
            show_requested = QtCore.Signal()
            hide_requested = QtCore.Signal()
            close_requested = QtCore.Signal()
            labels_requested = QtCore.Signal(str, str)

            def __init__(self) -> None:
                super().__init__(native)
                self.show_requested.connect(self.show_window)
                self.hide_requested.connect(native.hide)
                self.close_requested.connect(self.close_window)
                self.labels_requested.connect(self.set_labels)

            @QtCore.Slot()
            def show_window(self) -> None:
                native.showNormal()
                native.show()
                native.raise_()
                native.activateWindow()
                handle = native.windowHandle()
                if handle is not None:
                    try:
                        handle.requestActivate()
                    except Exception:
                        pass

            @QtCore.Slot()
            def close_window(self) -> None:
                if controller._tray is not None:
                    controller._tray.hide()
                native.close()

            @QtCore.Slot(str, str)
            def set_labels(self, open_label: str, quit_label: str) -> None:
                if controller._open_action is not None:
                    controller._open_action.setText(open_label)
                if controller._quit_action is not None:
                    controller._quit_action.setText(quit_label)

        self._dispatcher = _QtDispatcher()
        icon = self._load_icon(QIcon)
        if icon.isNull():
            icon = native.windowIcon()
        else:
            native.setWindowIcon(icon)

        menu = QMenu(native)
        open_action = menu.addAction(self._open_label)
        open_action.triggered.connect(self.show_window)
        menu.addSeparator()
        quit_action = menu.addAction(self._quit_label)
        quit_action.triggered.connect(self.quit)

        tray = QSystemTrayIcon(icon, native)
        tray.setToolTip("Vice")
        tray.setContextMenu(menu)

        def _activated(reason: Any) -> None:
            reasons = QSystemTrayIcon.ActivationReason
            if reason in (reasons.Trigger, reasons.DoubleClick):
                self.show_window()

        tray.activated.connect(_activated)
        tray.show()

        # Keep Python wrappers alive for the lifetime of the native window.
        self._tray = tray
        self._menu = menu
        self._open_action = open_action
        self._quit_action = quit_action
        self._activated = _activated
        self._tray_available = True
        self._log.info("Vice system tray icon enabled")

    def _load_icon(self, QIcon: Any) -> Any:
        for path in self._icon_paths:
            if path.exists():
                icon = QIcon(str(path))
                if not icon.isNull():
                    self._log.debug("Using Vice tray icon from %s", path)
                    return icon
        return QIcon.fromTheme("vice")

    def _on_closing(self) -> bool | None:
        """X hides to tray only when a real tray host exists."""
        if self._quitting or not self._tray_available:
            return None
        self.hide_window()
        return False

    def show_window(self) -> None:
        if self._dispatcher is not None:
            self._dispatcher.show_requested.emit()
            return
        # Public pywebview methods are safe for activation requests even
        # before the Qt before_show hook has run.
        try:
            self._win.restore()
        except Exception:
            self._log.debug("win.restore() failed while activating", exc_info=True)
        try:
            self._win.show()
        except Exception:
            self._log.debug("win.show() failed while activating", exc_info=True)

    def hide_window(self) -> None:
        if self._dispatcher is not None:
            self._dispatcher.hide_requested.emit()
            return
        self._win.hide()

    def keep_running(self) -> None:
        """In-app Minimize: hide to tray, or preserve upstream close behavior."""
        if self._tray_available:
            self.hide_window()
        else:
            self._close_window()

    def set_labels(self, open_label: str, quit_label: str) -> None:
        """Store translated action labels and update Qt actions on its thread."""
        if open_label:
            self._open_label = str(open_label)
        if quit_label:
            self._quit_label = str(quit_label)
        if self._dispatcher is not None:
            try:
                self._dispatcher.labels_requested.emit(self._open_label, self._quit_label)
            except Exception:
                self._log.debug("Could not update tray action labels", exc_info=True)

    def quit(self) -> None:
        """Stop recorder ownership completely, then close the GUI and tray."""
        if self._quitting:
            return
        self._quitting = True
        threading.Thread(
            target=self._shutdown_worker,
            name="vice-tray-quit",
            daemon=True,
        ).start()

    def _shutdown_worker(self) -> None:
        try:
            self._shutdown_daemon()
        except Exception:
            self._log.exception("Could not fully stop Vice during quit")
            self._quitting = False
            return

        if self._dispatcher is not None:
            self._dispatcher.close_requested.emit()
        else:
            self._close_window()
