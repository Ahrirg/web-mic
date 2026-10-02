import json
import struct

import pytest

from app.server import protocol as p


def test_pack_parse_roundtrip():
    payload = struct.pack("<4h", 1, -2, 3, -4)
    data = p.pack_audio(42, 1234.5, payload, flags=p.FLAG_MUTED)
    assert len(data) == p.HEADER_SIZE + 8
    pkt = p.parse_audio(data)
    assert pkt.seq == 42 and pkt.timestamp_ms == 1234.5 and pkt.payload == payload and pkt.muted


def test_header_layout_matches_browser():
    # The browser writes: 'P','M', version, flags, u32 seq LE, f64 time LE
    data = p.pack_audio(0x01020304, 0.0, b"")
    assert data[:4] == b"PM\x01\x00"
    assert data[4:8] == b"\x04\x03\x02\x01"


@pytest.mark.parametrize("data,code", [
    (b"PM", "malformed"),
    (b"XX" + bytes(14), "malformed"),
    (p.HEADER.pack(b"PM", 9, 0, 0, 0.0), "unsupported_version"),
    (p.pack_audio(1, 0, b"\x00"), "malformed"),  # odd byte count
    (p.pack_audio(1, 0, bytes(p.MAX_PAYLOAD_BYTES + 2)), "malformed"),
])
def test_malformed_frames(data, code):
    with pytest.raises(p.ProtocolError) as exc:
        p.parse_audio(data)
    assert exc.value.code == code


def test_stereo_payload_must_be_whole_frames():
    with pytest.raises(p.ProtocolError):
        p.parse_audio(p.pack_audio(1, 0, bytes(6)), channels=2)
    assert p.parse_audio(p.pack_audio(1, 0, bytes(8)), channels=2)


def test_seq_gap_and_wraparound():
    assert p.seq_gap(None, 5) == 0
    assert p.seq_gap(5, 6) == 0
    assert p.seq_gap(5, 9) == 3
    assert p.seq_gap(5, 5) == -1
    assert p.seq_gap(5, 3) == -1
    assert p.seq_gap(0xFFFFFFFF, 0) == 0
    assert p.seq_gap(0xFFFFFFFE, 1) == 2


def test_parse_text_rejects_garbage():
    for bad in ["not json", "[]", '{"no_type": 1}', "x" * 9000]:
        with pytest.raises(p.ProtocolError):
            p.parse_text(bad)
    assert p.parse_text('{"type":"pong"}')["type"] == "pong"


def test_validate_hello_sanitizes():
    h = p.validate_hello({
        "type": "hello", "client_id": "abc-123", "device_name": "Pixel\x00 8 " + "x" * 200,
        "pairing_code": "12-34-56", "platform": "Android",
    })
    assert h.client_id == "abc-123"
    assert "\x00" not in h.device_name and len(h.device_name) <= 80
    assert h.pairing_code == "123456"


def test_validate_hello_errors():
    with pytest.raises(p.ProtocolError) as e:
        p.validate_hello({"type": "audio_config"})
    assert e.value.code == "expected_hello"
    with pytest.raises(p.ProtocolError):
        p.validate_hello({"type": "hello", "client_id": "../etc"})
    with pytest.raises(p.ProtocolError):
        p.validate_hello({"type": "hello", "protocol": 2})


def test_audio_config_validation():
    cfg = p.validate_audio_config({"type": "audio_config", "sample_rate": 44100, "channels": 1, "format": "s16le"})
    assert cfg.sample_rate == 44100 and cfg.frame_ms == 10.0
    for bad in [
        {"type": "audio_config", "sample_rate": 12345},
        {"type": "audio_config", "sample_rate": 48000, "channels": 6},
        {"type": "audio_config", "sample_rate": 48000, "format": "opus"},
        {"type": "audio_config", "sample_rate": True},
    ]:
        with pytest.raises(p.ProtocolError):
            p.validate_audio_config(bad)


def test_dumps():
    assert json.loads(p.dumps("ping", t=1)) == {"type": "ping", "t": 1}
