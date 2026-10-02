"""Fixed-capacity float32 ring buffer. Memory use never grows after construction."""

from __future__ import annotations

import threading

import numpy as np


class RingBuffer:
    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = int(capacity)
        self._buf = np.zeros(self.capacity, dtype=np.float32)
        self._read = 0
        self._size = 0
        self._lock = threading.Lock()
        self.overflow_samples = 0

    def __len__(self) -> int:
        return self._size

    @property
    def available(self) -> int:
        return self._size

    @property
    def free(self) -> int:
        return self.capacity - self._size

    def clear(self) -> None:
        with self._lock:
            self._read = 0
            self._size = 0

    def write(self, data: np.ndarray) -> int:
        """Append samples. If they do not fit, the oldest samples are discarded.

        Returns the number of samples that were discarded.
        """
        data = np.asarray(data, dtype=np.float32).ravel()
        n = data.size
        if n == 0:
            return 0
        with self._lock:
            dropped = 0
            if n >= self.capacity:
                dropped = self._size + n - self.capacity
                data = data[-self.capacity :]
                n = data.size
                self._read = 0
                self._size = 0
            elif n > self.capacity - self._size:
                dropped = n - (self.capacity - self._size)
                self._discard_locked(dropped)
            start = (self._read + self._size) % self.capacity
            first = min(n, self.capacity - start)
            self._buf[start : start + first] = data[:first]
            if first < n:
                self._buf[: n - first] = data[first:]
            self._size += n
            self.overflow_samples += dropped
            return dropped

    def read(self, n: int) -> np.ndarray:
        """Remove and return up to n samples (fewer if not enough are buffered)."""
        with self._lock:
            n = min(int(n), self._size)
            out = self._peek_locked(n)
            self._read = (self._read + n) % self.capacity
            self._size -= n
            return out

    def peek(self, n: int) -> np.ndarray:
        with self._lock:
            return self._peek_locked(min(int(n), self._size))

    def discard(self, n: int) -> int:
        with self._lock:
            return self._discard_locked(n)

    def _discard_locked(self, n: int) -> int:
        n = max(0, min(int(n), self._size))
        self._read = (self._read + n) % self.capacity
        self._size -= n
        return n

    def _peek_locked(self, n: int) -> np.ndarray:
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        first = min(n, self.capacity - self._read)
        if first == n:
            return self._buf[self._read : self._read + n].copy()
        return np.concatenate((self._buf[self._read :], self._buf[: n - first]))
