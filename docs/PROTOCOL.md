# Phone Mic Router WebSocket protocol (version 1)

The browser connects to `ws://HOST:PORT/ws`, or `wss://HOST:HTTPS_PORT/ws` on the HTTPS listener.
Text frames carry JSON control messages. Binary frames carry audio.
The implementation is in `app/server/protocol.py` (server) and `app/web/app.js` (browser).

## Connection sequence

```
client                                   server
  │ ── hello ───────────────────────────▶ │  validate, check pairing code or token
  │ ◀────────────────────────── welcome ─ │  (or error + close 4001/4002)
  │ ── audio_config ────────────────────▶ │  allocate jitter buffer and resampler
  │ ◀──────────────────── audio_config_ack│
  │ ══ binary audio frames (every 10 ms) ▶│
  │ ◀───────────────────────────── ping ──│  every second
  │ ── pong ────────────────────────────▶ │  RTT measurement
  │ ── stop / start ────────────────────▶ │  microphone paused / resumed on the phone
  │ ◀───────────────────────────── kick ──│  server disconnects the client
```

## Client → server

### `hello` (must be the first message, within 10 s)

```json
{
  "type": "hello",
  "protocol": 1,
  "client_id": "2f1c…",          // random id kept in localStorage; identifies the device across reconnects
  "device_name": "Pixel 8 (Chrome)",
  "browser": "Chrome",
  "platform": "Android",
  "user_agent": "Mozilla/5.0 …",
  "pairing_code": "842951",      // digits only; may be empty when a token is sent
  "token": "…"                   // optional session token from a previous welcome
}
```

`client_id` may contain only letters, digits, `-` and `_`, up to 64 characters. Strings are cleaned and truncated.

### `audio_config`

```json
{"type": "audio_config", "sample_rate": 48000, "channels": 1, "format": "s16le", "frame_ms": 10}
```

* `sample_rate` must be one of 8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 88200 or 96000. Anything other than 48000 is resampled on the server.
* `channels` is 1 or 2. Stereo is down-mixed to mono.
* `format` must be `s16le`.

It may be sent again at any time, for example after the user switches microphones. Doing so resets the jitter buffer.

### `pong`

```json
{"type": "pong", "t": 123456.7, "buffer_ms": 12}
```

`t` echoes the server's `ping.t`. `buffer_ms` is the browser's own buffering estimate (AudioContext base latency plus queued socket data) and is used in the latency estimate.

### Other messages

* `{"type":"stop"}`: the user stopped the microphone. The device stays registered and is shown as paused.
* `{"type":"start"}`: the microphone was started again.
* `{"type":"client_error","message":"…"}`: a browser-side problem, written to the server log.

## Server → client

| Message | Fields |
|---|---|
| `welcome` | `session_id`, `client_id`, `token`, `name` (display name/alias), `server_rate`, `buffer_ms`, `source_description`, `server_version` |
| `audio_config_ack` | `sample_rate`, `server_rate`, `resampling` (bool) |
| `ping` | `t` (server monotonic ms, echo it), `state`, `routed` (bool), `target` (virtual microphone name), `muted`, `level_db`, `latency_ms` (approximate), `lost` |
| `error` | `code`, `message` |
| `kick` | `reason`: `replaced` (same device connected again elsewhere), `disconnected by user`, `removed`, `server_shutdown` |

Error codes: `auth_failed`, `rate_limited`, `expected_hello`, `malformed`, `unsupported_version`, `unsupported_format`, `timeout`.

WebSocket close codes: 4001 auth failed, 4002 protocol error, 4003 kicked, 4004 replaced by a newer connection, 4008 hello timeout, 1001 server shutdown.

## Binary audio frames

All fields are little-endian. The header is 16 bytes:

| Offset | Size | Type | Field |
|---|---|---|---|
| 0 | 2 | bytes | magic `"PM"` (0x50 0x4D) |
| 2 | 1 | u8 | version = 1 |
| 3 | 1 | u8 | flags: bit 0 = muted on the phone (payload is silence), bit 1 = resampled by the client |
| 4 | 4 | u32 | sequence number, +1 per frame, wraps at 2³² |
| 8 | 8 | f64 | client timestamp in ms (`performance.timeOrigin + performance.now()`) |
| 16 | N | s16le | interleaved PCM samples; N must be a multiple of 2 × channels, at most 192000 bytes |

The browser sends 10 ms frames: 480 samples at 48 kHz, or 441 at 44.1 kHz.

The server checks each frame:

* **Dropped packets:** a sequence gap is counted as lost frames.
* **Reordered or duplicate frames:** an older sequence number is ignored.
* **Malformed frames:** a bad magic, version or length, or audio sent before `audio_config`, is counted. More than 50 close the connection.
* **Stalled clients:** no audio for 2 s while streaming marks the client as *stalled*.
* **Excessive latency:** an estimated latency above 400 ms is flagged in the GUI and log.
* **Overload:** each per-client packet queue holds at most 64 frames and drops the oldest. Each jitter buffer is capped at 2 s and trims back to its target after bursts.

The browser itself drops frames instead of queueing when the socket has more than about 0.5 s unsent (`WebSocket.bufferedAmount`), so a slow network never builds up latency.
