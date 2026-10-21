import { Link } from "./net.js";
import * as companion from "./companion.js";
import { esc, md, ICON, STATE_RU, stepRow, resultCard, approvalCard, toolTitle, fmtMs, fmtWhen, fmtTime, shortPath, decisionCard } from "./render.js";
import { agencyPanel, wireAgency } from "./agency-ui.js";
import { Voice } from "./voice.js";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const S = {
  connected: false, convId: null, messages: [], tasks: {}, approvals: {}, core: { state: "OFFLINE" },
  provider: null, reminders: [], recent: [], streaming: {}, progress: {}, tools: [], grants: [],
  view: "chat", voice: null, firstSnapshot: true, providers: [], dev: localStorage.getItem("jarvis.dev") === "1",
};

// ------------------------------------------------------------------ boot
const theme = localStorage.getItem("jarvis.theme");
if (theme) document.documentElement.dataset.theme = theme;
if (localStorage.getItem("jarvis.reduceMotion") === "1") document.documentElement.classList.add("anim-off");

companion.mount($("#railCompanion"));

const link = new Link({
  onSnapshot: (d) => { applySnapshot(d); },
  onEvent: (ev) => onEvent(ev),
  onStatus: (ok) => { S.connected = ok; if (!ok) toast("Нет связи с ядром JARVIS — переподключаюсь…", "warn"); renderStatus(); },
});

const voice = new Voice({
  link,
  onState: (v) => {
    S.voice = v; renderStatus(); renderComposer(); renderPrivacy();
    if (v === "interrupted") setTimeout(() => { if (S.voice === "interrupted") voice.set(voice.listening ? "listening" : null); }, 700);
  },
  onTurn: () => { if (S.view !== "chat") nav("chat"); },
  onError: (m) => toast(m, "warn"),
  onOutput: (r) => {
    S.audioOut = r;
    const w = outputProblem(r);
    if (w) toast(w + " — откройте Настройки → Голос, там есть кнопка «Сделать громче».", "warn");
    if (S.view === "settings") renderAudioOut();
  },
});

function outputProblem(r) {
  if (!r?.available) return "";
  if (r.sink_muted) return "Звук компьютера выключен";
  if (r.stream_found && r.muted) return "Звук JARVIS выключен в системном микшере";
  if (r.stream_found && r.volume_pct != null && r.volume_pct < 20) return `Громкость JARVIS в системе всего ${r.volume_pct} %`;
  if (r.sink_volume_pct != null && r.sink_volume_pct < 10) return `Общая громкость компьютера всего ${r.sink_volume_pct} %`;
  return "";
}

function applySnapshot(d) {
  S.convId = d.conversation_id;
  S.messages = d.messages;
  S.tasks = d.tasks;
  S.approvals = Object.fromEntries(d.approvals.map((a) => [a.id, a]));
  S.core = d.core;
  S.provider = d.provider;
  S.providers = d.providers || [];
  S.reminders = d.reminders;
  S.recent = d.recent_tasks;
  S.tools = d.tools;
  S.grants = d.session_grants;
  S.streaming = {};
  turnCache.clear();
  $("#thread").innerHTML = "";
  renderAll();
  if (S.firstSnapshot) { scrollToEnd(false); S.firstSnapshot = false; }
}

// ------------------------------------------------------------------ events
let floraMod = null, pendingFlora = null;
function onEvent(ev) {
  floraMod?.onEvent(ev);
  commsMod?.onEvent(ev);
  const tid = ev.task_id;
  switch (ev.kind) {
    case "message": {
      const m = ev.message;
      if (m.conversation_id !== S.convId) return;
      if (S.messages.some((x) => x.id === m.id)) return;
      S.messages.push(m);
      if (m.role === "assistant" && m.task_id) delete S.streaming[m.task_id];
      renderThread();
      break;
    }
    case "task.created": case "task.updated": {
      const t = ev.task;
      S.tasks[t.id] = t;
      const i = S.recent.findIndex((r) => r.id === t.id);
      const row = { id: t.id, request: t.request, state: t.state, created_at: t.created_at, route: t.route };
      if (i >= 0) S.recent[i] = row; else S.recent.unshift(row);
      S.recent = S.recent.slice(0, 12);
      renderThread(); renderSide();
      if (S.view === "tasks") renderTasksView();
      break;
    }
    case "step.updated": {
      const st = ev.step, t = S.tasks[tid];
      if (!t) return;
      const i = t.steps.findIndex((x) => x.id === st.id);
      if (i >= 0) t.steps[i] = st; else t.steps.push(st);
      if (!["running"].includes(st.state)) delete S.progress[st.id];
      renderThread(); renderSide();
      break;
    }
    case "step.progress": S.progress[ev.step_id] = ev.message; renderThread(); break;
    case "approval.created": case "approval.updated": {
      S.approvals[ev.approval.id] = ev.approval;
      renderThread(); renderSide();
      if (ev.kind === "approval.created") { notifyAttention(); }
      break;
    }
    case "assistant.delta": {
      const s = S.streaming[tid] || (S.streaming[tid] = { text: "" });
      s.text += ev.text;
      updateStreaming(tid);
      break;
    }
    case "core.state": S.core = ev; renderStatus(); renderSide(); break;
    case "reminder.created": S.reminders.push(ev.reminder); renderSide(); break;
    case "reminder.fired": {
      S.reminders = S.reminders.filter((r) => r.id !== ev.reminder.id);
      toast(`⏰ ${ev.body}`, ev.late ? "warn" : "");
      renderSide();
      break;
    }
    case "estop": toast(ev.active ? "Экстренная остановка включена" : "Экстренная остановка снята", ev.active ? "err" : ""); break;
    case "speech.stop": voice.stopSpeaking(); break;
    case "speech.synth": if (!voice.audio && !["recording", "transcribing"].includes(S.voice)) voice.set("synthesizing"); break;
    case "speech.audio": voice.enqueue(ev); break;
    case "speech.end": if (!voice.audio && ["synthesizing", "error"].includes(S.voice)) voice.set(voice.listening ? "listening" : null); break;
    case "speech.cancelled": voice.stopSpeaking(tid); if (S.voice === "synthesizing") voice.set(voice.listening ? "listening" : null); break;
    case "speech.error":
      toast("Ответ не удалось озвучить — он показан текстом. Причина: " + ev.error, "warn");
      if (!voice.audio) voice.set("error");
      setTimeout(() => { if (S.voice === "error") voice.set(voice.listening ? "listening" : null); }, 2500);
      break;
    case "conversation.new": link.request("snapshot").then(applySnapshot).catch(() => {}); break;
    case "ui.navigate": pendingFlora = { tab: ev.tab }; nav(ev.view); break;
    case "providers": S.providers = ev.providers; S.provider = ev.provider; renderStatus(); renderComposer();
      if (S.view === "settings" && !document.activeElement?.closest?.(".pcard")) renderSettings(); break;
    case "task.decision": {
      const t = S.tasks[tid];
      if (t) { t.decision = ev.decision; renderThread(); }
      break;
    }
  }
}

function notifyAttention() {
  if (document.hidden && "Notification" in window && Notification.permission === "granted") {
    new Notification("JARVIS: нужно ваше решение");
  }
}

// ------------------------------------------------------------------ rendering: status
const STATUS_RU = { SYNTHESIZING: "Готовлю голос…", INTERRUPTED: "Перебито — слушаю", IDLE: "Готов", STANDBY: "Экстренная остановка — действия заблокированы", LISTENING: "Слушаю…",
  PROCESSING: "Работаю…", THINKING: "Думаю…", EXECUTING: "Выполняю…", SPEAKING: "Говорю…",
  WAITING_APPROVAL: "Нужно ваше решение", PAUSED: "Пауза", MUTED: "Готов · микрофон выключен", COMPLETED: "Готово",
  ERROR: "Не получилось", OFFLINE: "Нет связи с ядром" };

function uiState() {
  if (!S.connected) return "OFFLINE";
  if (S.voice === "recording") return "LISTENING";
  if (S.voice === "interrupted") return "INTERRUPTED";
  if (S.voice === "transcribing") return "PROCESSING";
  if (S.voice === "speaking") return "SPEAKING";
  if (S.voice === "synthesizing") return "SYNTHESIZING";
  if (S.voice === "error") return "ERROR";
  if (S.voice === "listening" && ["IDLE", "COMPLETED"].includes(S.core.state)) return "LISTENING";
  const st = S.core.state;
  if (st === "IDLE" && voice.muted) return "MUTED";
  return st;
}

function renderStatus() {
  const st = uiState();
  companion.setState(st === "SYNTHESIZING" ? "PROCESSING" : st, { muted: voice.muted });
  const el = $("#status");
  el.dataset.state = st;
  let label = STATUS_RU[st] || st;
  if (st === "LISTENING") label = S.voice === "recording" ? "Слушаю…" : "Жду вашу фразу";
  if (S.voice === "transcribing") label = "Распознаю речь…";
  if (S.core.working && ["EXECUTING", "THINKING", "PROCESSING"].includes(st)) label = `${S.core.working} работает…`;
  if (st === "ERROR" && S.voice === "error") label = "Звук ответа не получился — текст на экране";
  if (st === "WAITING_APPROVAL" && S.core.pending_approvals > 1) label += ` (${S.core.pending_approvals})`;
  $(".status-label", el).textContent = label;
  $("#app").dataset.estop = S.core.estop ? "1" : "0";
  document.title = st === "WAITING_APPROVAL" ? "● JARVIS" : "JARVIS";
  renderChips();
}

function renderChips() {
  const chips = [];
  if (S.core.estop) chips.push(`<button class="chip err" data-act="estop_clear">${ICON.stop} Остановлено · снять</button>`);
  if (voice.listening) chips.push(`<button class="chip live" data-act="conv">${ICON.mic} ${voice.conversation ? "Разговор: микрофон включён" : "Слушаю одну фразу"}</button>`);
  if (S.providers.length) {
    const up = S.providers.filter((p) => p.health === "CONNECTED");
    const soft = S.providers.filter((p) => p.health === "DEGRADED");
    const names = [...up, ...soft].map((p) => p.name);
    const cls = up.length ? "ok" : soft.length ? "warn" : "";
    const label = names.length ? `ИИ: ${names.join(" · ")}` : "ИИ не подключён";
    chips.push(`<button class="chip ${cls}" data-nav="settings" title="AI-агентство">${ICON.spark} ${esc(label)}</button>`);
  }
  const prof = { AUTHORIZED_CLOUD: "Облако разрешено", PRIVATE_LOCAL: "Приватно: спрашивать", STRICT_OFFLINE: "Строго офлайн" }[S.core.privacy];
  if (prof) chips.push(`<button class="chip" data-nav="settings">${ICON.lock} ${prof}</button>`);
  $("#chips").innerHTML = chips.join("");
}

// ------------------------------------------------------------------ rendering: thread
const turnCache = new Map();   // key -> {el, sig}

function buildTurns() {
  const turns = [], byTask = new Map();
  for (const m of S.messages) {
    if (m.task_id) {
      let t = byTask.get(m.task_id);
      if (!t) { t = { key: m.task_id, task_id: m.task_id, user: null, replies: [] }; byTask.set(m.task_id, t); turns.push(t); }
      if (m.role === "user") t.user = m; else t.replies.push(m);
    } else {
      turns.push({ key: m.id, task_id: null, user: m.role === "user" ? m : null, replies: m.role === "user" ? [] : [m] });
    }
  }
  return turns;
}

function renderThread() {
  if (S.view !== "chat") return;
  const thread = $("#thread");
  const turns = buildTurns();
  if (!turns.length) {
    if (!$(".hero", thread)) {
      turnCache.clear();
      thread.innerHTML = heroHtml();
      companion.mount($(".hero-companion", thread));
    }
    return;
  }
  $(".hero", thread)?.remove();
  const near = nearBottom();
  const keys = new Set();
  let prev = null;
  for (const t of turns) {
    keys.add(t.key);
    const task = t.task_id ? S.tasks[t.task_id] : null;
    const approvals = t.task_id ? Object.values(S.approvals).filter((a) => a.task_id === t.task_id) : [];
    const sig = JSON.stringify([t.user?.id, t.replies.map((r) => r.id), task?.state, task?.steps, task?.decision, S.dev,
      approvals.map((a) => [a.id, a.status]), task?.steps?.map((s) => S.progress[s.id])]);
    let c = turnCache.get(t.key);
    if (!c) {
      const el = document.createElement("div");
      el.className = "turn";
      el.dataset.key = t.key;
      c = { el, sig: null };
      turnCache.set(t.key, c);
    }
    if (c.sig !== sig) {
      c.el.innerHTML = turnHtml(t, task, approvals);
      c.sig = sig;
      if (t.task_id && S.streaming[t.task_id]) updateStreaming(t.task_id, false);
    }
    const want = prev ? prev.nextSibling : thread.firstChild;
    if (c.el !== want) thread.insertBefore(c.el, want);
    prev = c.el;
  }
  for (const [k, c] of turnCache) if (!keys.has(k)) { c.el.remove(); turnCache.delete(k); }
  if (near) scrollToEnd();
}

function heroHtml() {
  const h = new Date().getHours();
  const greet = h < 5 ? "Доброй ночи" : h < 12 ? "Доброе утро" : h < 18 ? "Добрый день" : "Добрый вечер";
  const ideas = ["Что важного по Флоре?", "Состояние системы", "Найди файл смета", "Громкость 40",
    "Запиши заметку: позвонить фабрике", "Напомни через 10 минут проверить почту", "Сделай скриншот", "Что ты умеешь?"];
  return `<div class="hero"><div class="hero-companion"></div><h1>${greet}.</h1>
    <p>Скажите или напишите, что нужно сделать. Простые команды выполняются сразу, на этом компьютере.</p>
    <div class="suggestions">${ideas.map((i) => `<button class="suggestion" data-suggest="${esc(i)}">${esc(i)}</button>`).join("")}</div></div>`;
}

function turnHtml(t, task, approvals) {
  const parts = [];
  if (t.user) {
    const src = t.user.meta?.source === "voice" ? `<span class="src" title="Голосом">${ICON.mic}</span>` : "";
    parts.push(`<div class="msg-user">${src}${esc(t.user.text)}</div>`);
  }
  const body = [];
  if (task) {
    const visible = task.steps.filter((s) => s.tool_id !== "cloud_send" && !["awaiting_approval", "resumed"].includes(s.state)
      && (s.tool_id !== "agent_chat" || S.dev || ["failed", "cancelled"].includes(s.state)));   // plain answers need no step row
    if (task.decision) body.push(decisionCard(task.decision, S.dev));
    for (const a of approvals) if (a.status !== "pending") body.push(approvalCard(a));
    if (visible.length) {
      body.push(`<div class="steps">${visible.map((s) => stepRow(s, S.progress[s.id])).join("")}</div>`);
      for (const s of visible) { const c = resultCard(s); if (c) body.push(c); }
    }
    for (const a of approvals) if (a.status === "pending") body.push(approvalCard(a));
  }
  for (const r of t.replies) {
    const err = r.meta?.route === "error" || r.meta?.error_kind;
    body.push(`<div class="msg-ai ${err ? "error" : ""} ${t.user ? "" : "standalone"}">${md(r.text)}</div>`);
  }
  const active = task && !["completed", "failed", "cancelled", "blocked"].includes(task.state);
  if (task && active && !t.replies.length) {
    body.push(`<div class="msg-ai" data-stream="${esc(task.id)}" hidden></div>`);
    const busy = task.steps.some((s) => ["running", "verifying", "awaiting_approval"].includes(s.state));
    if (!busy && task.state !== "waiting_for_approval") {
      body.push(`<div class="typing" data-typing="${esc(task.id)}"><i></i><i></i><i></i><span>${task.state === "paused" ? "пауза" : task.route === "ai" ? "думаю" : "работаю"}</span></div>`);
    }
  }
  if (task && task.state === "blocked") {
    body.push(`<div class="msg-ai faint">Задача прервана перезапуском JARVIS. ${esc(task.error || "")}</div>`);
  }
  const meta = metaLine(t, task);
  if (meta) body.push(meta);
  if (body.length) parts.push(`<div class="assistant">${body.join("")}</div>`);
  return parts.join("");
}

function metaLine(t, task) {
  const r = t.replies[t.replies.length - 1];
  if (!r || !task) return "";
  const m = r.meta || {};
  const bits = [];
  if (m.route === "ai") {
    bits.push(`<span class="cloud">${ICON.cloud.replace("<svg", '<svg style="width:13px;height:13px;vertical-align:-2px"')} ${esc(m.model || "Gemini")}</span>`);
    if (m.first_token_ms) bits.push(`первый ответ ${fmtMs(m.first_token_ms)}`);
    if (m.usage?.total_token_count) bits.push(`${m.usage.total_token_count} токенов`);
  } else if (m.route === "agency") {
    const names = { gemini: "Gemini", claude_code: "Claude Code" };
    if (m.providers?.length) bits.push(`<span class="cloud">${ICON.spark.replace("<svg", '<svg style="width:13px;height:13px;vertical-align:-2px"')} ${esc(m.providers.map((p) => names[p] || p).join(" → "))}</span>`);
    else bits.push("без ИИ");
  } else if (m.route === "local" || m.route === "flora") {
    bits.push(m.route === "flora" ? "демо-данные · локально" : "локально, без ИИ");
  }
  if (task.finished_at) bits.push(fmtMs((task.finished_at - task.created_at) * 1000));
  if (!bits.length) return "";
  return `<div class="meta">${bits.join('<span class="sep">·</span>')}</div>`;
}

let streamRaf = 0;
const streamDirty = new Set();
function updateStreaming(tid, schedule = true) {
  streamDirty.add(tid);
  if (!schedule) return flushStream();
  if (!streamRaf) streamRaf = requestAnimationFrame(flushStream);
}
function flushStream() {
  streamRaf = 0;
  const near = nearBottom();
  for (const tid of streamDirty) {
    const el = document.querySelector(`[data-stream="${CSS.escape(tid)}"]`);
    const s = S.streaming[tid];
    if (!el || !s) continue;
    el.hidden = false;
    el.classList.add("streaming");
    el.innerHTML = md(s.text);
    document.querySelector(`[data-typing="${CSS.escape(tid)}"]`)?.remove();
  }
  streamDirty.clear();
  if (near) scrollToEnd();
}

const scroller = $("#scroller");
function nearBottom() { return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120; }
function scrollToEnd(smooth = true) {
  requestAnimationFrame(() => scroller.scrollTo({ top: scroller.scrollHeight, behavior: smooth ? "smooth" : "auto" }));
}

// ------------------------------------------------------------------ rendering: side panel
function renderSide() {
  const active = Object.values(S.tasks).filter((t) => !["completed", "failed", "cancelled"].includes(t.state))
    .sort((a, b) => a.created_at - b.created_at);
  $("#activeTasks").innerHTML = active.length ? active.map(taskCard).join("")
    : `<div class="empty-note">Нет активных задач</div>`;
  const recent = S.recent.filter((r) => ["completed", "failed", "cancelled"].includes(r.state)).slice(0, 6);
  $("#recentTasks").innerHTML = recent.length ? recent.map((r) => `<div class="task"><div class="top">
      <span class="req" title="${esc(r.request)}">${esc(r.request)}</span><span class="pill ${esc(r.state)}">${esc(STATE_RU[r.state] || r.state)}</span></div></div>`).join("")
    : `<div class="empty-note">Пока пусто</div>`;
  const rem = S.reminders.filter((r) => r.status === "scheduled");
  $("#remindersSection").hidden = !rem.length;
  $("#reminders").innerHTML = rem.map((r) => `<div class="task"><div class="top">${ICON.clock}
      <span class="req">${esc(r.text)}</span><span class="pill">${fmtWhen(r.due_at)}</span></div></div>`).join("");
  renderPrivacy();
}

function taskCard(t) {
  const running = t.steps.find((s) => ["running", "verifying"].includes(s.state));
  const canPause = ["running", "planning"].includes(t.state);
  const canResume = t.state === "paused";
  const sub = t.state === "blocked" ? "прервана перезапуском"
    : running ? `${toolTitle(running.tool_id)} · ${S.progress[running.id] || "выполняю"}`
    : t.state === "waiting_for_approval" ? "нужно ваше решение" : "";
  return `<div class="task"><div class="top">
      <span class="req" title="${esc(t.request)}">${esc(t.request)}</span>
      <span class="ctl">
        ${canPause ? `<button class="icon-btn" data-task-act="pause" data-task="${esc(t.id)}" aria-label="Пауза" title="Пауза">${ICON.pause}</button>` : ""}
        ${canResume ? `<button class="icon-btn" data-task-act="resume" data-task="${esc(t.id)}" aria-label="Продолжить" title="Продолжить">${ICON.play}</button>` : ""}
        <button class="icon-btn" data-task-act="cancel" data-task="${esc(t.id)}" aria-label="Отменить" title="Отменить">${ICON.x}</button>
      </span></div>
      <div class="sub"><span class="pill ${esc(t.state)}">${esc(STATE_RU[t.state] || t.state)}</span><span>${esc(sub)}</span></div></div>`;
}

function renderPrivacy() {
  const mic = voice.listening ? `<b class="on">${voice.conversation ? "включён (разговор)" : "включён"}</b>` : voice.muted ? "<b>выключен</b>" : `<b>не используется</b>`;
  const cloud = { AUTHORIZED_CLOUD: "по запросу", PRIVATE_LOCAL: "только с согласия", STRICT_OFFLINE: "выключено" }[S.core.privacy] || "—";
  $("#privacy").innerHTML = `
    <div class="line">${voice.muted ? ICON.micOff : ICON.mic} Микрофон ${mic}</div>
    <div class="line">${ICON.cloud} Отправка в Gemini <b>${cloud}</b></div>
    <div class="line">${ICON.image} Камера <b>не используется</b></div>
    <div class="line">${ICON.shield} Подтверждения <b class="ok">включены</b></div>
    <button class="btn ghost" data-act="toggle-mute" style="align-self:flex-start;margin-top:2px">${voice.muted ? ICON.mic + " Включить микрофон" : ICON.micOff + " Выключить микрофон"}</button>`;
}

// ------------------------------------------------------------------ composer
const input = $("#input");
function renderComposer() {
  const mic = $("#micBtn"), conv = $("#convBtn");
  const aiReady = S.provider?.configured && S.core.privacy === "AUTHORIZED_CLOUD";
  mic.classList.toggle("recording", voice.listening && !voice.conversation);
  mic.classList.toggle("busy", S.voice === "transcribing");
  conv.classList.toggle("on", voice.conversation);
  for (const b of [mic, conv]) b.disabled = voice.muted || !voice.supported || !aiReady;
  const why = voice.muted ? "Микрофон выключен" : !S.provider?.configured ? "Голос требует подключённого Gemini"
    : !aiReady ? "Голос распознаётся в облаке — включите режим «Облако разрешено»" : "";
  mic.title = why || "Сказать одну фразу (Ctrl+Space)";
  conv.title = why || (voice.conversation ? "Закончить разговор (Ctrl+Shift+Space)" : "Разговор без кнопок (Ctrl+Shift+Space)");
  $("#sendBtn").disabled = !input.value.trim();
  const hint = S.voice === "recording" ? "Говорите… фраза закончится сама после паузы · <span class=\"kbd\">Esc</span> — отмена"
    : S.voice === "transcribing" ? "Распознаю речь…"
    : S.voice === "speaking" ? "JARVIS говорит — начните говорить, чтобы перебить, или <span class=\"kbd\">Esc</span>"
    : voice.conversation ? "Разговор: просто говорите. Пауза — конец фразы. Скажите «стоп», чтобы перебить."
    : `<span><span class="kbd">Enter</span> отправить</span><span><span class="kbd">Ctrl Space</span> фраза</span><span><span class="kbd">Ctrl Shift Space</span> разговор</span><span><span class="kbd">Ctrl Shift X</span> экстренный стоп</span>`;
  $("#composerHint").innerHTML = hint;
}

function autosize() { input.style.height = "auto"; input.style.height = Math.min(180, input.scrollHeight) + "px"; }
input.addEventListener("input", () => { autosize(); $("#sendBtn").disabled = !input.value.trim(); });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
});
$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = ""; autosize();
  submit(text, "text");
});
$("#micBtn").addEventListener("click", () => voice.listenOnce());
$("#convBtn").addEventListener("click", () => voice.toggleConversation());

async function submit(text, source) {
  if (S.view !== "chat") nav("chat");
  try {
    await link.request("submit", { text, source, speak: voice.speakReplies || voice.conversation }, 15000);
  } catch (e) {
    toast(e.message, "err");
    if (source === "text" && !input.value) { input.value = text; autosize(); }
  }
  renderComposer();
}

// ------------------------------------------------------------------ global clicks & keys
document.addEventListener("click", async (e) => {
  const t = e.target.closest("button, [data-open], [data-url]");
  if (!t) return;
  if (t.dataset.nav) {
    if (t.dataset.floraTab || t.dataset.floraItem) pendingFlora = { tab: t.dataset.floraTab, item: t.dataset.floraItem };
    return nav(t.dataset.nav);
  }
  if (t.dataset.suggest) return submit(t.dataset.suggest, "text");
  if (t.id === "railCompanion") { nav("chat"); input.focus(); return; }
  if (t.id === "estopBtn") return estop();
  if (t.id === "sideToggle") return toggleSide();
  if (t.dataset.open) return link.request("tool.run", { tool: "open_path", args: { path: t.dataset.open } }).catch((er) => toast(er.message, "err"));
  if (t.dataset.url) return link.request("tool.run", { tool: "open_url", args: { url: t.dataset.url } }).catch((er) => toast(er.message, "err"));
  if (t.dataset.approve) {
    const card = t.closest("[data-approval]");
    const a = S.approvals[t.dataset.approve];
    const edited = {};
    let changed = false;
    $$("[data-edit]", card).forEach((ta) => { edited[ta.dataset.edit] = ta.value; if (ta.value !== a.args[ta.dataset.edit]) changed = true; });
    const remember = $("[data-remember]", card)?.checked;
    t.disabled = true;
    return link.request("approve", { id: a.id, edited: changed ? { ...a.args, ...edited } : null, remember })
      .catch((er) => { toast(er.message, "err"); t.disabled = false; });
  }
  if (t.dataset.deny) return link.request("deny", { id: t.dataset.deny }).catch((er) => toast(er.message, "err"));
  if (t.dataset.revise) {
    const card = t.closest("[data-approval]");
    const body = $("[data-rev-body]", card)?.value, subject = $("[data-rev-subject]", card)?.value;
    t.disabled = true;
    return link.request("comms.revise", { approval_id: t.dataset.revise, body, subject: subject ?? null })
      .then(() => toast("Изменил — подтвердите ещё раз")).catch((er) => { toast(er.message, "err"); t.disabled = false; });
  }
  if (t.dataset.taskAct) return link.request(t.dataset.taskAct, { task_id: t.dataset.task }).then((r) => r.message && toast(r.message)).catch((er) => toast(er.message, "err"));
  if (t.dataset.act === "estop_clear") return link.request("estop_clear").then((r) => toast(r.message));
  if (t.dataset.act === "conv") { voice.toggleConversation(); return; }
  if (t.dataset.act === "toggle-mute") { voice.setMuted(!voice.muted); renderStatus(); renderComposer(); return; }
});

async function estop() {
  voice.stopAll();
  try { const r = await link.request("estop"); toast(r.message, "err"); } catch (e) { toast(e.message, "err"); }
}

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.shiftKey && (e.key === "X" || e.key === "x" || e.code === "KeyX")) { e.preventDefault(); return estop(); }
  if (e.ctrlKey && e.shiftKey && e.code === "Space") { e.preventDefault(); if (!$("#convBtn").disabled) voice.toggleConversation(); return; }
  if (e.ctrlKey && e.code === "Space") { e.preventDefault(); if (!$("#micBtn").disabled) voice.listenOnce(); return; }
  if (e.key === "Escape") {
    if (voice.recording || voice.audio) { voice.stopAll(); return; }
    if ($("#app").classList.contains("side-open")) { toggleSide(); return; }
  }
  if (e.ctrlKey && !e.shiftKey) {
    const m = { Digit1: "chat", Digit2: "tasks", Digit3: "flora", Digit4: "comms", Comma: "settings" }[e.code];
    if (m) { e.preventDefault(); nav(m); return; }
    if (e.code === "KeyK") { e.preventDefault(); nav("chat"); input.focus(); }
  }
  if (e.key === "/" && document.activeElement === document.body) { e.preventDefault(); input.focus(); }
});

function toggleSide() {
  const app = $("#app");
  if (innerWidth <= 1080) app.classList.toggle("side-open");
  else app.classList.toggle("side-hidden");
}

// ------------------------------------------------------------------ views
const VIEWS = { chat: "#viewChat", tasks: "#viewTasks", flora: "#viewFlora", comms: "#viewComms", settings: "#viewSettings" };
function nav(v) {
  S.view = v;
  $("#app").dataset.view = v;
  for (const [k, sel] of Object.entries(VIEWS)) $(sel).hidden = k !== v;
  $$(".rail-btn").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.nav === v)));
  if (v === "chat") { renderThread(); scrollToEnd(false); setTimeout(() => input.focus(), 0); }
  if (v === "tasks") renderTasksView();
  if (v === "settings") renderSettings();
  if (v === "flora") renderFlora();
  if (v === "comms") renderComms();
}

let commsMod = null;
async function renderComms() {
  const el = $("#viewComms");
  try {
    commsMod = commsMod || await import("./comms.js");
    await commsMod.render(el, { link, toast });
  } catch (e) {
    el.innerHTML = `<div class="page"><h1>Сообщения</h1><p class="lead">Модуль не загрузился: ${esc(e.message)}</p></div>`;
  }
}

async function renderTasksView() {
  const el = $("#viewTasks");
  let list = [];
  try { list = await link.request("tasks.list", { limit: 80 }); } catch { list = Object.values(S.tasks); }
  const open = el.dataset.open;
  el.innerHTML = `<div class="page"><h1>Задачи</h1><p class="lead">Журнал того, что JARVIS делал: какие шаги, сколько заняло, что проверено.</p>
    <div class="panel" style="padding:0">${list.length ? list.map((t) => `
      <button class="history-row" data-task-open="${esc(t.id)}">
        <span>${esc(t.request)}</span><span class="pill ${esc(t.state)}">${esc(STATE_RU[t.state] || t.state)}</span>
        <span class="when">${fmtWhen(t.created_at)}</span></button>
      ${open === t.id ? `<div style="padding:4px 14px 14px" class="task-detail">
        <div class="meta">маршрут: ${esc(t.route || "—")}${t.provider ? " · " + esc(t.provider) : ""}${t.finished_at ? " · " + fmtMs((t.finished_at - t.created_at) * 1000) : ""}${t.error ? " · " + esc(t.error) : ""}</div>
        ${t.decision ? decisionCard(t.decision, true) : ""}
        <div class="steps">${(t.steps || []).map((s) => stepRow(s)).join("") || '<div class="empty-note">Без шагов с инструментами</div>'}</div></div>` : ""}`).join("")
      : '<div class="empty-note" style="padding:16px">Задач ещё не было</div>'}</div></div>`;
  $$("[data-task-open]", el).forEach((b) => b.addEventListener("click", () => {
    el.dataset.open = el.dataset.open === b.dataset.taskOpen ? "" : b.dataset.taskOpen;
    renderTasksView();
  }));
}

async function renderFlora() {
  const el = $("#viewFlora");
  try {
    const mod = floraMod || (floraMod = await import("./flora.js"));
    const p = pendingFlora; pendingFlora = null;
    if (p?.tab) mod.setTab(p.tab);
    if (p?.item) mod.selectItem(p.item);
    await mod.render(el, { link, S, toast, submit, nav });
  } catch (e) {
    console.error(e);
    if (!floraMod) el.innerHTML = `<div class="page"><h1>Проект «Флора»</h1><p class="lead">Модуль не загрузился: ${esc(e.message)}</p></div>`;
  }
}

function renderSettings() {
  const el = $("#viewSettings");
  const p = S.provider || { models: [] };
  const prof = S.core.privacy;
  const choice = (id, title, text) => `<button class="choice" role="radio" aria-checked="${prof === id}" data-privacy="${id}"><b>${title}</b><span>${text}</span></button>`;
  const statusRu = !p.configured ? "не подключён" : { ready: "готов", error: "ошибка", unknown: "проверка…", unconfigured: "не подключён" }[p.status] || p.status;
  el.innerHTML = `<div class="page">
    <h1>Настройки</h1>
    ${agencyPanel(S)}
    <div class="panel">
      <h2>Приватность</h2>
      <div class="muted" style="font-size:13.5px">Что можно отправлять во внешний ИИ. Локальные действия работают в любом режиме.</div>
      <div class="choices" role="radiogroup" aria-label="Режим приватности">
        ${choice("AUTHORIZED_CLOUD", "Облако разрешено", "Свободные запросы уходят в Gemini без дополнительного вопроса. Файлы — только те, что нужны для задачи.")}
        ${choice("PRIVATE_LOCAL", "Приватно", "Перед каждой отправкой в облако JARVIS спросит разрешение.")}
        ${choice("STRICT_OFFLINE", "Строго офлайн", "Ничего не уходит в облако. Голос и свободные вопросы отключены.")}
      </div>
    </div>
    <div class="panel">
      <h2>Голос</h2>
      <div class="muted" style="font-size:13.5px">Речь распознаёт Gemini; отвечает голосом локальный синтез (Piper, голос «Денис») —
        ответ начинает звучать, пока Gemini ещё пишет. Микрофон слушает только когда включён (кнопка или разговор); речь определяется на компьютере,
        в облако уходят только сами фразы.</div>
      <label class="toggle"><input type="checkbox" id="speakToggle" ${voice.speakReplies ? "checked" : ""}> Озвучивать ответы и на текстовые запросы</label>
      <label class="toggle"><input type="checkbox" id="muteToggle" ${voice.muted ? "checked" : ""}> Выключить микрофон</label>
      <div class="field"><span style="font-size:13.5px">Пауза, после которой фраза считается законченной:</span>
        <input type="range" id="silenceRange" min="400" max="1500" step="50" value="${voice.silenceMs}" style="flex:1;min-width:160px">
        <span class="faint" id="silenceVal">${(voice.silenceMs / 1000).toFixed(2).replace(".", ",")} с</span></div>
      <div class="field" style="margin-top:10px">
        <button class="btn" id="soundTest">Проверить звук</button>
        <span id="audioOut" class="faint" style="font-size:13px"></span>
      </div>
      <div id="voiceTurns" style="margin-top:12px"></div>
    </div>
    <div class="panel">
      <h2>Разрешения</h2>
      <div class="muted" style="font-size:13.5px">Каждое действие проходит через единую политику. Средний и высокий риск требуют вашего решения.</div>
      <label class="toggle"><input type="checkbox" id="fullAccess" ${S.core.access === "full" ? "checked" : ""}> <span><b>Полный доступ к ноутбуку</b> —
        чтение и поиск по всей домашней папке, обычные действия без вопросов, «да» голосом на любое подтверждение, сообщения себе без вопроса.
        По-прежнему спрашиваю один раз: сообщения другим людям, изменения кода, правила «без подтверждения».
        Никогда не открываю ключи, пароли и данные браузеров.</span></label>
      <table class="tbl"><thead><tr><th>Действие</th><th>Риск</th><th>Проверка результата</th></tr></thead><tbody>
        ${S.tools.filter((t) => t.id !== "cloud_send").map((t) => `<tr><td>${esc(t.title)}${S.grants.includes(t.id) ? ` <button class="btn ghost" data-revoke="${esc(t.id)}" style="height:24px;font-size:12px">отозвать разрешение</button>` : ""}</td>
          <td><span class="risk ${esc(t.risk)}">${esc({ low: "низкий", moderate: "средний", high: "высокий" }[t.risk] || t.risk)}</span></td>
          <td class="faint">${esc(t.verification)}</td></tr>`).join("")}
      </tbody></table>
    </div>
    <div class="panel">
      <h2>Интерфейс</h2>
      <div class="field">
        <button class="btn ${!document.documentElement.dataset.theme ? "primary" : ""}" data-theme-set="">Как в системе</button>
        <button class="btn ${document.documentElement.dataset.theme === "dark" ? "primary" : ""}" data-theme-set="dark">Тёмная</button>
        <button class="btn ${document.documentElement.dataset.theme === "light" ? "primary" : ""}" data-theme-set="light">Светлая</button>
      </div>
      <label class="toggle"><input type="checkbox" id="motionToggle" ${localStorage.getItem("jarvis.reduceMotion") === "1" ? "checked" : ""}> Без анимации</label>
      <label class="toggle"><input type="checkbox" id="devToggle" ${S.dev ? "checked" : ""}> Режим разработчика: показывать решения директора (исполнители, причины, кандидаты)</label>
    </div>
  </div>`;
  wireAgency(el, { link, S, toast, rerender: () => { renderSettings(); renderStatus(); renderComposer(); } });
  $$("[data-privacy]", el).forEach((b) => b.addEventListener("click", async () => {
    try { await link.request("privacy.set", { profile: b.dataset.privacy }); S.core.privacy = b.dataset.privacy; } catch (er) { toast(er.message, "err"); }
    renderSettings(); renderStatus(); renderPrivacy(); renderComposer();
  }));
  $("#fullAccess", el).addEventListener("change", async (e) => {
    try { const r = await link.request("access.set", { level: e.target.checked ? "full" : "standard" }); S.core.access = r.access;
      toast(r.access === "full" ? "Полный доступ включён" : "Обычный доступ: снова спрашиваю перед изменениями"); }
    catch (er) { toast(er.message, "err"); e.target.checked = !e.target.checked; }
  });
  $$("[data-revoke]", el).forEach((b) => b.addEventListener("click", async () => {
    const r = await link.request("grant.revoke", { tool_id: b.dataset.revoke }); S.grants = r.session_grants; renderSettings();
  }));
  $$("[data-theme-set]", el).forEach((b) => b.addEventListener("click", () => {
    const v = b.dataset.themeSet;
    if (v) { document.documentElement.dataset.theme = v; localStorage.setItem("jarvis.theme", v); }
    else { delete document.documentElement.dataset.theme; localStorage.removeItem("jarvis.theme"); }
    renderSettings();
  }));
  $("#speakToggle", el).addEventListener("change", (e) => { voice.speakReplies = e.target.checked; localStorage.setItem("jarvis.speak", e.target.checked ? "1" : "0"); });
  $("#muteToggle", el).addEventListener("change", (e) => { voice.setMuted(e.target.checked); renderStatus(); renderComposer(); });
  $("#silenceRange", el).addEventListener("input", (e) => {
    voice.silenceMs = +e.target.value; localStorage.setItem("jarvis.silenceMs", String(voice.silenceMs));
    $("#silenceVal", el).textContent = (voice.silenceMs / 1000).toFixed(2).replace(".", ",") + " с";
  });
  renderVoiceTurns($("#voiceTurns", el));
  renderAudioOut();
  $("#soundTest", el).addEventListener("click", async () => {
    try { await link.request("voice.test"); } catch (er) { toast(er.message, "err"); return; }
    setTimeout(() => voice.checkOutput(true), 900);
  });
  $("#devToggle", el).addEventListener("change", (e) => {
    S.dev = e.target.checked; localStorage.setItem("jarvis.dev", S.dev ? "1" : "0"); turnCache.clear(); $("#thread").innerHTML = "";
  });
  $("#motionToggle", el).addEventListener("change", (e) => {
    localStorage.setItem("jarvis.reduceMotion", e.target.checked ? "1" : "0");
    document.documentElement.classList.toggle("anim-off", e.target.checked);
  });
}

function renderAudioOut() {
  const box = $("#audioOut");
  if (!box) return;
  const r = S.audioOut;
  if (!r) { box.textContent = "Нажмите — JARVIS скажет тестовую фразу и проверит громкость своего звука."; return; }
  if (!r.available) { box.textContent = "Системный микшер недоступен — проверьте громкость вручную."; return; }
  const w = outputProblem(r);
  box.innerHTML = `${esc(r.sink || "Устройство вывода")}: общая громкость ${r.sink_volume_pct ?? "?"} %${r.sink_muted ? " (выключен)" : ""}`
    + (r.stream_found ? ` · JARVIS ${r.volume_pct ?? "?"} %${r.muted ? " (выключен)" : ""}` : " · поток JARVIS сейчас не играет")
    + (w ? ` <b style="color:var(--warn)">— ${esc(w)}</b> <button class="btn primary" id="audioFix" style="height:28px">Сделать громче</button>` : " ✓");
  $("#audioFix")?.addEventListener("click", async () => {
    try { await link.request("voice.test"); await new Promise((r) => setTimeout(r, 700)); S.audioOut = await link.request("voice.output", { fix: true }); }
    catch (er) { toast(er.message, "err"); }
    renderAudioOut();
  });
}

async function renderVoiceTurns(box) {
  let turns = [];
  try { turns = await link.request("voice.turns", { limit: 10 }); } catch { return; }
  if (!turns.length) { box.innerHTML = '<div class="faint" style="font-size:12.5px">Замеры появятся после первых голосовых фраз.</div>'; return; }
  const s = (v) => v == null ? "—" : (v / 1000).toFixed(1).replace(".", ",");
  box.innerHTML = `<h2 style="font-size:13.5px;margin:0 0 4px">Задержка последних голосовых ходов, с (от конца вашей фразы)</h2>
    <table class="tbl"><thead><tr><th>Фраза</th><th>Распознано</th><th>Первое слово ответа</th><th>Первый звук</th><th>Ответ готов</th></tr></thead><tbody>
    ${turns.map((t) => `<tr><td>${esc((t.transcript || "—").slice(0, 46))}</td><td>${s(t.ms.stt_end)}</td><td>${s(t.ms.ttft)}</td>
      <td><b>${s(t.ms.first_audio_played ?? t.ms.first_audio_sent)}</b></td><td>${s(t.ms.answer_done)}</td></tr>`).join("")}</tbody></table>`;
}

// ------------------------------------------------------------------ toasts
function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "err" ? 6000 : 3500);
  while ($("#toasts").children.length > 3) $("#toasts").firstChild.remove();
}

function renderAll() { renderStatus(); renderThread(); renderSide(); renderComposer(); if (S.view !== "chat") nav(S.view); }

nav("chat");
renderAll();
window.__jarvis = { S, link, voice, render: () => { turnCache.clear(); renderThread(); } };   // for automated UI tests
