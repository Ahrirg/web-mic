import time

import pytest

from app.config.settings import Settings
from app.devices import registry as reg
from app.devices.registry import DeviceRegistry, estimate_latency_ms
from app.server.protocol import AudioConfig, AudioPacket


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def registry(settings):
    return DeviceRegistry(lambda: settings)


def pkt(seq, n=480):
    return AudioPacket(seq=seq, timestamp_ms=0.0, flags=0, payload=bytes(n * 2))


def test_register_and_display_name(registry, settings):
    s = registry.register("c1", "192.168.1.5", "Pixel 8 (Chrome)", connection="Wi-Fi/LAN")
    assert s.state == reg.WAITING and s.number == 1
    assert registry.display_name(s) == "Pixel 8 (Chrome)"
    settings.device("c1").alias = "Desk phone"
    assert registry.display_name(s) == "Desk phone"
    assert registry.connected_count() == 1


def test_anonymous_client_gets_id(registry):
    s = registry.register("", "1.1.1.1", "x")
    assert s.client_id.startswith("anon-")


def test_reconnect_reuses_number_and_closes_old(registry):
    closed = []
    s1 = registry.register("c1", "ip", "A")
    s1.closer = closed.append
    s2 = registry.register("c1", "ip", "A")
    assert closed == ["replaced"]
    assert s2.number == s1.number and s2.reconnects == 1
    # The old connection's late unregister must not mark the new session disconnected
    registry.unregister(s1, "old socket closed")
    assert registry.get("c1") is s2 and s2.connected


def test_disconnect_and_reconnect_cycle(registry):
    s = registry.register("c1", "ip", "A")
    registry.configure_audio(s, AudioConfig(48000, 1), 48000, 40)
    s.push_packet(pkt(0))
    registry.unregister(s, "network lost")
    assert s.state == reg.DISCONNECTED and not s.queue and s.pending_flush
    assert registry.connected_count() == 0
    s2 = registry.register("c1", "ip", "A")
    assert s2.connected and s2.reconnects == 1


def test_packet_accounting(registry):
    s = registry.register("c1", "ip", "A")
    registry.configure_audio(s, AudioConfig(48000, 1), 48000, 40)
    s.push_packet(pkt(0))
    assert s.state == reg.STREAMING
    s.push_packet(pkt(1))
    s.push_packet(pkt(5))  # 3 lost
    s.push_packet(pkt(4))  # late / reordered -> ignored
    assert s.lost_packets == 3 and s.reordered_packets == 1 and s.packets == 3


def test_packet_queue_is_bounded(registry):
    s = registry.register("c1", "ip", "A")
    registry.configure_audio(s, AudioConfig(48000, 1), 48000, 40)
    for i in range(reg.PACKET_QUEUE_MAX * 3):
        s.push_packet(pkt(i))
    assert len(s.queue) == reg.PACKET_QUEUE_MAX
    assert s.queue_overflows > 0


def test_stall_detection(registry, monkeypatch):
    s = registry.register("c1", "ip", "A")
    registry.configure_audio(s, AudioConfig(48000, 1), 48000, 40)
    s.push_packet(pkt(0))
    s.last_packet_monotonic = time.monotonic() - reg.STALL_SECONDS - 1
    events = registry.check_health()
    assert (s, "stalled") in events and s.state == reg.STALLED
    s.push_packet(pkt(1))
    assert s.state == reg.STREAMING


def test_high_latency_detection(registry):
    s = registry.register("c1", "ip", "A")
    registry.configure_audio(s, AudioConfig(48000, 1), 48000, 40)
    s.push_packet(pkt(0))
    s.rtt_ms = 1000
    assert (s, "high_latency") in registry.check_health()
    assert "high_latency" in s.warnings


def test_disconnect_and_remove(registry):
    reasons = []
    s = registry.register("c1", "ip", "A")
    s.closer = reasons.append
    assert registry.disconnect("c1")
    assert reasons == ["disconnected by user"]
    registry.remove("c1")
    assert registry.get("c1") is None


def test_prune_old_disconnected(registry):
    s = registry.register("c1", "ip", "A")
    registry.unregister(s)
    s.disconnected_at = time.time() - reg.DISCONNECTED_KEEP_SECONDS - 1
    registry.prune()
    assert registry.get("c1") is None


def test_latency_estimate(registry):
    s = registry.register("c1", "ip", "A")
    assert estimate_latency_ms(s) is None
    registry.configure_audio(s, AudioConfig(48000, 1, frame_ms=10), 48000, 40)
    s.rtt_ms = 20
    lat = estimate_latency_ms(s, backend_ms=15)
    assert lat == pytest.approx(10 + 10 + 15)
