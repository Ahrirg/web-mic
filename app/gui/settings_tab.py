"""Settings tab: startup, network, audio format, buffer, authentication, naming."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app import APP_ID
from app.config.settings import BUFFER_CHOICES_MS
from app.gui.widgets import Card, muted_label, run_in_background

AUTOSTART = Path.home() / ".config" / "autostart" / f"{APP_ID}.desktop"


def autostart_enabled() -> bool:
    return AUTOSTART.exists()


def set_autostart(on: bool) -> None:
    if not on:
        AUTOSTART.unlink(missing_ok=True)
        return
    launcher = shutil.which("phone-mic-router")
    exec_line = f"{launcher} --minimized" if launcher else f"{sys.executable} -m app --minimized"
    root = Path(__file__).resolve().parent.parent.parent
    AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART.write_text(
        "[Desktop Entry]\nType=Application\nName=Phone Mic Router\n"
        f"Exec={exec_line}\nPath={root}\nIcon=audio-input-microphone\n"
        "X-GNOME-Autostart-enabled=true\nComment=Phone browser microphones for Linux\n"
    )


class SettingsTab(QWidget):
    def __init__(self, core, parent=None):
        super().__init__(parent)
        self.core = core
        self._aliases_key = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        grid = QGridLayout(body)
        grid.setContentsMargins(16, 16, 16, 16)
        grid.setSpacing(14)
        s = core.store.settings

        c = Card("Startup")
        self.minimized = QCheckBox("Start minimized to the system tray")
        self.minimized.setChecked(s.start_minimized)
        self.tray = QCheckBox("Closing the window keeps the app running in the tray")
        self.tray.setChecked(s.close_to_tray)
        self.autostart = QCheckBox("Start automatically when I log in")
        self.autostart.setChecked(autostart_enabled())
        self.adb_auto = QCheckBox("Enable USB microphone automatically for authorized Android devices")
        self.adb_auto.setChecked(s.adb_auto_reverse)
        self.regen = QCheckBox("Generate a new pairing code at every start")
        self.regen.setChecked(s.regenerate_code_on_start)
        for w in (self.minimized, self.tray, self.autostart, self.adb_auto, self.regen):
            c.body.addWidget(w)
        c.body.addStretch(1)
        grid.addWidget(c, 0, 0)

        c = Card("Network")
        f = QFormLayout()
        self.port = QSpinBox()
        self.port.setRange(1025, 65535)
        self.port.setValue(s.port)
        self.https_port = QSpinBox()
        self.https_port.setRange(1025, 65535)
        self.https_port.setValue(s.https_port)
        self.https = QCheckBox("Serve HTTPS (needed for Wi-Fi microphones)")
        self.https.setChecked(s.https_enabled)
        self.host = QLineEdit(s.host)
        self.host.setToolTip("0.0.0.0 listens on every interface (LAN + USB). 127.0.0.1 allows only USB/ADB and this PC.")
        self.ipv6 = QCheckBox("Also listen on IPv6")
        self.ipv6.setChecked(s.enable_ipv6)
        self.public = QCheckBox("Accept clients outside private networks (not recommended)")
        self.public.setChecked(s.allow_public_clients)
        f.addRow("HTTP port", self.port)
        f.addRow("HTTPS port", self.https_port)
        f.addRow("", self.https)
        f.addRow("Listen address", self.host)
        f.addRow("", self.ipv6)
        f.addRow("", self.public)
        c.body.addLayout(f)
        grid.addWidget(c, 0, 1)

        c = Card("Audio")
        f = QFormLayout()
        f.addRow("Virtual microphone format", QLabel("signed 16-bit PCM, mono"))
        self.rate = QComboBox()
        self.rate.addItem("48000 Hz (recommended)", 48000)
        self.rate.addItem("44100 Hz", 44100)
        self.rate.setCurrentIndex(max(0, self.rate.findData(s.sample_rate)))
        f.addRow("Sample rate", self.rate)
        self.buffer = QComboBox()
        for ms in BUFFER_CHOICES_MS:
            self.buffer.addItem(f"{ms} ms", ms)
        self.buffer.setCurrentIndex(max(0, self.buffer.findData(s.buffer_ms)))
        f.addRow("Jitter buffer", self.buffer)
        self.src_desc = QLineEdit(s.source_description)
        self.src_name = QLineEdit(s.source_name)
        f.addRow("Virtual source name", self.src_desc)
        f.addRow("Virtual source node", self.src_name)
        self.auth = QCheckBox("Require pairing code")
        self.auth.setChecked(s.auth_enabled)
        f.addRow("Authentication", self.auth)
        c.body.addLayout(f)
        c.body.addWidget(muted_label("Browsers that cannot capture at 48 kHz are resampled on this computer. "
                                     "Changing the port, HTTPS or sample rate restarts the server or requires an app restart."))
        grid.addWidget(c, 1, 0)

        c = Card("Device names")
        c.body.addWidget(muted_label("Names you give devices are stored on this computer and used whenever they reconnect."))
        self.alias_table = QTableWidget(0, 3)
        self.alias_table.setHorizontalHeaderLabels(["Name", "Device ID", "Gain"])
        self.alias_table.verticalHeader().hide()
        self.alias_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.alias_table.horizontalHeader().setStretchLastSection(True)
        self.alias_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.alias_table.itemChanged.connect(self._alias_edited)
        c.body.addWidget(self.alias_table)
        forget = QPushButton("Forget selected")
        forget.clicked.connect(self._forget_selected)
        row = QHBoxLayout()
        row.addWidget(forget)
        row.addStretch(1)
        c.body.addLayout(row)
        grid.addWidget(c, 1, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.path_label = muted_label(f"Settings file: {core.store.path}", wrap=False)
        row.addWidget(self.path_label)
        save = QPushButton("Save settings")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        row.addWidget(save)
        grid.addLayout(row, 2, 0, 1, 2)
        grid.setRowStretch(3, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

    # ------------------------------------------------------------------
    def _save(self) -> None:
        s = self.core.store.settings
        old_net = (s.port, s.https_port, s.https_enabled, s.host, s.enable_ipv6)
        old_src = (s.source_name, s.source_description)
        old_rate = s.sample_rate
        if self.port.value() == self.https_port.value():
            QMessageBox.warning(self, "Settings", "HTTP and HTTPS ports must differ.")
            return
        self.core.set_setting(
            start_minimized=self.minimized.isChecked(), close_to_tray=self.tray.isChecked(),
            adb_auto_reverse=self.adb_auto.isChecked(), regenerate_code_on_start=self.regen.isChecked(),
            port=self.port.value(), https_port=self.https_port.value(), https_enabled=self.https.isChecked(),
            host=self.host.text().strip() or "0.0.0.0", enable_ipv6=self.ipv6.isChecked(),
            allow_public_clients=self.public.isChecked(), sample_rate=self.rate.currentData(),
            buffer_ms=self.buffer.currentData(), auth_enabled=self.auth.isChecked(),
        )
        try:
            set_autostart(self.autostart.isChecked())
        except OSError as exc:
            QMessageBox.warning(self, "Settings", f"Could not change autostart: {exc}")
        new_net = (s.port, s.https_port, s.https_enabled, s.host, s.enable_ipv6)
        if new_net != old_net:
            self.core.tls_enabled = s.https_enabled
            run_in_background(self.core.restart_server, lambda *_: self._report_server())
        new_src = (self.src_name.text().strip() or "phone-mic", self.src_desc.text().strip() or "Phone Microphone")
        if new_src != old_src:
            run_in_background(lambda: self.core.set_virtual_source(*new_src))
        if s.sample_rate != old_rate:
            QMessageBox.information(self, "Settings", "The new sample rate takes effect after restarting Phone Mic Router.")

    def _report_server(self) -> None:
        if self.core.server_error:
            QMessageBox.warning(self, "Server", self.core.server_error)

    def _alias_edited(self, item: QTableWidgetItem) -> None:
        if item.column() != 0 or self._filling:
            return
        cid = self.alias_table.item(item.row(), 1).text()
        self.core.rename_device(cid, item.text())

    _filling = False

    def _forget_selected(self) -> None:
        rows = {i.row() for i in self.alias_table.selectedIndexes()}
        for r in rows:
            self.core.forget_device(self.alias_table.item(r, 1).text())

    def update_snapshot(self, snap: dict) -> None:
        devs = self.core.store.settings.devices
        names = {d["client_id"]: d["name"] for d in snap["devices"]}
        key = sorted((cid, d.alias, round(d.gain_db, 1), names.get(cid, "")) for cid, d in devs.items())
        if key == self._aliases_key or self.alias_table.state() == QAbstractItemView.EditingState:
            return
        self._aliases_key = key
        self._filling = True
        self.alias_table.setRowCount(len(key))
        for i, (cid, alias, gain, name) in enumerate(key):
            a = QTableWidgetItem(alias or name or "")
            self.alias_table.setItem(i, 0, a)
            idi = QTableWidgetItem(cid)
            idi.setFlags(idi.flags() & ~Qt.ItemIsEditable)
            self.alias_table.setItem(i, 1, idi)
            g = QTableWidgetItem(f"{gain:+.1f} dB")
            g.setFlags(g.flags() & ~Qt.ItemIsEditable)
            self.alias_table.setItem(i, 2, g)
        self._filling = False
