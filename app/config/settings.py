"""Persistent JSON configuration with defaults and atomic writes.

Only settings are stored here. Microphone audio is never written to disk.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from app.config import paths

log = logging.getLogger(__name__)

BUFFER_CHOICES_MS = (10, 20, 40, 80, 120)
ROUTE_MODES = ("single", "mix")


@dataclass
class DeviceSettings:
    alias: str = ""
    gain_db: float = 0.0
    muted: bool = False
    routed: bool = True
    disabled: bool = False
    monitor: bool = False  # also play this device on a real output (speakers)


@dataclass
class Settings:
    # Network
    host: str = "0.0.0.0"
    port: int = 8765
    https_enabled: bool = True
    https_port: int = 8766
    enable_ipv6: bool = True
    allow_public_clients: bool = False

    # Pairing / authentication
    auth_enabled: bool = True
    pairing_code: str = ""
    regenerate_code_on_start: bool = False

    # Virtual microphone
    source_name: str = "phone-mic"
    source_description: str = "Phone Microphone"
    backend: str = "auto"  # auto | pipewire | pulse | null
    sample_rate: int = 48000
    channels: int = 1

    # Routing and buffering
    route_mode: str = "single"  # single | mix
    active_client: str = ""  # client_id used when route_mode == single
    buffer_ms: int = 40
    master_gain_db: float = 0.0
    master_muted: bool = False
    monitor_sink: str = ""  # sink for per-device monitoring, "" = default output

    # Startup / UI
    adb_auto_reverse: bool = True
    start_minimized: bool = False
    close_to_tray: bool = True
    show_log_panel: bool = True

    devices: dict[str, DeviceSettings] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                continue
            if key == "devices":
                devs = {}
                if isinstance(value, dict):
                    dev_known = {f.name for f in fields(DeviceSettings)}
                    for cid, d in value.items():
                        if isinstance(d, dict):
                            devs[str(cid)] = DeviceSettings(
                                **{k: v for k, v in d.items() if k in dev_known}
                            )
                kwargs[key] = devs
            else:
                kwargs[key] = value
        s = cls(**kwargs)
        s.validate()
        return s

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        for f in fields(Settings):
            if f.name == "devices":
                continue
            default = f.default
            value = getattr(self, f.name)
            if default is not None and not isinstance(value, type(default)):
                # bool is an int subclass, so check exact types for numbers
                try:
                    setattr(self, f.name, type(default)(value))
                except (TypeError, ValueError):
                    log.warning("Invalid config value for %s: %r, using default", f.name, value)
                    setattr(self, f.name, default)
        if not (1 <= self.port <= 65535):
            self.port = 8765
        if not (1 <= self.https_port <= 65535) or self.https_port == self.port:
            self.https_port = self.port + 1
        if self.buffer_ms not in BUFFER_CHOICES_MS:
            self.buffer_ms = min(BUFFER_CHOICES_MS, key=lambda c: abs(c - int(self.buffer_ms)))
        if self.route_mode not in ROUTE_MODES:
            self.route_mode = "single"
        if self.backend not in ("auto", "pipewire", "pulse", "null"):
            self.backend = "auto"
        if self.sample_rate not in (44100, 48000):
            self.sample_rate = 48000
        self.channels = 1
        self.master_gain_db = max(-60.0, min(24.0, float(self.master_gain_db)))
        name = "".join(c for c in self.source_name if c.isalnum() or c in "-_.")
        self.source_name = name or "phone-mic"
        self.source_description = (self.source_description or "Phone Microphone").strip()[:64]

    def device(self, client_id: str) -> DeviceSettings:
        dev = self.devices.get(client_id)
        if dev is None:
            dev = DeviceSettings()
            self.devices[client_id] = dev
        return dev


class ConfigStore:
    """Thread-safe owner of the Settings object and its file."""

    def __init__(self, path: Path | None = None):
        self.path = path or (paths.config_dir() / "config.json")
        self._lock = threading.RLock()
        self.settings = self._load()
        # Values set from the command line apply to this run only and are not saved.
        self._persisted: dict[str, Any] = {}

    def apply_overrides(self, **overrides: Any) -> None:
        with self._lock:
            for key, value in overrides.items():
                if value is None:
                    continue
                if key not in self._persisted:
                    self._persisted[key] = getattr(self.settings, key)
                setattr(self.settings, key, value)
            self.settings.validate()

    def _load(self) -> Settings:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("config root is not an object")
            log.info("Loaded configuration from %s", self.path)
            return Settings.from_dict(data)
        except FileNotFoundError:
            log.info("No configuration at %s, using defaults", self.path)
        except (OSError, ValueError, TypeError) as exc:
            log.warning("Could not read configuration %s (%s), using defaults", self.path, exc)
            try:
                self.path.rename(self.path.with_suffix(".json.bad"))
            except OSError:
                pass
        return Settings()

    def save(self) -> None:
        with self._lock:
            data = self.settings.to_dict()
            data.update(self._persisted)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".config-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, sort_keys=True)
                    fh.write("\n")
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            log.debug("Saved configuration to %s", self.path)

    def update(self, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                if not hasattr(self.settings, key):
                    raise AttributeError(key)
                self._persisted.pop(key, None)
                setattr(self.settings, key, value)
            self.settings.validate()
            self.save()

    @property
    def lock(self) -> threading.RLock:
        return self._lock
