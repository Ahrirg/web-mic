import json

from app.config.settings import ConfigStore, Settings


def test_defaults(tmp_path):
    store = ConfigStore(tmp_path / "c.json")
    s = store.settings
    assert s.port == 8765 and s.source_name == "phone-mic" and s.source_description == "Phone Microphone"
    assert s.buffer_ms == 40 and s.auth_enabled


def test_roundtrip_including_devices(tmp_path):
    path = tmp_path / "c.json"
    store = ConfigStore(path)
    store.settings.port = 9000
    store.settings.device("abc").alias = "Kitchen phone"
    store.settings.device("abc").gain_db = 6.0
    store.settings.route_mode = "mix"
    store.save()
    again = ConfigStore(path).settings
    assert again.port == 9000 and again.route_mode == "mix"
    assert again.devices["abc"].alias == "Kitchen phone" and again.devices["abc"].gain_db == 6.0


def test_invalid_values_are_corrected(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({
        "port": 99999, "buffer_ms": 33, "route_mode": "weird", "source_name": "bad name!/",
        "master_gain_db": 500, "unknown_key": 1, "auth_enabled": "yes",
        "devices": {"x": {"alias": "A", "bogus": 1}, "y": "not a dict"},
    }))
    s = ConfigStore(path).settings
    assert s.port == 8765 and s.buffer_ms == 40 and s.route_mode == "single"
    assert s.source_name == "badname" and s.master_gain_db == 24.0
    assert s.devices["x"].alias == "A" and "y" not in s.devices


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{not json")
    store = ConfigStore(path)
    assert store.settings.port == 8765
    assert (tmp_path / "c.json.bad").exists()


def test_update_saves_atomically(tmp_path):
    path = tmp_path / "sub" / "c.json"
    store = ConfigStore(path)
    store.update(buffer_ms=80, source_description="Desk Mic")
    data = json.loads(path.read_text())
    assert data["buffer_ms"] == 80 and data["source_description"] == "Desk Mic"
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".config-")]


def test_cli_overrides_are_not_persisted(tmp_path):
    path = tmp_path / "c.json"
    store = ConfigStore(path)
    store.apply_overrides(port=9999, auth_enabled=False, host=None)
    assert store.settings.port == 9999 and not store.settings.auth_enabled
    store.save()
    data = json.loads(path.read_text())
    assert data["port"] == 8765 and data["auth_enabled"] is True
    # an explicit change from the GUI wins and is saved
    store.update(port=9100)
    assert json.loads(path.read_text())["port"] == 9100


def test_https_port_conflict():
    s = Settings(port=8000, https_port=8000)
    s.validate()
    assert s.https_port == 8001
