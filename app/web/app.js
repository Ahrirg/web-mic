// Phone Microphone browser client.
// Captures the microphone with getUserMedia + AudioWorklet and streams 16-bit
// PCM frames to the Linux computer over a WebSocket. No frameworks, no
// external resources: everything stays on the local network.
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const ui = {
    startBtn: $('startBtn'), muteBtn: $('muteBtn'), statusDot: $('statusDot'), statusText: $('statusText'),
    statusDetail: $('statusDetail'), pairCard: $('pairCard'), codeInput: $('codeInput'),
    meterFill: $('meterFill'), meterPeak: $('meterPeak'), levelText: $('levelText'), clipText: $('clipText'),
    micSelect: $('micSelect'), rateText: $('rateText'), channelsText: $('channelsText'), connText: $('connText'),
    targetText: $('targetText'), latencyText: $('latencyText'), nameInput: $('nameInput'),
    optNoise: $('optNoise'), optEcho: $('optEcho'), optAgc: $('optAgc'), optWake: $('optWake'),
    secureWarning: $('secureWarning'), httpsHint: $('httpsHint'), httpsLink: $('httpsLink'),
    unsupported: $('unsupported'), unsupportedText: $('unsupportedText'), serverName: $('serverName'),
  };

  const FRAME_MS = 10;
  const MAX_BUFFERED_BYTES = 48000; // ~0.5 s of audio queued in the socket => drop frames
  const store = {
    get(k, d) { try { const v = localStorage.getItem('pmr.' + k); return v === null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem('pmr.' + k, v); } catch (e) { /* private mode */ } },
    del(k) { try { localStorage.removeItem('pmr.' + k); } catch (e) { /* ignore */ } },
  };

  const state = {
    running: false,       // user wants the mic on
    ws: null,
    wsReady: false,       // welcome received + audio_config sent
    ctx: null, stream: null, source: null, node: null, sink: null,
    seq: 0, muted: false, reconnectDelay: 500, reconnectTimer: null,
    wakeLock: null, peakHold: 0, peakTime: 0, clipTime: 0, dropped: 0,
    info: null, authRequired: true, stopReason: '',
  };

  // ---------------------------------------------------------------- helpers
  function uuid() {
    if (crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, '');
    let s = '';
    for (let i = 0; i < 32; i++) s += Math.floor(Math.random() * 16).toString(16);
    return s;
  }
  const clientId = store.get('clientId', '') || (() => { const id = uuid(); store.set('clientId', id); return id; })();

  function setStatus(kind, text, detail) {
    ui.statusText.textContent = text;
    ui.statusDot.className = 'dot' + (kind ? ' ' + kind : '');
    if (detail !== undefined) ui.statusDetail.textContent = detail;
  }

  function isLocalhost() {
    return ['localhost', '127.0.0.1', '[::1]', '::1'].includes(location.hostname);
  }

  function connectionLabel() {
    const secure = location.protocol === 'https:' ? 'HTTPS' : 'HTTP';
    return (isLocalhost() ? 'USB (ADB) / localhost' : 'Wi-Fi / LAN') + ' · ' + secure;
  }

  function dbfs(v) { return v > 1e-5 ? Math.min(0, 20 * Math.log10(v)) : -100; }

  function platformName() {
    const ua = navigator.userAgent;
    if (/Android/.test(ua)) return 'Android';
    if (/iPhone|iPad/.test(ua)) return 'iOS';
    if (/Windows/.test(ua)) return 'Windows';
    if (/Mac OS X/.test(ua)) return 'macOS';
    if (/CrOS/.test(ua)) return 'ChromeOS';
    if (/Linux/.test(ua)) return 'Linux';
    return 'Unknown';
  }

  function browserName() {
    const ua = navigator.userAgent;
    if (/SamsungBrowser/.test(ua)) return 'Samsung Internet';
    if (/EdgA|Edg\//.test(ua)) return 'Edge';
    if (/OPR\//.test(ua)) return 'Opera';
    if (/Firefox|FxiOS/.test(ua)) return 'Firefox';
    if (/Chrome|CriOS/.test(ua)) return 'Chrome';
    if (/Safari/.test(ua)) return 'Safari';
    return 'Browser';
  }

  let detectedModel = '';
  async function detectModel() {
    // Chrome on Android reports the model ("Pixel 8") through UA client hints.
    try {
      if (navigator.userAgentData && navigator.userAgentData.getHighEntropyValues) {
        const v = await navigator.userAgentData.getHighEntropyValues(['model', 'platformVersion']);
        if (v.model) detectedModel = v.model;
      }
    } catch (e) { /* not available */ }
  }

  function deviceName() {
    const custom = ui.nameInput.value.trim();
    if (custom) return custom;
    if (detectedModel) return detectedModel + ' (' + browserName() + ')';
    return '';
  }

  // ---------------------------------------------------------------- capability checks
  function checkEnvironment() {
    ui.connText.textContent = connectionLabel();
    const hasMedia = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!window.isSecureContext) {
      ui.secureWarning.hidden = false;
      fetch('/api/info').then((r) => r.json()).then((info) => {
        if (info.https_port) {
          // https:// is a different origin with its own storage, so carry the pairing code over.
          const code = store.get('code', '');
          const url = 'https://' + location.hostname + ':' + info.https_port + '/' + (code ? '?code=' + code : '');
          ui.httpsLink.href = url;
          ui.httpsLink.hidden = false;
          ui.httpsHint.textContent = 'Use the secure address instead: ' + url;
        } else {
          ui.httpsHint.textContent = 'HTTPS is disabled on the computer. Enable it in Settings, or connect via USB (ADB) and open http://127.0.0.1 instead.';
        }
      }).catch(() => {});
      ui.startBtn.disabled = true;
      setStatus('err', 'Not a secure page', 'Open the HTTPS address or use USB.');
      return false;
    }
    if (!hasMedia || !AC || !window.WebSocket) {
      ui.unsupported.hidden = false;
      ui.startBtn.disabled = true;
      setStatus('err', 'Unsupported browser');
      return false;
    }
    return true;
  }

  async function loadInfo() {
    try {
      const r = await fetch('/api/info', { cache: 'no-store' });
      state.info = await r.json();
      state.authRequired = !!state.info.auth_required;
      ui.serverName.textContent = 'Connected to ' + state.info.app + ' · sending to ' + state.info.source_description;
    } catch (e) {
      ui.serverName.textContent = 'Computer not reachable';
    }
    const hasToken = !!store.get('token', '');
    ui.pairCard.hidden = !state.authRequired || (hasToken && !!store.get('code', ''));
  }

  // ---------------------------------------------------------------- devices
  async function refreshMicList() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const current = store.get('micId', '');
      const inputs = devices.filter((d) => d.kind === 'audioinput');
      ui.micSelect.innerHTML = '';
      const def = document.createElement('option');
      def.value = '';
      def.textContent = 'Default microphone';
      ui.micSelect.appendChild(def);
      inputs.forEach((d, i) => {
        if (d.deviceId === 'default' || d.deviceId === '') return;
        const o = document.createElement('option');
        o.value = d.deviceId;
        o.textContent = d.label || ('Microphone ' + (i + 1));
        ui.micSelect.appendChild(o);
      });
      ui.micSelect.value = [...ui.micSelect.options].some((o) => o.value === current) ? current : '';
    } catch (e) { /* ignore */ }
  }

  // ---------------------------------------------------------------- audio capture
  function micErrorMessage(err) {
    switch (err && err.name) {
      case 'NotAllowedError':
      case 'PermissionDeniedError':
        return 'Microphone permission denied. Tap the lock icon next to the address, allow the microphone, and try again.';
      case 'NotFoundError':
        return 'No microphone was found on this device.';
      case 'NotReadableError':
        return 'The microphone is in use by another app (a call or recorder). Close it and try again.';
      case 'OverconstrainedError':
        return 'The selected microphone is not available. Choose the default microphone.';
      case 'SecurityError':
        return 'The browser blocked microphone access on this page (not a secure context).';
      default:
        return 'Could not start the microphone: ' + ((err && err.message) || err);
    }
  }

  async function createContext(stream) {
    const AC = window.AudioContext || window.webkitAudioContext;
    let ctx = null;
    try {
      ctx = new AC({ sampleRate: 48000, latencyHint: 'interactive' });
    } catch (e) {
      ctx = new AC({ latencyHint: 'interactive' });
    }
    let source;
    try {
      source = ctx.createMediaStreamSource(stream);
    } catch (e) {
      // Firefox cannot connect a stream to a context running at a different
      // rate. Fall back to the device's native rate; the server resamples.
      try { await ctx.close(); } catch (e2) { /* ignore */ }
      ctx = new AC({ latencyHint: 'interactive' });
      source = ctx.createMediaStreamSource(stream);
    }
    return { ctx, source };
  }

  async function startCapture() {
    const micId = ui.micSelect.value;
    const constraints = {
      audio: {
        channelCount: { ideal: 1 },
        echoCancellation: ui.optEcho.checked,
        noiseSuppression: ui.optNoise.checked,
        autoGainControl: ui.optAgc.checked,
        sampleRate: { ideal: 48000 },
      },
      video: false,
    };
    if (micId) constraints.audio.deviceId = { exact: micId };
    const stream = await navigator.mediaDevices.getUserMedia(constraints);
    state.stream = stream;
    const track = stream.getAudioTracks()[0];
    track.addEventListener('ended', () => {
      if (state.running) {
        reportError('Microphone track ended');
        stopAll('The microphone was taken away by the system or another app.');
      }
    });
    const { ctx, source } = await createContext(stream);
    state.ctx = ctx;
    state.source = source;
    const frameSize = Math.round(ctx.sampleRate * FRAME_MS / 1000);
    // A silent sink keeps the graph pulling audio on every browser.
    const sink = ctx.createGain();
    sink.gain.value = 0;
    sink.connect(ctx.destination);
    state.sink = sink;

    if (ctx.audioWorklet && window.AudioWorkletNode) {
      await ctx.audioWorklet.addModule('worklet.js');
      const node = new AudioWorkletNode(ctx, 'pcm-capture', {
        numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
        channelCount: 1, channelCountMode: 'explicit', channelInterpretation: 'speakers',
        processorOptions: { frameSize },
      });
      node.port.onmessage = (e) => onFrame(e.data.pcm, e.data.peak, e.data.rms);
      node.port.postMessage({ muted: state.muted });
      source.connect(node);
      node.connect(sink);
      state.node = node;
    } else {
      // Fallback for browsers without AudioWorklet (deprecated API, still widely available).
      const proc = ctx.createScriptProcessor(1024, 1, 1);
      let pending = new Int16Array(frameSize);
      let fill = 0;
      proc.onaudioprocess = (ev) => {
        const data = ev.inputBuffer.getChannelData(0);
        let peak = 0;
        let sum = 0;
        for (let i = 0; i < data.length; i++) {
          let s = state.muted ? 0 : data[i];
          const a = Math.abs(data[i]);
          if (a > peak) peak = a;
          sum += data[i] * data[i];
          s = Math.max(-1, Math.min(1, s));
          pending[fill++] = s < 0 ? s * 32768 : s * 32767;
          if (fill === frameSize) {
            onFrame(pending.buffer, peak, Math.sqrt(sum / (i + 1)));
            pending = new Int16Array(frameSize);
            fill = 0;
          }
        }
      };
      source.connect(proc);
      proc.connect(sink);
      state.node = proc;
    }
    if (ctx.state === 'suspended') await ctx.resume();
    ui.rateText.textContent = ctx.sampleRate + ' Hz' + (ctx.sampleRate !== 48000 ? ' (resampled to 48000 Hz on the computer)' : '');
    ui.channelsText.textContent = 'Mono';
    const settings = track.getSettings ? track.getSettings() : {};
    if (settings.deviceId) store.set('micId', micId);
    await refreshMicList();
  }

  function stopCapture() {
    try { if (state.node) state.node.disconnect(); } catch (e) { /* ignore */ }
    try { if (state.source) state.source.disconnect(); } catch (e) { /* ignore */ }
    if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
    if (state.ctx) state.ctx.close().catch(() => {});
    state.node = state.source = state.stream = state.ctx = state.sink = null;
  }

  // ---------------------------------------------------------------- streaming
  function onFrame(pcmBuffer, peak, rms) {
    updateMeter(peak, rms);
    const ws = state.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN || !state.wsReady) return;
    if (ws.bufferedAmount > MAX_BUFFERED_BYTES) {
      state.dropped++; // network too slow: drop instead of building up latency
      return;
    }
    const header = new ArrayBuffer(16);
    const dv = new DataView(header);
    dv.setUint8(0, 0x50); // 'P'
    dv.setUint8(1, 0x4d); // 'M'
    dv.setUint8(2, 1);    // version
    dv.setUint8(3, state.muted ? 1 : 0);
    dv.setUint32(4, state.seq >>> 0, true);
    dv.setFloat64(8, performance.timeOrigin + performance.now(), true);
    state.seq = (state.seq + 1) >>> 0;
    const packet = new Uint8Array(16 + pcmBuffer.byteLength);
    packet.set(new Uint8Array(header), 0);
    packet.set(new Uint8Array(pcmBuffer), 16);
    ws.send(packet.buffer);
  }

  function connect() {
    clearTimeout(state.reconnectTimer);
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(proto + '//' + location.host + '/ws');
    ws.binaryType = 'arraybuffer';
    state.ws = ws;
    state.wsReady = false;
    setStatus('busy', 'Connecting', 'Connecting to the computer…');
    ws.onopen = () => {
      const code = (ui.codeInput.value || store.get('code', '')).replace(/\D/g, '');
      ws.send(JSON.stringify({
        type: 'hello', protocol: 1, client_id: clientId, device_name: deviceName(),
        browser: browserName(), platform: platformName(), user_agent: navigator.userAgent,
        pairing_code: code, token: store.get('token', ''),
      }));
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data !== 'string') return;
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      handleMessage(ws, msg);
    };
    ws.onclose = (ev) => {
      if (state.ws !== ws) return;
      state.wsReady = false;
      state.ws = null;
      if (!state.running) return;
      if (state.stopReason) { stopAll(state.stopReason); return; }
      const delay = state.reconnectDelay;
      state.reconnectDelay = Math.min(state.reconnectDelay * 2, 5000);
      setStatus('busy', 'Reconnecting', 'Connection lost' + (ev.code ? ' (' + ev.code + ')' : '') + '. Retrying in ' + (delay / 1000).toFixed(1) + ' s…');
      state.reconnectTimer = setTimeout(connect, delay);
    };
    ws.onerror = () => { /* onclose follows */ };
  }

  function handleMessage(ws, msg) {
    switch (msg.type) {
      case 'welcome': {
        store.set('token', msg.token);
        const typed = ui.codeInput.value.replace(/\D/g, '');
        if (typed) store.set('code', typed);
        ui.pairCard.hidden = true;
        state.reconnectDelay = 500;
        ws.send(JSON.stringify({
          type: 'audio_config', sample_rate: state.ctx ? state.ctx.sampleRate : 48000,
          channels: 1, format: 's16le', frame_ms: FRAME_MS,
        }));
        state.wsReady = true;
        state.seq = 0;
        ui.targetText.textContent = msg.source_description || '–';
        setStatus('ok', 'Streaming', 'Connected as "' + msg.name + '"');
        break;
      }
      case 'ping': {
        const base = state.ctx ? (state.ctx.baseLatency || 0) * 1000 : 0;
        const queued = state.ws ? state.ws.bufferedAmount / 96 : 0; // bytes -> ms at 48 kHz s16
        ws.send(JSON.stringify({ type: 'pong', t: msg.t, buffer_ms: Math.round(base + queued) }));
        ui.latencyText.textContent = msg.latency_ms == null ? '–' : '~' + msg.latency_ms + ' ms (approx.)';
        if (msg.routed) {
          ui.targetText.textContent = msg.target + (msg.muted ? ' (muted on computer)' : '');
          setStatus('ok', 'Streaming', msg.muted ? 'Muted on the computer' : 'Sending audio to ' + msg.target);
        } else {
          ui.targetText.textContent = 'Not routed (another device is active)';
          setStatus('ok', 'Connected', 'Connected, but the computer is using another device for "' + msg.target + '".');
        }
        break;
      }
      case 'error': {
        if (msg.code === 'auth_failed' || msg.code === 'rate_limited') {
          store.del('token');
          store.del('code');
          ui.pairCard.hidden = false;
          state.stopReason = msg.message;
          ui.codeInput.focus();
        } else {
          state.stopReason = msg.message || msg.code;
        }
        break;
      }
      case 'kick': {
        if (msg.reason === 'server_shutdown') {
          setStatus('busy', 'Reconnecting', 'The computer is restarting the server…');
        } else if (msg.reason === 'replaced') {
          state.stopReason = 'This device connected again from another tab or window.';
        } else {
          state.stopReason = 'Disconnected by the computer (' + msg.reason + ').';
        }
        break;
      }
      default:
        break;
    }
  }

  function reportError(message) {
    const ws = state.ws;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'client_error', message }));
  }

  // ---------------------------------------------------------------- meter
  function updateMeter(peak, rms) {
    const now = performance.now();
    const db = dbfs(rms);
    const pct = Math.max(0, Math.min(100, (db + 60) / 60 * 100));
    ui.meterFill.style.width = pct.toFixed(1) + '%';
    if (peak > state.peakHold || now - state.peakTime > 1500) { state.peakHold = peak; state.peakTime = now; }
    const pk = Math.max(0, Math.min(100, (dbfs(state.peakHold) + 60) / 60 * 100));
    ui.meterPeak.style.left = pk.toFixed(1) + '%';
    ui.meterPeak.style.opacity = pk > 0.5 ? '0.8' : '0';
    if (peak >= 0.999) state.clipTime = now;
    ui.clipText.hidden = now - state.clipTime > 2000;
    ui.levelText.textContent = (db <= -99 ? '-∞' : db.toFixed(0)) + ' dBFS  ·  peak ' + (dbfs(state.peakHold) <= -99 ? '-∞' : dbfs(state.peakHold).toFixed(0));
  }

  function resetMeter() {
    ui.meterFill.style.width = '0%';
    ui.meterPeak.style.left = '0%';
    ui.meterPeak.style.opacity = '0';
    ui.levelText.textContent = '– dBFS';
    ui.clipText.hidden = true;
  }

  // ---------------------------------------------------------------- wake lock
  async function requestWakeLock() {
    if (!ui.optWake.checked || !('wakeLock' in navigator)) return;
    try {
      state.wakeLock = await navigator.wakeLock.request('screen');
      state.wakeLock.addEventListener('release', () => { state.wakeLock = null; });
    } catch (e) { /* battery saver or unsupported */ }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && state.running && !state.wakeLock) requestWakeLock();
    if (document.visibilityState === 'visible' && state.ctx && state.ctx.state === 'suspended') state.ctx.resume();
  });

  // ---------------------------------------------------------------- start/stop
  async function startAll() {
    if (state.authRequired && !store.get('token', '') && !ui.codeInput.value.replace(/\D/g, '') && !store.get('code', '')) {
      ui.pairCard.hidden = false;
      setStatus('err', 'Pairing code needed', 'Enter the code shown on the computer, then press Start.');
      ui.codeInput.focus();
      return;
    }
    state.stopReason = '';
    ui.startBtn.disabled = true;
    setStatus('busy', 'Starting', 'Waiting for microphone permission…');
    try {
      await startCapture();
    } catch (err) {
      stopCapture();
      ui.startBtn.disabled = false;
      setStatus('err', 'Microphone unavailable', micErrorMessage(err));
      return;
    }
    state.running = true;
    ui.startBtn.disabled = false;
    ui.startBtn.textContent = 'Stop Microphone';
    ui.startBtn.classList.add('stop');
    ui.muteBtn.hidden = false;
    requestWakeLock();
    connect();
  }

  function stopAll(reason) {
    state.running = false;
    clearTimeout(state.reconnectTimer);
    const ws = state.ws;
    state.ws = null;
    if (ws) {
      try { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'stop' })); } catch (e) { /* ignore */ }
      try { ws.close(1000, 'stopped'); } catch (e) { /* ignore */ }
    }
    stopCapture();
    if (state.wakeLock) { state.wakeLock.release().catch(() => {}); state.wakeLock = null; }
    ui.startBtn.textContent = 'Start Microphone';
    ui.startBtn.classList.remove('stop');
    ui.startBtn.disabled = false;
    ui.muteBtn.hidden = true;
    ui.latencyText.textContent = '–';
    resetMeter();
    if (reason) setStatus('err', 'Disconnected', reason);
    else setStatus('', 'Disconnected', '');
    state.stopReason = '';
  }

  function toggleMute() {
    state.muted = !state.muted;
    if (state.node && state.node.port) state.node.port.postMessage({ muted: state.muted });
    ui.muteBtn.textContent = state.muted ? 'Unmute' : 'Mute';
    ui.muteBtn.classList.toggle('active', state.muted);
  }

  // ---------------------------------------------------------------- init
  function init() {
    const params = new URLSearchParams(location.search);
    const code = (params.get('code') || '').replace(/\D/g, '');
    if (code) {
      store.set('code', code);
      store.del('token');
      ui.codeInput.value = code;
      history.replaceState(null, '', location.pathname); // keep the code out of history/bookmarks
    } else {
      ui.codeInput.value = store.get('code', '');
    }
    ui.nameInput.value = store.get('name', '');
    ui.optNoise.checked = store.get('noise', '0') === '1';
    ui.optEcho.checked = store.get('echo', '0') === '1';
    ui.optAgc.checked = store.get('agc', '0') === '1';
    ui.optWake.checked = store.get('wake', '1') === '1';
    ui.nameInput.addEventListener('change', () => store.set('name', ui.nameInput.value.trim()));
    ui.optNoise.addEventListener('change', () => store.set('noise', ui.optNoise.checked ? '1' : '0'));
    ui.optEcho.addEventListener('change', () => store.set('echo', ui.optEcho.checked ? '1' : '0'));
    ui.optAgc.addEventListener('change', () => store.set('agc', ui.optAgc.checked ? '1' : '0'));
    ui.optWake.addEventListener('change', () => store.set('wake', ui.optWake.checked ? '1' : '0'));
    ui.codeInput.addEventListener('input', () => { store.del('token'); });
    ui.micSelect.addEventListener('change', async () => {
      store.set('micId', ui.micSelect.value);
      if (state.running) {
        // Restart capture with the new microphone and keep the connection.
        stopCapture();
        try {
          await startCapture();
          if (state.ws && state.wsReady) {
            state.ws.send(JSON.stringify({ type: 'audio_config', sample_rate: state.ctx.sampleRate, channels: 1, format: 's16le', frame_ms: FRAME_MS }));
          }
        } catch (err) {
          stopAll(micErrorMessage(err));
        }
      }
    });
    ui.startBtn.addEventListener('click', () => { if (state.running) stopAll(); else startAll(); });
    ui.muteBtn.addEventListener('click', toggleMute);
    if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
      navigator.mediaDevices.addEventListener('devicechange', refreshMicList);
    }
    if (!checkEnvironment()) return;
    detectModel();
    loadInfo();
    refreshMicList();
    setStatus('', 'Disconnected', 'Press Start to send this microphone to the computer.');
  }

  init();
})();
