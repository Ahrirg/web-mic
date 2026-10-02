"""Optional playback of selected phones to a real output (speakers/headphones)."""

from __future__ import annotations

import logging
import os
import shutil

from app.audio.backends import procutil

log = logging.getLogger(__name__)


class MonitorOutput:
    """Plays a mono s16le stream to a sink using pw-cat (or pacat as a fallback)."""

    def __init__(self, sink: str = "", rate: int = 48000):
        self.sink = sink
        self.rate = rate
        self._proc = None
        self._fd: int | None = None
        self.error = ""

    def command(self) -> list[str]:
        if shutil.which("pw-cat"):
            cmd = ["pw-cat", "--playback", "--raw", "--format", "s16", "--rate", str(self.rate),
                   "--channels", "1", "--latency", "20ms", "--media-role", "Production",
                   "-P", '{ node.name=phone-mic-monitor application.name="Phone Mic Router" media.name="Phone monitor" }']
            if self.sink:
                cmd += ["--target", self.sink]
            return cmd + ["-"]
        cmd = ["pacat", "--playback", "--raw", "--format=s16le", f"--rate={self.rate}", "--channels=1",
               "--latency-msec=30", "--client-name=Phone Mic Router", "--stream-name=Phone monitor"]
        if self.sink:
            cmd.append(f"--device={self.sink}")
        return cmd

    def start(self) -> bool:
        self.stop()
        try:
            self._proc = procutil.spawn(self.command(), stdin=True, pipe_size=8192)
        except FileNotFoundError:
            self.error = "Neither pw-cat nor pacat is installed"
            return False
        self._fd = self._proc.stdin.fileno()
        self.error = ""
        log.info("Monitor output started%s", f" on {self.sink}" if self.sink else " on the default output")
        return True

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def write(self, pcm: bytes) -> None:
        if self._fd is None:
            return
        q = procutil.pipe_queued_bytes(self._fd)
        if q is not None and q > self.rate * 2 * 0.08:  # keep at most ~80 ms queued
            return
        try:
            os.write(self._fd, pcm)
        except BlockingIOError:
            pass
        except OSError:
            self._fd = None

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except (OSError, AttributeError):
                pass
            procutil.terminate(self._proc)
            log.info("Monitor output stopped")
        self._proc = None
        self._fd = None
