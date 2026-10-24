// Task Center: every task the person gave JARVIS, live. List (active / all / problems) + the selected task's plan,
// execution timeline (from persisted step and approval times) and controls: stop, pause, resume, retry.
// Second tab: action history (lasting changes on the computer), moved here from its own page.
import { ICON, esc, md, fmtMs, fmtTime, shortPath, approvalCard, resultCard } from "../render.js";
import { byId } from "./registry.js";
import { header, emptyState, ago, clock } from "./common.js";
import { statusOf, planRows, timeline, groupTasks, upsert, canRetry, isWorking, stepCounts, isUserTask, visibleSteps } from "../core/tasks-model.js";
import { frameBatch } from "../core/store.js";

const MARK = { done: "✓", active: "●", waiting: "!", failed: "✕", skipped: "–", pending: "○" };
const KIND_RU = { change: "Изменения на компьютере", note: "Заметка", file: "Файлы", install: "Установка программ", publish: "Публикация в интернете" };

export function create(ctx) {
  const ws = byId("tasks");
  const el = document.createElement("section");
  el.className = "view view-ws";
  el.dataset.ws = "tasks";
  el.setAttribute("aria-label", "Задачи");
  el.hidden = true;
  const st = { tab: "tasks", filter: "active", list: [], loaded: false, sel: null, detail: null, detailFor: null, history: [] };

  async function load() {
    try { st.list = (await ctx.agent.recentTasks(150)).filter(isUserTask); st.loaded = true; }
    catch (e) { ctx.toast(e.message, "err"); }
    // Active tasks of the snapshot may be newer than the list.
    for (const t of Object.values(ctx.S.tasks)) if (isUserTask(t)) st.list = upsert(st.list, t);
    if (!st.sel || !filtered().some((t) => t.id === st.sel)) {
      const g = groupTasks(st.list);
      if (!st.sel && !g.active.length && !g.attention.length) st.filter = "all";     // nothing running: show recent work
      st.sel = filtered()[0]?.id || null;
    }
    draw();
    loadDetail();
  }
  async function loadDetail(force = false) {
    if (!st.sel || (!force && st.detailFor === st.sel)) return;
    const id = st.sel;
    try { const d = await ctx.agent.taskDetail(id); if (st.sel === id) { st.detail = d; st.detailFor = id; st.list = upsert(st.list, d.task); drawDetail(); } }
    catch (e) { if (st.sel === id) { st.detail = null; st.detailFor = id; drawDetail(); } }
  }

  function draw() {
    if (el.hidden) return;
    const g = groupTasks(st.list);
    const tabs = `<div class="seg-tabs" role="tablist">
      <button role="tab" data-tab="tasks" aria-selected="${st.tab === "tasks"}">Задачи</button>
      <button role="tab" data-tab="history" aria-selected="${st.tab === "history"}">История действий</button></div>`;
    if (st.tab === "history") {
      el.innerHTML = `<div class="ws narrow">${header(ws, ctx.env(), { title: "История действий", right: tabs })}
        <p class="lead muted" style="margin:-6px 0 0">Только то, что JARVIS действительно сделал на компьютере: сайты, программы, код, файлы, заметки.
        Сообщения, громкость, окна и вопросы сюда не попадают.</p><div id="hList"></div></div>`;
      drawHistory();
      return;
    }
    const counts = { active: g.active.length, problems: g.attention.length + g.finished.filter((t) => t.state === "failed").length, all: g.active.length + g.attention.length + g.finished.length };
    el.innerHTML = `<div class="ws">${header(ws, ctx.env(), { title: "Задачи", right: tabs })}
      <div class="seg-tabs" role="tablist" aria-label="Фильтр">
        ${[["active", "Активные"], ["all", "Все"], ["problems", "Проблемы"]].map(([k, l]) =>
          `<button role="tab" data-filter="${k}" aria-selected="${st.filter === k}">${l} <span class="n">${counts[k]}</span></button>`).join("")}</div>
      <div class="tc-split"><div class="tc-list" id="tcList" role="listbox" aria-label="Задачи"></div><div class="tc-detail" id="tcDetail"></div></div></div>`;
    drawList();
    drawDetail();
  }

  function filtered() {
    const g = groupTasks(st.list);
    if (st.filter === "active") return [...g.attention, ...g.active];
    if (st.filter === "problems") return [...g.attention, ...g.finished.filter((t) => t.state === "failed")];
    return [...g.attention, ...g.active.slice().reverse(), ...g.finished];
  }

  function drawList() {
    const box = el.querySelector("#tcList");
    if (!box) return;
    const items = filtered();
    if (!items.length) {
      box.innerHTML = st.loaded ? `<div class="panel">${emptyState({ icon: "tasks", title: st.filter === "active" ? "Активных задач нет" : st.filter === "problems" ? "Проблем нет" : "Задач пока не было",
        text: "Задачи появляются, когда вы просите JARVIS что-то сделать — голосом, в беседе или из любого раздела.",
        tries: st.filter === "problems" ? [] : ["Состояние системы", "Найди файл смета"] })}</div>` : `<div class="empty-note">Загружаю задачи…</div>`;
      return;
    }
    box.innerHTML = items.map((t) => {
      const s = statusOf(t), c = stepCounts(t);
      return `<button class="tc-card" role="option" data-sel="${esc(t.id)}" aria-selected="${t.id === st.sel}">
        <div class="top"><span class="req" title="${esc(t.request)}">${esc(t.request)}</span><span class="pill ${s.key}">${esc(s.label)}</span></div>
        <div class="sub"><span>${esc(ago(t.created_at))}</span>${c.total ? `<span>шагов: ${c.done} из ${c.total}</span>` : ""}
          ${t.finished_at ? `<span>${fmtMs((t.finished_at - t.created_at) * 1000)}</span>` : ""}</div></button>`;
    }).join("");
  }

  function drawDetail() {
    const box = el.querySelector("#tcDetail");
    if (!box) return;
    const t = st.list.find((x) => x.id === st.sel);
    if (!t) { box.innerHTML = `<div class="panel">${emptyState({ icon: "tasks", title: "Выберите задачу", text: "Здесь будут её шаги, ход выполнения и управление." })}</div>`; return; }
    const s = statusOf(t);
    const approvals = st.detailFor === t.id && st.detail ? st.detail.approvals : Object.values(ctx.S.approvals).filter((a) => a.task_id === t.id);
    const rows = planRows(t), tl = timeline(t, approvals);
    const pending = approvals.filter((a) => a.status === "pending");
    const working = isWorking(t);
    const reply = [...ctx.S.messages].reverse().find((m) => m.task_id === t.id && m.role === "assistant");
    const cards = visibleSteps(t).map((x) => resultCard(x)).filter(Boolean).join("");
    box.innerHTML = `<div class="panel" style="display:flex;flex-direction:column;gap:14px">
      <div><h2>${esc(t.request)}</h2>
        <div class="tc-meta" style="margin-top:6px"><span class="pill ${s.key}">${esc(s.label)}</span>
          <span>начата ${esc(fmtTime(t.created_at))}</span>${t.finished_at ? `<span>· заняла ${fmtMs((t.finished_at - t.created_at) * 1000)}</span>` : ""}
          <span>· ${esc({ local: "на компьютере, без ИИ", tool: "на компьютере, без ИИ", ai: "через ИИ", comms: "сообщения", ui: "раздел окна" }[t.route] || t.route || "")}</span></div></div>
      <div class="tc-ctl">
        ${working ? `<button class="btn danger" data-t="cancel">${ICON.stop} Остановить</button>` : ""}
        ${["running", "planning"].includes(t.state) ? `<button class="btn ghost" data-t="pause">${ICON.pause} Пауза</button>` : ""}
        ${t.state === "paused" ? `<button class="btn" data-t="resume">${ICON.play} Продолжить</button>` : ""}
        ${t.state === "blocked" ? `<button class="btn ghost" data-t="cancel">Закрыть</button>` : ""}
        ${canRetry(t) ? `<button class="btn" data-t="retry">${ICON.retry} Повторить</button>` : ""}
        <button class="btn ghost" data-nav="chat">Открыть в беседе</button></div>
      ${pending.map((a) => approvalCard(a)).join("")}
      ${t.error && !working ? `<div class="callout err">${ICON.alert}<span>${esc(t.error)}</span></div>` : ""}
      ${rows.length ? `<div><div class="blk-h" style="margin-bottom:6px">План</div><div class="cd-plan" style="max-height:none">${rows.map((r) =>
        `<div class="plan-row" data-mark="${r.mark}"><span class="m">${MARK[r.mark]}</span><span title="${esc(r.label)}">${esc(r.label)}${r.error && r.mark === "failed" ? `<span class="e">${esc(r.error)}</span>` : ""}</span></div>`).join("")}</div></div>` : ""}
      <div><div class="blk-h" style="margin-bottom:6px">Ход выполнения</div><div class="tl">${tl.map((e) =>
        `<div class="tl-ev" data-mark="${e.mark}" data-kind="${e.kind}"><span class="ts">${clock(e.ts)}</span><span class="dot"></span>
          <span><span class="lb">${esc(e.label)}</span>${e.ms != null ? `<span class="ms">${fmtMs(e.ms)}</span>` : ""}${e.detail ? `<span class="dt">${esc(e.detail)}</span>` : ""}</span></div>`).join("")}</div></div>
      ${reply ? `<div><div class="blk-h" style="margin-bottom:6px">Ответ</div><div class="reply-text msg-ai">${md(reply.text)}</div></div>` : ""}
      ${cards ? `<div style="display:flex;flex-direction:column;gap:10px">${cards}</div>` : ""}
    </div>`;
  }

  // ---------------------------------------------------------------- action history (was its own page)
  function dayLabel(ts) {
    const d = new Date(ts * 1000), t = new Date(), y = new Date(Date.now() - 864e5);
    if (d.toDateString() === t.toDateString()) return "Сегодня";
    if (d.toDateString() === y.toDateString()) return "Вчера";
    return d.toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: d.getFullYear() === t.getFullYear() ? undefined : "numeric" });
  }
  function actionCard(a) {
    const det = a.details || {};
    const files = a.files || [];
    const more = (a.files_total || files.length) - Math.min(files.length, 6);
    const meta = [fmtTime(a.created_at)];
    if (det.duration_ms >= 1000) meta.push(fmtMs(det.duration_ms));
    if (det.tests) meta.push(det.tests.ok ? "тесты прошли" : det.tests.ran ? "тесты не прошли" : "");
    if (typeof det.cost_usd === "number") meta.push(`$${det.cost_usd.toFixed(det.cost_usd < 0.1 ? 3 : 2)}`);
    return `<article class="action ${a.status === "failed" ? "failed" : ""}">
      <div class="action-head">
        <span class="action-icon">${a.kind === "note" ? ICON.note : a.kind === "publish" ? ICON.globe : ICON.spark}</span>
        <div class="action-title"><b>${esc(a.title)}</b><span>${esc(KIND_RU[a.kind] || "Действие")}${a.status === "failed" ? " · не удалось" : ""}</span></div>
        <button class="icon-btn action-del" data-action-del="${esc(a.id)}" aria-label="Убрать из истории" title="Убрать из истории (файлы останутся)">${ICON.x}</button>
      </div>
      ${a.summary ? `<p class="action-sum">${esc(a.summary)}</p>` : ""}
      ${det.error ? `<p class="action-err">${esc(det.error)}</p>` : ""}
      ${files.length ? `<div class="action-files">${files.slice(0, 6).map((f) => typeof f === "string" ? `<code>${esc(f)}</code>`
        : `<code class="${esc(f.op)}" title="${{ new: "новый файл", edit: "изменён", del: "удалён" }[f.op] || ""}">${esc(f.p)}</code>`).join("")}${more > 0 ? `<span>ещё ${more}</span>` : ""}</div>` : ""}
      <div class="action-foot">
        ${a.folder ? `<button class="action-folder" data-open="${esc(a.folder)}" title="Открыть папку: ${esc(a.folder)}">${ICON.folder}<span>${esc(shortPath(a.folder_short || a.folder))}</span></button>` : ""}
        ${det.url ? `<button class="action-folder" data-url="${esc(det.url)}" title="Открыть сайт">${ICON.globe}<span>${esc(det.url)}</span></button>` : ""}
        <span class="action-meta">${meta.filter(Boolean).map(esc).join(" · ")}</span>
      </div>
    </article>`;
  }
  async function drawHistory() {
    const box = el.querySelector("#hList");
    if (!box) return;
    try { st.history = await ctx.link.request("history.list", { limit: 150 }); } catch (e) { ctx.toast(e.message, "err"); }
    const groups = [];
    for (const a of st.history) {
      const label = dayLabel(a.created_at);
      if (!groups.length || groups.at(-1).label !== label) groups.push({ label, items: [] });
      groups.at(-1).items.push(a);
    }
    box.innerHTML = groups.length ? groups.map((g) => `<section class="action-day"><h2>${esc(g.label)}</h2>${g.items.map(actionCard).join("")}</section>`).join("")
      : `<div class="panel">${emptyState({ icon: "history", title: "Пока пусто", text: "Попросите, например: «Создай сайт-визитку в папке Projects/site» — после выполнения действие появится здесь." })}</div>`;
  }

  // ---------------------------------------------------------------- interaction
  el.addEventListener("click", async (e) => {
    const tab = e.target.closest("[data-tab]");
    if (tab) { st.tab = tab.dataset.tab; draw(); return; }
    const f = e.target.closest("[data-filter]");
    if (f) {
      st.filter = f.dataset.filter;
      const list = filtered();
      if (!list.some((t) => t.id === st.sel)) { st.sel = list[0]?.id || null; loadDetail(); }   // the detail always matches the list
      drawList(); drawDetail();
      el.querySelectorAll("[data-filter]").forEach((b) => b.setAttribute("aria-selected", String(b === f)));
      return;
    }
    const sel = e.target.closest("[data-sel]");
    if (sel) { st.sel = sel.dataset.sel; drawList(); drawDetail(); loadDetail(); return; }
    const b = e.target.closest("[data-t]");
    if (!b) return;
    const t = st.list.find((x) => x.id === st.sel);
    if (!t) return;
    b.disabled = true;
    try {
      if (b.dataset.t === "cancel") { const r = await ctx.agent.cancelTask(t.id); if (r?.message) ctx.toast(r.message); }
      if (b.dataset.t === "pause") await ctx.agent.pauseTask(t.id);
      if (b.dataset.t === "resume") await ctx.agent.resumeTask(t.id);
      if (b.dataset.t === "retry") { const id = await ctx.submit(t.request, { stay: true }); if (id) { st.sel = id; } }
    } catch (er) { ctx.toast(er.message, "err"); }
    b.disabled = false;
  });

  const redraw = frameBatch(() => { drawList(); drawDetail(); });
  const offs = [];
  return {
    el,
    show(params = {}) {
      if (params.tab) st.tab = params.tab;
      if (params.task) { st.sel = params.task; st.filter = "all"; st.tab = "tasks"; }
      draw();
      load();
      if (!offs.length) {
        offs.push(ctx.bus.on("task", (t) => {
          if (!isUserTask(t)) return;
          const isNew = !st.list.some((x) => x.id === t.id);
          st.list = upsert(st.list, t);
          if (isNew && !st.sel) st.sel = t.id;
          if (t.id === st.sel && !isWorking(t)) loadDetail(true);
          if (isNew && st.tab === "tasks") draw(); else redraw();
        }));
        offs.push(ctx.bus.on("approvals", (a) => { if (a?.task_id === st.sel) loadDetail(true); else redraw(); }));
        offs.push(ctx.bus.on(["messages", "link"], redraw));
        offs.push(ctx.bus.on("actions", () => { if (st.tab === "history") drawHistory(); }));
      }
    },
    hide() { while (offs.length) offs.pop()(); },
    findTask: (id) => st.list.find((t) => t.id === id) || null,
  };
}
