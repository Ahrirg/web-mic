"""Wire protocol between the browser client and the server.

Text frames carry JSON control messages. Binary frames carry audio:

    offset size type   field
    0      2    bytes  magic  b"PM"
    2      1    u8     version (1)
    3      1    u8     flags   (bit0 = client-side muted, bit1 = resampled by client)
    4      4    u32    sequence number, increments by one per frame, wraps at 2**32
    8      8    f64    client timestamp in milliseconds (performance.timeOrigin + performance.now())
    16     N    s16le  interleaved PCM samples, N must be a multiple of 2 * channels

All header fields are little-endian.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

MAGIC = b"PM"
VERSION = 1
HEADER = struct.Struct("<2sBBId")
HEADER_SIZE = HEADER.size  # 16
MAX_PAYLOAD_BYTES = 48000 * 2 * 2  # one second of 48 kHz stereo s16, far above normal 10-40 ms frames
MAX_TEXT_BYTES = 8192

FLAG_MUTED = 0x01
FLAG_RESAMPLED = 0x02

SUPPORTED_RATES = (8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000)
SUPPORTED_FORMATS = ("s16le",)


class ProtocolError(ValueError):
    """Raised when a client sends something that violates the protocol."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AudioPacket:
    seq: int
    timestamp_ms: float
    flags: int
    payload: bytes

    @property
    def muted(self) -> bool:
        return bool(self.flags & FLAG_MUTED)


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int
    channels: int
    format: str = "s16le"
    frame_ms: float = 10.0


def pack_audio(seq: int, timestamp_ms: float, payload: bytes, flags: int = 0) -> bytes:
    return HEADER.pack(MAGIC, VERSION, flags & 0xFF, seq & 0xFFFFFFFF, float(timestamp_ms)) + payload


def parse_audio(data: bytes, channels: int = 1) -> AudioPacket:
    if len(data) < HEADER_SIZE:
        raise ProtocolError("malformed", f"binary frame too short ({len(data)} bytes)")
    magic, version, flags, seq, ts = HEADER.unpack_from(data)
    if magic != MAGIC:
        raise ProtocolError("malformed", "bad magic in audio frame")
    if version != VERSION:
        raise ProtocolError("unsupported_version", f"unsupported audio frame version {version}")
    payload = data[HEADER_SIZE:]
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ProtocolError("malformed", f"audio payload too large ({len(payload)} bytes)")
    if len(payload) % (2 * channels):
        raise ProtocolError("malformed", "audio payload is not a whole number of samples")
    if ts != ts:  # NaN
        ts = 0.0
    return AudioPacket(seq=seq, timestamp_ms=ts, flags=flags, payload=bytes(payload))


def seq_gap(prev: int | None, current: int) -> int:
    """Number of frames missing between prev and current, handling wraparound.

    Returns 0 for the next expected frame, a positive count for lost frames and
    -1 for a duplicate or reordered (older) frame.
    """
    if prev is None:
        return 0
    delta = (current - prev) & 0xFFFFFFFF
    if delta == 0 or delta > 0x7FFFFFFF:
        return -1
    return delta - 1


def parse_text(text: str) -> dict[str, Any]:
    if len(text) > MAX_TEXT_BYTES:
        raise ProtocolError("malformed", "control message too large")
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("malformed", f"invalid JSON: {exc.msg}") from None
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("malformed", "control message must be an object with a 'type'")
    return msg


def _clean_str(value: Any, limit: int = 80) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isprintable())[:limit].strip()


@dataclass(frozen=True)
class Hello:
    client_id: str
    device_name: str
    browser: str
    platform: str
    user_agent: str
    pairing_code: str
    token: str


def validate_hello(msg: dict[str, Any]) -> Hello:
    if msg.get("type") != "hello":
        raise ProtocolError("expected_hello", "first message must be 'hello'")
    if msg.get("protocol", 1) != 1:
        raise ProtocolError("unsupported_version", "unsupported protocol version")
    client_id = _clean_str(msg.get("client_id"), 64)
    if client_id and not all(c.isalnum() or c in "-_" for c in client_id):
        raise ProtocolError("malformed", "invalid client_id")
    code = msg.get("pairing_code", "")
    code = "".join(c for c in str(code) if c.isdigit())[:12]
    return Hello(
        client_id=client_id,
        device_name=_clean_str(msg.get("device_name")),
        browser=_clean_str(msg.get("browser")),
        platform=_clean_str(msg.get("platform")),
        user_agent=_clean_str(msg.get("user_agent"), 300),
        pairing_code=code,
        token=_clean_str(msg.get("token"), 128),
    )


def validate_audio_config(msg: dict[str, Any]) -> AudioConfig:
    if msg.get("type") != "audio_config":
        raise ProtocolError("expected_audio_config", "expected 'audio_config'")
    rate = msg.get("sample_rate")
    channels = msg.get("channels", 1)
    fmt = msg.get("format", "s16le")
    if not isinstance(rate, int) or isinstance(rate, bool) or rate not in SUPPORTED_RATES:
        raise ProtocolError("unsupported_format", f"unsupported sample rate {rate!r}")
    if channels not in (1, 2):
        raise ProtocolError("unsupported_format", f"unsupported channel count {channels!r}")
    if fmt not in SUPPORTED_FORMATS:
        raise ProtocolError("unsupported_format", f"unsupported sample format {fmt!r}")
    frame_ms = msg.get("frame_ms", 10.0)
    if not isinstance(frame_ms, (int, float)) or not (1 <= frame_ms <= 200):
        frame_ms = 10.0
    return AudioConfig(sample_rate=rate, channels=channels, format=fmt, frame_ms=float(frame_ms))


def dumps(msg_type: str, **fields: Any) -> str:
    return json.dumps({"type": msg_type, **fields}, separators=(",", ":"))
