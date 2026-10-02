#!/usr/bin/env python3
"""Synthetic test client: streams a sine wave to Phone Mic Router like a phone would.

Examples:
    python tools/sine_client.py --code 123456
    python tools/sine_client.py --url wss://127.0.0.1:8766/ws --insecure --rate 44100 --freq 1000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import ssl
import sys
import time
import uuid
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402
from aiohttp import ClientSession, WSMsgType  # noqa: E402

from app.server.protocol import pack_audio  # noqa: E402


async def run(args) -> int:
    ssl_ctx = None
    if args.url.startswith("wss://"):
        ssl_ctx = ssl.create_default_context()
        if args.insecure:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
    frame = int(args.rate * args.frame_ms / 1000)
    phase = 0.0
    step = 2 * math.pi * args.freq / args.rate
    async with ClientSession() as http:
        async with http.ws_connect(args.url, ssl=ssl_ctx, heartbeat=None) as ws:
            await ws.send_str(json.dumps({
                "type": "hello", "protocol": 1, "client_id": args.client_id,
                "device_name": args.name, "browser": "sine_client", "platform": "Test",
                "user_agent": "sine_client/1.0", "pairing_code": args.code or "",
            }))
            msg = await ws.receive(timeout=5)
            data = json.loads(msg.data) if msg.type == WSMsgType.TEXT else {}
            if data.get("type") != "welcome":
                print(f"Server refused: {data}", file=sys.stderr)
                return 1
            print(f"Connected as {data['name']} (session {data['session_id']})")
            await ws.send_str(json.dumps({"type": "audio_config", "sample_rate": args.rate, "channels": 1,
                                          "format": "s16le", "frame_ms": args.frame_ms}))

            async def reader():
                async for m in ws:
                    if m.type == WSMsgType.TEXT:
                        d = json.loads(m.data)
                        if d["type"] == "ping":
                            await ws.send_str(json.dumps({"type": "pong", "t": d["t"], "buffer_ms": args.frame_ms}))
                            if args.verbose:
                                print(f"server: state={d.get('state')} routed={d.get('routed')} level={d.get('level_db')} dBFS latency~{d.get('latency_ms')} ms")
                        elif d["type"] in ("kick", "error"):
                            print(f"server: {d}")
                            return

            rtask = asyncio.create_task(reader())
            seq = 0
            start = time.monotonic()
            amp = 10 ** (args.level / 20)
            while not ws.closed and (args.duration <= 0 or time.monotonic() - start < args.duration):
                idx = np.arange(frame)
                samples = amp * np.sin(phase + step * idx)
                phase = (phase + step * frame) % (2 * math.pi)
                pcm = (samples * 32767).astype("<i2").tobytes()
                if not (args.drop_every and seq and seq % args.drop_every == 0):
                    await ws.send_bytes(pack_audio(seq, time.time() * 1000, pcm))
                seq += 1
                # pace in real time on an absolute clock (with optional jitter)
                target = start + seq * args.frame_ms / 1000
                delay = target - time.monotonic()
                if args.jitter_ms:
                    delay += (np.random.rand() - 0.5) * args.jitter_ms / 1000
                if delay > 0:
                    await asyncio.sleep(delay)
            rtask.cancel()
            print(f"Sent {seq} frames ({seq * args.frame_ms / 1000:.1f} s)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    p.add_argument("--code", default="", help="pairing code (digits)")
    p.add_argument("--rate", type=int, default=48000)
    p.add_argument("--freq", type=float, default=440.0)
    p.add_argument("--level", type=float, default=-12.0, help="sine level in dBFS")
    p.add_argument("--frame-ms", type=float, default=10.0)
    p.add_argument("--duration", type=float, default=0, help="seconds, 0 = forever")
    p.add_argument("--name", default="Sine generator")
    p.add_argument("--client-id", default="sine-" + uuid.uuid4().hex[:8])
    p.add_argument("--jitter-ms", type=float, default=0.0, help="random send jitter to exercise the jitter buffer")
    p.add_argument("--drop-every", type=int, default=0, help="skip every Nth frame to simulate packet loss")
    p.add_argument("--insecure", action="store_true", help="accept the self-signed certificate")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    except (OSError, aiohttp.ClientError) as exc:
        print(f"Connection ended: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
