# Phone Mic Router

Use an Android phone, or any device with a modern web browser, as a microphone on Linux.
The phone opens a web page served by this application and taps **Start Microphone**.
Its audio then appears as a virtual microphone named **Phone Microphone**.
Discord, OBS, games, browsers and pavucontrol can select it like any other microphone.

* No Android app, nothing to compile or install on the phone, no root.
* Works over **USB** through `adb reverse` and over **Wi-Fi/LAN** through local HTTPS.
* Several phones at once: pick one active phone, or mix them with per-phone gain.
* Everything stays on your network. There is no cloud service, account, relay or telemetry.

```
 Android phone (Chrome/Firefox)                      Linux PC: Phone Mic Router
┌──────────────────────────────┐  WebSocket   ┌──────────────────────────────────────────────┐
│ getUserMedia → AudioWorklet  │  16-bit PCM  │ HTTP/HTTPS + WebSocket server (aiohttp)       │
│ 48 kHz mono s16 frames, 10 ms├─────────────▶│   → bounded packet queue per phone            │
└──────────────────────────────┘  USB (adb)   │   → jitter buffer + resampler + drift control │
                                  or Wi-Fi    │   → gain / mute → mixer → soft limiter        │
                                              │   → PipeWire virtual SOURCE "Phone Microphone"│
                                              │ Qt GUI · ADB manager · config · diagnostics   │
                                              └───────────────────────┬──────────────────────┘
                                                                      ▼
                                                     Discord · OBS · games · any app
```

## Contents

1. [Install](#install)
2. [Quick start](#quick-start)
3. [Connecting over USB](#connecting-over-usb-adb)
4. [Connecting over Wi-Fi](#connecting-over-wi-fi--lan-https)
5. [Using the microphone in applications](#using-the-microphone-in-applications)
6. [The GUI](#the-gui)
7. [Command line](#command-line)
8. [How the audio path works](#how-the-audio-path-works)
9. [Security](#security)
10. [Files and settings](#files-and-settings)
11. [Running as a systemd user service](#running-as-a-systemd-user-service)
12. [Development and tests](#development-and-tests)
13. [Known limitations](#known-limitations)

See also [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) and [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Install

### 1. System packages

You need Python 3.10 or newer, PipeWire with its PulseAudio compatibility layer and command-line tools, and optionally `adb` for USB.

| Distribution | Command |
|---|---|
| Arch / Manjaro | `sudo pacman -S python pipewire pipewire-pulse wireplumber libpulse android-tools` |
| Debian 12+ / Ubuntu 22.10+ | `sudo apt install python3 python3-venv python3-pip pipewire pipewire-pulse pipewire-bin wireplumber pulseaudio-utils adb` |
| Fedora | `sudo dnf install python3 pipewire pipewire-pulseaudio pipewire-utils wireplumber pulseaudio-utils android-tools` |
| openSUSE | `sudo zypper install python3 pipewire pipewire-pulseaudio pipewire-tools wireplumber pulseaudio-utils android-tools` |

The application uses these tools:

* `pw-loopback` and `pw-cat` create and feed the virtual microphone.
* `pw-dump` finds node IDs and the applications that are recording.
* `pactl` lists sources and sinks and provides the PulseAudio fallback.
* `adb` provides USB support.

On X11 the Qt platform plugin may also need `libxcb-cursor0` (Debian/Ubuntu) or `xcb-util-cursor` (Arch/Fedora).

### 2. Application

```bash
git clone <this repository> phone-mic-router   # or copy the folder
cd phone-mic-router
./scripts/install.sh
```

The installer does three things:

* It creates `.venv/` and installs `requirements.txt` there: PySide6, aiohttp, numpy, cryptography and segno. All of them ship as pre-built wheels, so nothing is compiled.
* It adds the launcher `~/.local/bin/phone-mic-router`.
* It adds an application-menu entry.

To install manually instead:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app
```

## Quick start

1. Start **Phone Mic Router** from the application menu, or run `phone-mic-router` in a terminal.
2. The **Dashboard** shows a QR code, the phone address and a pairing code such as `84-29-51`.
3. Connect the phone using one of these:
   * **Wi-Fi:** scan the QR code. It opens `https://<your-PC-IP>:8766/` with the pairing code filled in. Accept the certificate warning once by tapping *Advanced* and then *Proceed*.
   * **USB:** see below. The phone opens `http://127.0.0.1:8765/`.
4. Tap **Start Microphone** and allow microphone access.
5. In Discord, OBS or a game, select **Phone Microphone** as the input device.

The Dashboard shows the live level, latency and which applications are recording from the virtual microphone.

## Connecting over USB (ADB)

USB is the most reliable and lowest-latency path. It needs no certificates, because browsers treat `http://127.0.0.1` as a secure context.

1. On the phone, enable developer options: *Settings → About phone → tap Build number 7 times*.
2. Turn on *Settings → System → Developer options → USB debugging*.
3. Connect the cable and accept the *Allow USB debugging?* prompt on the phone.
4. In Phone Mic Router open **Network → USB / ADB** and click **Enable USB microphone**.
   This runs `adb reverse tcp:8765 tcp:8765`. By default it happens automatically for every authorized phone; see Settings.
5. On the phone open **http://127.0.0.1:8765** in Chrome or Firefox and tap **Start Microphone**.

The app watches `adb devices` every two seconds. It re-applies the forwarding when the phone is unplugged and plugged back in, and removes it when the app exits. Nothing is installed on the phone.
The device list shows `unauthorized`, `offline` and `no permissions` states with an explanation; see Troubleshooting for udev rules.

## Connecting over Wi-Fi / LAN (HTTPS)

**Browsers only allow microphone access on secure pages.** That means `https://` pages, or `http://localhost`.
Opening `http://192.168.x.y:8765` on a phone shows the page, but Chrome, Firefox and Safari block `getUserMedia` there.
The page detects this and offers a link to the secure address.

So the app also serves **HTTPS on port 8766** with a certificate generated on your computer:

* A local certificate authority, *Phone Mic Router Local CA*, is created once in `~/.config/phone-mic-router/tls/`. Its private key is readable only by you.
* A server certificate signed by that CA lists every local IP address, `localhost` and your hostname. It is regenerated automatically when your IP addresses change.

The first time a phone opens the `https://` address, the browser warns that the connection is not private, because it does not know your local CA. Tap **Advanced**, then **Proceed to …**. Chrome and Firefox remember the decision for that address.

To get rid of the warning permanently, install the CA certificate on the phone. This is optional.

1. Open `https://<PC-IP>:8766/ca.crt`. There is also a link on the phone page.
2. Go to *Settings → Security → More security settings → Encryption & credentials → Install a certificate → CA certificate*, and pick the downloaded file. Menu names vary by vendor.
3. Compare the fingerprint with the one in **Network → HTTPS certificate**.

The **Network** tab lists every address with a clear verdict:

| Address | Microphone capture |
|---|---|
| `http://127.0.0.1:8765/` (USB/ADB, or a browser on this PC) | Yes. localhost is a secure context. |
| `https://192.168.1.50:8766/` | Yes, after accepting the certificate once |
| `http://192.168.1.50:8765/` | No. Browsers block the microphone on plain HTTP. |

Addresses are detected dynamically from all interfaces that are up, so there is no assumption about `192.168.1.x`.
IPv6 addresses are listed too. VPN interfaces such as Tailscale are listed only when *Accept clients outside private networks* is enabled.

## Using the microphone in applications

The virtual microphone is a normal PipeWire **source** with:

| Property | Value |
|---|---|
| Display name (`node.description`) | **Phone Microphone** (configurable) |
| Node name (`node.name`) | `phone-mic` (configurable) |
| Format | 48 000 Hz, mono, signed 16-bit |

* **Discord / Vesktop:** *User Settings → Voice & Video → Input Device → Phone Microphone*. Consider disabling Discord's own noise suppression if the phone already does it.
* **OBS Studio:** add an *Audio Input Capture (PulseAudio)* or *(PipeWire)* source and choose *Phone Microphone*.
* **Games:** choose *Phone Microphone* in the game's voice settings. If the game only uses the system default, set it as the default in your desktop's sound settings or with `pactl set-default-source phone-mic`.
* **pavucontrol:** the *Input Devices* tab shows *Phone Microphone*. The *Recording* tab lets you move any application to it.
* **Command line:** `pw-record --target phone-mic test.wav` or `parecord -d phone-mic test.wav`.

Phone Mic Router never changes your default microphone.

### Sink vs. source

A microphone is an audio **source**: applications record from it. A speaker is a **sink**: applications play into it.
The phone's audio comes *into* the PC and is exposed as a **source**, so the UI says "virtual microphone (source)".

Internally, PipeWire offers no command-line tool that creates a free-standing source fed from a pipe. So the PipeWire backend builds the source the documented way, with a loopback:

```
pw-cat (our PCM)  ──▶  phone-mic-input   (Audio/Sink, "Phone Microphone (internal feed)")
                         │ pw-loopback
                         ▼
                       phone-mic          (Audio/Source, "Phone Microphone")  ──▶ applications
```

The helper sink `phone-mic-input` appears in sink lists as *Phone Microphone (internal feed)*. It is plumbing, so do not select it as a speaker.
Both nodes are owned by helper processes that the kernel terminates if Phone Mic Router dies, even with `kill -9`. PipeWire then removes the nodes, so **no orphaned sources are left behind**.
If PipeWire restarts, the app recreates the nodes within a few seconds.

On a system with plain PulseAudio and no PipeWire, the app falls back to `module-pipe-source`. Modules belong to the sound server, so a crash can leave one behind. The next start removes it automatically.

## The GUI

| Tab | What it shows |
|---|---|
| **Dashboard** | Server status, LAN and USB addresses, QR code, pairing code, current microphone with level and latency, the route and a master mute |
| **Devices** | Every phone or browser with IP, connection type, duration, status, format, live level, latency and routing. Per-device controls: rename, gain, mute, route to the virtual microphone, monitor on speakers, use as active, disconnect, disable, remove |
| **Audio** | The routing diagram (inputs → virtual source with node name and ID → recording applications), single or mix mode, active device, jitter buffer, output gain, monitor output, recreating the virtual source, and all PipeWire/PulseAudio sources and sinks |
| **Network** | Every URL with its secure/insecure status, a QR code for the selected URL, the pairing code, HTTPS certificate details and the USB/ADB device list with enable/disable |
| **Settings** | Startup and tray behavior, autostart, ports, HTTPS, IPv6, the network restriction, sample rate, buffer, authentication, virtual source name and device names |
| **Diagnostics** | Status lights for PipeWire, PulseAudio compatibility, ADB, HTTP, HTTPS, WebSocket, the virtual microphone, LAN and the engine; detected tools; the live log; **Copy diagnostic information** |

Networking and audio run in their own threads. The GUI only polls a snapshot about 12 times a second, so it stays responsive while audio streams.
Closing the window keeps the app running in the system tray. This is configurable. Use the tray menu to quit.

### Several phones

* **One active device** (default): the most recently connected phone feeds the virtual microphone, unless you pick a specific *Active device*. Other phones stay connected on standby.
* **Mix all routed devices:** every phone with *Route to virtual microphone* enabled is mixed, each with its own gain, followed by a soft limiter.

## Command line

```
phone-mic-router [--port 8765] [--https-port 8766] [--host 0.0.0.0] [--no-https]
                 [--no-auth] [--backend auto|pipewire|pulse|null] [--buffer 10|20|40|80|120]
                 [--source-name phone-mic] [--source-description "Phone Microphone"]
                 [--no-adb] [--no-gui] [--minimized] [--debug]
                 [--list-devices] [--list-audio-sources] [--version]
```

Examples:

```bash
phone-mic-router                       # GUI (normal mode)
phone-mic-router --no-gui              # headless: prints URLs and the pairing code
phone-mic-router --no-gui --debug      # verbose log on the console
phone-mic-router --port 9000 --https-port 9001
phone-mic-router --host 127.0.0.1      # USB/ADB and this PC only, nothing on the LAN
phone-mic-router --list-devices        # Android devices seen by adb
phone-mic-router --list-audio-sources  # PipeWire/PulseAudio sources and sinks
```

Options given on the command line apply to that run only and are not written to the settings file.

## How the audio path works

```
browser: getUserMedia ─▶ AudioWorklet (Float32 → Int16, 10 ms frames) ─▶ WebSocket binary frames
server:  WebSocket receiver ─▶ bounded packet queue (64 frames, drops oldest)
         ─▶ jitter buffer: resampler to 48 kHz + ring buffer (2 s cap) + clock-drift correction
         ─▶ gain / mute ─▶ mixer ─▶ soft limiter ─▶ 10 ms blocks ─▶ PipeWire virtual source
```

* **Format:** the browser asks for a 48 kHz AudioContext. If the browser cannot provide it, for example Firefox with a 44.1 kHz device, the page uses the native rate and the server resamples. 48 kHz clients are passed through bit-exactly with no transcoding. Compressed formats such as MediaRecorder or Opus are never used.
* **Jitter buffer:** the default is 40 ms; you can choose 10, 20, 40, 80 or 120 ms. On underrun the output fades out and re-buffers instead of clicking. After a network stall, the backlog is discarded down to the target, so latency cannot creep up.
* **Clock drift:** the phone's clock and the sound card's clock differ slightly. The resampling ratio is nudged by at most 0.3% to keep each buffer at its target.
* **Pacing:** with PipeWire, the engine follows the sound card clock by keeping about 20 ms queued in the feed pipe.
* **Latency:** the displayed latency is an *approximate* sum of half the network round trip, the browser frame and buffering, the server queue, the jitter buffer and the backend queue. Phones add 10–40 ms of internal input latency that no browser exposes. Typical totals are 60–100 ms over USB or good Wi-Fi with the default buffer, and less with a 10–20 ms buffer.
* **Bounded memory:** every queue and buffer has a fixed maximum. Stalled clients are detected after 2 s, and high latency is flagged.

## Security

* **Pairing code:** a random 6-digit code, shown in the GUI, is required to stream. The QR code contains it. After pairing, the browser receives a per-device session token so reloads and reconnects work. Generating a new code invalidates all tokens. Ten wrong codes from one address lock it out for a minute. You can disable pairing on trusted networks.
* **Local network only:** connections from public, non-private IP addresses are refused unless you explicitly allow them.
* **HTTPS:** the local CA and certificates are generated on your machine. Private keys are stored with mode 0600.
* Strict Content-Security-Policy headers are sent. The web page loads nothing from the internet.
* **Never stored or logged:** microphone audio and the pairing code are never written to logs or diagnostics. Settings and logs contain no audio.

## Files and settings

| What | Where |
|---|---|
| Settings (JSON) | `~/.config/phone-mic-router/config.json` |
| Local CA and HTTPS certificate | `~/.config/phone-mic-router/tls/` |
| Logs (rotating, 3 × 2 MB) | `~/.local/state/phone-mic-router/log/phone-mic-router.log` |
| Single-instance lock, FIFO for the Pulse backend | `$XDG_RUNTIME_DIR/phone-mic-router/` |
| Autostart entry (if enabled) | `~/.config/autostart/phone-mic-router.desktop` |

Settings include the ports, HTTPS, host, IPv6, virtual source name and description, backend, route mode, active device, buffer size, master gain, pairing settings, per-device aliases, gain, mute and routing, and startup options.

## Running as a systemd user service

For a headless machine, or to keep the microphone available without the GUI:

```bash
mkdir -p ~/.config/systemd/user
cp packaging/phone-mic-router.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now phone-mic-router
journalctl --user -u phone-mic-router     # shows the URLs and the pairing code
```

Only one instance can run at a time, so stop the service before starting the GUI.

## Development and tests

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The unit and integration tests use a mocked audio backend and mocked `adb`/`pactl`/`pw-dump` output. They cover:

* The protocol, malformed frames and sequence gaps.
* Pairing, tokens and rate limiting.
* Client registration, disconnect and reconnect, stalls and latency.
* PCM handling, resampling, the ring buffer, jitter buffer, mixer and meters.
* The audio engine and routing.
* The real aiohttp WebSocket server in-process.
* Configuration persistence, ADB parsing and reverse management.
* PipeWire/PulseAudio detection, netinfo, TLS and the core lifecycle.

Tools that exercise the real system without a phone:

```bash
# Synthetic phone: streams a 440 Hz sine (optionally 44.1 kHz, jitter, packet loss)
python tools/sine_client.py --code 123456
python tools/sine_client.py --url wss://127.0.0.1:8766/ws --insecure --rate 44100 --jitter-ms 15

# Full PipeWire round trip: starts the app, streams a sine, records the virtual mic,
# checks frequency/level/glitches and that the source disappears on exit (or on kill -9)
python tools/e2e_pipewire_check.py
python tools/e2e_pipewire_check.py --rate 44100 --jitter 15 --freq 1000
python tools/e2e_pipewire_check.py --kill
python tools/e2e_pipewire_check.py --backend pulse

# Real browser: headless Chromium with a fake microphone runs the actual web client
# over localhost, LAN HTTPS, LAN HTTP (must be blocked), a server restart (must reconnect)
# and a wrong pairing code (must be refused)
python tools/browser_e2e_check.py
```

Code layout:

```
app/
  main.py            CLI entry point, single-instance lock, headless mode
  core.py            AppCore: owns all subsystems, thread-safe controls, snapshots, diagnostics
  config/            settings dataclasses, JSON persistence, XDG paths
  server/            aiohttp HTTP/WebSocket server, protocol, pairing, TLS, network discovery
  devices/           client registry and sessions, friendly device names
  audio/             ring buffer, resampler, jitter buffer, mixer, meters, engine thread
  audio/backends/    PipeWire (pw-loopback + pw-cat), PulseAudio (module-pipe-source), null, detection
  adb/               adb detection, device monitor, reverse forwarding
  gui/               PySide6 main window and tabs
  web/               browser client: index.html, app.js, worklet.js, style.css
tests/               pytest suite
tools/               sine client and end-to-end checks
packaging/           systemd user unit, .desktop file
```

## Known limitations

* **Wi-Fi needs HTTPS.** This is browser policy, not a bug: plain-HTTP LAN pages cannot use the microphone. Use the `https://` address and accept the certificate once, install the CA, or use USB.
* **Background tabs and locked screens:** Android may pause microphone capture when the screen locks or the browser goes to the background. The page holds a screen wake lock while streaming, which you can disable in its settings. Keep the page in the foreground.
* **Latency is approximate.** Browsers do not expose the phone's input latency.
* **iOS Safari** works over HTTPS after the certificate is trusted, but USB/ADB is Android-only.
* With the PulseAudio fallback backend, the engine paces itself with the system clock. With PipeWire it follows the sound card clock. PipeWire is recommended.
* One virtual microphone is supported. Several phones can be mixed into it, but not routed to separate virtual microphones.
