import numpy as np
import pytest

from app.audio.backends.null import NullBackend
from app.audio.engine import AudioEngine
from app.config.settings import Settings
from app.devices.registry import DeviceRegistry
from app.server.protocol import AudioConfig, AudioPacket


def sine_payload(freq=440, n=480, amp=0.5, rate=48000, offset=0):
    t = (np.arange(n) + offset) / rate
    return (amp * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2").tobytes()


@pytest.fixture
def env():
    settings = Settings(buffer_ms=10)
    registry = DeviceRegistry(lambda: settings)
    backend = NullBackend("t", "T", keep_last=1000)
    backend.start()
    engine = AudioEngine(registry, backend, lambda: settings)
    return settings, registry, engine, backend


def add_client(registry, cid, connected_at=0.0, amp=0.5):
    s = registry.register(cid, "ip", cid)
    s.connected_at = connected_at
    registry.configure_audio(s, AudioConfig(48000, 1), 48000, 10)
    for i in range(5):
        s.push_packet(AudioPacket(i, 0.0, 0, sine_payload(amp=amp, offset=i * 480)))
    return s


def level(pcm: bytes) -> float:
    a = np.frombuffer(pcm, "<i2").astype(float) / 32768
    return float(np.max(np.abs(a)))


def test_silence_without_clients(env):
    _, _, engine, _ = env
    assert level(engine.process_block()) == 0.0


def test_single_mode_uses_newest_client_by_default(env):
    settings, registry, engine, _ = env
    add_client(registry, "old", connected_at=1)
    add_client(registry, "new", connected_at=2)
    engine.process_block()
    assert engine.route.active == ["new"]
    settings.active_client = "old"
    engine.process_block()
    assert engine.route.active == ["old"]


def test_audio_flows_through_engine(env):
    _, registry, engine, _ = env
    add_client(registry, "a")
    out = b"".join(engine.process_block() for _ in range(4))
    assert 0.45 < level(out) < 0.51


def test_mix_mode_sums_clients_with_gain(env):
    settings, registry, engine, _ = env
    settings.route_mode = "mix"
    add_client(registry, "a", amp=0.2)
    add_client(registry, "b", amp=0.2)
    settings.device("b").gain_db = -120
    engine.process_block()
    out = b"".join(engine.process_block() for _ in range(3))
    assert sorted(engine.route.active) == ["a", "b"]
    assert 0.18 < level(out) < 0.21  # b is effectively silent
    settings.device("b").gain_db = 0
    add_client(registry, "a", amp=0.2)  # fresh, in-phase buffers
    add_client(registry, "b", amp=0.2)
    out = b"".join(engine.process_block() for _ in range(3))
    assert 0.38 < level(out) < 0.41


def test_mute_disable_and_unrouted(env):
    settings, registry, engine, _ = env
    add_client(registry, "a")
    settings.device("a").muted = True
    out = b"".join(engine.process_block() for _ in range(3))
    assert level(out) == 0.0
    settings.device("a").muted = False
    settings.device("a").routed = False
    engine.process_block()
    assert engine.route.active == []
    settings.device("a").routed = True
    settings.device("a").disabled = True
    engine.process_block()
    assert engine.route.active == []


def test_master_mute(env):
    settings, registry, engine, _ = env
    settings.master_muted = True
    add_client(registry, "a")
    assert level(b"".join(engine.process_block() for _ in range(3))) == 0.0


def test_disconnected_client_buffer_is_flushed(env):
    _, registry, engine, _ = env
    s = add_client(registry, "a")
    engine.process_block()
    registry.unregister(s)
    engine.process_block()
    assert s.jitter.fill == 0


def test_engine_thread_writes_to_backend(env):
    import time
    _, registry, engine, backend = env
    add_client(registry, "a")
    engine.start()
    time.sleep(0.25)
    engine.stop()
    # ~10 ms blocks on the monotonic clock
    assert 15 <= engine.blocks_produced <= 40
    assert backend.bytes_written == engine.blocks_produced * 960
