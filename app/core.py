"""AppCore: owns every subsystem and exposes thread-safe controls and snapshots.

The GUI never talks to sockets or audio directly. It calls methods here and
polls snapshot(), which returns plain dicts.
"""

from __future__ import annotations

import collections
import logging
import platform
import sys
import threading
import time
from dataclasses import asdict
from typing import Any

from app import APP_NAME, __version__
from app.adb.adb import AdbManager
from app.audio.backends import AudioBackend, BackendError, create_backend
from app.audio.backends.detect import AudioSystemInfo, detect_audio_system, list_audio_devices, recording_applications
from app.audio.backends.monitor import MonitorOutput
from app.audio.backends.null import NullBackend
from app.audio.engine import AudioEngine
from app.config import paths
from app.config.settings import BUFFER_CHOICES_MS, ConfigStore
from app.devices.registry import DISCONNECTED, DeviceRegistry, estimate_latency_ms
from app.server import netinfo
from app.server.auth import Authenticator, format_code, generate_pairing_code
from app.server.tls import CertificateManager
from app.server.web_server import ServerContext, ServerError, WebServer

log = logging.getLogger(__name__)


class AppCore:
    def __init__(self, store: ConfigStore, *, backend: AudioBackend | None = None, log_path: str | None = None,
                 enable_adb: bool = True, enable_tls: bool | None = None):
        self.store = store
        s = store.settings
        if not s.pairing_code or s.regenerate_code_on_start:
            s.pairing_code = generate_pairing_code()
            store.save()
        self.auth = Authenticator(s.pairing_code, s.auth_enabled)
        self.registry = DeviceRegistry(lambda: self.store.settings, self._changed)
        self.audio_info = AudioSystemInfo()
        self._forced_backend = backend
        self.backend: AudioBackend = backend or NullBackend(s.source_name, s.source_description, s.sample_rate)
        self.engine = AudioEngine(self.registry, self.backend, lambda: self.store.settings, s.sample_rate)
        self.monitor = MonitorOutput(s.monitor_sink, s.sample_rate)
        self.engine.monitor = None
        self.adb = AdbManager(lambda: self.server.http_port or self.store.settings.port)
        self.adb.on_change = self._changed
        self.enable_adb = enable_adb
        self.tls_enabled = s.https_enabled if enable_tls is None else enable_tls
        self.certs = CertificateManager(paths.tls_dir())
        self.ssl_ctx = None
        self.addresses: list[netinfo.LocalAddress] = []
        self.server = WebServer(ServerContext(
            registry=self.registry,
            auth=self.auth,
            settings=lambda: self.store.settings,
            output_rate=s.sample_rate,
            local_addresses=lambda: self.addresses,
            route_info=self.route_info,
            backend_queue_ms=self.backend_latency_ms,
            ca_der=self._ca_der,
            https_port=lambda: self.server.https_port,
        ))
        self.server_error = ""
        self.backend_error = ""
        self.tls_error = ""
        self.log_path = log_path
        self.started = False
        self.starting = False
        self.version = 0  # bumps on structural changes
        self.notifications: collections.deque[tuple[float, str, str]] = collections.deque(maxlen=50)
        self._dirty = False
        self._stop = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._cache_lock = threading.Lock()
        self._audio_devices: dict[str, list] = {"sources": [], "sinks": []}
        self._recording_apps: list[str] = []
        self.start_time = time.time()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.starting = True
        s = self.store.settings
        log.info("%s %s starting (Python %s, %s)", APP_NAME, __version__, platform.python_version(), platform.platform())
        # 1. audio system
        if self._forced_backend is None:
            self.audio_info = detect_audio_system()
            log.info(
                "Audio: PipeWire %s, PulseAudio API %s (%s)",
                f"running {self.audio_info.pipewire_version}" if self.audio_info.pipewire_running else "not detected",
                "available" if self.audio_info.pulse_available else "unavailable",
                self.audio_info.pulse_server_name or "-",
            )
            for err in self.audio_info.errors:
                self.notify("error", err)
            self.backend = create_backend(s.backend, self.audio_info, s.source_name, s.source_description, s.sample_rate)
            self.engine.swap_backend(self.backend)
        self._start_backend()
        # 2. network addresses + TLS
        self.refresh_addresses()
        self._setup_tls()
        # 3. server
        self._start_server()
        # 4. engine
        self.engine.start()
        # 5. adb
        if self.enable_adb:
            self.adb.detect()
            if self.adb.available:
                log.info("adb found: %s (%s)", self.adb.adb_path, self.adb.version)
                self.adb.auto_all = s.adb_auto_reverse
                self.adb.start_monitor()
            else:
                log.warning(self.adb.last_error)
        self._update_monitor()
        self._refresh_audio_lists()
        self._supervisor = threading.Thread(target=self._supervise, name="supervisor", daemon=True)
        self._supervisor.start()
        self.started = True
        self.starting = False
        self._changed()
        log.info("Startup complete%s", "" if self.auth.enabled else " (pairing disabled)")

    def stop(self) -> None:
        log.info("Shutting down")
        self._stop.set()
        for sess in self.registry.sessions():
            if sess.connected:
                self.registry.disconnect(sess.client_id, "server_shutdown")
        time.sleep(0.1)
        self.server.stop()
        self.engine.stop()
        self.monitor.stop()
        try:
            self.backend.stop()
        except Exception:
            log.exception("Error removing virtual microphone")
        if self.enable_adb:
            self.adb.stop_monitor()
            self.adb.remove_all()
        if self._supervisor:
            self._supervisor.join(timeout=3)
        self.save_if_dirty(force=True)
        self.started = False
        log.info("Shutdown complete")

    def _start_backend(self) -> None:
        try:
            self.backend.start()
            self.backend_error = ""
        except BackendError as exc:
            self.backend_error = str(exc)
            self.backend.info.state = "error"
            self.backend.info.error = str(exc)
            self.notify("error", f"Virtual microphone could not be created: {exc}")
            log.error("Virtual microphone could not be created: %s", exc)

    def _setup_tls(self) -> None:
        if not self.tls_enabled:
            self.ssl_ctx = None
            return
        try:
            self.certs.ensure(self._cert_hosts())
            if self.ssl_ctx is None:
                self.ssl_ctx = self.certs.ssl_context()
            else:
                self.ssl_ctx.load_cert_chain(self.certs.cert_path, self.certs.key_path)
            self.tls_error = ""
        except Exception as exc:  # noqa: BLE001 - report any crypto/IO failure
            self.tls_error = f"HTTPS unavailable: {exc}"
            self.ssl_ctx = None
            log.exception("TLS setup failed")
            self.notify("error", self.tls_error)

    def _cert_hosts(self) -> list[str]:
        hosts = [a.address for a in self.addresses]
        hn = netinfo.hostname()
        if hn:
            hosts += [hn, f"{hn}.local"]
        return hosts

    def _start_server(self) -> None:
        s = self.store.settings
        try:
            self.server.start(s.host, s.port, s.https_port if self.ssl_ctx else None, self.ssl_ctx, s.enable_ipv6)
            self.server_error = ""
        except (ServerError, OSError) as exc:
            self.server_error = str(exc)
            self.notify("error", str(exc))
            log.error("Web server failed: %s", exc)

    def restart_server(self) -> None:
        if self.enable_adb:
            # Drop forwardings for the old port; the ADB monitor re-applies them
            # (for every device the user enabled) with the new port.
            self.adb.remove_all()
        self.server.stop()
        self.server = WebServer(self.server.ctx)
        self._setup_tls()
        self._start_server()
        if self.enable_adb and self.adb.available:
            self.adb.refresh()
        self._changed()

    def _supervise(self) -> None:
        tick = 0
        while not self._stop.wait(1.0):
            tick += 1
            try:
                self.registry.check_health()
                if tick % 2 == 0:
                    if self.backend.info.state != "stopped" or self.backend_error:
                        if not self.backend.is_alive():
                            if self.backend.ensure_running():
                                self.backend_error = ""
                                self.notify("info", "Virtual microphone was recreated (audio server restarted?)")
                        elif tick % 4 == 0:
                            self.backend.refresh_info()  # pw-dump: node ids + recording apps
                    if self.monitor.running is False and self.engine.monitor is not None:
                        self._update_monitor()
                if tick % 5 == 0:
                    self._refresh_audio_lists()
                    self.save_if_dirty()
                if tick % 15 == 0:
                    old = {a.address for a in self.addresses}
                    self.refresh_addresses()
                    if {a.address for a in self.addresses} != old:
                        log.info("Network addresses changed")
                        self._setup_tls()
                        self._changed()
                if tick % 60 == 0:
                    self.registry.prune()
            except Exception:
                log.exception("Supervisor error")

    def _refresh_audio_lists(self) -> None:
        if self.audio_info.pulse_available:
            sources = list_audio_devices("sources")
            sinks = list_audio_devices("sinks")
            apps = recording_applications()
        else:
            sources, sinks, apps = [], [], []
        with self._cache_lock:
            self._audio_devices = {"sources": sources, "sinks": sinks}
            self._recording_apps = apps

    def refresh_addresses(self) -> None:
        self.addresses = netinfo.local_addresses(self.store.settings.enable_ipv6)

    # ------------------------------------------------------------------ helpers
    def _changed(self) -> None:
        self.version += 1

    def notify(self, level: str, text: str) -> None:
        self.notifications.append((time.time(), level, text))
        self._changed()

    def _ca_der(self) -> bytes | None:
        if not self.ssl_ctx:
            return None
        try:
            return self.certs.ca_der()
        except OSError:
            return None

    def backend_latency_ms(self) -> float:
        q = self.backend.queued_ms()
        base = getattr(self.backend, "graph_latency_ms", 10)
        return (q or 0.0) + base

    def route_info(self, client_id: str) -> dict:
        s = self.store.settings
        dev = s.devices.get(client_id)
        return {
            "routed": client_id in self.engine.route.active,
            "target": s.source_description,
            "muted": bool(dev and dev.muted) or s.master_muted,
        }

    def mark_dirty(self) -> None:
        self._dirty = True

    def save_if_dirty(self, force: bool = False) -> None:
        if self._dirty or force:
            self._dirty = False
            try:
                self.store.save()
            except OSError as exc:
                log.error("Could not save configuration: %s", exc)

    # ------------------------------------------------------------------ controls (thread-safe)
    def set_device(self, client_id: str, **changes: Any) -> None:
        with self.store.lock:
            dev = self.store.settings.device(client_id)
            for k, v in changes.items():
                if not hasattr(dev, k):
                    raise AttributeError(k)
                setattr(dev, k, v)
        if "gain_db" in changes:
            self.mark_dirty()
        else:
            self.save_if_dirty(force=True)
        if "monitor" in changes:
            self._update_monitor()
        self._changed()

    def rename_device(self, client_id: str, alias: str) -> None:
        alias = "".join(c for c in alias if c.isprintable()).strip()[:60]
        self.set_device(client_id, alias=alias)
        log.info("Device %s renamed to %r", client_id, alias)

    def disconnect_device(self, client_id: str) -> None:
        self.registry.disconnect(client_id, "disconnected by user")

    def forget_device(self, client_id: str) -> None:
        self.registry.remove(client_id)
        with self.store.lock:
            self.store.settings.devices.pop(client_id, None)
            if self.store.settings.active_client == client_id:
                self.store.settings.active_client = ""
        self.save_if_dirty(force=True)
        self._changed()

    def set_active_client(self, client_id: str) -> None:
        with self.store.lock:
            self.store.settings.active_client = client_id
            if client_id:
                self.store.settings.device(client_id).routed = True
        self.save_if_dirty(force=True)
        self._changed()

    def set_setting(self, **changes: Any) -> None:
        self.store.update(**changes)
        if "auth_enabled" in changes:
            self.auth.enabled = self.store.settings.auth_enabled
        if "adb_auto_reverse" in changes:
            self.adb.auto_all = self.store.settings.adb_auto_reverse
        if "monitor_sink" in changes:
            self.monitor.sink = self.store.settings.monitor_sink
            if self.monitor.running:
                self.monitor.start()
        self._changed()

    def set_buffer_ms(self, ms: int) -> None:
        if ms not in BUFFER_CHOICES_MS:
            raise ValueError(ms)
        self.set_setting(buffer_ms=ms)
        log.info("Jitter buffer set to %d ms", ms)

    def regenerate_pairing_code(self) -> str:
        code = self.auth.regenerate()
        self.store.update(pairing_code=code)
        log.info("New pairing code generated")
        self._changed()
        return code

    def set_virtual_source(self, name: str, description: str, backend_kind: str | None = None) -> None:
        """Recreate the virtual microphone with a new name/description/backend."""
        self.store.update(source_name=name, source_description=description,
                          **({"backend": backend_kind} if backend_kind else {}))
        s = self.store.settings
        log.info("Recreating virtual microphone as %s (%s)", s.source_name, s.source_description)
        new = create_backend(s.backend, self.audio_info, s.source_name, s.source_description, s.sample_rate)
        old = self.engine.swap_backend(NullBackend(s.source_name, s.source_description, s.sample_rate))
        old.stop()
        self.backend = new
        self._start_backend()
        self.engine.swap_backend(self.backend)
        self._changed()

    def _update_monitor(self) -> None:
        wanted = any(d.monitor for d in self.store.settings.devices.values())
        if wanted and not self.monitor.running:
            if self.monitor.start():
                self.engine.monitor = self.monitor
        elif not wanted and self.monitor.running:
            self.engine.monitor = None
            self.monitor.stop()

    def adb_enable(self, serial: str) -> tuple[bool, str]:
        ok = self.adb.enable_reverse(serial)
        self._changed()
        return ok, self.adb.errors.get(serial, "")

    def adb_disable(self, serial: str) -> None:
        self.adb.disable_reverse(serial)

    def adb_refresh(self) -> None:
        self.adb.detect()
        if self.adb.available:
            self.adb.refresh()
            self.adb.start_monitor()
        self._changed()

    def redetect_audio(self) -> None:
        self.audio_info = detect_audio_system()
        self._refresh_audio_lists()
        self._changed()

    # ------------------------------------------------------------------ snapshot
    def urls(self) -> list[dict]:
        s = self.store.settings
        out = []
        port = self.server.http_port or s.port
        https = self.server.https_port
        out.append({
            "url": f"http://127.0.0.1:{port}/", "kind": "USB (ADB) / this PC", "interface": "localhost",
            "secure": True, "mic": "Yes (localhost is a secure context)",
        })
        for a in netinfo.usable_lan_addresses(self.addresses):
            if a.kind == "vpn" and not s.allow_public_clients:
                continue  # VPN peers are rejected unless "accept clients outside private networks" is on
            label = {"wifi": "Wi-Fi", "lan": "LAN", "vpn": "VPN"}.get(a.kind, a.kind)
            if https:
                out.append({
                    "url": a.url(https, "https"), "kind": f"{label} (HTTPS)", "interface": a.interface,
                    "secure": True, "mic": "Yes, after accepting the certificate once",
                })
            out.append({
                "url": a.url(port, "http"), "kind": f"{label} (HTTP)", "interface": a.interface,
                "secure": False, "mic": "No: browsers block the microphone on plain HTTP",
            })
        return out

    def best_phone_url(self, with_code: bool = True) -> str:
        for u in self.urls():
            if u["secure"] and u["interface"] != "localhost":
                url = u["url"]
                break
        else:
            url = self.urls()[0]["url"]
        if with_code and self.auth.enabled:
            url += f"?code={self.auth.code}"
        return url

    def device_rows(self) -> list[dict]:
        s = self.store.settings
        rows = []
        now = time.time()
        backend_ms = self.backend_latency_ms()
        mono = time.monotonic()
        for sess in self.registry.sessions():
            dev = s.devices.get(sess.client_id)
            cfg = sess.audio_config
            stale = not sess.last_packet_monotonic or mono - sess.last_packet_monotonic > 0.5
            jb = sess.jitter
            rows.append({
                "client_id": sess.client_id,
                "number": sess.number,
                "name": self.registry.display_name(sess),
                "detected_name": sess.name,
                "alias": dev.alias if dev else "",
                "ip": sess.remote_ip,
                "connection": sess.connection,
                "browser": sess.browser,
                "platform": sess.platform,
                "secure": sess.secure,
                "state": sess.state,
                "connected": sess.connected,
                "duration": sess.duration(now),
                "sample_rate": cfg.sample_rate if cfg else None,
                "channels": cfg.channels if cfg else None,
                "resampling": bool(cfg and cfg.sample_rate != s.sample_rate),
                "rms_db": -90.0 if stale else sess.meter.rms_db,
                "peak_db": -90.0 if stale else sess.meter.peak_hold_db,
                "clipping": sess.meter.clipping(),
                "latency_ms": estimate_latency_ms(sess, backend_ms) if sess.connected else None,
                "rtt_ms": sess.rtt_ms,
                "buffer_ms": jb.fill_ms if jb else 0.0,
                "underruns": jb.underruns if jb else 0,
                "routed_now": sess.client_id in self.engine.route.active,
                "route_target": s.source_description,
                "gain_db": dev.gain_db if dev else 0.0,
                "muted": bool(dev and dev.muted),
                "routed": dev.routed if dev else True,
                "disabled": bool(dev and dev.disabled),
                "monitor": bool(dev and dev.monitor),
                "packets": sess.packets,
                "lost": sess.lost_packets,
                "malformed": sess.malformed_packets,
                "overflows": sess.queue_overflows,
                "reconnects": sess.reconnects,
                "client_muted": sess.client_reported_muted,
                "warnings": list(sess.warnings),
            })
        return rows

    def snapshot(self) -> dict:
        s = self.store.settings
        with self._cache_lock:
            audio_devices = {k: [asdict(d) for d in v] for k, v in self._audio_devices.items()}
            rec_apps = list(self._recording_apps)
        info = self.backend.info
        return {
            "started": self.started,
            "starting": self.starting,
            "version": self.version,
            "server": {
                "running": self.server.running,
                "http_port": self.server.http_port or s.port,
                "https_port": self.server.https_port,
                "listen_hosts": self.server.listen_hosts,
                "ws_connections": self.server.ws_connections,
                "total_connections": self.server.total_connections,
                "rejected": self.server.rejected_connections,
                "error": self.server_error,
                "tls_error": self.tls_error,
            },
            "urls": self.urls(),
            "phone_url": self.best_phone_url(),
            "auth": {"enabled": self.auth.enabled, "code": self.auth.code, "formatted": format_code(self.auth.code)},
            "audio": {
                "system": asdict(self.audio_info),
                "backend": self.backend.name,
                "source": asdict(info),
                "error": self.backend_error,
                "route_mode": s.route_mode,
                "active": list(self.engine.route.active),
                "buffer_ms": s.buffer_ms,
                "master_gain_db": s.master_gain_db,
                "master_muted": s.master_muted,
                "master_rms_db": self.master_meter_rms(),
                "master_peak_db": self.engine.master_meter.peak_hold_db,
                "master_clipping": self.engine.master_meter.clipping(),
                "engine_running": self.engine.running,
                "engine_cpu": self.engine.cpu_load,
                "backend_queue_ms": self.backend.queued_ms(),
                "backend_drops": self.backend.write_drops,
                "backend_stalls": self.engine.backend_stalls,
                "devices": audio_devices,
                "recording_apps": rec_apps,
                "monitor_running": self.monitor.running,
            },
            "devices": self.device_rows(),
            "connected_count": self.registry.connected_count(),
            "adb": self.adb.snapshot(),
            "addresses": [asdict(a) for a in self.addresses],
            "tls": {
                "enabled": bool(self.ssl_ctx),
                "ca_path": str(self.certs.ca_cert_path),
                "fingerprint": self._safe_fingerprint(),
                "hosts": self.certs.san_hosts(),
            },
            "notifications": list(self.notifications),
            "log_path": self.log_path,
            "config_path": str(self.store.path),
        }

    def master_meter_rms(self) -> float:
        if not self.engine.route.active:
            return -90.0
        return self.engine.master_meter.rms_db

    def _safe_fingerprint(self) -> str:
        try:
            return self.certs.ca_fingerprint() if self.ssl_ctx else ""
        except (OSError, ValueError):
            return ""

    def diagnostics_text(self) -> str:
        """Plain-text diagnostic report. Contains no audio data and no pairing code."""
        snap = self.snapshot()
        a = snap["audio"]
        sysinfo = a["system"]
        lines = [
            f"{APP_NAME} {__version__}",
            f"Python {sys.version.split()[0]} on {platform.platform()}",
            f"Uptime: {int(time.time() - self.start_time)} s",
            "",
            "[Audio]",
            f"PipeWire: {'running ' + sysinfo['pipewire_version'] if sysinfo['pipewire_running'] else 'NOT detected'}",
            f"PulseAudio compatibility: {'available (' + sysinfo['pulse_server_name'] + ')' if sysinfo['pulse_available'] else 'unavailable'}",
            "Tools: " + ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in sysinfo["tools"].items()),
            f"Backend: {a['backend']}  state={a['source']['state']}  error={a['error'] or '-'}",
            f"Virtual source: {a['source']['node_name']} ({a['source']['description']}) node id={a['source']['node_id']} serial={a['source']['object_serial']}",
            f"Helper sink: {a['source']['helper_node_name'] or '-'} id={a['source']['helper_node_id']}",
            f"Recording from virtual mic: {', '.join(a['source']['consumers']) or 'nobody'}",
            f"Restarts: {a['source']['restarts']}  backend drops: {a['backend_drops']}  stalls: {a['backend_stalls']}",
            f"Route mode: {a['route_mode']}  active: {a['active']}  buffer: {a['buffer_ms']} ms",
            f"Engine running: {a['engine_running']}  CPU: {a['engine_cpu'] * 100:.1f}% of real time",
            f"Default source: {sysinfo['default_source']}",
            f"Default sink: {sysinfo['default_sink']}",
            "",
            "[Server]",
            f"HTTP: {'running' if snap['server']['running'] else 'stopped'} port {snap['server']['http_port']} on {snap['server']['listen_hosts']}",
            f"HTTPS: port {snap['server']['https_port']}  TLS error: {snap['server']['tls_error'] or '-'}",
            f"Server error: {snap['server']['error'] or '-'}",
            f"WebSocket connections: {snap['server']['ws_connections']} (total {snap['server']['total_connections']}, rejected {snap['server']['rejected']})",
            f"Pairing: {'enabled' if snap['auth']['enabled'] else 'disabled'}",
            f"CA fingerprint: {snap['tls']['fingerprint'] or '-'}",
            f"Certificate hosts: {', '.join(snap['tls']['hosts'])}",
            "",
            "[Network]",
        ]
        for addr in snap["addresses"]:
            lines.append(f"  {addr['interface']:<12} {addr['address']}/{addr['prefixlen']} ({addr['kind']})")
        lines.append("URLs:")
        for u in snap["urls"]:
            lines.append(f"  {u['url']:<36} {u['kind']:<22} mic: {u['mic']}")
        adb = snap["adb"]
        lines += [
            "",
            "[ADB]",
            f"adb: {adb['path'] or 'NOT found'} {adb['version']}",
            f"adb server running: {adb['server_running']}  auto reverse: {adb['auto_all']}",
        ]
        for d in adb["devices"]:
            lines.append(f"  {d['serial']} {d['name']} state={d['state']} reverse={d['reverse']} error={d['error'] or '-'}")
        if not adb["devices"]:
            lines.append("  no devices")
        lines += ["", "[Devices]"]
        for d in snap["devices"]:
            lines.append(
                f"  #{d['number']} {d['name']} {d['ip']} {d['connection']} state={d['state']} "
                f"rate={d['sample_rate']} ch={d['channels']} rtt={d['rtt_ms'] and round(d['rtt_ms'])}ms "
                f"buf={d['buffer_ms']:.0f}ms lost={d['lost']} malformed={d['malformed']} underruns={d['underruns']} "
                f"reconnects={d['reconnects']} browser={d['browser']} secure={d['secure']}"
            )
        if not snap["devices"]:
            lines.append("  none")
        lines += ["", f"Config: {snap['config_path']}", f"Log: {snap['log_path']}"]
        return "\n".join(lines)
