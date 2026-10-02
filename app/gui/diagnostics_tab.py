"""Diagnostics tab: status lights, dependencies, logs and a copyable report."""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config import paths
from app.gui.widgets import Card, StatusDot, mono_font, muted_label, run_in_background
from app.logging_setup import memory_handler

ROWS = [
    ("pipewire", "PipeWire"),
    ("pulse", "PulseAudio compatibility"),
    ("vmic", "Virtual microphone"),
    ("http", "HTTP server"),
    ("https", "HTTPS"),
    ("ws", "WebSocket"),
    ("lan", "LAN"),
    ("adb", "ADB"),
    ("adbdev", "ADB devices"),
    ("engine", "Audio engine"),
]


class DiagnosticsTab(QWidget):
    def __init__(self, core, parent=None):
        super().__init__(parent)
        self.core = core
        self._log_counter = -1
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(14)

        left = QVBoxLayout()
        c = Card("System status")
        g = QGridLayout()
        g.setHorizontalSpacing(16)
        self.dots: dict[str, StatusDot] = {}
        for i, (key, title) in enumerate(ROWS):
            g.addWidget(QLabel(title), i, 0)
            d = StatusDot("–")
            self.dots[key] = d
            g.addWidget(d, i, 1)
        c.body.addLayout(g)
        left.addWidget(c)
        c = Card("Detected tools")
        self.tools = muted_label()
        c.body.addWidget(self.tools)
        left.addWidget(c)
        btn = QPushButton("Copy diagnostic information")
        btn.setObjectName("primary")
        btn.clicked.connect(self._copy)
        left.addWidget(btn)
        self.copied = muted_label("The report contains no audio and no pairing code.")
        left.addWidget(self.copied)
        b2 = QPushButton("Re-detect audio system and adb")
        b2.clicked.connect(lambda: run_in_background(lambda: (self.core.redetect_audio(), self.core.adb_refresh())))
        left.addWidget(b2)
        b3 = QPushButton("Open log folder")
        b3.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.log_dir()))))
        left.addWidget(b3)
        left.addStretch(1)
        lay.addLayout(left, 2)

        right = QVBoxLayout()
        c = Card("Recent log")
        row = QHBoxLayout()
        row.addWidget(QLabel("Show"))
        self.level = QComboBox()
        for lvl in ("DEBUG", "INFO", "WARNING", "ERROR"):
            self.level.addItem(lvl)
        self.level.setCurrentText("INFO")
        self.level.currentIndexChanged.connect(lambda _: self._reload_log(force=True))
        row.addWidget(self.level)
        row.addStretch(1)
        self.log_path = muted_label(wrap=False)
        row.addWidget(self.log_path)
        c.body.addLayout(row)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(mono_font(9))
        self.log.setMaximumBlockCount(2000)
        self.log.setLineWrapMode(QPlainTextEdit.NoWrap)
        c.body.addWidget(self.log)
        right.addWidget(c)
        lay.addLayout(right, 3)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.core.diagnostics_text())
        self.copied.setText("Copied to the clipboard. It contains no audio and no pairing code.")

    def _reload_log(self, force: bool = False) -> None:
        counter, lines = memory_handler.snapshot()
        if counter == self._log_counter and not force:
            return
        self._log_counter = counter
        order = ["DEBUG", "INFO", "WARNING", "ERROR"]
        min_idx = order.index(self.level.currentText())
        wanted = tuple(order[min_idx:]) + ("CRITICAL",)
        # lines look like "12:34:56 WARNING message"
        shown = [ln for ln in lines if (ln[9:18].split() or [""])[0] in wanted or not ln[:2].isdigit()]
        bar = self.log.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        self.log.setPlainText("\n".join(shown[-1000:]))
        if at_bottom:
            self.log.moveCursor(QTextCursor.End)

    def update_snapshot(self, snap: dict) -> None:
        a = snap["audio"]
        sysi = a["system"]
        srv = snap["server"]
        adb = snap["adb"]
        d = self.dots
        d["pipewire"].set("ok" if sysi["pipewire_running"] else "error",
                          f"Connected ({sysi['pipewire_version']})" if sysi["pipewire_running"] else "PipeWire not detected")
        d["pulse"].set("ok" if sysi["pulse_available"] else "warn",
                       "Available" if sysi["pulse_available"] else "Unavailable (pactl not working)")
        src = a["source"]
        vm_state = {"running": "ok", "error": "error", "starting": "busy"}.get(src["state"], "off")
        d["vmic"].set(vm_state, {"running": f"Created: {src['description']} ({src['node_name']}, id {src['node_id']})",
                                 "error": f"Error: {a['error'] or src['error']}"}.get(src["state"], src["state"].capitalize()))
        d["http"].set("ok" if srv["running"] else "error",
                      f"Running on port {srv['http_port']}" if srv["running"] else (srv["error"] or "Stopped"))
        d["https"].set("ok" if srv["https_port"] else "warn",
                       f"Running on port {srv['https_port']}" if srv["https_port"] else (srv["tls_error"] or "Disabled"))
        d["ws"].set("ok" if srv["running"] else "error",
                    f"Running · {srv['ws_connections']} open · {srv['total_connections']} total · {srv['rejected']} rejected"
                    if srv["running"] else "Stopped")
        lan = [x for x in snap["addresses"] if x["kind"] in ("lan", "wifi")]
        d["lan"].set("ok" if lan else "warn", ", ".join(x["address"] for x in lan if x["family"] == 4) or "No LAN interface up")
        d["adb"].set("ok" if adb["available"] else "warn",
                     f"Available ({adb['version']})" if adb["available"] else "adb executable not found")
        ready = sum(1 for x in adb["devices"] if x["ready"])
        d["adbdev"].set("ok" if ready else "off",
                        f"{len(adb['devices'])} ({ready} ready)" if adb["devices"] else "No Android devices detected")
        d["engine"].set("ok" if a["engine_running"] else "error",
                        f"Running · {a['engine_cpu'] * 100:.1f}% CPU of real time · {a['backend_drops']} drops"
                        if a["engine_running"] else "Stopped")
        self.tools.setText(" · ".join(f"{k} {'✔' if v else '✖'}" for k, v in sysi["tools"].items())
                           + f"<br>adb: {adb['path'] or 'not found'}")
        self.log_path.setText(snap["log_path"] or "")
        self._reload_log()
