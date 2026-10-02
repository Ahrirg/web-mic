"""Offscreen GUI smoke test: every tab renders a live snapshot without errors."""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.audio.backends.null import NullBackend  # noqa: E402
from app.config.settings import ConfigStore  # noqa: E402
from app.core import AppCore  # noqa: E402
from app.server.protocol import AudioConfig, AudioPacket  # noqa: E402
from tests.conftest import free_port  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_all_tabs_render_with_a_streaming_device(qapp, tmp_path):
    from app.gui.app import apply_theme, make_icon
    from app.gui.main_window import MainWindow

    store = ConfigStore(tmp_path / "config.json")
    store.settings.port = free_port()
    store.settings.https_port = free_port()
    store.settings.host = "127.0.0.1"
    core = AppCore(store, backend=NullBackend("phone-mic", "Phone Microphone"), enable_adb=False)
    core.start()
    try:
        sess = core.registry.register("pixel", "192.168.1.20", "Pixel 8 (Chrome)", connection="Wi-Fi/LAN",
                                      browser="Chrome", platform="Android", secure=True)
        core.registry.configure_audio(sess, AudioConfig(48000, 1), 48000, 40)
        pcm = (0.3 * np.sin(np.arange(480) / 5) * 32767).astype("<i2").tobytes()
        for i in range(20):
            sess.push_packet(AudioPacket(i, 0.0, 0, pcm))
        for _ in range(10):
            core.engine.process_block()

        apply_theme(qapp)
        w = MainWindow(core, make_icon())
        w.resize(1200, 800)
        w.show()
        for i in range(w.tabs.count()):
            w.tabs.setCurrentIndex(i)
            w.refresh()
            qapp.processEvents()
            assert not w.grab().isNull()

        # Devices tab interactions go through the core
        w.show_tab("Devices")
        w.refresh()
        w.devices.table.selectRow(0)
        qapp.processEvents()
        assert w.devices.selected == "pixel"
        w.devices.d_alias.setText("Desk phone")
        w.devices._rename()
        w.devices.d_mute.setChecked(True)
        w.devices.d_gain.setValue(60)
        w.refresh()
        dev = store.settings.devices["pixel"]
        assert dev.alias == "Desk phone" and dev.muted and dev.gain_db == pytest.approx(6.0)
        row = w.devices.model.rows[0]
        assert row["name"] == "Desk phone" and row["routed_now"]

        # Dashboard shows the routed device and the virtual source
        w.show_tab("Dashboard")
        w.refresh()
        assert "Desk phone" in w.dashboard.input_name.text()
        assert "Phone Microphone" in w.dashboard.route_target.text()
        w.quitting = True
        w.close()
    finally:
        core.stop()
