#!/usr/bin/env python3
"""Drive a real headless Chromium against the web client (fake microphone).

Proves the browser side works end to end: page load, secure-context checks,
getUserMedia, AudioWorklet capture, the WebSocket protocol, pairing, routing
and audio arriving in the PipeWire virtual microphone.

Requires: chromium (or google-chrome) and a running PipeWire session.
Usage: python tools/browser_e2e_check.py [--browser chromium]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import numpy as np
from aiohttp import ClientSession

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.server import netinfo  # noqa: E402

PORT, HTTPS_PORT, DEVTOOLS = 18865, 18866, 19333
NAME = "pmr-browser-test"


class Page:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0

    async def eval(self, expr: str, gesture: bool = False):
        self.n += 1
        mid = self.n
        await self.ws.send_str(json.dumps({"id": mid, "method": "Runtime.evaluate", "params": {
            "expression": expr, "awaitPromise": True, "returnByValue": True, "userGesture": gesture}}))
        while True:
            msg = json.loads((await self.ws.receive(timeout=20)).data)
            if msg.get("id") == mid:
                res = msg.get("result", {})
                if "exceptionDetails" in res:
                    raise RuntimeError(res["exceptionDetails"])
                return res.get("result", {}).get("value")

    async def goto(self, url: str):
        self.n += 1
        await self.ws.send_str(json.dumps({"id": self.n, "method": "Page.navigate", "params": {"url": url}}))
        await asyncio.sleep(1.5)


def record(seconds: float, path: Path) -> np.ndarray:
    rec = subprocess.Popen(["pw-record", "--target", NAME, "--rate", "48000", "--channels", "1", "--format", "s16", "-a", str(path)])
    time.sleep(seconds)
    rec.send_signal(signal.SIGINT)
    rec.wait(5)
    return np.frombuffer(path.read_bytes(), "<i2").astype(np.float64) / 32768


async def scenario(page: Page, url: str, expect_secure: bool, tmp: Path, label: str) -> bool:
    print(f"\n--- {label}: {url}")
    await page.goto(url)
    secure = await page.eval("window.isSecureContext")
    warn_visible = await page.eval("!document.getElementById('secureWarning').hidden")
    print(f"isSecureContext={secure} insecure-warning-visible={warn_visible}")
    if not expect_secure:
        ok = (secure is False) and warn_visible
        btn_disabled = await page.eval("document.getElementById('startBtn').disabled")
        print(f"start button disabled={btn_disabled}")
        return ok and btn_disabled
    await page.eval("document.getElementById('startBtn').click()", gesture=True)
    status = ""
    for _ in range(40):
        await asyncio.sleep(0.25)
        status = await page.eval("document.getElementById('statusText').textContent")
        if status == "Streaming":
            break
    detail = await page.eval("document.getElementById('statusDetail').textContent")
    rate = await page.eval("document.getElementById('rateText').textContent")
    print(f"status={status!r} detail={detail!r} rate={rate!r}")
    if status != "Streaming":
        return False
    await asyncio.sleep(2.5)
    target = await page.eval("document.getElementById('targetText').textContent")
    latency = await page.eval("document.getElementById('latencyText').textContent")
    level = await page.eval("document.getElementById('levelText').textContent")
    print(f"page: target={target!r} latency={latency!r} level={level!r}")
    audio = record(3.0, tmp / f"{label}.raw")
    rms = 20 * np.log10(np.sqrt(np.mean(audio[4800:] ** 2)) + 1e-12)
    print(f"virtual microphone RMS over 3 s: {rms:.1f} dBFS ({audio.size} samples)")
    await page.eval("document.getElementById('startBtn').click()", gesture=True)  # stop
    await asyncio.sleep(0.5)
    return rms > -60 and target.startswith("PMR Browser Test")


async def main_async(args) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pmr-browser-"))
    env = dict(os.environ, PHONE_MIC_ROUTER_CONFIG_DIR=str(tmp / "cfg"), PHONE_MIC_ROUTER_STATE_DIR=str(tmp / "state"))
    def start_app():
        proc = subprocess.Popen([sys.executable, "-m", "app", "--no-gui", "--no-adb", "--port", str(PORT), "--https-port",
                                 str(HTTPS_PORT), "--source-name", NAME, "--source-description", "PMR Browser Test"],
                                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/info", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        return proc

    app = start_app()
    browser = None
    results = {}
    try:
        code = json.loads((tmp / "cfg" / "config.json").read_text())["pairing_code"]
        lan = [a.address for a in netinfo.usable_lan_addresses(netinfo.local_addresses(False)) if a.kind in ("lan", "wifi")]
        browser = subprocess.Popen([
            args.browser, "--headless=new", f"--remote-debugging-port={DEVTOOLS}", f"--user-data-dir={tmp / 'profile'}",
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--ignore-certificate-errors",
            "--autoplay-policy=no-user-gesture-required", "--no-first-run", "--no-default-browser-check", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        async with ClientSession() as http:
            for _ in range(100):
                try:
                    targets = await (await http.get(f"http://127.0.0.1:{DEVTOOLS}/json")).json()
                    pages = [t for t in targets if t["type"] == "page"]
                    if pages:
                        break
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(0.1)
            async with http.ws_connect(pages[0]["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                page = Page(ws)
                results["localhost (USB/ADB path)"] = await scenario(
                    page, f"http://127.0.0.1:{PORT}/?code={code}", True, tmp, "localhost")
                if lan:
                    results["LAN HTTPS"] = await scenario(
                        page, f"https://{lan[0]}:{HTTPS_PORT}/?code={code}", True, tmp, "lan-https")
                    results["LAN plain HTTP is blocked + explained"] = await scenario(
                        page, f"http://{lan[0]}:{PORT}/", False, tmp, "lan-http")
                # automatic reconnect after the server restarts
                print("\n--- reconnect after server restart")
                await page.goto(f"http://127.0.0.1:{PORT}/?code={code}")
                await page.eval("document.getElementById('startBtn').click()", gesture=True)
                await asyncio.sleep(2)
                app.send_signal(signal.SIGINT)
                app.communicate(timeout=10)
                await asyncio.sleep(0.5)
                during = await page.eval("document.getElementById('statusText').textContent")
                app = start_app()
                status = ""
                for _ in range(40):
                    await asyncio.sleep(0.25)
                    status = await page.eval("document.getElementById('statusText').textContent")
                    if status == "Streaming":
                        break
                print(f"while server down: {during!r}; after restart: {status!r}")
                audio = record(2.0, tmp / "reconnect.raw")
                rms = 20 * np.log10(np.sqrt(np.mean(audio[4800:] ** 2)) + 1e-12)
                print(f"virtual microphone RMS after reconnect: {rms:.1f} dBFS")
                results["auto reconnect after server restart"] = during == "Reconnecting" and status == "Streaming" and rms > -60
                await page.eval("document.getElementById('startBtn').click()", gesture=True)

                # wrong pairing code must be refused
                await page.goto(f"http://127.0.0.1:{PORT}/?code=000000")
                await page.eval("localStorage.removeItem('pmr.token')")
                await page.eval("document.getElementById('startBtn').click()", gesture=True)
                await asyncio.sleep(2.5)
                st = await page.eval("document.getElementById('statusDetail').textContent")
                print(f"\n--- wrong code: {st!r}")
                results["wrong pairing code rejected"] = "pairing code" in st.lower()
    finally:
        if browser:
            browser.terminate()
            browser.wait(5)
        app.send_signal(signal.SIGINT)
        try:
            out, _ = app.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            app.kill()
            out, _ = app.communicate()
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nRESULTS")
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    ok = bool(results) and all(results.values())
    if not ok:
        print(out[-4000:])
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--browser", default=shutil.which("chromium") or shutil.which("google-chrome") or "chromium")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
