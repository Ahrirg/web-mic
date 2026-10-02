"""PipeWire virtual microphone.

PipeWire has no command-line tool that creates a free-standing source node fed
from stdin. The documented way to build a virtual source is a loopback whose
playback side has media.class=Audio/Source. This backend runs:

  pw-loopback   capture side:  Audio/Sink   "<name>-input"  (internal feed)
                playback side: Audio/Source "<name>"         (the virtual microphone)
  pw-cat        plays our PCM from a pipe into "<name>-input"

Applications record from "<name>" ("Phone Microphone"). The helper sink shows
up in sink lists as "Phone Microphone (internal feed)". Do not select it as
a speaker.

Both nodes belong to the helper processes, which receive SIGTERM through
PR_SET_PDEATHSIG if this application dies. PipeWire removes the nodes when the
processes exit, so no orphaned sources are left behind, even after a crash.
"""

from __future__ import annotations

import logging
import os
import time

from app.audio.backends import procutil
from app.audio.backends.base import AudioBackend, BackendError
from app.audio.backends.detect import consumers_of, find_nodes, node_props, pw_dump

log = logging.getLogger(__name__)


def _spa_quote(value: str) -> str:
    return '"' + value.replace("\\", "").replace('"', "'") + '"'


class PipeWireBackend(AudioBackend):
    name = "pipewire"
    queue_target_ms = 20.0
    graph_latency_ms = 10

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper_name = f"{self.node_name}-input"
        self.feeder_name = f"{self.node_name}-feeder"
        self._loopback = None
        self._feeder = None
        self._fd: int | None = None
        self.info.helper_node_name = self.helper_name

    # ------------------------------------------------------------------
    def loopback_cmd(self) -> list[str]:
        desc = self.description
        capture = (
            f"media.class=Audio/Sink node.name={self.helper_name} "
            f"node.description={_spa_quote(desc + ' (internal feed)')} "
            "audio.position=[MONO] node.virtual=true"
        )
        playback = (
            f"media.class=Audio/Source node.name={self.node_name} "
            f"node.description={_spa_quote(desc)} node.nick={_spa_quote(desc)} "
            "audio.position=[MONO] device.icon-name=audio-input-microphone "
            "application.name=\"Phone Mic Router\""
        )
        return [
            "pw-loopback",
            "--name", f"{self.node_name}-loopback",
            "--channels", str(self.channels),
            "--channel-map", "[ MONO ]",
            "--latency", str(self.graph_latency_ms),
            "--capture-props", capture,
            "--playback-props", playback,
        ]

    def feeder_cmd(self) -> list[str]:
        props = (
            f"{{ node.name={self.feeder_name} target.object={self.helper_name} "
            "node.dont-fallback=true node.dont-reconnect=true "
            "application.name=\"Phone Mic Router\" media.name=\"Phone audio feed\" }"
        )
        return [
            "pw-cat", "--playback", "--raw",
            "--format", "s16", "--rate", str(self.rate), "--channels", str(self.channels),
            "--channel-map", "MONO" if self.channels == 1 else "FL,FR",
            "--latency", f"{self.graph_latency_ms}ms",
            "--target", self.helper_name,
            "--media-role", "Communication",
            "-P", props,
            "-",
        ]

    # ------------------------------------------------------------------
    def start(self) -> None:
        self.info.state = "starting"
        self.info.error = ""
        dump = pw_dump()
        existing = find_nodes(dump, self.node_name) + find_nodes(dump, self.helper_name)
        if existing:
            owners = {node_props(n).get("application.process.id") for n in existing}
            raise BackendError(
                f"An audio node named '{self.node_name}' already exists (owned by process {', '.join(map(str, owners))}). "
                "Is another Phone Mic Router running? Change the virtual source name in Settings or stop the other instance."
            )
        try:
            self._loopback = procutil.spawn(self.loopback_cmd())
        except FileNotFoundError:
            raise BackendError("pw-loopback was not found. Install the PipeWire tools package (pipewire / pipewire-bin).") from None
        if not self._wait_for_node(self.helper_name, 4.0):
            err = procutil.read_stderr(self._loopback)
            self.stop()
            raise BackendError(f"PipeWire did not create the virtual microphone node. {err}".strip())
        try:
            # 4 KiB pipe = ~42 ms of 48 kHz mono s16; the engine keeps ~20 ms queued.
            self._feeder = procutil.spawn(self.feeder_cmd(), stdin=True, pipe_size=4096)
        except FileNotFoundError:
            self.stop()
            raise BackendError("pw-cat was not found. Install the PipeWire tools package.") from None
        self._fd = self._feeder.stdin.fileno()
        time.sleep(0.2)
        if self._feeder.poll() is not None:
            err = procutil.read_stderr(self._feeder)
            self.stop()
            raise BackendError(f"pw-cat failed to connect to the virtual microphone: {err}")
        self.info.state = "running"
        self.refresh_info()
        log.info(
            "Virtual microphone '%s' (%s) created via PipeWire, node id %s",
            self.description, self.node_name, self.info.node_id,
        )

    def _wait_for_node(self, name: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._loopback is not None and self._loopback.poll() is not None:
                return False
            if find_nodes(pw_dump(), name):
                return True
            time.sleep(0.1)
        return False

    def stop(self) -> None:
        if self._feeder is not None:
            try:
                if self._feeder.stdin:
                    self._feeder.stdin.close()
            except OSError:
                pass
        procutil.terminate(self._feeder)
        procutil.terminate(self._loopback)
        if self._loopback is not None or self._feeder is not None:
            log.info("Virtual microphone '%s' removed", self.node_name)
        self._feeder = None
        self._loopback = None
        self._fd = None
        self.info.state = "stopped"
        self.info.node_id = None
        self.info.helper_node_id = None
        self.info.consumers = []

    def is_alive(self) -> bool:
        return (
            self._loopback is not None and self._loopback.poll() is None
            and self._feeder is not None and self._feeder.poll() is None
        )

    def write(self, pcm: bytes) -> bool:
        fd = self._fd
        if fd is None:
            return False
        try:
            n = os.write(fd, pcm)
        except BlockingIOError:
            self.write_drops += 1
            return False
        except (BrokenPipeError, OSError):
            self._fd = None
            return False
        self.bytes_written += n
        if n < len(pcm):
            # Partial write: the rest is dropped to keep latency bounded. Rare
            # because the engine checks the queue first.
            self.write_drops += 1
        return True

    def queued_ms(self) -> float | None:
        fd = self._fd
        if fd is None:
            return None
        q = procutil.pipe_queued_bytes(fd)
        if q is None:
            return None
        return q / (self.rate * 2 * self.channels) * 1000.0

    def refresh_info(self) -> None:
        if not self.is_alive():
            if self.info.state == "running":
                self.info.state = "error"
                self.info.error = "PipeWire helper process exited"
            return
        dump = pw_dump()
        src = find_nodes(dump, self.node_name)
        helper = find_nodes(dump, self.helper_name)
        if src:
            self.info.node_id = src[0].get("id")
            self.info.object_serial = node_props(src[0]).get("object.serial")
            self.info.consumers = consumers_of(dump, self.info.node_id)
        else:
            self.info.node_id = None
            self.info.consumers = []
        self.info.helper_node_id = helper[0].get("id") if helper else None
