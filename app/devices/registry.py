"""Registry of browser microphone clients.

Threading model:
* The asyncio server thread creates sessions and pushes packets.
* The audio engine thread drains packet queues and fills jitter buffers.
* The GUI thread reads snapshots and calls the control methods.
Shared mutable state is guarded by the registry lock. Packet queues are
bounded deques, so a stalled or flooding client cannot grow memory.
"""

from __future__ import annotations

import collections
import itertools
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from app.audio.jitter import JitterBuffer
from app.audio.meter import LevelMeter
from app.server.protocol import AudioConfig, AudioPacket, seq_gap

log = logging.getLogger(__name__)

PACKET_QUEUE_MAX = 64  # about 640 ms of 10 ms frames
STALL_SECONDS = 2.0
HIGH_LATENCY_MS = 400.0
DISCONNECTED_KEEP_SECONDS = 600.0

# states
CONNECTING = "connecting"
WAITING = "waiting"  # paired, waiting for audio_config / first audio
STREAMING = "streaming"
STALLED = "stalled"
PAUSED = "paused"  # client stopped its microphone but stays connected
DISCONNECTED = "disconnected"


@dataclass
class ClientSession:
    client_id: str
    remote_ip: str
    name: str
    browser: str = ""
    platform: str = ""
    user_agent: str = ""
    connection: str = "Wi-Fi"
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    number: int = 0
    state: str = CONNECTING
    connected_at: float = field(default_factory=time.time)
    disconnected_at: float | None = None
    reconnects: int = 0
    audio_config: AudioConfig | None = None
    secure: bool = False

    # stats (written by server thread)
    packets: int = 0
    bytes_received: int = 0
    lost_packets: int = 0
    reordered_packets: int = 0
    malformed_packets: int = 0
    queue_overflows: int = 0
    last_seq: int | None = None
    last_packet_monotonic: float = 0.0
    rtt_ms: float | None = None
    client_buffer_ms: float = 0.0
    client_reported_muted: bool = False
    warnings: list[str] = field(default_factory=list)

    # audio (engine thread)
    queue: collections.deque = field(default_factory=lambda: collections.deque(maxlen=PACKET_QUEUE_MAX))
    jitter: JitterBuffer | None = None
    meter: LevelMeter = field(default_factory=LevelMeter)
    pending_flush: bool = False

    # close callback set by the server: callable(reason: str)
    closer: Callable[[str], None] | None = None

    @property
    def connected(self) -> bool:
        return self.state != DISCONNECTED

    def duration(self, now: float | None = None) -> float:
        end = self.disconnected_at if self.disconnected_at else (now or time.time())
        return max(0.0, end - self.connected_at)

    def push_packet(self, pkt: AudioPacket) -> None:
        gap = seq_gap(self.last_seq, pkt.seq)
        if gap < 0:
            self.reordered_packets += 1
            return
        if gap > 0:
            self.lost_packets += gap
        self.last_seq = pkt.seq
        self.packets += 1
        self.bytes_received += len(pkt.payload)
        self.last_packet_monotonic = time.monotonic()
        self.client_reported_muted = pkt.muted
        if len(self.queue) == self.queue.maxlen:
            self.queue_overflows += 1
        self.queue.append(pkt.payload)  # deque(maxlen) drops the oldest packet
        if self.state in (WAITING, STALLED, PAUSED):
            self.state = STREAMING


class DeviceRegistry:
    def __init__(self, settings_getter: Callable[[], Any], on_change: Callable[[], None] | None = None):
        self._settings = settings_getter
        self._lock = threading.RLock()
        self._sessions: dict[str, ClientSession] = {}
        self._numbers = itertools.count(1)
        self.on_change = on_change or (lambda: None)

    # --- lifecycle -------------------------------------------------------
    def register(self, client_id: str, remote_ip: str, name: str, **info: Any) -> ClientSession:
        """Create a session or revive an existing one for a reconnecting client."""
        if not client_id:
            client_id = "anon-" + uuid.uuid4().hex[:12]
        with self._lock:
            old = self._sessions.get(client_id)
            if old is not None and old.connected and old.closer:
                log.info("Client %s reconnected; closing its previous connection", client_id)
                try:
                    old.closer("replaced")
                except Exception:  # pragma: no cover
                    log.debug("closing old session failed", exc_info=True)
            sess = ClientSession(client_id=client_id, remote_ip=remote_ip, name=name, **info)
            if old is not None:
                sess.number = old.number
                sess.reconnects = old.reconnects + 1
            else:
                sess.number = next(self._numbers)
            sess.state = WAITING
            self._sessions[client_id] = sess
        log.info(
            "Registered device %s (%s) from %s via %s%s",
            self.display_name(sess), client_id, remote_ip, sess.connection,
            f", reconnect #{sess.reconnects}" if sess.reconnects else "",
        )
        self.on_change()
        return sess

    def configure_audio(self, sess: ClientSession, cfg: AudioConfig, out_rate: int, buffer_ms: float) -> None:
        with self._lock:
            sess.audio_config = cfg
            sess.jitter = JitterBuffer(cfg.sample_rate, out_rate, buffer_ms)
            sess.queue.clear()
            sess.last_seq = None
        log.info(
            "Device %s audio: %d Hz, %d ch, %s, %.0f ms frames%s",
            self.display_name(sess), cfg.sample_rate, cfg.channels, cfg.format, cfg.frame_ms,
            "" if cfg.sample_rate == out_rate else f" (resampling to {out_rate} Hz)",
        )
        self.on_change()

    def unregister(self, sess: ClientSession, reason: str = "") -> None:
        with self._lock:
            if sess.state == DISCONNECTED:
                return
            sess.state = DISCONNECTED
            sess.disconnected_at = time.time()
            sess.closer = None
            sess.queue.clear()
            sess.pending_flush = True
            current = self._sessions.get(sess.client_id)
        log.info("Device %s disconnected%s", self.display_name(sess), f" ({reason})" if reason else "")
        if current is not sess:
            return
        self.on_change()

    def remove(self, client_id: str) -> None:
        with self._lock:
            sess = self._sessions.get(client_id)
            if sess is None:
                return
            if sess.connected and sess.closer:
                sess.closer("removed")
            del self._sessions[client_id]
        self.on_change()

    def disconnect(self, client_id: str, reason: str = "disconnected by user") -> bool:
        with self._lock:
            sess = self._sessions.get(client_id)
            closer = sess.closer if sess else None
        if closer:
            closer(reason)
            return True
        return False

    def prune(self) -> None:
        now = time.time()
        changed = False
        with self._lock:
            for cid, sess in list(self._sessions.items()):
                if sess.state == DISCONNECTED and sess.disconnected_at and now - sess.disconnected_at > DISCONNECTED_KEEP_SECONDS:
                    del self._sessions[cid]
                    changed = True
        if changed:
            self.on_change()

    # --- queries ---------------------------------------------------------
    def sessions(self) -> list[ClientSession]:
        with self._lock:
            return sorted(self._sessions.values(), key=lambda s: s.number)

    def get(self, client_id: str) -> ClientSession | None:
        with self._lock:
            return self._sessions.get(client_id)

    def connected_count(self) -> int:
        with self._lock:
            return sum(1 for s in self._sessions.values() if s.connected)

    def display_name(self, sess: ClientSession) -> str:
        settings = self._settings()
        dev = settings.devices.get(sess.client_id) if settings else None
        if dev and dev.alias:
            return dev.alias
        return sess.name or sess.client_id

    def check_health(self) -> list[tuple[ClientSession, str]]:
        """Detect stalled clients and excessive latency. Returns (session, problem) events."""
        events = []
        now = time.monotonic()
        with self._lock:
            for sess in self._sessions.values():
                if sess.state == STREAMING and sess.last_packet_monotonic:
                    if now - sess.last_packet_monotonic > STALL_SECONDS:
                        sess.state = STALLED
                        events.append((sess, "stalled"))
                lat = estimate_latency_ms(sess)
                if sess.state == STREAMING and lat is not None and lat > HIGH_LATENCY_MS:
                    if "high_latency" not in sess.warnings:
                        sess.warnings.append("high_latency")
                        events.append((sess, "high_latency"))
                elif "high_latency" in sess.warnings and lat is not None and lat < HIGH_LATENCY_MS * 0.7:
                    sess.warnings.remove("high_latency")
        for sess, problem in events:
            if problem == "stalled":
                log.warning("Device %s stalled: no audio for %.1f s", self.display_name(sess), STALL_SECONDS)
            else:
                log.warning("Device %s has high latency (~%.0f ms)", self.display_name(sess), estimate_latency_ms(sess) or 0)
        if events:
            self.on_change()
        return events


def estimate_latency_ms(sess: ClientSession, backend_ms: float = 0.0) -> float | None:
    """Approximate mouth-to-virtual-mic latency.

    one-way network (RTT / 2) + browser frame size + browser-reported buffering
    + server packet queue + jitter buffer + audio backend queue.
    Browser and phone timing is not observable precisely, so this is an estimate.
    """
    if sess.rtt_ms is None and sess.jitter is None:
        return None
    total = (sess.rtt_ms or 0.0) / 2.0
    cfg = sess.audio_config
    frame_ms = cfg.frame_ms if cfg else 10.0
    total += frame_ms + sess.client_buffer_ms
    total += len(sess.queue) * frame_ms
    if sess.jitter is not None:
        total += sess.jitter.fill_ms
    return total + backend_ms
