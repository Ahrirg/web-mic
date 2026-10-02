#!/usr/bin/env python3
"""End-to-end check against the real sound server, no phone required.

1. Starts Phone Mic Router headless (temporary config, custom ports/source name).
2. Streams a sine wave with tools/sine_client.py (optionally at 44.1 kHz with jitter).
3. Records the virtual microphone with pw-record (or parec).
4. Verifies the dominant frequency, level and counts discontinuities (clicks/dropouts).
5. Stops the app and verifies the virtual source is gone.

Usage: python tools/e2e_pipewire_check.py [--rate 44100] [--jitter 8] [--seconds 6] [--kill]
--kill sends SIGKILL instead of SIGINT to prove no orphaned nodes are left after a crash.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def source_exists(name: str) -> bool:
    out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True).stdout
    return any(line.split("\t")[1] == name for line in out.splitlines() if "\t" in line)


def wait_for(pred, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


def analyze(path: Path, freq: float, skip_s: float = 0.5) -> dict:
    a = np.frombuffer(path.read_bytes(), "<i2").astype(np.float64) / 32768.0
    a = a[int(48000 * skip_s):]
    if a.size < 48000:
        return {"ok": False, "reason": f"recording too short ({a.size} samples)"}
    win = np.hanning(a.size)
    spec = np.abs(np.fft.rfft(a * win))
    peak_hz = np.argmax(spec) * 48000 / a.size
    rms_db = 20 * np.log10(np.sqrt(np.mean(a ** 2)) + 1e-12)
    # A pure sine's second difference is bounded by A*(2*pi*f/fs)^2. Clicks and
    # dropouts produce spikes far above that bound.
    amp = np.sqrt(2) * np.sqrt(np.mean(a ** 2))
    bound = amp * (2 * np.pi * freq / 48000) ** 2
    d2 = np.abs(np.diff(a, 2))
    spikes = np.flatnonzero(d2 > max(bound * 8, 0.01))
    # group spikes closer than 5 ms into one event
    events = 0
    last = -10**9
    for i in spikes:
        if i - last > 240:
            events += 1
        last = i
    return {"ok": True, "peak_hz": round(peak_hz, 1), "rms_db": round(rms_db, 1), "glitches": events,
            "seconds": round(a.size / 48000, 2)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rate", type=int, default=48000)
    ap.add_argument("--freq", type=float, default=440.0)
    ap.add_argument("--jitter", type=float, default=0.0, help="send jitter in ms")
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--buffer", type=int, default=40)
    ap.add_argument("--kill", action="store_true", help="SIGKILL the app at the end (crash test)")
    ap.add_argument("--port", type=int, default=18765)
    ap.add_argument("--backend", default="auto", choices=["auto", "pipewire", "pulse"])
    args = ap.parse_args()

    if not shutil.which("pactl"):
        print("pactl not found: a PipeWire/PulseAudio session is required")
        return 2
    name = "pmr-e2e-test"
    tmp = Path(tempfile.mkdtemp(prefix="pmr-e2e-"))
    env = dict(os.environ, PHONE_MIC_ROUTER_CONFIG_DIR=str(tmp / "cfg"), PHONE_MIC_ROUTER_STATE_DIR=str(tmp / "state"),
               XDG_RUNTIME_DIR=str(tmp / "run"))
    (tmp / "run").mkdir(mode=0o700)
    # pw-* tools need the real runtime dir to find the PipeWire socket
    env["PIPEWIRE_RUNTIME_DIR"] = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env["PULSE_RUNTIME_PATH"] = os.path.join(env["PIPEWIRE_RUNTIME_DIR"], "pulse")
    app = subprocess.Popen(
        [PY, "-m", "app", "--no-gui", "--no-auth", "--no-adb", "--no-https", "--port", str(args.port),
         "--source-name", name, "--source-description", "PMR E2E Test", "--buffer", str(args.buffer),
         "--backend", args.backend],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    result = 1
    try:
        if not wait_for(lambda: source_exists(name), 10):
            print("FAIL: virtual source did not appear")
            return 1
        print(f"virtual source '{name}' present")

        def http_up() -> bool:
            import urllib.request
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/api/info", timeout=1) as r:
                    return r.status == 200
            except OSError:
                return False

        if not wait_for(http_up, 10):
            print("FAIL: HTTP server did not come up")
            return 1
        client = subprocess.Popen(
            [PY, str(ROOT / "tools/sine_client.py"), "--url", f"ws://127.0.0.1:{args.port}/ws",
             "--rate", str(args.rate), "--freq", str(args.freq), "--duration", str(args.seconds + 2),
             "--jitter-ms", str(args.jitter)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(1.0)
        rec = tmp / "rec.raw"
        if shutil.which("pw-record"):
            cmd = ["pw-record", "--target", name, "--rate", "48000", "--channels", "1", "--format", "s16", "-a", str(rec)]
        else:
            cmd = ["parec", "-d", name, "--rate=48000", "--channels=1", "--format=s16le", "--raw", str(rec)]
        recorder = subprocess.Popen(cmd)
        time.sleep(args.seconds)
        recorder.send_signal(signal.SIGINT)
        recorder.wait(5)
        client_out, _ = client.communicate(timeout=15)
        if client.returncode:
            print("sine client failed:\n" + client_out[-2000:])
        res = analyze(rec, args.freq)
        print("analysis:", res)
        ok = res.get("ok") and abs(res["peak_hz"] - args.freq) < 5 and res["rms_db"] > -30 and res["glitches"] <= 1
        print("audio check:", "PASS" if ok else "FAIL")
        result = 0 if ok else 1
    finally:
        if args.kill:
            app.kill()
        else:
            app.send_signal(signal.SIGINT)
        try:
            out, _ = app.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            app.kill()
            out, _ = app.communicate()
            print("FAIL: app did not stop within 10 s")
            result = 1
        gone = wait_for(lambda: not source_exists(name), 5)
        print(f"after {'SIGKILL' if args.kill else 'SIGINT'}: virtual source {'removed' if gone else 'STILL PRESENT'}")
        if not gone:
            result = 1
        if result and out:
            print(out[-3000:])
        shutil.rmtree(tmp, ignore_errors=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
