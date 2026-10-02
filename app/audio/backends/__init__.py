"""Audio backends that expose the mixed stream as a virtual microphone source."""

from __future__ import annotations

import logging

from app.audio.backends.base import AudioBackend, BackendError, SourceInfo
from app.audio.backends.detect import AudioSystemInfo
from app.audio.backends.null import NullBackend

log = logging.getLogger(__name__)

__all__ = ["AudioBackend", "BackendError", "SourceInfo", "create_backend"]


def create_backend(kind: str, info: AudioSystemInfo | None, node_name: str, description: str, rate: int = 48000) -> AudioBackend:
    if kind == "auto":
        kind = info.recommended_backend() if info else "null"
    if kind == "pipewire":
        from app.audio.backends.pipewire import PipeWireBackend
        return PipeWireBackend(node_name, description, rate)
    if kind == "pulse":
        from app.audio.backends.pulse import PulseBackend
        return PulseBackend(node_name, description, rate)
    return NullBackend(node_name, description, rate)
