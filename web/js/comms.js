// Сообщения: подключение Telegram / WhatsApp / почты, очередь отправки (запланировано, отправлено, ошибки),
// правила «без подтверждения», адресная книга. Всё состояние — с ядра; статусы честные (CONNECTED / NOT CONNECTED…).
import { esc, ICON, fmtWhen } from "./render.js";

const st = { filter: "scheduled", data: null, jobs: [], tgStage: null, busy: false };
let root = null, ctx = null, poll = null;

const STATE = {
  connected: ["ok", "Подключено"], not_connected: ["", "Не подключено"], connecting: ["warn", "Подключение…"],
  error: ["err", "Ошибка"],
};
const JOB = {
  scheduled: ["warn", "Запланировано"], sending: ["warn", "Отправляется"], sent: ["ok", "Отправлено"],
  failed: ["err", "Не отправлено"], cancelled: ["", "Отменено"], unknown: ["err", "Статус неизвестен"],
  missed: ["err", "Пропущено"],
};
const CH = { telegram: "Telegram", whatsapp: "WhatsApp", email: "Почта" };

export async function render(el, c) {
  root = el; ctx = c;
  await refresh();
}

export function onEvent(ev) {
  if (!root || root.hidden) return;
  if (ev.kind === "comms.job") {
    const i = st.jobs.findIndex((j) => j.id === ev.job.id);
    if (i >= 0) st.jobs[i] = ev.job; else st.jobs.unshift(ev.job);
    drawJobs();
  }
  if (ev.kind === "comms.providers" && st.data) { st.data.providers = ev.providers; drawAccounts(); }
}

async function refresh() {
  try {
    const [d, j] = await Promise.all([ctx.link.request("comms.state"), ctx.link.request("comms.jobs", { limit: 150 })]);
    st.data = d; st.jobs = j.jobs;
  } catch (e) {
    root.innerHTML = `<div class="page"><h1>Сообщения</h1><p class="lead">Модуль связи недоступен: ${esc(e.message)}</p></div>`;
    return;
  }
  draw();
}

function draw() {
  root.innerHTML = `<div class="page comms">
    <h1>Сообщения</h1>
    <p class="lead">Telegram, WhatsApp и почта — голосом: «Отправь Ивану в Telegram в 13:00: буду через час».
      Перед отправкой JARVIS всегда переспрашивает; ответьте «да» голосом или кнопкой.</p>
    <div class="panel"><h2>Очередь и история</h2>
      <div class="field" role="tablist">${[["scheduled", "Запланировано"], ["sent", "Отправлено"], ["problems", "Проблемы"], ["all", "Все"]]
        .map(([k, l]) => `<button class="btn ${st.filter === k ? "primary" : "ghost"}" data-cf="${k}">${l}</button>`).join("")}</div>
      <div id="cjobs"></div></div>
    <div class="panel"><h2>Подключения</h2>
      <div class="muted" style="font-size:13px">Вход — только через сами сервисы (код Telegram, QR-код WhatsApp, пароль приложения почты).
        Ключи и сессии хранятся в системной связке ключей и никогда не передаются ИИ.</div>
      <div class="pcards" id="caccounts"></div></div>
    <div class="panel" id="crules"></div>
    <div class="panel" id="cbook"></div>
  </div>`;
  drawJobs(); drawAccounts(); drawRules(); drawBook();
  root.querySelectorAll("[data-cf]").forEach((b) => b.addEventListener("click", () => { st.filter = b.dataset.cf; draw(); }));
}

function drawJobs() {
  const box = root?.querySelector("#cjobs");
  if (!box) return;
  const f = st.filter;
  const jobs = st.jobs.filter((j) => f === "all" || (f === "problems" ? ["failed", "unknown", "missed"].includes(j.status) : j.status === f))
    .sort((a, b) => f === "scheduled" ? a.due_at - b.due_at : (b.sent_at || b.updated_at) - (a.sent_at || a.updated_at));
  if (!jobs.length) {
    box.innerHTML = `<div class="empty-note" style="margin-top:10px">${{ scheduled: "Запланированных сообщений нет.", sent: "Ещё ничего не отправлено.",
      problems: "Проблем нет.", all: "Сообщений ещё не было." }[f]}</div>`;
    return;
  }
  box.innerHTML = `<table class="tbl"><thead><tr><th>Когда</th><th>Куда</th><th>Текст</th><th>Статус</th><th></th></tr></thead><tbody>
    ${jobs.map((j) => {
      const [cls, label] = JOB[j.status] || ["", j.status];
      const when = j.status === "sent" ? j.sent_at : (j.due_at || j.created_at);
      return `<tr><td style="white-space:nowrap">${fmtWhen(when)}</td>
        <td><b>${esc(CH[j.channel] || j.channel)}</b><br>${esc(j.to_name)}${j.to_address ? `<br><span class="faint">${esc(j.to_address)}</span>` : ""}</td>
        <td>${j.subject ? `<span class="faint">Тема: ${esc(j.subject)}</span><br>` : ""}${esc(j.body.slice(0, 300))}
          ${j.last_error && j.status !== "sent" ? `<div class="cerr">${esc(j.last_error)}</div>` : ""}
          ${j.verified ? `<div class="faint" style="font-size:12px">${esc(j.verified.replace(/^(verified|unverified|failed): /, "Проверка: "))}</div>` : ""}</td>
        <td><span class="cpill ${cls}">${label}</span></td>
        <td>${j.status === "scheduled" ? `<button class="btn ghost" data-ccancel="${esc(j.id)}">Отменить</button>` : ""}</td></tr>`;
    }).join("")}</tbody></table>`;
  box.querySelectorAll("[data-ccancel]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await ctx.link.request("comms.cancel", { job_id: b.dataset.ccancel }); ctx.toast("Отменяю…"); }
    catch (e) { ctx.toast(e.message, "err"); b.disabled = false; }
  }));
}

function badge(p) {
  const [cls, label] = STATE[p.state] || ["", p.state];
  return `<span class="cpill ${cls}">${label}</span>`;
}

function drawAccounts() {
  const box = root?.querySelector("#caccounts");
  if (!box || !st.data) return;
  if (box.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;   // don't wipe typing
  const P = Object.fromEntries(st.data.providers.map((p) => [p.id, p]));
  box.innerHTML = [tgCard(P.telegram), waCard(P.whatsapp), mailCard(P.email)].join("");
  wireAccounts(box, ctx, () => st.data, (d) => { st.data = d; drawAccounts(); });
  const connecting = st.data.providers.some((p) => p.state === "connecting" && p.id === "whatsapp");
  clearInterval(poll);
  if (connecting) poll = setInterval(async () => {
    if (!root || root.hidden) return clearInterval(poll);
    try { st.data = await ctx.link.request("comms.state"); drawAccounts(); } catch { }
  }, 2500);
}

function caps(p) {
  const c = p.caps || {};
  const list = [c.can_send_text && "текст", c.can_schedule && "по расписанию", c.can_search_contacts && "поиск контактов",
    c.can_send_file && "файлы"].filter(Boolean);
  return `<div class="hstate">Умеет: ${list.join(", ")}${c.can_send_file ? "" : " · файлы пока нет"}</div>`;
}

function tgCard(p) {
  if (!p) return "";
  let body = "";
  if (p.state === "connected") {
    body = `<div>${esc(p.account)}</div><div class="hstate">${esc(p.detail)}</div>
      <button class="btn ghost" data-ca="tg-logout">Отключить и выйти из сеанса</button>`;
  } else if (p.state === "connecting") {
    const pw = p.login_stage === "password";
    body = `<div class="hstate">${esc(p.detail)}</div>
      <input class="input" id="tgCode" ${pw ? 'type="password" placeholder="Облачный пароль Telegram"' : 'inputmode="numeric" placeholder="Код из Telegram"'} autocomplete="off">
      <button class="btn primary" data-ca="tg-finish">${pw ? "Войти" : "Подтвердить код"}</button>`;
  } else {
    body = `${p.state === "error" ? `<div class="cerr">${esc(p.detail)}</div>` : ""}
      <div class="hstate">${esc(p.requirement)}</div>
      <input class="input" id="tgId" placeholder="api_id" inputmode="numeric" autocomplete="off">
      <input class="input" id="tgHash" placeholder="api_hash" autocomplete="off">
      <input class="input" id="tgPhone" placeholder="Номер телефона, +7…" autocomplete="off">
      <button class="btn primary" data-ca="tg-begin">Получить код</button>`;
  }
  return `<div class="pcard"><div class="ph"><b>Telegram</b>${badge(p)}</div>${body}${caps(p)}</div>`;
}

function waCard(p) {
  if (!p) return "";
  let body = "";
  if (p.state === "connected") {
    body = `<div class="hstate">${esc(p.detail)}</div>
      <button class="btn ghost" data-ca="wa-logout">Отвязать</button>`;
  } else if (p.state === "connecting") {
    body = p.qr ? `<div class="hstate">Телефон: WhatsApp → Настройки → Связанные устройства → Привязка устройства</div>
      <img class="qr" alt="QR-код WhatsApp" src="data:image/png;base64,${p.qr}">` : `<div class="hstate">${esc(p.detail)}</div>`;
  } else {
    body = `${p.state === "error" ? `<div class="cerr">${esc(p.detail)}</div>` : ""}
      <div class="hstate">${esc(p.requirement)}</div>
      <div class="hstate">Официального API для личного WhatsApp нет: JARVIS работает через WhatsApp Web в отдельном профиле.
        WhatsApp не одобряет автоматизацию — используйте для своих обычных сообщений, не для рассылок.</div>
      <button class="btn primary" data-ca="wa-link">Подключить (QR-код)</button>`;
  }
  return `<div class="pcard"><div class="ph"><b>WhatsApp</b>${badge(p)}</div>${body}${caps(p)}</div>`;
}

function mailCard(p) {
  if (!p) return "";
  let body = "";
  if (p.state === "connected") {
    body = `<div>${esc(p.account)}</div><div class="hstate">${esc(p.detail)}</div>
      <button class="btn ghost" data-ca="mail-logout">Отключить</button>`;
  } else {
    body = `${p.state === "error" ? `<div class="cerr">${esc(p.detail)}</div>` : ""}
      <div class="hstate">${esc(p.requirement)}</div>
      <input class="input" id="mAddr" placeholder="Адрес почты" autocomplete="off">
      <input class="input" id="mPass" type="password" placeholder="Пароль приложения" autocomplete="off">
      <details><summary class="faint" style="cursor:pointer;font-size:12px">Другой почтовый сервер</summary>
        <input class="input" id="mSmtp" placeholder="SMTP-сервер" autocomplete="off" style="margin-top:6px">
        <input class="input" id="mPort" placeholder="Порт (465 или 587)" inputmode="numeric" autocomplete="off" style="margin-top:6px">
        <input class="input" id="mImap" placeholder="IMAP-сервер (для проверки «Отправленных»)" autocomplete="off" style="margin-top:6px"></details>
      <button class="btn primary" data-ca="mail-connect">Проверить и сохранить</button>`;
  }
  return `<div class="pcard"><div class="ph"><b>Почта</b>${badge(p)}</div>${body}${caps(p)}</div>`;
}

/** One service's connection card (Telegram / WhatsApp / mail) for a workspace page. */
export function accountCard(provider) {
  return { telegram: tgCard, whatsapp: waCard, email: mailCard }[provider?.id]?.(provider) || "";
}

/** Wire the buttons of account cards inside box. getData() → last comms.state; onData(d) after each change. */
export function wireAccounts(box, c, getData, onData) {
  const v = (id) => box.querySelector("#" + id)?.value?.trim() || "";
  const act = async (b, typ, args, ok) => {
    b.disabled = true;
    let d = null;
    try { d = await c.link.request(typ, args, 90000); if (ok) c.toast(ok, "ok"); }
    catch (e) { c.toast(e.message, "err"); }
    b.disabled = false;
    if (d) onData(d);
  };
  box.querySelectorAll("[data-ca]").forEach((b) => b.addEventListener("click", () => {
    const a = b.dataset.ca;
    if (a === "tg-begin") return act(b, "comms.telegram.begin", { api_id: v("tgId"), api_hash: v("tgHash"), phone: v("tgPhone") }, "Telegram отправил код");
    if (a === "tg-finish") {
      const p = getData().providers.find((x) => x.id === "telegram");
      return act(b, "comms.telegram.finish", p.login_stage === "password" ? { password: box.querySelector("#tgCode").value } : { code: v("tgCode") });
    }
    if (a === "tg-logout" && confirm("Выйти из сеанса Telegram в JARVIS?")) return act(b, "comms.telegram.logout", {}, "Telegram отключён");
    if (a === "wa-link") return act(b, "comms.whatsapp.link", {});
    if (a === "wa-logout" && confirm("Отвязать WhatsApp от JARVIS? Профиль будет удалён.")) return act(b, "comms.whatsapp.logout", {}, "WhatsApp отвязан");
    if (a === "mail-connect") return act(b, "comms.email.connect", { address: v("mAddr"), password: box.querySelector("#mPass").value,
      smtp_host: v("mSmtp"), smtp_port: v("mPort"), imap_host: v("mImap") }, "Почта подключена");
    if (a === "mail-logout" && confirm("Отключить почту?")) return act(b, "comms.email.logout", {}, "Почта отключена");
  }));
}

function drawRules() {
  const box = root?.querySelector("#crules");
  if (!box) return;
  const rules = st.data.rules || [];
  box.innerHTML = `<h2>Без подтверждения</h2>
    <div class="muted" style="font-size:13px">Правила создаются только по вашей просьбе («для сообщений Ивану в Telegram подтверждение не спрашивай») и вашей кнопкой.</div>
    <label class="toggle"><input type="checkbox" id="cAnnounce" ${st.data.announce ? "checked" : ""}> Сообщать голосом, когда отправлено сообщение по расписанию</label>
    ${rules.length ? `<table class="tbl"><tbody>${rules.map((r) => `<tr><td>${esc(r.to_name)} · ${esc(CH[r.channel])}</td>
      <td class="faint">с ${fmtWhen(r.created_at)}</td><td><button class="btn ghost" data-rrev="${esc(r.id)}">Отменить правило</button></td></tr>`).join("")}</tbody></table>`
      : '<div class="empty-note" style="margin-top:8px">Правил нет — JARVIS спрашивает перед каждой отправкой.</div>'}`;
  box.querySelectorAll("[data-rrev]").forEach((b) => b.addEventListener("click", async () => {
    st.data = await ctx.link.request("comms.rule.revoke", { id: b.dataset.rrev }); drawRules();
  }));
  box.querySelector("#cAnnounce").addEventListener("change", async (e) => {
    st.data = await ctx.link.request("comms.announce", { on: e.target.checked });
  });
}

function drawBook() {
  const box = root?.querySelector("#cbook");
  if (!box) return;
  const cs = st.data.contacts || [];
  box.innerHTML = `<h2>Адресная книга JARVIS</h2>
    <div class="muted" style="font-size:13px">Для почты и WhatsApp, когда адреса нет в переписке. Можно голосом: «запомни почту Сергея: …».</div>
    ${cs.length ? `<table class="tbl"><tbody>${cs.map((c) => `<tr><td>${esc(c.name)}</td><td>${esc([c.email, c.phone, c.telegram].filter(Boolean).join(" · "))}</td>
      <td><button class="btn ghost" data-cdel="${esc(c.id)}">Удалить</button></td></tr>`).join("")}</tbody></table>` : ""}
    <div class="field"><input class="input" id="bName" placeholder="Имя" style="min-width:140px">
      <input class="input" id="bMail" placeholder="Почта" style="min-width:160px"><input class="input" id="bPhone" placeholder="Телефон" style="min-width:140px">
      <button class="btn" id="bAdd">Добавить</button></div>`;
  box.querySelectorAll("[data-cdel]").forEach((b) => b.addEventListener("click", async () => {
    st.data = await ctx.link.request("comms.contact.delete", { id: b.dataset.cdel }); drawBook();
  }));
  box.querySelector("#bAdd").addEventListener("click", async () => {
    const g = (id) => box.querySelector("#" + id).value.trim();
    try { st.data = await ctx.link.request("comms.contact.save", { name: g("bName"), email: g("bMail"), phone: g("bPhone") }); drawBook(); }
    catch (e) { ctx.toast(e.message, "err"); }
  });
}
