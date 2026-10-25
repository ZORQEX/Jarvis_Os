// Action history: only the lasting changes JARVIS made on the computer (sites, programs, code, files, notes), by day.
// Not in the sidebar (Ctrl+K «История действий» or «открой историю действий»); it was a tab of the old task center.
import { ICON, esc, fmtMs, fmtTime, shortPath } from "../render.js";
import { byId } from "./registry.js";
import { header, emptyState } from "./common.js";

const KIND_RU = { change: "Изменения на компьютере", note: "Заметка", file: "Файлы", install: "Установка программ", publish: "Публикация в интернете" };

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

export function create(ctx) {
  const ws = byId("history");
  const el = document.createElement("section");
  el.className = "view view-ws";
  el.dataset.ws = "history";
  el.setAttribute("aria-label", "История действий");
  el.hidden = true;
  el.innerHTML = `<div class="ws narrow">${header(ws, ctx.env(), {})}
    <p class="lead muted" style="margin:-6px 0 0">Только то, что JARVIS действительно сделал на компьютере: сайты, программы, код, файлы, заметки.
    Сообщения, громкость, окна и вопросы сюда не попадают.</p><div id="hList"><div class="empty-note">Загружаю…</div></div></div>`;

  async function draw() {
    const box = el.querySelector("#hList");
    let list = [];
    try { list = await ctx.link.request("history.list", { limit: 150 }); } catch (e) { ctx.toast(e.message, "err"); }
    const groups = [];
    for (const a of list) {
      const label = dayLabel(a.created_at);
      if (!groups.length || groups.at(-1).label !== label) groups.push({ label, items: [] });
      groups.at(-1).items.push(a);
    }
    box.innerHTML = groups.length ? groups.map((g) => `<section class="action-day"><h2>${esc(g.label)}</h2>${g.items.map(actionCard).join("")}</section>`).join("")
      : `<div class="panel">${emptyState({ icon: "history", title: "Пока пусто", text: "Попросите, например: «Создай сайт-визитку в папке Projects/site» — после выполнения действие появится здесь." })}</div>`;
  }

  const offs = [];
  return {
    el,
    show() { draw(); if (!offs.length) offs.push(ctx.bus.on("actions", draw)); },
    hide() { while (offs.length) offs.pop()(); },
  };
}
