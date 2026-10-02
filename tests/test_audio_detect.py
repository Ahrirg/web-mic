import subprocess

from app.audio.backends import create_backend, detect
from app.audio.backends.detect import AudioSystemInfo, consumers_of, find_nodes, parse_pactl_info, parse_pactl_short
from app.audio.backends.null import NullBackend
from app.audio.backends.pipewire import PipeWireBackend
from app.audio.backends.pulse import find_our_modules, parse_modules

PACTL_INFO = """Server String: /run/user/1000/pulse/native
Server Name: PulseAudio (on PipeWire 1.6.9)
Server Version: 15.0.0
Default Sink: alsa_output.pci.analog-stereo
Default Source: alsa_input.usb-mic
"""

DUMP = [
    {"id": 50, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "phone-mic", "media.class": "Audio/Source", "object.serial": 900}}},
    {"id": 51, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "phone-mic-input", "media.class": "Audio/Sink"}}},
    {"id": 60, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "discord-in", "application.name": "Discord", "media.name": "RecordStream"}}},
    {"id": 61, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "obs", "application.name": "OBS Studio"}}},
    {"id": 70, "type": "PipeWire:Interface:Link", "info": {"output-node-id": 50, "input-node-id": 60}},
    {"id": 71, "type": "PipeWire:Interface:Link", "info": {"output-node-id": 50, "input-node-id": 61}},
    {"id": 72, "type": "PipeWire:Interface:Link", "info": {"output-node-id": 99, "input-node-id": 61}},
    {"id": 1, "type": "PipeWire:Interface:Core", "info": None},
]


def cp(out="", rc=0):
    return subprocess.CompletedProcess([], rc, out, "")


def test_parse_pactl_info():
    info = parse_pactl_info(PACTL_INFO)
    assert info["Server Name"] == "PulseAudio (on PipeWire 1.6.9)"
    assert info["Default Source"] == "alsa_input.usb-mic"


def test_detect_pipewire(monkeypatch):
    monkeypatch.setattr(detect.shutil, "which", lambda t: f"/usr/bin/{t}")

    def fake_run(cmd, timeout=4.0):
        if cmd[:2] == ["pactl", "info"]:
            return cp(PACTL_INFO)
        if cmd[0] == "pw-cli":
            return cp('\tid: 0\n\tversion: "1.6.9"\n')
        return cp("", 1)

    monkeypatch.setattr(detect, "run", fake_run)
    info = detect.detect_audio_system()
    assert info.pipewire_running and info.pulse_available and info.pipewire_version == "1.6.9"
    assert info.recommended_backend() == "pipewire"


def test_detect_plain_pulseaudio(monkeypatch):
    monkeypatch.setattr(detect.shutil, "which", lambda t: f"/usr/bin/{t}" if t.startswith("pa") else None)
    monkeypatch.setattr(detect, "run", lambda cmd, timeout=4.0: cp("Server Name: pulseaudio\n"))
    info = detect.detect_audio_system()
    assert not info.pipewire_running and info.pulse_available
    assert info.recommended_backend() == "pulse"


def test_detect_nothing(monkeypatch):
    monkeypatch.setattr(detect.shutil, "which", lambda t: None)
    info = detect.detect_audio_system()
    assert info.recommended_backend() == "null"
    assert any("PipeWire not detected" in e for e in info.errors)


def test_find_nodes_and_consumers():
    assert find_nodes(DUMP, "phone-mic")[0]["id"] == 50
    assert find_nodes(DUMP, "nope") == []
    assert consumers_of(DUMP, 50) == ["Discord (RecordStream)", "OBS Studio"]


def test_parse_pactl_short():
    devs = parse_pactl_short("12\tphone-mic\tPipeWire\ts16le 1ch 48000Hz\tIDLE\n13\tx.monitor\tPipeWire\tf32\tRUNNING\n", "source")
    assert devs[0].name == "phone-mic" and devs[0].index == 12 and devs[0].state == "IDLE"
    assert devs[1].is_monitor


def test_pulse_stale_module_detection():
    text = ("1\tmodule-null-sink\tsink_name=foo\n"
            "22\tmodule-pipe-source\tsource_name=phone-mic file=/run/user/1000/phone-mic-router/phone-mic.fifo format=s16le\n"
            "23\tmodule-pipe-source\tsource_name=phone-mic-other file=/x\n")
    assert [m[0] for m in parse_modules(text)] == [1, 22, 23]
    assert find_our_modules(text, "phone-mic") == [22]


def test_pipewire_commands_define_a_source():
    b = PipeWireBackend("phone-mic", 'Phone "Mic"')
    lb = " ".join(b.loopback_cmd())
    assert "media.class=Audio/Source node.name=phone-mic " in lb
    assert "media.class=Audio/Sink node.name=phone-mic-input" in lb
    assert '"Phone \'Mic\'"' in lb  # quotes sanitized
    feeder = b.feeder_cmd()
    assert feeder[:3] == ["pw-cat", "--playback", "--raw"]
    assert "--target" in feeder and "phone-mic-input" in feeder
    props = feeder[feeder.index("-P") + 1]
    assert "node.dont-fallback=true" in props  # never falls back to the speakers


def test_create_backend_selection():
    info = AudioSystemInfo(tools={"pw-cat": "x", "pw-loopback": "x", "pactl": "x"}, pipewire_running=True, pulse_available=True)
    assert create_backend("auto", info, "n", "d").name == "pipewire"
    assert create_backend("pulse", info, "n", "d").name == "pulse"
    assert isinstance(create_backend("null", info, "n", "d"), NullBackend)
    assert create_backend("auto", None, "n", "d").name == "null"
