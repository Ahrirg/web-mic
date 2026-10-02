import numpy as np

from app.audio.resampler import StreamingResampler


def tone(freq, rate, seconds, amp=0.5):
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def dominant(x, rate):
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    return np.argmax(spec) * rate / x.size


def test_same_rate_is_bit_exact_passthrough():
    r = StreamingResampler(48000, 48000)
    x = (np.random.default_rng(1).integers(-32768, 32767, 4800) / 32768).astype(np.float32)
    out = np.concatenate([r.process(x[i:i + 480]) for i in range(0, x.size, 480)])
    # no transcoding: the output is exactly the input (2 samples held back for interpolation)
    assert np.array_equal(out, x[: out.size])
    assert out.size >= x.size - 2


def test_upsample_44100_to_48000():
    r = StreamingResampler(44100, 48000)
    x = tone(1000, 44100, 1.0)
    chunks = [r.process(x[i:i + 441]) for i in range(0, x.size, 441)]
    out = np.concatenate(chunks)
    assert abs(out.size - 48000) <= 3
    assert abs(dominant(out[1000:], 48000) - 1000) < 3
    # amplitude preserved
    assert abs(np.max(np.abs(out[1000:])) - 0.5) < 0.01


def test_streaming_equals_one_shot():
    x = tone(700, 32000, 0.3)
    a = StreamingResampler(32000, 48000)
    one = a.process(x)
    b = StreamingResampler(32000, 48000)
    rng = np.random.default_rng(0)
    parts, i = [], 0
    while i < x.size:
        n = int(rng.integers(1, 700))
        parts.append(b.process(x[i:i + n]))
        i += n
    streamed = np.concatenate(parts)
    n = min(one.size, streamed.size)
    assert abs(one.size - streamed.size) <= 1
    assert np.allclose(one[:n], streamed[:n], atol=1e-6)


def test_downsample_removes_content_above_nyquist():
    r = StreamingResampler(96000, 48000)
    x = tone(30000, 96000, 0.5) + tone(1000, 96000, 0.5)
    out = r.process(x)
    spec = np.abs(np.fft.rfft(out[500:] * np.hanning(out.size - 500)))
    freqs = np.fft.rfftfreq(out.size - 500, 1 / 48000)
    alias = spec[np.argmin(np.abs(freqs - 18000))]  # 30 kHz would alias to 18 kHz
    wanted = spec[np.argmin(np.abs(freqs - 1000))]
    assert alias < wanted * 0.01


def test_drift_adjust_changes_output_length():
    x = tone(440, 48000, 1.0)
    fast = StreamingResampler(48000, 48000).process(x, adjust=0.002)
    slow = StreamingResampler(48000, 48000).process(x, adjust=-0.002)
    assert fast.size < x.size < slow.size
    assert abs(fast.size - x.size / 1.002) < 3


def test_switching_between_fast_path_and_drift_correction_is_continuous():
    r = StreamingResampler(48000, 48000)
    x = tone(440, 48000, 1.0)
    parts = []
    for i, start in enumerate(range(0, x.size, 480)):
        adjust = 0.0 if (i // 5) % 2 == 0 else 0.003
        parts.append(r.process(x[start:start + 480], adjust))
    out = np.concatenate(parts)
    bound = 0.5 * (2 * np.pi * 440 / 48000) ** 2
    assert np.max(np.abs(np.diff(out, 2))) < bound * 1.5  # no clicks at the switches
    assert x.size * 0.99 < out.size <= x.size
