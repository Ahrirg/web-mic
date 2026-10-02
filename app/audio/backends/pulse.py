"""PulseAudio (or pipewire-pulse) virtual microphone using module-pipe-source.

A FIFO is created and loaded as a source with
`pactl load-module module-pipe-source`. Modules are owned by the sound server,
so they survive a crash of this application. On every start we unload any
module that references our source name before loading a new one, so a crash
never leaves a duplicate or orphaned source for long.
"""

from __future__ import annotations

import logging
import os
import stat

from app.audio.backends import procutil
from app.audio.backends.base import AudioBackend, BackendError
from app.audio.backends.detect import run
from app.config import paths

log = logging.getLogger(__name__)


def parse_modules(text: str) -> list[tuple[int, str, str]]:
    mods = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip().isdigit():
            mods.append((int(parts[0]), parts[1].strip(), parts[2] if len(parts) > 2 else ""))
    return mods


def find_our_modules(text: str, source_name: str) -> list[int]:
    """Indexes of module-pipe-source instances that created our source name."""
    token = f"source_name={source_name}"
    return sorted(
        idx for idx, name, args in parse_modules(text)
        if name == "module-pipe-source" and token in args.split()
    )


class PulseBackend(AudioBackend):
    name = "pulse"
    queue_target_ms = 30.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fifo_path = paths.runtime_dir() / f"{self.node_name}.fifo"
        self.module_index: int | None = None
        self._fd: int | None = None

    def cleanup_stale(self) -> int:
        res = run(["pactl", "list", "short", "modules"])
        if res is None or res.returncode != 0:
            return 0
        stale = find_our_modules(res.stdout, self.node_name)
        for idx in stale:
            log.warning("Unloading stale virtual microphone module #%d from a previous run", idx)
            run(["pactl", "unload-module", str(idx)])
        return len(stale)

    def start(self) -> None:
        self.info.state = "starting"
        self.info.error = ""
        if run(["pactl", "info"]) is None:
            raise BackendError("pactl was not found. Install pulseaudio-utils / libpulse.")
        self.cleanup_stale()
        self.fifo_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.fifo_path.exists() and not stat.S_ISFIFO(self.fifo_path.stat().st_mode):
                self.fifo_path.unlink()
            if not self.fifo_path.exists():
                os.mkfifo(self.fifo_path, 0o600)
        except OSError as exc:
            raise BackendError(f"Could not create FIFO {self.fifo_path}: {exc}") from None
        desc = self.description.replace('"', "'")
        cmd = [
            "pactl", "load-module", "module-pipe-source",
            f"source_name={self.node_name}",
            f"file={self.fifo_path}",
            "format=s16le", f"rate={self.rate}", f"channels={self.channels}",
            f'source_properties=device.description="{desc}"',
        ]
        res = run(cmd, timeout=6.0)
        if res is None or res.returncode != 0:
            msg = (res.stderr or res.stdout).strip() if res else "pactl timed out"
            raise BackendError(f"Could not load module-pipe-source: {msg}")
        try:
            self.module_index = int(res.stdout.strip().split()[-1])
        except (ValueError, IndexError):
            self.module_index = None
        try:
            self._fd = os.open(self.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            self.stop()
            raise BackendError(f"Could not open the virtual microphone FIFO: {exc}") from None
        try:
            import fcntl
            fcntl.fcntl(self._fd, procutil.F_SETPIPE_SZ, 4096)
        except OSError:
            pass
        self.info.state = "running"
        self.info.node_id = self.module_index
        log.info("Virtual microphone '%s' created via PulseAudio module #%s", self.description, self.module_index)

    def stop(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self.module_index is not None:
            run(["pactl", "unload-module", str(self.module_index)])
            log.info("Virtual microphone module #%d unloaded", self.module_index)
            self.module_index = None
        try:
            self.fifo_path.unlink()
        except OSError:
            pass
        self.info.state = "stopped"
        self.info.node_id = None

    def is_alive(self) -> bool:
        return self._fd is not None and self.info.state == "running"

    def write(self, pcm: bytes) -> bool:
        fd = self._fd
        if fd is None:
            return False
        try:
            n = os.write(fd, pcm)
        except BlockingIOError:
            self.write_drops += 1
            return False
        except OSError:
            self.info.state = "error"
            self.info.error = "virtual microphone FIFO closed"
            return False
        self.bytes_written += n
        return True

    def queued_ms(self) -> float | None:
        # pipewire-pulse's pipe source drains the FIFO greedily, so the FIFO fill
        # level says nothing about playback progress. Returning None makes the
        # engine pace itself with the monotonic clock instead.
        return None

    def refresh_info(self) -> None:
        if self.module_index is None:
            return
        res = run(["pactl", "list", "short", "modules"])
        if res is not None and res.returncode == 0:
            if self.module_index not in [m[0] for m in parse_modules(res.stdout)]:
                self.info.state = "error"
                self.info.error = "module was unloaded externally"
                if self._fd is not None:
                    try:
                        os.close(self._fd)
                    except OSError:
                        pass
                    self._fd = None
                self.module_index = None
        res = run(["pactl", "-f", "json", "list", "source-outputs"])
        consumers = []
        if res is not None and res.returncode == 0:
            import json
            try:
                data = json.loads(res.stdout)
            except json.JSONDecodeError:
                data = []
            src_index = self._source_index()
            for so in data:
                if src_index is not None and so.get("source") == src_index:
                    p = so.get("properties") or {}
                    consumers.append(p.get("application.name") or p.get("media.name") or "?")
        self.info.consumers = consumers

    def _source_index(self) -> int | None:
        res = run(["pactl", "list", "short", "sources"])
        if res is None:
            return None
        for line in res.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) > 1 and parts[1] == self.node_name:
                try:
                    return int(parts[0])
                except ValueError:
                    return None
        return None


