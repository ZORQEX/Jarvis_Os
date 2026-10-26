// JARVIS WorkOS — the window shell. One WebSocket to the local core; one state object; a topic bus so each part
// redraws only for what it shows. Layout: sidebar (workspaces) · top bar (real status) · workspace, plus the global
// companion, the command palette and notifications above everything.
import { Link } from "./net.js";
import { Voice } from "./voice.js";
import { esc, md, ICON, STATE_RU, stepRow, resultCard, approvalCard, fmtMs, fmtWhen, decisionCard, orderCard } from "./render.js";
import { createBus, frameBatch, prefs } from "./core/store.js";
import { flashFor, PHASES } from "./core/agent-state.js";
import { classify } from "./core/intent.js";
import { isUserTask, isWorking, canRetry } from "./core/tasks-model.js";
import { WORKSPACES, ALL, byId, navBadge, connectionOf } from "./workspaces/registry.js";
import { AgentStream } from "./adapters/agent.js";
import { DesktopAdapter } from "./adapters/desktop.js";
import { createNotifier } from "./ui/notify.js";
import { createCompanionDock } from "./ui/companion-dock.js";
import { createPalette } from "./ui/palette.js";
import * as browserWs from "./workspaces/browser.js";
import * as commWs from "./workspaces/communication.js";
import * as filesWs from "./workspaces/files.js";
import * as settingsWs from "./workspaces/settings.js";
import * as ordersWs from "./workspaces/orders.js";
import * as abilitiesWs from "./workspaces/abilities.js";
import * as errorsWs from "./workspaces/errors.js";
import * as historyWs from "./workspaces/history.js";
import { createCodingAsk } from "./ui/coding-ask.js";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
/** Write markup only when it changed: the top bar redraws on every event (each streamed word). */
const setHtml = (el, html) => { if (el._html !== html) { el._html = html; el.innerHTML = html; } };

const S = {
  connected: false, convId: null, messages: [], tasks: {}, approvals: {}, core: { state: "OFFLINE" },
  provider: null, reminders: [], streaming: {}, progress: {}, tools: [], grants: [],
  view: "chat", voice: null, firstSnapshot: true, providers: [], dev: localStorage.getItem("jarvis.dev") === "1",
  comms: {},              // messenger providers by channel (comms.state / comms.providers)
  flash: null,            // last finished task's reaction (SUCCESS / ERROR / CANCELLED)
  lastReply: null,        // {text, taskId, at} — newest assistant message, for the companion
  attachments: [],        // file paths attached to the next chat message
  lastVoiceAt: 0,
  askedHere: new Set(),   // tasks asked from outside the chat (companion, a workspace): their answer pops up there
  newErrors: 0,           // tasks that failed since «Ошибки» was last opened (a dot in the sidebar)
};

// ------------------------------------------------------------------ boot
const theme = localStorage.getItem("jarvis.theme");
if (theme) document.documentElement.dataset.theme = theme;
if (localStorage.getItem("jarvis.reduceMotion") === "1") document.documentElement.classList.add("anim-off");
const app = $("#app");
const bus = createBus();
const toast = createNotifier($("#toasts"));

const link = new Link({
  onSnapshot: (d) => applySnapshot(d),
  onEvent: (ev) => onEvent(ev),
  onStatus: (ok) => {
    const was = S.connected;
    S.connected = ok;
    if (!ok && was) toast("Нет связи с ядром JARVIS — переподключаюсь…", "warn");
    if (ok) {
      loadComms(); link.request("ui.view", { view: S.view }).catch(() => {});
      link.request("coding.state").then((d) => codingAsk(d.requests || [])).catch(() => {});   // a request already waiting
    }
    bus.emit("link");
  },
});
const codingAsk = createCodingAsk(link, toast);     // Claude Code / Codex permission cards (opt-in)
const agent = new AgentStream(link);
const desktop = new DesktopAdapter(link);

const voice = new Voice({
  link,
  onState: (v) => {
    S.voice = v; bus.emit("voice");
    if (voice.listening !== S.micWasOpen) {            // the desktop companion pauses «Джарвис» listening meanwhile
      S.micWasOpen = voice.listening;
      link.request("voice.presence", { client: "window", listening: voice.listening }).catch(() => {});
    }
    if (v === "interrupted") setTimeout(() => { if (S.voice === "interrupted") voice.set(voice.listening ? "listening" : null); }, 700);
  },
  onTurn: (r) => {
    if (r?.pending) { toast(`Слушаю дальше: «${(r.text || "").slice(0, 60)}…»`); return; }   // phrase cut off: wait for the rest
    S.lastVoiceAt = Date.now();      // the answer shows at the companion in any workspace; no forced switch to chat
  },
  onError: (m) => toast(m, "warn"),
  onOutput: (r) => {
    S.audioOut = r;
    const w = settingsWs.outputProblem(r);
    if (w) toast(w + " — откройте Настройки → Голос, там есть кнопка «Сделать громче».", "warn");
    if (S.view === "settings") settings.renderAudioOut();
  },
});
// JARVIS is waiting for the user's answer: a confirmation, or a question it just asked (the mic stays open for it).
voice.holdOpen = () => Object.values(S.approvals).some((a) => a.status === "pending" && a.tool_id !== "trust_rule_add")
  || (() => { const m = S.messages.at(-1); return !!m && m.role === "assistant" && /\?\s*$/.test(m.text || "")
    && Date.now() / 1000 - m.created_at < 30; })();

function applySnapshot(d) {
  // JARVIS restarted with a new interface: load it (never mid-recording).
  if (d.ui_build && S.uiBuild && d.ui_build !== S.uiBuild && !["recording", "transcribing"].includes(S.voice)) {
    location.reload(); return;
  }
  S.uiBuild = d.ui_build || S.uiBuild;
  S.convId = d.conversation_id;
  S.messages = d.messages;
  S.tasks = d.tasks;
  S.approvals = Object.fromEntries(d.approvals.map((a) => [a.id, a]));
  S.core = d.core;
  S.provider = d.provider;
  S.providers = d.providers || [];
  S.reminders = d.reminders;
  S.tools = d.tools;
  S.grants = d.session_grants;
  S.streaming = {};
  turnCache.clear();
  $("#thread").innerHTML = "";
  for (const t of ["core", "tasks", "approvals", "messages", "reminders", "providers"]) bus.emit(t);
  renderThread();
  if (S.firstSnapshot) { scrollToEnd(false); S.firstSnapshot = false; }
}

// ------------------------------------------------------------------ events from the core
let commsMod = null;
function onEvent(ev) {
  commsMod?.onEvent(ev);
  const tid = ev.task_id;
  // Answers to what was said to the desktop companion are spoken there, not here (no double voice).
  if (ev.owner === "overlay" && String(ev.kind).startsWith("speech.")) return;
  switch (ev.kind) {
    case "message": {
      const m = ev.message;
      if (m.conversation_id !== S.convId) return;
      if (S.messages.some((x) => x.id === m.id)) return;
      S.messages.push(m);
      if (m.role === "assistant" && m.task_id) delete S.streaming[m.task_id];
      if (m.role === "assistant" && m.task_id && isUserTask(S.tasks[m.task_id]) && S.tasks[m.task_id]?.state !== "cancelled") {
        const byVoice = S.messages.some((x) => x.task_id === m.task_id && x.role === "user" && x.meta?.source === "voice");
        S.lastReply = { text: m.text, taskId: m.task_id, at: Date.now(), pop: S.askedHere.has(m.task_id) || byVoice };
      }
      bus.emit("messages", m);
      break;
    }
    case "message.updated": {               // an orders card was confirmed or cancelled
      const m = S.messages.find((x) => x.id === ev.message_id);
      if (m) { m.meta = ev.meta; bus.emit("messages", m); }
      break;
    }
    case "task.created": case "task.updated": {
      const t = ev.task, prev = S.tasks[t.id];
      if (t.route === "ui_read") return;            // a folder click in «Файлы»: not a task to show or keep
      S.tasks[t.id] = t;
      if ((!prev || isWorking(prev)) && !isWorking(t)) taskFinished(t);
      bus.emit("task", t); bus.emit("tasks");
      break;
    }
    case "action.recorded":
      if (ev.action?.title && ev.action.status !== "failed") toast(ev.action.title, "ok");
      bus.emit("actions"); break;
    case "action.deleted": bus.emit("actions"); break;
    case "step.updated": {
      const st = ev.step, t = S.tasks[tid];
      if (!t) return;
      const i = t.steps.findIndex((x) => x.id === st.id);
      if (i >= 0) t.steps[i] = st; else t.steps.push(st);
      if (!["running"].includes(st.state)) delete S.progress[st.id];
      bus.emit("task", t); bus.emit("tasks");
      break;
    }
    case "step.progress": S.progress[ev.step_id] = ev.message; bus.emit("tasks"); break;
    case "approval.created": case "approval.updated": {
      S.approvals[ev.approval.id] = ev.approval;
      bus.emit("approvals", ev.approval);
      if (ev.kind === "approval.created") notifyAttention();
      break;
    }
    case "assistant.delta": {
      const s = S.streaming[tid] || (S.streaming[tid] = { text: "" });
      s.text += ev.text;
      updateStreaming(tid);
      bus.emit("stream");
      break;
    }
    case "core.state": S.core = ev; bus.emit("core"); break;
    case "reminder.created": S.reminders.push(ev.reminder); bus.emit("reminders"); break;
    case "reminder.updated": {                       // cancelled or moved
      S.reminders = S.reminders.filter((r) => r.id !== ev.reminder.id);
      if (ev.reminder.status === "scheduled") S.reminders.push(ev.reminder);
      S.reminders.sort((a, b) => a.due_at - b.due_at);
      bus.emit("reminders");
      break;
    }
    case "reminder.fired": {
      S.reminders = S.reminders.filter((r) => r.id !== ev.reminder.id);
      toast(`⏰ ${ev.body}`, ev.late ? "warn" : "", { ttl: 12000 });
      bus.emit("reminders");
      break;
    }
    case "meeting.state":
      S.core.meeting = ev.meeting;
      toast(ev.meeting ? `🔴 Записываю встречу «${ev.meeting.title}»` : "Запись встречи остановлена", ev.meeting ? "warn" : "", { ttl: 6000 });
      bus.emit("core");
      break;
    case "voice.mic": if (!ev.ok) toast(ev.text, "warn", { ttl: 30000 }); break;
    case "notice": toast(`🔔 ${ev.title}${ev.body ? ". " + ev.body : ""}`, ev.urgent ? "warn" : "", { ttl: ev.urgent ? 20000 : 10000 }); break;
    case "estop": toast(ev.active ? "Экстренная остановка включена" : "Экстренная остановка снята", ev.active ? "err" : "ok"); break;
    case "speech.stop": voice.stopSpeaking(); break;
    case "speech.synth": if (!voice.audio && !["recording", "transcribing"].includes(S.voice)) voice.set("synthesizing"); break;
    case "speech.fallback":
      if (Date.now() - (S.fallbackToldAt || 0) > 10 * 60000) {
        S.fallbackToldAt = Date.now();
        toast(`Выбранный голос не сработал (${ev.reason || "Silero"}) — отвечаю запасным голосом. Подробности: Настройки → Голос.`, "warn");
      }
      break;
    case "speech.audio": voice.enqueue(ev); break;
    case "speech.end": if (!voice.audio && ["synthesizing", "error"].includes(S.voice)) voice.set(voice.listening ? "listening" : null); break;
    case "speech.cancelled": voice.stopSpeaking(tid); if (S.voice === "synthesizing") voice.set(voice.listening ? "listening" : null); break;
    case "speech.error":
      toast("Ответ не удалось озвучить — он показан текстом. Причина: " + ev.error, "warn");
      if (!voice.audio) voice.set("error");
      setTimeout(() => { if (S.voice === "error") voice.set(voice.listening ? "listening" : null); }, 2500);
      break;
    case "conversation.new": link.request("snapshot").then(applySnapshot).catch(() => {}); break;
    case "ui.navigate":
      nav(ev.view);
      break;
    case "hub.backends": case "hub.catalog": case "hub.event": case "hub.connect":   // «Возможности» (OpenClaw, Hermes)
      if (ev.kind === "hub.connect") viewFor("abilities");
      views.abilities?.onEvent(ev);
      break;
    case "coding.agents":                   // Claude Code / Codex sessions; their permission requests get a card
      codingAsk(ev.requests || []);
      views.abilities?.onEvent(ev);
      break;
    case "fashion.turn":                    // a request about orders, answered by the orders assistant
      if (ev.show && S.view !== "orders") nav("orders");
      viewFor("orders")?.turn(ev);
      break;
    case "providers": S.providers = ev.providers; S.provider = ev.provider; bus.emit("providers");
      if (S.view === "settings" && !document.activeElement?.closest?.(".pcard")) settings.show(); break;
    case "task.decision": {
      const t = S.tasks[tid];
      if (t) { t.decision = ev.decision; bus.emit("task", t); bus.emit("tasks"); }
      break;
    }
    case "comms.providers": {
      const before = { ...S.comms };
      setProviders(ev.providers);
      for (const p of ev.providers) {
        const b = before[p.id];
        if (b?.state === "connected" && p.state !== "connected") toast(`${p.title}: ${connectionOf(p).label.toLowerCase()}${p.detail ? " — " + p.detail : ""}`, "warn");
      }
      break;
    }
    case "comms.job": {
      const j = ev.job;
      bus.emit("comms.job", j);
      if (j.status === "sent") toast(`Сообщение отправлено: ${j.to_name}`, "ok");
      if (j.status === "failed") toast(`Не отправлено: ${j.to_name}${j.last_error ? " — " + j.last_error : ""}`, "err");
      break;
    }
  }
}

function taskFinished(t) {
  const f = flashFor(t, Date.now());
  if (f) { S.flash = f; setTimeout(() => bus.emit("core"), 2700); }
  if (t.state === "failed" && isUserTask(t) && t.route !== "ui") {
    if (S.view !== "errors") { S.newErrors++; renderSidebar(); }
    if (S.view !== "chat") toast(`Не получилось: ${t.request.slice(0, 60)}`, "err", { action: { label: "Подробнее", run: () => openTask(t.id) } });
  }
}

function notifyAttention() {
  if (document.hidden && "Notification" in window && Notification.permission === "granted") {
    new Notification("JARVIS: нужно ваше решение");
  }
}

function setProviders(list) {
  S.comms = Object.fromEntries((list || []).map((p) => [p.id, p]));
  bus.emit("providers");
}
async function loadComms() {
  try { const d = await link.request("comms.state"); setProviders(d.providers); } catch { /* comms module not built in */ }
}

// ------------------------------------------------------------------ workspaces
const sections = { chat: $("#viewChat"), comms: $("#viewComms"), settings: $("#viewSettings") };
const views = {};
const ctx = {
  S, bus, link, agent, desktop, voice, toast, nav: (id, p) => nav(id, p),
  submit: (text, o) => submit(text, o), openTask: (id) => openTask(id), openApproval: (id) => openApproval(id),
  attachFile: (p) => attachFile(p), findTask: (id) => S.tasks[id] || null,
  connected: () => S.connected, providers: () => S.comms, setProviders: (l) => setProviders(l),
  env: () => ({ connected: S.connected, providers: S.comms }), currentView: () => S.view,
};
const settings = settingsWs.create({ ...ctx, onChange: () => { bus.emit("core"); bus.emit("voice"); },
  onDevToggle: () => { turnCache.clear(); $("#thread").innerHTML = ""; } });
views.settings = settings;
const FACTORY = { browser: browserWs.create, files: filesWs.create, orders: ordersWs.create, abilities: abilitiesWs.create,
  errors: errorsWs.create, history: historyWs.create,
  telegram: (c) => commWs.create(c, "telegram"), whatsapp: (c) => commWs.create(c, "whatsapp"), mail: (c) => commWs.create(c, "mail") };

function viewFor(id) {
  if (views[id]) return views[id];
  if (!FACTORY[id]) return null;
  const v = FACTORY[id](ctx);
  $("#workspaces").appendChild(v.el);
  sections[id] = v.el;
  views[id] = v;
  return v;
}

function nav(id, params = {}) {
  const def = byId(id);
  if (!def) return;
  const prev = S.view;
  if (prev !== id) {
    views[prev]?.hide?.();
    if (sections[prev]) sections[prev].hidden = true;
  }
  S.view = id;
  app.dataset.view = id;
  prefs.set("ws", id);
  const v = viewFor(id);
  if (sections[id]) sections[id].hidden = false;
  if (id === "errors" && S.newErrors) { S.newErrors = 0; renderSidebar(); }
  if (id === "chat") { renderThread(); scrollToEnd(false); setTimeout(focusComposer, 0); }
  else if (id === "comms") renderComms();
  else v?.show?.(params);
  $$("[data-nav]", $("#sidebar")).forEach((b) => b.setAttribute("aria-selected", String(b.dataset.nav === id)));
  bus.emit("workspace", id);
  if (S.connected) link.request("ui.view", { view: id }).catch(() => {});   // «Заказы» open → order phrases go there
}

/** The chat's field gets the keyboard — unless the person already moved it (keys are handled before this timer:
 *  Ctrl+K right after opening the chat used to lose its letters to the chat field). */
function focusComposer() {
  const a = document.activeElement;
  if (S.view === "chat" && !palette.isOpen && (!a || a === document.body || a.closest("#sidebar, .cdock, .toasts"))) input.focus();
}

/** A task lives in the chat: its request, steps, cards and answer. Show that turn. */
function openTask(id) {
  nav("chat");
  requestAnimationFrame(() => {
    const turn = document.querySelector(`.turn[data-key="${CSS.escape(id)}"]`);
    if (!turn) return;
    turn.scrollIntoView({ block: "center", behavior: "smooth" });
    turn.classList.remove("flash"); void turn.offsetWidth; turn.classList.add("flash");
  });
}
function openApproval(id) {
  nav("chat");
  requestAnimationFrame(() => {
    const card = document.querySelector(`[data-approval="${CSS.escape(id)}"]`);
    if (!card) return;
    card.scrollIntoView({ block: "center", behavior: "smooth" });
    const d = card.querySelector("details.revise");
    if (d) d.open = true;
    (card.querySelector("details.revise textarea, textarea[data-edit]") || card.querySelector("[data-approve]"))?.focus();
  });
}

async function renderComms() {
  const el = $("#viewComms");
  try {
    commsMod = commsMod || await import("./comms.js");
    await commsMod.render(el, { link, toast });
  } catch (e) {
    el.innerHTML = `<div class="page"><h1>Сообщения</h1><p class="lead">Модуль не загрузился: ${esc(e.message)}</p></div>`;
  }
}

// ------------------------------------------------------------------ sidebar
function buildSidebar() {
  $("#nav").innerHTML = WORKSPACES.map((w) => `<button class="sb-btn" data-nav="${w.id}" role="tab" aria-selected="false"
      aria-label="${esc(w.title)}" data-tip="${esc(w.title)}${w.hotkey ? "  ·  Ctrl+" + w.hotkey : ""}">
      <span class="sb-ico" aria-hidden="true">${ICON[w.icon] || ICON.dot}<span class="sb-badge" hidden></span></span>
      <span class="sb-label">${esc(w.title)}</span><span class="sb-count" hidden></span></button>`).join("");
}
const renderSidebar = frameBatch(() => {
  const env = ctx.env();
  for (const w of WORKSPACES) {
    const b = $(`#nav [data-nav="${w.id}"]`);
    if (!b) continue;
    const badge = navBadge(w, env), dot = $(".sb-badge", b);
    dot.hidden = !badge;
    if (badge) { dot.className = `sb-badge ${badge}`; b.dataset.tip = `${w.title} · ${connectionOf(S.comms[w.channel]).label.toLowerCase()}${w.hotkey ? "  ·  Ctrl+" + w.hotkey : ""}`; }
  }
  const c = $('#nav [data-nav="errors"] .sb-count');
  if (c) { c.hidden = !S.newErrors; c.textContent = S.newErrors; }
});
function setSidebarWide(wide) {
  app.classList.toggle("sb-wide", wide);
  prefs.set("sidebar", wide ? "wide" : "narrow");
  const t = $("#sbToggle");
  t.setAttribute("aria-label", wide ? "Скрыть подписи" : "Показать подписи");
  t.dataset.tip = wide ? "Скрыть подписи" : "Показать подписи";
  $(".sb-label", t).textContent = wide ? "Свернуть" : "Развернуть";
}

// ------------------------------------------------------------------ top bar
const STATUS_TITLE = { WAITING_CONFIRMATION: "● JARVIS", ERROR: "JARVIS — ошибка", OFFLINE: "JARVIS — нет связи" };
function renderStatus() {
  const a = dock.agent;
  if (!a) return;
  const el = $("#status");
  el.dataset.state = a.mode;
  $(".status-label", el).textContent = a.label + (a.count > 1 ? ` (${a.count})` : "");
  el.title = `${a.label}${a.detail ? " — " + a.detail : ""} · Ctrl+J`;
  app.dataset.estop = S.core.estop ? "1" : "0";
  document.title = STATUS_TITLE[a.mode] || "JARVIS";
  const voiceFlow = ["recording", "transcribing"].includes(S.voice) || (Date.now() - S.lastVoiceAt < 60000 && a.busy);
  const ph = $("#phases");
  const idx = PHASES.findIndex(([k]) => k === a.phase);
  ph.hidden = !(voiceFlow && idx >= 0 && a.mode !== "SPEAKING");
  if (!ph.hidden) setHtml(ph, PHASES.map(([, l], i) => `<span class="${i < idx ? "done" : i === idx ? "on" : ""}">${l}</span>`).join("<i></i>"));
  renderChips();
}

function renderChips() {
  const chips = [];
  if (S.core.estop) chips.push(`<button class="chip err" data-act="estop_clear">${ICON.stop} Остановлено · снять</button>`);
  const pend = Object.values(S.approvals).filter((x) => x.status === "pending").length;
  if (pend) chips.push(`<button class="chip attn" data-act="show-approvals">${ICON.shield} Нужно решение${pend > 1 ? ` · ${pend}` : ""}</button>`);
  // Reminders (they had a list in the removed activity panel): the next one, all of them in the tooltip.
  const rem = S.reminders.filter((r) => r.status === "scheduled").sort((a, b) => a.due_at - b.due_at);
  if (rem.length) chips.push(`<span class="chip" title="${esc(rem.map((r) => `${fmtWhen(r.due_at)} — ${r.text}`).join("\n"))}">${ICON.clock} ${fmtWhen(rem[0].due_at)}${rem.length > 1 ? ` · ${rem.length}` : ""}</span>`);
  if (voice.listening) chips.push(`<button class="chip live" data-act="conv">${ICON.mic} ${voice.conversation ? "Разговор: микрофон включён" : "Слушаю одну фразу"}</button>`);
  if (S.providers.length && S.connected) {
    const up = S.providers.filter((p) => p.health === "CONNECTED");
    const soft = S.providers.filter((p) => p.health === "DEGRADED");
    const names = [...up, ...soft].map((p) => p.name);
    const cls = up.length ? "ok" : soft.length ? "warn" : "";
    const full = names.length ? `ИИ: ${names.join(" · ")}` : "ИИ не подключён";
    chips.push(`<button class="chip ${cls}" data-nav="settings" title="AI-агентство — ${esc(full)}">${ICON.spark} <span class="long">${esc(full)}</span><span class="short">${names.length ? "ИИ" : "ИИ нет"}</span></button>`);
  }
  // Privacy shows up here only when it restricts something; the normal mode is in Settings.
  const prof = { PRIVATE_LOCAL: "Приватно: спрашивать", STRICT_OFFLINE: "Строго офлайн" }[S.core.privacy];
  if (prof && S.connected) chips.push(`<button class="chip warn" data-nav="settings">${ICON.lock} ${prof}</button>`);
  setHtml($("#chips"), chips.join(""));
}

// ------------------------------------------------------------------ chat thread
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
    if (!$(".hero", thread)) { turnCache.clear(); thread.innerHTML = heroHtml(); }
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
    const sig = JSON.stringify([t.user?.id, t.replies.map((r) => [r.id, r.meta?.fashion?.status]), task?.state, task?.steps, task?.decision, S.dev,
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
  const ideas = ["Состояние системы", "Найди файл смета", "Громкость 40",
    "Запиши заметку: позвонить фабрике", "Напомни через 10 минут проверить почту", "Сделай скриншот", "Что ты умеешь?"];
  return `<div class="hero"><h1>${greet}.</h1>
    <p>Скажите или напишите, что нужно сделать. Простые команды выполняются сразу, на этом компьютере.</p>
    <div class="suggestions">${ideas.map((i) => `<button class="suggestion" data-suggest="${esc(i)}">${esc(i)}</button>`).join("")}</div></div>`;
}

function turnHtml(t, task, approvals) {
  const parts = [];
  if (t.user) {
    const src = t.user.meta?.source === "voice" ? `<span class="src" title="Голосом">${ICON.mic}</span>`
      : t.user.meta?.source === "scheduled" ? `<span class="src" title="По расписанию">${ICON.clock}</span>` : "";
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
    if (r.meta?.fashion) body.push(orderCard(r.meta.fashion, r.id));     // an orders change waiting for «Подтвердить»
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
  // Turn controls: stop while it runs, retry when it failed / was cancelled.
  if (task && active && task.route !== "ui") {
    body.push(`<div class="turn-ctl"><button class="btn ghost" data-task-act="cancel" data-task="${esc(task.id)}">${ICON.stop} Остановить</button></div>`);
  } else if (task && canRetry(task) && t.user) {
    body.push(`<div class="turn-ctl"><button class="btn ghost" data-retry="${esc(task.id)}">${ICON.retry} Повторить</button></div>`);
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
    bits.push(`<span class="cloud">${ICON.cloud.replace("<svg", '<svg style="width:13px;height:13px;vertical-align:-2px"')} ${esc(m.model || "Claude")}</span>`);
    if (m.first_token_ms) bits.push(`первый ответ ${fmtMs(m.first_token_ms)}`);
    if (m.usage?.total_token_count) bits.push(`${m.usage.total_token_count} токенов`);
  } else if (m.route === "agency") {
    const names = { claude: "Claude", claude_code: "Claude Code" };
    if (m.providers?.length) bits.push(`<span class="cloud">${ICON.spark.replace("<svg", '<svg style="width:13px;height:13px;vertical-align:-2px"')} ${esc(m.providers.map((p) => names[p] || p).join(" → "))}</span>`);
    else bits.push("без ИИ");
  } else if (m.route === "local") {
    bits.push("локально, без ИИ");
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
  if (S.view !== "chat") { streamDirty.clear(); return; }
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

// ------------------------------------------------------------------ composer
const input = $("#input");
function stoppable() {
  if (voice.audio || S.voice === "speaking") return "speech";
  const t = Object.values(S.tasks).filter((x) => isUserTask(x) && isWorking(x) && x.route !== "ui")
    .sort((a, b) => b.created_at - a.created_at)[0];
  return t ? t.id : null;
}
function renderComposer() {
  const mic = $("#micBtn"), conv = $("#convBtn");
  mic.classList.toggle("recording", voice.listening && !voice.conversation);
  mic.classList.toggle("busy", S.voice === "transcribing");
  conv.classList.toggle("on", voice.conversation);
  for (const b of [mic, conv]) b.disabled = voice.muted || !voice.supported;
  const why = voice.muted ? "Микрофон выключен" : "";
  mic.title = why || "Сказать одну фразу (Ctrl+Space)";
  conv.title = why || (voice.conversation ? "Закончить разговор (Ctrl+Shift+Space)" : "Разговор без кнопок (Ctrl+Shift+Space)");
  const stop = !input.value.trim() && !S.attachments.length && stoppable();
  $("#stopBtn").hidden = !stop;
  $("#sendBtn").hidden = !!stop;
  $("#stopBtn span").textContent = stop === "speech" ? "Замолчать" : "Стоп";
  $("#sendBtn").disabled = !input.value.trim();
  const hint = S.voice === "recording" ? "Говорите… фраза закончится сама после паузы · <span class=\"kbd\">Esc</span> — отмена"
    : S.voice === "transcribing" ? "Распознаю речь…"
    : S.voice === "speaking" ? "JARVIS говорит — начните говорить, чтобы перебить, или <span class=\"kbd\">Esc</span>"
    : voice.conversation ? "Разговор: просто говорите. Пауза — конец фразы. Скажите «стоп», чтобы перебить."
    : stop ? `<span><span class="kbd">Esc</span> остановить</span><span><span class="kbd">Enter</span> отправить</span>`
    : `<span><span class="kbd">Enter</span> отправить</span><span><span class="kbd">Ctrl Space</span> фраза</span><span><span class="kbd">Ctrl J</span> JARVIS</span>`;
  setHtml($("#composerHint"), hint);
}

function autosize() { input.style.height = "auto"; input.style.height = Math.min(180, input.scrollHeight) + "px"; }
input.addEventListener("input", () => { autosize(); renderComposer(); });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
});
$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  let text = input.value.trim();
  if (!text && !S.attachments.length) return;
  if (S.attachments.length) {
    text = (text || "Посмотри эти файлы") + "\n\nФайлы:\n" + S.attachments.map((a) => "- " + a.path).join("\n");
    S.attachments = []; renderAttachments();
  }
  input.value = ""; autosize();
  submit(text, { source: "text" });
});
$("#micBtn").addEventListener("click", () => voice.listenOnce());
$("#convBtn").addEventListener("click", () => voice.toggleConversation());
$("#stopBtn").addEventListener("click", () => stopCurrent());

async function stopCurrent() {
  const s = stoppable();
  if (!s) return false;
  if (s === "speech") { voice.stopAll(); return true; }
  try { const r = await agent.cancelTask(s); toast(r?.message || "Задача остановлена", "ok"); } catch (e) { toast(e.message, "err"); }
  return true;
}

// Attachments: file paths from the computer (search here or pick in «Файлы»). The path goes with the message;
// JARVIS reads the file through its own tools, within the allowed folders.
function attachFile(path) {
  if (!S.attachments.some((a) => a.path === path)) S.attachments.push({ path, name: path.split("/").pop() });
  renderAttachments();
  nav("chat");
  setTimeout(() => input.focus(), 0);
}
function renderAttachments() {
  const box = $("#attachments");
  box.hidden = !S.attachments.length;
  box.innerHTML = S.attachments.map((a, i) => `<span class="att" title="${esc(a.path)}">${ICON.file}<span>${esc(a.name)}</span>
    <button type="button" class="icon-btn" data-att-del="${i}" aria-label="Убрать ${esc(a.name)}">${ICON.x}</button></span>`).join("");
  renderComposer();
}
let attachPop = null;
$("#attachBtn").addEventListener("click", () => {
  if (attachPop) { attachPop.remove(); attachPop = null; return; }
  attachPop = document.createElement("div");
  attachPop.className = "attach-pop";
  attachPop.innerHTML = `<input type="text" placeholder="Имя файла — поиск в разрешённых папках" aria-label="Найти файл для вложения">
    <div class="rows"></div><div style="display:flex;gap:6px"><button type="button" class="btn ghost" data-nav="files">${ICON.folder} Выбрать в «Файлах»</button></div>`;
  $("#composer").appendChild(attachPop);
  const q = $("input", attachPop), rows = $(".rows", attachPop);
  let timer = 0;
  q.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      if (!q.value.trim()) { rows.innerHTML = ""; return; }
      rows.innerHTML = `<div class="empty-note">Ищу…</div>`;
      try {
        const d = await desktop.searchFiles(q.value.trim());
        rows.innerHTML = d.results.length ? d.results.slice(0, 12).map((h) => `<button type="button" class="row" data-attach="${esc(h.path)}" title="${esc(h.path)}">
          <span>${h.dir ? ICON.folder : ICON.file}</span><span class="t"><div>${esc(h.name)}</div><div class="p">${esc(h.path.replace(/^\/home\/[^/]+/, "~"))}</div></span><span class="r"></span></button>`).join("")
          : `<div class="empty-note">Ничего не найдено</div>`;
      } catch (e) { rows.innerHTML = `<div class="cerr">${esc(e.message)}</div>`; }
    }, 250);
  });
  q.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); attachPop.remove(); attachPop = null; input.focus(); } });
  q.focus();
});

/** Send a request to JARVIS. Window-level commands (switch workspace, stop voice, emergency stop) act at once. */
async function submit(text, { source = "text", stay = false } = {}) {
  const intent = classify(text);
  if (intent.kind === "nav" && source === "text") { nav(intent.view); return ""; }
  if (intent.kind === "control" && intent.local === "estop") { estop(); return ""; }
  if (intent.kind === "control" && intent.local === "stop_voice") voice.stopAll();
  if (!stay && S.view !== "chat") nav("chat");
  try {
    // Typed answers are spoken by the core itself (Settings → Голос → «Озвучивать ответы и на текстовые запросы»).
    const id = await agent.sendMessage(text, { source, speak: voice.conversation });
    if (id && stay && S.view !== "chat") {
      S.askedHere.add(id);
      // A local command can finish before this reply arrives: mark its answer now.
      if (S.lastReply?.taskId === id && !S.lastReply.pop) { S.lastReply.pop = true; bus.emit("messages"); }
    }
    return id;
  } catch (e) {
    toast(e.message, "err");
    if (source === "text" && !stay && !input.value) { input.value = text; autosize(); }
    return "";
  } finally { renderComposer(); }
}

// ------------------------------------------------------------------ global clicks & keys
document.addEventListener("click", async (e) => {
  if (attachPop && !e.target.closest(".attach-pop, #attachBtn")) { attachPop.remove(); attachPop = null; }
  const t = e.target.closest("button, [data-open], [data-url]");
  if (!t) return;
  if (t.dataset.nav) return nav(t.dataset.nav);
  if (t.dataset.suggest) return submit(t.dataset.suggest, { source: "text", stay: S.view !== "chat" });   // stay in the workspace
  if (t.dataset.attach) { attachPop?.remove(); attachPop = null; return attachFile(t.dataset.attach); }
  if (t.dataset.attDel != null) { S.attachments.splice(+t.dataset.attDel, 1); return renderAttachments(); }
  if (t.dataset.retry) { const task = ctx.findTask(t.dataset.retry); if (task) submit(task.request, { source: "text" }); return; }
  if (t.dataset.openTask) return openTask(t.dataset.openTask);
  if (t.dataset.openApproval) return openApproval(t.dataset.openApproval);
  if (t.id === "sbToggle") { setSidebarWide(!app.classList.contains("sb-wide")); return; }
  if (t.id === "estopBtn") return estop();
  if (t.id === "paletteBtn") return palette.open();
  if (t.id === "status") return dock.toggle();
  if (t.dataset.open) return desktop.openPath(t.dataset.open).catch((er) => toast(er.message, "err"));
  if (t.dataset.actionDel) return link.request("history.delete", { id: t.dataset.actionDel }).then(() => bus.emit("actions")).catch((er) => toast(er.message, "err"));
  if (t.dataset.url) return desktop.openUrl(t.dataset.url).catch((er) => toast(er.message, "err"));
  if (t.dataset.approve) {
    const card = t.closest("[data-approval]");
    const a = S.approvals[t.dataset.approve];
    if (!a) return;
    const edited = {};
    let changed = false;
    if (card) $$("[data-edit]", card).forEach((ta) => { edited[ta.dataset.edit] = ta.value; if (ta.value !== a.args[ta.dataset.edit]) changed = true; });
    const remember = card ? $("[data-remember]", card)?.checked : false;
    t.disabled = true;
    return agent.confirmAction(a.id, changed ? { ...a.args, ...edited } : null, remember)
      .catch((er) => { toast(er.message, "err"); t.disabled = false; });
  }
  if (t.dataset.deny) return agent.denyAction(t.dataset.deny).catch((er) => toast(er.message, "err"));
  if (t.dataset.revise) {
    const card = t.closest("[data-approval]");
    const body = $("[data-rev-body]", card)?.value, subject = $("[data-rev-subject]", card)?.value;
    t.disabled = true;
    return link.request("comms.revise", { approval_id: t.dataset.revise, body, subject: subject ?? null })
      .then(() => toast("Изменил — подтвердите ещё раз", "ok")).catch((er) => { toast(er.message, "err"); t.disabled = false; });
  }
  if (t.dataset.taskAct) return link.request(t.dataset.taskAct, { task_id: t.dataset.task }).then((r) => r.message && toast(r.message)).catch((er) => toast(er.message, "err"));
  if (t.dataset.orderDecide) {
    const approve = t.dataset.orderDecide === "commit";
    t.closest(".actions")?.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    return link.request("fashion.decide", { id: t.dataset.proposal, approve, message_id: t.dataset.mid || null })
      .then((r) => toast(!r.acted ? ({ committed: "Это изменение уже сохранено", cancelled: "Это предложение уже отменено или заменено новым",
          missing: "Предложение не найдено" }[r.status] || "Решение уже принято") + " — ничего не менял"
        : approve ? "Сохранено в заказах" + (r.messages?.length ? ": " + r.messages.slice(0, 2).join("; ") : "") : "Изменение отменено",
        r.acted ? "ok" : "warn"))
      .catch((er) => { toast(er.message, "err"); t.closest(".actions")?.querySelectorAll("button").forEach((b) => { b.disabled = false; }); });
  }
  if (t.dataset.act === "estop_clear") return agent.control("estop_clear").then((r) => toast(r.message, "ok"));
  if (t.dataset.act === "conv") { voice.toggleConversation(); return; }
  if (t.dataset.act === "show-approvals") { const a = Object.values(S.approvals).find((x) => x.status === "pending"); if (a) { dock.show(); dock.focus(); } return; }
  if (t.dataset.act === "toggle-mute") { voice.setMuted(!voice.muted); bus.emit("voice"); return; }
});

async function estop() {
  voice.stopAll();
  try { const r = await agent.control("estop"); toast(r.message, "err"); } catch (e) { toast(e.message, "err"); }
}

const typingTarget = (el) => el && (el.tagName === "TEXTAREA" || el.tagName === "SELECT" || (el.tagName === "INPUT" && !["checkbox", "radio", "range", "button"].includes(el.type)) || el.isContentEditable);
document.addEventListener("keydown", (e) => {
  const k = e.code;
  if (e.ctrlKey && e.shiftKey && (e.key === "X" || e.key === "x" || k === "KeyX")) { e.preventDefault(); return estop(); }
  if (e.ctrlKey && e.shiftKey && k === "Space") { e.preventDefault(); if (!voice.muted && voice.supported) voice.toggleConversation(); return; }
  if (e.ctrlKey && k === "Space") { e.preventDefault(); if (!voice.muted && voice.supported) voice.listenOnce(); return; }
  if (e.ctrlKey && !e.shiftKey && !e.altKey && k === "KeyK") { e.preventDefault(); palette.isOpen ? palette.close() : palette.open(); return; }
  if (e.ctrlKey && !e.shiftKey && !e.altKey && k === "KeyJ") { e.preventDefault(); dock.focus(); return; }
  if (palette.isOpen) return;
  if (e.key === "Escape") {
    if (attachPop) { attachPop.remove(); attachPop = null; return; }
    if (voice.recording || voice.audio) { voice.stopAll(); return; }
    if (dock.state.expanded && !dock.state.pinned) { dock.toggle(); return; }
    // Esc stops the running task — from an empty composer or with nothing focused, never while typing.
    const a = document.activeElement;
    if (a === document.body || (a === input && !input.value)) { stopCurrent(); return; }
  }
  if (e.ctrlKey && !e.shiftKey && !e.altKey) {
    if (k === "Comma") { e.preventDefault(); nav("settings"); return; }
    const m = /^Digit([0-9])$/.exec(k);
    const w = m && WORKSPACES.find((x) => x.hotkey === m[1]);
    if (w) { e.preventDefault(); nav(w.id); return; }
  }
  if (!typingTarget(document.activeElement) && !e.ctrlKey && !e.altKey && !e.metaKey) {
    if (e.key === "/") { e.preventDefault(); if (S.view === "chat") input.focus(); else dock.focus(); return; }
    // Space with nothing focused = say one phrase (never steals a space from a field or a focused button).
    if (k === "Space" && document.activeElement === document.body && !voice.muted && voice.supported) { e.preventDefault(); voice.listenOnce(); }
  }
});

// ------------------------------------------------------------------ companion & palette
const dock = createCompanionDock({
  ...ctx,
  agentInput: () => ({ connected: S.connected, core: S.core, voice: S.voice, micOpen: voice.listening, muted: voice.muted,
    tasks: Object.values(S.tasks), approvals: Object.values(S.approvals), streaming: S.streaming, progress: S.progress,
    flash: S.flash, now: Date.now() }),
  lastReply: () => S.lastReply,
  onLayout: (docked) => { app.dataset.cdock = docked ? "1" : "0"; },
});

function commands() {
  const C = [];
  const go = (w) => ({ id: "nav:" + w.id, title: w.title, sub: w.context !== w.title ? w.context : "", group: "Перейти", icon: w.icon,
    hint: w.hotkey ? "Ctrl+" + w.hotkey : w.id === "settings" ? "Ctrl+," : "", keywords: `открыть раздел ${w.id} ${w.context}`, run: () => nav(w.id) });
  for (const w of ALL) if (w.id !== S.view) C.push(go(w));
  const cur = stoppable();
  if (cur && cur !== "speech") C.unshift({ id: "stop", title: "Остановить текущую задачу", group: "JARVIS", icon: "stop", hint: "Esc", keywords: "стоп cancel отмена", run: () => stopCurrent() });
  if (cur === "speech") C.unshift({ id: "quiet", title: "Замолчать", group: "JARVIS", icon: "stop", hint: "Esc", keywords: "стоп тише", run: () => voice.stopAll() });
  C.push(
    { id: "focus", title: "Спросить JARVIS", sub: "Компаньон с полем ввода — из любого раздела", group: "JARVIS", icon: "spark", hint: "Ctrl+J", keywords: "focus фокус", run: () => dock.focus() },
    { id: "listen", title: "Сказать одну фразу", group: "JARVIS", icon: "mic", hint: "Ctrl+Space", keywords: "микрофон голос слушать listen", run: () => voice.listenOnce() },
    { id: "conv", title: voice.conversation ? "Закончить разговор" : "Разговор без кнопок", group: "JARVIS", icon: "wave", hint: "Ctrl+Shift+Space", keywords: "голос микрофон conversation", run: () => voice.toggleConversation() },
    { id: "pause", title: "Пауза всех задач", group: "JARVIS", icon: "pause", keywords: "pause приостановить", run: () => agent.control("pause_all").then((r) => toast(r.message)) },
    { id: "resume", title: "Продолжить задачи", group: "JARVIS", icon: "play", keywords: "resume продолжить", run: () => agent.control("resume_all").then((r) => toast(r.message)) },
    S.core.estop ? { id: "estop_clear", title: "Снять экстренную остановку", group: "JARVIS", icon: "play", keywords: "estop снять", run: () => agent.control("estop_clear").then((r) => toast(r.message, "ok")) }
      : { id: "estop", title: "Экстренная остановка", sub: "Остановить всё и заблокировать действия", group: "JARVIS", icon: "estop", hint: "Ctrl+Shift+X", keywords: "стоп stop emergency", run: () => estop() },
    { id: "newconv", title: "Новая беседа", group: "JARVIS", icon: "chat", keywords: "new conversation начать заново", run: () => agent.control("new_conversation").then(() => nav("chat")) },
    { id: "help", title: "Что ты умеешь?", group: "JARVIS", icon: "spark", keywords: "помощь help команды", run: () => submit("Что ты умеешь?", { source: "text" }) },
    { id: "sys", title: "Состояние системы", group: "Быстрые действия", icon: "cpu", keywords: "батарея память диск процессор", run: () => submit("Состояние системы", { stay: true }) },
    { id: "win", title: "Какие окна открыты", group: "Быстрые действия", icon: "window", keywords: "окна hyprland", run: () => submit("Какие окна открыты", { stay: true }) },
    { id: "find", title: "Найти файл…", group: "Быстрые действия", icon: "search", keywords: "поиск файлов", run: () => nav("files") },
    { id: "cd-show", title: dock.state.hidden ? "Показать JARVIS" : "Скрыть JARVIS до следующего события", group: "Компаньон", icon: dock.state.hidden ? "spark" : "eyeOff", keywords: "компаньон companion", run: () => (dock.state.hidden ? dock.show() : dock.hide()) },
    { id: "cd-dock", title: "Вернуть JARVIS на место", sub: "В верхний левый угол", group: "Компаньон", icon: "dock", keywords: "компаньон позиция сбросить", run: () => dock.redock() },
    { id: "cd-pin", title: dock.state.pinned ? "Открепить JARVIS" : "Закрепить JARVIS открытым", group: "Компаньон", icon: "pin", keywords: "компаньон pin", run: () => dock.togglePin() },
    { id: "sb", title: app.classList.contains("sb-wide") ? "Скрыть подписи меню" : "Показать подписи меню", group: "Интерфейс", icon: "sidebar", keywords: "sidebar боковая панель", run: () => setSidebarWide(!app.classList.contains("sb-wide")) },
    ...[["", "Тема как в системе"], ["dark", "Тёмная тема"], ["light", "Светлая тема"]].map(([v, title]) => ({ id: "theme:" + v, title, group: "Интерфейс", icon: "image", keywords: "тема theme оформление",
      run: () => { if (v) { document.documentElement.dataset.theme = v; localStorage.setItem("jarvis.theme", v); } else { delete document.documentElement.dataset.theme; localStorage.removeItem("jarvis.theme"); } } })),
  );
  const recent = Object.values(S.tasks).filter((t) => isUserTask(t) && t.route !== "ui").sort((a, b) => b.created_at - a.created_at).slice(0, 6);
  for (const t of recent) C.push({ id: "task:" + t.id, title: t.request, sub: STATE_RU[t.state] || t.state, group: "Недавние задачи", icon: "tasks", keywords: "задача task", run: () => openTask(t.id) });
  return C;
}
const palette = createPalette({ commands, toast, ask: (q) => submit(q, { source: "text", stay: S.view !== "chat" }) });

// ------------------------------------------------------------------ wiring
const shell = frameBatch(() => { dock.render(); renderStatus(); renderComposer(); renderSidebar(); });
bus.on(["link", "core", "tasks", "approvals", "voice", "stream", "messages", "providers", "reminders", "workspace"], shell);
bus.on(["tasks", "approvals", "messages"], frameBatch(renderThread));

buildSidebar();
setSidebarWide(prefs.get("sidebar", innerWidth >= 1560 ? "wide" : "narrow") === "wide");
const start = prefs.get("ws", "chat");
nav(byId(start) && start !== "history" ? start : "chat");
shell();
window.__jarvis = { S, link, voice, bus, dock, palette, nav, openTask, render: () => { turnCache.clear(); renderThread(); } };   // for automated UI tests
