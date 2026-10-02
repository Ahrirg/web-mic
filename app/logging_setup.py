"""Logging configuration: rotating file, console and an in-memory tail for the GUI."""

from __future__ import annotations

import collections
import logging
import logging.handlers
import threading

from app.config import paths

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class MemoryLogHandler(logging.Handler):
    """Keeps the most recent formatted records for display in the GUI."""

    def __init__(self, capacity: int = 1000):
        super().__init__()
        self.records: collections.deque[str] = collections.deque(maxlen=capacity)
        self._counter = 0
        self._lock2 = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:  # pragma: no cover
            return
        with self._lock2:
            self.records.append(msg)
            self._counter += 1

    def snapshot(self) -> tuple[int, list[str]]:
        with self._lock2:
            return self._counter, list(self.records)


memory_handler = MemoryLogHandler()


def setup_logging(debug: bool = False, to_file: bool = True) -> str | None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter(FORMAT)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    memory_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    memory_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    root.addHandler(memory_handler)

    log_path = None
    if to_file:
        try:
            d = paths.log_dir()
            d.mkdir(parents=True, exist_ok=True)
            log_path = str(d / "phone-mic-router.log")
            fh = logging.handlers.RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3)
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as exc:
            logging.getLogger(__name__).warning("File logging disabled: %s", exc)
            log_path = None

    for noisy in ("aiohttp.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_path
