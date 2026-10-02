"""Per-client jitter buffer.

Packets arrive in bursts over Wi-Fi. The jitter buffer holds a small target
amount of audio so the output can be pulled at a steady rate. It also corrects
for the small clock difference between the phone and the PC by nudging the
resampling ratio. When the buffer runs dry it fades out and re-buffers
instead of producing clicks.
"""

from __future__ import annotations

import numpy as np

from app.audio.resampler import StreamingResampler
from app.audio.ringbuffer import RingBuffer

FADE_SAMPLES = 96  # 2 ms at 48 kHz


class JitterBuffer:
    def __init__(self, in_rate: int, out_rate: int, target_ms: float):
        self.out_rate = out_rate
        self.resampler = StreamingResampler(in_rate, out_rate)
        self.ring = RingBuffer(out_rate * 2)  # hard cap: 2 s
        self.set_target_ms(target_ms)
        self.priming = True
        self.underruns = 0
        self.overflow_drops = 0
        self._fill_avg = 0.0
        self._last_sample = 0.0
        self.adjust = 0.0
        self._started = False

    def set_target_ms(self, target_ms: float) -> None:
        self.target = max(1, int(self.out_rate * target_ms / 1000))
        # Allow bursts up to target + 60 ms before discarding old audio.
        self.high_water = self.target * 2 + int(self.out_rate * 0.06)

    @property
    def fill(self) -> int:
        return self.ring.available

    @property
    def fill_ms(self) -> float:
        return self.ring.available * 1000.0 / self.out_rate

    def push(self, samples: np.ndarray) -> None:
        # Drift control: small ratio change proportional to the buffer error.
        error = (self._fill_avg - self.target) / max(self.target, 1)
        if abs(error) < 0.25:
            self.adjust = 0.0
        else:
            self.adjust = float(np.clip(error * 0.002, -0.003, 0.003))
        out = self.resampler.process(samples, self.adjust)
        self.ring.write(out)
        if self.ring.available > self.high_water:
            dropped = self.ring.discard(self.ring.available - self.target)
            self.overflow_drops += dropped

    def pull(self, frames: int) -> np.ndarray:
        """Returns exactly `frames` samples, padding with faded silence on underrun."""
        avail = self.ring.available
        self._fill_avg = self._fill_avg * 0.95 + avail * 0.05
        fade_in = False
        if self.priming:
            if avail < self.target:
                return np.zeros(frames, dtype=np.float32)
            self.priming = False
            fade_in = self._started
            self._started = True
        if avail >= frames:
            out = self.ring.read(frames)
        else:
            got = self.ring.read(avail)
            out = np.zeros(frames, dtype=np.float32)
            out[: got.size] = got
            tail = float(got[-1]) if got.size else self._last_sample
            fade = min(FADE_SAMPLES, frames - got.size)
            out[got.size : got.size + fade] = tail * np.linspace(1.0, 0.0, fade, dtype=np.float32)
            self.underruns += 1
            self.priming = True
        if fade_in:
            n = min(FADE_SAMPLES, frames)
            out[:n] *= np.linspace(0.0, 1.0, n, dtype=np.float32)
        self._last_sample = float(out[-1])
        return out

    def flush(self) -> None:
        self.ring.clear()
        self.priming = True
        self._fill_avg = 0.0
