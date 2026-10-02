"""Main window: tabs, status bar, tray icon and the snapshot polling timer."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtWidgets import QLabel, QMainWindow, QMenu, QMessageBox, QSystemTrayIcon, QTabWidget

from app import APP_NAME
from app.gui.audio_tab import AudioTab
from app.gui.dashboard import Dashboard
from app.gui.devices_tab import DevicesTab
from app.gui.diagnostics_tab import DiagnosticsTab
from app.gui.network_tab import NetworkTab
from app.gui.settings_tab import SettingsTab
from app.gui.widgets import StatusDot

log = logging.getLogger(__name__)

POLL_MS = 80


class MainWindow(QMainWindow):
    def __init__(self, core, icon: QIcon):
        super().__init__()
        self.core = core
        self.quitting = False
        self._last_notification = 0.0
        self._error_shown = False
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icon)
        self.resize(1180, 820)
        self.setMinimumSize(900, 620)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.dashboard = Dashboard(core)
        self.devices = DevicesTab(core)
        self.audio = AudioTab(core)
        self.network = NetworkTab(core)
        self.settings = SettingsTab(core)
        self.diagnostics = DiagnosticsTab(core)
        for w, name in ((self.dashboard, "Dashboard"), (self.devices, "Devices"), (self.audio, "Audio"),
                        (self.network, "Network"), (self.settings, "Settings"), (self.diagnostics, "Diagnostics")):
            self.tabs.addTab(w, name)
        self.dashboard.open_tab.connect(self.show_tab)
        self.setCentralWidget(self.tabs)

        sb = self.statusBar()
        self.sb_server = StatusDot("Starting…", "busy")
        self.sb_clients = QLabel()
        self.sb_code = QLabel()
        self.sb_msg = QLabel()
        sb.addWidget(self.sb_server)
        sb.addWidget(self.sb_msg, 1)
        sb.addPermanentWidget(self.sb_clients)
        sb.addPermanentWidget(self.sb_code)

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(icon, self)
            menu = QMenu()
            self.tray_status = QAction("Starting…", self)
            self.tray_status.setEnabled(False)
            menu.addAction(self.tray_status)
            menu.addSeparator()
            show = QAction("Show window", self)
            show.triggered.connect(self.show_normal)
            menu.addAction(show)
            self.tray_mute = QAction("Mute virtual microphone", self)
            self.tray_mute.setCheckable(True)
            self.tray_mute.toggled.connect(lambda v: self.core.set_setting(master_muted=v))
            menu.addAction(self.tray_mute)
            menu.addSeparator()
            quit_action = QAction("Quit", self)
            quit_action.triggered.connect(self.quit)
            menu.addAction(quit_action)
            self.tray.setContextMenu(menu)
            self.tray.setToolTip(APP_NAME)
            self.tray.activated.connect(self._tray_activated)
            self.tray.show()

        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    # ------------------------------------------------------------------
    def show_tab(self, name: str) -> None:
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == name:
                self.tabs.setCurrentIndex(i)

    def show_normal(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.show_normal()

    def refresh(self) -> None:
        try:
            snap = self.core.snapshot()
        except Exception:  # noqa: BLE001 - never let the GUI die on a snapshot race
            log.debug("snapshot failed", exc_info=True)
            return
        # Only repaint the visible tab (plus the status bar) to keep CPU low.
        current = self.tabs.currentWidget()
        visible = self.isVisible() and not self.isMinimized()
        if visible:
            try:
                current.update_snapshot(snap)
            except Exception:  # noqa: BLE001
                log.exception("GUI update failed")
        srv = snap["server"]
        if snap["starting"] and not snap["started"]:
            self.sb_server.set("busy", "Starting…")
        elif srv["running"]:
            self.sb_server.set("ok", f"Server running · port {srv['http_port']}")
        else:
            self.sb_server.set("error", "Server stopped")
        n = snap["connected_count"]
        self.sb_clients.setText(f"{n} device{'s' if n != 1 else ''} connected   ")
        self.sb_code.setText(f"Pairing code {snap['auth']['formatted']}" if snap["auth"]["enabled"] else "Pairing off")
        notes = snap["notifications"]
        if notes and notes[-1][0] > self._last_notification:
            for ts, level, text in notes:
                if ts <= self._last_notification:
                    continue
                self.sb_msg.setText(text)
                self.sb_msg.setStyleSheet("color:#ef5350" if level == "error" else "")
                if level == "error" and self.tray and not visible:
                    self.tray.showMessage(APP_NAME, text, QSystemTrayIcon.Warning, 5000)
            self._last_notification = notes[-1][0]
        if self.tray:
            self.tray_status.setText(f"{n} connected · {snap['audio']['source']['description']}")
            self.tray.setToolTip(f"{APP_NAME}\n{n} device(s) connected\nPairing code {snap['auth']['formatted']}")
            if self.tray_mute.isChecked() != snap["audio"]["master_muted"]:
                self.tray_mute.blockSignals(True)
                self.tray_mute.setChecked(snap["audio"]["master_muted"])
                self.tray_mute.blockSignals(False)
        if snap["started"] and not self._error_shown and (srv["error"] or snap["audio"]["error"]):
            self._error_shown = True
            msg = "\n\n".join(e for e in (srv["error"], snap["audio"]["error"]) if e)
            QTimer.singleShot(0, lambda: QMessageBox.warning(self, APP_NAME, msg))

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.quitting and self.tray is not None and self.core.store.settings.close_to_tray:
            event.ignore()
            self.hide()
            if not getattr(self, "_told_tray", False):
                self._told_tray = True
                self.tray.showMessage(APP_NAME, "Still running in the tray. The virtual microphone stays available.",
                                      QSystemTrayIcon.Information, 4000)
            return
        self.quitting = True
        event.accept()
        self.quit()

    def quit(self) -> None:
        self.quitting = True
        self.timer.stop()
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()

