"""WebSocket protocol tests against the real aiohttp application (in-process)."""

import asyncio
import json

import numpy as np
import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from app.audio.backends.null import NullBackend
from app.audio.engine import AudioEngine
from app.config.settings import Settings
from app.devices import registry as reg
from app.devices.registry import DeviceRegistry
from app.server.auth import Authenticator
from app.server.protocol import pack_audio
from app.server.web_server import ServerContext, WebServer

CODE = "246810"


class Env:
    def __init__(self, auth_enabled=True):
        self.settings = Settings(buffer_ms=10, auth_enabled=auth_enabled)
        self.registry = DeviceRegistry(lambda: self.settings)
        self.auth = Authenticator(CODE, auth_enabled)
        self.backend = NullBackend("t", "Phone Microphone", keep_last=500)
        self.backend.start()
        self.engine = AudioEngine(self.registry, self.backend, lambda: self.settings)
        self.server = WebServer(ServerContext(registry=self.registry, auth=self.auth, settings=lambda: self.settings,
                                              route_info=lambda cid: {"routed": True, "target": "Phone Microphone"}))


@pytest.fixture
async def env():
    e = Env()
    client = TestClient(TestServer(e.server.make_app()))
    await client.start_server()
    e.client = client
    yield e
    await client.close()


def hello(**kw):
    msg = {"type": "hello", "protocol": 1, "client_id": "dev1", "device_name": "Test Phone",
           "platform": "Android", "browser": "Chrome", "pairing_code": CODE}
    msg.update(kw)
    return json.dumps(msg)


async def handshake(ws, **kw):
    await ws.send_str(hello(**kw))
    msg = json.loads((await ws.receive(timeout=3)).data)
    return msg


def sine(n=480, offset=0, amp=0.5):
    t = (np.arange(n) + offset) / 48000
    return (amp * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2").tobytes()


async def test_index_and_static_files(env):
    r = await env.client.get("/")
    assert r.status == 200 and "Start Microphone" in await r.text()
    assert "Content-Security-Policy" in r.headers
    for name in ("app.js", "worklet.js", "style.css"):
        assert (await env.client.get(f"/{name}")).status == 200
    assert (await env.client.get("/../config.json")).status == 404
    info = await (await env.client.get("/api/info")).json()
    assert info["auth_required"] is True and info["sample_rate"] == 48000


async def test_registration_and_audio_pipeline(env):
    ws = await env.client.ws_connect("/ws")
    welcome = await handshake(ws)
    assert welcome["type"] == "welcome" and welcome["token"] and welcome["name"] == "Test Phone"
    sess = env.registry.get("dev1")
    assert sess.state == reg.WAITING and sess.connection == "USB (ADB)"  # loopback + Android
    await ws.send_str(json.dumps({"type": "audio_config", "sample_rate": 48000, "channels": 1, "format": "s16le"}))
    ack = json.loads((await ws.receive(timeout=3)).data)
    assert ack["type"] == "audio_config_ack" and ack["resampling"] is False
    for i in range(10):
        await ws.send_bytes(pack_audio(i, 0.0, sine(offset=i * 480)))
    for _ in range(50):
        if sess.packets == 10:
            break
        await asyncio.sleep(0.01)
    assert sess.packets == 10 and sess.state == reg.STREAMING
    out = b"".join(env.engine.process_block() for _ in range(8))
    peak = np.max(np.abs(np.frombuffer(out, "<i2"))) / 32768
    assert 0.45 < peak < 0.51
    await ws.close()
    for _ in range(50):
        if sess.state == reg.DISCONNECTED:
            break
        await asyncio.sleep(0.01)
    assert sess.state == reg.DISCONNECTED


async def test_wrong_pairing_code_is_rejected(env):
    ws = await env.client.ws_connect("/ws")
    msg = await handshake(ws, pairing_code="000000")
    assert msg["type"] == "error" and msg["code"] == "auth_failed"
    closing = await ws.receive(timeout=3)
    assert closing.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
    assert ws.close_code == 4001
    assert env.registry.get("dev1") is None


async def test_reconnect_with_token_and_no_code(env):
    ws = await env.client.ws_connect("/ws")
    token = (await handshake(ws))["token"]
    await ws.close()
    await asyncio.sleep(0.05)
    ws2 = await env.client.ws_connect("/ws")
    msg = await handshake(ws2, pairing_code="", token=token)
    assert msg["type"] == "welcome"
    assert env.registry.get("dev1").reconnects == 1
    await ws2.close()


async def test_second_connection_replaces_first(env):
    ws1 = await env.client.ws_connect("/ws")
    await handshake(ws1)
    ws2 = await env.client.ws_connect("/ws")
    await handshake(ws2)
    kicked = False
    for _ in range(5):
        m = await ws1.receive(timeout=3)
        if m.type == WSMsgType.TEXT and json.loads(m.data).get("reason") == "replaced":
            kicked = True
        if m.type in (WSMsgType.CLOSE, WSMsgType.CLOSED):
            break
    assert kicked
    await asyncio.sleep(0.05)
    assert env.registry.get("dev1").connected
    await ws2.close()


async def test_malformed_packets_are_counted_and_limited(env):
    ws = await env.client.ws_connect("/ws")
    await handshake(ws)
    await ws.send_bytes(b"\x00\x01")  # before audio_config
    await ws.send_str(json.dumps({"type": "audio_config", "sample_rate": 48000}))
    await ws.receive(timeout=3)
    await ws.send_bytes(b"garbage-garbage-garbage")
    await ws.send_str("not json")
    await asyncio.sleep(0.1)
    sess = env.registry.get("dev1")
    assert sess.malformed_packets == 2 and sess.connected
    for _ in range(60):
        await ws.send_bytes(b"XX" + bytes(20))
    msg = None
    for _ in range(10):
        m = await ws.receive(timeout=3)
        if m.type == WSMsgType.TEXT:
            msg = json.loads(m.data)
        if m.type in (WSMsgType.CLOSE, WSMsgType.CLOSED):
            break
    assert msg and msg["code"] == "malformed"


async def test_first_message_must_be_hello(env):
    ws = await env.client.ws_connect("/ws")
    await ws.send_bytes(pack_audio(0, 0, sine()))
    msg = json.loads((await ws.receive(timeout=3)).data)
    assert msg["code"] == "expected_hello"


async def test_unsupported_audio_config(env):
    ws = await env.client.ws_connect("/ws")
    await handshake(ws)
    await ws.send_str(json.dumps({"type": "audio_config", "sample_rate": 12345}))
    msg = json.loads((await ws.receive(timeout=3)).data)
    assert msg["type"] == "error" and msg["code"] == "unsupported_format"


async def test_ping_pong_measures_rtt(env, monkeypatch):
    import app.server.web_server as ws_mod
    monkeypatch.setattr(ws_mod, "PING_INTERVAL", 0.05)
    ws = await env.client.ws_connect("/ws")
    await handshake(ws)
    for _ in range(20):
        m = json.loads((await ws.receive(timeout=3)).data)
        if m["type"] == "ping":
            assert m["routed"] is True and m["target"] == "Phone Microphone"
            await ws.send_str(json.dumps({"type": "pong", "t": m["t"], "buffer_ms": 12}))
            break
    await asyncio.sleep(0.05)
    sess = env.registry.get("dev1")
    assert sess.rtt_ms is not None and 0 <= sess.rtt_ms < 1000
    assert sess.client_buffer_ms == 12


async def test_server_side_disconnect(env):
    ws = await env.client.ws_connect("/ws")
    await handshake(ws)
    env.registry.disconnect("dev1")
    got_kick = False
    for _ in range(5):
        m = await ws.receive(timeout=3)
        if m.type == WSMsgType.TEXT and json.loads(m.data)["type"] == "kick":
            got_kick = True
        if m.type in (WSMsgType.CLOSE, WSMsgType.CLOSED):
            break
    assert got_kick


async def test_stop_and_start_messages(env):
    ws = await env.client.ws_connect("/ws")
    await handshake(ws)
    await ws.send_str(json.dumps({"type": "stop"}))
    await asyncio.sleep(0.05)
    assert env.registry.get("dev1").state == reg.PAUSED
    await ws.close()


async def test_auth_disabled():
    e = Env(auth_enabled=False)
    async with TestClient(TestServer(e.server.make_app())) as client:
        ws = await client.ws_connect("/ws")
        msg = await handshake(ws, pairing_code="")
        assert msg["type"] == "welcome"
