// The JARVIS state machine shown by the companion and the top bar. One mode, derived only from real runtime state:
// the WebSocket link, the core's own state, the microphone/speech state, active tasks and pending confirmations.
// Short SUCCESS / ERROR / CANCELLED reactions come from real task transitions (task.updated), never from a timer alone.
import { currentStep, isUserTask, isWorking, stepLabel } from "./tasks-model.js";

export const MODES = ["OFFLINE", "STANDBY", "IDLE", "LISTENING", "THINKING", "PLANNING", "EXECUTING",
  "WAITING_CONFIRMATION", "SPEAKING", "SUCCESS", "ERROR", "CANCELLED", "PAUSED"];

/** How long a finished task's reaction stays visible. */
export const FLASH_MS = 2600;

/** Busy modes: the companion offers «Стоп». */
const BUSY = new Set(["THINKING", "PLANNING", "EXECUTING", "SPEAKING", "WAITING_CONFIRMATION", "PAUSED"]);

const CH = { telegram: "Telegram", whatsapp: "WhatsApp", email: "почта" };

/**
 * @param {object} i
 * @param {boolean} i.connected           WebSocket to the core is open
 * @param {object} i.core                 core.state payload {state, estop, working, pending_approvals}
 * @param {string|null} i.voice           null | listening | recording | transcribing | synthesizing | speaking | interrupted | error
 * @param {boolean} [i.micOpen]           microphone stream open (conversation mode or a single phrase)
 * @param {boolean} [i.muted]
 * @param {object[]} [i.tasks]            task records (any order)
 * @param {object[]} [i.approvals]        approvals (only pending ones matter)
 * @param {Record<string,string>} [i.streaming]  task id → text streamed so far
 * @param {Record<string,string>} [i.progress]   step id → latest progress message
 * @param {{kind:string, at:number, taskId?:string, text?:string}|null} [i.flash]
 * @param {number} i.now                  ms
 */
export function deriveAgent(i) {
  const core = i.core || {};
  const tasks = (i.tasks || []).filter((t) => isUserTask(t) && isWorking(t)).sort((a, b) => b.created_at - a.created_at);
  const pending = (i.approvals || []).filter((a) => a.status === "pending").sort((a, b) => a.created_at - b.created_at);
  const withStep = tasks.find((t) => currentStep(t));
  const task = withStep || tasks[0] || null;
  const out = (mode, label, extra = {}) => ({
    mode, label, detail: "", phase: null, taskId: task?.id || null, approvalId: null,
    busy: BUSY.has(mode), canStop: BUSY.has(mode) && (!!task || mode === "SPEAKING"), ...extra,
  });

  if (!i.connected) return out("OFFLINE", "Нет связи с ядром", { detail: "Переподключаюсь…", busy: false, canStop: false, taskId: null });
  if (core.estop) return out("STANDBY", "Экстренная остановка", { detail: "Действия заблокированы, пока вы не снимете остановку", busy: false, canStop: false });

  const v = i.voice;
  if (v === "recording") return out("LISTENING", "Слушаю…", { phase: "listening", busy: false, canStop: false });
  if (v === "interrupted") return out("LISTENING", "Перебито — слушаю", { phase: "listening", busy: false, canStop: false });
  if (v === "transcribing") return out("THINKING", "Распознаю речь…", { phase: "transcribing", detail: "На этом компьютере, без облака" });

  if (pending.length) {
    const a = pending[0];
    const extra = { approvalId: a.id, taskId: a.task_id, detail: a.summary?.action || a.summary?.title || "" };
    if (pending.length > 1) extra.count = pending.length;
    if (a.tool_id === "message_send") {
      const d = (a.args?.deliveries || [])[0];
      const to = d ? `${d.to_name}${CH[d.channel] ? " · " + CH[d.channel] : ""}` : "";
      return out("WAITING_CONFIRMATION", "Готово к отправке", { ...extra, detail: to, quote: a.args?.body || "", send: true });
    }
    return out("WAITING_CONFIRMATION", "Нужно ваше решение", extra);
  }

  if (v === "speaking") return out("SPEAKING", "Говорю…", { phase: "speaking" });
  const working = core.working ? `${core.working} ` : "";
  const st = core.state;
  if (task) {
    const step = currentStep(task);
    const stream = i.streaming?.[task.id];
    if (stream) return out("THINKING", `${working || "Claude "}отвечает…`.replace(/^\s+/, ""), { phase: "understanding", detail: task.request });
    if (task.state === "planning") return out("PLANNING", "Составляю план", { phase: "understanding", detail: task.request });
    if (task.state === "paused") return out("PAUSED", "Пауза", { detail: task.request });
    if (step && step.state !== "awaiting_approval") {
      const prog = i.progress?.[step.id];
      return out("EXECUTING", "Выполняю", { phase: "executing", detail: stepLabel(step) + (prog ? ` · ${prog}` : "") });
    }
    if (st === "THINKING") return out("THINKING", working ? `${working}думает…` : "Думаю…", { phase: "understanding", detail: task.request });
    return out("THINKING", "Работаю…", { phase: "understanding", detail: task.request });
  }
  if (v === "synthesizing") return out("SPEAKING", "Готовлю голос…", { phase: "speaking" });
  if (st === "PAUSED") return out("PAUSED", "Пауза");

  const f = i.flash;
  if (f && i.now - f.at < FLASH_MS) {
    if (f.kind === "success") return out("SUCCESS", "Готово", { detail: f.text || "", busy: false, canStop: false, taskId: f.taskId || null });
    if (f.kind === "error") return out("ERROR", "Не получилось", { detail: f.text || "", busy: false, canStop: false, taskId: f.taskId || null });
    if (f.kind === "cancelled") return out("CANCELLED", "Задача остановлена", { detail: f.text || "", busy: false, canStop: false, taskId: f.taskId || null });
  }
  if (v === "error") return out("ERROR", "Звук ответа не получился", { detail: "Ответ показан текстом", busy: false, canStop: false });
  if (i.micOpen) return out("LISTENING", "Жду вашу фразу", { phase: "listening", detail: "Микрофон включён", busy: false, canStop: false });
  return out("IDLE", i.muted ? "Готов · микрофон выключен" : "Готов", { busy: false, canStop: false, taskId: null });
}

/** A finished task → the short reaction it deserves (or null when the task was not the person's own work). */
export function flashFor(task, now) {
  if (!isUserTask(task) || task.route === "ui") return null;
  if (task.state === "completed") return { kind: "success", at: now, taskId: task.id, text: task.request };
  if (task.state === "failed") return { kind: "error", at: now, taskId: task.id, text: task.error || task.request };
  if (task.state === "cancelled") return { kind: "cancelled", at: now, taskId: task.id, text: task.request };
  return null;
}

/** The voice pipeline the top bar can show: Listening → Transcribing → Understanding → Executing. */
export const PHASES = [["listening", "Слушаю"], ["transcribing", "Распознаю"], ["understanding", "Понимаю"], ["executing", "Выполняю"]];
