"""Command-line entry point. GUI mode is the default."""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import sys
import threading

from app import APP_NAME, __version__
from app.config import paths

log = logging.getLogger("app")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="phone-mic-router",
        description="Use phones and other browsers as microphones on Linux (PipeWire/PulseAudio virtual source).",
    )
    p.add_argument("--port", type=int, help="HTTP port (default 8765)")
    p.add_argument("--https-port", type=int, help="HTTPS port for Wi-Fi microphones (default 8766)")
    p.add_argument("--host", help="address to listen on (default 0.0.0.0 = all interfaces)")
    p.add_argument("--no-https", action="store_true", help="disable the HTTPS listener")
    p.add_argument("--no-auth", action="store_true", help="disable pairing codes for this run (trusted networks only)")
    p.add_argument("--backend", choices=["auto", "pipewire", "pulse", "null"], help="audio backend")
    p.add_argument("--buffer", type=int, choices=[10, 20, 40, 80, 120], help="jitter buffer in ms")
    p.add_argument("--source-name", help="node name of the virtual microphone (default phone-mic)")
    p.add_argument("--source-description", help="display name of the virtual microphone (default 'Phone Microphone')")
    p.add_argument("--no-adb", action="store_true", help="do not use adb")
    p.add_argument("--no-gui", action="store_true", help="run headless (server + virtual microphone only)")
    p.add_argument("--minimized", action="store_true", help="start minimized to the system tray")
    p.add_argument("--debug", action="store_true", help="verbose logging")
    p.add_argument("--list-devices", action="store_true", help="list Android devices visible to adb and exit")
    p.add_argument("--list-audio-sources", action="store_true", help="list PipeWire/PulseAudio sources and sinks and exit")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return p


def list_devices() -> int:
    from app.adb.adb import AdbManager

    adb = AdbManager(lambda: 8765)
    adb.detect()
    if not adb.available:
        print(adb.last_error)
        return 1
    print(f"adb: {adb.adb_path} ({adb.version})")
    devices = adb.refresh()
    if not adb.server_running:
        print(f"adb is not working: {adb.last_error}")
        return 1
    if not devices:
        print("No Android devices detected. Connect a phone with USB debugging enabled.")
        return 0
    print(f"{'SERIAL':<24} {'NAME':<24} STATE")
    for d in devices:
        print(f"{d.serial:<24} {d.name:<24} {d.state_message()}")
    return 0


def list_audio_sources() -> int:
    from app.audio.backends.detect import detect_audio_system, list_audio_devices

    info = detect_audio_system()
    print(f"PipeWire: {'running ' + info.pipewire_version if info.pipewire_running else 'not detected'}")
    print(f"PulseAudio API: {'available - ' + info.pulse_server_name if info.pulse_available else 'unavailable'}")
    if not info.pulse_available:
        return 1
    for kind, title in (("sources", "SOURCES (microphones / inputs)"), ("sinks", "SINKS (speakers / outputs)")):
        print(f"\n{title}")
        for d in list_audio_devices(kind):
            if d.is_monitor:
                continue
            mark = "*" if d.is_default else " "
            print(f" {mark} {d.index!s:>5}  {d.name:<60} {d.description}")
    print("\n* = default")
    return 0


class InstanceLock:
    def __init__(self) -> None:
        self.path = paths.runtime_dir() / "instance.lock"
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return True


def make_core(args):
    from app.config.settings import ConfigStore
    from app.core import AppCore
    from app.logging_setup import setup_logging

    log_path = setup_logging(args.debug)
    store = ConfigStore()
    store.apply_overrides(
        port=args.port, https_port=args.https_port, host=args.host,
        backend=args.backend, buffer_ms=args.buffer,
        source_name=args.source_name, source_description=args.source_description,
        auth_enabled=False if args.no_auth else None,
        https_enabled=False if args.no_https else None,
    )
    return AppCore(store, log_path=log_path, enable_adb=not args.no_adb)


def run_headless(args) -> int:
    core = make_core(args)
    stop = threading.Event()

    def on_signal(signum, _frame):
        log.info("Received signal %d", signum)
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    try:
        core.start()
        snap = core.snapshot()
        print()
        print(f"  {APP_NAME} running.  Virtual microphone: {snap['audio']['source']['description']} "
              f"({snap['audio']['source']['node_name']}, backend {snap['audio']['backend']})")
        if snap["audio"]["error"]:
            print(f"  ! {snap['audio']['error']}")
        if snap["server"]["error"]:
            print(f"  ! {snap['server']['error']}")
        print(f"  Pairing code: {snap['auth']['formatted'] if snap['auth']['enabled'] else 'disabled'}")
        print("  Open on the phone:")
        for u in snap["urls"]:
            print(f"    {u['url']:<36} {u['kind']:<20} mic: {u['mic']}")
        print("  Press Ctrl+C to stop.\n", flush=True)
        last = 0
        while not stop.wait(1.0):
            if args.debug or core.version != last:
                last = core.version
                for d in core.device_rows():
                    if d["connected"]:
                        log.debug("%s %s %.0f dBFS latency~%s ms", d["name"], d["state"], d["rms_db"],
                                  None if d["latency_ms"] is None else round(d["latency_ms"]))
    finally:
        core.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_devices:
        return list_devices()
    if args.list_audio_sources:
        return list_audio_sources()
    lock = InstanceLock()
    if not lock.acquire():
        msg = f"{APP_NAME} is already running (lock {lock.path})."
        print(msg, file=sys.stderr)
        if not args.no_gui:
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox
                app = QApplication.instance() or QApplication(sys.argv)
                QMessageBox.warning(None, APP_NAME, msg + "\nCheck your system tray.")
            except Exception:  # noqa: BLE001
                pass
        return 2
    if args.no_gui:
        return run_headless(args)
    try:
        from app.gui.app import run_gui
    except ImportError as exc:
        print(f"Cannot start the GUI ({exc}). Install PySide6 or use --no-gui.", file=sys.stderr)
        return 1
    core = make_core(args)
    return run_gui(core, start_minimized=args.minimized or core.store.settings.start_minimized)


if __name__ == "__main__":
    raise SystemExit(main())
