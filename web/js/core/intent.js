// Window-level intent router for typed commands and the command palette. It handles only what the window itself
// does — switching workspaces and the global commands — instantly, offline, with no model call. Everything else
// goes to the core, whose own local router (jarvis/router.py) handles system commands without AI and sends only
// open-ended requests to Claude. Voice goes straight to the core; the core sends ui.navigate back.
//
// Keep the navigation grammar in step with router.ui_view() (tests on both sides use the same phrases).

const norm = (s) => String(s || "").trim()
  .replace(/^\s*(?:hey|эй|ok|окей)?\s*(?:джарвис|jarvis|жарвис|джервис)(?=[\s,!.:]|$)[\s,!.:]*/i, "")
  .toLowerCase().replace(/ё/g, "е").replace(/[«»"“”!?.,;]+/g, " ").replace(/\s+/g, " ").trim();

export const UI_VIEWS = [
  ["chat", /^(бесед\S*|чат\S*|главн\S*|домашн\S*( экран\S*)?|home)$/],   // no «Главная» any more: the chat is the first screen
  ["errors", /^(ошибк\S*|проблем\S*|журнал\S* ошибок)$/],
  ["history", /^истори\S*( действий)?$/],
  ["browser", /^браузер\S*$/],
  ["telegram", /^(телеграм\S*|телег[аеиу]|telegram)$/],
  ["whatsapp", /^(ват[сц]ап\S*|вотсап\S*|вацап\S*|whats ?app)$/],
  ["mail", /^(почт\S*|gmail|джимейл\S*)$/],
  ["files", /^файл\S*$/],
  ["settings", /^настройк\S*$/],
  ["comms", /^(сообщени\S*|очеред\S* сообщений)$/],
  ["abilities", /^(возможност\S*|навык\S*|агент\S*)$/],
];
const NOUN = "(?:раздел|вкладк[уа]|экран|страниц[уа]|рабочее пространство|пространство)";
const WITH_NOUN = new RegExp(`^(?:перейди|вернись|переключись|покажи|открой|go to)(?: мне)?(?: (?:на|в|во|к))? ${NOUN} (.+)$`);
const GO = /^(?:перейди|вернись)(?: (?:на|в|во|к))? (.+)$/;
const SHOW = /^(?:покажи|открой) (ошибки|историю действий|настройки|возможности)$/;

/** Text → workspace id, or null. Never a bare «открой X» (that opens the application X). */
export function uiView(text) {
  const t = norm(text);
  if (/^(домой|на главную|вернись (домой|на главную)|go home)$/.test(t)) return "chat";
  const m = t.match(WITH_NOUN) || t.match(GO) || t.match(SHOW);
  if (!m) return null;
  const name = m[1].trim();
  for (const [view, re] of UI_VIEWS) if (re.test(name)) return view;
  return null;
}

/** Global commands: work from any workspace. «local» is what the window does at once before the core answers. */
const CONTROLS = [
  [/^(экстренная остановка|остановить все|останови все|прекрати все|emergency stop|стоп все|стоп стоп|отмени все)$/, "estop", "estop"],
  [/^(стоп|stop|остановись|останови|прекрати|остановить|стоп джарвис|джарвис стоп)$/, "interrupt", "stop_voice"],
  [/^(подожди|погоди|замолчи|хватит|хватит говорить|помолчи|достаточно)$/, "quiet", "stop_voice"],
  [/^(отмени|отмена|отменить|отмени задачу|отмени это|cancel|cancel task)$/, "cancel_last", null],
  [/^(пауза|поставь на паузу|приостанови|pause)$/, "pause_all", null],
  [/^(продолжи|продолжай|возобнови|resume|continue)$/, "resume_all", null],
];

/**
 * @returns {{kind:"nav", view:string} | {kind:"control", op:string, local:string|null} | {kind:"help"} | {kind:"agent", text:string}}
 */
export function classify(text) {
  const t = norm(text);
  if (!t) return { kind: "agent", text: String(text || "").trim() };
  const view = uiView(text);
  if (view) return { kind: "nav", view };
  for (const [re, op, local] of CONTROLS) if (re.test(t)) return { kind: "control", op, local };
  if (/^(помощь|help|что ты умеешь|какие команды|что ты можешь)$/.test(t)) return { kind: "help" };
  return { kind: "agent", text: String(text).trim() };
}
