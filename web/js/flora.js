// «Флора» workspace: decisions with evidence, drawing viewer with annotations,
// quote comparison, reply editor with approval packet, project journal.
// All content comes from the core; DEMO data is labelled as such.
import { esc, ICON, approvalCard, typeBadge, ruDate, rub, fmtWhen, shortPath } from "./render.js";

let ctx = null, root = null;
const st = {
  data: null, tab: "decisions", item: null, mode: "compare", split: 50, zoom: 1, pan: { x: 0, y: 0 }, fitted: false,
  tool: "hand", shapes: [], annFile: null, saveTimer: null, draftTimer: null, draftState: "",
};

const TABS = [["decisions", "Решения"], ["drawing", "Чертёж"], ["quotes", "Сметы"], ["reply", "Ответ"], ["journal", "Журнал"]];

export async function render(el, c) {
  ctx = c; root = el;
  if (!st.data) {
    el.innerHTML = `<div class="flora"><div class="faint">Загружаю проект…</div></div>`;
    await load();
  }
  draw();
}

export function setTab(tab) { if (tab) st.tab = tab; if (root) draw(); }
export function selectItem(id) { if (id) { st.item = id; st.tab = "decisions"; } if (root) draw(); }

export async function onEvent(ev) {
  if (!root || ctx.S.view !== "flora") return;
  if (ev.kind === "flora.draft") { if (st.data) st.data.draft = ev.draft; if (st.tab === "reply" && !isTyping()) draw(); return; }
  if (ev.kind.startsWith("approval.") || (ev.kind === "task.updated" && ev.task.steps?.some((s) => s.tool_id?.startsWith("flora_")))) {
    if (ev.kind === "task.updated" && ev.task.state === "completed") await load();
    if (st.tab === "reply" || st.tab === "journal") { if (!isTyping()) draw(); }
  }
}

const isTyping = () => document.activeElement && root.contains(document.activeElement) && /TEXTAREA|INPUT/.test(document.activeElement.tagName);

async function load() {
  try { st.data = await ctx.link.request("flora.action", { action: "state" }); }
  catch (e) { root.innerHTML = `<div class="flora"><div class="callout err">${ICON.alert}<span>${esc(e.message)}</span></div></div>`; throw e; }
  if (!st.item) st.item = st.data.analysis.items[0]?.id;
}

function draw() {
  const d = st.data;
  if (!d) return;
  const a = d.analysis;
  const decisions = a.items.filter((i) => i.type === "DECISION_REQUIRED").length;
  const pending = Object.values(ctx.S.approvals).filter((x) => x.status === "pending" && x.tool_id === "flora_send").length;
  root.innerHTML = `<div class="flora">
    <div class="flora-head"><h1>${esc(a.project.name)}</h1><span class="faint">${esc(a.project.code)}</span>
      <span class="demo-badge">${ICON.alert.replace("<svg", '<svg style="width:13px;height:13px"')} демо-данные</span>
      <span class="faint" style="margin-left:auto">Почта: ${esc(d.mail.account)} · ${d.mail.live ? "подключена" : "демо-ящик, не настоящая почта"}</span>
      <button class="icon-btn" data-fl="reload" title="Перечитать почту и документы" aria-label="Обновить">${ICON.search}</button></div>
    <div class="tabs" role="tablist">${TABS.map(([id, label]) => `<button class="tab" role="tab" data-fl-tab="${id}" aria-selected="${st.tab === id}">${label}
      ${id === "decisions" && decisions ? `<span class="count">${decisions}</span>` : ""}${id === "reply" && pending ? `<span class="count">!</span>` : ""}</button>`).join("")}</div>
    <div class="flora-body">${body()}</div></div>`;
  wire();
}

function body() {
  switch (st.tab) {
    case "drawing": return drawingTab();
    case "quotes": return quotesTab();
    case "reply": return replyTab();
    case "journal": return journalTab();
    default: return decisionsTab();
  }
}

// ------------------------------------------------------------------ decisions
function decisionsTab() {
  const a = st.data.analysis;
  const list = a.items.map((i) => mailItem(i)).join("");
  const exc = a.excluded.map((i) => mailItem(i, true)).join("");
  const sel = [...a.items, ...a.excluded].find((i) => i.id === st.item) || a.items[0];
  return `<div class="split"><div class="inbox">${list}
      <details class="excluded"><summary>Скрыто: ${a.excluded.length} — реклама, другие проекты, подозрительное ▾</summary>
        <div class="inbox">${exc}</div></details></div>
    <div class="mail-view">${sel ? mailView(sel) : ""}</div></div>`;
}

function mailItem(i, muted = false) {
  const cat = { MARKETING: "рассылка", OTHER_PROJECT: "другой проект", SUSPICIOUS: "подозрительное" }[i.category];
  return `<button class="mail-item ${muted ? "muted" : ""}" data-fl-item="${esc(i.id)}" aria-selected="${i.id === st.item}">
    <div class="top">${muted ? `<span class="tbadge" ${i.category === "SUSPICIOUS" ? 'style="color:var(--err);background:var(--err-soft)"' : ""}>${esc(cat)}</span>` : typeBadge(i.type)}
      <span class="from">${esc(i.from.name)}</span>${i.deadline ? `<span class="dl">до ${esc(ruDate(i.deadline))}</span>` : ""}</div>
    <div class="subj">${esc(i.subject)}</div>
    ${i.reply ? `<div class="reason" style="color:var(--ok)">✓ ответ отправлен ${esc(fmtWhen(i.reply.sent_at))} — ${esc(REPLY_RU[i.reply.option] || "")}</div>` : ""}</button>`;
}

const REPLY_RU = { clarify: "запрошено уточнение", approve: "согласовано с условиями", decline: "изменение отклонено" };

function factLabel(f) {
  if (f.kind === "deadline") return `Срок: <b>${esc(ruDate(f.value))}</b>`;
  if (f.kind === "delivery") return `Отгрузка: <b>${esc(ruDate(f.value))}</b>`;
  if (f.kind === "date") return `Дата: <b>${esc(ruDate(f.value))}</b>`;
  if (f.kind === "money") return `Сумма: <b>${esc(f.text)}</b>${f.approx ? " (приблизительно)" : ""}`;
  if (f.kind === "dimension") return `${f.element ? esc(f.element) + ": " : "Размер: "}<b>${f.width}×${f.height}</b>`;
  return esc(f.text);
}

function mailView(i) {
  const msg = st.data.messages[i.id];
  const facts = (i.facts || []).map((f, n) => ({ ...f, n })).sort((x, y) => x.evidence.start - y.evidence.start);
  let html = "", pos = 0;
  for (const f of facts) {
    if (f.evidence.field !== "body" || f.evidence.start < pos) continue;
    html += esc(msg.body.slice(pos, f.evidence.start)) + `<mark data-fact="${f.n}">${esc(msg.body.slice(f.evidence.start, f.evidence.end))}</mark>`;
    pos = f.evidence.end;
  }
  html += esc(msg.body.slice(pos));
  const a = st.data.analysis;
  const extra = [];
  if (i.category === "SUSPICIOUS") extra.push(`<div class="callout err">${ICON.shield}<span>Письмо изолировано. Текст содержит команды для ассистента — JARVIS их не выполняет. Ничего не пересылалось.</span></div>`);
  if (i.subject.includes("W-3")) {
    const cl = a.cost_claims.find((c) => c.source.id === i.id);
    if (cl && !cl.matches_lines) extra.push(`<div class="callout warn">${ICON.alert}<span>В письме «${esc(cl.stated_text)}», а по строкам КП разница <b>+${rub(cl.lines_delta)}</b>.
      ${cl.matches_file_total ? "Цифра из письма совпадает с итогом файла, в котором формула пропускает строку." : ""} <button class="btn ghost" data-fl-tab="quotes" style="height:26px">Сметы</button></span></div>`);
    for (const c of a.contradictions) extra.push(`<div class="callout warn">${ICON.alert}<span>Не совпадает ${esc(c.attribute_ru)} ${esc(c.element)}: ${[...new Set(c.values.map((v) => v.value))].map((v) => `<b>${v} мм</b>`).join(" и ")} в разных документах. Нужно уточнение. <button class="btn ghost" data-fl-tab="quotes" style="height:26px">Источники</button></span></div>`);
  }
  if (i.reply) extra.unshift(`<div class="callout info">${ICON.check}<span>Ответ отправлен ${esc(fmtWhen(i.reply.sent_at))} (демо): ${esc(REPLY_RU[i.reply.option] || "")}. Ждём ответа.</span></div>`);
  return `<h2>${esc(msg.subject)}</h2>
    <div class="mail-meta"><span>${esc(msg.from.name)} &lt;${esc(msg.from.email)}&gt;</span><span>${esc(fmtWhen(Date.parse(msg.date) / 1000))}</span></div>
    ${extra.join("")}
    ${facts.length ? `<div class="facts">${facts.map((f) => `<button class="fact" data-fl-fact="${f.n}" title="Показать в тексте">${factLabel(f)}</button>`).join("")}</div>` : ""}
    <div class="mail-body">${html}</div>
    ${msg.attachments.length ? `<div class="attach">${msg.attachments.map((at) => `<button class="btn" data-fl-tab="${at.kind === "drawing" ? "drawing" : "quotes"}">${at.kind === "drawing" ? ICON.image : ICON.file} ${esc(at.name)}</button>`).join("")}</div>` : ""}
    <div>${(i.reasons || []).map((r) => `<div class="reason ${i.category === "SUSPICIOUS" ? "bad" : ""}">• ${esc(r)}</div>`).join("")}</div>
    ${i.subject.includes("W-3") ? `<div><button class="btn primary" data-fl-compose="clarify">${ICON.mail} Подготовить ответ</button></div>` : ""}`;
}

// ------------------------------------------------------------------ drawing
const fileUrl = (name) => `/artifact?path=${encodeURIComponent(st.data.fixtures + "/" + name)}`;

function drawingTab() {
  const [b, c] = st.data.analysis.drawings;
  const active = st.mode === "B" ? b : st.mode === "C" ? c : null;
  const tools = [["hand", "Перемещать", '<svg viewBox="0 0 24 24"><path d="M8 13V5.5a1.5 1.5 0 0 1 3 0V12M11 11V4.5a1.5 1.5 0 0 1 3 0V11M14 11V5.5a1.5 1.5 0 0 1 3 0V13M17 9.5a1.5 1.5 0 0 1 3 0V15a6 6 0 0 1-6 6h-1.5a6 6 0 0 1-4.6-2.2L4.6 14.9a1.5 1.5 0 0 1 2.3-1.9L8 14.5"/></svg>'],
    ["pen", "Карандаш", '<svg viewBox="0 0 24 24"><path d="M4 20l4-1 11-11-3-3L5 16z"/></svg>'],
    ["arrow", "Стрелка", '<svg viewBox="0 0 24 24"><path d="M5 19 19 5M10 5h9v9"/></svg>'],
    ["rect", "Рамка", '<svg viewBox="0 0 24 24"><rect x="4" y="6" width="16" height="12" rx="1.5"/></svg>'],
    ["highlight", "Маркер", '<svg viewBox="0 0 24 24"><path d="M5 19h6M9 15l-2-2 8-8 4 4-8 8z"/></svg>'],
    ["text", "Текст", '<svg viewBox="0 0 24 24"><path d="M5 6V4.5h14V6M12 4.5v15M9 19.5h6"/></svg>']];
  return `<div style="display:flex;flex-direction:column;gap:10px">
    <div class="viewer-bar">
      <div class="seg" role="group" aria-label="Ревизия">
        <button data-fl-mode="B" aria-pressed="${st.mode === "B"}" title="Согласованная ревизия">Рев. ${esc(b.revision)} ✓</button>
        <button data-fl-mode="compare" aria-pressed="${st.mode === "compare"}">Сравнить</button>
        <button data-fl-mode="C" aria-pressed="${st.mode === "C"}" title="Предложение архитектора, не согласовано">Рев. ${esc(c.revision)}</button></div>

      ${tools.map(([id, label, svg]) => `<button class="tool-btn" data-fl-tool="${id}" aria-pressed="${st.tool === id}" title="${label}${id !== "hand" && !active ? " — выберите ревизию" : ""}" ${id !== "hand" && !active ? "disabled" : ""} aria-label="${label}">${svg}</button>`).join("")}
      <button class="tool-btn" data-fl="undo" title="Отменить пометку" aria-label="Отменить" ${active ? "" : "disabled"}><svg viewBox="0 0 24 24"><path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/></svg></button>
      <span class="save-state" id="annState">${active ? (st.shapes.length ? `пометок: ${st.shapes.length}` : "пометок нет") : ""}</span>
      <span style="margin-left:auto"></span>
      <button class="tool-btn" data-fl="zoom-out" aria-label="Уменьшить">−</button>
      <button class="tool-btn" data-fl="fit" aria-label="Вписать" title="Вписать">⤢</button>
      <button class="tool-btn" data-fl="zoom-in" aria-label="Увеличить">+</button></div>
    <div class="viewer" id="viewer">
      <div class="stage" id="stage">
        ${st.mode === "C" ? `<img src="${fileUrl(c.file)}" alt="План, ревизия ${esc(c.revision)}">`
          : `<img src="${fileUrl(b.file)}" alt="План, ревизия ${esc(b.revision)}">`}
        ${st.mode === "compare" ? `<img class="top" src="${fileUrl(c.file)}" alt="План, ревизия ${esc(c.revision)}" style="--split:${st.split}%">` : ""}
        <svg class="ann" id="ann" viewBox="0 0 900 700"><defs><marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0 10 5 0 10z" fill="context-stroke"/></marker></defs><g id="annG"></g></svg>
      </div>
      ${st.mode === "compare" ? `<div class="divider" id="divider" data-l="рев. ${esc(b.revision)}" data-r="рев. ${esc(c.revision)}"></div>` : ""}
    </div>
    ${st.mode === "compare" ? `<input type="range" min="0" max="100" value="${st.split}" id="splitRange" aria-label="Граница сравнения ревизий">` : ""}
    <div class="viewer-legend">
      <span>Рев. ${esc(b.revision)} (${esc(b.date)}, согласовано): ${b.elements.map((e) => `${esc(e.element)} ${e.width}×${e.height}`).join(", ")}</span>
      <span>Рев. ${esc(c.revision)} (${esc(c.date)}, не согласовано): ${c.elements.map((e) => `${esc(e.element)} ${e.width}×${e.height}`).join(", ")}</span>
      <span class="faint">Пометки сохраняются отдельно — исходные файлы чертежей не меняются. Размеры по пикселям без масштаба не вычисляются.</span>
    </div></div>`;
}

function renderShapes() {
  const g = root.querySelector("#annG");
  if (!g) return;
  g.innerHTML = st.shapes.map((s) => {
    const p = s.points;
    if (s.type === "pen" || s.type === "highlight") {
      return `<polyline points="${p.map((q) => q.join(",")).join(" ")}" fill="none" stroke="${s.type === "highlight" ? "#ffd60a" : esc(s.color)}" stroke-width="${s.type === "highlight" ? 18 : 3}" stroke-opacity="${s.type === "highlight" ? .4 : 1}" stroke-linecap="round" stroke-linejoin="round"/>`;
    }
    if (p.length < 2 && s.type !== "text") return "";
    if (s.type === "arrow") return `<line x1="${p[0][0]}" y1="${p[0][1]}" x2="${p[1][0]}" y2="${p[1][1]}" stroke="${esc(s.color)}" stroke-width="3" marker-end="url(#ah)"/>`;
    if (s.type === "rect") {
      const [x1, y1] = p[0], [x2, y2] = p[1];
      return `<rect x="${Math.min(x1, x2)}" y="${Math.min(y1, y2)}" width="${Math.abs(x2 - x1)}" height="${Math.abs(y2 - y1)}" fill="none" stroke="${esc(s.color)}" stroke-width="3" rx="3"/>`;
    }
    if (s.type === "text") return `<text x="${p[0][0]}" y="${p[0][1]}" fill="${esc(s.color)}" font-size="18" font-weight="600" stroke="#fff" stroke-width="4" paint-order="stroke" font-family="Adwaita Sans, sans-serif">${esc(s.text)}</text>`;
    return "";
  }).join("");
  const s = root.querySelector("#annState");
  if (s && !s.dataset.busy) s.textContent = st.shapes.length ? `пометок: ${st.shapes.length}` : "пометок нет";
}

async function loadShapes() {
  const [b, c] = st.data.analysis.drawings;
  const file = st.mode === "B" ? b.file : st.mode === "C" ? c.file : null;
  if (file === st.annFile) { renderShapes(); return; }
  await flushShapes();
  st.annFile = file;
  st.shapes = [];
  if (file) {
    try { st.shapes = (await ctx.link.request("flora.action", { action: "annotations.get", file })).shapes; } catch { }
  }
  renderShapes();
}

let pendingSave = null;
function saveShapesSoon() {
  clearTimeout(st.saveTimer);
  const s = root.querySelector("#annState");
  if (s) { s.textContent = "сохраняю…"; s.dataset.busy = "1"; }
  pendingSave = { file: st.annFile, shapes: st.shapes.slice() };
  st.saveTimer = setTimeout(flushShapes, 500);
}
async function flushShapes() {
  clearTimeout(st.saveTimer);
  const job = pendingSave; pendingSave = null;
  if (!job) return;
  const s = root?.querySelector("#annState");
  try {
    await ctx.link.request("flora.action", { action: "annotations.save", ...job });
    if (s) s.textContent = `сохранено · пометок: ${job.shapes.length}`;
  } catch (e) { if (s) s.textContent = "не сохранено: " + e.message; }
  if (s) delete s.dataset.busy;
}

function applyTransform() {
  const stage = root.querySelector("#stage");
  if (stage) stage.style.transform = `translate(${st.pan.x}px, ${st.pan.y}px) scale(${st.zoom})`;
  const div = root.querySelector("#divider");
  if (div) div.style.left = `${st.pan.x + 900 * st.zoom * st.split / 100}px`;
}

function fit() {
  const v = root.querySelector("#viewer");
  if (!v) return;
  const r = v.getBoundingClientRect();
  st.zoom = Math.min(r.width / 900, r.height / 700) * 0.98;
  st.pan = { x: (r.width - 900 * st.zoom) / 2, y: (r.height - 700 * st.zoom) / 2 };
  applyTransform();
}

function wireViewer() {
  const v = root.querySelector("#viewer");
  if (!v) return;
  if (!st.fitted) { fit(); st.fitted = true; } else applyTransform();
  loadShapes();
  v.classList.toggle("drawing", st.tool !== "hand");
  const toStage = (e) => {
    const r = v.getBoundingClientRect();
    return [+((e.clientX - r.left - st.pan.x) / st.zoom).toFixed(1), +((e.clientY - r.top - st.pan.y) / st.zoom).toFixed(1)];
  };
  let drag = null;
  v.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.target.closest(".text-pop")) return;
    v.setPointerCapture(e.pointerId);
    if (st.tool === "hand" || !st.annFile) { drag = { kind: "pan", x: e.clientX - st.pan.x, y: e.clientY - st.pan.y }; return; }
    const p = toStage(e);
    if (st.tool === "text") { e.preventDefault(); textPopup(v, e, p); return; }
    const shape = { type: st.tool, points: [p], color: "#e5484d" };
    if (st.tool === "arrow" || st.tool === "rect") shape.points.push(p);
    st.shapes.push(shape);
    drag = { kind: "draw", shape };
  });
  v.addEventListener("pointermove", (e) => {
    if (!drag) return;
    if (drag.kind === "pan") { st.pan = { x: e.clientX - drag.x, y: e.clientY - drag.y }; applyTransform(); return; }
    const p = toStage(e);
    if (drag.shape.type === "pen" || drag.shape.type === "highlight") drag.shape.points.push(p);
    else drag.shape.points[1] = p;
    renderShapes();
  });
  const end = () => {
    if (drag?.kind === "draw") {
      const s = drag.shape, p = s.points;
      const tiny = p.length < 2 || Math.hypot(p[p.length - 1][0] - p[0][0], p[p.length - 1][1] - p[0][1]) < 4;
      if (tiny) st.shapes.pop(); else saveShapesSoon();
      renderShapes();
    }
    drag = null;
  };
  v.addEventListener("pointerup", end);
  v.addEventListener("pointercancel", end);
  v.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = v.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    const k = Math.exp(-e.deltaY * 0.0015);
    const z = Math.min(6, Math.max(0.3, st.zoom * k));
    st.pan = { x: mx - (mx - st.pan.x) * z / st.zoom, y: my - (my - st.pan.y) * z / st.zoom };
    st.zoom = z;
    applyTransform();
  }, { passive: false });
  root.querySelector("#splitRange")?.addEventListener("input", (e) => {
    st.split = +e.target.value;
    root.querySelector(".stage img.top")?.style.setProperty("--split", st.split + "%");
    applyTransform();
  });
}

function textPopup(v, e, p) {
  v.querySelector(".text-pop")?.remove();
  const r = v.getBoundingClientRect();
  const pop = document.createElement("div");
  pop.className = "text-pop";
  pop.style.left = `${e.clientX - r.left}px`;
  pop.style.top = `${e.clientY - r.top - 15}px`;
  pop.innerHTML = `<input type="text" maxlength="120" placeholder="Текст и Enter" aria-label="Текст пометки">`;
  v.appendChild(pop);
  const inp = pop.querySelector("input");
  inp.addEventListener("pointerdown", (x) => x.stopPropagation());
  inp.focus();
  inp.addEventListener("keydown", (k) => {
    if (k.key === "Enter" && inp.value.trim()) {
      st.shapes.push({ type: "text", points: [p], color: "#e5484d", text: inp.value.trim() });
      inp.value = ""; pop.remove(); renderShapes(); saveShapesSoon();
    }
    if (k.key === "Escape") pop.remove();
    k.stopPropagation();
  });
  inp.addEventListener("blur", () => setTimeout(() => {
    if (inp.value.trim() && pop.isConnected) {
      st.shapes.push({ type: "text", points: [p], color: "#e5484d", text: inp.value.trim() });
      renderShapes(); saveShapesSoon();
    }
    pop.remove();
  }, 120));
}

// ------------------------------------------------------------------ quotes
function quotesTab() {
  const a = st.data.analysis, c = a.comparison, q = a.quotes;
  const label = { same: "без изменений", changed: "изменено", replaced: "заменено", added: "добавлено", removed: "удалено" };
  const rows = c.rows.map((r) => `<tr class="${esc(r.status)}">
      <td><b>${esc(r.code)}</b><div class="faint" style="font-size:11.5px">${label[r.status]}</div></td>
      <td>${r.base ? esc(r.base.name) + `<div class="cell">${esc(c.base_file)} · ${esc(r.base.cells.sum)}</div>` : "—"}</td>
      <td>${r.new ? esc(r.new.name) + `<div class="cell">${esc(c.new_file)} · ${esc(r.new.cells.sum)}</div>` : "—"}</td>
      <td class="num">${r.base ? rub(r.base.sum) : "—"}</td><td class="num">${r.new ? rub(r.new.sum) : "—"}</td>
      <td class="num ${r.delta > 0 ? "pos" : ""}">${r.delta ? (r.delta > 0 ? "+" : "") + rub(r.delta) : "0"}</td></tr>`).join("");
  const ft = q.new.total;
  const anomaly = c.anomalies.find((x) => x.kind === "total_formula_range");
  const contr = a.contradictions.map((ct) => {
    const groups = {};
    for (const v of ct.values) (groups[v.value] ||= []).push(v.source);
    return `<div class="panel"><h2>${ICON.alert.replace("<svg", '<svg style="width:16px;height:16px;vertical-align:-3px;color:var(--warn)"')} ${esc(ct.element)}: не совпадает ${esc(ct.attribute_ru)}</h2>
      ${Object.entries(groups).map(([val, srcs]) => `<div style="margin-top:8px"><b>${val} мм</b><ul class="src-list">${srcs.map((s) => `<li>${esc(s.label || s.file)}${s.quote ? ` — «${esc(s.quote.length > 90 ? s.quote.slice(0, 90) + "…" : s.quote)}»` : ""}</li>`).join("")}</ul></div>`).join("")}
      <div class="callout warn" style="margin-top:10px">${ICON.alert}<span>Статус: <b>не решено</b>. JARVIS не выбирает размер за вас — нужно подтверждение архитектора или фабрики.</span></div></div>`;
  }).join("");
  const alt = q.alt;
  return `<div style="display:flex;flex-direction:column;gap:var(--s4)">
    <div class="panel" style="padding:0;overflow-x:auto"><table class="qtable"><thead><tr><th>Код</th><th>Согласовано: ${esc(c.base_file)}</th><th>Предложено: ${esc(c.new_file)}</th><th class="num">Было</th><th class="num">Стало</th><th class="num">Разница</th></tr></thead>
      <tbody>${rows}
      <tr class="total"><td colspan="3">Сумма по строкам (пересчитано JARVIS)</td><td class="num">${rub(c.base_total)}</td><td class="num">${rub(c.new_total)}</td><td class="num pos">+${rub(c.delta)}</td></tr>
      ${ft ? `<tr class="${anomaly ? "filetotal" : ""}"><td colspan="3">Итог, записанный в файле ${esc(c.new_file)} <span class="cell">${esc(ft.cell)}: ${esc(ft.formula)}</span></td><td class="num">${rub(q.base.total?.value)}</td><td class="num">${rub(ft.value)}</td><td class="num">${c.file_delta != null ? "+" + rub(c.file_delta) : "—"}</td></tr>` : ""}
      </tbody></table></div>
    ${c.anomalies.map((x) => `<div class="callout err">${ICON.alert}<span>${esc(x.text)}. Ячейка <code>${esc(x.cell)}</code> содержит <code>${esc(x.formula || "")}</code>. Исходный файл не изменялся.</span></div>`).join("")}
    ${a.cost_claims.map((cl) => `<div class="callout warn">${ICON.mail}<span>${esc(cl.source.label)}: «${esc(cl.stated_text)}». По строкам КП: <b>+${rub(cl.lines_delta)}</b>${cl.matches_file_total ? `; цифра из письма совпадает с ошибочным итогом файла (+${rub(cl.file_delta)})` : ""}.</span></div>`).join("")}
    <div class="grid2">${contr}
      <div class="panel"><h2>Альтернатива: ${esc(alt.file)}</h2>
        <table class="qtable">${alt.lines.map((l) => `<tr><td>${esc(l.code)}</td><td>${esc(l.name)}</td><td class="num">${rub(l.sum, alt.currency)}</td></tr>`).join("")}</table>
        <div class="callout ${a.alternative.comparable ? "info" : "warn"}" style="margin-top:10px">${ICON.alert}<span><b>${a.alternative.comparable ? "Сравнимо" : "Не сравнимо без уточнения"}</b>${a.alternative.reasons.map((r) => `<br>• ${esc(r)}`).join("")}</span></div></div>
    </div></div>`;
}

// ------------------------------------------------------------------ reply
function replyTab() {
  const d = st.data.draft;
  const pend = Object.values(ctx.S.approvals).filter((x) => x.status === "pending" && x.tool_id === "flora_send");
  const opts = [["clarify", "Запросить уточнения"], ["approve", "Согласовать с условиями"], ["decline", "Оставить W-3"]];
  if (!d) {
    return `<div class="panel"><h2>Ответ архитектору</h2><p class="muted" style="margin:6px 0 12px">Выберите, какой ответ подготовить. Текст строится только из фактов писем и КП; ничего не отправится без вашего подтверждения.</p>
      <div class="field">${opts.map(([id, l]) => `<button class="btn ${id === "clarify" ? "primary" : ""}" data-fl-compose="${id}">${l}</button>`).join("")}</div></div>`;
  }
  const sent = d.status === "sent";
  return `<div style="display:flex;flex-direction:column;gap:var(--s4)">
    ${sent ? "" : `<div class="viewer-bar"><span class="faint" style="font-size:13px">Вариант:</span><div class="seg">${opts.map(([id, l]) => `<button data-fl-compose="${id}" aria-pressed="${d.option === id}">${l}</button>`).join("")}</div></div>`}
    <div class="panel editor">
      <div class="line"><span>Кому</span><div>${esc(d.to_name)} &lt;${esc(d.to_addr)}&gt;</div></div>
      <div class="line"><span>Тема</span><input class="input" id="drSubject" value="${esc(d.subject)}" ${sent ? "disabled" : ""} aria-label="Тема"></div>
      <textarea id="drBody" aria-label="Текст письма" ${sent ? "disabled" : ""}>${esc(d.body)}</textarea>
      <div class="viewer-bar"><span class="save-state" id="drState">${sent ? `отправлено (демо) ${fmtWhen(d.sent_at)} · ${esc(d.provider_message_id)}` : `черновик · версия ${d.version}`}</span>
        <span style="margin-left:auto"></span>
        ${sent ? `<span class="vbadge verified">проверено в «Отправленных»</span>` : `<button class="btn warn" data-fl="send" ${pend.length ? "disabled" : ""}>${ICON.mail} Отправить…</button>`}</div>
    </div>
    ${pend.map((a) => approvalCard(a)).join("")}
    ${sent ? "" : `<div class="faint" style="font-size:12.5px">Отправка — действие высокого риска: JARVIS покажет точный текст и получателя и попросит подтверждение. Если вы измените текст после запроса, запрос аннулируется. Это демо-почта: письмо не покидает компьютер.</div>`}
  </div>`;
}

// ------------------------------------------------------------------ journal
function journalTab() {
  const dec = st.data.decisions;
  const status = { approved: "согласовано", clarification_requested: "запрошено уточнение", approved_with_conditions: "согласовано с условиями",
    declined: "отклонено", replied: "отвечено" };
  return `<div class="grid2">
    <div class="panel"><h2>Решения по проекту</h2>
      <table class="tbl"><tbody>${dec.map((x) => `<tr><td>${esc(x.subject)}<div class="faint" style="font-size:12px">${esc(x.note || "")}</div></td>
        <td><span class="pill ${x.status === "approved" ? "completed" : "waiting_for_approval"}">${esc(status[x.status] || x.status)}</span></td>
        <td class="faint" style="white-space:nowrap">${x.date ? new Date(x.date + "T12:00").toLocaleDateString("ru-RU", { day: "numeric", month: "short" }) : fmtWhen(x.created_at)}</td></tr>`).join("")}</tbody></table></div>
    <div class="panel"><h2>Отправлено (демо)</h2>
      ${st.data.sent.length ? `<table class="tbl"><tbody>${st.data.sent.map((s) => `<tr><td>${esc(s.subject)}<div class="faint" style="font-size:12px">${esc(s.to)} · ${esc(s.provider_message_id)}</div></td><td class="faint">${fmtWhen(s.sent_at)}</td></tr>`).join("")}</tbody></table>`
        : `<div class="empty-note">Пока ничего не отправлялось</div>`}</div></div>`;
}

// ------------------------------------------------------------------ events
function wire() {
  root.querySelectorAll("[data-fl-tab]").forEach((b) => b.addEventListener("click", () => { st.tab = b.dataset.flTab; draw(); }));
  root.querySelectorAll("[data-fl-item]").forEach((b) => b.addEventListener("click", () => { st.item = b.dataset.flItem; draw(); }));
  root.querySelectorAll("[data-fl-fact]").forEach((b) => b.addEventListener("click", () => {
    root.querySelectorAll("mark.hot").forEach((m) => m.classList.remove("hot"));
    const m = root.querySelector(`mark[data-fact="${b.dataset.flFact}"]`);
    if (m) { m.classList.add("hot"); m.scrollIntoView({ block: "center", behavior: "smooth" }); }
  }));
  root.querySelectorAll("[data-fl-mode]").forEach((b) => b.addEventListener("click", () => {
    st.mode = b.dataset.flMode;
    if (st.mode === "compare") st.tool = "hand";
    draw();
  }));
  root.querySelectorAll("[data-fl-tool]").forEach((b) => b.addEventListener("click", () => { st.tool = b.dataset.flTool; draw(); }));
  root.querySelectorAll("[data-fl-compose]").forEach((b) => b.addEventListener("click", () => compose(b.dataset.flCompose)));
  root.querySelectorAll("[data-fl]").forEach((b) => b.addEventListener("click", () => action(b.dataset.fl)));
  if (st.tab === "drawing") wireViewer();
  if (st.tab === "reply") wireEditor();
}

async function action(a) {
  if (a === "reload") { await load(); draw(); ctx.toast("Почта и документы перечитаны"); }
  if (a === "undo") { st.shapes.pop(); renderShapes(); saveShapesSoon(); }
  if (a === "fit") fit();
  if (a === "zoom-in" || a === "zoom-out") {
    const v = root.querySelector("#viewer").getBoundingClientRect();
    const z = Math.min(6, Math.max(0.3, st.zoom * (a === "zoom-in" ? 1.25 : 0.8)));
    st.pan = { x: v.width / 2 - (v.width / 2 - st.pan.x) * z / st.zoom, y: v.height / 2 - (v.height / 2 - st.pan.y) * z / st.zoom };
    st.zoom = z; applyTransform();
  }
  if (a === "send") {
    await flushDraft();
    try { await ctx.link.request("flora.action", { action: "send" }); }
    catch (e) { ctx.toast(e.message, "err"); }
  }
}

async function compose(option) {
  const d = st.data.draft;
  if (d && d.status !== "sent" && d.version > 1 && !confirm("Заменить текущий текст черновика новым вариантом?")) return;
  try {
    const r = await ctx.link.request("flora.action", { action: "draft.compose", option });
    st.data.draft = r.draft; st.tab = "reply"; draw();
  } catch (e) { ctx.toast(e.message, "err"); }
}

function wireEditor() {
  const body = root.querySelector("#drBody"), subj = root.querySelector("#drSubject");
  if (!body) return;
  const changed = () => {
    const s = root.querySelector("#drState");
    if (s) s.textContent = "изменено…";
    clearTimeout(st.draftTimer);
    st.draftTimer = setTimeout(flushDraft, 700);
  };
  body.addEventListener("input", changed);
  subj.addEventListener("input", changed);
}

async function flushDraft() {
  clearTimeout(st.draftTimer);
  const body = root.querySelector("#drBody"), subj = root.querySelector("#drSubject"), d = st.data.draft;
  if (!body || !d || d.status === "sent") return;
  if (body.value === d.body && subj.value === d.subject) return;
  try {
    const r = await ctx.link.request("flora.action", { action: "draft.save", draft_id: d.id, body: body.value, subject: subj.value });
    st.data.draft = r.draft;
    const s = root.querySelector("#drState");
    if (s) s.textContent = `сохранено · версия ${r.draft.version}`;
  } catch (e) { ctx.toast(e.message, "err"); }
}
