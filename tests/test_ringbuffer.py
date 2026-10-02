import numpy as np
import pytest

from app.audio.ringbuffer import RingBuffer


def test_write_read_wraparound():
    rb = RingBuffer(8)
    rb.write(np.arange(6, dtype=np.float32))
    assert rb.read(4).tolist() == [0, 1, 2, 3]
    rb.write(np.arange(10, 16, dtype=np.float32))  # wraps
    assert len(rb) == 8
    assert rb.read(8).tolist() == [4, 5, 10, 11, 12, 13, 14, 15]
    assert len(rb) == 0


def test_overflow_drops_oldest_and_stays_bounded():
    rb = RingBuffer(5)
    assert rb.write(np.arange(4, dtype=np.float32)) == 0
    assert rb.write(np.arange(10, 13, dtype=np.float32)) == 2
    assert rb.read(5).tolist() == [2, 3, 10, 11, 12]
    assert rb.write(np.arange(100, dtype=np.float32)) == 95
    assert rb.read(10).tolist() == list(range(95, 100))
    assert rb.overflow_samples == 97


def test_read_more_than_available_and_peek_discard():
    rb = RingBuffer(4)
    rb.write(np.array([1, 2], dtype=np.float32))
    assert rb.peek(5).tolist() == [1, 2]
    assert rb.discard(1) == 1
    assert rb.read(10).tolist() == [2]
    assert rb.read(3).size == 0


def test_invalid_capacity():
    with pytest.raises(ValueError):
        RingBuffer(0)


def test_memory_does_not_grow():
    rb = RingBuffer(1000)
    buf_id = id(rb._buf)
    for _ in range(1000):
        rb.write(np.zeros(480, dtype=np.float32))
        rb.read(100)
    assert id(rb._buf) == buf_id and len(rb) <= 1000
