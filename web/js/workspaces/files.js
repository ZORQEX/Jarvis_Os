// Files workspace: browse the folders JARVIS may read, search names, preview text, open a file in its application,
// or hand it to JARVIS («Спросить JARVIS о файле» attaches the path to the conversation). Browsing is read-only and
// goes through the core's gateway (scopes, secret paths, audit) without adding chat messages.
import { ICON, esc, fmtBytes, fmtWhen, shortPath } from "../render.js";
import { byId } from "./registry.js";
import { header, emptyState } from "./common.js";
import { prefs } from "../core/store.js";

const TEXT = /\.(txt|md|markdown|csv|json|ya?ml|toml|ini|log|py|js|mjs|ts|tsx|jsx|html?|css|sh|xml|sql|rs|go|java|c|h|cpp|pdf)$/i;

export function create(ctx) {
  const ws = byId("files");
  const el = document.createElement("section");
  el.className = "view view-ws";
  el.dataset.ws = "files";
  el.setAttribute("aria-label", "Файлы");
  el.hidden = true;
  const st = { roots: [], home: "", path: prefs.get("files.path", null), entries: [], sel: null, preview: null, query: "", results: null, busy: false, error: "" };

  async function init() {
    try {
      const r = await ctx.desktop.roots();
      st.roots = r.roots; st.home = r.home;
      if (!st.path || !st.roots.some((x) => st.path.startsWith(x))) st.path = st.roots[0] || null;
    } catch (e) { st.error = e.message; }
    draw();
    if (st.path) open(st.path);
  }

  async function open(path) {
    st.busy = true; st.results = null; st.sel = null; st.preview = null;
    drawList();
    try {
      const d = await ctx.desktop.listFolder(path);
      st.path = d.path; st.entries = d.entries; st.error = "";
      prefs.set("files.path", st.path);
    } catch (e) { st.error = e.message; st.entries = []; }
    st.busy = false;
    drawPath(); drawRoots(); drawList(); drawPreview();
  }

  async function search(q) {
    st.query = q;
    if (!q.trim()) { st.results = null; drawList(); return; }
    st.busy = true; drawList();
    try { const d = await ctx.desktop.searchFiles(q.trim()); st.results = d.results; st.error = ""; }
    catch (e) { st.error = e.message; st.results = []; }
    st.busy = false; drawList();
  }

  async function select(item) {
    st.sel = item;
    // Only the selection changes: the rows stay (a redraw would break double-click and keyboard focus).
    el.querySelectorAll("#fxList [data-item]").forEach((r) => r.setAttribute("aria-selected", String(r.dataset.item === item.path)));
    st.preview = { loading: TEXT.test(item.name) && !item.dir };
    drawPreview();
    if (!st.preview.loading) return;
    try { const d = await ctx.desktop.readText(item.path); if (st.sel === item) st.preview = { text: d.text, clipped: d.clipped }; }
    catch (e) { if (st.sel === item) st.preview = { error: e.message }; }
    drawPreview();
  }

  const short = (p) => shortPath(p);
  const RU = { Documents: "Документы", Downloads: "Загрузки", Projects: "Проекты", Desktop: "Рабочий стол", Pictures: "Изображения",
    Music: "Музыка", Videos: "Видео", Work: "Работа", Jarvis: "Jarvis — записи JARVIS" };
  const rootName = (r) => { const n = r.split("/").pop() || r; return RU[n] || n; };
  const join = (dir, name) => dir.replace(/\/$/, "") + "/" + name;

  function draw() {
    el.innerHTML = `<div class="ws" style="max-width:1280px">${header(ws, ctx.env(), { sub: "только разрешённые папки" })}
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <form id="fxSearch" style="display:flex;gap:6px;flex:1;min-width:260px" role="search">
          <input class="input" id="fxQ" placeholder="Найти файл по имени во всех разрешённых папках" aria-label="Поиск файлов" value="${esc(st.query)}">
          <button class="btn" type="submit">${ICON.search} Найти</button></form>
      </div>
      <div class="fx-path" id="fxPath"></div>
      <div class="fx-split">
        <nav class="fx-roots" id="fxRoots" aria-label="Папки"></nav>
        <div class="fx-list" id="fxList" role="listbox" aria-label="Содержимое папки"></div>
        <aside class="fx-preview panel" id="fxPrev"></aside>
      </div></div>`;
    el.querySelector("#fxSearch").addEventListener("submit", (e) => { e.preventDefault(); search(el.querySelector("#fxQ").value); });
    drawRoots(); drawPath(); drawList(); drawPreview();
  }

  function drawRoots() {
    const box = el.querySelector("#fxRoots");
    if (!box) return;
    box.innerHTML = st.roots.map((r) => `<button class="fx-root" data-dir="${esc(r)}" aria-current="${st.path?.startsWith(r)}" title="${esc(r)}">${ICON.folder}<span>${esc(rootName(r))}</span></button>`).join("")
      || `<div class="empty-note">${st.error ? esc(st.error) : "Загружаю…"}</div>`;
  }
  function drawPath() {
    const box = el.querySelector("#fxPath");
    if (!box || !st.path) return;
    const root = st.roots.find((r) => st.path.startsWith(r)) || st.path;
    const rest = st.path.slice(root.length).split("/").filter(Boolean);
    const crumbs = [[root, short(root)]];
    rest.forEach((p, i) => crumbs.push([root + "/" + rest.slice(0, i + 1).join("/"), p]));
    box.innerHTML = crumbs.map(([p, n], i) => `${i ? "<span>/</span>" : ""}<button data-dir="${esc(p)}">${esc(n)}</button>`).join("");
  }
  function row(item) {
    return `<button class="fx-row" role="option" data-item="${esc(item.path)}" aria-selected="${st.sel?.path === item.path}" title="${esc(short(item.path))}">
      ${item.dir ? ICON.folder : ICON.file}<span class="n">${esc(item.name)}${item.where ? `<span class="faint" style="font-size:12px"> · ${esc(short(item.where))}</span>` : ""}</span>
      <span class="s">${item.dir ? "" : fmtBytes(item.size)}</span><span class="d">${item.modified ? esc(fmtWhen(item.modified)) : ""}</span></button>`;
  }
  function items() {
    if (st.results) return st.results.map((r) => ({ name: r.name, path: r.path, dir: r.dir, size: r.size, modified: r.mtime, where: r.path.replace(/\/[^/]+$/, "") }));
    return st.entries.map((e) => ({ ...e, path: join(st.path, e.name) }));
  }
  function drawList() {
    const box = el.querySelector("#fxList");
    if (!box) return;
    if (st.busy) { box.innerHTML = `<div class="empty-note" style="padding:14px">${st.results === null && !st.query ? "Читаю папку…" : "Ищу…"}</div>`; return; }
    if (st.error) { box.innerHTML = emptyState({ icon: "alert", title: "Не получилось", text: esc(st.error) }); return; }
    const list = items();
    const head = st.results ? `<div class="fx-row" style="cursor:default;color:var(--text-3);font-size:12.5px"><span></span><span>Найдено: ${st.results.length} по «${esc(st.query)}»</span><span></span><button class="btn ghost" data-clear style="height:24px;font-size:12px;justify-self:end">Сбросить</button></div>` : "";
    box.innerHTML = head + (list.length ? list.map(row).join("") : emptyState({ icon: "folder", title: st.results ? "Ничего не найдено" : "Папка пуста",
      text: st.results ? "Ищу по именам файлов и папок; все слова запроса должны быть в имени." : "" }));
  }
  function drawPreview() {
    const box = el.querySelector("#fxPrev");
    if (!box) return;
    const s = st.sel;
    if (!s) { box.innerHTML = emptyState({ icon: "file", title: "Файл не выбран", text: "Выберите файл, чтобы увидеть начало текста, открыть его или спросить JARVIS о нём." }); return; }
    const p = st.preview || {};
    box.innerHTML = `<div style="display:flex;flex-direction:column;gap:10px">
      <div><b style="overflow-wrap:anywhere">${esc(s.name)}</b><div class="faint" style="font-size:12px;overflow-wrap:anywhere">${esc(short(s.path))}</div></div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        ${s.dir ? `<button class="btn" data-dir="${esc(s.path)}">${ICON.folder} Войти</button>` : ""}
        <button class="btn" data-fx="open">${ICON.open} Открыть</button>
        <button class="btn primary" data-fx="ask">${ICON.spark} Спросить JARVIS</button></div>
      ${s.dir ? "" : p.loading ? `<div class="faint" style="font-size:12.5px">Читаю…</div>`
        : p.error ? `<div class="cerr">${esc(p.error)}</div>`
        : p.text != null ? `<pre>${esc(p.text.slice(0, 6000))}</pre>${p.clipped ? `<div class="faint" style="font-size:12px">Показано начало файла</div>` : ""}`
        : `<div class="faint" style="font-size:12.5px">Предпросмотр — для текстовых файлов и PDF.</div>`}</div>`;
  }

  el.addEventListener("click", async (e) => {
    const d = e.target.closest("[data-dir]");
    if (d) { open(d.dataset.dir); return; }
    if (e.target.closest("[data-clear]")) { st.query = ""; st.results = null; const q = el.querySelector("#fxQ"); if (q) q.value = ""; drawList(); return; }
    const it = e.target.closest("[data-item]");
    if (it) {
      const item = items().find((x) => x.path === it.dataset.item);
      if (item?.dir) open(item.path);          // a folder opens with one click
      else if (item) select(item);
      return;
    }
    const a = e.target.closest("[data-fx]");
    if (!a || !st.sel) return;
    if (a.dataset.fx === "open") { try { await ctx.desktop.openPath(st.sel.path); ctx.toast(`Открываю «${st.sel.name}»`, "ok"); } catch (er) { ctx.toast(er.message, "err"); } }
    if (a.dataset.fx === "ask") ctx.attachFile(st.sel.path);
  });
  el.addEventListener("dblclick", async (e) => {
    const it = e.target.closest("[data-item]");
    if (!it) return;
    const item = items().find((x) => x.path === it.dataset.item);
    if (item && !item.dir) { try { await ctx.desktop.openPath(item.path); ctx.toast(`Открываю «${item.name}»`, "ok"); } catch (er) { ctx.toast(er.message, "err"); } }
  });

  return {
    el,
    show() { if (!st.roots.length) init(); else { draw(); open(st.path); } },
    hide() {},
    context() { return st.sel ? st.sel.name : st.path ? short(st.path) : ""; },
  };
}
