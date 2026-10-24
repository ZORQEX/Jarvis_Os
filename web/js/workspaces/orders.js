// «Заказы»: the orders module (fashion/, started by the core) as a JARVIS workspace. Its sections are tabs in this
// header; the pages below are the module's own (in JARVIS's look: the module drops its chrome inside this frame —
// fashion/app/jarvis_embed.py). JARVIS is the only assistant: spoken and typed requests about orders go through the
// core (the module's rules answer at once, the rest is answered by JARVIS's own conversation with the orders tools),
// and a change waits for «Подтвердить» in JARVIS's chat. Messages from the frame are accepted only from its origin.
import { esc } from "../render.js";
import { byId } from "./registry.js";
import { header, emptyState } from "./common.js";

/** The module's areas (fashion/app/navigation.py AREAS without its own assistant) and which pages belong to each. */
export const AREAS = [
  { path: "/", label: "Обзор", prefixes: ["/operations", "/validation"] },
  { path: "/board", label: "Заказы", prefixes: ["/board", "/worksheet", "/excel", "/import", "/imports", "/product-images",
    "/brands", "/seasons", "/orders", "/manual-entry", "/logistics", "/ready-batches", "/shipments", "/groups", "/money",
    "/payments", "/credits", "/tables"] },
  { path: "/factories", label: "Фабрики", prefixes: ["/factories"] },
  { path: "/pricing", label: "Клиенты", prefixes: ["/customers", "/reports", "/pricing"] },
  { path: "/documents", label: "Документы", prefixes: ["/inbox", "/intake", "/ai-intake", "/source-reviews",
    "/intake-sources", "/sources/", "/documents", "/factory-import", "/originals"] },
];
const ADD = [
  ["/factory-import", "Файл фабрики (Excel / PDF)"], ["/manual-entry", "Позиция вручную"],
  ["/money#do-record-payment", "Оплата клиента или возврат"], ["/logistics#do-process-ready", "Список готовых от фабрики"],
  ["/manual-entry#new-season", "Новый сезон"], ["/settings", "Настройки заказов"],
];

/** The area a module page belongs to ("" for settings and other pages outside the tabs). */
export function areaOf(path) {
  const p = String(path || "/").split(/[?#]/)[0] || "/";
  if (p === "/") return "/";
  for (const a of AREAS) if (a.prefixes.some((x) => p === x || p.startsWith(x.endsWith("/") ? x : x + "/") || p === x)) return a.path;
  return "";
}

export function create(ctx) {
  const ws = byId("orders");
  const el = document.createElement("section");
  el.className = "view view-ws view-orders";
  el.dataset.ws = "orders";
  el.setAttribute("aria-label", "Заказы");
  el.hidden = true;
  const st = { state: null, frame: null, origin: "", ready: false, queued: null, timer: null, path: "/board" };

  async function load() {
    try { st.state = await ctx.link.request("fashion.state"); } catch { st.state = null; }
    draw();
    clearTimeout(st.timer);
    if (st.state?.installed && st.state.enabled && !st.state.running) st.timer = setTimeout(load, 2000);   // starting
  }

  function tabs() {
    const cur = areaOf(st.path);
    return `<div class="seg-tabs" role="tablist" aria-label="Разделы заказов">${AREAS.map((a) =>
      `<button role="tab" data-ord-tab="${a.path}" aria-selected="${cur === a.path}"${cur === a.path ? ' aria-current="page"' : ""}>${esc(a.label)}</button>`).join("")}</div>`;
  }

  function go(path) {
    if (!st.frame || !/^\/[\w\-/#?=&.%]*$/.test(path)) return;
    st.path = path;
    st.ready = false;
    st.frame.src = st.state.url + path;
    markTabs();
  }

  function markTabs() {
    const cur = areaOf(st.path);
    el.querySelectorAll("[data-ord-tab]").forEach((b) => {
      const on = b.dataset.ordTab === cur;
      b.setAttribute("aria-selected", String(on));
      if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
  }

  function sendTheme() {
    const t = document.documentElement.dataset.theme;
    st.frame?.contentWindow?.postMessage({ type: "jarvis.theme", theme: t === "light" || t === "dark" ? t : "" }, st.origin);
  }

  function draw() {
    const s = st.state;
    if (s?.running) {
      if (!st.frame) {
        st.origin = new URL(s.url).origin;
        st.ready = false;
        const add = `<details class="menu ord-add"><summary class="btn sm">Добавить</summary><div class="menu-panel" role="menu">${
          ADD.map(([p, l]) => `<button role="menuitem" data-ord-go="${esc(p)}">${esc(l)}</button>`).join("")}</div></details>`;
        el.innerHTML = `<div class="orders-shell">${header(ws, ctx.env(), { right: tabs() + add })}
          <div class="orders-wrap"><iframe class="orders-frame" title="Заказы" src="${esc(s.url)}${esc(st.path)}"></iframe></div></div>`;
        st.frame = el.querySelector("iframe");
        el.querySelectorAll("[data-ord-tab]").forEach((b) => b.addEventListener("click", () => go(b.dataset.ordTab)));
        el.querySelectorAll("[data-ord-go]").forEach((b) => b.addEventListener("click", () => {
          b.closest("details")?.removeAttribute("open");
          go(b.dataset.ordGo);
        }));
        st.frame.addEventListener("load", async () => {
          // Every page of the module adopts JARVIS's dialog first, so a voice turn and the screen are one conversation.
          try { st.state = await ctx.link.request("fashion.state"); } catch { /* keep the last state */ }
          st.frame?.contentWindow.postMessage({ type: "jarvis.conversation", conversation: st.state?.conversation }, st.origin);
          sendTheme();
          st.ready = true;
          if (st.queued) { const q = st.queued; st.queued = null; turn(q); }
        });
      }
      return;
    }
    st.frame = null;
    const why = !s ? "Нет связи с ядром JARVIS." : !s.installed
      ? "Модуль заказов не установлен. Установка: scripts/install.sh (раздел «Заказы»)."
      : !s.enabled ? "Модуль заказов выключен в настройках JARVIS." : (s.error || "Запускаю модуль заказов…");
    el.innerHTML = `<div class="ws">${header(ws, ctx.env(), { sub: "фабрики, клиенты, предоплаты, документы" })}
      <div class="panel">${emptyState({ icon: "box", title: "Заказы", text: esc(why),
        actions: s?.installed && s.enabled && s.error ? '<button class="btn primary" id="ordStart">Запустить ещё раз</button>' : "" })}</div></div>`;
    el.querySelector("#ordStart")?.addEventListener("click", async (e) => {
      e.target.disabled = true;
      try { st.state = await ctx.link.request("fashion.start"); } catch (er) { ctx.toast(er.message, "err"); }
      draw();
    });
  }

  window.addEventListener("message", (e) => {
    if (!st.frame || e.origin !== st.origin || e.source !== st.frame.contentWindow) return;
    const d = e.data && typeof e.data === "object" ? e.data : {};
    if (d.type === "fashion.state") {
      if (typeof d.path === "string" && d.path.startsWith("/")) { st.path = d.path; markTabs(); }
      if (/^[a-f0-9]{16}$/.test(d.conversation || "")) ctx.link.request("fashion.conversation", { id: d.conversation }).catch(() => {});
    } else if (d.type === "fashion.ask" && typeof d.text === "string" && d.text.trim()) {
      ctx.submit(d.text.trim().slice(0, 2000), { source: "text", stay: true });    // a question from a module page: to JARVIS
    } else if (d.type === "fashion.listen") {
      ctx.voice.listenOnce();               // a microphone button on a module page: JARVIS listens (local recognition)
    }
  });

  /** A turn JARVIS ran by voice or from the chat: follow its navigation, or reload after a confirmed change. */
  function turn(ev) {
    if (!st.frame || !st.ready) { st.queued = ev; return; }
    if (typeof ev.navigate === "string" && ev.navigate.startsWith("/")) { st.path = ev.navigate; markTabs(); }
    st.frame.contentWindow.postMessage({ type: "jarvis.turn", conversation: ev.conversation,
      navigate: typeof ev.navigate === "string" && ev.navigate.startsWith("/") ? ev.navigate : null,
      committed: !!ev.committed }, st.origin);
  }

  return { el, show: () => { load(); sendTheme(); }, hide: () => clearTimeout(st.timer), turn };
}
