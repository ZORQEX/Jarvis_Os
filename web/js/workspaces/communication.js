// CommunicationWorkspace: one foundation for Telegram, WhatsApp and mail; the differences live in the adapters.
// Left: people (JARVIS address book + everyone JARVIS has written to on this service). Right: the messages JARVIS
// sent them with their real delivery status, and a composer. Sending goes through JARVIS: the core finds the person,
// prepares the message and asks «Отправить?» — the confirmation card appears right here and at the companion.
// Incoming messages are not read by the core yet; the page says so instead of drawing a fake chat.
import { ICON, esc, fmtWhen, approvalCard } from "../render.js";
import { byId, connectionOf } from "./registry.js";
import { header, emptyState, avatar, ago } from "./common.js";
import { ADAPTERS } from "../adapters/channels.js";
import { accountCard, wireAccounts } from "../comms.js";
import { frameBatch } from "../core/store.js";

const JOB = {
  scheduled: ["warn", "запланировано"], sending: ["warn", "отправляется"], sent: ["ok", "отправлено ✓"], failed: ["err", "не отправлено"],
  cancelled: ["", "отменено"], unknown: ["err", "статус неизвестен"], missed: ["err", "пропущено"],
};

export function create(ctx, wsId) {
  const ws = byId(wsId);
  const ad = ADAPTERS[ws.channel];
  const el = document.createElement("section");
  el.className = "view view-ws";
  el.dataset.ws = wsId;
  el.setAttribute("aria-label", ws.title);
  el.hidden = true;
  const st = { data: null, jobs: [], q: "", sel: null, newTo: false, drafts: {}, loading: false, error: "" };

  const typing = () => { const a = document.activeElement; return !!a && el.contains(a) && ["INPUT", "TEXTAREA"].includes(a.tagName) && !!a.value; };
  const provider = () => st.data?.providers?.find((p) => p.id === ws.channel) || ctx.providers()[ws.channel] || null;

  async function load() {
    st.loading = true;
    try {
      const [d, j] = await Promise.all([ctx.link.request("comms.state"), ctx.link.request("comms.jobs", { limit: 300 })]);
      st.data = d; st.jobs = j.jobs; st.error = "";
      ctx.setProviders(d.providers);
    } catch (e) { st.error = e.message; }
    st.loading = false;
    if (!typing()) draw();
    // While WhatsApp shows a QR code (or Telegram waits for a code) the state changes without events: poll gently.
    clearTimeout(st.poll);
    if (!el.hidden && provider()?.state === "connecting") st.poll = setTimeout(() => { if (!el.hidden) load(); }, 2500);
  }

  const people = () => ad.people(st.data?.contacts, st.jobs);
  const selected = () => st.sel && people().find((p) => p.id === st.sel) || null;

  function draw() {
    if (el.hidden) return;
    const p = provider(), c = connectionOf(p);
    const pill = p ? `<span class="cpill ${{ connected: "ok", connecting: "warn", error: "err" }[p.state] || ""}">${esc(c.label)}</span>` : "";
    const right = `<button class="btn ghost" data-nav="comms" title="Очередь, правила «без подтверждения», адресная книга">Очередь и правила</button>`;
    if (st.error && !st.data) {
      el.innerHTML = `<div class="ws">${header(ws, ctx.env(), { right })}<div class="panel">${emptyState({ icon: ws.icon, title: "Модуль связи недоступен", text: esc(st.error) })}</div></div>`;
      return;
    }
    const connected = p?.state === "connected";
    el.innerHTML = `<div class="ws cw">${header(ws, ctx.env(), { sub: `${pill}${p?.account && p.account !== ws.title && p.account !== p.title ? ` <span class="faint">${esc(p.account)}</span>` : ""}`, right })}
      ${!connected && st.data ? `<div class="cw-connect" id="cwConnect">${accountCard(p)}</div>` : ""}
      <div class="cw-split">
        <div class="cw-side">
          <div class="cw-search"><input id="cwQ" placeholder="Поиск" aria-label="Поиск по людям" value="${esc(st.q)}">
            <button class="icon-btn" id="cwNew" aria-label="Новое сообщение" title="Новое сообщение">${ICON.plus}</button></div>
          <div class="cw-list" id="cwList" role="listbox" aria-label="Получатели"></div>
        </div>
        <div class="cw-main" id="cwMain"></div>
      </div></div>`;
    const conn = el.querySelector("#cwConnect");
    if (conn) wireAccounts(conn, ctx, () => st.data, (d) => { st.data = d; ctx.setProviders(d.providers); draw(); });
    el.querySelector("#cwQ").addEventListener("input", (e) => { st.q = e.target.value; drawList(); });
    el.querySelector("#cwNew").addEventListener("click", () => { st.sel = null; st.newTo = true; drawList(); drawMain(); el.querySelector("#cwTo")?.focus(); });
    drawList();
    drawMain();
  }

  function drawList() {
    const box = el.querySelector("#cwList");
    if (!box) return;
    const q = st.q.trim().toLowerCase();
    const list = people().filter((p) => !q || p.name.toLowerCase().includes(q) || p.address.toLowerCase().includes(q));
    box.innerHTML = list.length ? list.map((p) => `<button class="cw-item" role="option" data-person="${esc(p.id)}" aria-selected="${p.id === st.sel}">
        ${avatar(p.name)}<span style="min-width:0"><div class="nm">${esc(p.name)}</div><div class="pv">${esc(p.preview || p.address || "в адресной книге")}</div></span>
        <span class="w">${p.last ? esc(ago(p.last)) : ""}</span></button>`).join("")
      : `<div class="empty-note" style="padding:12px">${st.loading ? "Загружаю…" : q ? "Никого не нашлось." : `Пока никого. Нажмите «+» или скажите: «Отправь Ивану ${esc(ad.toPhrase)}: буду в 19:00».`}</div>`;
  }

  function pendingHere(name) {
    return Object.values(ctx.S.approvals).filter((a) => a.status === "pending" && a.tool_id === "message_send"
      && (a.args?.deliveries || []).some((d) => d.channel === ws.channel && (!name || d.to_name.toLowerCase() === name.toLowerCase())));
  }

  function drawMain() {
    const box = el.querySelector("#cwMain");
    if (!box) return;
    const p = selected();
    const connected = provider()?.state === "connected";
    if (!p && !st.newTo) {
      const pend = pendingHere(null);
      box.innerHTML = `${pend.length ? `<div class="cw-confirm" style="margin-top:12px">${pend.map(approvalCard).join("")}</div>` : ""}
        ${emptyState({ icon: ws.icon, title: "Никто не выбран", text: `Выберите получателя слева или попросите JARVIS — он найдёт человека, подготовит текст и спросит перед отправкой.`,
          tries: [`Отправь Ивану ${ad.toPhrase}: буду примерно в 19:00`, `Напиши маме ${ad.toPhrase} в 9:00: доброе утро`] })}`;
      return;
    }
    const head = p ? `${avatar(p.name)}<div><div class="nm">${esc(p.name)}</div><div class="ad">${esc(p.address || "адрес найдёт JARVIS")}</div></div>`
      : `<div style="flex:1"><input class="input" id="cwTo" placeholder="Кому? Имя, как в контактах" aria-label="Получатель" style="width:100%"></div>`;
    const msgs = p ? p.jobs.map((j) => {
      const [cls, label] = JOB[j.status] || ["", j.status];
      const when = j.status === "sent" ? j.sent_at : j.due_at || j.created_at;
      return `<div class="bubble ${j.status === "failed" ? "failed" : ""}">${j.subject ? `<b>${esc(j.subject)}</b>\n` : ""}${esc(j.body)}
        <div class="bm"><span>${esc(fmtWhen(when))}</span><span class="st ${cls}">${esc(label)}</span>
        ${j.status === "scheduled" ? `<button class="btn ghost" data-cancel-job="${esc(j.id)}" style="height:20px;font-size:11.5px;padding:0 6px">отменить</button>` : ""}</div>
        ${j.last_error && j.status !== "sent" ? `<div class="cerr">${esc(j.last_error)}</div>` : ""}</div>`;
    }).join("") : "";
    const pend = pendingHere(p?.name || null);
    box.innerHTML = `<div class="cw-conv-head">${head}</div>
      <div class="cw-msgs" id="cwMsgs">
        <div class="cw-note">${ICON.lock.replace("<svg", '<svg style="width:13px;height:13px;vertical-align:-2px"')} Здесь — сообщения, отправленные через JARVIS. Входящие JARVIS пока не читает.</div>
        ${msgs || `<div class="cw-note">Через JARVIS вы ещё не писали${p ? " этому человеку" : ""} ${esc(ad.toPhrase)}.</div>`}
      </div>
      ${pend.length ? `<div class="cw-confirm">${pend.map(approvalCard).join("")}</div>` : ""}
      <form class="cw-compose" id="cwForm" autocomplete="off">
        <textarea id="cwText" rows="1" aria-label="Текст сообщения" placeholder="${connected ? `Написать ${esc(p ? p.name : "")}…` : `${esc(ws.title)} не подключён — подключите выше, чтобы отправлять`}">${esc(st.drafts[p?.id || "_new"] || "")}</textarea>
        <button type="button" class="icon-btn mic" id="cwMic" aria-label="Надиктовать JARVIS" title="Сказать JARVIS (Ctrl+Space)">${ICON.mic}</button>
        <button type="submit" class="send" id="cwSend" aria-label="Подготовить к отправке" title="JARVIS подготовит и спросит перед отправкой" disabled>${ICON.arrowUp}</button>
      </form>`;
    const ta = el.querySelector("#cwText"), send = el.querySelector("#cwSend"), to = el.querySelector("#cwTo");
    const upd = () => {
      st.drafts[p?.id || "_new"] = ta.value;
      send.disabled = !ta.value.trim() || (!p && !to?.value.trim());
      ta.style.height = "auto"; ta.style.height = Math.min(140, ta.scrollHeight) + "px";
    };
    ta.addEventListener("input", upd); to?.addEventListener("input", upd);
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); el.querySelector("#cwForm").requestSubmit(); } });
    el.querySelector("#cwMic").addEventListener("click", () => ctx.voice.listenOnce());
    el.querySelector("#cwForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = ta.value.trim(), name = p ? p.name : to.value.trim();
      if (!text || !name) return;
      send.disabled = true;
      const id = await ctx.submit(ad.composePhrase(name, text), { stay: true });
      if (id) { st.drafts[p?.id || "_new"] = ""; ta.value = ""; ctx.toast("JARVIS готовит сообщение — подтвердите отправку", "ok"); }
      upd();
    });
    upd();
    const m = el.querySelector("#cwMsgs"); m.scrollTop = m.scrollHeight;
  }

  el.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-person]");
    if (b) { st.sel = b.dataset.person; st.newTo = false; drawList(); drawMain(); el.querySelector("#cwText")?.focus(); return; }
    const c = e.target.closest("[data-cancel-job]");
    if (c) { c.disabled = true; try { await ctx.link.request("comms.cancel", { job_id: c.dataset.cancelJob }); } catch (er) { ctx.toast(er.message, "err"); c.disabled = false; } }
  });

  const redrawMain = frameBatch(() => { if (!el.hidden && !el.querySelector("#cwText:focus, #cwTo:focus")) drawMain(); else if (!el.hidden) drawPendingOnly(); });
  function drawPendingOnly() {
    // Typing in the composer: refresh only the confirmation cards, never the text field.
    const p = selected(), pend = pendingHere(p?.name || null);
    let box = el.querySelector(".cw-confirm");
    if (!pend.length) { box?.remove(); return; }
    if (!box) { box = document.createElement("div"); box.className = "cw-confirm"; el.querySelector("#cwForm")?.before(box); }
    box.innerHTML = pend.map(approvalCard).join("");
  }
  const offs = [];
  return {
    el,
    show() {
      draw(); load();
      if (!offs.length) {
        offs.push(ctx.bus.on("comms.job", (job) => {
          const i = st.jobs.findIndex((j) => j.id === job.id);
          if (i >= 0) st.jobs[i] = job; else st.jobs.unshift(job);
          drawList(); redrawMain();
        }));
        offs.push(ctx.bus.on("providers", () => {
          if (st.data) st.data.providers = Object.values(ctx.providers());
          if (!typing()) draw();       // never wipe what the person is typing (a Telegram code, a message)
        }));
        offs.push(ctx.bus.on(["approvals", "link"], redrawMain));
      }
    },
    hide() { clearTimeout(st.poll); while (offs.length) offs.pop()(); },
  };
}
