import numpy as np

from app.audio.meter import LevelMeter, db_to_gain, to_dbfs
from app.audio.mixer import float_to_s16, mix, s16_to_float, soft_clip


def test_mix_applies_gains_and_sums():
    a = np.full(10, 0.1, dtype=np.float32)
    b = np.full(10, 0.2, dtype=np.float32)
    out = mix([(a, 1.0), (b, 0.5)], 10)
    assert np.allclose(out, 0.2)


def test_mix_handles_short_blocks_and_silence():
    out = mix([(np.ones(3, dtype=np.float32) * 0.1, 1.0)], 5)
    assert np.allclose(out, [0.1, 0.1, 0.1, 0, 0])
    assert not mix([], 4).any()


def test_soft_clip_bounds_and_transparency():
    x = np.array([0.5, -0.5, 0.88, 2.0, -5.0], dtype=np.float32)
    y = soft_clip(x)
    assert y[0] == 0.5 and y[1] == -0.5 and y[2] == np.float32(0.88)
    assert 0.89 < y[3] <= 1.0 and -1.0 <= y[4] < -0.89
    assert float_to_s16(np.array([y[3]], dtype=np.float32)) == np.array([32767], '<i2').tobytes()


def test_s16_roundtrip_is_exact():
    ints = np.array([-32768, -1, 0, 1, 12345, 32767], dtype="<i2")
    f = s16_to_float(ints.tobytes())
    assert float_to_s16(f) == ints.tobytes()


def test_stereo_downmix():
    stereo = np.array([1000, 3000, -2000, 0], dtype="<i2").tobytes()
    mono = s16_to_float(stereo, channels=2)
    assert np.allclose(mono * 32768, [2000, -1000])


def test_meter_levels_and_clipping():
    m = LevelMeter()
    t = np.arange(4800) / 48000
    m.update((0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), now=0.0)
    assert abs(m.rms_db - to_dbfs(0.5 / np.sqrt(2))) < 0.2
    assert abs(m.peak_db - to_dbfs(0.5)) < 0.1
    assert not m.clipping(now=0.0)
    m.update(np.array([1.0, -1.0], dtype=np.float32), now=1.0)
    assert m.clipping(now=1.5) and not m.clipping(now=10.0)


def test_db_helpers():
    assert to_dbfs(0) == -90.0
    assert abs(db_to_gain(-6.0206) - 0.5) < 1e-4
