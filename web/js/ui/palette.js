// Command palette (Ctrl+K): every workspace, global command and recent task, searchable; anything else typed is
// sent to JARVIS as a request («Спросить JARVIS: …»). Keyboard: ↑ ↓ Enter, Esc closes. Commands come from the app,
// so the list always reflects real state (e.g. «Снять экстренную остановку» appears only when it is on).
import { rank } from "../core/fuzzy.js";
import { ICON, esc } from "../render.js";

/** @typedef {{id:string, title:string, keywords?:string, group:string, icon?:string, hint?:string, sub?:string, run:() => any}} Command */

export function createPalette(ctx) {
  const el = document.createElement("div");
  el.className = "palette";
  el.hidden = true;
  el.innerHTML = `<div class="pal" role="dialog" aria-modal="true" aria-label="Команды">
    <div class="pal-in">${ICON.search}<input type="text" role="combobox" aria-expanded="true" aria-controls="palList" aria-autocomplete="list"
      placeholder="Команда, раздел или вопрос для JARVIS…" autocomplete="off" spellcheck="false"></div>
    <div class="pal-list" id="palList" role="listbox"></div>
    <div class="pal-foot"><span><span class="kbd">↑↓</span> выбрать</span><span><span class="kbd">Enter</span> выполнить</span><span><span class="kbd">Esc</span> закрыть</span></div>
  </div>`;
  document.body.appendChild(el);
  const input = el.querySelector("input"), list = el.querySelector(".pal-list");
  let items = [], shown = [], sel = 0, prevFocus = null;

  function build() {
    items = ctx.commands();
    draw();
  }
  function draw() {
    const q = input.value.trim();
    shown = q ? rank(q, items, 40) : items.filter((c) => !c.searchOnly);
    if (q) shown.push({ id: "ask", title: `Спросить JARVIS: «${q}»`, group: "JARVIS", icon: "spark", sub: "Отправить как запрос — простые команды выполнятся на компьютере, остальное ответит Claude",
      run: () => ctx.ask(q) });
    sel = Math.min(sel, Math.max(0, shown.length - 1));
    if (!shown.length) { list.innerHTML = `<div class="pal-empty">Ничего не найдено</div>`; return; }
    let group = null;
    list.innerHTML = shown.map((c, i) => {
      const head = c.group !== group ? `<div class="pal-group" role="presentation">${esc(c.group)}</div>` : "";
      group = c.group;
      return `${head}<button class="pal-item" role="option" id="pal-${i}" data-i="${i}" aria-selected="${i === sel}">
        <span aria-hidden="true">${ICON[c.icon] || ICON.arrowRight}</span>
        <span class="t">${esc(c.title)}${c.sub ? `<span class="sub">${esc(c.sub)}</span>` : ""}</span>
        <span class="k">${esc(c.hint || "")}</span></button>`;
    }).join("");
    input.setAttribute("aria-activedescendant", `pal-${sel}`);
  }
  function move(d) {
    if (!shown.length) return;
    sel = (sel + d + shown.length) % shown.length;
    list.querySelectorAll(".pal-item").forEach((b) => b.setAttribute("aria-selected", String(+b.dataset.i === sel)));
    list.querySelector(`[data-i="${sel}"]`)?.scrollIntoView({ block: "nearest" });
    input.setAttribute("aria-activedescendant", `pal-${sel}`);
  }
  function run(i) {
    const c = shown[i];
    if (!c) return;
    close(false);
    try { const r = c.run(); if (r?.catch) r.catch((e) => ctx.toast(e.message, "err")); } catch (e) { ctx.toast(e.message, "err"); }
  }
  input.addEventListener("input", () => { sel = 0; draw(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
    else if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); run(sel); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    else if (e.key === "Tab") e.preventDefault();          // focus stays in the palette
  });
  list.addEventListener("click", (e) => { const b = e.target.closest(".pal-item"); if (b) run(+b.dataset.i); });
  list.addEventListener("mousemove", (e) => {
    const b = e.target.closest(".pal-item");
    if (b && +b.dataset.i !== sel) { sel = +b.dataset.i; list.querySelectorAll(".pal-item").forEach((x) => x.setAttribute("aria-selected", String(+x.dataset.i === sel))); }
  });
  el.addEventListener("mousedown", (e) => { if (e.target === el) close(); });

  function open(query = "") {
    if (!el.hidden) { input.select(); return; }
    prevFocus = document.activeElement;
    el.hidden = false;
    input.value = query;
    sel = 0;
    build();
    input.focus();
  }
  function close(restore = true) {
    if (el.hidden) return;
    el.hidden = true;
    if (restore && prevFocus?.focus) prevFocus.focus();
  }
  return { open, close, get isOpen() { return !el.hidden; }, el };
}
