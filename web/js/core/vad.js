// Voice activity detection on 20 ms frames (pure logic, unit-tested in Node; the audio worklet supplies the numbers).
//
// Why not just loudness: a laptop keyboard next to the microphone is as loud as a voice (measured 2026-10-24: typing
// peaks at 0.37 RMS, the room at 0.002). With a loudness-only rule every key press reset the «pause» counter, so a
// phrase said while typing kept recording for 8.6 s and 30 s and JARVIS seemed deaf. Speech differs from clicks in two
// ways that are cheap to measure: it lasts (vowels are 60+ ms, a click 10–30 ms) and most of its energy is below
// ~1 kHz (`low`, the share of energy under a 1 kHz low-pass). The noise floor is the quietest stretch of the last
// ~3 s, so it follows a fan or a TV in seconds and is never pulled up by the speech itself.
export class Vad {
  constructor({ silenceMs = 1300, maxMs = 30000 } = {}) {
    this.silenceMs = silenceMs;
    this.maxMs = maxMs;
    this.reset();
  }

  reset() {
    this.blocks = [];            // minimum of the smoothed level per 200 ms block, last 15 blocks (3 s)
    this.blockMin = Infinity; this.blockN = 0;
    this.smooth = 0;
    this.floor = 0.002;
    this.run = 0;                // loud speech-like frames in a row (start of a phrase)
    this.recent = [];            // loud frames among the last 15 (start while JARVIS speaks)
    this.last3 = [];             // speech-like frames among the last 3 (inside a phrase)
    this.inSeg = false;
    this.silent = 0; this.dur = 0; this.voiceMs = 0;
  }

  get thresholds() {
    return { start: Math.max(0.010, this.floor * 3.2), quiet: Math.max(0.006, this.floor * 2.2) };
  }

  track(rms) {
    this.smooth += (rms - this.smooth) * 0.35;
    this.blockMin = Math.min(this.blockMin, this.smooth);
    if (++this.blockN >= 10) {
      this.blocks.push(this.blockMin);
      if (this.blocks.length > 15) this.blocks.shift();
      this.blockMin = Infinity; this.blockN = 0;
      this.floor = Math.max(0.0005, Math.min(...this.blocks));
    }
  }

  // One frame → "start" | "end" | "max" | null. `speaking`: JARVIS's own voice is playing (a higher bar to cut in).
  push({ rms, low = 1 }, speaking = false) {
    this.track(rms);
    const { start, quiet } = this.thresholds;
    const voiced = low >= 0.3;
    if (!this.inSeg) {
      const thr = start * (speaking ? 1.7 : 1);
      const loud = rms > thr && voiced;
      this.run = loud ? this.run + 1 : 0;
      this.recent.push(loud);
      if (this.recent.length > 15) this.recent.shift();
      // While JARVIS talks, a short «Стоп» must still cut in: its consonants leave gaps, so count loud frames in the
      // last 300 ms (8 of 15) instead of a run.
      const go = speaking ? this.recent.filter(Boolean).length >= 8 : this.run >= 4;
      if (go) {
        // The loud frames that started the phrase are part of it: a quick «Джарвис» is only ~0.3 s of voice, and
        // without its first 80 ms it fell under the 200 ms «just a noise» bar (measured 2026-10-26: 220 ms counted).
        const onset = speaking ? 0 : this.run * 20;
        this.inSeg = true; this.silent = 0; this.dur = onset; this.voiceMs = onset; this.last3 = [];
        return "start";
      }
      return null;
    }
    this.dur += 20;
    this.last3.push(rms > quiet && voiced);
    if (this.last3.length > 3) this.last3.shift();
    if (this.last3.filter(Boolean).length >= 2) { this.silent = 0; this.voiceMs += 20; }
    else this.silent += 20;
    if (this.silent >= this.silenceMs && this.dur > 300) { this.inSeg = false; this.run = 0; this.recent = []; return "end"; }
    if (this.dur >= this.maxMs) { this.inSeg = false; this.run = 0; this.recent = []; return "max"; }
    return null;
  }

  // The caller ended the phrase itself (an early answer after a short pause).
  close() { this.inSeg = false; this.run = 0; this.recent = []; }
}

// Frame features, the same numbers the audio worklet computes: level and the share of energy under a 1 kHz low-pass.
export const LP_A = 1 - Math.exp(-2 * Math.PI * 1000 / 16000);
export function features(pcm, st = { lp: 0 }) {
  let e = 0, le = 0, lp = st.lp;
  for (let i = 0; i < pcm.length; i++) { const x = pcm[i]; lp += LP_A * (x - lp); e += x * x; le += lp * lp; }
  st.lp = lp;
  return { rms: Math.sqrt(e / pcm.length), low: e > 0 ? le / e : 1 };
}
