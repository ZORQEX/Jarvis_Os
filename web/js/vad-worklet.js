// Audio worklet: downsample microphone input to 16 kHz mono and post ~20 ms frames with their RMS.
// Runs off the main thread; nothing here leaves the computer.
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
          let e = 0;
          for (let k = 0; k < this.n; k++) e += this.frame[k] * this.frame[k];
          this.port.postMessage({ rms: Math.sqrt(e / this.n), pcm: this.frame.slice(0) });
          this.n = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor("vad-frames", VadFrames);
