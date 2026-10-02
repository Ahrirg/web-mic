"""Audio backend interface: a virtual microphone SOURCE fed with s16le mono PCM."""

from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class SourceInfo:
    backend: str
    node_name: str
    description: str
    state: str = "stopped"  # stopped | starting | running | error
    node_id: int | None = None
    object_serial: int | None = None
    helper_node_name: str = ""
    helper_node_id: int | None = None
    error: str = ""
    consumers: list[str] = field(default_factory=list)  # applications recording from the source
    restarts: int = 0


class BackendError(RuntimeError):
    pass


class AudioBackend(abc.ABC):
    """A virtual microphone. Implementations must be safe to call from the engine thread."""

    name = "base"
    #: target amount of audio kept queued inside the backend, in ms
    queue_target_ms = 20.0

    def __init__(self, node_name: str, description: str, rate: int = 48000, channels: int = 1):
        self.node_name = node_name
        self.description = description
        self.rate = rate
        self.channels = channels
        self.info = SourceInfo(backend=self.name, node_name=node_name, description=description)
        self.bytes_written = 0
        self.write_drops = 0
        self._last_restart = 0.0

    @abc.abstractmethod
    def start(self) -> None:
        """Create the virtual source. Raises BackendError with a user-friendly message."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Remove the virtual source and any helper processes/modules."""

    @abc.abstractmethod
    def write(self, pcm: bytes) -> bool:
        """Non-blocking write of s16le PCM. Returns False if the data was dropped."""

    @abc.abstractmethod
    def is_alive(self) -> bool:
        ...

    def queued_ms(self) -> float | None:
        """Audio currently queued in the backend, or None if unknown (engine uses its own clock)."""
        return None

    def refresh_info(self) -> None:
        """Update node ids/consumers. Called periodically from a supervisor thread."""

    def ensure_running(self, min_interval: float = 3.0) -> bool:
        """Restart the source if it died (for example after a PipeWire restart)."""
        if self.is_alive():
            return True
        now = time.monotonic()
        if now - self._last_restart < min_interval:
            return False
        self._last_restart = now
        log.warning("Virtual microphone backend %s is not running; recreating it", self.name)
        try:
            self.stop()
            self.start()
            self.info.restarts += 1
            return True
        except BackendError as exc:
            self.info.state = "error"
            self.info.error = str(exc)
            log.error("Could not recreate virtual microphone: %s", exc)
            return False
