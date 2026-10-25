// Workspaces of JARVIS WorkOS: each is a context with its own tools. A tool's status is computed from real
// state (the core link, messenger connections) — «ready» only when the core can really do it right now.
// Adding a workspace (Slack, Calendar, CRM…) = one entry here + one module; the shell, companion and palette
// pick it up without changes.

/** @typedef {{id:string, label:string, needs?:"core"|"connection"|"contacts", missing?:string}} ToolDef */
/** @typedef {{id:string, title:string, icon:string, hotkey?:string, context:string, channel?:string,
 *   module?:string, tools:ToolDef[], hidden?:boolean}} Workspace */

const channelTools = (ch, title, extra = []) => [
  { id: `${ch}.compose`, label: "Написать сообщение через JARVIS", needs: "core" },
  { id: `${ch}.send`, label: "Отправить — после вашего подтверждения", needs: "connection" },
  { id: `${ch}.schedule`, label: "Отправить по расписанию", needs: "connection" },
  { id: `${ch}.search`, label: "Найти получателя", needs: "connection" },
  ...extra,
  { id: `${ch}.read`, label: `Читать переписку ${title}`, missing: "Ядро пока не читает чужие сообщения — это отдельное решение о приватности" },
];

/** @type {Workspace[]} */
export const WORKSPACES = [
  { id: "chat", title: "Беседа", icon: "chat", hotkey: "1", context: "Разговор с JARVIS",
    tools: [{ id: "jarvis.chat", label: "Беседа с потоковыми ответами", needs: "core" }] },
  { id: "browser", title: "Браузер", icon: "globe", hotkey: "2", context: "Сайты и поиск", module: "browser",
    tools: [
      { id: "browser.open", label: "Открыть ссылку в вашем браузере", needs: "core" },
      { id: "browser.search", label: "Искать в интернете", needs: "core" },
      { id: "browser.agent", label: "Поручить работу с сайтом (отдельный браузер без ваших входов)", needs: "core" },
      { id: "browser.navigate", label: "Назад, вперёд, обновить в окне браузера", missing: "Окно вашего браузера JARVIS не управляет — нужен адаптер" },
    ] },
  { id: "telegram", title: "Telegram", icon: "telegram", hotkey: "3", context: "Telegram", channel: "telegram", module: "communication",
    tools: channelTools("telegram", "Telegram", [{ id: "telegram.open", label: "Открыть приложение Telegram", needs: "core" }]) },
  { id: "whatsapp", title: "WhatsApp", icon: "whatsapp", hotkey: "4", context: "WhatsApp", channel: "whatsapp", module: "communication",
    tools: channelTools("whatsapp", "WhatsApp") },
  { id: "mail", title: "Почта", icon: "mail", hotkey: "5", context: "Почта", channel: "email", module: "communication",
    tools: channelTools("email", "почты", [{ id: "email.gmail", label: "Gmail API (только чтение)", missing: "Запланировано: нужен OAuth-клиент Google" }]) },
  { id: "files", title: "Файлы", icon: "folder", hotkey: "6", context: "Файлы", module: "files",
    tools: [{ id: "files.list", label: "Просмотр папок", needs: "core" }, { id: "files.search", label: "Поиск файлов", needs: "core" },
      { id: "files.open", label: "Открыть в приложении", needs: "core" }, { id: "files.ask", label: "Спросить JARVIS о файле", needs: "core" }] },
  { id: "orders", title: "Заказы", icon: "box", hotkey: "7", context: "Заказы: фабрики, клиенты, предоплаты", module: "orders",
    tools: [
      { id: "orders.import", label: "Загрузить файл фабрики (Excel / PDF) — сохраняется после «Подтвердить»", needs: "core" },
      { id: "orders.markup", label: "Наценки клиентов, предоплаты фабрике и клиентам, ваш взнос", needs: "core" },
      { id: "orders.docs", label: "Excel и PDF для каждого клиента — только его позиции", needs: "core" },
      { id: "orders.voice", label: "Голосом через JARVIS: «Джарвис, поставь IDS 35 процентов»", needs: "core" },
    ] },
  { id: "abilities", title: "Возможности", icon: "bolt", hotkey: "8", context: "OpenClaw, Hermes, навыки и агенты", module: "abilities",
    tools: [
      { id: "hub.find", label: "Найти, что умеет JARVIS и его агенты (каталог навыков, плагинов, каналов)", needs: "core" },
      { id: "hub.specialist", label: "Поручить долгую работу Hermes — рискованные шаги с подтверждением", needs: "core" },
      { id: "hub.skills", label: "Навыки ClawHub: проверка, установка, обновление с откатом", needs: "core" },
      { id: "hub.channels", label: "Каналы через OpenClaw: Telegram-бот, WhatsApp, Slack, Discord, Signal", needs: "core" },
      { id: "coding.agents", label: "Claude Code и Codex на компьютере: что делают, разрешения из окна", needs: "core" },
    ] },
  // What went wrong, kept for fixing later (JARVIS's own records); «Исправить» = self-improvement after your «да».
  { id: "errors", title: "Ошибки", icon: "alert", hotkey: "9", context: "Что пошло не так — список для исправления", module: "errors",
    tools: [] },
];

/** Reachable from the palette and links, not shown in the sidebar list. */
export const EXTRA = [
  { id: "settings", title: "Настройки", icon: "settings", context: "Настройки", tools: [] },
  { id: "comms", title: "Сообщения", icon: "send", context: "Очередь сообщений, правила, контакты", tools: [] },
  { id: "history", title: "История действий", icon: "history", context: "Что JARVIS изменил на компьютере", tools: [] },
];

export const ALL = [...WORKSPACES, ...EXTRA];
export const byId = (id) => ALL.find((w) => w.id === id) || null;

/** Messenger provider (from comms.state / comms.providers) → connection label. */
export function connectionOf(provider) {
  if (!provider) return { state: "unknown", label: "Состояние неизвестно" };
  const map = { connected: "Подключено", not_connected: "Не подключено", connecting: "Подключение…", error: "Ошибка подключения" };
  return { state: provider.state, label: map[provider.state] || provider.state, detail: provider.detail || provider.requirement || "",
    account: provider.account || "" };
}

/**
 * Real status of every tool of a workspace.
 * @param {Workspace} ws
 * @param {{connected:boolean, providers?:Record<string, any>}} env
 * @returns {{id:string, label:string, state:"ready"|"needs_connection"|"offline"|"missing", why:string}[]}
 */
export function toolStatus(ws, env) {
  const prov = ws.channel ? env.providers?.[ws.channel] : null;
  return ws.tools.map((t) => {
    if (t.missing) return { id: t.id, label: t.label, state: "missing", why: t.missing };
    if (!env.connected) return { id: t.id, label: t.label, state: "offline", why: "Нет связи с ядром JARVIS" };
    if (t.needs === "connection" && prov?.state !== "connected") {
      return { id: t.id, label: t.label, state: "needs_connection", why: `${ws.title}: ${connectionOf(prov).label.toLowerCase()}` };
    }
    return { id: t.id, label: t.label, state: "ready", why: "" };
  });
}

/** A small dot for the sidebar: only workspaces with a connection have one, and only from real state. */
export function navBadge(ws, env) {
  if (!ws.channel) return null;
  const p = env.providers?.[ws.channel];
  if (!p) return null;
  if (!env.connected) return "off";            // no link to the core: the last known state is not shown as current
  return { connected: "ok", connecting: "warn", error: "err" }[p.state] || "off";
}
