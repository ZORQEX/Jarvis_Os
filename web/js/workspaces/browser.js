// Browser workspace. What is real today: open a link or a web search in the person's own browser (open_url through
// the core's gateway) and hand a website job to JARVIS (Claude Code drives a separate headless browser without the
// person's logins). Sites are not embedded: they refuse to be framed, and the window's security policy stays strict.
// Back / forward / reload of the person's browser window need a browser adapter — shown as not connected.
import { ICON, esc } from "../render.js";
import { byId } from "./registry.js";
import { header, emptyState, ago } from "./common.js";
import { prefs } from "../core/store.js";

const ENGINES = {
  google: ["Google", "https://www.google.com/search?q="],
  yandex: ["Яндекс", "https://yandex.ru/search/?text="],
  ddg: ["DuckDuckGo", "https://duckduckgo.com/?q="],
};

/** Text in the address bar → a URL to open, or null when it is a search query. */
export function asUrl(text) {
  const t = text.trim();
  if (!t || /\s/.test(t)) return null;
  if (/^https?:\/\/\S+$/i.test(t)) return t;
  if (/^(localhost|(\d{1,3}\.){3}\d{1,3})(:\d+)?(\/\S*)?$/i.test(t)) return "http://" + t;
  if (/^[\p{L}\d-]+(\.[\p{L}\d-]+)*\.[\p{L}]{2,}(:\d+)?(\/\S*)?$/u.test(t)) return "https://" + t;
  return null;
}

export function create(ctx) {
  const ws = byId("browser");
  const el = document.createElement("section");
  el.className = "view view-ws";
  el.dataset.ws = "browser";
  el.setAttribute("aria-label", "Браузер");
  el.hidden = true;
  const st = { engine: prefs.get("browser.engine", "google"), links: [] };

  function draw() {
    const nav = (icon, label) => `<button class="icon-btn" disabled aria-label="${label}" title="${label} — окно вашего браузера JARVIS пока не управляет">${ICON[icon]}</button>`;
    el.innerHTML = `<div class="ws">${header(ws, ctx.env(), { sub: "открывается в вашем браузере" })}
      <form class="br-bar" id="brForm" autocomplete="off">
        ${nav("back", "Назад")}${nav("forward", "Вперёд")}${nav("refresh", "Обновить")}
        <label class="br-url">${ICON.globe}<input id="brInput" aria-label="Адрес или поиск" placeholder="Адрес сайта или что найти" spellcheck="false">
          <select id="brEngine" aria-label="Поисковая система">${Object.entries(ENGINES).map(([k, [n]]) => `<option value="${k}" ${k === st.engine ? "selected" : ""}>${n}</option>`).join("")}</select></label>
        <button class="btn primary" type="submit" id="brGo">Открыть</button>
      </form>
      <div class="br-body">
        <div style="display:flex;flex-direction:column;gap:var(--s4);min-width:0">
          <div class="panel">${emptyState({ icon: "globe", title: "Сайты открываются в вашем браузере",
            text: "Введите адрес или запрос — JARVIS откроет его в браузере по умолчанию. Встроенного просмотра нет: сайты запрещают показ внутри других окон, а JARVIS не ослабляет защиту своего окна." })}</div>
          <div class="panel"><div class="blk-h" style="margin-bottom:6px">${ICON.history} Открытые через JARVIS</div><div id="brLinks"></div></div>
        </div>
        <div class="panel agent-box" style="display:flex;flex-direction:column;gap:10px">
          <div class="blk-h">${ICON.spark} Поручить работу с сайтом</div>
          <div class="muted" style="font-size:13px">JARVIS откроет сайт в отдельном браузере — без ваших входов и cookies — и сделает то, что вы попросите: найти, сравнить, заполнить. Нужен Claude; действия с последствиями — с вашего подтверждения.</div>
          <input class="input" id="brSite" placeholder="Сайт, например ozon.ru" style="min-width:0" spellcheck="false">
          <textarea id="brTask" placeholder="Что сделать? Например: найди цену на iPhone 17 и пришли ссылку"></textarea>
          <button class="btn primary" id="brAgent" style="align-self:flex-start" disabled>${ICON.spark} Поручить JARVIS</button>
        </div>
      </div></div>`;
    const form = el.querySelector("#brForm"), input = el.querySelector("#brInput");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const t = input.value.trim();
      if (!t) return;
      const url = asUrl(t) || ENGINES[st.engine][1] + encodeURIComponent(t);
      el.querySelector("#brGo").disabled = true;
      try { await ctx.desktop.openUrl(url); ctx.toast(`Открываю: ${url.length > 70 ? url.slice(0, 70) + "…" : url}`, "ok"); input.value = ""; setTimeout(loadLinks, 1500); }
      catch (er) { ctx.toast(er.message, "err"); }
      el.querySelector("#brGo").disabled = false;
    });
    el.querySelector("#brEngine").addEventListener("change", (e) => { st.engine = e.target.value; prefs.set("browser.engine", st.engine); });
    const site = el.querySelector("#brSite"), task = el.querySelector("#brTask"), go = el.querySelector("#brAgent");
    const upd = () => { go.disabled = !task.value.trim(); };
    site.addEventListener("input", upd); task.addEventListener("input", upd);
    go.addEventListener("click", async () => {
      const s = site.value.trim(), t = task.value.trim();
      const phrase = s ? `Зайди на сайт ${s} в браузере и ${t.charAt(0).toLowerCase() + t.slice(1)}` : `Через браузер: ${t}`;
      go.disabled = true;
      const id = await ctx.submit(phrase, { stay: true });
      if (id) { ctx.toast("Поручено JARVIS — ход выполнения в «Задачах» и у компаньона", "ok"); task.value = ""; }
      upd();
    });
    drawLinks();
  }

  function drawLinks() {
    const box = el.querySelector("#brLinks");
    if (!box) return;
    box.innerHTML = st.links.length ? st.links.map((l) => `<button class="link-row" data-url="${esc(l.url)}" title="Открыть снова: ${esc(l.url)}">${ICON.globe}
        <span class="u">${esc(hostOf(l.url))}<small>${esc(l.url)}</small></span><span class="w">${esc(ago(l.at))}</span></button>`).join("")
      : `<div class="empty-note">Пока ничего. Ссылки, открытые JARVIS по вашей просьбе, появятся здесь.</div>`;
  }
  const hostOf = (u) => { try { return new URL(u).host; } catch { return u; } };

  /** Real record: open_url steps of recent tasks (what JARVIS actually opened). */
  async function loadLinks() {
    try {
      const tasks = await ctx.agent.recentTasks(150);
      const seen = new Set(), out = [];
      for (const t of tasks) for (const s of t.steps || []) {
        if (s.tool_id !== "open_url" || s.state !== "succeeded") continue;
        const url = s.args?.url;
        if (!url || seen.has(url)) continue;
        seen.add(url); out.push({ url, at: s.finished_at || t.created_at });
      }
      st.links = out.sort((a, b) => b.at - a.at).slice(0, 12);
    } catch { /* offline: keep the last list */ }
    drawLinks();
  }

  return {
    el,
    show() { draw(); loadLinks(); setTimeout(() => el.querySelector("#brInput")?.focus(), 0); },
    hide() {},
  };
}
