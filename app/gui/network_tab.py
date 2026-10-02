"""Network tab: addresses, pairing code, HTTPS certificate and USB/ADB."""

from __future__ import annotations

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.gui.widgets import Card, StatusDot, muted_label, qr_pixmap, run_in_background


class NetworkTab(QWidget):
    def __init__(self, core, parent=None):
        super().__init__(parent)
        self.core = core
        self._urls_key = None
        self._adb_key = None
        self._qr_for = ""
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

        # --- addresses ---
        c = Card("Browser addresses")
        self.server_status = StatusDot("–")
        c.body.addWidget(self.server_status)
        self.url_table = QTableWidget(0, 4)
        self.url_table.setHorizontalHeaderLabels(["Address", "Interface", "Type", "Microphone capture"])
        self.url_table.verticalHeader().hide()
        self.url_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.url_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.url_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.url_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.url_table.horizontalHeader().setStretchLastSection(True)
        self.url_table.setMinimumHeight(200)
        self.url_table.itemSelectionChanged.connect(self._url_selected)
        c.body.addWidget(self.url_table)
        row = QHBoxLayout()
        copy = QPushButton("Copy address")
        copy.clicked.connect(self._copy_url)
        row.addWidget(copy)
        openb = QPushButton("Open in browser")
        openb.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self._selected_url(False))))
        row.addWidget(openb)
        row.addStretch(1)
        c.body.addLayout(row)
        c.body.addWidget(muted_label(
            "Phones only allow the microphone on secure pages. Over Wi-Fi use an https:// address; the browser shows a "
            "certificate warning the first time because the certificate was made by this computer. Tap Advanced, then "
            "Proceed. Over USB, http://127.0.0.1 counts as secure and needs no certificate."))
        grid.addWidget(c, 0, 0, 1, 2)

        # --- QR + pairing ---
        c = Card("Pairing")
        row = QHBoxLayout()
        self.qr = QLabel()
        self.qr.setFixedSize(200, 200)
        self.qr.setScaledContents(True)
        row.addWidget(self.qr)
        col = QVBoxLayout()
        col.addWidget(muted_label("Pairing code"))
        self.code = QLabel()
        self.code.setObjectName("code")
        self.code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(self.code)
        self.qr_caption = muted_label()
        col.addWidget(self.qr_caption)
        regen = QPushButton("New pairing code")
        regen.setToolTip("Generates a new code. Paired phones must enter the new code.")
        regen.clicked.connect(self._regen)
        col.addWidget(regen)
        self.auth_box = QCheckBox("Require pairing code")
        self.auth_box.toggled.connect(self._auth_toggled)
        col.addWidget(self.auth_box)
        col.addWidget(muted_label("Disable only on a trusted network. Clients outside the local network are always rejected."))
        col.addStretch(1)
        row.addLayout(col, 1)
        c.body.addLayout(row)
        grid.addWidget(c, 1, 0)

        # --- HTTPS ---
        c = Card("HTTPS certificate")
        self.tls_status = StatusDot("–")
        c.body.addWidget(self.tls_status)
        self.tls_detail = muted_label()
        c.body.addWidget(self.tls_detail)
        c.body.addWidget(muted_label(
            "To remove the warning permanently, open /ca.crt on the phone (link on the phone page) and install it under "
            "Settings → Security → Encryption & credentials → Install a certificate → CA certificate. Optional."))
        row = QHBoxLayout()
        b = QPushButton("Show certificate folder")
        b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.core.certs.dir))))
        row.addWidget(b)
        row.addStretch(1)
        c.body.addLayout(row)
        c.body.addStretch(1)
        grid.addWidget(c, 1, 1)

        # --- ADB ---
        c = Card("USB / ADB")
        self.adb_status = StatusDot("–")
        c.body.addWidget(self.adb_status)
        self.adb_table = QTableWidget(0, 4)
        self.adb_table.setHorizontalHeaderLabels(["Device", "Serial", "Status", "USB microphone"])
        self.adb_table.verticalHeader().hide()
        self.adb_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.adb_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.adb_table.horizontalHeader().setStretchLastSection(True)
        self.adb_table.setMinimumHeight(130)
        c.body.addWidget(self.adb_table)
        row = QHBoxLayout()
        self.auto_box = QCheckBox("Automatically enable for every authorized phone")
        self.auto_box.toggled.connect(lambda v: self.core.set_setting(adb_auto_reverse=v))
        row.addWidget(self.auto_box)
        row.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(lambda: run_in_background(self.core.adb_refresh))
        row.addWidget(refresh)
        c.body.addLayout(row)
        self.adb_help = muted_label()
        c.body.addWidget(self.adb_help)
        grid.addWidget(c, 2, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

    # ------------------------------------------------------------------
    def _selected_url(self, with_code: bool = True) -> str:
        rows = self.url_table.selectionModel().selectedRows()
        if rows:
            url = self.url_table.item(rows[0].row(), 0).text()
        else:
            url = self.core.best_phone_url(with_code=False)
        if with_code and self.core.auth.enabled:
            url += f"?code={self.core.auth.code}"
        return url

    def _copy_url(self) -> None:
        QGuiApplication.clipboard().setText(self._selected_url(False))

    def _url_selected(self) -> None:
        self._qr_for = ""  # force QR refresh

    def _regen(self) -> None:
        if QMessageBox.question(self, "New pairing code",
                                "Generate a new pairing code? Phones that already paired must enter the new code.") \
                == QMessageBox.Yes:
            self.core.regenerate_pairing_code()

    def _auth_toggled(self, on: bool) -> None:
        if on == self.core.auth.enabled:
            return
        if not on and QMessageBox.warning(
                self, "Disable pairing",
                "Without a pairing code any device on your local network can stream audio into the virtual microphone. "
                "Continue?", QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            self.auth_box.setChecked(True)
            return
        self.core.set_setting(auth_enabled=on)

    def _adb_toggle(self, serial: str, enable: bool) -> None:
        def done(res, exc):
            if exc:
                QMessageBox.warning(self, "USB microphone", str(exc))
            elif enable and res and not res[0]:
                QMessageBox.warning(self, "USB microphone", f"Could not enable USB forwarding:\n{res[1]}")
        if enable:
            run_in_background(lambda: self.core.adb_enable(serial), done)
        else:
            run_in_background(lambda: self.core.adb_disable(serial), done)

    def update_snapshot(self, snap: dict) -> None:
        srv = snap["server"]
        if srv["running"]:
            self.server_status.set("ok", f"Server running on {', '.join(srv['listen_hosts']) or '?'} · HTTP {srv['http_port']}"
                                   + (f" · HTTPS {srv['https_port']}" if srv["https_port"] else " · HTTPS disabled"))
        else:
            self.server_status.set("error", srv["error"] or "Server stopped")

        key = [u["url"] for u in snap["urls"]]
        if key != self._urls_key:
            self._urls_key = key
            sel = self._selected_url(False) if self.url_table.rowCount() else ""
            self.url_table.setRowCount(len(snap["urls"]))
            for i, u in enumerate(snap["urls"]):
                vals = [u["url"], u["interface"], u["kind"], ("✔ " if u["secure"] else "✖ ") + u["mic"]]
                for j, v in enumerate(vals):
                    it = QTableWidgetItem(v)
                    if j == 3:
                        it.setForeground(Qt.GlobalColor.green if u["secure"] else Qt.GlobalColor.gray)
                    self.url_table.setItem(i, j, it)
                if u["url"] == sel:
                    self.url_table.selectRow(i)
            self._qr_for = ""

        auth = snap["auth"]
        self.code.setText(auth["formatted"] if auth["enabled"] else "disabled")
        if self.auth_box.isChecked() != auth["enabled"]:
            self.auth_box.blockSignals(True)
            self.auth_box.setChecked(auth["enabled"])
            self.auth_box.blockSignals(False)
        qr_text = self._selected_url(True)
        if qr_text != self._qr_for:
            self._qr_for = qr_text
            self.qr.setPixmap(qr_pixmap(qr_text, 400))
            self.qr_caption.setText("QR opens: " + qr_text.split("?")[0] + ("  (includes the pairing code)" if auth["enabled"] else ""))

        tls = snap["tls"]
        if tls["enabled"]:
            self.tls_status.set("ok", "HTTPS enabled (local certificate)")
            self.tls_detail.setText(f"CA fingerprint (SHA-256):<br><small>{tls['fingerprint']}</small><br>"
                                    f"Valid for: {', '.join(tls['hosts'])}")
        else:
            self.tls_status.set("warn", "HTTPS disabled: Wi-Fi microphones will not work in most browsers")
            self.tls_detail.setText(srv["tls_error"] or "Enable HTTPS in Settings.")

        adb = snap["adb"]
        if self.auto_box.isChecked() != adb["auto_all"]:
            self.auto_box.blockSignals(True)
            self.auto_box.setChecked(adb["auto_all"])
            self.auto_box.blockSignals(False)
        if not adb["available"]:
            self.adb_status.set("error", "adb executable not found")
            self.adb_help.setText("Install adb to use phones over USB: <b>sudo pacman -S android-tools</b> (Arch), "
                                  "<b>sudo apt install adb</b> (Debian/Ubuntu), <b>sudo dnf install android-tools</b> (Fedora).")
        elif not adb["server_running"]:
            self.adb_status.set("warn", f"adb {adb['version']} found but not responding")
            self.adb_help.setText(adb["last_error"])
        else:
            n = len(adb["devices"])
            self.adb_status.set("ok" if any(d["reverse"] for d in adb["devices"]) else "off",
                                f"adb {adb['version']} · {n} device{'s' if n != 1 else ''}")
            port = snap["server"]["http_port"]
            self.adb_help.setText(
                f"Enable USB debugging on the phone (Settings → About → tap Build number 7 times → Developer options → USB debugging), "
                f"connect the cable and accept the prompt. Then enable the USB microphone here and open "
                f"<b>http://127.0.0.1:{port}</b> on the phone. This runs <code>adb reverse tcp:{port} tcp:{port}</code>; "
                f"nothing is installed on the phone." if n == 0 or not any(d["reverse"] for d in adb["devices"]) else
                f"On the phone open <b>http://127.0.0.1:{port}</b> in Chrome or Firefox and tap Start Microphone."
            )
        key = [(d["serial"], d["state"], d["reverse"], d["error"]) for d in adb["devices"]]
        if key != self._adb_key:
            self._adb_key = key
            self.adb_table.setRowCount(len(adb["devices"]))
            for i, d in enumerate(adb["devices"]):
                self.adb_table.setItem(i, 0, QTableWidgetItem(d["name"]))
                self.adb_table.setItem(i, 1, QTableWidgetItem(d["serial"]))
                st = QTableWidgetItem(("● " if d["ready"] else "") + d["message"] + (f" — {d['error']}" if d["error"] else ""))
                st.setForeground(Qt.GlobalColor.green if d["ready"] and not d["error"] else Qt.GlobalColor.yellow)
                self.adb_table.setItem(i, 2, st)
                btn = QPushButton("Disable USB microphone" if d["reverse"] else "Enable USB microphone")
                btn.setObjectName("" if d["reverse"] else "primary")
                btn.setEnabled(d["ready"])
                serial, rev = d["serial"], d["reverse"]
                btn.clicked.connect(lambda _=False, s=serial, r=rev: self._adb_toggle(s, not r))
                self.adb_table.setCellWidget(i, 3, btn)
