// Audio worklet: downsample microphone input to 16 kHz mono and post ~20 ms frames with their RMS and the share of
// their energy under a 1 kHz low-pass (voice vs. keyboard clicks — see core/vad.js, same formula as features()).
// Runs off the main thread; nothing here leaves the computer.
const LP_A = 1 - Math.exp(-2 * Math.PI * 1000 / 16000);
class VadFrames extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.acc = 0;
    this.pos = 0;
    this.frame = new Float32Array(320);   // 20 ms at 16 kHz
    this.n = 0;
    this.sum = 0;
    this.cnt = 0;
    this.lp = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.sum += ch[i];
      this.cnt++;
      this.acc += 1;
      if (this.acc >= this.ratio) {           // average-decimate to 16 kHz
        this.acc -= this.ratio;
        const v = this.sum / this.cnt;
        this.sum = 0; this.cnt = 0;
        this.frame[this.n++] = v;
        if (this.n === this.frame.length) {
          let e = 0, le = 0, lp = this.lp;
          for (let k = 0; k < this.n; k++) { const x = this.frame[k]; lp += LP_A * (x - lp); e += x * x; le += lp * lp; }
          this.lp = lp;
          // Frames go out in batches of 5 (100 ms): one message instead of five (measured: the per-frame messages
          // were most of the renderer's work while an always-on mic waits for «Джарвис»).
          (this.batch ||= []).push({ rms: Math.sqrt(e / this.n), low: e > 0 ? le / e : 1, pcm: this.frame.slice(0) });
          if (this.batch.length >= 5) { this.port.postMessage({ batch: this.batch }); this.batch = []; }
          this.n = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor("vad-frames", VadFrames);
