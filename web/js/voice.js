// Voice: push-to-talk capture and spoken replies.
// LISTENING is reported only while a real microphone stream is open, and the
// microphone is released immediately after each utterance.

import { setLevel } from "./companion.js";

export class Voice {
  constructor({ onState, onTranscript, onError, link }) {
    this.onState = onState;          // (voiceState: 'off'|'listening'|'transcribing'|'speaking'|null)
    this.onTranscript = onTranscript;
    this.onError = onError;
    this.link = link;
    this.stream = null;
    this.rec = null;
    this.chunks = [];
    this.audio = null;
    this.muted = localStorage.getItem("jarvis.micMuted") === "1";
    this.speakReplies = localStorage.getItem("jarvis.speak") === "1";
    this.state = null;
    this.supported = !!(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
  }

  set(state) { this.state = state; this.onState(state); }

  setMuted(m) {
    this.muted = m;
    localStorage.setItem("jarvis.micMuted", m ? "1" : "0");
    if (m) this.cancel();
    this.onState(this.state);
  }

  get recording() { return this.rec?.state === "recording"; }

  async toggle() {
    if (this.recording) return this.stop();
    return this.start();
  }

  async start() {
    if (this.muted) { this.onError("Микрофон выключен — включите его в панели приватности"); return; }
    if (!this.supported) { this.onError("Запись звука недоступна в этом окне"); return; }
    this.stopSpeaking();
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    } catch (e) {
      this.onError(e.name === "NotAllowedError" ? "Нет доступа к микрофону" : "Микрофон не найден");
      return;
    }
    const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "";
    this.rec = new MediaRecorder(this.stream, mime ? { mimeType: mime, audioBitsPerSecond: 32000 } : {});
    this.chunks = [];
    this.rec.ondataavailable = (e) => e.data.size && this.chunks.push(e.data);
    this.rec.onstop = () => this.finish();
    this.rec.start(250);
    this.startedAt = performance.now();
    this.meter();
    this.set("listening");
    // Hard cap: 60 s per utterance.
    this.capTimer = setTimeout(() => this.stop(), 60000);
  }

  meter() {
    const ctx = new AudioContext();
    const src = ctx.createMediaStreamSource(this.stream);
    const an = ctx.createAnalyser();
    an.fftSize = 512;
    src.connect(an);
    const buf = new Uint8Array(an.fftSize);
    this.actx = ctx;
    const tick = () => {
      if (!this.recording) { setLevel(0); return; }
      an.getByteTimeDomainData(buf);
      let sum = 0;
      for (const v of buf) { const x = (v - 128) / 128; sum += x * x; }
      const rms = Math.sqrt(sum / buf.length);
      const lvl = Math.min(1, rms * 6);
      setLevel(lvl);
      document.documentElement.style.setProperty("--lvl", lvl.toFixed(3));
      requestAnimationFrame(tick);
    };
    tick();
  }

  release() {
    clearTimeout(this.capTimer);
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.actx?.close();
    this.actx = null;
    setLevel(0);
    document.documentElement.style.setProperty("--lvl", "0");
  }

  stop() { if (this.recording) this.rec.stop(); }

  cancel() {
    if (this.recording) { this.rec.onstop = null; this.rec.stop(); }
    this.release();
    this.chunks = [];
    if (this.state === "listening") this.set(null);
  }

  async finish() {
    this.release();
    const dur = performance.now() - this.startedAt;
    const blob = new Blob(this.chunks, { type: this.rec.mimeType || "audio/webm" });
    this.chunks = [];
    if (dur < 400 || blob.size < 1500) { this.set(null); return; }
    this.set("transcribing");
    try {
      const b64 = await blobToB64(blob);
      const r = await this.link.request("voice.transcribe", { audio_b64: b64, mime: blob.type }, 45000);
      this.set(null);
      if (r.text) this.onTranscript(r.text);
      else this.onError("Не расслышал — попробуйте ещё раз");
    } catch (e) {
      this.set(null);
      this.onError(e.message);
    }
  }

  // ---------------------------------------------------------------- speech out
  async speak(text) {
    if (!this.speakReplies || !text) return;
    const clean = text.replace(/[*_`#>]/g, "").replace(/\n+/g, " ").slice(0, 900);
    try {
      const res = await fetch("/api/tts", { method: "POST", headers: { "Content-Type": "application/json", "X-Jarvis": "1" },
        body: JSON.stringify({ text: clean }) });
      if (!res.ok) throw new Error((await res.json()).error || "TTS недоступен");
      const url = URL.createObjectURL(await res.blob());
      this.stopSpeaking();
      const a = new Audio(url);
      this.audio = a;
      a.onplay = () => this.set("speaking");
      a.onended = a.onpause = () => { URL.revokeObjectURL(url); if (this.audio === a) { this.audio = null; this.set(null); } };
      await a.play();
    } catch (e) {
      this.onError("Озвучивание не удалось: " + e.message + " (текст ответа показан)");
    }
  }

  stopSpeaking() {
    if (this.audio) { const a = this.audio; this.audio = null; a.pause(); this.set(null); }
  }
}

function blobToB64(blob) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(",")[1]);
    r.onerror = rej;
    r.readAsDataURL(blob);
  });
}
