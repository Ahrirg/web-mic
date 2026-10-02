"""Mix several mono float32 blocks with independent gains and a soft limiter."""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np

KNEE = 0.89  # about -1 dBFS


def soft_clip(x: np.ndarray, knee: float = KNEE) -> np.ndarray:
    """Leaves samples below the knee untouched and smoothly limits the rest to < 1.0."""
    over = np.abs(x) > knee
    if not over.any():
        return x
    y = x.copy()
    s = np.sign(x[over])
    excess = np.abs(x[over]) - knee
    headroom = 1.0 - knee
    y[over] = s * (knee + headroom * np.tanh(excess / headroom))
    return y


def mix(blocks: Iterable[tuple[np.ndarray, float]], frames: int, master_gain: float = 1.0) -> np.ndarray:
    out = np.zeros(frames, dtype=np.float32)
    for block, gain in blocks:
        if gain == 0.0 or block.size == 0:
            continue
        n = min(frames, block.size)
        out[:n] += block[:n] * np.float32(gain)
    if master_gain != 1.0:
        out *= np.float32(master_gain)
    return soft_clip(out)


def float_to_s16(x: np.ndarray) -> bytes:
    return np.clip(np.round(x * 32768.0), -32768, 32767).astype("<i2").tobytes()


def s16_to_float(data: bytes, channels: int = 1) -> np.ndarray:
    samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    if channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)
    return samples
