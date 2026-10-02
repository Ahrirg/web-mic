"""Detect PipeWire / PulseAudio and enumerate sources, sinks and nodes."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

TOOLS = ("pw-cli", "pw-dump", "pw-cat", "pw-loopback", "pactl", "pacat")


def run(cmd: list[str], timeout: float = 4.0) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("Command %s failed: %s", cmd[0], exc)
        return None


@dataclass
class AudioSystemInfo:
    tools: dict[str, str | None] = field(default_factory=dict)
    pipewire_running: bool = False
    pipewire_version: str = ""
    pulse_available: bool = False
    pulse_server_name: str = ""
    default_source: str = ""
    default_sink: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def can_pipewire_backend(self) -> bool:
        return self.pipewire_running and bool(self.tools.get("pw-cat")) and bool(self.tools.get("pw-loopback"))

    @property
    def can_pulse_backend(self) -> bool:
        return self.pulse_available and bool(self.tools.get("pactl"))

    def recommended_backend(self) -> str:
        if self.can_pipewire_backend:
            return "pipewire"
        if self.can_pulse_backend:
            return "pulse"
        return "null"


def parse_pactl_info(text: str) -> dict[str, str]:
    info = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            info[key.strip()] = value.strip()
    return info


def detect_audio_system() -> AudioSystemInfo:
    info = AudioSystemInfo(tools={t: shutil.which(t) for t in TOOLS})
    if info.tools.get("pactl"):
        res = run(["pactl", "info"])
        if res is not None and res.returncode == 0:
            pi = parse_pactl_info(res.stdout)
            info.pulse_available = True
            info.pulse_server_name = pi.get("Server Name", "")
            info.default_source = pi.get("Default Source", "")
            info.default_sink = pi.get("Default Sink", "")
            if "PipeWire" in info.pulse_server_name:
                info.pipewire_running = True
                info.pipewire_version = info.pulse_server_name.split("PipeWire")[-1].strip(" )")
        else:
            info.errors.append("pactl could not connect to a PulseAudio/PipeWire server.")
    if info.tools.get("pw-cli"):
        res = run(["pw-cli", "info", "0"])
        if res is not None and res.returncode == 0 and "version" in res.stdout:
            info.pipewire_running = True
            if not info.pipewire_version:
                for line in res.stdout.splitlines():
                    if "version" in line and '"' in line:
                        info.pipewire_version = line.split('"')[1]
                        break
    if not info.pipewire_running and not info.pulse_available:
        info.errors.append("PipeWire not detected and PulseAudio is not reachable.")
    return info


def pw_dump() -> list[dict[str, Any]]:
    res = run(["pw-dump", "--no-colors"], timeout=5.0)
    if res is None or res.returncode != 0:
        res = run(["pw-dump"], timeout=5.0)
    if res is None or res.returncode != 0:
        return []
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def node_props(obj: dict[str, Any]) -> dict[str, Any]:
    info = obj.get("info") or {}
    return info.get("props") or {}


def find_nodes(dump: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [
        o for o in dump
        if o.get("type", "").endswith(":Node") and node_props(o).get("node.name") == name
    ]


def consumers_of(dump: list[dict[str, Any]], node_id: int) -> list[str]:
    """Names of applications/nodes that record from the given node."""
    by_id = {o.get("id"): o for o in dump}
    names: list[str] = []
    for o in dump:
        if not o.get("type", "").endswith(":Link"):
            continue
        li = o.get("info") or {}
        if li.get("output-node-id") != node_id:
            continue
        target = by_id.get(li.get("input-node-id"))
        if not target:
            continue
        p = node_props(target)
        label = p.get("application.name") or p.get("node.description") or p.get("node.nick") or p.get("node.name") or "?"
        media = p.get("media.name")
        if media and media != label and len(media) < 40:
            label = f"{label} ({media})"
        if label not in names:
            names.append(label)
    return names


@dataclass
class AudioDevice:
    kind: str  # source | sink
    name: str
    description: str
    index: int | None = None
    state: str = ""
    sample_spec: str = ""
    is_monitor: bool = False
    is_default: bool = False


def _pactl_json(kind: str) -> list[dict[str, Any]] | None:
    res = run(["pactl", "-f", "json", "list", kind])
    if res is None or res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def parse_pactl_short(text: str, kind: str) -> list[AudioDevice]:
    devices = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            idx = None
        name = parts[1]
        devices.append(AudioDevice(
            kind=kind, name=name, description=name, index=idx,
            sample_spec=parts[3] if len(parts) > 3 else "",
            state=parts[4] if len(parts) > 4 else "",
            is_monitor=name.endswith(".monitor"),
        ))
    return devices


def list_audio_devices(kind: str) -> list[AudioDevice]:
    """kind is 'sources' or 'sinks'."""
    singular = kind.rstrip("s")
    defaults = detect_defaults()
    default = defaults.get(singular, "")
    data = _pactl_json(kind)
    devices: list[AudioDevice] = []
    if data is not None:
        for d in data:
            name = d.get("name", "")
            props = d.get("properties") or {}
            devices.append(AudioDevice(
                kind=singular, name=name,
                description=d.get("description") or props.get("node.description") or name,
                index=d.get("index"), state=d.get("state", ""),
                sample_spec=d.get("sample_specification", ""),
                is_monitor=bool(d.get("monitor_of_sink") not in (None, "", "n/a")) or name.endswith(".monitor"),
            ))
    else:
        res = run(["pactl", "list", "short", kind])
        if res is not None and res.returncode == 0:
            devices = parse_pactl_short(res.stdout, singular)
    for dev in devices:
        dev.is_default = dev.name == default
    return devices


def detect_defaults() -> dict[str, str]:
    res = run(["pactl", "info"])
    if res is None or res.returncode != 0:
        return {}
    pi = parse_pactl_info(res.stdout)
    return {"source": pi.get("Default Source", ""), "sink": pi.get("Default Sink", "")}


def recording_applications() -> list[str]:
    """Applications currently recording from any source (pactl source-outputs)."""
    res = run(["pactl", "-f", "json", "list", "source-outputs"])
    apps: list[str] = []
    if res is None or res.returncode != 0:
        return apps
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return apps
    for so in data if isinstance(data, list) else []:
        props = so.get("properties") or {}
        name = props.get("application.name") or props.get("media.name")
        if name and name not in apps:
            apps.append(name)
    return apps
