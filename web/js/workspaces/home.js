// Home: the calm command center — greeting, real status, one place to ask, what is running now, the last answer,
// connections and the latest lasting actions. Five blocks, all from the core's state; no invented numbers.
import { ICON, esc, md, fmtMs } from "../render.js";
import { byId } from "./registry.js";
import { header, ago } from "./common.js";
import { statusOf, planRows, isUserTask, isWorking, canRetry } from "../core/tasks-model.js";
import { frameBatch } from "../core/store.js";

const MARK = { done: "✓", active: "●", waiting: "!", failed: "✕", skipped: "–", pending: "○" };
const QUICK = [
  { label: "Состояние системы", run: "Состояние системы" },
  { label: "Какие окна открыты", run: "Какие окна открыты" },
  { label: "Найти файл…", fill: "Найди файл " },
  { label: "Заметка…", fill: "Запиши заметку: " },
  { label: "Напомнить…", fill: "Напомни через 10 минут " },
  { label: "Что ты умеешь?", run: "Что ты умеешь?" },
];

export function create(ctx) {
  const ws = byId("home");
  const el = document.createElement("section");
  el.className = "view view-ws";
  el.dataset.ws = "home";
  el.setAttribute("aria-label", "Главная");
  el.hidden = true;
  let actions = [], actionsAt = 0;

  el.innerHTML = `<div class="ws home">
    <div class="home-hello"><h1 id="hHello"></h1><p id="hStatus"></p></div>
    <form class="home-ask" id="hAsk" autocomplete="off">
      <input id="hInput" aria-label="Что сделать?" placeholder="Что сделать? Например: «Найди файл смета» или «Напомни через 10 минут позвонить»">
      <button type="button" class="icon-btn mic" id="hMic" aria-label="Сказать фразу" title="Сказать фразу (Ctrl+Space)">${ICON.mic}</button>
      <button type="submit" class="icon-btn go" id="hGo" aria-label="Отправить" disabled>${ICON.arrowUp}</button>
    </form>
    <div class="quick" id="hQuick">${QUICK.map((q, i) => `<button class="say" data-q="${i}">${esc(q.label)}</button>`).join("")}</div>
    <div class="home-grid">
      <div class="home-col"><section class="blk" id="hNow"></section><section class="blk" id="hLast"></section></div>
      <div class="home-col"><section class="blk" id="hConn"></section><section class="blk" id="hActs"></section></div>
    </div></div>`;

  const input = el.querySelector("#hInput"), go = el.querySelector("#hGo");
  input.addEventListener("input", () => { go.disabled = !input.value.trim(); });
  el.querySelector("#hAsk").addEventListener("submit", (e) => {
    e.preventDefault();
    const t = input.value.trim();
    if (!t) return;
    input.value = ""; go.disabled = true;
    ctx.submit(t, { stay: true });
  });
  el.querySelector("#hMic").addEventListener("click", () => ctx.voice.listenOnce());
  el.querySelector("#hQuick").addEventListener("click", (e) => {
    const b = e.target.closest("[data-q]");
    if (!b) return;
    const q = QUICK[+b.dataset.q];
    if (q.run) return ctx.submit(q.run, { stay: true });
    input.value = q.fill; go.disabled = false; input.focus();
  });
  el.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-h]");
    if (!b) return;
    const id = b.dataset.id;
    try {
      if (b.dataset.h === "stop") await ctx.agent.cancelTask(id);
      if (b.dataset.h === "retry") { const t = ctx.findTask(id); if (t) ctx.submit(t.request, { stay: true }); }
      if (b.dataset.h === "task") ctx.openTask(id);
    } catch (er) { ctx.toast(er.message, "err"); }
  });

  // ---------------------------------------------------------------- blocks
  function hello() {
    const h = new Date().getHours();
    el.querySelector("#hHello").textContent = (h < 5 ? "Доброй ночи" : h < 12 ? "Доброе утро" : h < 18 ? "Добрый день" : "Добрый вечер") + ".";
    const S = ctx.S, a = ctx.agentState();
    const ai = S.providers.filter((p) => p.health === "CONNECTED").map((p) => p.name);
    const part = S.providers.filter((p) => p.health === "DEGRADED").map((p) => p.name + " — частично");
    const bits = [ctx.connected() ? `JARVIS: ${a.label.toLowerCase()}` : "Нет связи с ядром JARVIS — переподключаюсь"];
    if (ctx.connected()) bits.push(ai.length || part.length ? `ИИ: ${[...ai, ...part].join(", ")}` : "ИИ не подключён — локальные команды работают");
    bits.push(ctx.voice.listening ? "микрофон включён" : ctx.voice.muted ? "микрофон выключен" : "микрофон не используется");
    el.querySelector("#hStatus").textContent = bits.join(" · ");
    el.querySelector("#hMic").classList.toggle("recording", ctx.voice.recording);
    el.querySelector("#hMic").disabled = ctx.voice.muted || !ctx.voice.supported;
  }

  function now() {
    const S = ctx.S;
    const act = Object.values(S.tasks).filter((t) => isUserTask(t) && isWorking(t)).sort((a, b) => a.created_at - b.created_at);
    const pend = Object.values(S.approvals).filter((a) => a.status === "pending");
    const box = el.querySelector("#hNow");
    box.innerHTML = `<div class="blk-h">${ICON.bolt} Сейчас<button class="more" data-nav="tasks">Все задачи</button></div>` + (act.length
      ? act.map((t) => {
        const st = statusOf(t), rows = planRows(t).slice(-4);
        return `<div class="mini-task"><div class="top"><span class="req" title="${esc(t.request)}">${esc(t.request)}</span>
          <span class="pill ${st.key}">${esc(st.label)}</span>
          <button class="icon-btn" data-h="stop" data-id="${esc(t.id)}" aria-label="Остановить задачу" title="Остановить">${ICON.stop}</button></div>
          ${rows.map((r) => `<div class="plan-row" data-mark="${r.mark}"><span class="m">${MARK[r.mark]}</span><span>${esc(r.label)}</span></div>`).join("")}</div>`;
      }).join("")
      : `<div class="empty-note">${pend.length ? `Ждут вашего решения: ${pend.length} — карточка у JARVIS вверху слева.` : "Ничего не выполняется. Скажите или напишите, что сделать."}</div>`);
  }

  function last() {
    const S = ctx.S;
    const m = [...S.messages].reverse().find((x) => x.role === "assistant" && S.tasks[x.task_id]?.route !== "ui");   // not «Открываю…»
    const box = el.querySelector("#hLast");
    if (!m) { box.innerHTML = `<div class="blk-h">${ICON.chat} Последний ответ</div><div class="empty-note">Беседа пока пустая.</div>`; return; }
    const t = m.task_id ? S.tasks[m.task_id] : null;
    const u = t ? S.messages.find((x) => x.task_id === t.id && x.role === "user") : null;
    const st = t ? statusOf(t) : null;
    const took = t?.finished_at ? fmtMs((t.finished_at - t.created_at) * 1000) : "";
    box.innerHTML = `<div class="blk-h">${ICON.chat} Последний ответ<button class="more" data-nav="chat">Открыть беседу</button></div>
      ${u ? `<div class="req">${esc(u.text)}</div>` : ""}
      <div class="reply-text msg-ai">${md(m.text.length > 1200 ? m.text.slice(0, 1200) + "…" : m.text)}</div>
      <div class="meta">${st ? `<span class="pill ${st.key}">${esc(st.label)}</span>` : ""}<span>${esc(ago(m.created_at))}</span>${took ? `<span class="sep">·</span><span>${took}</span>` : ""}
        ${t && canRetry(t) ? `<button class="btn ghost" data-h="retry" data-id="${esc(t.id)}" style="height:26px;font-size:12.5px">${ICON.retry} Повторить</button>` : ""}
        ${t ? `<button class="btn ghost" data-h="task" data-id="${esc(t.id)}" style="height:26px;font-size:12.5px">Подробнее</button>` : ""}</div>`;
  }

  function conn() {
    const S = ctx.S, P = ctx.providers();
    const row = (icon, name, state, label, nav) => `<button class="conn" ${nav ? `data-nav="${nav}"` : ""}>${ICON[icon]}<span>${esc(name)}</span>
      <span class="cpill ${state}">${esc(label)}</span></button>`;
    if (!ctx.connected()) {      // last known states would be a guess now
      el.querySelector("#hConn").innerHTML = `<div class="blk-h">${ICON.shield} Подключения</div>`
        + [["spark", "Claude"], ["telegram", "Telegram"], ["whatsapp", "WhatsApp"], ["mail", "Почта"]].map(([i, n]) => row(i, n, "", "нет связи с ядром", null)).join("");
      return;
    }
    const ch = (id, ws, icon, name) => {
      const p = P[id];
      if (!p) return row(icon, name, "", "проверяю…", ws);
      const m = { connected: ["ok", "подключено"], connecting: ["warn", "подключение…"], error: ["err", "ошибка"] }[p.state] || ["", "не подключено"];
      return row(icon, name, m[0], m[1], ws);
    };
    const ai = S.providers.length ? S.providers.map((p) => row("spark", p.name,
      p.health === "CONNECTED" ? "ok" : p.health === "DEGRADED" ? "warn" : "", { CONNECTED: "готов", DEGRADED: "частично", UNCONFIGURED: "не настроен" }[p.health] || String(p.health || "").toLowerCase(), "settings")).join("")
      : row("spark", "Claude", "", ctx.connected() ? "проверяю…" : "нет связи", "settings");
    el.querySelector("#hConn").innerHTML = `<div class="blk-h">${ICON.shield} Подключения</div>${ai}
      ${ch("telegram", "telegram", "telegram", "Telegram")}${ch("whatsapp", "whatsapp", "whatsapp", "WhatsApp")}${ch("email", "mail", "mail", "Почта")}`;
  }

  async function acts(force = false) {
    const box = el.querySelector("#hActs");
    if (force || Date.now() - actionsAt > 30000) {
      try { actions = await ctx.link.request("history.list", { limit: 5 }); actionsAt = Date.now(); } catch { /* offline */ }
    }
    box.innerHTML = `<div class="blk-h">${ICON.history} Последние действия<button class="more" data-nav="history">История</button></div>`
      + (actions.length ? actions.map((a) => `<div class="act-row"><span title="${esc(a.title)}">${esc(a.title)}</span><span class="w">${esc(ago(a.created_at))}</span></div>`).join("")
        : `<div class="empty-note">Здесь появятся изменения на компьютере: сайты, программы, файлы, заметки.</div>`);
  }

  const redraw = frameBatch(() => { if (el.hidden) return; hello(); now(); last(); conn(); });
  const offs = [];
  return {
    el,
    show() {
      redraw(); acts(true);
      if (!offs.length) offs.push(ctx.bus.on(["link", "core", "tasks", "approvals", "messages", "providers", "voice"], redraw),
        ctx.bus.on("actions", () => acts(true)));
      setTimeout(() => { if (!el.hidden && document.activeElement === document.body) input.focus(); }, 0);
    },
    hide() { while (offs.length) offs.pop()(); },
    focusAsk() { input.focus(); },
  };
}
