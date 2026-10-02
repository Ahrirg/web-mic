"""QApplication setup and the GUI main loop."""

from __future__ import annotations

import logging
import signal
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import QApplication, QStyleFactory

from app import APP_ID, APP_NAME, __version__
from app.gui.main_window import MainWindow

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent


def make_icon() -> QIcon:
    icon = QIcon.fromTheme("audio-input-microphone")
    svg = HERE.parent / "web" / "icon.svg"
    if svg.exists():
        pm = QPixmap(str(svg))
        if not pm.isNull():
            return QIcon(pm)
    return icon


def apply_theme(app: QApplication) -> None:
    app.setStyle(QStyleFactory.create("Fusion"))
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor("#16181d"))
    pal.setColor(QPalette.WindowText, QColor("#e8eaf0"))
    pal.setColor(QPalette.Base, QColor("#12141a"))
    pal.setColor(QPalette.AlternateBase, QColor("#1f222a"))
    pal.setColor(QPalette.Text, QColor("#e8eaf0"))
    pal.setColor(QPalette.Button, QColor("#2f3440"))
    pal.setColor(QPalette.ButtonText, QColor("#e8eaf0"))
    pal.setColor(QPalette.Highlight, QColor("#4f8cff"))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ToolTipBase, QColor("#20232b"))
    pal.setColor(QPalette.ToolTipText, QColor("#e8eaf0"))
    pal.setColor(QPalette.PlaceholderText, QColor("#6b7180"))
    pal.setColor(QPalette.Link, QColor("#4f8cff"))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#6b7180"))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#6b7180"))
    app.setPalette(pal)
    qss = HERE / "style.qss"
    if qss.exists():
        app.setStyleSheet(qss.read_text())


def run_gui(core, start_minimized: bool = False) -> int:
    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationVersion(__version__)
    QApplication.setDesktopFileName(APP_ID)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    apply_theme(app)
    icon = make_icon()
    app.setWindowIcon(icon)
    window = MainWindow(core, icon)

    # Start subsystems off the GUI thread; the window shows "Starting…" meanwhile.
    def start_core():
        try:
            core.start()
        except Exception:  # noqa: BLE001
            log.exception("Startup failed")
            core.notify("error", "Startup failed. See the Diagnostics tab for details.")
            core.starting = False

    threading.Thread(target=start_core, name="core-start", daemon=True).start()

    # Let Ctrl+C in the terminal quit cleanly.
    signal.signal(signal.SIGINT, lambda *_: window.quit())
    signal.signal(signal.SIGTERM, lambda *_: window.quit())
    wake = QTimer()
    wake.start(250)
    wake.timeout.connect(lambda: None)  # gives Python a chance to run signal handlers

    if start_minimized and window.tray is not None:
        window.hide()
    else:
        window.show()
    rc = app.exec()
    window.timer.stop()
    log.info("GUI closed; stopping services")
    # wait for a still-running startup before stopping
    for _ in range(100):
        if not core.starting:
            break
        threading.Event().wait(0.1)
    core.stop()
    return rc
