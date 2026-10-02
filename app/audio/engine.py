"""Audio engine: the dedicated thread that turns client packets into the virtual mic stream.

    WebSocket receiver (server thread)
          -> bounded per-client packet queue
          -> per-client jitter buffer (resampler + ring buffer + drift control)
          -> gain / mute -> mixer -> soft limiter
          -> backend (PipeWire / PulseAudio virtual source)

The engine produces 10 ms blocks. With a real backend it is paced by how much
audio is still queued in the backend pipe, so its clock follows the sound
card clock and the pipe stays short. Without that information it uses the
monotonic clock.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from app.audio.backends.base import AudioBackend
from app.audio.meter import LevelMeter, db_to_gain
from app.audio.mixer import float_to_s16, mix, s16_to_float
from app.devices.registry import DISCONNECTED, ClientSession, DeviceRegistry

log = logging.getLogger(__name__)

BLOCK_MS = 10


@dataclass
class RouteDecision:
    active: list[str]  # client ids feeding the virtual mic
    mode: str


class AudioEngine:
    def __init__(
        self,
        registry: DeviceRegistry,
        backend: AudioBackend,
        settings_getter: Callable,
        rate: int = 48000,
    ):
        self.registry = registry
        self.backend = backend
        self._settings = settings_getter
        self.rate = rate
        self.block = rate * BLOCK_MS // 1000
        self.master_meter = LevelMeter()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._backend_lock = threading.Lock()
        self.monitor = None  # MonitorOutput, set by the core
        self.blocks_produced = 0
        self.late_blocks = 0
        self.backend_stalls = 0
        self.route = RouteDecision([], "single")
        self.running = False
        self.cpu_load = 0.0  # fraction of real time spent processing
        self._silence_f = np.zeros(self.block, dtype=np.float32)
        self._silence = bytes(self.block * 2)

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="audio-engine", daemon=True)
        self._thread.start()
        self.running = True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self.running = False

    def swap_backend(self, backend: AudioBackend) -> AudioBackend:
        with self._backend_lock:
            old, self.backend = self.backend, backend
            return old

    # ------------------------------------------------------------------
    def choose_route(self, sessions: list[ClientSession]) -> RouteDecision:
        s = self._settings()
        candidates = []
        for sess in sessions:
            if sess.state == DISCONNECTED or sess.jitter is None:
                continue
            dev = s.devices.get(sess.client_id)
            if dev is not None and (dev.disabled or not dev.routed):
                continue
            candidates.append(sess)
        if s.route_mode == "mix":
            return RouteDecision([c.client_id for c in candidates], "mix")
        if s.active_client and any(c.client_id == s.active_client for c in candidates):
            return RouteDecision([s.active_client], "single")
        # Fall back to the most recently connected routed device.
        if candidates:
            newest = max(candidates, key=lambda c: c.connected_at)
            return RouteDecision([newest.client_id], "single")
        return RouteDecision([], "single")

    def process_block(self, now: float | None = None) -> bytes:
        """Produce one block. Separated from the thread loop so it can be unit tested."""
        s = self._settings()
        sessions = self.registry.sessions()
        if not sessions:
            # Nothing connected: skip all DSP and emit cached silence.
            self.route = RouteDecision([], s.route_mode)
            if self.master_meter.rms_db > -90.0:
                self.master_meter.update(self._silence_f, now)
            self.blocks_produced += 1
            return self._silence
        self.route = route = self.choose_route(sessions)
        active = set(route.active)
        to_mix: list[tuple[np.ndarray, float]] = []
        to_monitor: list[tuple[np.ndarray, float]] = []
        for sess in sessions:
            jb = sess.jitter
            if jb is None:
                continue
            if sess.pending_flush:
                jb.flush()
                sess.pending_flush = False
            if jb.target != int(self.rate * s.buffer_ms / 1000):
                jb.set_target_ms(s.buffer_ms)
            channels = sess.audio_config.channels if sess.audio_config else 1
            # Drain the packet queue into the jitter buffer.
            while sess.queue:
                try:
                    payload = sess.queue.popleft()
                except IndexError:
                    break
                samples = s16_to_float(payload, channels)
                sess.meter.update(samples, now)
                jb.push(samples)
            if sess.state == DISCONNECTED and jb.fill == 0:
                continue
            block = jb.pull(self.block)
            dev = s.devices.get(sess.client_id)
            if dev is None:
                gain = 1.0
            elif dev.muted or dev.disabled:
                gain = 0.0
            else:
                gain = db_to_gain(dev.gain_db)
            if sess.client_id in active:
                to_mix.append((block, gain))
            if dev is not None and dev.monitor:
                to_monitor.append((block, gain))
        master = 0.0 if s.master_muted else db_to_gain(s.master_gain_db)
        out = mix(to_mix, self.block, master)
        self.master_meter.update(out, now)
        pcm = float_to_s16(out)
        if to_monitor and self.monitor is not None:
            self.monitor.write(float_to_s16(mix(to_monitor, self.block, 1.0)))
        self.blocks_produced += 1
        return pcm

    def _run(self) -> None:
        log.info("Audio engine started (%d Hz, %d ms blocks)", self.rate, BLOCK_MS)
        period = BLOCK_MS / 1000.0
        next_t = time.monotonic()
        stall_since = None
        while not self._stop.is_set():
            backend = self.backend
            queued = backend.queued_ms() if backend.is_alive() else None
            now = time.monotonic()
            if queued is not None:
                # Backend-paced: wait while the pipe still holds enough audio.
                if queued > backend.queue_target_ms:
                    if stall_since is None:
                        stall_since = now
                    if now - stall_since < 0.25:
                        # Sleep until roughly enough audio has been consumed.
                        time.sleep(min(0.02, max(0.001, (queued - backend.queue_target_ms) / 1000.0)))
                        continue
                    # The backend stopped consuming (sound server hung). Keep the
                    # client buffers bounded by running on the system clock.
                    self.backend_stalls += 1
                    stall_since = now
                else:
                    stall_since = None
                next_t = now
            else:
                if now < next_t:
                    time.sleep(min(next_t - now, period))
                    continue
                if now - next_t > 0.2:
                    self.late_blocks += 1
                    next_t = now
                next_t += period
            t0 = time.perf_counter()
            try:
                pcm = self.process_block()
                with self._backend_lock:
                    self.backend.write(pcm)
            except Exception:
                log.exception("Audio engine error")
                time.sleep(0.05)
            elapsed = time.perf_counter() - t0
            self.cpu_load = self.cpu_load * 0.99 + (elapsed / period) * 0.01
        log.info("Audio engine stopped")

