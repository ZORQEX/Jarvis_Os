// «Заказы»: the fashion-orders app (fashion/, started by the core) shown inside the window — its own pages (orders,
// factories, customers, documents), its assistant dock and its «Подтвердить» buttons. JARVIS adds the voice: spoken
// requests about orders go to the app's assistant through the core (same dialog), JARVIS speaks the short answer, and
// the turn then appears here. The frame is the app's own loopback page (allowed by the window's CSP frame-src);
// messages from it are accepted only from that origin and that frame.
import { esc } from "../render.js";
import { byId } from "./registry.js";
import { header, emptyState } from "./common.js";

export function create(ctx) {
  const ws = byId("orders");
  const el = document.createElement("section");
  el.className = "view view-ws view-orders";
  el.dataset.ws = "orders";
  el.setAttribute("aria-label", "Заказы");
  el.hidden = true;
  const st = { state: null, frame: null, origin: "", ready: false, queued: null, timer: null };

  async function load() {
    try { st.state = await ctx.link.request("fashion.state"); } catch { st.state = null; }
    draw();
    clearTimeout(st.timer);
    if (st.state?.installed && st.state.enabled && !st.state.running) st.timer = setTimeout(load, 2000);   // starting
  }

  function draw() {
    const s = st.state;
    if (s?.running) {
      if (!st.frame) {
        st.origin = new URL(s.url).origin;
        st.ready = false;
        el.innerHTML = `<div class="orders-wrap"><iframe class="orders-frame" title="Заказы" src="${esc(s.url)}/board"></iframe></div>`;
        st.frame = el.querySelector("iframe");
        st.frame.addEventListener("load", () => { st.ready = true; if (st.queued) { const q = st.queued; st.queued = null; turn(q); } });
      }
      return;
    }
    st.frame = null;
    const why = !s ? "Нет связи с ядром JARVIS." : !s.installed
      ? "Модуль заказов не установлен. Установка: scripts/install.sh (раздел «Заказы»)."
      : !s.enabled ? "Модуль заказов выключен в настройках JARVIS." : (s.error || "Запускаю приложение заказов…");
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
    if (d.type === "fashion.state" && /^[a-f0-9]{16}$/.test(d.conversation || "")) {
      ctx.link.request("fashion.conversation", { id: d.conversation }).catch(() => {});
    } else if (d.type === "fashion.listen") {
      ctx.voice.listenOnce();               // the app's mic button: JARVIS listens (local speech recognition)
    }
  });

  /** A turn JARVIS ran by voice or from the chat: show it in the app's dock (or follow its navigation / reload). */
  function turn(ev) {
    if (!st.frame || !st.ready) { st.queued = ev; return; }
    st.frame.contentWindow.postMessage({ type: "jarvis.turn", conversation: ev.conversation,
      navigate: typeof ev.navigate === "string" && ev.navigate.startsWith("/") ? ev.navigate : null,
      committed: !!ev.committed }, st.origin);
  }

  return { el, show: () => load(), hide: () => clearTimeout(st.timer), turn };
}
