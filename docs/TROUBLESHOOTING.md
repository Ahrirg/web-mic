# Troubleshooting

First open **Diagnostics** in the app. The status lights usually point to the problem.
**Copy diagnostic information** produces a report without audio or the pairing code.
The detailed log is in `~/.local/state/phone-mic-router/log/phone-mic-router.log`. Run with `--debug` for more detail.

## The phone page says "Microphone blocked on this address"

You opened `http://<LAN-IP>:8765`. Browsers only allow microphones on `https://` pages and on `localhost`.
Use the `https://<LAN-IP>:8766` address. The page links to it, and the QR code already uses it. Or connect over USB.

## Certificate warning on the phone

The HTTPS certificate is created by your computer, so the phone does not know it yet.

* **Chrome:** tap *Advanced → Proceed to 192.168.x.y (unsafe)*.
* **Firefox:** tap *Advanced → Accept the Risk and Continue*.

To avoid the warning entirely, install the CA certificate from `https://<LAN-IP>:8766/ca.crt` as a CA certificate in Android's security settings. Check that the fingerprint matches the one in **Network → HTTPS certificate**.
The certificate is regenerated automatically when your PC's IP addresses change. The CA stays the same, so an installed CA keeps working.

## "Microphone permission denied"

The browser remembered a denial. Tap the lock icon next to the address, then *Permissions → Microphone → Allow*, and reload.
Also check *Android Settings → Apps → Chrome → Permissions → Microphone*.

## "The microphone is in use by another app"

A phone call, voice recorder or another browser tab is using the microphone. Close it and press Start again.

## The phone cannot reach the computer over Wi-Fi

* The phone must be on the **same network**. Guest Wi-Fi and "AP/client isolation" block device-to-device traffic.
* Check the computer's firewall and allow TCP ports 8765 and 8766:
  * firewalld: `sudo firewall-cmd --add-port=8765-8766/tcp --permanent && sudo firewall-cmd --reload`
  * ufw: `sudo ufw allow 8765:8766/tcp`
* Use an address from **Network** that is on the same subnet as the phone. The app lists every interface.
* VPN-only addresses such as Tailscale are rejected unless *Accept clients outside private networks* is enabled.

## "Port 8765 is already in use"

Another program, or another copy of Phone Mic Router (check the tray), is using the port. Change the ports in **Settings** or close the other program:

```bash
ss -ltnp | grep 8765
```

## "PipeWire not detected" / no virtual microphone

* Check that PipeWire and its PulseAudio layer are running:

  ```bash
  systemctl --user status pipewire pipewire-pulse wireplumber
  pactl info | grep "Server Name"     # should mention PipeWire
  ```

* The PipeWire tools must be installed. You need `pw-cat`, `pw-loopback` and `pw-dump`, which come from `pipewire-bin` (Debian/Ubuntu), `pipewire-utils` (Fedora) or `pipewire` (Arch).
* On plain PulseAudio, the app uses `module-pipe-source` automatically. You need `pactl` from `pulseaudio-utils` / `libpulse`.
* "An audio node named 'phone-mic' already exists": another instance or the systemd service is running. Stop it, or choose another node name under **Audio → Virtual microphone**.

## The virtual microphone is silent

* **Dashboard → Route** must show your phone as the input. In *One active device* mode only one phone is routed; select it on the **Devices** tab with *Use as active microphone*.
* Check that nothing is muted: the master mute on the Dashboard, the device mute or *Disable* on Devices, or *Mute* on the phone page.
* In pavucontrol, under *Input Devices*, make sure *Phone Microphone* is not muted and its volume is up.
* Test from a terminal:

  ```bash
  pw-record --target phone-mic /tmp/test.wav     # speak, Ctrl+C, then play it back
  ```

## Phone Microphone does not show up in an application

* Applications list devices when they start. Restart the application or reopen its settings.
* Flatpak apps such as Discord or OBS need audio access. They usually have it via the PulseAudio socket.
* Some games only use the default microphone. Make it the default: `pactl set-default-source phone-mic`.
* Do not select **Phone Microphone (internal feed)**. That is the helper *sink* that feeds the source.

## Crackling, dropouts or robotic sound

* Increase the jitter buffer under **Audio** (80 ms is very robust on busy Wi-Fi).
* Use 5 GHz Wi-Fi, or USB.
* On the **Devices** tab, look at *lost* and *underruns* in the selected device's statistics. Lost packets point to network problems; underruns mean the buffer is too small.
* Disable battery saver on the phone and keep the page in the foreground.

## Latency is too high

* Use USB, a smaller buffer (10–20 ms on a good connection) and close other apps that use the microphone.
* The displayed value is approximate and excludes the phone's internal input latency, typically 10–40 ms.

## Audio stops when the phone screen turns off

Android may suspend microphone access for background or locked pages. Keep the page open and in the foreground.
The page requests a screen wake lock while streaming; check that the option is enabled in its settings. Some battery savers override it.

## USB / ADB problems

| Status | Fix |
|---|---|
| `adb executable not found` | Install `android-tools` (Arch/Fedora) or `adb` (Debian/Ubuntu). |
| *No Android devices detected* | Enable *USB debugging*, use a data-capable cable and set the USB mode to *File transfer* if devices still don't appear. |
| `unauthorized` | Unlock the phone and accept *Allow USB debugging?* (tick *Always allow*). If no prompt appears, revoke USB debugging authorizations in Developer options and reconnect. |
| `no permissions` | Your user may not access the USB device. Install udev rules: `android-udev` (Arch), `android-sdk-platform-tools-common` (Debian/Ubuntu). Then replug the phone. |
| `offline` | Replug the cable or run `adb kill-server && adb start-server`. |
| *does not support adb reverse* | Needs Android 5.0+ and a current `adb`. Use Wi-Fi with HTTPS instead. |

After enabling, the phone must open **http://127.0.0.1:8765** (not the LAN IP). Check from the computer:

```bash
adb reverse --list        # should show tcp:8765 tcp:8765
```

## Leftover "Phone Microphone" after a crash

With the PipeWire backend this cannot happen: the helper processes die with the app and PipeWire removes their nodes.
With the PulseAudio fallback, the next start removes the leftover module. To remove it by hand:

```bash
pactl list short modules | grep phone-mic
pactl unload-module <index>
```

## GUI does not start

* `qt.qpa.plugin: Could not load the Qt platform plugin "xcb"`: install `libxcb-cursor0` (Debian/Ubuntu) or `xcb-util-cursor` (Arch/Fedora). On Wayland you can also try `QT_QPA_PLATFORM=wayland`.
* Headless machines: use `phone-mic-router --no-gui` or the systemd user service.
* "Already running": look for the tray icon, or check for a stale process with `pgrep -af phone-mic-router`.
