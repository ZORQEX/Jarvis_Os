// Small, safe renderers. All dynamic text goes through esc(); markdown is a
// tiny subset rendered after escaping, so model output can never inject HTML.

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function md(src) {
  const blocks = [];
  let text = esc(src).replace(/```([\s\S]*?)```/g, (_, code) => {
    blocks.push(`<pre><code>${code.replace(/^\w*\n/, "")}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  const inline = (s) => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])_([^_]+)_(?=$|[\s).,!?:;])/g, "$1<em>$2</em>")
    .replace(/(^|[\s(])\*([^*]+)\*(?=$|[\s).,!?:;])/g, "$1<em>$2</em>");
  const out = [];
  for (const para of text.split(/\n{2,}/)) {
    const lines = para.split("\n");
    if (lines.every((l) => /^\s*[-*•]\s+/.test(l))) {
      out.push("<ul>" + lines.map((l) => `<li>${inline(l.replace(/^\s*[-*•]\s+/, ""))}</li>`).join("") + "</ul>");
    } else if (lines.every((l) => /^\s*\d+[.)]\s+/.test(l))) {
      out.push("<ol>" + lines.map((l) => `<li>${inline(l.replace(/^\s*\d+[.)]\s+/, ""))}</li>`).join("") + "</ol>");
    } else if (/^#{1,3}\s/.test(lines[0])) {
      out.push(`<p><strong>${inline(lines[0].replace(/^#+\s/, ""))}</strong></p>` +
        (lines.length > 1 ? `<p>${lines.slice(1).map(inline).join("<br>")}</p>` : ""));
    } else {
      out.push(`<p>${lines.map(inline).join("<br>")}</p>`);
    }
  }
  return out.join("").replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
}

export const ICON = {
  check: '<svg viewBox="0 0 24 24"><path d="m5 12.5 4.2 4.2L19 7"/></svg>',
  x: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  alert: '<svg viewBox="0 0 24 24"><path d="M12 4 2.8 19.5h18.4z"/><path d="M12 10v4M12 17h0"/></svg>',
  pause: '<svg viewBox="0 0 24 24"><path d="M9 6v12M15 6v12"/></svg>',
  play: '<svg viewBox="0 0 24 24"><path d="M8 5.5v13l10-6.5z"/></svg>',
  stop: '<svg viewBox="0 0 24 24"><rect x="6.5" y="6.5" width="11" height="11" rx="2"/></svg>',
  dot: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/></svg>',
  file: '<svg viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  window: '<svg viewBox="0 0 24 24"><rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="M3 9h18"/></svg>',
  mic: '<svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/></svg>',
  micOff: '<svg viewBox="0 0 24 24"><path d="M15 10V6a3 3 0 0 0-5.7-1.3M9 9v2a3 3 0 0 0 4.6 2.5M5.5 11a6.5 6.5 0 0 0 10.4 5.2M18.5 11a6.5 6.5 0 0 1-.6 2.6M12 17.5V21M4 4l16 16"/></svg>',
  cloud: '<svg viewBox="0 0 24 24"><path d="M7 18h10.5a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.6 9.2 4.5 4.5 0 0 0 7 18z"/></svg>',
  lock: '<svg viewBox="0 0 24 24"><rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5"/></svg>',
  shield: '<svg viewBox="0 0 24 24"><path d="M12 3 5 6v5.5c0 4.5 3 8 7 9.5 4-1.5 7-5 7-9.5V6z"/></svg>',
  bolt: '<svg viewBox="0 0 24 24"><path d="M13 3 5 13.5h6L10 21l8-10.5h-6z"/></svg>',
  clock: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
  keyboard: '<svg viewBox="0 0 24 24"><rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h0M10 10h0M14 10h0M18 10h0M7 14h10"/></svg>',
  image: '<svg viewBox="0 0 24 24"><rect x="3" y="4.5" width="18" height="15" rx="2"/><circle cx="8.5" cy="9.5" r="1.5"/><path d="m21 16-5-5-9 8.5"/></svg>',
  note: '<svg viewBox="0 0 24 24"><path d="M5 4h14v12l-4 4H5z"/><path d="M15 20v-4h4M8.5 9h7M8.5 12.5h4"/></svg>',
  volume: '<svg viewBox="0 0 24 24"><path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/></svg>',
  cpu: '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9.5 2.5V6M14.5 2.5V6M9.5 18v3.5M14.5 18v3.5M2.5 9.5H6M2.5 14.5H6M18 9.5h3.5M18 14.5h3.5"/></svg>',
  search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/></svg>',
  open: '<svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v4.5A1.5 1.5 0 0 1 16.5 20h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/></svg>',
  mail: '<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 6.5 8.5 6.5 8.5-6.5"/></svg>',
  spark: '<svg viewBox="0 0 24 24"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6"/></svg>',
};

const TOOL_ICON = { system_info: "cpu", search_files: "search", read_file: "file", open_path: "open", create_note: "note",
  app_open: "window", window_list: "window", workspace_switch: "window", volume_set: "volume", screenshot: "image",
  notify: "alert", reminder_create: "clock", cloud_send: "cloud" };

export const STATE_RU = {
  created: "создана", queued: "в очереди", planning: "планирую", running: "выполняется",
  waiting_for_approval: "ждёт решения", paused: "пауза", verifying: "проверяю", completed: "готово",
  failed: "ошибка", cancelled: "отменена", blocked: "прервана", retrying: "повтор", waiting_for_dependency: "ждёт данных",
};
const STEP_RU = { planned: "в плане", running: "выполняю", verifying: "проверяю", succeeded: "готово", failed: "ошибка",
  invalid: "неверные данные", denied: "не выполнено", cancelled: "отменено", awaiting_approval: "ждёт решения",
  interrupted: "прервано", resumed: "продолжено" };
const VERIF_RU = { verified: "проверено", unverified: "не подтверждено", failed: "проверка не прошла" };

export const fmtBytes = (n) => {
  if (n == null) return "";
  const u = ["Б", "КБ", "МБ", "ГБ", "ТБ"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0).replace(".", ",")} ${u[i]}`;
};
export const fmtMs = (ms) => ms == null ? "" : ms < 1000 ? `${Math.round(ms)} мс` : `${(ms / 1000).toFixed(1).replace(".", ",")} с`;
export const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
export const fmtWhen = (ts) => {
  const d = new Date(ts * 1000), now = new Date();
  return d.toDateString() === now.toDateString() ? fmtTime(ts)
    : d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" }) + " " + fmtTime(ts);
};

export function stepRow(s, progress) {
  const state = s.state;
  const busy = state === "running" || state === "verifying";
  const ico = busy ? '<span class="spinner"></span>'
    : state === "succeeded" ? ICON.check
    : ["failed", "invalid"].includes(state) ? ICON.x
    : ["denied", "cancelled", "interrupted", "awaiting_approval"].includes(state) ? ICON.alert : ICON[TOOL_ICON[s.tool_id] || "dot"];
  const title = s.title || s.result?.summary || s.tool_id;
  const v = s.verification;
  const right = [
    v ? `<span class="vbadge ${esc(v.status)}" title="${esc(v.detail)}">${esc(VERIF_RU[v.status] || v.status)}</span>` : "",
    !v ? `<span>${esc(progress || STEP_RU[state] || state)}</span>` : "",
    s.duration_ms != null ? `<span>${fmtMs(s.duration_ms)}</span>` : "",
  ].join("");
  const details = {
    инструмент: s.tool_id, риск: s.risk, почему: s.why, параметры: s.args,
    ...(s.error ? { ошибка: s.error } : {}), ...(v ? { проверка: v.detail } : {}),
  };
  const failed = s.error && !busy && state !== "succeeded";
  return `<details class="step" data-state="${esc(state)}">
    <summary><span class="ico">${ico}</span>
    <span class="label"><b>${esc(toolTitle(s.tool_id))}</b> · ${esc(title)}${failed ? ` — <span class="err-text">${esc(s.error)}</span>` : ""}</span>
    <span class="right">${right}</span></summary>
    <pre>${esc(JSON.stringify(details, null, 2))}</pre>
  </details>`;
}

const TITLES = { system_info: "Система", search_files: "Поиск", read_file: "Чтение", open_path: "Открытие",
  create_note: "Заметка", app_open: "Приложение", window_list: "Окна", workspace_switch: "Рабочий стол",
  volume_set: "Звук", screenshot: "Снимок экрана", notify: "Уведомление", reminder_create: "Напоминание",
  cloud_send: "Облако", flora_inbox: "Почта «Флоры»", flora_compare: "Сравнение смет", flora_draft: "Черновик",
  flora_send: "Отправка (демо)" };
export const toolTitle = (id) => TITLES[id] || id;

// ---------------------------------------------------------------- result cards
export function resultCard(step) {
  const r = step.result;
  if (!r || step.state !== "succeeded") return "";
  const d = r.data || {};
  switch (r.card) {
    case "system": return systemCard(d);
    case "files": return filesCard(d);
    case "windows": return windowsCard(d);
    case "screenshot": return shotCard(d);
    case "note": return noteCard(d);
    case "file_preview": return previewCard(d);
    case "reminder": return reminderCard(d);
    case "flora_inbox": return floraInboxCard(d);
    case "flora_compare": return floraCompareCard(d);
    case "flora_draft": return floraDraftCard(d);
    case "flora_sent": return floraSentCard(d);
    default: return "";
  }
}

function systemCard(d) {
  const mem = d.memory, disk = d.disk, cpu = d.cpu, bat = d.battery;
  const meter = (p) => `<div class="meter"><i class="${p > 85 ? "hot" : ""}" style="width:${Math.min(100, p)}%"></i></div>`;
  return `<div class="card"><div class="card-head">${ICON.cpu}<strong>${esc(d.hostname)}</strong>
    <span class="faint">${esc(d.os)}${d.omarchy ? " · Omarchy " + esc(d.omarchy) : ""}</span></div>
    <div class="stats">
      <div class="stat"><div class="k">Процессор</div><div class="v">${Math.round(cpu.load_pct)}%</div>
        <div class="sub">${cpu.temp_c != null ? Math.round(cpu.temp_c) + " °C · " : ""}${cpu.threads} потоков</div>${meter(cpu.load_pct)}</div>
      <div class="stat"><div class="k">Память</div><div class="v">${Math.round(mem.used_pct)}%</div>
        <div class="sub">свободно ${fmtBytes(mem.available)}</div>${meter(mem.used_pct)}</div>
      <div class="stat"><div class="k">Диск</div><div class="v">${fmtBytes(disk.free)}</div>
        <div class="sub">свободно из ${fmtBytes(disk.total)}</div>${meter(disk.used_pct)}</div>
      ${bat ? `<div class="stat"><div class="k">Батарея</div><div class="v">${bat.percent}%</div>
        <div class="sub">${esc({ Charging: "заряжается", Discharging: "от батареи", Full: "заряжена", "Not charging": "не заряжается" }[bat.status] || bat.status)}</div>${meter(bat.percent)}</div>` : ""}
    </div></div>`;
}

function filesCard(d) {
  if (!d.results?.length) return "";
  const rows = d.results.map((h, i) => `<button class="row" data-open="${esc(h.path)}" title="${esc(h.path)}">
      <span class="n">${i + 1}</span>
      <span class="t"><div>${h.dir ? "📁 " : ""}${esc(h.name)}</div><div class="p">${esc(shortPath(h.path))}</div></span>
      <span class="r">${h.dir ? "" : fmtBytes(h.size) + " · "}${fmtWhen(h.mtime)}</span></button>`).join("");
  return `<div class="card"><div class="card-head">${ICON.search}<strong>«${esc(d.query)}»</strong>
    <span class="faint">${d.total} найдено${d.truncated ? " · поиск ограничен" : ""}</span></div><div class="rows">${rows}</div></div>`;
}

export const shortPath = (p) => String(p).replace(/^\/home\/[^/]+/, "~");

function windowsCard(d) {
  const rows = (d.windows || []).map((w) => `<div class="row"><span class="n">${w.workspace}</span>
    <span class="t"><div>${esc(w.title || w.class)}</div><div class="p">${esc(w.class)}</div></span><span class="r"></span></div>`).join("");
  return `<div class="card"><div class="card-head">${ICON.window}<strong>Окна</strong><span class="faint">номер — рабочий стол</span></div>
    <div class="rows">${rows || '<div class="row"><span></span><span class="t faint">Окон нет</span></div>'}</div></div>`;
}

function shotCard(d) {
  return `<div class="card"><img class="shot" alt="Снимок экрана" src="/artifact?path=${encodeURIComponent(d.path)}">
    <div class="card-head">${ICON.image}<strong>${esc(d.path.split("/").pop())}</strong>
    <span class="faint">${d.width}×${d.height} · ${fmtBytes(d.bytes)}</span>
    <button class="btn ghost" style="margin-left:auto" data-open="${esc(d.path)}">${ICON.open} Открыть</button></div></div>`;
}

function noteCard(d) {
  return `<div class="card"><div class="row" style="grid-template-columns:26px minmax(0,1fr) auto">${ICON.note}
    <span class="t"><div>${esc(d.name.replace(/\.md$/, ""))}</div><div class="p">${esc(shortPath(d.path.replace(/\/[^/]+$/, "")))}</div></span>
    <button class="btn ghost" data-open="${esc(d.path)}">${ICON.open} Открыть</button></div></div>`;
}

function previewCard(d) {
  return `<div class="card"><div class="card-head">${ICON.file}<strong>${esc(d.name)}</strong>
    <span class="faint">${fmtBytes(d.size)}${d.clipped ? " · показано начало" : ""}</span></div>
    <pre class="preview-text">${esc((d.text || "").slice(0, 4000))}</pre></div>`;
}

function reminderCard(d) {
  return `<div class="card"><div class="card-head">${ICON.clock}<strong>${esc(d.due_local)}</strong><span>${esc(d.text)}</span></div></div>`;
}

// ---------------------------------------------------------------- Flora cards
const TYPE_RU = { DECISION_REQUIRED: "решение", QUESTION: "вопрос", PROMISE: "обещание", FYI: "к сведению" };
export const typeBadge = (t) => `<span class="tbadge ${esc(t)}">${esc(TYPE_RU[t] || t)}</span>`;
export const ruDate = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("ru-RU", { weekday: "short", day: "numeric", month: "long" });
export const rub = (v, cur = "RUB") => v == null ? "—" : Math.round(v).toLocaleString("ru-RU") + (cur === "RUB" ? " ₽" : " €");

function floraInboxCard(d) {
  const a = d.analysis;
  if (!a) return "";
  const rows = a.items.slice(0, 6).map((i) => `<button class="row" data-nav="flora" data-flora-item="${esc(i.id)}">
    ${typeBadge(i.type)}<span class="t"><div>${esc(i.subject)}</div><div class="p">${esc(i.from.name)}</div></span>
    <span class="r">${i.deadline ? "до " + esc(ruDate(i.deadline)) : ""}</span></button>`).join("");
  return `<div class="card"><div class="card-head">${ICON.mail}<strong>Флора · входящие</strong><span class="demo-badge">демо</span>
    <span class="faint" style="margin-left:auto">${a.items.length} по проекту · ${a.excluded.length} скрыто</span></div>
    <div class="rows flora-rows">${rows}</div>
    <div class="card-foot"><button class="btn" data-nav="flora">Открыть проект</button></div></div>`;
}

function floraCompareCard(d) {
  const c = d.comparison;
  const an = c.anomalies.map((x) => `<div class="callout warn">${ICON.alert}<span>${esc(x.text)} — ячейка <code>${esc(x.cell)}</code>: <code>${esc(x.formula || "")}</code></span></div>`).join("");
  return `<div class="card"><div class="card-head">${ICON.file}<strong>Сметы «Флоры»</strong><span class="demo-badge">демо</span></div>
    <div class="stats"><div class="stat"><div class="k">Согласовано (v1)</div><div class="v">${rub(c.base_total)}</div></div>
      <div class="stat"><div class="k">Предложено (v2)</div><div class="v">${rub(c.new_total)}</div></div>
      <div class="stat"><div class="k">Разница</div><div class="v" style="color:var(--warn)">+${rub(c.delta)}</div><div class="sub">по строкам, с НДС</div></div></div>
    <div style="padding:0 14px 14px;display:flex;flex-direction:column;gap:8px">${an}
    <button class="btn" style="align-self:flex-start" data-nav="flora" data-flora-tab="quotes">Подробное сравнение</button></div></div>`;
}

function floraDraftCard(d) {
  const dr = d.draft;
  return `<div class="card"><div class="card-head">${ICON.mail}<strong>Черновик</strong><span class="faint">кому: ${esc(dr.to_name)}</span>
    <span class="pill" style="margin-left:auto">не отправлен</span></div>
    <pre class="preview-text" style="font-family:var(--font);font-size:13.5px">${esc(dr.body)}</pre>
    <div class="card-foot"><button class="btn" data-nav="flora" data-flora-tab="reply">Открыть и править</button></div></div>`;
}

function floraSentCard(d) {
  return `<div class="card"><div class="card-head">${ICON.check}<strong>${d.duplicate ? "Уже было отправлено" : "Отправлено"} (демо)</strong>
    <span class="faint">${esc(d.to)} · ${esc(d.provider_message_id)}</span></div></div>`;
}

// ---------------------------------------------------------------- approval card
function packet(p) {
  return `<dl class="packet">
    <dt>Канал</dt><dd>Демо-почта (письмо не покидает компьютер)</dd>
    <dt>От</dt><dd>client@demo.example</dd>
    <dt>Кому</dt><dd>${esc(p.to)}</dd>
    <dt>Тема</dt><dd>${esc(p.subject)}</dd>
    <dt>Вложения</dt><dd>нет</dd>
    <dt>Проект</dt><dd>Флора · версия черновика ${esc(p.version)}</dd>
    <dt>Текст</dt><dd><pre>${esc(p.body)}</pre></dd>
  </dl>`;
}

export function approvalCard(a) {
  const s = a.summary || {};
  const open = a.status === "pending";
  if (!open) {
    const ok = a.status === "approved";
    const label = { approved: "Разрешено вами", denied: "Отклонено", expired: "Не подтверждено вовремя" }[a.status] || a.status;
    const edited = ok && JSON.stringify(a.decided_args) !== JSON.stringify(a.args) ? " · с вашими правками" : "";
    return `<div class="decision ${ok ? "ok" : "no"}">${ok ? ICON.shield : ICON.x}<span>${esc(label)}${edited}</span>
      <span class="faint">· ${esc(s.action || s.title)} · ${fmtWhen(a.decided_at || a.created_at)}</span></div>`;
  }
  const editable = (s.editable || []).filter((k) => typeof a.args?.[k] === "string");
  const statusRu = { approved: "Разрешено", denied: "Отклонено", expired: "Срок истёк" }[a.status];
  const risk = { low: "низкий риск", moderate: "средний риск", high: "высокий риск" }[a.risk] || a.risk;
  const effects = { local: "изменит данные на этом компьютере", external: "отправит данные за пределы компьютера",
    none: "только чтение" }[s.side_effects] || "";
  return `<div class="card approval ${open ? "" : "closed"}" data-approval="${esc(a.id)}">
    <div class="card-head">${ICON.shield}<strong>${open ? "Нужно ваше решение" : esc(statusRu)}</strong>
      <span class="risk ${esc(a.risk)}">${esc(risk)}</span></div>
    <div class="what">${esc(s.action || s.title)}</div>
    <div class="why">${esc(s.impact || [s.reason, effects].filter(Boolean).join(" · "))}</div>
    ${a.tool_id === "flora_send" ? packet(a.args) : ""}
    ${open ? editable.map((k) => `<textarea data-edit="${esc(k)}" aria-label="Изменить: ${esc(k)}">${esc(a.args[k])}</textarea>`).join("") : ""}
    ${open ? `<div class="actions">
      <button class="btn warn" data-approve="${esc(a.id)}">${ICON.check} Разрешить</button>
      <button class="btn ghost" data-deny="${esc(a.id)}">Отклонить</button>
      <details style="flex-basis:100%"><summary class="faint" style="cursor:pointer;font-size:12px">Точные параметры</summary>
        <pre style="font-size:11.5px;color:var(--text-2);white-space:pre-wrap">${esc(JSON.stringify(a.args, null, 2))}</pre></details>
      ${s.session_grant_allowed ? `<label class="remember"><input type="checkbox" data-remember> Не спрашивать до перезапуска</label>` : ""}
    </div>` : `<div class="actions faint" style="font-size:12px">${fmtWhen(a.decided_at || a.created_at)}</div>`}
  </div>`;
}
