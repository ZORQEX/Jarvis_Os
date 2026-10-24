// Desktop companion: JARVIS above every workspace and application. A small round window (Hyprland pins it to all
// workspaces); click to open the card, drag to move it anywhere on the screen (the core moves the window after the
// cursor), say «Джарвис, …» when wake listening is on. Same state machine and same core as the main window.
import { Link } from "./net.js";
import * as companion from "./companion.js";
import { Voice } from "./voice.js";
import { deriveAgent, flashFor, PHASES } from "./core/agent-state.js";
import { planRows, isUserTask, isWorking } from "./core/tasks-model.js";
import { ICON, esc, md } from "./render.js";

const $ = (s) => document.querySelector(s);
const ORB = 64, CARD = { w: 360, h: 470 };
const MARK = { done: "✓", active: "●", waiting: "!", failed: "✕", skipped: "–", pending: "○" };
const S = { connected: false, core: { state: "OFFLINE" }, tasks: {}, approvals: {}, streaming: {}, progress: {}, flash: null,
  messages: [], lastReply: null, voice: null, wake: false, expanded: false, autoOpened: false, own: new Set(), lastAttn: null };

// Must match jarvis/overlay.py title_for(): the core finds its own companion window by this title.
if (location.port && location.port !== "8791") document.title = `JARVIS · компаньон :${location.port}`;
const theme = localStorage.getItem("jarvis.theme");
if (theme) document.documentElement.dataset.theme = theme;
companion.mount($("#ovFace"));
companion.mount($("#ovMini"));
$("#ovMain").innerHTML = ICON.open;
$("#ovClose").innerHTML = ICON.minus;
$("#ovHide").innerHTML = ICON.eyeOff;
$("#ovTalk").innerHTML = ICON.mic;
$("#ovGo").innerHTML = ICON.arrowUp;

const link = new Link({
  onSnapshot: (d) => {
    // JARVIS restarted with a new interface: load it (never in the middle of a phrase).
    if (d.ui_build && S.build && d.ui_build !== S.build && !["recording", "transcribing"].includes(S.voice)) { location.reload(); return; }
    S.build = d.ui_build || S.build;
    // The window keeps its size across a page reload (e.g. a JARVIS restart while the card was open): match it.
    if (!S.sized) { S.sized = true; if (!S.expanded) link.request("overlay.size", { w: ORB, h: ORB }).catch(() => {}); }
    S.tasks = d.tasks; S.core = d.core; S.messages = d.messages.slice(-30);
    S.approvals = Object.fromEntries(d.approvals.map((a) => [a.id, a]));
    S.streaming = {};
    link.request("overlay.state").then(applySettings).catch(() => {});
    render();
  },
  onEvent: (ev) => onEvent(ev),
  onStatus: (ok) => { S.connected = ok; render(); },
});
const voice = new Voice({
  link, wake: true, client: "overlay",
  onState: (v) => { S.voice = v; render(); },
  onTurn: (r) => { if (r?.task_id) S.own.add(r.task_id); },
  onError: (m) => { S.error = m; render(); },
  onOutput: () => {},
});

function applySettings(st) {
  const want = !!st?.wake;
  S.wake = want;
  // The main window has the microphone (its conversation mode is on): wait until it lets go, even if the companion
  // started after it — two open microphones hear each other's voice.
  if (want && st?.window_listening) { S.yielded = true; if (voice.conversation) voice.toggleConversation(); render(); return; }
  if (want && !voice.conversation) voice.toggleConversation();          // mic open, «Джарвис» wakes it
  if (!want && voice.conversation) voice.toggleConversation();
  render();
}

function onEvent(ev) {
  const tid = ev.task_id;
  if (String(ev.kind).startsWith("speech.")) {                           // only answers to what was said here
    if (ev.owner !== "overlay") return;
    if (ev.kind === "speech.audio") voice.enqueue(ev);
    else if (ev.kind === "speech.stop" || ev.kind === "speech.cancelled") voice.stopSpeaking(tid);
    else if (ev.kind === "speech.synth" && !voice.audio) voice.set("synthesizing");
    else if (ev.kind === "speech.end" && !voice.audio && ["synthesizing", "error"].includes(S.voice)) voice.set(voice.listening ? "listening" : null);
    return;
  }
  switch (ev.kind) {
    case "task.created": case "task.updated": {
      const t = ev.task, prev = S.tasks[t.id];
      if (t.route === "ui_read") return;
      S.tasks[t.id] = t;
      if ((!prev || isWorking(prev)) && !isWorking(t)) { const f = flashFor(t, Date.now()); if (f) { S.flash = f; setTimeout(render, 2700); } }
      break;
    }
    case "step.updated": {
      const t = S.tasks[tid];
      if (!t) return;
      const i = t.steps.findIndex((x) => x.id === ev.step.id);
      if (i >= 0) t.steps[i] = ev.step; else t.steps.push(ev.step);
      break;
    }
    case "step.progress": S.progress[ev.step_id] = ev.message; break;
    case "approval.created": case "approval.updated": S.approvals[ev.approval.id] = ev.approval; break;
    case "assistant.delta": (S.streaming[tid] ||= { text: "" }).text += ev.text; break;
    case "message": {
      const m = ev.message;
      S.messages.push(m); S.messages = S.messages.slice(-30);
      if (m.role === "assistant" && m.task_id) {
        delete S.streaming[m.task_id];
        if (S.own.has(m.task_id) && isUserTask(S.tasks[m.task_id])) { S.lastReply = { text: m.text, taskId: m.task_id, at: Date.now() }; expand("reply"); }
      }
      break;
    }
    case "core.state": S.core = ev; break;
    case "wake.heard": expand("wake"); break;
    case "voice.presence":                     // the main window took the mic: pause, then listen again
      if (ev.client === "window") {
        if (ev.listening && voice.conversation) { S.yielded = true; voice.toggleConversation(); }
        else if (!ev.listening && S.yielded) { S.yielded = false; if (S.wake && !voice.conversation) voice.toggleConversation(); }
      }
      break;
    case "overlay.settings": applySettings(ev); break;
    case "dictation":                          // speech is typed into the person's window: the mic listens meanwhile
      S.dictation = ev.on ? ev : null;
      if (ev.on && !voice.conversation && !S.yielded) { S.dictationMic = true; voice.toggleConversation(); }
      if (!ev.on && S.dictationMic) { S.dictationMic = false; if (!S.wake && voice.conversation) voice.toggleConversation(); }
      break;
    case "conversation.new": link.request("snapshot").then((d) => link.onSnapshot(d)).catch(() => {}); return;
    default: return;
  }
  render();
}

// ---------------------------------------------------------------- open / close (the core resizes the window)
async function expand(reason = "user") {
  if (S.expanded) { if (reason === "user") S.autoOpened = false; return; }
  S.autoOpened = reason !== "user";
  // Grow the window first (no flash of a cut card); opened in a normal tab there is no window to grow — open anyway.
  try { await link.request("overlay.size", CARD); } catch { }
  S.expanded = true;
  $("#ov").dataset.expanded = "1";
  render();
  if (reason === "user" || reason === "wake") setTimeout(() => $("#ovInput").focus(), 50);
  scheduleCollapse();
}
async function collapse() {
  if (!S.expanded) return;
  S.expanded = false; S.autoOpened = false;
  $("#ov").dataset.expanded = "0";
  render();
  try { await link.request("overlay.size", { w: ORB, h: ORB }); } catch { }
}
let collapseTimer = 0, hover = false;
function scheduleCollapse() {
  clearTimeout(collapseTimer);
  if (!S.expanded || !S.autoOpened) return;
  collapseTimer = setTimeout(() => {
    const a = state();
    const busy = a.mode === "WAITING_CONFIRMATION" || a.busy || voice.recording;
    if (!hover && !busy && !document.activeElement?.closest?.(".ov-card input")) collapse(); else scheduleCollapse();
  }, 9000);
}
document.addEventListener("pointerenter", () => { hover = true; });
document.addEventListener("pointerleave", () => { hover = false; scheduleCollapse(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") collapse(); });

// ---------------------------------------------------------------- drag anywhere on the screen
function draggable(el, onClick) {
  let start = null, sentAt = 0;
  el.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.target.closest("button:not(.ov-orb), input")) return;
    el.setPointerCapture(e.pointerId);
    start = { x: e.clientX, y: e.clientY, sx: e.screenX, sy: e.screenY, moved: false };
  });
  el.addEventListener("pointermove", (e) => {
    if (!start) return;
    // screenX moves with the pointer even though the window moves under it; window coordinates would not.
    if (!start.moved && Math.hypot(e.screenX - start.sx, e.screenY - start.sy) < 4 && Math.hypot(e.clientX - start.x, e.clientY - start.y) < 4) return;
    if (!start.moved) {
      start.moved = true;
      $("#ov").dataset.dragging = "1";
      link.request("overlay.drag", { phase: "start", ox: start.x, oy: start.y }).catch(() => {});
    }
    if (Date.now() - sentAt > 120) { sentAt = Date.now(); link.request("overlay.drag", { phase: "move" }).catch(() => {}); }
  });
  const end = () => {
    if (!start) return;
    const moved = start.moved;
    start = null;
    delete $("#ov").dataset.dragging;
    if (moved) link.request("overlay.drag", { phase: "end" }).catch(() => {});
    else onClick?.();
  };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
}
draggable($("#ovOrb"), () => expand("user"));
draggable($("#ovHead"), null);
$("#ovClose").addEventListener("click", collapse);
$("#ovHide").addEventListener("click", () => link.request("overlay.close").catch(() => {}));
$("#ovMain").addEventListener("click", () => link.request("window.open").catch(() => {}));
$("#ovWake").addEventListener("click", async () => {
  try { applySettings(await link.request("overlay.settings", { wake: !S.wake })); } catch { }
});
$("#ovTalk").addEventListener("click", () => {
  if (S.wake) return;                         // already listening for «Джарвис»
  voice.listenOnce();
});

// ---------------------------------------------------------------- ask
const input = $("#ovInput"), go = $("#ovGo");
input.addEventListener("input", renderGo);
$("#ovAsk").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  const a = state();
  if (!text) { if (a.canStop) stop(a); return; }
  input.value = ""; renderGo();
  try { const r = await link.request("submit", { text, source: "text" }, 15000); if (r?.task_id) S.own.add(r.task_id); }
  catch (er) { S.error = er.message; render(); }
});
async function stop(a) {
  if (a.mode === "SPEAKING" || voice.audio) voice.stopAll();
  if (a.taskId && a.mode !== "SPEAKING") link.request("cancel", { task_id: a.taskId }).catch(() => {});
}
function renderGo() {
  const a = state();
  const stopMode = !input.value.trim() && a.canStop && a.mode !== "WAITING_CONFIRMATION";
  go.disabled = !input.value.trim() && !stopMode;
  go.innerHTML = stopMode ? ICON.stop : ICON.arrowUp;
  go.className = `icon-btn ${stopMode ? "stop" : "go"}`;
  go.setAttribute("aria-label", stopMode ? "Остановить" : "Отправить");
}

$("#ovMainBody").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-o]");
  if (!b) return;
  const a = state();
  b.disabled = true;
  try {
    if (b.dataset.o === "approve") await link.request("approve", { id: b.dataset.id, edited: null, remember: false });
    if (b.dataset.o === "deny") await link.request("deny", { id: b.dataset.id });
    if (b.dataset.o === "stop") await stop(a);
    if (b.dataset.o === "main") await link.request("window.open");
    if (b.dataset.o === "estop_clear") await link.request("estop_clear");
  } catch (er) { S.error = er.message; }
  b.disabled = false;
  render();
});

// ---------------------------------------------------------------- render
function state() {
  return deriveAgent({ connected: S.connected, core: S.core, voice: S.voice, micOpen: voice.listening, muted: false,
    tasks: Object.values(S.tasks), approvals: Object.values(S.approvals), streaming: S.streaming, progress: S.progress,
    flash: S.flash, now: Date.now() });
}

function body(a) {
  if (a.mode === "STANDBY") return `<div class="row-btns"><button class="btn" data-o="estop_clear">${ICON.play} Снять остановку</button></div>`;
  if (a.mode === "WAITING_CONFIRMATION") {
    return `${a.quote ? `<div class="quote">${esc(a.quote)}</div>` : ""}
      <div class="row-btns"><button class="btn warn" data-o="approve" data-id="${esc(a.approvalId)}">${ICON.check} ${a.send ? "Отправить" : "Разрешить"}</button>
      <button class="btn ghost" data-o="deny" data-id="${esc(a.approvalId)}">Отклонить</button>
      <button class="btn ghost" data-o="main">Подробнее</button></div>
      <div class="hint">или скажите «да» / «нет»</div>`;
  }
  const t = a.taskId ? S.tasks[a.taskId] : null;
  if (a.busy && t) {
    const rows = planRows(t).slice(-5);
    return `${rows.map((r) => `<div class="plan-row" data-mark="${r.mark}"><span class="m">${MARK[r.mark]}</span><span>${esc(r.label)}</span></div>`).join("")}
      ${a.canStop ? `<div class="row-btns"><button class="btn danger" data-o="stop">${ICON.stop} Остановить</button></div>` : ""}`;
  }
  const r = S.lastReply;
  if (r && Date.now() - r.at < 10 * 60000) {
    return `<div class="reply">${md(r.text.length > 1200 ? r.text.slice(0, 1200) + "…" : r.text)}</div>
      <div class="row-btns"><button class="btn ghost" data-o="main">Открыть в JARVIS</button></div>`;
  }
  return `<div class="hint">${S.wake ? "Слушаю: начните фразу со слова «Джарвис»." : "Спросите здесь или включите микрофон — тогда достаточно сказать «Джарвис, …»."}
    Перетащите меня в любое место экрана.</div>`;
}

let lastSig = "";
function render() {
  const a = state();
  const pending = Object.values(S.approvals).filter((x) => x.status === "pending").length;
  // A decision or an error needs the person: open the card (once per event).
  const attn = a.mode === "WAITING_CONFIRMATION" ? "a:" + a.approvalId : a.mode === "ERROR" ? "e:" + a.taskId : null;
  if (attn && attn !== S.lastAttn) { S.lastAttn = attn; if (!S.expanded) expand("attention"); }
  companion.setState(a.mode, { muted: false });
  const ov = $("#ov");
  ov.dataset.mode = a.mode;
  $("#ovMic").hidden = !voice.listening;
  $("#ovBadge").hidden = !(pending && !S.expanded);
  $("#ovBadge").textContent = pending;
  $("#ovOrb").setAttribute("aria-label", `JARVIS: ${a.label}`);
  $("#ovOrb").title = `JARVIS · ${a.label}${a.detail ? " — " + a.detail : ""}\nНажмите — открыть, перетащите — переместить`;
  $("#ovWake").innerHTML = S.wake ? ICON.mic : ICON.micOff;
  $("#ovWake").setAttribute("aria-pressed", String(S.wake));
  $("#ovWake").title = S.wake ? "Слушаю «Джарвис» — выключить микрофон" : "Слушать «Джарвис» (микрофон, распознавание на этом компьютере)";
  $("#ovTalk").hidden = S.wake;
  $("#ovTalk").classList.toggle("recording", voice.recording);
  if (!S.expanded) return;
  const sig = JSON.stringify([a, S.lastReply?.at, S.wake, S.error, a.taskId && planRows(S.tasks[a.taskId] || { steps: [] })]);
  if (sig === lastSig) return;
  lastSig = sig;
  $("#ovLabel").textContent = a.label + (a.count > 1 ? ` (${a.count})` : "");
  $("#ovSub").textContent = S.dictation ? `диктовка${S.dictation.title ? " → " + S.dictation.title : ""}: «стоп диктовка»`
    : S.wake ? "микрофон: жду «Джарвис»" : S.connected ? "JARVIS" : "нет связи с ядром";
  $("#ovDetail").textContent = S.error || a.detail || "";
  S.error = "";
  const ph = $("#ovPhases"), idx = PHASES.findIndex(([k]) => k === a.phase);
  ph.hidden = idx < 0 || a.mode === "SPEAKING";
  if (!ph.hidden) ph.innerHTML = PHASES.map(([k, l], i) => `<span class="${i < idx ? "done" : i === idx ? "on" : ""}">${l}</span>`).join("<i></i>");
  $("#ovMainBody").innerHTML = body(a);
  renderGo();
  scheduleCollapse();
}
setInterval(() => { if (S.flash) render(); }, 800);
window.__overlay = { S, link, voice, expand, collapse };   // for automated checks
