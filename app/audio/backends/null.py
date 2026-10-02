"""Backend that discards audio. Used for tests and when no sound server exists."""

from __future__ import annotations

import collections

from app.audio.backends.base import AudioBackend


class NullBackend(AudioBackend):
    name = "null"

    def __init__(self, *args, keep_last: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self._running = False
        self.chunks: collections.deque[bytes] = collections.deque(maxlen=keep_last or None) if keep_last else collections.deque(maxlen=1)
        self.keep = bool(keep_last)

    def start(self) -> None:
        self._running = True
        self.info.state = "running"
        self.info.error = ""

    def stop(self) -> None:
        self._running = False
        self.info.state = "stopped"

    def write(self, pcm: bytes) -> bool:
        if not self._running:
            return False
        self.bytes_written += len(pcm)
        if self.keep:
            self.chunks.append(pcm)
        return True

    def is_alive(self) -> bool:
        return self._running
