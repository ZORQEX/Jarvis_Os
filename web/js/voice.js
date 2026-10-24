// Voice: local voice-activity detection, hands-free conversation, streamed speech playback, barge-in.
// The microphone is open only while listening is on (shown in the UI); only detected speech segments are
// sent for recognition. LISTENING is reported only while a real microphone stream is open.
import { setLevel } from "./companion.js";
import { Vad } from "./core/vad.js";

const now = () => performance.timeOrigin + performance.now();   // epoch ms, same clock as the core

export class Voice {
  constructor({ link, onState, onError, onTurn, onOutput, wake = false, client = "window" }) {
    this.link = link;
    // Wake listening (desktop companion): the mic stays open, every phrase is recognised on this computer and only
    // the ones starting with «Джарвис» (or a direct follow-up) are acted on — the core drops the rest unseen.
    this.wake = wake;
    // Echo cancellation / noise suppression / gain stay on everywhere. Turning them off to save ~2% of a core made the
    // companion deaf in practice and unable to be interrupted; with echo cancellation the person can talk over JARVIS.
    this.apm = true;
    this.client = client;
    this.onOutput = onOutput || (() => {});
    this.outputChecked = 0;
    this.onState = onState;
    this.onError = onError;
    this.onTurn = onTurn || (() => {});
    this.muted = localStorage.getItem("jarvis.micMuted") === "1";
    this.speakReplies = localStorage.getItem("jarvis.speak") === "1";
    // Pause that ends a phrase. 0.65 s cut the user off mid-command («Отправь» / «в Телеграме» / …): now 1.3 s.
    this.silenceMs = +(localStorage.getItem("jarvis.silenceMs2") || 1300);
    // Early end of phrase: after this pause the phrase is recognised already; a clearly finished one is answered
    // at once, a cut-off one («Отправь маме в…») still waits the full pause (saves ~0.7 s per answer).
    this.earlyMs = 600;
    // A phrase longer than this is cut and recognised anyway (wake listening: a TV or a long monologue must not keep
    // «Джарвис, …» waiting).
    this.maxMs = wake ? 12000 : 30000;
    this.probe = null; this.voiceFrames = 0;
    this.closeWhenQuiet = false;  // mic button: stay open while JARVIS answers, so it can be interrupted
    this.holdOpen = () => false;  // set by the app: JARVIS waits for the user's answer («да» / «нет», a question)
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
    if (!this.vad) this.vad = new Vad({ silenceMs: this.silenceMs, maxMs: this.maxMs });
    this.vad.reset();
    this.seg = null; this.preroll = []; this.segStart = 0;
    this.probe = null; this.voiceFrames = 0;
    this.duck(false);
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
      const apm = this.apm ?? true;           // echo cancellation / noise suppression / gain (WebRTC processing)
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: apm, noiseSuppression: apm, autoGainControl: apm, channelCount: 1 } });
    } catch (e) {
      this.onError(e.name === "NotAllowedError" ? "Нет доступа к микрофону" : "Микрофон не найден");
      return false;
    }
    // 16 kHz straight from the browser's own resampler: the worklet handles 3× fewer samples than at 48 kHz.
    try { this.ctx = new AudioContext({ sampleRate: 16000 }); } catch { this.ctx = new AudioContext(); }
    await this.ctx.audioWorklet.addModule("/static/js/vad-worklet.js");
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "vad-frames");
    this.node.port.onmessage = (e) => { if (e.data.batch) for (const f of e.data.batch) { if (this.node) this.frame(f); } else this.frame(e.data); };
    src.connect(this.node);
    this.resetVad();
    this.idleSince = now();
    this.set("listening");
    return true;
  }

  closeMic() {
    this.closeWhenQuiet = false;
    if (this.seg) this.active(false);
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
  frame({ rms, low = 1, pcm }) {
    const speaking = this.audio;
    // The level drives the companion's halo (a style change per frame). Waiting for «Джарвис» it is shown only while a
    // phrase is being recorded: an idle always-on mic must not redraw 50 times a second (measured: ~4% of a core).
    if (!this.wake || this.seg) { this.lvlTick = (this.lvlTick || 0) + 1; if (this.lvlTick % 2 === 0) setLevel(Math.min(1, rms * 8)); }
    else if (this.lvlShown) { setLevel(0); }
    this.lvlShown = !this.wake || !!this.seg;
    // While JARVIS waits for an answer the single-phrase mic stays open (up to 45 s), so «да» is never too late.
    const waiting = this.holdOpen() && now() - this.idleSince < 45000;
    if (!this.seg && this.closeWhenQuiet && !speaking && !waiting && this.state !== "transcribing" && this.state !== "synthesizing"
        && now() - this.idleSince > 6000 && now() - this.lastAudioEnd > 2000) {
      this.closeWhenQuiet = false; this.closeMic(); return;   // answer finished: the single-phrase mic turns off
    }
    const ev = this.vad.push({ rms, low }, speaking);
    if (!this.seg) {
      this.preroll.push(pcm);
      if (this.preroll.length > 20) this.preroll.shift();     // keep 400 ms before speech start
      if (ev === "start") this.startSegment();
      else if (this.conversation && !this.wake && now() - this.idleSince > 180000) {   // 3 min without speech: stop listening
        this.conversation = false; this.closeMic(); this.onError("Разговор завершён: 3 минуты тишины");
      }
      return;
    }
    this.seg.push(pcm);
    this.voiceFrames = this.vad.voiceMs / 20;
    // The person talks over JARVIS: its voice was turned down at once; after 0.4 s of real speech it stops and listens
    // (a cough or a door does not cut the answer off).
    if (this.ducked && this.vad.voiceMs >= 400) this.bargeIn();
    if (ev === "end" || ev === "max") { this.endSegment(); return; }
    const silent = this.vad.silent, dur = this.vad.dur;
    const early = Math.min(this.earlyMs, this.silenceMs - 400);
    if (!this.probe && early >= 300 && silent >= early && dur - silent >= 400) this.startProbe();
  }

  startSegment() {
    this.segStart = now() - this.preroll.length * 20;
    this.seg = this.preroll.slice(0);
    this.seg.barge = this.audio;               // started while JARVIS was talking: the person cut in
    this.preroll = [];
    if (this.audio) this.duck(true);
    this.set("recording");
    this.active(true);                         // the core does not start talking over the person meanwhile
    if (!this.wake) this.link.request("voice.warm").catch(() => {});   // warm the cloud connection while the user speaks
  }

  // Recognise the phrase during the pause. The user may go on talking: then the probe is simply dropped.
  startProbe() {
    const seg = this.seg, mark = this.voiceFrames;
    const probe = this.probe = { seg, mark, id: null, complete: false, speechEnd: now() - this.vad.silent };
    b64(encodeWav(seg.slice(0))).then((audio) => this.link.request("voice.probe", { audio_b64: audio, mime: "audio/wav", ...this.flags(seg) }, 15000))
      .then((r) => {
        probe.id = r.probe_id; probe.complete = !!r.complete;
        // Still the same phrase, nothing said since, and the phrase is finished: answer now.
        if (this.seg === seg && this.voiceFrames === mark && probe.complete) { this.vad.close(); this.endSegment(probe); }
      })
      .catch(() => { probe.failed = true; });
  }

  // What the core needs to know about a phrase: wake listening (must start with «Джарвис» or follow up), who heard it,
  // and whether the person cut JARVIS off (then no «Джарвис» is needed).
  flags(seg) {
    return { wake: this.wake && this.conversation, client: this.client, barge: !!seg?.barge };
  }

  async endSegment(viaProbe = null) {
    const frames = this.seg; this.seg = null;
    const probe = this.probe; this.probe = null;
    const speechEnd = viaProbe ? viaProbe.speechEnd : now() - this.vad.silent;
    const voicedMs = this.vad.voiceMs;
    if (!viaProbe && voicedMs < 200) { this.duck(false); this.active(false); this.set(this.listening ? "listening" : null); return; }
    // The probe already recognised exactly this phrase (no voice since): reuse it instead of a second recognition.
    const reuse = probe && probe.id && probe.seg === frames && probe.mark === this.voiceFrames ? probe.id : null;
    this.voiceFrames = 0;
    this.idleSince = now();
    if (this.once && !this.conversation) { this.once = false; this.closeWhenQuiet = true; }
    this.set("transcribing");
    this.talking = false;                      // the phrase itself tells the core the person stopped
    const times = { speech_start: this.segStart / 1000, speech_end: speechEnd / 1000 };
    try {
      let r = null;
      const flags = this.flags(frames);                // a pressed mic (not a conversation) needs no «Джарвис»
      if (reuse) r = await this.link.request("voice.commit", { probe_id: reuse, ...flags, ...times }, 45000).catch(() => null);
      if (!r) r = await this.link.request("voice.transcribe", { audio_b64: await b64(encodeWav(frames)), mime: "audio/wav",
        ...flags, ...times }, 45000);
      if (r.ignored) { this.duck(false); this.set(this.listening ? "listening" : (this.audio ? "speaking" : null)); return; }   // not for JARVIS
      if (this.ducked) this.bargeIn();                 // a short phrase over JARVIS («стоп», «хватит»): it was for JARVIS
      if (r.task_id && r.turn) this.turnOf.set(r.task_id, r.turn);
      if (r.turn) this.turnOf.set("notice-" + r.turn, r.turn);
      this.onTurn(r);
    } catch (e) {
      this.duck(false);
      this.active(false);
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
    if (!this.gain) { this.gain = this.out.createGain(); this.gain.connect(this.out.destination); }
    src.connect(this.gain);
    const at = Math.max(this.out.currentTime + 0.02, this.nextAt);
    src.start(at);
    this.nextAt = at + buf.duration;
    const item = { src, task: ev.task_id };
    this.playing.push(item);
    src.onended = () => {
      this.playing = this.playing.filter((x) => x !== item);
      this.lastAudioEnd = now();
      if (!this.playing.length) this.duck(false);
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

  // Turn JARVIS's voice down while the person may be cutting in (instantly, without stopping it yet).
  duck(on) {
    this.ducked = !!on && this.audio;
    if (this.gain && this.out) this.gain.gain.setTargetAtTime(this.ducked ? 0.15 : 1, this.out.currentTime, 0.03);
  }

  // The person started (true) or stopped (false) talking; the core holds JARVIS's next sentence meanwhile.
  active(on) {
    if (on === !!this.talking) return;
    this.talking = on;
    this.link.request("voice.active", { on, wake: this.wake && this.conversation }).catch(() => {});
  }

  heardTasks() { return [...new Set(this.playing.map((x) => x.task).filter(Boolean))]; }

  bargeIn() {                                 // the person talks over JARVIS: stop the voice and listen (the work goes on)
    this.ducked = false;
    const tasks = this.heardTasks();
    for (const x of this.playing) this.dropped.add(x.task);
    this.stopSpeaking();
    if (this.gain && this.out) this.gain.gain.setValueAtTime(1, this.out.currentTime);
    if (this.state !== "recording") this.set("interrupted");
    this.link.request("voice.barge", { tasks }).catch(() => {});
  }

  stopAll() {                                 // Esc / stop button: this answer stops, other tasks go on
    const tasks = this.heardTasks();
    for (const x of this.playing) this.dropped.add(x.task);
    this.stopSpeaking();
    this.duck(false);
    if (this.seg) { this.seg = null; this.vad.close(); this.active(false); }
    this.link.request("voice.interrupt", { tasks }).catch(() => {});
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
