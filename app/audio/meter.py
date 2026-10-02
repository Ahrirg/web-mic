"""RMS / peak level metering with peak hold and clip detection."""

from __future__ import annotations

import math
import time

import numpy as np

SILENCE_DB = -90.0


def to_dbfs(value: float) -> float:
    if value <= 1e-9:
        return SILENCE_DB
    return max(SILENCE_DB, 20.0 * math.log10(value))


def db_to_gain(db: float) -> float:
    return 10.0 ** (db / 20.0)


class LevelMeter:
    PEAK_HOLD_S = 1.5
    CLIP_HOLD_S = 2.0
    CLIP_THRESHOLD = 0.999

    def __init__(self) -> None:
        self.rms_db = SILENCE_DB
        self.peak_db = SILENCE_DB
        self.peak_hold_db = SILENCE_DB
        self._peak_hold_time = 0.0
        self._clip_time = -1e9
        self.clip_count = 0

    def update(self, block: np.ndarray, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if block.size == 0:
            rms = 0.0
            peak = 0.0
        else:
            peak = float(np.max(np.abs(block)))
            rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
        # Smooth the RMS a little so the meter does not flicker.
        new_rms_db = to_dbfs(rms)
        self.rms_db = new_rms_db if new_rms_db > self.rms_db else self.rms_db * 0.7 + new_rms_db * 0.3
        self.peak_db = to_dbfs(peak)
        if self.peak_db >= self.peak_hold_db or now - self._peak_hold_time > self.PEAK_HOLD_S:
            self.peak_hold_db = self.peak_db
            self._peak_hold_time = now
        if peak >= self.CLIP_THRESHOLD:
            self._clip_time = now
            self.clip_count += 1

    def clipping(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return now - self._clip_time < self.CLIP_HOLD_S

    def reset(self) -> None:
        self.__init__()

    def snapshot(self) -> dict:
        return {
            "rms_db": round(self.rms_db, 1),
            "peak_db": round(self.peak_hold_db, 1),
            "clipping": self.clipping(),
        }
