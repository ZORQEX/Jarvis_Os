// Unit tests of the window's pure logic (no DOM): node --test tests/ui/unit/
import test from "node:test";
import assert from "node:assert/strict";

import { deriveAgent, flashFor, FLASH_MS } from "../../../web/js/core/agent-state.js";
import { clamp, snap, toAnchor, fromAnchor, nearDock, readStored } from "../../../web/js/core/geometry.js";
import { classify, uiView } from "../../../web/js/core/intent.js";
import { rank, score } from "../../../web/js/core/fuzzy.js";
import { timeline, groupTasks, planRows, stepCounts, currentStep, canRetry, statusOf, upsert } from "../../../web/js/core/tasks-model.js";
import { WORKSPACES, ALL, byId, toolStatus, navBadge } from "../../../web/js/workspaces/registry.js";

const NOW = 1_800_000_000_000;
const base = { connected: true, core: { state: "IDLE" }, voice: null, tasks: [], approvals: [], now: NOW };
const task = (over = {}) => ({ id: "t1", request: "Состояние системы", state: "running", route: "tool", created_at: 100, steps: [], ...over });
const step = (over = {}) => ({ id: "s1", tool_id: "system_info", state: "running", seq: 1, title: "Чтение /proc", ...over });

// ---------------------------------------------------------------- agent state machine
test("offline wins over everything", () => {
  const a = deriveAgent({ ...base, connected: false, voice: "recording", tasks: [task()] });
  assert.equal(a.mode, "OFFLINE");
  assert.equal(a.canStop, false);
});

test("emergency stop is STANDBY", () => {
  assert.equal(deriveAgent({ ...base, core: { state: "STANDBY", estop: true } }).mode, "STANDBY");
});

test("idle, muted idle and open microphone", () => {
  assert.equal(deriveAgent(base).mode, "IDLE");
  assert.match(deriveAgent({ ...base, muted: true }).label, /микрофон выключен/);
  const l = deriveAgent({ ...base, micOpen: true });
  assert.equal(l.mode, "LISTENING");
  assert.equal(l.phase, "listening");
});

test("voice pipeline: listening → transcribing → understanding → executing", () => {
  assert.equal(deriveAgent({ ...base, voice: "recording" }).phase, "listening");
  const tr = deriveAgent({ ...base, voice: "transcribing" });
  assert.equal(tr.mode, "THINKING");
  assert.equal(tr.phase, "transcribing");
  const th = deriveAgent({ ...base, core: { state: "THINKING", working: "Claude" }, tasks: [task({ route: "ai" })] });
  assert.equal(th.mode, "THINKING");
  assert.equal(th.phase, "understanding");
  assert.match(th.label, /Claude/);
  const ex = deriveAgent({ ...base, core: { state: "EXECUTING" }, tasks: [task({ steps: [step()] })], progress: { s1: "читаю" } });
  assert.equal(ex.mode, "EXECUTING");
  assert.equal(ex.phase, "executing");
  assert.match(ex.detail, /Система: Чтение \/proc · читаю/);
  assert.equal(ex.canStop, true);
  assert.equal(ex.taskId, "t1");
});

test("planning and paused come from the task's own state", () => {
  assert.equal(deriveAgent({ ...base, tasks: [task({ state: "planning" })] }).mode, "PLANNING");
  assert.equal(deriveAgent({ ...base, tasks: [task({ state: "paused" })] }).mode, "PAUSED");
});

test("streaming text shows the answer being written", () => {
  const a = deriveAgent({ ...base, core: { state: "EXECUTING", working: "Claude" }, tasks: [task({ route: "ai" })], streaming: { t1: "При" } });
  assert.equal(a.mode, "THINKING");
  assert.match(a.label, /отвечает/);
});

test("pending confirmation: generic and «ready to send»", () => {
  const gen = deriveAgent({ ...base, tasks: [task({ state: "waiting_for_approval" })],
    approvals: [{ id: "a1", task_id: "t1", status: "pending", created_at: 1, tool_id: "create_note", summary: { action: "Создать заметку" } }] });
  assert.equal(gen.mode, "WAITING_CONFIRMATION");
  assert.equal(gen.approvalId, "a1");
  assert.equal(gen.detail, "Создать заметку");
  const msg = deriveAgent({ ...base, approvals: [{ id: "a2", task_id: "t2", status: "pending", created_at: 1, tool_id: "message_send",
    args: { body: "Буду в 19:00", deliveries: [{ channel: "telegram", to_name: "Иван Петров" }] } }] });
  assert.equal(msg.label, "Готово к отправке");
  assert.equal(msg.detail, "Иван Петров · Telegram");
  assert.equal(msg.quote, "Буду в 19:00");
  assert.equal(msg.send, true);
  const decided = deriveAgent({ ...base, approvals: [{ id: "a3", status: "approved", created_at: 1, tool_id: "x" }] });
  assert.equal(decided.mode, "IDLE");
});

test("finished task reactions only for the person's own tasks, and they expire", () => {
  const ok = flashFor(task({ state: "completed" }), NOW);
  assert.equal(deriveAgent({ ...base, flash: ok }).mode, "SUCCESS");
  assert.equal(deriveAgent({ ...base, flash: ok, now: NOW + FLASH_MS + 1 }).mode, "IDLE");
  assert.equal(deriveAgent({ ...base, flash: flashFor(task({ state: "failed", error: "нет сети" }), NOW) }).detail, "нет сети");
  assert.equal(deriveAgent({ ...base, flash: flashFor(task({ state: "cancelled" }), NOW) }).mode, "CANCELLED");
  assert.equal(flashFor(task({ state: "completed", route: "ui_read" }), NOW), null);   // folder click
  assert.equal(flashFor(task({ state: "completed", route: "ui" }), NOW), null);        // navigation
  assert.equal(deriveAgent({ ...base, tasks: [task({ route: "ui", state: "running" })] }).mode, "IDLE");
});

test("folder browsing is never shown as JARVIS working", () => {
  const a = deriveAgent({ ...base, tasks: [task({ route: "ui_read", steps: [step({ tool_id: "folder_list" })] })] });
  assert.equal(a.mode, "IDLE");
});

test("speaking can be stopped even without a task", () => {
  const a = deriveAgent({ ...base, voice: "speaking" });
  assert.equal(a.mode, "SPEAKING");
  assert.equal(a.canStop, true);
});

// ---------------------------------------------------------------- geometry
const view = { w: 1280, h: 720 }, size = { w: 56, h: 56 };
test("clamp keeps the companion inside the window", () => {
  assert.deepEqual(clamp({ x: -50, y: 9000 }, size, view), { x: 8, y: 720 - 56 - 8 });
});
test("snap pulls to near edges only", () => {
  assert.deepEqual(snap({ x: 20, y: 300 }, size, view), { x: 12, y: 300 });
  assert.deepEqual(snap({ x: 1280 - 56 - 30, y: 720 - 56 - 20 }, size, view), { x: 1280 - 56 - 12, y: 720 - 56 - 12 });
  assert.deepEqual(snap({ x: 500, y: 300 }, size, view), { x: 500, y: 300 });
});
test("anchor keeps a bottom-right companion bottom-right after resize", () => {
  const p = { x: 1280 - 56 - 12, y: 720 - 56 - 12 };
  const a = toAnchor(p, size, view);
  assert.deepEqual(a, { h: "right", dx: 12, v: "bottom", dy: 12 });
  assert.deepEqual(fromAnchor(a, size, { w: 1920, h: 1080 }), { x: 1920 - 56 - 12, y: 1080 - 56 - 12 });
  assert.deepEqual(fromAnchor({ h: "left", dx: 3000, v: "top", dy: 10 }, size, view), { x: 1280 - 56 - 8, y: 10 });
});
test("dock detection and stored state validation", () => {
  assert.equal(nearDock({ x: 80, y: 10 }, { x: 76, y: 7 }), true);
  assert.equal(nearDock({ x: 400, y: 10 }, { x: 76, y: 7 }), false);
  assert.deepEqual(readStored(null), { docked: true, anchor: null, expanded: false, pinned: false, hidden: false });
  assert.equal(readStored({ docked: false, anchor: { h: "left", v: "top", dx: -5, dy: 2 } }).docked, true);   // bad anchor
  const ok = readStored({ docked: false, anchor: { h: "right", v: "bottom", dx: 12, dy: 12 }, pinned: true, hidden: true });
  assert.equal(ok.docked, false);
  assert.equal(ok.pinned, true);
  assert.equal(ok.hidden, false);   // hiding is temporary
});

// ---------------------------------------------------------------- intent router (window level)
for (const [text, viewId] of [["перейди в ошибки", "errors"], ["Джарвис, домой", "chat"], ["вернись на главную", "chat"],
  ["перейди в телеграм", "telegram"], ["перейди в WhatsApp", "whatsapp"], ["открой раздел файлы", "files"],
  ["переключись на вкладку почта", "mail"], ["покажи ошибки", "errors"], ["открой историю действий", "history"],
  ["открой вкладку настройки", "settings"]]) {
  test(`navigation: «${text}»`, () => assert.deepEqual(classify(text), { kind: "nav", view: viewId }));
}
// «перейди в задачи» is not a section any more: the core answers what is running.
for (const text of ["открой телеграм", "перейди на 3 рабочий стол", "перейди на сайт ya.ru", "перейди в загрузки", "состояние системы",
  "перейди в задачи", "покажи задачи"]) {
  test(`not navigation: «${text}»`, () => assert.equal(uiView(text), null));
}
test("global commands", () => {
  assert.deepEqual(classify("Стоп"), { kind: "control", op: "interrupt", local: "stop_voice" });
  assert.deepEqual(classify("джарвис, отмена"), { kind: "control", op: "cancel_last", local: null });
  assert.deepEqual(classify("пауза"), { kind: "control", op: "pause_all", local: null });
  assert.deepEqual(classify("продолжи"), { kind: "control", op: "resume_all", local: null });
  assert.deepEqual(classify("экстренная остановка"), { kind: "control", op: "estop", local: "estop" });
  assert.deepEqual(classify("помощь"), { kind: "help" });
});
test("complex requests go to the agent unchanged", () => {
  assert.deepEqual(classify("Найди Ивана и подготовь ему ответ"), { kind: "agent", text: "Найди Ивана и подготовь ему ответ" });
});

// ---------------------------------------------------------------- palette search
test("palette ranking", () => {
  const items = [{ title: "Открыть Telegram", keywords: "телеграм мессенджер" }, { title: "Открыть задачи", keywords: "task center" },
    { title: "Экстренная остановка", keywords: "стоп stop" }];
  assert.equal(rank("тел", items)[0].title, "Открыть Telegram");
  assert.equal(rank("зад", items)[0].title, "Открыть задачи");
  assert.equal(rank("стоп", items)[0].title, "Экстренная остановка");
  assert.equal(rank("xyz", items).length, 0);
  assert.equal(rank("", items).length, 3);
  assert.ok(score("откр тел", "Открыть Telegram", "телеграм") > 0);
});

// ---------------------------------------------------------------- task model
const full = task({ state: "completed", created_at: 10, finished_at: 20, steps: [
  step({ id: "s1", state: "succeeded", started_at: 11, finished_at: 12, duration_ms: 900, verification: { status: "verified", detail: "прочитано" } }),
  step({ id: "s2", tool_id: "create_note", state: "succeeded", seq: 2, title: "Заметка", started_at: 15, finished_at: 16 }),
] });
test("timeline from persisted timestamps, in order", () => {
  const ev = timeline(full, [{ id: "a", task_id: "t1", status: "approved", created_at: 13, decided_at: 14, tool_id: "create_note", summary: { action: "Создать заметку" } }]);
  assert.deepEqual(ev.map((e) => e.kind), ["task.created", "tool.started", "tool.completed", "confirmation.required",
    "confirmation.decided", "tool.started", "tool.completed", "task.completed"]);
  assert.match(ev[2].detail, /проверено/);
});
test("plan rows, counts and current step", () => {
  const t = task({ steps: [step({ id: "a", state: "succeeded" }), step({ id: "b", state: "running" }), step({ id: "c", state: "planned" }),
    step({ id: "d", tool_id: "cloud_send", state: "succeeded" })] });
  assert.deepEqual(planRows(t).map((r) => r.mark), ["done", "active", "pending"]);
  assert.deepEqual(stepCounts(t), { done: 1, total: 3 });
  assert.equal(currentStep(t).id, "b");
});
test("groups, retry and status mapping", () => {
  const g = groupTasks([task({ id: "a" }), task({ id: "b", state: "blocked" }), task({ id: "c", state: "failed", finished_at: 5 }),
    task({ id: "d", route: "ui_read", state: "running" }), task({ id: "e", route: "ui", state: "completed", finished_at: 6 })]);
  assert.deepEqual([g.active.map((t) => t.id), g.attention.map((t) => t.id), g.finished.map((t) => t.id)], [["a"], ["b"], ["c"]]);
  assert.equal(canRetry(task({ state: "failed" })), true);
  assert.equal(canRetry(task({ state: "completed" })), false);
  assert.equal(statusOf(task({ state: "waiting_for_approval" })).key, "waiting");
  assert.deepEqual(upsert([task({ id: "x", state: "running" })], task({ id: "x", state: "completed" })).map((t) => t.state), ["completed"]);
});

// ---------------------------------------------------------------- workspaces
test("workspace registry is complete and unique", () => {
  const ids = ALL.map((w) => w.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const id of ["chat", "errors", "history", "browser", "telegram", "whatsapp", "mail", "files", "settings"]) assert.ok(byId(id), id);
  for (const id of ["home", "tasks"]) assert.equal(byId(id), null, id + " was removed");
  assert.deepEqual(WORKSPACES.map((w) => w.hotkey), ["1", "2", "3", "4", "5", "6", "7", "8", "9"]);   // Ctrl+1… in sidebar order
  for (const w of WORKSPACES) { assert.ok(w.title && w.icon && w.context, w.id); assert.ok(Array.isArray(w.tools), w.id); }
});
test("tool status follows the real connection, never assumed", () => {
  const tg = byId("telegram");
  const off = toolStatus(tg, { connected: true, providers: { telegram: { state: "not_connected" } } });
  assert.equal(off.find((t) => t.id === "telegram.send").state, "needs_connection");
  assert.equal(off.find((t) => t.id === "telegram.compose").state, "ready");
  assert.equal(off.find((t) => t.id === "telegram.read").state, "missing");
  const on = toolStatus(tg, { connected: true, providers: { telegram: { state: "connected" } } });
  assert.equal(on.find((t) => t.id === "telegram.send").state, "ready");
  const down = toolStatus(tg, { connected: false, providers: { telegram: { state: "connected" } } });
  assert.equal(down.find((t) => t.id === "telegram.send").state, "offline");
  assert.equal(navBadge(tg, { providers: {} }), null);
  assert.equal(navBadge(tg, { connected: true, providers: { telegram: { state: "connected" } } }), "ok");
  assert.equal(navBadge(tg, { connected: false, providers: { telegram: { state: "connected" } } }), "off");
  assert.equal(navBadge(byId("files"), { providers: {} }), null);
});

test("md: Claude's tables are tables, numbers right-aligned", async () => {
  const { md } = await import("../../../web/js/render.js");
  const h = md("Сравнение:\n\n| Поставщик | Декабрь | Март |\n|---|---|---|\n| **GPA** | 5 675 836,92 £ | 5 784 736,64 £ |\n| NS&I | 13 591 752,54 £ | 4 597 041,33 £ |\n\nИтог ниже.");
  assert.match(h, /<table><thead><tr><th>Поставщик<\/th>/);
  assert.equal((h.match(/<td class="num">/g) || []).length, 4);
  assert.match(h, /<td><strong>GPA<\/strong><\/td>/);
  assert.match(h, /NS&amp;I/);                                  // still escaped
  assert.match(h, /<p>Итог ниже.<\/p>/);
  assert.doesNotMatch(md("Просто | палка в тексте"), /<table>/);
});
