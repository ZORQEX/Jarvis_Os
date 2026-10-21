// Voice: local voice-activity detection, hands-free conversation, streamed speech playback, barge-in.
// The microphone is open only while listening is on (shown in the UI); only detected speech segments are
// sent for recognition. LISTENING is reported only while a real microphone stream is open.
import { setLevel } from "./companion.js";

const now = () => performance.timeOrigin + performance.now();   // epoch ms, same clock as the core

export class Voice {
  constructor({ link, onState, onError, onTurn, onOutput }) {
    this.link = link;
    this.onOutput = onOutput || (() => {});
    this.outputChecked = 0;
    this.onState = onState;
    this.onError = onError;
    this.onTurn = onTurn || (() => {});
    this.muted = localStorage.getItem("jarvis.micMuted") === "1";
    this.speakReplies = localStorage.getItem("jarvis.speak") === "1";
    // Pause that ends a phrase. 0.65 s cut the user off mid-command («Отправь» / «в Телеграме» / …): now 1.3 s.
    this.silenceMs = +(localStorage.getItem("jarvis.silenceMs2") || 1300);
    this.closeWhenQuiet = false;  // mic button: stay open while JARVIS answers, so it can be interrupted
    this.lastAudioEnd = 0;
    this.supported = !!(navigator.mediaDevices?.getUserMedia && window.AudioWorkletNode);
    this.state = null;  // null | listening | recording | transcribing | synthesizing | speaking | interrupted | error
    this.conversation = false;    // hands-free: keep listening after each answer
    this.once = false;            // single utterance (mic button)
    this.stream = null; this.ctx = null; this.node = null;
    this.out = null; this.playing = []; this.nextAt = 0;
    this.turnOf = new Map();      // task_id -> turn id (telemetry)
    this.dropped = new Set();     // task ids interrupted: ignore late audio
    this.resetVad();
  }

  set(s) { if (this.state !== s) { this.state = s; this.onState(s); } }
  get listening() { return !!this.stream; }
  get recording() { return this.state === "recording"; }
  get audio() { return this.playing.length > 0; }

  resetVad() {
    this.floor = 0.004; this.voiced = 0; this.silent = 0; this.seg = null; this.preroll = []; this.segStart = 0;
  }

  setMuted(m) {
    this.muted = m;
    localStorage.setItem("jarvis.micMuted", m ? "1" : "0");
    if (m) this.closeMic();
    this.onState(this.state);
  }

  // ---------------------------------------------------------------- microphone
  async openMic() {
    if (this.stream) return true;
    if (this.muted) { this.onError("Микрофон выключен — включите его в панели приватности"); return false; }
    if (!this.supported) { this.onError("Запись звука недоступна в этом окне"); return false; }
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    } catch (e) {
      this.onError(e.name === "NotAllowedError" ? "Нет доступа к микрофону" : "Микрофон не найден");
      return false;
    }
    this.ctx = new AudioContext();
    await this.ctx.audioWorklet.addModule("/static/js/vad-worklet.js");
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "vad-frames");
    this.node.port.onmessage = (e) => this.frame(e.data);
    src.connect(this.node);
    this.resetVad();
    this.idleSince = now();
    this.set("listening");
    return true;
  }

  closeMic() {
    this.closeWhenQuiet = false;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.node?.disconnect(); this.node = null;
    this.ctx?.close(); this.ctx = null;
    this.resetVad();
    setLevel(0);
    if (["listening", "recording"].includes(this.state)) this.set(this.audio ? "speaking" : null);
  }

  async listenOnce() {                      // mic button: one utterance, ends by itself on silence
    if (this.listening && !this.conversation) { this.closeMic(); return; }
    this.once = true; this.closeWhenQuiet = false;
    await this.openMic();
  }

  async toggleConversation() {
    this.conversation = !this.conversation;
    if (this.conversation) { this.once = false; await this.openMic() || (this.conversation = false); }
    else this.closeMic();
    this.onState(this.state);
  }

  // ---------------------------------------------------------------- VAD (local, ~20 ms frames)
  frame({ rms, pcm }) {
    const speaking = this.audio;
    // Slightly higher bar while JARVIS talks (echo cancellation removes most of its own voice), so the user can
    // still cut in with a normal voice: 1.7× threshold for 200 ms (was 2.4× for 280 ms — too hard to interrupt).
    const thr = Math.max(0.012, this.floor * 3.2) * (speaking ? 1.7 : 1);
    const need = speaking ? 10 : 4;          // frames of voice to start (200 ms / 80 ms)
    setLevel(Math.min(1, rms * 8));
    if (!this.seg && this.closeWhenQuiet && !speaking && this.state !== "transcribing" && this.state !== "synthesizing"
        && now() - this.idleSince > 6000 && now() - this.lastAudioEnd > 2000) {
      this.closeWhenQuiet = false; this.closeMic(); return;   // answer finished: the single-phrase mic turns off
    }
    if (!this.seg) {
      this.preroll.push(pcm);
      if (this.preroll.length > 20) this.preroll.shift();     // keep 400 ms before speech start
      if (rms > thr) this.voiced++; else { this.voiced = 0; this.floor = this.floor * 0.995 + rms * 0.005; }
      if (this.voiced >= need) this.startSegment();
      if (this.conversation && now() - this.idleSince > 180000) {   // 3 min without speech: stop listening
        this.conversation = false; this.closeMic(); this.onError("Разговор завершён: 3 минуты тишины");
      }
      return;
    }
    this.seg.push(pcm);
    if (rms > thr * 0.7) this.silent = 0; else this.silent += 20;
    const dur = this.seg.length * 20;
    if ((this.silent >= this.silenceMs && dur > 300) || dur > 30000) this.endSegment();
  }

  startSegment() {
    this.segStart = now() - this.preroll.length * 20;
    this.seg = this.preroll.slice(0);
    this.preroll = [];
    this.silent = 0;
    if (this.audio) this.bargeIn();
    this.set("recording");
    this.link.request("voice.warm").catch(() => {});        // warm the cloud connection while the user speaks
  }

  async endSegment() {
    const frames = this.seg; this.seg = null; this.voiced = 0;
    const speechEnd = now() - this.silent;
    const voicedMs = frames.length * 20 - this.silent;
    if (voicedMs < 250) { this.set("listening"); return; }
    const wav = encodeWav(frames);
    this.idleSince = now();
    if (this.once && !this.conversation) { this.once = false; this.closeWhenQuiet = true; }
    this.set("transcribing");
    try {
      const r = await this.link.request("voice.transcribe", {
        audio_b64: await b64(wav), mime: "audio/wav", speech_start: this.segStart / 1000, speech_end: speechEnd / 1000,
      }, 45000);
      if (r.task_id && r.turn) this.turnOf.set(r.task_id, r.turn);
      if (r.turn) this.turnOf.set("notice-" + r.turn, r.turn);
      this.onTurn(r);
    } catch (e) {
      this.onError(e.message);
    }
    this.set(this.listening ? "listening" : (this.audio ? "speaking" : null));
  }

  // ---------------------------------------------------------------- playback queue
  async enqueue(ev) {
    if (this.dropped.has(ev.task_id)) return;
    if (!this.out) this.out = new AudioContext();
    if (this.out.state === "suspended") await this.out.resume().catch(() => {});
    if (this.out.state !== "running") {        // never claim we are speaking when the browser blocks audio
      this.set("error"); this.onError("Окно не может воспроизвести звук (аудио заблокировано). Нажмите в окне JARVIS и повторите.");
      return;
    }
    let buf;
    try {
      const bytes = Uint8Array.from(atob(ev.audio_b64), (c) => c.charCodeAt(0));
      buf = await this.out.decodeAudioData(bytes.buffer);
    } catch {
      this.onError("Не удалось воспроизвести звук ответа — текст показан на экране");
      if (this.state === "synthesizing") this.set(this.listening ? "listening" : null);
      return;
    }
    if (this.dropped.has(ev.task_id)) return;
    const src = this.out.createBufferSource();
    src.buffer = buf;
    src.connect(this.out.destination);
    const at = Math.max(this.out.currentTime + 0.02, this.nextAt);
    src.start(at);
    this.nextAt = at + buf.duration;
    const item = { src, task: ev.task_id };
    this.playing.push(item);
    src.onended = () => {
      this.playing = this.playing.filter((x) => x !== item);
      this.lastAudioEnd = now();
      if (!this.playing.length && this.state === "speaking") this.set(this.listening ? "listening" : null);
    };
    const delay = Math.max(0, (at - this.out.currentTime) * 1000);
    setTimeout(() => {                         // SPEAKING only once the sound really starts
      if (!this.playing.includes(item)) return;
      if (this.state !== "recording" && this.state !== "transcribing") this.set("speaking");
      const turn = this.turnOf.get(ev.task_id);
      if (turn && ev.seq === 1) this.link.request("voice.metric", { turn, field: "first_audio_played", ts: now() / 1000 }).catch(() => {});
      this.checkOutput();
    }, delay);
  }

  // Is JARVIS's own playback stream audible? (PipeWire per-application volume; checked while sound plays)
  async checkOutput(force = false) {
    if (!force && now() - this.outputChecked < 60000) return null;
    this.outputChecked = now();
    let r;
    try { r = await this.link.request("voice.output"); } catch { return null; }
    this.onOutput(r);
    return r;
  }

  stopSpeaking(taskId) {
    const victims = taskId ? this.playing.filter((x) => x.task === taskId) : this.playing;
    for (const x of victims) { try { x.src.stop(); } catch { } }
    this.playing = this.playing.filter((x) => !victims.includes(x));
    if (!this.playing.length) this.nextAt = 0;
    if (!this.playing.length && this.state === "speaking") this.set(this.listening ? "listening" : null);
  }

  bargeIn() {                                 // the user started talking over JARVIS
    for (const x of this.playing) this.dropped.add(x.task);
    this.stopSpeaking();
    this.set("interrupted");
    this.link.request("voice.interrupt").catch(() => {});
  }

  stopAll() {                                 // Esc / stop button
    for (const x of this.playing) this.dropped.add(x.task);
    this.stopSpeaking();
    if (this.seg) { this.seg = null; this.voiced = 0; }
    this.link.request("voice.interrupt").catch(() => {});
  }
}

function encodeWav(frames) {
  const n = frames.reduce((a, f) => a + f.length, 0);
  const buf = new ArrayBuffer(44 + n * 2);
  const v = new DataView(buf);
  const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); w(8, "WAVE"); w(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, 16000, true);
  v.setUint32(28, 32000, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, n * 2, true);
  let o = 44;
  for (const f of frames) for (let i = 0; i < f.length; i++, o += 2) v.setInt16(o, Math.max(-1, Math.min(1, f[i])) * 0x7fff, true);
  return new Blob([buf], { type: "audio/wav" });
}

function b64(blob) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(",")[1]);
    r.onerror = rej;
    r.readAsDataURL(blob);
  });
}
