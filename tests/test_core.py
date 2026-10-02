import asyncio
import json
import time

import numpy as np
from aiohttp import ClientSession

from app.audio.backends.null import NullBackend
from app.config.settings import ConfigStore
from app.core import AppCore
from app.server.protocol import pack_audio
from tests.conftest import free_port


def make_core(tmp_path, **kw):
    store = ConfigStore(tmp_path / "config.json")
    store.settings.port = free_port()
    store.settings.https_port = free_port()
    store.settings.host = "127.0.0.1"
    for k, v in kw.items():
        setattr(store.settings, k, v)
    backend = NullBackend(store.settings.source_name, store.settings.source_description, keep_last=2000)
    return AppCore(store, backend=backend, enable_adb=False)


async def stream(port, code, seconds=0.5, client_id="phone-1"):
    async with ClientSession() as http:
        async with http.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
            await ws.send_str(json.dumps({"type": "hello", "client_id": client_id, "device_name": "Core Test",
                                          "pairing_code": code}))
            welcome = json.loads((await ws.receive(timeout=3)).data)
            assert welcome["type"] == "welcome", welcome
            await ws.send_str(json.dumps({"type": "audio_config", "sample_rate": 48000, "channels": 1}))
            await ws.receive(timeout=3)
            t = np.arange(480) / 48000
            pcm = (0.25 * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2").tobytes()
            for i in range(int(seconds * 100)):
                await ws.send_bytes(pack_audio(i, 0, pcm))
                await asyncio.sleep(0.01)


def test_full_core_lifecycle(tmp_path):
    core = make_core(tmp_path)
    core.start()
    try:
        snap = core.snapshot()
        assert snap["started"] and snap["server"]["running"] and snap["audio"]["engine_running"]
        assert snap["tls"]["enabled"] and snap["server"]["https_port"]
        assert snap["urls"][0]["url"].startswith("http://127.0.0.1:")
        asyncio.run(stream(core.server.http_port, core.auth.code))
        rows = core.device_rows()
        assert rows[0]["name"] == "Core Test" and rows[0]["sample_rate"] == 48000
        assert core.backend.bytes_written > 0
        # renaming persists
        core.rename_device("phone-1", "Studio phone")
        assert ConfigStore(core.store.path).settings.devices["phone-1"].alias == "Studio phone"
        diag = core.diagnostics_text()
        assert "Phone Mic Router" in diag and "Studio phone" in diag
        assert core.auth.code not in diag  # never leak the pairing code into copied diagnostics
        time.sleep(0.1)
        assert core.snapshot()["devices"][0]["connected"] is False
    finally:
        core.stop()
    assert not core.server.running and not core.engine.running


def test_port_in_use_is_reported(tmp_path):
    import socket
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    core = make_core(tmp_path, port=blocker.getsockname()[1])
    core.start()
    try:
        assert "already in use" in core.server_error
        assert not core.server.running
    finally:
        core.stop()
        blocker.close()


def test_regenerate_code_and_controls(tmp_path):
    core = make_core(tmp_path)
    old = core.auth.code
    new = core.regenerate_pairing_code()
    assert core.store.settings.pairing_code == new
    assert new == core.auth.code and (new != old or len(new) == 6)
    core.set_buffer_ms(20)
    assert core.store.settings.buffer_ms == 20
    core.set_device("x", gain_db=3.0)
    core.save_if_dirty()
    assert ConfigStore(core.store.path).settings.devices["x"].gain_db == 3.0
    core.set_active_client("x")
    assert core.store.settings.active_client == "x"
    core.forget_device("x")
    assert core.store.settings.active_client == "" and "x" not in core.store.settings.devices
