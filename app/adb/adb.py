"""Android Debug Bridge integration for USB microphones.

`adb reverse tcp:PORT tcp:PORT` makes http://127.0.0.1:PORT on the phone reach
this computer over the USB cable. Browsers treat localhost as a secure
context, so microphone capture works without HTTPS. Nothing is installed on
the phone and no root is needed. USB debugging must be enabled once in the
phone's developer options.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

POLL_INTERVAL = 2.0


@dataclass
class AdbDevice:
    serial: str
    state: str  # device | unauthorized | offline | no permissions | recovery | ...
    model: str = ""
    product: str = ""
    device: str = ""
    transport_id: str = ""
    usb: str = ""

    @property
    def name(self) -> str:
        if self.model:
            return self.model.replace("_", " ")
        return self.device or self.serial

    @property
    def ready(self) -> bool:
        return self.state == "device"

    @property
    def is_network(self) -> bool:
        return ":" in self.serial or self.serial.startswith("adb-")

    def state_message(self) -> str:
        return {
            "device": "Connected",
            "unauthorized": "Unauthorized: unlock the phone and accept the 'Allow USB debugging' prompt",
            "offline": "Offline: reconnect the USB cable or restart adb",
            "no permissions": "No permission: add a udev rule for Android devices (see Troubleshooting)",
            "recovery": "In recovery mode",
            "sideload": "In sideload mode",
            "bootloader": "In bootloader mode",
            "authorizing": "Authorizing…",
            "connecting": "Connecting…",
        }.get(self.state, self.state)


def parse_devices(text: str) -> list[AdbDevice]:
    """Parse `adb devices -l` output."""
    devices = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial = parts[0]
        rest = parts[1:]
        # The state may be two words ("no permissions"); key:value pairs follow.
        state_words = []
        props = {}
        for tok in rest:
            if ":" in tok and not state_words[-1:] == ["no"]:
                k, _, v = tok.partition(":")
                props[k] = v
            elif not props:
                state_words.append(tok)
        state = " ".join(state_words)
        if state.startswith("no permissions"):
            state = "no permissions"
        devices.append(AdbDevice(
            serial=serial, state=state,
            model=props.get("model", ""), product=props.get("product", ""),
            device=props.get("device", ""), transport_id=props.get("transport_id", ""),
            usb=props.get("usb", ""),
        ))
    return devices


def parse_reverse_list(text: str) -> list[tuple[str, str, str]]:
    """Parse `adb reverse --list`: lines like '<serial> tcp:8765 tcp:8765' (serial may be 'UsbFfs')."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            out.append((parts[0], parts[1], parts[2]))
        elif len(parts) == 2:
            out.append(("", parts[0], parts[1]))
    return out


def explain_reverse_error(stderr: str) -> str:
    s = stderr.lower()
    if "unauthorized" in s:
        return "The phone has not authorized this computer. Unlock it and accept the 'Allow USB debugging' prompt."
    if "offline" in s:
        return "The phone is offline. Reconnect the USB cable or run 'adb kill-server'."
    if "no devices" in s or "not found" in s:
        return "No Android device is connected via USB."
    if "more than one" in s:
        return "More than one device is connected; select one."
    if "unknown command" in s or "not supported" in s or "closed" in s:
        return "This device or adb version does not support 'adb reverse' (requires Android 5.0+ and a recent adb)."
    if "cannot bind" in s or "address already in use" in s:
        return "The phone already uses this port for something else. Choose another server port."
    return stderr.strip() or "adb reverse failed for an unknown reason."


class AdbManager:
    def __init__(self, port_getter: Callable[[], int], runner: Callable[..., subprocess.CompletedProcess] | None = None):
        self._port = port_getter
        self._runner = runner or self._default_runner
        self.adb_path: str | None = shutil.which("adb")
        self.version = ""
        self.devices: list[AdbDevice] = []
        self.reversed: dict[str, int] = {}  # serial -> port with an active reverse
        self.wanted: set[str] = set()  # serials the user enabled
        self.auto_all = False  # enable reverse on every authorized device automatically
        self.errors: dict[str, str] = {}
        self.last_error = ""
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.on_change: Callable[[], None] = lambda: None
        self.server_running = False

    @staticmethod
    def _default_runner(args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    @property
    def available(self) -> bool:
        return self.adb_path is not None

    def _adb(self, *args: str, serial: str | None = None, timeout: float = 5.0) -> subprocess.CompletedProcess | None:
        if not self.adb_path:
            return None
        cmd = [self.adb_path]
        if serial:
            cmd += ["-s", serial]
        cmd += list(args)
        try:
            return self._runner(cmd, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.last_error = f"adb {' '.join(args)} failed: {exc}"
            log.debug(self.last_error)
            return None

    def detect(self) -> None:
        self.adb_path = shutil.which("adb") if self._runner is self._default_runner else (self.adb_path or "adb")
        if not self.adb_path:
            self.last_error = "adb executable not found. Install android-tools (Arch), adb (Debian/Ubuntu) or android-tools (Fedora)."
            return
        res = self._adb("version")
        if res is not None and res.returncode == 0:
            first = res.stdout.splitlines()[0] if res.stdout else ""
            self.version = first.replace("Android Debug Bridge version", "").strip()

    def refresh(self) -> list[AdbDevice]:
        if not self.adb_path:
            return []
        res = self._adb("devices", "-l", timeout=8.0)
        if res is None or res.returncode != 0:
            self.server_running = False
            if res is not None:
                self.last_error = (res.stderr or "").strip()
            return self.devices
        self.server_running = True
        devices = parse_devices(res.stdout)
        changed = False
        with self._lock:
            old = {(d.serial, d.state) for d in self.devices}
            new = {(d.serial, d.state) for d in devices}
            if old != new:
                changed = True
                for d in devices:
                    if (d.serial, d.state) not in old:
                        log.info("ADB device %s (%s): %s", d.name, d.serial, d.state_message())
                gone = {s for s, _ in old} - {d.serial for d in devices}
                for serial in gone:
                    log.info("ADB device %s disconnected", serial)
                    self.reversed.pop(serial, None)
            self.devices = devices
            ready = [d for d in devices if d.ready]
            port = self._port()
        # (Re)apply reverse forwarding for devices that want it.
        for d in ready:
            if (d.serial in self.wanted or self.auto_all) and self.reversed.get(d.serial) != port:
                if self.enable_reverse(d.serial, quiet=True):
                    changed = True
            elif d.serial in self.reversed and not self._reverse_present(d.serial, port):
                # Reverse disappeared (device rebooted, adb restarted).
                self.reversed.pop(d.serial, None)
                changed = True
        if changed:
            self.on_change()
        return devices

    def _reverse_present(self, serial: str, port: int) -> bool:
        res = self._adb("reverse", "--list", serial=serial)
        if res is None or res.returncode != 0:
            return False
        return any(remote == f"tcp:{port}" for _, remote, _ in parse_reverse_list(res.stdout))

    def enable_reverse(self, serial: str, quiet: bool = False) -> bool:
        port = self._port()
        with self._lock:
            self.wanted.add(serial)
        res = self._adb("reverse", f"tcp:{port}", f"tcp:{port}", serial=serial)
        if res is None:
            self.errors[serial] = self.last_error or "adb did not respond"
            return False
        if res.returncode != 0:
            msg = explain_reverse_error(res.stderr or res.stdout)
            self.errors[serial] = msg
            if not quiet or self.reversed.get(serial):
                log.error("adb reverse failed for %s: %s", serial, msg)
            return False
        if not self._reverse_present(serial, port):
            self.errors[serial] = "adb reverse reported success but the forwarding is not listed."
            log.warning("adb reverse on %s not visible in --list", serial)
        self.errors.pop(serial, None)
        with self._lock:
            self.reversed[serial] = port
        log.info("USB microphone enabled for %s: phone http://127.0.0.1:%d -> this computer", serial, port)
        return True

    def disable_reverse(self, serial: str) -> None:
        port = self.reversed.get(serial, self._port())
        with self._lock:
            self.wanted.discard(serial)
            self.reversed.pop(serial, None)
        res = self._adb("reverse", "--remove", f"tcp:{port}", serial=serial)
        if res is not None and res.returncode == 0:
            log.info("USB forwarding removed for %s", serial)
        self.on_change()

    def remove_all(self) -> None:
        for serial in list(self.reversed):
            port = self.reversed[serial]
            self._adb("reverse", "--remove", f"tcp:{port}", serial=serial, timeout=3.0)
        self.reversed.clear()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "available": self.available,
                "path": self.adb_path,
                "version": self.version,
                "server_running": self.server_running,
                "devices": [
                    {
                        "serial": d.serial, "name": d.name, "state": d.state,
                        "message": d.state_message(), "ready": d.ready,
                        "reverse": d.serial in self.reversed, "wanted": d.serial in self.wanted,
                        "error": self.errors.get(d.serial, ""), "network": d.is_network,
                    }
                    for d in self.devices
                ],
                "auto_all": self.auto_all,
                "last_error": self.last_error,
            }

    # ------------------------------------------------------------------ monitor thread
    def start_monitor(self) -> None:
        if not self.available or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="adb-monitor", daemon=True)
        self._thread.start()

    def stop_monitor(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                log.exception("ADB monitor error")
            self._stop.wait(POLL_INTERVAL)
            # Back off a little if adb is failing repeatedly
            if not self.server_running:
                self._stop.wait(POLL_INTERVAL * 2)
