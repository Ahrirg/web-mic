import numpy as np

from app.audio.jitter import JitterBuffer


def frames(n, value=0.5):
    return np.full(n, value, dtype=np.float32)


def test_priming_until_target_reached():
    jb = JitterBuffer(48000, 48000, target_ms=20)
    jb.push(frames(480))
    assert not jb.pull(480).any()  # still priming (10 ms < 20 ms)
    jb.push(frames(960))
    out = jb.pull(480)
    assert out.any()


def test_underrun_fades_instead_of_clicking():
    jb = JitterBuffer(48000, 48000, target_ms=10)
    jb.push(frames(600))
    jb.pull(480)
    out = jb.pull(480)  # only ~120 samples left
    assert jb.underruns == 1
    tail = out[150:300]
    assert np.all(np.diff(np.abs(tail[tail != 0])) <= 1e-6)  # monotonically fading
    assert out[-1] == 0
    assert jb.priming


def test_overflow_trims_to_target():
    jb = JitterBuffer(48000, 48000, target_ms=20)
    jb.push(frames(48000))  # a 1 s burst after a network stall
    assert jb.fill <= jb.target + 2
    assert jb.overflow_drops > 0


def test_drift_correction_direction():
    jb = JitterBuffer(48000, 48000, target_ms=20)
    jb._fill_avg = jb.target * 2
    jb.push(frames(480))
    assert jb.adjust > 0  # buffer too full -> consume faster
    jb._fill_avg = 0
    jb.push(frames(480))
    assert jb.adjust < 0


def test_steady_state_latency_stays_near_target():
    jb = JitterBuffer(44100, 48000, target_ms=40)
    rng = np.random.default_rng(3)
    produced = 0
    for i in range(3000):  # 30 s of 10 ms ticks with bursty arrival
        if rng.random() < 0.8:
            jb.push(frames(441 * int(rng.integers(1, 3)), 0.1))
            produced += 1
        jb.pull(480)
    assert jb.fill_ms < 40 + 60 + 20
