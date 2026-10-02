// AudioWorklet processor: converts the microphone's Float32 frames into
// fixed-size 16-bit PCM chunks and reports levels. Runs on the audio thread.
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.frameSize = opts.frameSize || 480;
    this.buffer = new Int16Array(this.frameSize);
    this.fill = 0;
    this.muted = false;
    this.peak = 0;
    this.sumSq = 0;
    this.count = 0;
    this.port.onmessage = (e) => {
      if (e.data && typeof e.data.muted === 'boolean') this.muted = e.data.muted;
    };
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const ch0 = input[0];
    const ch1 = input.length > 1 ? input[1] : null;
    for (let i = 0; i < ch0.length; i++) {
      // Down-mix to mono if the browser delivers two channels.
      let s = ch1 ? (ch0[i] + ch1[i]) * 0.5 : ch0[i];
      const a = s < 0 ? -s : s;
      if (a > this.peak) this.peak = a;
      this.sumSq += s * s;
      this.count++;
      if (this.muted) s = 0;
      if (s > 1) s = 1; else if (s < -1) s = -1;
      this.buffer[this.fill++] = s < 0 ? Math.round(s * 32768) : Math.round(s * 32767);
      if (this.fill === this.frameSize) {
        const out = this.buffer;
        this.port.postMessage({
          pcm: out.buffer,
          peak: this.peak,
          rms: Math.sqrt(this.sumSq / Math.max(1, this.count)),
        }, [out.buffer]);
        this.buffer = new Int16Array(this.frameSize);
        this.fill = 0;
        this.peak = 0;
        this.sumSq = 0;
        this.count = 0;
      }
    }
    return true;
  }
}

registerProcessor('pcm-capture', PcmCaptureProcessor);
