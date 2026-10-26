// «Архитектура» — JARVIS Architect in the window: a plan (PNG/JPG/WEBP/PDF/DXF) → the master model → FreeCAD (BIM,
// OCCT checks) → Blender → this 3D preview. The window never edits geometry itself: every change is a JARVIS task
// (the same tools and gateway as a spoken command), the core rebuilds only what changed, and the window reloads.
// 2D and 3D share one selection: a wall picked in the plan is the wall lit in 3D, and «эту стену» in a spoken
// command means it.
import { ICON, esc } from "../render.js";
import { byId } from "./registry.js";
import { header, emptyState } from "./common.js";
import { planSvg, describe, findEl } from "../architect/plan2d.js";

const STAGES = [["source", "Источник"], ["analysis", "Анализ 2D"], ["plan", "Структурный план"], ["model", "Мастер-модель"],
  ["cad", "CAD (FreeCAD/OCCT)"], ["preview", "3D (Blender)"], ["edit", "Правка"], ["export", "Экспорт"]];
const STATE_RU = { idle: "ещё нет", running: "идёт", done: "готово", error: "ошибка", waiting: "ждёт ответа" };
const EXPORTS = [["glb", "GLB"], ["obj", "OBJ"], ["blend", "Blender"], ["fcstd", "FreeCAD"], ["step", "STEP"], ["stl", "STL"],
  ["dxf", "DXF"], ["json", "JSON"]];
const MATS = [["plaster_white", "Белая штукатурка"], ["plaster_gray", "Серая"], ["plaster_beige", "Бежевая"], ["paint_blue", "Голубая"],
  ["paint_green", "Зелёная"], ["brick_red", "Кирпич"], ["concrete", "Бетон"], ["oak_light", "Светлый дуб"], ["oak_dark", "Тёмный дуб"],
  ["walnut", "Орех"], ["tile_white", "Белая плитка"], ["tile_gray", "Серая плитка"], ["marble", "Мрамор"], ["carpet_gray", "Ковролин"]];
const TYPE_RU = { wall: "Стена", door: "Дверь", window: "Окно", room: "Помещение", furniture: "Мебель", column: "Колонна",
  stair: "Лестница", slab: "Перекрытие", roof: "Крыша", floor: "Пол", ceiling: "Потолок" };

export function create(ctx) {
  const ws = byId("architect");
  const el = document.createElement("section");
  el.className = "view view-ws view-arch";
  el.dataset.ws = "architect";
  el.setAttribute("aria-label", "Архитектура");
  el.hidden = true;
  const st = { s: null, sel: null, tab: "plan", under: true, viewer: null, viewerErr: "", glb: "", mode: "persp", level: null,
    roof: false, busy: "", loading: false, timer: 0, ops: [] };

  async function load(project) {
    st.loading = true;
    try {
      st.s = await ctx.link.request("architect.state", project ? { project } : {});
    } catch (e) { st.s = { error: e.message }; }
    st.loading = false;
    draw();
  }
  const reload = () => { clearTimeout(st.timer); st.timer = setTimeout(() => load(st.s?.active), 250); };

  const model = () => st.s?.model || null;

  // ------------------------------------------------------------------ actions (all through JARVIS)
  async function run(tool, args, label) {
    st.busy = label || "Работаю…";
    drawStatus();
    try { await ctx.link.request("tool.run", { tool, args }); }
    catch (e) { ctx.toast(e.message, "err"); st.busy = ""; drawStatus(); }
  }
  function say(text) {
    if (!text.trim()) return;
    ctx.submit(text.trim(), { source: "text", stay: true });
    st.busy = "JARVIS выполняет: «" + text.trim().slice(0, 60) + "»";
    drawStatus();
  }
  async function upload(file) {
    if (!file) return;
    if (file.size > 9 * 1024 * 1024) return ctx.toast("Файл больше 9 МБ", "err");
    st.busy = "Загружаю " + file.name;
    drawStatus();
    const b64 = await new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = () => res(String(r.result).split(",")[1] || "");
      r.onerror = () => rej(r.error);
      r.readAsDataURL(file);
    });
    try {
      st.s = await ctx.link.request("architect.upload", { name: file.name, data_b64: b64 }, 120000);
      st.sel = null; st.glb = "";
      st.busy = "";
      draw();
      await run("architect_build", { project: st.s.active }, "Строю 3D по плану…");
    } catch (e) { st.busy = ""; drawStatus(); ctx.toast(e.message, "err"); }
  }
  function selectId(id) {
    st.sel = id || null;
    ctx.link.request("architect.select", { id: st.sel || "" }).catch(() => {});
    st.viewer?.select(st.sel);
    drawPlan();
    drawInspector();
  }

  // ------------------------------------------------------------------ drawing
  function draw() {
    const s = st.s || {};
    const p = s.project;
    const projects = s.projects || [];
    const sub = p ? esc(p.source_name || "") : "план → 3D: FreeCAD, Open CASCADE, Blender";
    const pick = projects.length ? `<select class="input ap-proj" id="apProj" aria-label="Проект">${projects.map((x) =>
      `<option value="${esc(x.id)}"${x.id === s.active ? " selected" : ""}>${esc(x.name)}</option>`).join("")}</select>` : "";
    el.innerHTML = `<div class="ws ws-arch">${header(ws, ctx.env(), { title: "Архитектура", sub,
      right: `${pick}<label class="btn" title="PNG, JPG, WEBP, PDF или DXF">${ICON.plus || "+"} План<input type="file" id="apFile" accept=".png,.jpg,.jpeg,.webp,.pdf,.dxf,.bmp,.tif,.tiff" hidden></label>` })}
      <div class="ap-pipe" id="apPipe" role="list" aria-label="Этапы"></div>
      <div class="ap-status" id="apStatus" aria-live="polite"></div>
      <div class="ap-q" id="apQ" hidden></div>
      ${p ? `<div class="ap-grid">
        <div class="panel ap-2d"><div class="ap-bar"><div class="seg-tabs" role="tablist">
          <button role="tab" data-tab="plan" aria-selected="${st.tab === "plan"}">План</button>
          <button role="tab" data-tab="source" aria-selected="${st.tab === "source"}">Чертёж</button>
          <button role="tab" data-tab="marks" aria-selected="${st.tab === "marks"}">Распознано</button></div>
          <span class="grow"></span><label class="ap-chk"><input type="checkbox" id="apUnder"${st.under ? " checked" : ""}> подложка</label></div>
          <div class="ap-plan" id="apPlan"></div></div>
        <div class="panel ap-3d"><div class="ap-bar">
          <div class="seg-tabs" role="tablist" aria-label="Вид">${[["persp", "3D"], ["top", "Сверху"], ["walk", "Изнутри"]].map(([m, l]) =>
            `<button role="tab" data-mode="${m}" aria-selected="${st.mode === m}">${l}</button>`).join("")}</div>
          <select class="input ap-lv" id="apLevel" aria-label="Этаж"><option value="">Все этажи</option>${(model()?.levels || []).map((l) =>
            `<option value="${esc(l.id)}"${st.level === l.id ? " selected" : ""}>${esc(l.name)}</option>`).join("")}</select>
          <label class="ap-chk"><input type="checkbox" id="apRoof"${st.roof ? " checked" : ""}> крыша</label>
          <span class="grow"></span><button class="btn sm" id="apRender" title="Визуализация в Blender">Рендер</button></div>
          <div class="ap-view" id="apView"><div class="ap-hint" id="apHint"></div></div></div>
        <aside class="panel ap-side" id="apSide"></aside>
      </div>
      <form class="ap-cmd" id="apCmd" autocomplete="off"><input class="input" id="apCmdIn" placeholder="Команда для модели: «убери стену между кухней и гостиной», «добавь диван в гостиную»…" aria-label="Команда для модели"><button class="btn primary" type="submit">Выполнить</button></form>
      <div class="ap-bottom"><div class="panel ap-exp" id="apExp"></div><div class="panel ap-ops" id="apOps"></div></div>`
      : `<div class="panel ap-drop" id="apDrop">${emptyState({ icon: "plan", title: "Перетащите сюда план", text:
        "PNG, JPG, WEBP, PDF или DXF: чертёж, скан, фото или рисунок от руки. JARVIS распознает стены, комнаты, двери, окна и мебель, построит параметрическую модель в FreeCAD и 3D в Blender.",
        tries: ["Сделай 3D по этому плану"] })}</div>`}
      <details class="ap-eng"><summary>Инструменты на компьютере</summary><div id="apEng"></div></details></div>`;
    drawPipe(); drawStatus(); drawQuestion(); drawPlan(); drawInspector(); drawExports(); drawOps(); drawEngines();
    if (p) mountViewer();
  }

  function drawPipe() {
    const box = el.querySelector("#apPipe");
    if (!box) return;
    const pl = st.s?.project?.pipeline || {};
    box.innerHTML = STAGES.map(([k, label], i) => {
      const s = pl[k]?.state || "idle";
      return `<div class="ap-stage" role="listitem" data-state="${s}" title="${esc(pl[k]?.detail || STATE_RU[s])}"><span class="n">${i + 1}</span><span class="l">${label}</span><span class="s">${STATE_RU[s] || s}</span></div>`;
    }).join('<span class="ap-arrow" aria-hidden="true">→</span>');
  }

  function drawStatus() {
    const box = el.querySelector("#apStatus");
    if (!box) return;
    const last = st.ops.length ? st.ops[st.ops.length - 1] : null;
    const running = last && last.status === "running" ? `${opName(last.operation)}…` : "";
    const text = st.busy || running || st.viewerErr;
    box.innerHTML = text ? `<span class="spinner"${st.viewerErr && !st.busy ? " hidden" : ""}></span> ${esc(text)}` : "";
    box.hidden = !text;
  }

  function drawQuestion() {
    const box = el.querySelector("#apQ");
    if (!box) return;
    const q = st.s?.project?.pending;
    box.hidden = !q;
    if (!q) return;
    const opts = (q.options || []).map((o, i) => `<button class="btn" data-answer="${i + 1}">${esc(o.label)}</button>`).join("");
    const yes = q.kind === "scale" || q.kind === "confirm" ? `<button class="btn primary" data-say="да">Да</button>` : "";
    box.innerHTML = `<div class="ap-q-text">${ICON.alert} ${esc(q.text)}</div><div class="btn-row">${opts}${yes}
      <form class="ap-q-form" id="apQForm"><input class="input" id="apQIn" placeholder="${q.kind === "scale" ? "например: ширина дома 10 метров" : "ответ"}"><button class="btn" type="submit">Ответить</button></form></div>`;
  }

  function drawPlan() {
    const box = el.querySelector("#apPlan");
    if (!box) return;
    const m = model();
    const f = st.s?.files || {};
    if (st.tab === "source" || st.tab === "marks") {
      const url = st.tab === "source" ? f["analysis/source.png"] : f["analysis/annotated.png"];
      box.innerHTML = url ? `<img class="ap-img" src="${esc(url)}" alt="${st.tab === "source" ? "Исходный чертёж" : "Найденные стены и проёмы"}">`
        : `<p class="muted">Нет изображения${st.s?.project?.source_name?.toLowerCase().endsWith(".dxf") ? " (DXF читается как линии)" : ""}.</p>`;
      return;
    }
    if (!m) { box.innerHTML = `<p class="muted">Модель ещё не построена.</p>`; return; }
    const src = st.s?.plan_source;
    const under = st.under && src && src.mm_per_px && f["analysis/source.png"] ? { url: f["analysis/source.png"], w: src.px[0], h: src.px[1],
      mm_per_px: src.mm_per_px, origin_px: src.origin_px } : null;
    box.innerHTML = planSvg(m, { selected: st.sel, level: st.level, underlay: under });
  }

  function field(label, name, value, attrs = "") {
    return `<label class="ap-f"><span>${label}</span><input class="input" name="${name}" value="${esc(value)}" ${attrs}></label>`;
  }
  function matSelect(name, cur) {
    return `<label class="ap-f"><span>Материал</span><select class="input" name="${name}"><option value="">—</option>${MATS.map(([k, l]) =>
      `<option value="${k}"${k === cur ? " selected" : ""}>${l}</option>`).join("")}</select></label>`;
  }

  function drawInspector() {
    const box = el.querySelector("#apSide");
    if (!box) return;
    const m = model();
    if (!m) { box.innerHTML = `<p class="muted">Здесь появятся помещения, стены, двери и окна.</p>`; return; }
    const e = findEl(m, st.sel);
    let insp = "";
    if (e) {
      const conf = e.confidence != null ? `<span class="ap-conf" style="--c:${Math.round((e.confidence || 0) * 100)}%" title="уверенность распознавания">${(e.confidence || 0).toFixed(2)}</span>` : "";
      let form = "";
      if (e.type === "wall") form = field("Толщина, мм", "thickness", Math.round(e.thickness), 'type="number" min="50" max="1500"') +
        field("Высота, мм", "height", Math.round(e.height), 'type="number" min="500" max="20000"') + matSelect("material", e.material);
      else if (e.type === "door" || e.type === "window") form = field("Ширина, мм", "width", Math.round(e.width), 'type="number" min="300" max="6000"') +
        field("Высота, мм", "height", Math.round(e.height), 'type="number" min="300" max="6000"') +
        (e.type === "window" ? field("Подоконник, мм", "sill", Math.round(e.sill), 'type="number" min="0" max="3000"') : "") +
        field("От начала стены, мм", "offset", Math.round(e.offset), 'type="number"');
      else if (e.type === "room") form = field("Название", "name", e.name, 'maxlength="60"') + matSelect("floor", e.floor_material || "");
      else if (e.type === "furniture") form = field("Поворот, °", "rotation", Math.round(e.rotation), 'type="number" step="15"') + matSelect("material", e.material);
      insp = `<div class="ap-insp"><div class="ap-insp-h"><b>${esc(TYPE_RU[e.type] || e.type)} ${esc(e.id)}</b>${conf}</div>
        <div class="muted">${esc(e.name && e.type !== "room" ? e.name + " · " : "")}${esc(describe(m, e))}</div>
        ${form ? `<form class="ap-form" id="apForm">${form}<div class="btn-row"><button class="btn primary sm" type="submit">Применить</button>
        ${["wall", "door", "window", "furniture", "room"].includes(e.type) ? `<button class="btn sm danger" type="button" id="apDel">Удалить</button>` : ""}</div></form>` : ""}</div>`;
    }
    const sum = st.s?.summary || {};
    const rooms = (m.rooms || []).map((r) => row(r, r.name, `${String(r.net_area_m2 ?? r.area_m2).replace(".", ",")} м²`)).join("");
    const walls = (m.walls || []).map((w) => row(w, w.id, describe(m, w))).join("");
    const ops = [...(m.doors || []), ...(m.windows || [])].map((o) => row(o, `${o.type === "door" ? "Дверь" : "Окно"} ${o.id.split("_")[1]}`, describe(m, o))).join("");
    const furn = (m.furniture || []).map((f) => row(f, f.name, describe(m, f))).join("");
    const warn = (m.warnings || []).slice(0, 8).map((w) => `<li>${esc(w)}</li>`).join("");
    const sc = sum.scale || {};
    box.innerHTML = `${insp}
      <div class="ap-sum">${(m.rooms || []).length} помещ. · ${(m.walls || []).length} стен · ${(m.doors || []).length} дв. · ${(m.windows || []).length} окон · ${(m.furniture || []).length} мебели
        <div class="muted">Масштаб: ${esc((m.source || {}).scale_note || (sc.mm_per_px ? sc.mm_per_px + " мм/пикс." : "—"))}${(m.source || {}).scale_confidence != null ? ` (уверенность ${(+(m.source || {}).scale_confidence).toFixed(2)})` : ""} · ревизия ${m.revision ?? 0}</div></div>
      ${group("Помещения", rooms)}${group("Двери и окна", ops)}${group("Мебель", furn)}${group("Стены", walls, true)}
      ${warn ? `<details class="ap-group" open><summary>Предупреждения</summary><ul class="ap-warn">${warn}</ul></details>` : ""}`;
  }
  function row(e, name, text) {
    const c = e.confidence ?? 1;
    return `<button class="ap-row${e.id === st.sel ? " sel" : ""}${c < 0.5 ? " low" : ""}" data-sel="${esc(e.id)}"><span class="nm">${esc(name)}</span><span class="tx">${esc(text)}</span>${c < 1 ? `<span class="ap-conf" style="--c:${Math.round(c * 100)}%">${c.toFixed(2)}</span>` : ""}</button>`;
  }
  function group(title, rows, closed) {
    return rows ? `<details class="ap-group"${closed ? "" : " open"}><summary>${title}</summary><div class="ap-rows">${rows}</div></details>` : "";
  }

  function drawExports() {
    const box = el.querySelector("#apExp");
    if (!box) return;
    const files = st.s?.exports || [];
    box.innerHTML = `<div class="ap-h">Экспорт</div><div class="btn-row">${EXPORTS.map(([k, l]) => `<button class="btn sm" data-exp="${k}">${l}</button>`).join("")}
      <button class="btn sm" data-exp="all">Все</button></div>
      ${files.length ? `<div class="ap-files">${files.map((f) => `<button class="ap-file" data-open="${esc(f.path)}" title="${esc(f.path)}">${ICON.file}<span>${esc(f.name)}</span><span class="muted">${Math.max(1, Math.round(f.size / 1024))} КБ</span></button>`).join("")}</div>` : ""}
      ${st.s?.project ? `<button class="btn sm ghost" data-open="${esc(st.s.project.dir)}">Открыть папку проекта</button>` : ""}`;
  }

  function opName(o) {
    return { analyze_plan: "Анализ плана", create_cad: "FreeCAD: модель", update_cad: "FreeCAD: обновление", sync_blender: "Blender",
      edit_model: "Правка", export_model: "Экспорт", render: "Рендер", undo: "Отмена" }[o] || o;
  }
  function drawOps() {
    const box = el.querySelector("#apOps");
    if (!box) return;
    const ops = [...(st.s?.ops || []), ...st.ops.filter((o) => !(st.s?.ops || []).some((x) => x.op_id === o.op_id))].slice(-12).reverse();
    box.innerHTML = `<div class="ap-h">Операции</div>${ops.length ? `<div class="ap-oplist">${ops.map((o) =>
      `<div class="ap-op" data-state="${esc(o.status)}" title="${esc(o.op_id)}${o.task_id ? " · задача " + esc(o.task_id) : ""}${o.affected?.length ? " · " + esc(o.affected.slice(0, 20).join(", ")) : ""}">
        <span class="d"></span><span class="nm">${esc(opName(o.operation))}</span><span class="tx">${esc(o.error || o.detail || "")}</span>
        <span class="muted">${o.ms != null ? (o.ms / 1000).toFixed(1) + " с" : "…"}</span></div>`).join("")}</div>` : `<p class="muted">Пока ничего.</p>`}`;
  }

  function drawEngines() {
    const box = el.querySelector("#apEng");
    if (!box) return;
    box.innerHTML = `<div class="ap-eng-list">${(st.s?.engines || []).map((e) => `<div class="tool-row" data-state="${e.available ? "ready" : "missing"}"><span class="d"></span>
      <span>${esc(e.title)} ${e.version ? `<code>${esc(e.version)}</code>` : ""}<span class="w">${esc(e.available ? e.path : e.detail)}</span></span></div>`).join("")}</div>`;
  }

  async function mountViewer() {
    const host = el.querySelector("#apView");
    if (!host) return;
    const url = st.s?.files?.["previews/building.glb"];
    if (st.viewer && st.viewerHost === host) {
      if (url && url !== st.glb) { st.glb = url; st.viewer.load(url).catch((e) => { st.viewerErr = "3D не загрузилось: " + e.message; drawStatus(); }); }
      return;
    }
    if (st.viewer) { st.viewer.dispose(); st.viewer = null; }
    const hint = el.querySelector("#apHint");
    if (!url) { if (hint) hint.textContent = "3D появится после построения модели"; return; }
    try {
      const { createViewer } = await import("../architect/viewer.js");
      st.viewer = await createViewer(host, { onSelect: (id) => selectId(id), onStatus: (t) => { if (hint) hint.textContent = t; } });
      st.viewerHost = host;
      st.glb = url;
      await st.viewer.load(url);
      st.viewer.setRoof(st.roof);
      st.viewer.setLevel(st.level);
      st.viewer.setMode(st.mode);
      if (st.sel) st.viewer.select(st.sel);
      if (hint) hint.textContent = st.mode === "walk" ? "Мышь — осмотреться, W A S D — идти" : "";
    } catch (e) {
      st.viewerErr = "3D-просмотр недоступен: " + e.message;
      drawStatus();
    }
  }

  // ------------------------------------------------------------------ events from the page
  el.addEventListener("click", (e) => {
    const t = e.target.closest("button, [data-id]");
    if (!t) return;
    if (t.dataset.tab) { st.tab = t.dataset.tab; el.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b === t))); drawPlan(); return; }
    if (t.dataset.mode) {
      st.mode = t.dataset.mode;
      el.querySelectorAll("[data-mode]").forEach((b) => b.setAttribute("aria-selected", String(b === t)));
      const room = st.sel && findEl(model(), st.sel)?.type === "room" ? st.sel : null;
      st.viewer?.setMode(st.mode, st.mode === "walk" ? room || (model()?.rooms?.[0]?.id) : null);
      const hint = el.querySelector("#apHint");
      if (hint) hint.textContent = st.mode === "walk" ? "Мышь — осмотреться, W A S D — идти" : "";
      return;
    }
    if (t.dataset.sel) { selectId(t.dataset.sel); return; }
    if (t.closest(".ap-svg") && t.dataset.id) { selectId(t.dataset.id); return; }
    if (t.dataset.exp) {
      const formats = t.dataset.exp === "all" ? EXPORTS.map(([k]) => k) : [t.dataset.exp];
      run("architect_export", { formats }, "Экспорт: " + formats.join(", ").toUpperCase());
      return;
    }
    if (t.dataset.open) { ctx.link.request("tool.run", { tool: "open_path", args: { path: t.dataset.open } }).catch((er) => ctx.toast(er.message, "err")); return; }
    if (t.dataset.answer) { run("architect_answer", { choice: +t.dataset.answer }, "Выполняю выбранный вариант…"); return; }
    if (t.dataset.say) { run("architect_answer", { text: t.dataset.say }, "Продолжаю…"); return; }
    if (t.id === "apRender") { run("architect_render", { view: st.mode === "top" ? "top" : st.sel && findEl(model(), st.sel)?.type === "room" ? "room" : "persp", room: st.sel && findEl(model(), st.sel)?.type === "room" ? st.sel : undefined }, "Blender рендерит…"); return; }
    if (t.id === "apDel" && st.sel) {
      const e2 = findEl(model(), st.sel);
      const op = { wall: "delete_wall", door: "delete_opening", window: "delete_opening", furniture: "delete_furniture", room: "delete_room" }[e2?.type];
      if (op) { run("architect_edit", { ops: [{ op, id: st.sel }] }, "Удаляю " + st.sel); st.sel = null; }
    }
  });
  el.addEventListener("change", (e) => {
    const t = e.target;
    if (t.id === "apProj") { ctx.link.request("architect.open", { project: t.value }).then((s) => { st.s = s; st.sel = null; st.glb = ""; draw(); }); }
    if (t.id === "apFile") upload(t.files?.[0]);
    if (t.id === "apUnder") { st.under = t.checked; drawPlan(); }
    if (t.id === "apRoof") { st.roof = t.checked; st.viewer?.setRoof(st.roof); }
    if (t.id === "apLevel") { st.level = t.value || null; st.viewer?.setLevel(st.level); drawPlan(); }
  });
  el.addEventListener("submit", (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.id === "apCmd") { const i = f.querySelector("input"); say(i.value); i.value = ""; return; }
    if (f.id === "apQForm") { const v = f.querySelector("input").value; if (v.trim()) run("architect_answer", { text: v.trim() }, "Продолжаю…"); return; }
    if (f.id === "apForm" && st.sel) {
      const e2 = findEl(model(), st.sel);
      const v = Object.fromEntries(new FormData(f).entries());
      const num = (k) => (v[k] !== undefined && v[k] !== "" ? +v[k] : undefined);
      const ops = [];
      if (e2.type === "wall") {
        ops.push({ op: "set_wall", id: e2.id, thickness: num("thickness"), height: num("height") });
        if (v.material && v.material !== e2.material) ops.push({ op: "set_material", target: e2.id, material: v.material });
      } else if (e2.type === "door" || e2.type === "window") {
        ops.push({ op: "set_opening", id: e2.id, width: num("width"), height: num("height"), ...(e2.type === "window" ? { sill: num("sill") } : {}) });
        if (num("offset") !== undefined && Math.abs(num("offset") - e2.offset) > 1) ops.push({ op: "move_opening", id: e2.id, to: num("offset") });
      } else if (e2.type === "room") {
        if (v.name && v.name !== e2.name) ops.push({ op: "rename_room", id: e2.id, name: v.name });
        if (v.floor) ops.push({ op: "set_material", target: "floor", room: e2.id, material: v.floor });
      } else if (e2.type === "furniture") {
        if (num("rotation") !== undefined && num("rotation") !== Math.round(e2.rotation)) ops.push({ op: "move_furniture", id: e2.id, rotation: num("rotation") });
        if (v.material && v.material !== e2.material) ops.push({ op: "set_furniture", id: e2.id, material: v.material });
      }
      if (ops.length) run("architect_edit", { ops: ops.map((o) => Object.fromEntries(Object.entries(o).filter(([, x]) => x !== undefined))) }, "Меняю " + e2.id);
    }
  });
  for (const ev of ["dragover", "dragenter"]) el.addEventListener(ev, (e) => { if (e.dataTransfer?.types?.includes("Files")) { e.preventDefault(); el.classList.add("drop"); } });
  el.addEventListener("dragleave", (e) => { if (e.target === el) el.classList.remove("drop"); });
  el.addEventListener("drop", (e) => { el.classList.remove("drop"); const f = e.dataTransfer?.files?.[0]; if (f) { e.preventDefault(); upload(f); } });

  // ------------------------------------------------------------------ events from the core
  function onEvent(ev) {
    switch (ev.kind) {
      case "architect.op": {
        const o = ev.op;
        st.ops = [...st.ops.filter((x) => x.op_id !== o.op_id), o].slice(-30);
        if (o.status !== "running") st.busy = "";
        drawStatus(); drawOps();
        break;
      }
      case "architect.stage": {
        if (st.s?.project && ev.project === st.s.active) {
          st.s.project.pipeline = { ...(st.s.project.pipeline || {}), [ev.stage]: { state: ev.state, detail: ev.detail } };
          drawPipe();
        }
        break;
      }
      case "architect.updated": case "architect.question":
        st.busy = "";
        reload();
        break;
      case "architect.view":
        if (ev.mode === "level") { st.level = ev.level || null; st.viewer?.setLevel(st.level); }
        else if (ev.mode === "focus") { st.sel = ev.target; st.viewer?.focus(ev.target); drawPlan(); drawInspector(); }
        else { st.mode = ev.mode; st.viewer?.setMode(ev.mode, ev.target || null); el.querySelectorAll("[data-mode]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.mode === ev.mode))); }
        break;
      case "task.updated":
        break;
    }
  }

  return {
    el,
    show: () => { load(st.s?.active); },
    hide: () => { st.viewer?.setMode(st.viewer.mode === "walk" ? "persp" : st.viewer.mode); },
    onEvent,
  };
}
