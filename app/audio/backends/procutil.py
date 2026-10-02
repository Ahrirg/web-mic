"""Helpers for helper subprocesses that must never outlive the application."""

from __future__ import annotations

import ctypes
import fcntl
import logging
import os
import signal
import subprocess
import termios
import array
import concurrent.futures
import queue
import threading

log = logging.getLogger(__name__)

PR_SET_PDEATHSIG = 1
F_SETPIPE_SZ = 1031

try:
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
except OSError:  # pragma: no cover - non-glibc systems
    _libc = None


def _die_with_parent() -> None:
    # Runs in the child between fork and exec. If the app is killed, even with
    # SIGKILL, the kernel sends SIGTERM to the helper so its PipeWire nodes vanish.
    if _libc is not None:
        _libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    os.setpgid(0, 0)  # do not receive the terminal's Ctrl+C directly


class _Spawner:
    """Runs every Popen on one long-lived thread.

    PR_SET_PDEATHSIG is delivered when the *thread* that forked the child
    exits, not when the process does. Spawning from short-lived worker threads
    would kill the helpers early, so all spawns go through this thread, which
    lives as long as the application.
    """

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def submit(self, fn, *args, **kwargs):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="helper-spawner", daemon=True)
                self._thread.start()
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._q.put((fut, fn, args, kwargs))
        return fut.result(timeout=30)

    def _run(self) -> None:
        while True:
            fut, fn, args, kwargs = self._q.get()
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001
                fut.set_exception(exc)


_spawner = _Spawner()


def spawn(cmd: list[str], *, stdin: bool = False, pipe_size: int | None = None) -> subprocess.Popen:
    log.debug("Spawning helper: %s", " ".join(cmd))
    return _spawner.submit(_spawn, cmd, stdin, pipe_size)


def _spawn(cmd: list[str], stdin: bool, pipe_size: int | None) -> subprocess.Popen:
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        preexec_fn=_die_with_parent,
        close_fds=True,
    )
    if stdin and proc.stdin is not None:
        fd = proc.stdin.fileno()
        if pipe_size:
            try:
                fcntl.fcntl(fd, F_SETPIPE_SZ, pipe_size)
            except OSError:
                log.debug("F_SETPIPE_SZ failed", exc_info=True)
        os.set_blocking(fd, False)
    return proc


def pipe_queued_bytes(fd: int) -> int | None:
    """Bytes written to a pipe/FIFO that the reader has not consumed yet."""
    buf = array.array("i", [0])
    try:
        fcntl.ioctl(fd, termios.FIONREAD, buf, True)
    except OSError:
        return None
    return buf[0]


def terminate(proc: subprocess.Popen | None, timeout: float = 2.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)
    except OSError:
        pass


def read_stderr(proc: subprocess.Popen, limit: int = 2000) -> str:
    if proc.stderr is None:
        return ""
    try:
        os.set_blocking(proc.stderr.fileno(), False)
        data = proc.stderr.read(limit) or b""
    except (OSError, ValueError):
        return ""
    return data.decode(errors="replace").strip()
