"""Dashboard: the one-glance overview."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.gui.widgets import Card, CopyButton, LevelMeter, StatusDot, big_label, fmt_db, muted_label, qr_pixmap


class Dashboard(QWidget):
    open_tab = Signal(str)

    def __init__(self, core, parent=None):
        super().__init__(parent)
        self.core = core
        self._qr_text = ""
        grid = QGridLayout(self)
        grid.setContentsMargins(16, 16, 16, 16)
        grid.setSpacing(14)

        # --- server ---
        c = Card("Server")
        self.server_dot = StatusDot("Starting…", "busy")
        self.server_dot.label.setObjectName("")
        f = self.server_dot.label.font()
        f.setPointSize(13)
        f.setBold(True)
        self.server_dot.label.setFont(f)
        c.body.addWidget(self.server_dot)
        self.server_detail = muted_label()
        c.body.addWidget(self.server_detail)
        self.lan_label = QLabel()
        self.lan_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lan_label.setWordWrap(True)
        c.body.addWidget(self.lan_label)
        self.usb_label = QLabel()
        self.usb_label.setWordWrap(True)
        self.usb_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        c.body.addWidget(self.usb_label)
        c.body.addStretch(1)
        grid.addWidget(c, 0, 0)

        # --- connect a phone ---
        c = Card("Connect a phone")
        row = QHBoxLayout()
        self.qr = QLabel()
        self.qr.setFixedSize(170, 170)
        self.qr.setScaledContents(True)
        row.addWidget(self.qr)
        col = QVBoxLayout()
        col.addWidget(muted_label("Pairing code"))
        self.code = QLabel("––-––-––")
        self.code.setObjectName("code")
        self.code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(self.code)
        self.url = QLabel()
        self.url.setWordWrap(True)
        self.url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(self.url)
        hb = QHBoxLayout()
        hb.addWidget(CopyButton(lambda: self.core.best_phone_url()))
        more = QPushButton("All addresses…")
        more.clicked.connect(lambda: self.open_tab.emit("Network"))
        hb.addWidget(more)
        hb.addStretch(1)
        col.addLayout(hb)
        col.addWidget(muted_label("Scan the code with the phone's camera, accept the certificate warning once, then tap Start Microphone."))
        col.addStretch(1)
        row.addLayout(col, 1)
        c.body.addLayout(row)
        grid.addWidget(c, 0, 1)

        # --- current input / route ---
        c = Card("Current microphone")
        self.input_name = big_label("No phone connected", 15)
        c.body.addWidget(self.input_name)
        self.input_detail = muted_label()
        c.body.addWidget(self.input_detail)
        self.meter = LevelMeter()
        c.body.addWidget(self.meter)
        lv = QHBoxLayout()
        self.level_text = QLabel("-∞ dBFS")
        self.peak_text = muted_label("Peak -∞", wrap=False)
        self.latency_text = QLabel("Latency –")
        self.latency_text.setToolTip("Approximate. Browser and phone timing cannot be measured exactly.")
        lv.addWidget(self.level_text)
        lv.addWidget(self.peak_text)
        lv.addStretch(1)
        lv.addWidget(self.latency_text)
        c.body.addLayout(lv)
        self.mute_btn = QPushButton("Mute virtual microphone")
        self.mute_btn.setCheckable(True)
        self.mute_btn.toggled.connect(self._mute_toggled)
        c.body.addWidget(self.mute_btn)
        c.body.addStretch(1)
        grid.addWidget(c, 1, 0)

        c = Card("Route")
        self.route_inputs = QLabel()
        self.route_inputs.setObjectName("routeNode")
        self.route_inputs.setWordWrap(True)
        c.body.addWidget(self.route_inputs)
        a = QLabel("↓")
        a.setObjectName("arrow")
        a.setAlignment(Qt.AlignCenter)
        c.body.addWidget(a)
        self.route_target = QLabel()
        self.route_target.setObjectName("routeTarget")
        self.route_target.setWordWrap(True)
        self.route_target.setTextInteractionFlags(Qt.TextSelectableByMouse)
        c.body.addWidget(self.route_target)
        a = QLabel("↓")
        a.setObjectName("arrow")
        a.setAlignment(Qt.AlignCenter)
        c.body.addWidget(a)
        self.route_apps = QLabel()
        self.route_apps.setObjectName("routeNode")
        self.route_apps.setWordWrap(True)
        c.body.addWidget(self.route_apps)
        btn = QPushButton("Change routing…")
        btn.clicked.connect(lambda: self.open_tab.emit("Audio"))
        c.body.addWidget(btn)
        grid.addWidget(c, 1, 1)

        self.alert = QLabel()
        self.alert.setWordWrap(True)
        self.alert.setStyleSheet("color:#ef5350; font-weight:600;")
        self.alert.hide()
        grid.addWidget(self.alert, 2, 0, 1, 2)
        grid.setRowStretch(3, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

    def _mute_toggled(self, checked: bool) -> None:
        self.core.set_setting(master_muted=checked)
        self.mute_btn.setText("Unmute virtual microphone" if checked else "Mute virtual microphone")

    def update_snapshot(self, snap: dict) -> None:
        srv = snap["server"]
        if snap["starting"] and not snap["started"]:
            self.server_dot.set("busy", "Starting…")
        elif srv["running"]:
            self.server_dot.set("ok", "Running")
        else:
            self.server_dot.set("error", "Stopped")
        https = f", HTTPS port {srv['https_port']}" if srv["https_port"] else ", HTTPS off"
        self.server_detail.setText(
            f"HTTP port {srv['http_port']}{https} · WebSocket {'running' if srv['running'] else 'stopped'} · "
            f"{snap['connected_count']} connected"
        )
        lan = [u for u in snap["urls"] if u["secure"] and u["interface"] != "localhost"][:3]
        self.lan_label.setText("<b>Wi-Fi / LAN</b><br>" + ("<br>".join(u["url"] for u in lan) if lan else "No LAN address found"))
        adb = snap["adb"]
        if not adb["available"]:
            usb = "adb not installed"
        else:
            ready = [d for d in adb["devices"] if d["ready"]]
            rev = [d for d in ready if d["reverse"]]
            if rev:
                usb = f"● {', '.join(d['name'] for d in rev)} via USB → http://127.0.0.1:{srv['http_port']}"
            elif ready:
                usb = f"{len(ready)} Android device(s) connected; USB microphone not enabled"
            elif adb["devices"]:
                usb = adb["devices"][0]["message"]
            else:
                usb = "No Android device on USB"
        self.usb_label.setText(f"<b>USB / ADB</b><br>{usb}")

        auth = snap["auth"]
        self.code.setText(auth["formatted"] if auth["enabled"] else "off")
        url = snap["phone_url"]
        shown = url.split("?")[0]
        self.url.setText(f"<a href='{shown}' style='color:#4f8cff'>{shown}</a>")
        if url != self._qr_text:
            self._qr_text = url
            self.qr.setPixmap(qr_pixmap(url, 340))

        audio = snap["audio"]
        rows = {d["client_id"]: d for d in snap["devices"]}
        active = [rows[c] for c in audio["active"] if c in rows]
        if active:
            d = active[0] if len(active) == 1 else None
            if d:
                self.input_name.setText(d["name"])
                fmt = f"{d['sample_rate'] / 1000:g} kHz mono" if d["sample_rate"] else "waiting for audio"
                self.input_detail.setText(f"{d['connection']} · {d['ip']} · {fmt} · {d['state'].capitalize()}")
                self.meter.set_level(d["rms_db"], d["peak_db"], d["clipping"], not d["muted"])
                self.level_text.setText(fmt_db(d["rms_db"]))
                self.peak_text.setText("Peak " + fmt_db(d["peak_db"]))
                lat = d["latency_ms"]
            else:
                self.input_name.setText(f"{len(active)} devices mixed")
                self.input_detail.setText(", ".join(x["name"] for x in active))
                self.meter.set_level(audio["master_rms_db"], audio["master_peak_db"], audio["master_clipping"])
                self.level_text.setText(fmt_db(audio["master_rms_db"]))
                self.peak_text.setText("Peak " + fmt_db(audio["master_peak_db"]))
                lats = [x["latency_ms"] for x in active if x["latency_ms"] is not None]
                lat = max(lats) if lats else None
            self.latency_text.setText("Latency –" if lat is None else f"Latency ~{lat:.0f} ms")
        else:
            connected = [d for d in snap["devices"] if d["connected"]]
            self.input_name.setText("No phone streaming" if connected else "No phone connected")
            self.input_detail.setText("Open the address on your phone and tap Start Microphone.")
            self.meter.set_level(-90, -90, False)
            self.level_text.setText("-∞ dBFS")
            self.peak_text.setText("Peak -∞")
            self.latency_text.setText("Latency –")
        if self.mute_btn.isChecked() != audio["master_muted"]:
            self.mute_btn.blockSignals(True)
            self.mute_btn.setChecked(audio["master_muted"])
            self.mute_btn.setText("Unmute virtual microphone" if audio["master_muted"] else "Mute virtual microphone")
            self.mute_btn.blockSignals(False)

        mode = "mix" if audio["route_mode"] == "mix" else "single device"
        self.route_inputs.setText(
            "<b>Input</b> (" + mode + ")<br>" + ("<br>".join(f"● {d['name']} · {d['state']}" for d in active) if active else "none")
        )
        src = audio["source"]
        state = {"running": "● Active", "error": "● Error", "starting": "Starting…", "stopped": "Stopped"}.get(src["state"], src["state"])
        node = f"node {src['node_name']}" + (f", id {src['node_id']}" if src["node_id"] is not None else "")
        self.route_target.setText(
            f"Sending audio to:<br><span style='font-size:13pt'>{src['description']}</span><br>"
            f"<span style='font-weight:400;color:#9aa0ad'>Virtual microphone (source) · {node} · {audio['backend']} · {state}</span>"
        )
        apps = src["consumers"]
        self.route_apps.setText("<b>Applications recording</b><br>" + (
            "<br>".join(apps) if apps else "None yet: select “" + src["description"] + "” as the microphone in Discord, OBS or a game"
        ))
        errs = [e for e in (srv["error"], audio["error"], srv["tls_error"]) if e]
        self.alert.setVisible(bool(errs))
        self.alert.setText("\n".join(errs))
