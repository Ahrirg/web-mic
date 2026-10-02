"""Devices tab: every browser microphone with per-device controls."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSplitter,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.gui.widgets import Card, LevelMeter, big_label, fmt_db, fmt_duration, muted_label, paint_meter

COLUMNS = ["Device", "IP address", "Connection", "Duration", "Audio status", "Format", "Level", "Latency", "Routing", "State"]
LEVEL_COL = COLUMNS.index("Level")

STATE_TEXT = {
    "connecting": "Connecting", "waiting": "Waiting for audio", "streaming": "● Streaming", "stalled": "Stalled",
    "paused": "Mic stopped on phone", "disconnected": "Disconnected",
}


class DevicesModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        return None

    def set_rows(self, rows: list[dict]) -> None:
        if [r["client_id"] for r in rows] != [r["client_id"] for r in self.rows]:
            self.beginResetModel()
            self.rows = rows
            self.endResetModel()
        else:
            self.rows = rows
            if rows:
                self.dataChanged.emit(self.index(0, 0), self.index(len(rows) - 1, len(COLUMNS) - 1))

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r = self.rows[index.row()]
        col = COLUMNS[index.column()]
        if role == Qt.DisplayRole:
            if col == "Device":
                return r["name"]
            if col == "IP address":
                return r["ip"]
            if col == "Connection":
                return r["connection"] + ("" if r["secure"] or r["connection"].startswith(("USB", "Local")) else " (HTTP)")
            if col == "Duration":
                return fmt_duration(r["duration"])
            if col == "Audio status":
                if not r["connected"]:
                    return "–"
                if r["disabled"]:
                    return "Disabled"
                if r["muted"]:
                    return "Muted"
                if r["client_muted"]:
                    return "Muted on phone"
                return STATE_TEXT.get(r["state"], r["state"])
            if col == "Format":
                if not r["sample_rate"]:
                    return "–"
                return f"{r['sample_rate'] / 1000:g} kHz {'mono' if r['channels'] == 1 else 'stereo'}"
            if col == "Level":
                return ""
            if col == "Latency":
                return "–" if r["latency_ms"] is None else f"~{r['latency_ms']:.0f} ms"
            if col == "Routing":
                if r["routed_now"]:
                    return f"→ {r['route_target']}"
                if not r["routed"] or r["disabled"]:
                    return "Not routed"
                return "Standby"
            if col == "State":
                return "Connected" if r["connected"] else "Disconnected"
        if role == Qt.ForegroundRole:
            if not r["connected"]:
                return QColor("#6b7180")
            if col == "Audio status" and r["state"] == "streaming" and not r["muted"]:
                return QColor("#3ccf7a")
            if col == "Audio status" and r["state"] == "stalled":
                return QColor("#f1b33c")
            if col == "Routing" and r["routed_now"]:
                return QColor("#4f8cff")
        if role == Qt.ToolTipRole:
            return (f"{r['detected_name']} · {r['browser']} on {r['platform']}\n"
                    f"Packets {r['packets']}, lost {r['lost']}, malformed {r['malformed']}, underruns {r['underruns']}\n"
                    f"RTT {r['rtt_ms'] and round(r['rtt_ms'])} ms, buffer {r['buffer_ms']:.0f} ms, reconnects {r['reconnects']}")
        if role == Qt.UserRole:
            return r
        return None


class LevelDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        super().paint(painter, option, index)  # selection background
        r = index.data(Qt.UserRole)
        if not r or not r["connected"]:
            return
        rect = QRectF(option.rect.adjusted(6, option.rect.height() // 2 - 6, -6, -(option.rect.height() // 2 - 6)))
        paint_meter(painter, rect, r["rms_db"], r["peak_db"], r["clipping"],
                    not (r["muted"] or r["disabled"]), lamp=True)


class DevicesTab(QWidget):
    def __init__(self, core, parent=None):
        super().__init__(parent)
        self.core = core
        self.selected: str | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        split = QSplitter(Qt.Vertical)
        lay.addWidget(split)

        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(muted_label("Every browser that opened the microphone page. Select a device to control it. "
                                 "Disconnected devices stay listed for a while so they can reconnect with the same settings."))
        self.model = DevicesModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setItemDelegateForColumn(LEVEL_COL, LevelDelegate(self.table))
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(LEVEL_COL, QHeaderView.Fixed)
        self.table.setColumnWidth(LEVEL_COL, 130)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        tl.addWidget(self.table)
        split.addWidget(top)

        # --- detail panel ---
        self.detail = Card("Selected device")
        d = self.detail.body
        head = QHBoxLayout()
        self.d_name = big_label("Select a device", 14)
        head.addWidget(self.d_name, 1)
        self.d_meter = LevelMeter()
        self.d_meter.setFixedWidth(260)
        head.addWidget(self.d_meter)
        self.d_level = QLabel("")
        self.d_level.setMinimumWidth(150)
        head.addWidget(self.d_level)
        d.addLayout(head)
        self.d_info = muted_label()
        d.addWidget(self.d_info)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        rename_row = QHBoxLayout()
        self.d_alias = QLineEdit()
        self.d_alias.setPlaceholderText("Friendly name (stored on this computer)")
        self.d_alias.returnPressed.connect(self._rename)
        rb = QPushButton("Rename")
        rb.clicked.connect(self._rename)
        rename_row.addWidget(self.d_alias, 1)
        rename_row.addWidget(rb)
        form.addRow("Name", rename_row)

        gain_row = QHBoxLayout()
        self.d_gain = QSlider(Qt.Horizontal)
        self.d_gain.setRange(-300, 240)  # tenths of dB
        self.d_gain.setSingleStep(5)
        self.d_gain.setPageStep(30)
        self.d_gain.valueChanged.connect(self._gain_changed)
        self.d_gain_label = QLabel("0.0 dB")
        self.d_gain_label.setMinimumWidth(60)
        reset = QPushButton("0 dB")
        reset.clicked.connect(lambda: self.d_gain.setValue(0))
        gain_row.addWidget(self.d_gain, 1)
        gain_row.addWidget(self.d_gain_label)
        gain_row.addWidget(reset)
        form.addRow("Gain", gain_row)

        route_row = QHBoxLayout()
        self.d_mute = QCheckBox("Mute")
        self.d_mute.toggled.connect(lambda v: self._set(muted=v))
        self.d_route = QCheckBox("Route to virtual microphone")
        self.d_route.toggled.connect(lambda v: self._set(routed=v))
        self.d_monitor = QCheckBox("Also play on output (monitor)")
        self.d_monitor.setToolTip("Play this phone on your speakers/headphones too. Choose the output on the Audio tab.")
        self.d_monitor.toggled.connect(lambda v: self._set(monitor=v))
        for w in (self.d_mute, self.d_route, self.d_monitor):
            route_row.addWidget(w)
        route_row.addStretch(1)
        form.addRow("Routing", route_row)
        d.addLayout(form)

        btns = QHBoxLayout()
        self.b_active = QPushButton("Use as active microphone")
        self.b_active.setObjectName("primary")
        self.b_active.clicked.connect(self._make_active)
        self.b_disconnect = QPushButton("Disconnect")
        self.b_disconnect.clicked.connect(lambda: self.selected and self.core.disconnect_device(self.selected))
        self.b_disable = QPushButton("Disable")
        self.b_disable.setCheckable(True)
        self.b_disable.setToolTip("A disabled device may stay connected but is never routed")
        self.b_disable.toggled.connect(lambda v: self._set(disabled=v))
        self.b_forget = QPushButton("Remove && forget")
        self.b_forget.setObjectName("danger")
        self.b_forget.clicked.connect(self._forget)
        for b in (self.b_active, self.b_disconnect, self.b_disable):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(self.b_forget)
        d.addLayout(btns)
        self.d_stats = muted_label()
        d.addWidget(self.d_stats)
        split.addWidget(self.detail)
        split.setSizes([300, 320])
        self._set_detail_enabled(False)

    # ------------------------------------------------------------------
    def _set_detail_enabled(self, on: bool) -> None:
        for w in (self.d_alias, self.d_gain, self.d_mute, self.d_route, self.d_monitor, self.b_active,
                  self.b_disconnect, self.b_disable, self.b_forget):
            w.setEnabled(on)

    def _selection_changed(self, *_):
        idx = self.table.selectionModel().selectedRows()
        self.selected = self.model.rows[idx[0].row()]["client_id"] if idx else None
        if self.selected:
            r = self._row()
            self.d_alias.setText(r["alias"] or r["detected_name"])
        self._refresh_detail(force=True)

    def _row(self) -> dict | None:
        for r in self.model.rows:
            if r["client_id"] == self.selected:
                return r
        return None

    def _set(self, **changes) -> None:
        if self.selected:
            self.core.set_device(self.selected, **changes)

    def _gain_changed(self, v: int) -> None:
        self.d_gain_label.setText(f"{v / 10:+.1f} dB")
        if self.selected and not self._updating:
            self.core.set_device(self.selected, gain_db=v / 10)

    _updating = False

    def _rename(self) -> None:
        if self.selected:
            self.core.rename_device(self.selected, self.d_alias.text())

    def _make_active(self) -> None:
        if self.selected:
            if self.core.store.settings.route_mode == "mix":
                self.core.set_device(self.selected, routed=True, disabled=False)
            else:
                self.core.set_active_client(self.selected)
                self.core.set_device(self.selected, disabled=False)

    def _forget(self) -> None:
        if self.selected:
            self.core.forget_device(self.selected)
            self.selected = None
            self.table.clearSelection()

    def _refresh_detail(self, force: bool = False) -> None:
        r = self._row()
        if not r:
            self.d_name.setText("Select a device")
            self.d_info.setText("")
            self.d_stats.setText("")
            self.d_level.setText("")
            self.d_meter.set_level(-90, -90, False)
            self._set_detail_enabled(False)
            return
        self._set_detail_enabled(True)
        self.b_disconnect.setEnabled(r["connected"])
        self.d_name.setText(r["name"])
        fmt = f"{r['sample_rate']} Hz {'mono' if r['channels'] == 1 else 'stereo'}" + (" (resampled to 48000 Hz)" if r["resampling"] else "") if r["sample_rate"] else "no audio format yet"
        self.d_info.setText(f"{r['browser']} on {r['platform']} · {r['ip']} · {r['connection']} · {fmt} · "
                            f"{STATE_TEXT.get(r['state'], r['state'])}")
        self.d_meter.set_level(r["rms_db"], r["peak_db"], r["clipping"], not (r["muted"] or r["disabled"]))
        self.d_level.setText(f"{fmt_db(r['rms_db'])} · peak {fmt_db(r['peak_db'])}" + ("  CLIP" if r["clipping"] else ""))
        rtt = "–" if r["rtt_ms"] is None else f"{r['rtt_ms']:.0f} ms"
        lat = "–" if r["latency_ms"] is None else f"~{r['latency_ms']:.0f} ms (approximate)"
        self.d_stats.setText(
            f"Latency {lat} · network RTT {rtt} · jitter buffer {r['buffer_ms']:.0f} ms · packets {r['packets']} · "
            f"lost {r['lost']} · malformed {r['malformed']} · underruns {r['underruns']} · reconnects {r['reconnects']}"
        )
        self._updating = True
        for box, val in ((self.d_mute, r["muted"]), (self.d_route, r["routed"]), (self.d_monitor, r["monitor"]),
                         (self.b_disable, r["disabled"])):
            if box.isChecked() != val:
                box.blockSignals(True)
                box.setChecked(val)
                box.blockSignals(False)
        if not self.d_gain.isSliderDown() and (force or self.d_gain.value() != round(r["gain_db"] * 10)):
            self.d_gain.setValue(round(r["gain_db"] * 10))
            self.d_gain_label.setText(f"{r['gain_db']:+.1f} dB")
        self._updating = False
        mode_single = self.core.store.settings.route_mode == "single"
        self.b_active.setText("Use as active microphone" if mode_single else "Include in mix")
        self.b_active.setEnabled(not r["routed_now"])

    def update_snapshot(self, snap: dict) -> None:
        rows = snap["devices"]
        self.model.set_rows(rows)
        if self.selected:
            for i, r in enumerate(rows):
                if r["client_id"] == self.selected:
                    if not self.table.selectionModel().isRowSelected(i, QModelIndex()):
                        self.table.selectRow(i)
                    break
            else:
                self.selected = None
        self._refresh_detail()

