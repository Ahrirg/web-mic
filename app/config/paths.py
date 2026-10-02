"""XDG-compliant locations for config, state and logs."""

from __future__ import annotations

import os
from pathlib import Path

from app import APP_ID


def _xdg(var: str, default: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / default


def config_dir() -> Path:
    override = os.environ.get("PHONE_MIC_ROUTER_CONFIG_DIR")
    if override:
        return Path(override)
    return _xdg("XDG_CONFIG_HOME", ".config") / APP_ID


def state_dir() -> Path:
    override = os.environ.get("PHONE_MIC_ROUTER_STATE_DIR")
    if override:
        return Path(override)
    return _xdg("XDG_STATE_HOME", ".local/state") / APP_ID


def log_dir() -> Path:
    return state_dir() / "log"


def tls_dir() -> Path:
    return config_dir() / "tls"


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / APP_ID
