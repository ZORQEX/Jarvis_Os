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
  const UL = /^\s*[-*•]\s+/, OL = /^\s*\d+[.)]\s+/, H = /^#{1,4}\s+/;
  for (const para of text.split(/\n{2,}/)) {
    // A paragraph may mix plain lines, headings and list runs: group consecutive lines of the same kind.
    let run = [], kind = null;
    const flush = () => {
      if (!run.length) return;
      if (kind === "ul") out.push("<ul>" + run.map((l) => `<li>${inline(l.replace(UL, ""))}</li>`).join("") + "</ul>");
      else if (kind === "ol") out.push("<ol>" + run.map((l) => `<li>${inline(l.replace(OL, ""))}</li>`).join("") + "</ol>");
      else out.push(`<p>${run.map(inline).join("<br>")}</p>`);
      run = [];
    };
    for (const l of para.split("\n")) {
      if (H.test(l)) { flush(); kind = null; out.push(`<p><strong>${inline(l.replace(H, ""))}</strong></p>`); continue; }
      const k = UL.test(l) ? "ul" : OL.test(l) ? "ol" : "p";
      if (k !== kind) { flush(); kind = k; }
      run.push(l);
    }
    flush();
  }
  return out.join("").replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
}

export const ICON = {
  box: '<svg viewBox="0 0 24 24"><path d="M3.5 7.5 12 3l8.5 4.5v9L12 21l-8.5-4.5z"/><path d="M3.5 7.5 12 12l8.5-4.5M12 12v9"/></svg>',
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
  globe: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.4 2.6 3.6 5.4 3.6 8.5s-1.2 5.9-3.6 8.5c-2.4-2.6-3.6-5.4-3.6-8.5s1.2-5.9 3.6-8.5z"/></svg>',
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
  home: '<svg viewBox="0 0 24 24"><path d="M3.5 11 12 4l8.5 7"/><path d="M5.5 9.5V20h13V9.5"/><path d="M10 20v-5.5h4V20"/></svg>',
  chat: '<svg viewBox="0 0 24 24"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4A1.5 1.5 0 0 1 4 14.5z"/></svg>',
  tasks: '<svg viewBox="0 0 24 24"><path d="m4 6.5 1.6 1.6L8.5 5M4 12.5l1.6 1.6 2.9-3.1M4 18.5l1.6 1.6 2.9-3.1"/><path d="M11.5 7h8.5M11.5 13h8.5M11.5 19h8.5"/></svg>',
  telegram: '<svg viewBox="0 0 24 24"><path d="M21 3 10 14"/><path d="M21 3 14.5 21l-4.5-7-7-4.5z"/></svg>',
  whatsapp: '<svg viewBox="0 0 24 24"><path d="M4.5 19.5 5.6 16A8 8 0 1 1 8.6 18.6z"/><path d="M9.2 8.6c.2-.5.5-.6.9-.6.4 0 .6.6.8 1.1.2.5-.6 1-.4 1.5.5 1 1.3 1.8 2.3 2.3.5.2 1-.6 1.5-.4.5.2 1.1.4 1.1.8 0 .4-.1.8-.6 1-1.4.6-3.5-.3-4.6-1.5-1.2-1.1-2-3.2-1.5-4.2z"/></svg>',
  settings: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4M5.5 5.5l1.7 1.7M16.8 16.8l1.7 1.7M5.5 18.5l1.7-1.7M16.8 7.2l1.7-1.7"/></svg>',
  send: '<svg viewBox="0 0 24 24"><path d="M21 3 10 14"/><path d="M21 3 14.5 21l-4.5-7-7-4.5z"/></svg>',
  history: '<svg viewBox="0 0 24 24"><path d="M3.5 12a8.5 8.5 0 1 0 2.5-6"/><path d="M3.5 4.5V9H8"/><path d="M12 7.5V12l3 2"/></svg>',
  command: '<svg viewBox="0 0 24 24"><path d="M9 6.5A2.5 2.5 0 1 0 6.5 9H9zM15 6.5A2.5 2.5 0 1 1 17.5 9H15zM9 17.5A2.5 2.5 0 1 1 6.5 15H9zM15 17.5a2.5 2.5 0 1 0 2.5-2.5H15z"/><path d="M9 9h6v6H9z"/></svg>',
  pin: '<svg viewBox="0 0 24 24"><path d="M9 4h6l-1 5 3 3v2H7v-2l3-3z"/><path d="M12 14v6"/></svg>',
  minus: '<svg viewBox="0 0 24 24"><path d="M6 12h12"/></svg>',
  eyeOff: '<svg viewBox="0 0 24 24"><path d="M3 3l18 18"/><path d="M10.6 6.1A9.7 9.7 0 0 1 12 6c5 0 8.5 4.5 9 6-.3.8-1.3 2.4-3 3.8M6.6 7.6C4.7 8.9 3.4 10.8 3 12c.5 1.5 4 6 9 6 1.6 0 3-.4 4.2-1.1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>',
  dock: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M3 9h18"/><circle cx="6.5" cy="6.5" r=".6"/></svg>',
  back: '<svg viewBox="0 0 24 24"><path d="M15 5 8 12l7 7"/></svg>',
  forward: '<svg viewBox="0 0 24 24"><path d="m9 5 7 7-7 7"/></svg>',
  refresh: '<svg viewBox="0 0 24 24"><path d="M20 11a8 8 0 0 0-14.3-4.9L4 8"/><path d="M4 3.5V8h4.5"/><path d="M4 13a8 8 0 0 0 14.3 4.9L20 16"/><path d="M20 20.5V16h-4.5"/></svg>',
  attach: '<svg viewBox="0 0 24 24"><path d="m20 11.5-7.8 7.8a5 5 0 0 1-7.1-7.1l8.2-8.2a3.4 3.4 0 0 1 4.8 4.8l-8.2 8.2a1.7 1.7 0 0 1-2.4-2.4l7.4-7.4"/></svg>',
  retry: '<svg viewBox="0 0 24 24"><path d="M4 12a8 8 0 1 0 2.4-5.7L4 8.7"/><path d="M4 3.5v5.2h5.2"/></svg>',
  sidebar: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M9 4v16"/></svg>',
  arrowRight: '<svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
  arrowUp: '<svg viewBox="0 0 24 24"><path d="M12 19V5M6 11l6-6 6 6"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  user: '<svg viewBox="0 0 24 24"><circle cx="12" cy="8.5" r="3.8"/><path d="M4.5 20c.9-3.6 3.9-5.5 7.5-5.5s6.6 1.9 7.5 5.5"/></svg>',
  wave: '<svg viewBox="0 0 24 24"><path d="M4 10v4M8 7v10M12 4v16M16 7v10M20 10v4"/></svg>',
  estop: '<svg viewBox="0 0 24 24"><path d="M8.2 3h7.6L21 8.2v7.6L15.8 21H8.2L3 15.8V8.2z"/><path d="M9 9h6v6H9z" class="fill"/></svg>',
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
const PLAN_MARK = { done: "✓", active: "●", waiting: "!", failed: "✕", skipped: "–", pending: "○" };
/** A task's plan (core/tasks-model planRows) as rows: the companion in the window and the desktop companion. */
export const planList = (rows) => rows.map((r) => `<div class="plan-row" data-mark="${r.mark}"><span class="m">${PLAN_MARK[r.mark]}</span><span title="${esc(r.label)}">${esc(r.label)}</span></div>`).join("");
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
    <span class="label">${AGENT_TOOLS.has(s.tool_id) ? agentLabel(title) : `<b>${esc(toolTitle(s.tool_id))}</b> · ${esc(title)}`}${failed ? ` — <span class="err-text">${esc(s.error)}</span>` : ""}</span>
    <span class="right">${right}</span></summary>
    <pre>${esc(JSON.stringify(details, null, 2))}</pre>
  </details>`;
}

const AGENT_TOOLS = new Set(["agent_generate", "agent_chat", "claude_code_inspect", "claude_code_change"]);
function agentLabel(title) {
  const [who, ...rest] = String(title).split(" · ");
  return `<b class="who">${esc(who)}</b>${rest.length ? " · " + esc(rest.join(" · ")) : ""}`;
}

const TITLES = { system_info: "Система", search_files: "Поиск", read_file: "Чтение", open_path: "Открытие",
  create_note: "Заметка", app_open: "Приложение", window_list: "Окна", workspace_switch: "Рабочий стол",
  volume_set: "Звук", screenshot: "Снимок экрана", notify: "Уведомление", reminder_create: "Напоминание",
  cloud_send: "Облако", agent_generate: "ИИ-шаг", agent_chat: "Ответ", claude_code_inspect: "Claude Code",
  claude_code_change: "Claude Code", open_url: "Ссылка", folder_list: "Папка", message_send: "Сообщение",
  message_deliver: "Доставка", message_cancel: "Отмена сообщения", contact_find: "Поиск контакта", contact_save: "Контакт",
  file_write: "Запись файла", folder_create: "Новая папка", file_trash: "В корзину", computer_task: "Claude Code",
  system_install: "Установка", shell_read: "Команда", trust_rule_add: "Правило", site_publish: "Публикация",
  sheet_read: "Таблица", sheet_query: "Расчёт по таблице", sheet_create: "Новый Excel", sheet_edit: "Правка Excel" };
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
    case "table": return tableCard(d.card || {});
    case "reminder": return reminderCard(d);
    case "agent_result": return agentCard(d);
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

function tableCard(c) {
  if (!c.columns?.length) return "";
  const head = c.columns.map((h) => `<th>${esc(h)}</th>`).join("");
  const num = (v) => /^[−-]?\d[\d\s\u00a0]*(,\d+)?$/.test(String(v ?? ""));     // «79 261 000», «3 781 020,78»: right-aligned, like Excel
  const rows = (c.rows || []).map((r) => `<tr>${c.columns.map((_, j) => `<td${num(r[j]) ? ' class="num"' : ""}>${esc(r[j] ?? "")}</td>`).join("")}</tr>`).join("");
  return `<div class="card"><div class="card-head">${ICON.file}<strong>${esc(c.title || "")}</strong>
    <span class="faint">${esc(c.sub || "")}</span>
    ${c.path ? `<button class="btn ghost" style="margin-left:auto" data-open="${esc(c.path)}">${ICON.open} Открыть</button>` : ""}</div>
    <div class="sheet"><table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div></div>`;
}

function reminderCard(d) {
  return `<div class="card"><div class="card-head">${ICON.clock}<strong>${esc(d.due_local)}</strong><span>${esc(d.text)}</span></div></div>`;
}

// ---------------------------------------------------------------- agency cards
const PNAME = { claude: "Claude", claude_code: "Claude Code" };
const ACT = { files: "файлы", read_file: "чтение файла", research: "поиск", analyze: "анализ", write: "текст", translate: "перевод", vision_screen: "экран",
  chat: "ответ", inspect_code: "просмотр кода", change_code: "изменение кода", compare_estimates: "сметы",
  read_mail: "почта", send_message: "отправка" };
const SSTAT = { planned: "в плане", running: "идёт", done: "готово", failed: "ошибка", blocked: "невозможно", skipped: "пропущено" };

function agentCard(d) {
  const src = (d.sources || []).filter((s) => s.url);
  const extra = [];
  if (d.tools_used?.length) extra.push(`<div class="faint" style="font-size:12px">Действия: ${esc(d.tools_used.slice(0, 12).join(" · "))}</div>`);
  if (d.git?.changed?.length) extra.push(`<div style="font-size:12.5px">Изменены файлы: ${d.git.changed.map((f) => `<code>${esc(f)}</code>`).join(", ")}</div>`);
  if (d.tests?.ran) extra.push(`<div style="font-size:12.5px">Тесты (JARVIS): ${d.tests.code === 0 ? "✓ прошли" : "✗ не прошли"}<pre class="preview-text" style="padding:6px 0 0;max-height:120px">${esc(d.tests.tail || "")}</pre></div>`);
  if (!src.length && !extra.length && !d.text) return "";
  return `<div class="card agent"><div class="card-head">${ICON.spark}<strong>${esc(PNAME[d.provider] || d.provider || "")}</strong>
      <span class="faint">${esc(d.model || "")}${d.cost_usd != null ? ` · $${Number(d.cost_usd).toFixed(3)}` : ""}${d.total_ms ? " · " + fmtMs(d.total_ms) : ""}</span></div>
    <div class="card-body" style="display:flex;flex-direction:column;gap:6px">
      ${src.length ? `<div class="srcs">${src.slice(0, 8).map((s, i) => `<button class="src" data-url="${esc(s.url)}" title="${esc(s.url)}">${i + 1}. ${esc(s.title || s.url)}</button>`).join("")}</div>` : ""}
      ${extra.join("")}
      ${d.text ? `<details><summary class="faint" style="cursor:pointer;font-size:12px">Результат шага</summary><div class="msg-ai" style="margin-top:6px">${md(d.text)}</div></details>` : ""}
    </div></div>`;
}

export function decisionCard(dec, dev = false) {
  if (!dec || !dec.steps) return "";
  const interesting = dev || dec.steps.length > 1 || dec.steps.some((s) => ["blocked", "failed"].includes(s.status)) || dec.override;
  if (!interesting) return "";
  const rows = dec.steps.map((s, i) => {
    const who = s.executor_kind === "tool" ? "JARVIS" : PNAME[s.executor] || "—";
    return `<div class="dstep ${esc(s.status)}"><span class="n">${i + 1}</span><b>${esc(who)}</b><span>${esc(ACT[s.action] || s.action)}</span>
      <span class="pill ${s.status === "done" ? "completed" : s.status === "running" ? "running" : ["failed", "blocked"].includes(s.status) ? "failed" : ""}">${esc(SSTAT[s.status] || s.status)}</span>
      ${s.approval === "required" ? `<span class="pill waiting_for_approval">нужно подтверждение</span>` : ""}
      ${dev || ["blocked", "failed"].includes(s.status) ? `<div class="why">${esc(s.reason || "")}</div>` : ""}
      ${dev && s.candidates?.length ? `<div class="why faint">кандидаты: ${s.candidates.filter((c) => c.provider).map((c) => `${esc(PNAME[c.provider] || c.provider)} ${c.eligible ? "✓ " + (c.score ?? "") : "✗ " + esc(c.why)}`).join(" · ")}</div>` : ""}
    </div>`;
  }).join("");
  const meta = dev ? `<div class="faint" style="font-size:11.5px;padding:0 14px 10px">план: ${esc(dec.planner)} · режим: ${esc(dec.mode)} · риск: ${esc(dec.risk)} · данные: ${esc(dec.sensitivity)}${dec.override ? " · выбран вами: " + esc(PNAME[dec.override] || dec.override) : ""}${dec.verification?.length ? " · проверки: " + esc(dec.verification.join(", ")) : ""}</div>` : "";
  return `<div class="card decision-card"><div class="card-head">${ICON.bolt}<strong>План</strong><span class="faint">${esc(dec.objective.slice(0, 90))}</span></div>
    <div class="dsteps">${rows}</div>${meta}
    ${(dec.notes || []).length ? `<div class="faint" style="font-size:12px;padding:0 14px 10px">${dec.notes.map(esc).join("<br>")}</div>` : ""}</div>`;
}

// ---------------------------------------------------------------- approval card
const CH_RU = { telegram: "Telegram", whatsapp: "WhatsApp", email: "Почта" };

function messagePacket(a, open) {
  const x = a.args || {};
  const to = (x.deliveries || []).map((d) => `${esc(CH_RU[d.channel] || d.channel)} → <b>${esc(d.to_name)}</b>${d.to_address ? ` <span class="faint">${esc(d.to_address)}</span>` : ""}`).join("<br>");
  const when = x.due_at ? new Date(x.due_at * 1000).toLocaleString("ru-RU", { weekday: "short", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }) : "сразу после подтверждения";
  const email = (x.deliveries || []).some((d) => d.channel === "email");
  return `<dl class="msgcard"><dt>Кому</dt><dd>${to}</dd><dt>Когда</dt><dd>${esc(when)}</dd>
    ${x.subject ? `<dt>Тема</dt><dd>${esc(x.subject)}</dd>` : ""}<dt>Текст</dt><dd class="body">${esc(x.body)}</dd></dl>
    ${open ? `<details class="revise" style="margin:0 14px 8px"><summary class="faint" style="cursor:pointer;font-size:12.5px">Изменить текст</summary>
      ${email ? `<input class="input" data-rev-subject value="${esc(x.subject || "")}" aria-label="Тема письма">` : ""}
      <textarea data-rev-body rows="3" aria-label="Текст сообщения">${esc(x.body)}</textarea>
      <button class="btn" data-revise="${esc(a.id)}" style="margin-top:6px">Сохранить и спросить снова</button></details>` : ""}`;
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
    ${a.tool_id === "message_send" ? messagePacket(a, open) : ""}
    ${open ? editable.map((k) => `<textarea data-edit="${esc(k)}" aria-label="Изменить: ${esc(k)}">${esc(a.args[k])}</textarea>`).join("") : ""}
    ${open ? `<div class="actions">
      <button class="btn warn" data-approve="${esc(a.id)}">${ICON.check} Разрешить</button>
      <button class="btn ghost" data-deny="${esc(a.id)}">Отклонить</button>
      ${a.tool_id === "trust_rule_add" ? "" : `<span class="faint voice-hint">${ICON.mic} или скажите «да» / «нет»</span>`}
      <details style="flex-basis:100%"><summary class="faint" style="cursor:pointer;font-size:12px">Точные параметры</summary>
        <pre style="font-size:11.5px;color:var(--text-2);white-space:pre-wrap">${esc(JSON.stringify(a.args, null, 2))}</pre></details>
      ${s.session_grant_allowed ? `<label class="remember"><input type="checkbox" data-remember> Не спрашивать до перезапуска</label>` : ""}
    </div>` : `<div class="actions faint" style="font-size:12px">${fmtWhen(a.decided_at || a.created_at)}</div>`}
  </div>`;
}

/** 12 345,50 € — narrow no-break spaces between thousands (JARVIS's rule for money). */
export function eur(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return esc(String(v ?? ""));
  const [i, f] = Math.abs(n).toFixed(2).split(".");
  return `${n < 0 ? "−" : ""}${i.replace(/\B(?=(\d{3})+(?!\d))/g, " ")},${f} €`;
}

/** An orders change waiting for the owner («Заказы» module proposal): what will change, money before / after, and the
 * buttons. Nothing is saved before «Подтвердить» (the click goes to the core, never to the model). */
export function orderCard(f, messageId) {
  const p = f?.card;
  const q = f?.question;
  const parts = [];
  if (p && p.id) {
    const st = p.status || "pending";
    const head = { pending: "Изменение в заказах — ждёт вашего решения", committed: "Сохранено в заказах",
      cancelled: "Отменено или заменено новым предложением", missing: "Предложение не найдено" }[st]
      || esc(st);
    const diff = (p.diff || []).length ? `<table class="ord-diff"><thead><tr><th></th><th>Было</th><th>Станет</th><th>Разница</th></tr></thead><tbody>${
      p.diff.map((r) => { const d = Number(r.after) - Number(r.before);
        return `<tr><td>${esc(r.label || "")}${r.season ? ` <span class="faint">${esc(r.season)}</span>` : ""}</td><td>${eur(r.before)}</td>
          <td><strong>${eur(r.after)}</strong></td><td class="${d > 0 ? "up" : d < 0 ? "down" : ""}">${d > 0 ? "+" : ""}${eur(d)}</td></tr>`; }).join("")}</tbody></table>` : "";
    const items = [...(p.will_do || []).map((l) => `<li>${esc(l)}</li>`),
      ...(p.documents || []).map((d) => `<li>Excel + PDF для ${esc(d.customer)} (${esc(d.season)})</li>`)].join("");
    parts.push(`<div class="card approval ord-card ${st === "pending" ? "" : "closed"}" data-order="${esc(p.id)}">
      <div class="card-head">${ICON.shield}<strong>${head}</strong></div>
      ${p.summary ? `<div class="what">${esc(p.summary)}</div>` : ""}
      ${items ? `<ul class="ord-will">${items}</ul>` : ""}${diff}
      ${st === "pending" ? `<div class="actions">
        <button class="btn warn" data-order-decide="commit" data-proposal="${esc(p.id)}" data-mid="${esc(messageId || "")}">${ICON.check} Подтвердить</button>
        <button class="btn ghost" data-order-decide="cancel" data-proposal="${esc(p.id)}" data-mid="${esc(messageId || "")}">Отменить</button>
        <span class="faint voice-hint">${p.button_only ? "только кнопкой" : `${ICON.mic} или скажите «да, сохрани»`}</span></div>` : ""}
    </div>`);
  }
  if ((f?.figures || []).length) {
    parts.push(`<dl class="ord-figures">${f.figures.map((x) => `<div><dt>${esc(x.label)}</dt><dd>${eur(x.value)}</dd></div>`).join("")}</dl>`);
  }
  if ((f?.files || []).length) {
    parts.push(`<div class="ord-files">${f.files.map((x) => `<button class="btn sm" data-url="${esc(x.url)}" title="Скачать">${ICON.file || ""}
      ${esc(x.format || "Файл")}${x.customer ? " · " + esc(x.customer) : ""} — ${esc(x.name)}</button>`).join("")}</div>`);
  }
  if (q && (q.options || []).length) {
    parts.push(`<div class="suggestions ord-options">${q.options.slice(0, 6).map((o) =>
      `<button class="suggestion" data-suggest="${esc(o)}">${esc(o)}</button>`).join("")}</div>`);
  }
  return parts.join("");
}
