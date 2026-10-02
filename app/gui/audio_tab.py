"""Audio tab: routing, virtual microphone, buffering, gain and the system's sources/sinks."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import BUFFER_CHOICES_MS
from app.gui.widgets import Card, LevelMeter, StatusDot, fmt_db, muted_label, run_in_background


class AudioTab(QWidget):
    def __init__(self, core, parent=None):
        super().__init__(parent)
        self.core = core
        self._last_lists = None
        self._last_devices = None
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

        # --- routing diagram ---
        c = Card("Audio routing")
        c.body.addWidget(muted_label("Phone input  →  virtual microphone SOURCE  →  applications. "
                                     "A microphone is an audio source; applications record from it."))
        self.inputs_list = QListWidget()
        self.inputs_list.setMaximumHeight(110)
        c.body.addWidget(QLabel("<b>Input</b> (phones/browsers feeding the virtual microphone)"))
        c.body.addWidget(self.inputs_list)
        arrow = QLabel("↓")
        arrow.setObjectName("arrow")
        arrow.setAlignment(Qt.AlignCenter)
        c.body.addWidget(arrow)
        self.target_label = QLabel()
        self.target_label.setObjectName("routeTarget")
        self.target_label.setWordWrap(True)
        self.target_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        c.body.addWidget(self.target_label)
        self.master_meter = LevelMeter()
        c.body.addWidget(self.master_meter)
        self.master_level = muted_label(wrap=False)
        c.body.addWidget(self.master_level)
        arrow = QLabel("↓")
        arrow.setObjectName("arrow")
        arrow.setAlignment(Qt.AlignCenter)
        c.body.addWidget(arrow)
        self.apps_label = QLabel()
        self.apps_label.setObjectName("routeNode")
        self.apps_label.setWordWrap(True)
        c.body.addWidget(self.apps_label)
        grid.addWidget(c, 0, 0, 2, 1)

        # --- routing controls ---
        c = Card("Routing")
        form = QFormLayout()
        mode_row = QHBoxLayout()
        self.mode_single = QRadioButton("One active device")
        self.mode_mix = QRadioButton("Mix all routed devices")
        grp = QButtonGroup(self)
        grp.addButton(self.mode_single)
        grp.addButton(self.mode_mix)
        self.mode_single.toggled.connect(lambda on: on and not self._updating and self.core.set_setting(route_mode="single"))
        self.mode_mix.toggled.connect(lambda on: on and not self._updating and self.core.set_setting(route_mode="mix"))
        mode_row.addWidget(self.mode_single)
        mode_row.addWidget(self.mode_mix)
        mode_row.addStretch(1)
        form.addRow("Mode", mode_row)
        self.active_combo = QComboBox()
        self.active_combo.activated.connect(self._active_chosen)
        form.addRow("Active device", self.active_combo)
        self.buffer_combo = QComboBox()
        for ms in BUFFER_CHOICES_MS:
            self.buffer_combo.addItem(f"{ms} ms" + ("  (default)" if ms == 40 else ""), ms)
        self.buffer_combo.activated.connect(lambda i: self.core.set_buffer_ms(self.buffer_combo.itemData(i)))
        self.buffer_combo.setToolTip("Jitter buffer per device. Smaller = lower latency; larger = fewer dropouts on busy Wi-Fi.")
        form.addRow("Jitter buffer", self.buffer_combo)
        gain_row = QHBoxLayout()
        self.master_gain = QSlider(Qt.Horizontal)
        self.master_gain.setRange(-300, 240)
        self.master_gain.valueChanged.connect(self._master_gain)
        self.master_gain_label = QLabel("0.0 dB")
        self.master_gain_label.setMinimumWidth(60)
        gain_row.addWidget(self.master_gain, 1)
        gain_row.addWidget(self.master_gain_label)
        form.addRow("Output gain", gain_row)
        self.monitor_combo = QComboBox()
        self.monitor_combo.activated.connect(lambda i: self.core.set_setting(monitor_sink=self.monitor_combo.itemData(i) or ""))
        self.monitor_combo.setToolTip("Where devices with 'Also play on output' enabled are heard")
        form.addRow("Monitor output", self.monitor_combo)
        c.body.addLayout(form)
        c.body.addWidget(muted_label("Per-device gain, mute and routing are on the Devices tab."))
        grid.addWidget(c, 0, 1)

        # --- virtual microphone ---
        c = Card("Virtual microphone (source)")
        self.vm_status = StatusDot("–")
        c.body.addWidget(self.vm_status)
        self.vm_detail = muted_label()
        c.body.addWidget(self.vm_detail)
        form = QFormLayout()
        self.vm_desc = QLineEdit()
        self.vm_desc.setPlaceholderText("Phone Microphone")
        self.vm_name = QLineEdit()
        self.vm_name.setPlaceholderText("phone-mic")
        self.vm_backend = QComboBox()
        for label, val in (("Automatic (PipeWire preferred)", "auto"), ("PipeWire (pw-loopback)", "pipewire"),
                           ("PulseAudio module-pipe-source", "pulse"), ("None (testing)", "null")):
            self.vm_backend.addItem(label, val)
        form.addRow("Display name", self.vm_desc)
        form.addRow("Node name", self.vm_name)
        form.addRow("Backend", self.vm_backend)
        c.body.addLayout(form)
        row = QHBoxLayout()
        self.vm_apply = QPushButton("Apply && recreate")
        self.vm_apply.clicked.connect(self._recreate)
        row.addWidget(self.vm_apply)
        row.addStretch(1)
        c.body.addLayout(row)
        grid.addWidget(c, 1, 1)

        # --- system devices ---
        c = Card("PipeWire / PulseAudio devices")
        self.dev_tree = QTreeWidget()
        self.dev_tree.setHeaderLabels(["Name", "Node / source name", "State", "Format"])
        self.dev_tree.setRootIsDecorated(True)
        self.dev_tree.setMinimumHeight(220)
        c.body.addWidget(self.dev_tree)
        r = QHBoxLayout()
        b = QPushButton("Refresh")
        b.clicked.connect(lambda: run_in_background(self.core.redetect_audio))
        r.addWidget(b)
        r.addWidget(muted_label("Sources are microphones/inputs; sinks are speakers/outputs. "
                                "The internal feed sink is plumbing for the virtual microphone; do not select it as a speaker."))
        c.body.addLayout(r)
        grid.addWidget(c, 2, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self._loaded_settings = False

    # ------------------------------------------------------------------
    def _active_chosen(self, idx: int) -> None:
        cid = self.active_combo.itemData(idx) or ""
        self.core.set_active_client(cid)

    def _master_gain(self, v: int) -> None:
        self.master_gain_label.setText(f"{v / 10:+.1f} dB")
        if not self._updating:
            self.core.set_setting(master_gain_db=v / 10)

    _updating = False

    def _recreate(self) -> None:
        desc = self.vm_desc.text().strip() or "Phone Microphone"
        name = self.vm_name.text().strip() or "phone-mic"
        backend = self.vm_backend.currentData()
        self.vm_apply.setEnabled(False)
        self.vm_status.set("busy", "Recreating…")

        def done(_res, exc):
            self.vm_apply.setEnabled(True)
            if exc:
                QMessageBox.warning(self, "Virtual microphone", f"Could not recreate the virtual microphone:\n{exc}")
            elif self.core.backend_error:
                QMessageBox.warning(self, "Virtual microphone", self.core.backend_error)

        run_in_background(lambda: self.core.set_virtual_source(name, desc, backend), done)

    def update_snapshot(self, snap: dict) -> None:
        a = snap["audio"]
        s = self.core.store.settings
        self._updating = True
        if not self._loaded_settings:
            self.vm_desc.setText(s.source_description)
            self.vm_name.setText(s.source_name)
            self.vm_backend.setCurrentIndex(max(0, self.vm_backend.findData(s.backend)))
            self._loaded_settings = True
        (self.mode_single if a["route_mode"] == "single" else self.mode_mix).setChecked(True)
        self.active_combo.setEnabled(a["route_mode"] == "single")
        bi = self.buffer_combo.findData(a["buffer_ms"])
        if bi >= 0 and self.buffer_combo.currentIndex() != bi:
            self.buffer_combo.setCurrentIndex(bi)
        if not self.master_gain.isSliderDown():
            v = round(a["master_gain_db"] * 10)
            if self.master_gain.value() != v:
                self.master_gain.setValue(v)
                self.master_gain_label.setText(f"{v / 10:+.1f} dB")
        self._updating = False

        devices = snap["devices"]
        connected = [d for d in devices if d["connected"]]
        key = [(d["client_id"], d["name"]) for d in connected] + [s.active_client]
        if key != self._last_devices:
            self._last_devices = key
            self.active_combo.clear()
            self.active_combo.addItem("Automatic (most recently connected)", "")
            for d in connected:
                self.active_combo.addItem(d["name"], d["client_id"])
            i = self.active_combo.findData(s.active_client)
            self.active_combo.setCurrentIndex(i if i >= 0 else 0)

        # inputs list
        self.inputs_list.clear()
        for d in devices:
            if not d["connected"]:
                continue
            mark = "→" if d["routed_now"] else " "
            fmt = f"{d['sample_rate'] / 1000:g} kHz mono" if d["sample_rate"] else "no audio yet"
            extra = " · muted" if d["muted"] else (" · disabled" if d["disabled"] else ("" if d["routed"] else " · not routed"))
            item = QListWidgetItem(f"{mark} {d['name']}  ·  {d['state']}  ·  {fmt}  ·  gain {d['gain_db']:+.1f} dB{extra}")
            if d["routed_now"]:
                f = item.font()
                f.setBold(True)
                item.setFont(f)
            self.inputs_list.addItem(item)
        if self.inputs_list.count() == 0:
            self.inputs_list.addItem("No device connected")

        src = a["source"]
        state = {"running": "ok", "error": "error", "starting": "busy"}.get(src["state"], "off")
        self.vm_status.set(state, {"running": "Created and running", "error": "Error", "starting": "Starting…",
                                   "stopped": "Not created"}.get(src["state"], src["state"]))
        node = f"node.name <b>{src['node_name']}</b>"
        if src["node_id"] is not None:
            node += f" · id {src['node_id']}"
        if src.get("object_serial"):
            node += f" · serial {src['object_serial']}"
        helper = f"<br>Internal feed sink: {src['helper_node_name']} (id {src['helper_node_id']})" if src["helper_node_name"] else ""
        self.vm_detail.setText(f"{node} · backend {a['backend']}{helper}" + (f"<br><span style='color:#ef5350'>{a['error']}</span>" if a["error"] else ""))
        mute = " · <span style='color:#f1b33c'>MUTED</span>" if a["master_muted"] else ""
        self.target_label.setText(
            f"Sending audio to:<br><span style='font-size:14pt'>{src['description']}</span>{mute}<br>"
            f"<span style='font-weight:400;color:#9aa0ad'>{src['node_name']}"
            f"{'' if src['node_id'] is None else ' · node id ' + str(src['node_id'])} · 48000 Hz mono s16</span>"
        )
        self.master_meter.set_level(a["master_rms_db"], a["master_peak_db"], a["master_clipping"], not a["master_muted"])
        self.master_level.setText(f"Output {fmt_db(a['master_rms_db'])} · peak {fmt_db(a['master_peak_db'])}"
                                  + ("  · CLIPPING" if a["master_clipping"] else "")
                                  + (f" · backend queue {a['backend_queue_ms']:.0f} ms" if a["backend_queue_ms"] is not None else ""))
        apps = src["consumers"]
        other = [x for x in a["recording_apps"] if x not in apps]
        txt = "<b>Applications recording from it</b><br>" + ("<br>".join(apps) if apps else
                                                            f"Nobody yet. Select “{src['description']}” as the input device in your application.")
        if other:
            txt += "<br><span style='color:#9aa0ad'>Other recording apps: " + ", ".join(other) + "</span>"
        self.apps_label.setText(txt)

        lists = (a["devices"], a["system"]["default_source"], a["system"]["default_sink"])
        if lists != self._last_lists:
            self._last_lists = lists
            self._fill_tree(a, src)
            self.monitor_combo.clear()
            self.monitor_combo.addItem("Default output", "")
            for d in a["devices"]["sinks"]:
                if d["name"] != src.get("helper_node_name"):
                    self.monitor_combo.addItem(d["description"], d["name"])
            i = self.monitor_combo.findData(s.monitor_sink)
            self.monitor_combo.setCurrentIndex(i if i >= 0 else 0)

    def _fill_tree(self, a: dict, src: dict) -> None:
        self.dev_tree.clear()
        for kind, title in (("sources", "Sources (microphones / inputs)"), ("sinks", "Sinks (speakers / outputs)")):
            top = QTreeWidgetItem([title])
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            self.dev_tree.addTopLevelItem(top)
            for d in a["devices"][kind]:
                if d["is_monitor"]:
                    continue
                desc = d["description"]
                if d["is_default"]:
                    desc += "  (default)"
                if d["name"] == src["node_name"]:
                    desc += "  ← virtual microphone"
                if d["name"] == src.get("helper_node_name"):
                    desc += "  (internal feed)"
                it = QTreeWidgetItem([desc, d["name"], d["state"], d["sample_spec"]])
                if d["name"] == src["node_name"]:
                    for col in range(4):
                        it.setForeground(col, Qt.GlobalColor.cyan)
                top.addChild(it)
            top.setExpanded(True)
        for col in range(4):
            self.dev_tree.resizeColumnToContents(col)
