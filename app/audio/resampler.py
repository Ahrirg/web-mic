"""Streaming resampler with a tiny, adjustable ratio for clock-drift correction.

Uses 4-point Catmull-Rom (cubic Hermite) interpolation. When the input and
output rates match and no drift correction is active, the output is
bit-identical to the input, so 48 kHz clients are not transcoded.
When downsampling, a windowed-sinc FIR low-pass removes content above the new
Nyquist frequency first.
"""

from __future__ import annotations

import numpy as np


def _lowpass_taps(cutoff: float, ntaps: int = 63) -> np.ndarray:
    """cutoff is a fraction of the input sample rate (0 < cutoff < 0.5)."""
    n = np.arange(ntaps) - (ntaps - 1) / 2
    h = 2 * cutoff * np.sinc(2 * cutoff * n)
    h *= np.blackman(ntaps)
    return (h / h.sum()).astype(np.float32)


class StreamingFIR:
    def __init__(self, taps: np.ndarray):
        self.taps = taps
        self._tail = np.zeros(len(taps) - 1, dtype=np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        buf = np.concatenate((self._tail, x))
        y = np.convolve(buf, self.taps, mode="valid").astype(np.float32)
        self._tail = buf[-(len(self.taps) - 1) :]
        return y


class StreamingResampler:
    MAX_ADJUST = 0.01

    def __init__(self, in_rate: int, out_rate: int):
        if in_rate <= 0 or out_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self.base_step = self.in_rate / self.out_rate
        self._hist = np.zeros(1, dtype=np.float32)  # one sample of history for the i-1 tap
        self._pos = 1.0
        self._fir = None
        if self.in_rate > self.out_rate:
            self._fir = StreamingFIR(_lowpass_taps(0.45 * self.out_rate / self.in_rate))

    @property
    def is_passthrough(self) -> bool:
        return self.in_rate == self.out_rate

    def process(self, x: np.ndarray, adjust: float = 0.0) -> np.ndarray:
        """Resample a block. adjust > 0 consumes input faster (produces fewer samples)."""
        x = np.asarray(x, dtype=np.float32).ravel()
        if self._fir is not None:
            x = self._fir.process(x)
        adjust = max(-self.MAX_ADJUST, min(self.MAX_ADJUST, adjust))
        step = self.base_step * (1.0 + adjust)
        buf = np.concatenate((self._hist, x)) if x.size else self._hist
        if step == 1.0 and self._pos.is_integer():
            # Fast exact path (same rate, no drift correction): plain copy.
            start = int(self._pos)
            end = len(buf) - 2
            if end <= start:
                self._hist = buf
                return np.zeros(0, dtype=np.float32)
            out = buf[start:end].copy()
            keep_from = end - 1
            self._hist = buf[keep_from:].copy()
            self._pos = float(end - keep_from)
            return out
        last_needed = len(buf) - 3  # floor(pos) + 2 must be a valid index
        if last_needed < self._pos:
            self._hist = buf
            return np.zeros(0, dtype=np.float32)
        count = int(np.floor((last_needed - self._pos) / step)) + 1
        positions = self._pos + step * np.arange(count, dtype=np.float64)
        idx = np.floor(positions).astype(np.int64)
        frac = (positions - idx).astype(np.float32)
        p0 = buf[idx - 1]
        p1 = buf[idx]
        p2 = buf[idx + 1]
        p3 = buf[idx + 2]
        # Catmull-Rom spline. At frac == 0 this returns p1 exactly.
        a = -0.5 * p0 + 1.5 * p1 - 1.5 * p2 + 0.5 * p3
        b = p0 - 2.5 * p1 + 2.0 * p2 - 0.5 * p3
        c = -0.5 * p0 + 0.5 * p2
        y = ((a * frac + b) * frac + c) * frac + p1
        exact = frac == 0.0
        if exact.any():
            y[exact] = p1[exact]
        new_pos = self._pos + step * count
        keep_from = int(np.floor(new_pos)) - 1
        self._hist = buf[keep_from:].copy()
        self._pos = new_pos - keep_from
        return y.astype(np.float32)

    def reset(self) -> None:
        self.__init__(self.in_rate, self.out_rate)
