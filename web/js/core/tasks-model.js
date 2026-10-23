// Task model for the Task Center, the timeline and the companion. Pure functions over the core's task records
// (tasks + task_steps + approvals); nothing here invents progress — counts and times come from the records.
import { toolTitle } from "../render.js";

/** @typedef {{id:string, tool_id:string, state:string, title?:string, seq:number, started_at?:number,
 *   finished_at?:number, duration_ms?:number, error?:string, verification?:{status:string, detail:string},
 *   result?:{summary?:string}}} Step */
/** @typedef {{id:string, request:string, state:string, route?:string, created_at:number, updated_at?:number,
 *   finished_at?:number, error?:string, steps:Step[]}} Task */
/** @typedef {{id:string, task_id:string, status:string, created_at:number, decided_at?:number,
 *   tool_id:string, summary?:{action?:string, title?:string}}} Approval */

/** Core task state → status shown to people (Queued / Running / Waiting / Completed / Failed / Cancelled). */
export const STATUS = {
  created: { key: "queued", label: "В очереди" },
  queued: { key: "queued", label: "В очереди" },
  planning: { key: "running", label: "Планирую" },
  running: { key: "running", label: "Выполняется" },
  verifying: { key: "running", label: "Проверяю результат" },
  retrying: { key: "running", label: "Повторяю" },
  waiting_for_approval: { key: "waiting", label: "Ждёт вашего решения" },
  waiting_for_dependency: { key: "waiting", label: "Ждёт данных" },
  paused: { key: "paused", label: "Пауза" },
  completed: { key: "completed", label: "Готово" },
  failed: { key: "failed", label: "Ошибка" },
  cancelled: { key: "cancelled", label: "Отменена" },
  blocked: { key: "failed", label: "Прервана перезапуском" },
};
export const statusOf = (t) => STATUS[t?.state] || { key: "queued", label: t?.state || "—" };

const WORKING = new Set(["created", "queued", "planning", "running", "verifying", "retrying", "waiting_for_approval",
  "waiting_for_dependency", "paused"]);
const FINISHED = new Set(["completed", "failed", "cancelled", "blocked"]);

/** Work the person asked for: not a folder click in «Файлы» (ui_read) and not a workspace switch (ui). */
export const isUserTask = (t) => !!t && t.route !== "ui_read" && t.route !== "ui";
/** Still in progress (blocked tasks are finished: they will not continue by themselves). */
export const isWorking = (t) => !!t && WORKING.has(t.state);
export const isFinished = (t) => !!t && FINISHED.has(t.state);
export const canRetry = (t) => !!t && ["failed", "cancelled", "blocked"].includes(t.state) && t.route !== "ui"
  && t.route !== "ui_read" && !!t.request;

/** Step rows that mean something to a person (internal cloud bookkeeping is hidden). */
export const visibleSteps = (t) => (t?.steps || []).filter((s) => s.tool_id !== "cloud_send" && s.state !== "resumed");

/** ✓ done · ● active · ! waiting for a decision · ✕ failed · – skipped · ○ not started */
export function stepMark(s) {
  switch (s.state) {
    case "succeeded": return "done";
    case "running": case "verifying": return "active";
    case "awaiting_approval": return "waiting";
    case "failed": case "invalid": return "failed";
    case "denied": case "cancelled": case "interrupted": return "skipped";
    default: return "pending";
  }
}

export function currentStep(t) {
  const steps = visibleSteps(t);
  return steps.find((s) => s.state === "running" || s.state === "verifying")
    || steps.find((s) => s.state === "awaiting_approval") || null;
}

/** Steps finished vs. known so far. Not a percentage: the plan can grow while the task runs. */
export function stepCounts(t) {
  const steps = visibleSteps(t);
  return { done: steps.filter((s) => stepMark(s) === "done").length, total: steps.length };
}

export const stepLabel = (s) => {
  const title = s.title || s.result?.summary || "";
  const tool = toolTitle(s.tool_id);
  return title && title !== tool ? `${tool}: ${title}` : tool;
};

/**
 * Execution timeline of one task from its persisted timestamps: created → each step started/finished →
 * approvals asked/decided → finished. Works after a reload because nothing comes from transient events.
 * @param {Task} task @param {Approval[]} approvals
 */
export function timeline(task, approvals = []) {
  if (!task) return [];
  const ev = [{ ts: task.created_at, kind: "task.created", mark: "info", label: "Задача получена", detail: task.request }];
  for (const s of visibleSteps(task)) {
    const label = stepLabel(s);
    if (s.started_at) ev.push({ ts: s.started_at, kind: "tool.started", mark: "active", label: `Начато: ${label}`, tool: s.tool_id });
    const m = stepMark(s);
    if (s.finished_at) {
      if (m === "done") {
        ev.push({ ts: s.finished_at, kind: "tool.completed", mark: "done", label, tool: s.tool_id,
          detail: s.verification?.status === "verified" ? `проверено: ${s.verification.detail}` : s.verification?.detail || "",
          ms: s.duration_ms });
      } else if (m === "failed") {
        ev.push({ ts: s.finished_at, kind: "tool.failed", mark: "failed", label, tool: s.tool_id, detail: s.error || "" });
      } else if (m === "skipped") {
        ev.push({ ts: s.finished_at, kind: "tool.skipped", mark: "skipped", label, tool: s.tool_id, detail: s.error || "" });
      }
    }
  }
  for (const a of approvals.filter((x) => x.task_id === task.id)) {
    const what = a.summary?.action || a.summary?.title || toolTitle(a.tool_id);
    ev.push({ ts: a.created_at, kind: "confirmation.required", mark: "waiting", label: "Нужно ваше решение", detail: what });
    if (a.decided_at) {
      const lbl = { approved: "Разрешено вами", denied: "Отклонено вами", expired: "Подтверждение истекло" }[a.status] || a.status;
      ev.push({ ts: a.decided_at, kind: "confirmation.decided", mark: a.status === "approved" ? "done" : "skipped", label: lbl, detail: what });
    }
  }
  if (task.finished_at) {
    const st = statusOf(task);
    const kind = { completed: "task.completed", failed: "task.failed", cancelled: "task.cancelled" }[st.key] || "task.finished";
    ev.push({ ts: task.finished_at, kind, mark: st.key === "completed" ? "done" : st.key === "cancelled" ? "skipped" : "failed",
      label: st.label, detail: st.key === "completed" ? "" : task.error || "" });
  }
  // Stable order: by time, then by the natural sequence of kinds within the same instant.
  const order = ["task.created", "tool.started", "confirmation.required", "confirmation.decided", "tool.completed",
    "tool.failed", "tool.skipped", "task.completed", "task.failed", "task.cancelled", "task.finished"];
  return ev.map((e, i) => ({ ...e, i })).sort((a, b) => a.ts - b.ts || order.indexOf(a.kind) - order.indexOf(b.kind) || a.i - b.i);
}

/** Plan rows: done / active / waiting / pending, for the ✓ ● ○ list on a task card. */
export const planRows = (t) => visibleSteps(t).map((s) => ({ id: s.id, mark: stepMark(s), label: stepLabel(s), error: s.error || "" }));

/** Task Center groups. */
export function groupTasks(list) {
  const user = list.filter(isUserTask);
  return {
    active: user.filter(isWorking).sort((a, b) => a.created_at - b.created_at),
    attention: user.filter((t) => t.state === "blocked"),
    finished: user.filter((t) => isFinished(t) && t.state !== "blocked").sort((a, b) => (b.finished_at || b.created_at) - (a.finished_at || a.created_at)),
  };
}

/** Merge a fresh task record into a list (newest first), replacing an older copy. */
export function upsert(list, t) {
  const i = list.findIndex((x) => x.id === t.id);
  if (i >= 0) { const out = list.slice(); out[i] = t; return out; }
  return [t, ...list];
}
