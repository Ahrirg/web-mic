"""Reusable Qt widgets: level meter, status dot, cards, QR code, background tasks."""

from __future__ import annotations

import io
import logging
from typing import Any, Callable

from PySide6.QtCore import QObject, QRectF, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

GREEN = QColor("#3ccf7a")
YELLOW = QColor("#f1b33c")
RED = QColor("#ef5350")
GREY = QColor("#6b7180")
BLUE = QColor("#4f8cff")

STATE_COLORS = {"ok": GREEN, "warn": YELLOW, "error": RED, "off": GREY, "busy": BLUE}


class StatusDot(QWidget):
    """A small coloured circle followed by a text label."""

    def __init__(self, text: str = "", state: str = "off", parent: QWidget | None = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._dot = _Dot(self)
        self.label = QLabel(text, self)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self._dot)
        lay.addWidget(self.label, 1)
        self.set(state, text)

    def set(self, state: str, text: str | None = None) -> None:
        self._dot.color = STATE_COLORS.get(state, GREY)
        self._dot.update()
        if text is not None and self.label.text() != text:
            self.label.setText(text)


class _Dot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.color = GREY
        self.setFixedSize(12, 12)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self.color)
        p.drawEllipse(1, 1, 10, 10)


class LevelMeter(QWidget):
    """Horizontal dBFS meter: RMS bar, peak-hold tick and a clip lamp."""

    MIN_DB = -60.0

    def __init__(self, parent: QWidget | None = None, compact: bool = False):
        super().__init__(parent)
        self.rms_db = -90.0
        self.peak_db = -90.0
        self.clipping = False
        self.enabled_look = True
        self.compact = compact
        self.setMinimumHeight(10 if compact else 18)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(160, 12 if self.compact else 20)

    def set_level(self, rms_db: float, peak_db: float, clipping: bool = False, active: bool = True) -> None:
        if (rms_db, peak_db, clipping, active) != (self.rms_db, self.peak_db, self.clipping, self.enabled_look):
            self.rms_db, self.peak_db, self.clipping, self.enabled_look = rms_db, peak_db, clipping, active
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        paint_meter(p, QRectF(0, 0, self.width(), self.height()), self.rms_db, self.peak_db,
                    self.clipping, self.enabled_look, lamp=not self.compact)


def _meter_x(db: float, width: float, min_db: float = LevelMeter.MIN_DB) -> float:
    frac = (max(min_db, min(0.0, db)) - min_db) / -min_db
    return frac * width


def paint_meter(p: QPainter, area: QRectF, rms_db: float, peak_db: float, clipping: bool,
                active: bool = True, lamp: bool = True) -> None:
    """Draw a dBFS meter into `area`. Shared by the widget and table delegates."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    lamp_w = 14 if lamp else 0
    r = QRectF(area.left() + 0.5, area.top() + 0.5, area.width() - 1 - lamp_w - (4 if lamp_w else 0), area.height() - 1)
    p.setPen(QPen(QColor("#2e323d"), 1))
    p.setBrush(QColor("#12141a"))
    p.drawRoundedRect(r, 3, 3)
    w = r.width() - 2
    grad = QLinearGradient(r.left(), 0, r.right(), 0)
    grad.setColorAt(0.0, GREEN)
    grad.setColorAt(0.70, GREEN)
    grad.setColorAt(0.75, YELLOW)
    grad.setColorAt(0.88, YELLOW)
    grad.setColorAt(0.92, RED)
    x = _meter_x(rms_db, w)
    if x > 0:
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.setOpacity(1.0 if active else 0.35)
        p.drawRoundedRect(QRectF(r.left() + 1, r.top() + 1, x, r.height() - 2), 2, 2)
        p.setOpacity(1.0)
    px = _meter_x(peak_db, w)
    if px > 1:
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.drawLine(int(r.left() + 1 + px), int(r.top() + 2), int(r.left() + 1 + px), int(r.bottom() - 2))
    p.setPen(QPen(QColor(255, 255, 255, 40), 1))
    for db in (-48, -36, -24, -12, -6):
        tx = r.left() + 1 + _meter_x(db, w)
        p.drawLine(int(tx), int(r.bottom() - 3), int(tx), int(r.bottom() - 1))
    if lamp_w:
        p.setPen(Qt.NoPen)
        p.setBrush(RED if clipping else QColor("#3a2224"))
        p.drawRoundedRect(QRectF(area.right() - lamp_w, area.top() + 1, lamp_w - 1, area.height() - 2), 3, 3)
    p.restore()


class Card(QFrame):
    """A titled panel."""

    def __init__(self, title: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setFrameShape(QFrame.NoFrame)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 12, 16, 14)
        self.body.setSpacing(8)
        if title:
            t = QLabel(title.upper(), self)
            t.setObjectName("cardTitle")
            self.body.addWidget(t)


def big_label(text: str = "", size: int = 15, bold: bool = True) -> QLabel:
    lbl = QLabel(text)
    f = lbl.font()
    f.setPointSize(size)
    f.setBold(bold)
    lbl.setFont(f)
    lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lbl


def muted_label(text: str = "", wrap: bool = True) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("muted")
    lbl.setWordWrap(wrap)
    lbl.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
    lbl.setOpenExternalLinks(False)
    return lbl


def mono_font(size: int = 10) -> QFont:
    f = QFont("monospace")
    f.setStyleHint(QFont.Monospace)
    f.setPointSize(size)
    return f


class CopyButton(QToolButton):
    def __init__(self, getter: Callable[[], str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setText("Copy")
        self.setToolTip("Copy to clipboard")
        self._getter = getter
        self.clicked.connect(lambda: QGuiApplication.clipboard().setText(self._getter()))


def qr_pixmap(text: str, size: int = 200) -> QPixmap:
    pm = QPixmap()
    try:
        import segno
    except ImportError:
        return pm
    qr = segno.make(text, error="m")
    buf = io.BytesIO()
    modules = qr.symbol_size(scale=1, border=2)[0]
    qr.save(buf, kind="png", scale=max(2, size // modules), border=2, dark="#000000", light="#ffffff")
    pm.loadFromData(buf.getvalue(), "PNG")
    return pm


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_db(db: float | None) -> str:
    if db is None or db <= -89.5:
        return "-∞ dBFS"
    return f"{db:.0f} dBFS"


class _TaskSignals(QObject):
    done = Signal(object, object)  # result, exception


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn
        self.signals = _TaskSignals()

    def run(self) -> None:
        try:
            res = self.fn()
        except Exception as exc:  # noqa: BLE001
            log.exception("Background task failed")
            self.signals.done.emit(None, exc)
        else:
            self.signals.done.emit(res, None)


_running_tasks: set = set()


def run_in_background(fn: Callable[[], Any], on_done: Callable[[Any, Exception | None], None] | None = None) -> None:
    """Run a blocking call (subprocess, network) off the GUI thread."""
    task = _Task(fn)
    _running_tasks.add(task.signals)

    def finished(res, exc, _sig=task.signals):
        _running_tasks.discard(_sig)
        if on_done:
            on_done(res, exc)

    task.signals.done.connect(finished, Qt.QueuedConnection)
    QThreadPool.globalInstance().start(task)
