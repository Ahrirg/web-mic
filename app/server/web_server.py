"""HTTP + WebSocket server (aiohttp) running in its own thread and event loop."""

from __future__ import annotations

import asyncio
import errno
import logging
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from aiohttp import WSMsgType, web

from app import APP_NAME, __version__
from app.devices.naming import browser_name, friendly_name, platform_name
from app.devices.registry import PAUSED, STREAMING, WAITING, ClientSession, DeviceRegistry, estimate_latency_ms
from app.server import netinfo
from app.server.auth import Authenticator
from app.server.protocol import (
    ProtocolError,
    dumps,
    parse_audio,
    parse_text,
    validate_audio_config,
    validate_hello,
)

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
HELLO_TIMEOUT = 10.0
MAX_MALFORMED = 50
PING_INTERVAL = 1.0
STATIC_FILES = {
    "app.js": "application/javascript",
    "worklet.js": "application/javascript",
    "style.css": "text/css",
    "icon.svg": "image/svg+xml",
    "manifest.webmanifest": "application/manifest+json",
}


class ServerError(RuntimeError):
    pass


@dataclass
class ServerContext:
    """What the server needs from the rest of the application."""

    registry: DeviceRegistry
    auth: Authenticator
    settings: Callable[[], Any]
    output_rate: int = 48000
    local_addresses: Callable[[], list] = field(default=lambda: [])
    route_info: Callable[[str], dict] = field(default=lambda cid: {})
    backend_queue_ms: Callable[[], float] = field(default=lambda: 0.0)
    ca_der: Callable[[], bytes | None] = field(default=lambda: None)
    https_port: Callable[[], int | None] = field(default=lambda: None)


def _listen_socket(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        if exc.errno == errno.EADDRINUSE:
            raise ServerError(f"Port {port} is already in use. Close the other program or choose another port in Settings.") from None
        if exc.errno == errno.EACCES:
            raise ServerError(f"Permission denied binding port {port}. Use a port above 1024.") from None
        if exc.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
            raise
        raise ServerError(f"Cannot listen on {host}:{port}: {exc.strerror}") from None
    sock.listen(64)
    sock.setblocking(False)
    return sock


def bind_sockets(host: str, port: int, ipv6: bool) -> list[socket.socket]:
    hosts = [host]
    if host == "0.0.0.0" and ipv6 and socket.has_ipv6:
        hosts.append("::")
    socks = []
    for h in hosts:
        try:
            socks.append(_listen_socket(h, port))
        except OSError as exc:  # IPv6 unavailable: not fatal when IPv4 works
            if h == "::" and socks:
                log.info("IPv6 listening disabled: %s", exc)
                continue
            for s in socks:
                s.close()
            if isinstance(exc, ServerError):
                raise
            raise ServerError(f"Cannot listen on {h}:{port}: {exc}") from None
    return socks


class WebServer:
    def __init__(self, ctx: ServerContext):
        self.ctx = ctx
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._runner: web.AppRunner | None = None
        self._started = threading.Event()
        self._start_error: BaseException | None = None
        self.http_port: int | None = None
        self.https_port: int | None = None
        self.running = False
        self.ws_connections = 0
        self.total_connections = 0
        self.rejected_connections = 0
        self.listen_hosts: list[str] = []
        self._ws_set: set[web.WebSocketResponse] = set()

    # ------------------------------------------------------------------ app
    def make_app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024)
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/index.html", self.handle_index)
        app.router.add_get("/api/info", self.handle_info)
        app.router.add_get("/ca.crt", self.handle_ca)
        app.router.add_get("/ws", self.handle_ws)
        for name in STATIC_FILES:
            app.router.add_get(f"/{name}", self.handle_static)
        app.middlewares.append(self._security_middleware)
        return app

    @web.middleware
    async def _security_middleware(self, request: web.Request, handler):
        remote = request.remote or ""
        s = self.ctx.settings()
        if not netinfo.is_local_client(remote, self.ctx.local_addresses(), s.allow_public_clients):
            self.rejected_connections += 1
            log.warning("Rejected request from non-local address %s", remote)
            return web.Response(status=403, text="Phone Mic Router only accepts clients on the local network.")
        resp = await handler(request)
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self' ws: wss:; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; worker-src 'self'; frame-ancestors 'none'"
        )
        resp.headers["Permissions-Policy"] = "microphone=(self)"
        return resp

    async def handle_index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "index.html", headers={"Content-Type": "text/html; charset=utf-8"})

    async def handle_static(self, request: web.Request) -> web.StreamResponse:
        name = request.path.lstrip("/")
        path = WEB_DIR / name
        if name not in STATIC_FILES or not path.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": STATIC_FILES[name]})

    async def handle_info(self, request: web.Request) -> web.Response:
        s = self.ctx.settings()
        https_port = self.ctx.https_port()
        return web.json_response({
            "app": APP_NAME,
            "version": __version__,
            "auth_required": self.ctx.auth.enabled,
            "secure": request.secure,
            "https_port": https_port,
            "source_description": s.source_description,
            "sample_rate": self.ctx.output_rate,
        })

    async def handle_ca(self, request: web.Request) -> web.Response:
        der = self.ctx.ca_der()
        if not der:
            raise web.HTTPNotFound(text="HTTPS is disabled")
        return web.Response(
            body=der,
            headers={
                "Content-Type": "application/x-x509-ca-cert",
                "Content-Disposition": 'attachment; filename="phone-mic-router-ca.crt"',
            },
        )

    # ------------------------------------------------------------------ websocket
    async def handle_ws(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(max_msg_size=256 * 1024, heartbeat=None, autoping=True)
        await ws.prepare(request)
        remote = request.remote or "?"
        self.total_connections += 1
        self.ws_connections += 1
        self._ws_set.add(ws)
        sess: ClientSession | None = None
        ping_task = None
        try:
            sess = await self._handshake(ws, request)
            if sess is None:
                return ws
            ping_task = asyncio.create_task(self._ping_loop(ws, sess))
            await self._receive_loop(ws, sess)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("WebSocket handler error for %s", remote)
        finally:
            if ping_task:
                ping_task.cancel()
            self.ws_connections -= 1
            self._ws_set.discard(ws)
            if sess is not None:
                reason = getattr(ws, "_pmr_close_reason", "") or (f"code {ws.close_code}" if ws.close_code else "connection lost")
                self.ctx.registry.unregister(sess, reason)
            if not ws.closed:
                await ws.close()
        return ws

    async def _send_error(self, ws: web.WebSocketResponse, code: str, message: str, close_code: int = 4000) -> None:
        try:
            await ws.send_str(dumps("error", code=code, message=message))
            await ws.close(code=close_code, message=code.encode()[:120])
        except (ConnectionError, RuntimeError):
            pass

    async def _handshake(self, ws: web.WebSocketResponse, request: web.Request) -> ClientSession | None:
        remote = request.remote or "?"
        try:
            msg = await ws.receive(timeout=HELLO_TIMEOUT)
        except asyncio.TimeoutError:
            await self._send_error(ws, "timeout", "No hello received", 4008)
            return None
        if msg.type != WSMsgType.TEXT:
            await self._send_error(ws, "expected_hello", "First message must be a JSON hello", 4002)
            return None
        try:
            hello = validate_hello(parse_text(msg.data))
        except ProtocolError as exc:
            log.warning("Bad hello from %s: %s", remote, exc)
            await self._send_error(ws, exc.code, str(exc), 4002)
            return None
        ok, reason = self.ctx.auth.check(remote, hello.client_id, hello.pairing_code, hello.token)
        if not ok:
            log.warning("Pairing failed for %s (%s)", remote, reason)
            text = {
                "rate_limited": "Too many wrong pairing codes. Wait a minute and try again.",
            }.get(reason, "Wrong or missing pairing code. Enter the code shown in the Phone Mic Router window.")
            await self._send_error(ws, "auth_failed" if reason != "rate_limited" else "rate_limited", text, 4001)
            return None

        ua = hello.user_agent or request.headers.get("User-Agent", "")
        platform = hello.platform or platform_name(ua)
        browser = hello.browser or browser_name(ua)
        if netinfo.is_loopback(remote):
            connection = "USB (ADB)" if platform == "Android" else "Local"
        else:
            connection = "Wi-Fi/LAN"
        name = friendly_name(hello.device_name, ua, platform, browser)
        sess = self.ctx.registry.register(
            hello.client_id, remote, name,
            browser=browser, platform=platform, user_agent=ua,
            connection=connection, secure=request.secure,
        )
        loop = asyncio.get_running_loop()

        def closer(reason: str, _ws=ws) -> None:
            async def _close():
                _ws._pmr_close_reason = reason  # type: ignore[attr-defined]
                try:
                    await _ws.send_str(dumps("kick", reason=reason))
                except (ConnectionError, RuntimeError):
                    pass
                await _ws.close(code=4003 if reason != "replaced" else 4004, message=reason.encode()[:120])
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_close()))

        sess.closer = closer
        token = self.ctx.auth.issue_token(sess.client_id)
        s = self.ctx.settings()
        await ws.send_str(dumps(
            "welcome",
            session_id=sess.session_id,
            client_id=sess.client_id,
            token=token,
            name=self.ctx.registry.display_name(sess),
            server_rate=self.ctx.output_rate,
            buffer_ms=s.buffer_ms,
            source_description=s.source_description,
            server_version=__version__,
        ))
        return sess

    async def _receive_loop(self, ws: web.WebSocketResponse, sess: ClientSession) -> None:
        malformed = 0
        registry = self.ctx.registry
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                if sess.audio_config is None:
                    malformed += 1
                    sess.malformed_packets += 1
                else:
                    try:
                        pkt = parse_audio(msg.data, sess.audio_config.channels)
                    except ProtocolError as exc:
                        malformed += 1
                        sess.malformed_packets += 1
                        if malformed <= 3:
                            log.warning("Malformed audio frame from %s: %s", registry.display_name(sess), exc)
                    else:
                        sess.push_packet(pkt)
                if malformed > MAX_MALFORMED:
                    await self._send_error(ws, "malformed", "Too many malformed packets", 4002)
                    ws._pmr_close_reason = "too many malformed packets"  # type: ignore[attr-defined]
                    return
            elif msg.type == WSMsgType.TEXT:
                try:
                    data = parse_text(msg.data)
                except ProtocolError as exc:
                    malformed += 1
                    log.debug("Bad control message: %s", exc)
                    continue
                await self._handle_control(ws, sess, data)
            elif msg.type == WSMsgType.ERROR:
                log.info("WebSocket error from %s: %s", registry.display_name(sess), ws.exception())
                return

    async def _handle_control(self, ws: web.WebSocketResponse, sess: ClientSession, data: dict) -> None:
        t = data["type"]
        registry = self.ctx.registry
        if t == "audio_config":
            try:
                cfg = validate_audio_config(data)
            except ProtocolError as exc:
                await ws.send_str(dumps("error", code=exc.code, message=str(exc)))
                return
            registry.configure_audio(sess, cfg, self.ctx.output_rate, self.ctx.settings().buffer_ms)
            sess.state = WAITING
            await ws.send_str(dumps("audio_config_ack", sample_rate=cfg.sample_rate, server_rate=self.ctx.output_rate,
                                    resampling=cfg.sample_rate != self.ctx.output_rate))
        elif t == "pong":
            sent = data.get("t")
            if isinstance(sent, (int, float)):
                rtt = time.monotonic() * 1000.0 - sent
                if 0 <= rtt < 10000:
                    sess.rtt_ms = rtt if sess.rtt_ms is None else sess.rtt_ms * 0.8 + rtt * 0.2
            buf = data.get("buffer_ms")
            if isinstance(buf, (int, float)) and 0 <= buf < 2000:
                sess.client_buffer_ms = float(buf)
        elif t == "stop":
            sess.state = PAUSED
            sess.queue.clear()
            sess.pending_flush = True
            log.info("Device %s stopped its microphone", registry.display_name(sess))
            registry.on_change()
        elif t == "start":
            sess.state = WAITING
            registry.on_change()
        elif t == "client_error":
            log.warning("Browser on %s reported: %s", registry.display_name(sess), str(data.get("message", ""))[:300])
        else:
            log.debug("Ignoring unknown control message type %r", t)

    async def _ping_loop(self, ws: web.WebSocketResponse, sess: ClientSession) -> None:
        while not ws.closed:
            await asyncio.sleep(PING_INTERVAL)
            route = self.ctx.route_info(sess.client_id)
            lat = estimate_latency_ms(sess, self.ctx.backend_queue_ms())
            try:
                await ws.send_str(dumps(
                    "ping",
                    t=time.monotonic() * 1000.0,
                    state=sess.state,
                    routed=route.get("routed", False),
                    target=route.get("target", ""),
                    muted=route.get("muted", False),
                    level_db=round(sess.meter.rms_db, 1),
                    latency_ms=None if lat is None else round(lat),
                    lost=sess.lost_packets,
                ))
            except (ConnectionError, RuntimeError):
                return

    # ------------------------------------------------------------------ lifecycle
    def start(self, host: str, port: int, https_port: int | None, ssl_ctx: ssl.SSLContext | None, ipv6: bool = True) -> None:
        if self.running:
            return
        # Bind first, in this thread, so port errors are reported synchronously.
        http_socks = bind_sockets(host, port, ipv6)
        https_socks: list[socket.socket] = []
        if https_port and ssl_ctx is not None:
            try:
                https_socks = bind_sockets(host, https_port, ipv6)
            except ServerError as exc:
                log.error("HTTPS disabled: %s", exc)
                https_port = None
        self._started.clear()
        self._start_error = None
        self._thread = threading.Thread(
            target=self._thread_main, args=(http_socks, https_socks, ssl_ctx), name="web-server", daemon=True
        )
        self._thread.start()
        if not self._started.wait(10.0):
            raise ServerError("Web server did not start in time")
        if self._start_error:
            raise ServerError(f"Web server failed to start: {self._start_error}")
        self.http_port = port
        self.https_port = https_port if https_socks else None
        self.listen_hosts = [s.getsockname()[0] for s in http_socks]
        self.running = True
        log.info("HTTP server listening on %s port %d", ", ".join(self.listen_hosts), port)
        if self.https_port:
            log.info("HTTPS server listening on port %d", self.https_port)

    def _thread_main(self, http_socks, https_socks, ssl_ctx) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_start(http_socks, https_socks, ssl_ctx))
        except BaseException as exc:  # pragma: no cover
            self._start_error = exc
            self._started.set()
            return
        self._started.set()
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(self._async_cleanup())
            loop.close()

    async def _async_start(self, http_socks, https_socks, ssl_ctx) -> None:
        self._runner = web.AppRunner(self.make_app(), access_log=None, handle_signals=False, shutdown_timeout=1.0)
        await self._runner.setup()
        for sock in http_socks:
            await web.SockSite(self._runner, sock).start()
        for sock in https_socks:
            await web.SockSite(self._runner, sock, ssl_context=ssl_ctx).start()

    async def _async_cleanup(self) -> None:
        for ws in list(self._ws_set):
            try:
                await ws.send_str(dumps("kick", reason="server_shutdown"))
                await ws.close(code=1001, message=b"server shutting down")
            except (ConnectionError, RuntimeError):
                pass
        if self._runner is not None:
            await self._runner.cleanup()

    def stop(self) -> None:
        if not self.running or self.loop is None:
            return
        self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread:
            self._thread.join(timeout=5.0)
        self.running = False
        log.info("Web server stopped")
