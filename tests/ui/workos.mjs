// WorkOS end-to-end check against a running (isolated) JARVIS core, in system Chromium (headless).
// Drives the window like a person: workspaces, companion drag/persistence, chat, confirmation, cancel, palette,
// files, voice-navigation through the core, and layout at several window sizes. Prints a JSON report; exit 1 on failure.
// Usage: node workos.mjs <port> <tokenFile> <outDir>   (LIVE=1 also checks real Claude streaming — uses the subscription)
import { chromium } from "playwright-core";
import fs from "node:fs";

const [port, tokenFile, outDir] = process.argv.slice(2);
const token = fs.readFileSync(tokenFile, "utf8").trim();
fs.mkdirSync(outDir, { recursive: true });
const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", headless: true,
  args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"] });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1.5, locale: "ru-RU", colorScheme: "dark" });
const page = await ctx.newPage();
const errors = [], results = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
const J = (f, a) => page.evaluate(f, a);
const shot = (n) => page.screenshot({ path: `${outDir}/${n}.png` });
async function check(name, fn) {
  const t0 = Date.now();
  try { const info = await fn(); results.push({ name, ok: true, ms: Date.now() - t0, ...(info ? { info } : {}) }); }
  catch (e) { results.push({ name, ok: false, error: String(e.message || e).slice(0, 400) }); await shot("FAIL-" + name.replace(/\W+/g, "_")).catch(() => {}); }
}
const visible = (sel) => page.locator(sel).first().isVisible();
const assert = (c, m) => { if (!c) throw new Error(m); };
const view = () => J(() => window.__jarvis.S.view);
const waitView = (v) => page.waitForFunction((x) => window.__jarvis.S.view === x, v, { timeout: 5000 });
const cdState = () => J(() => { const d = document.getElementById("cdock"); return { docked: d.dataset.docked, expanded: d.dataset.expanded, mode: d.dataset.mode, hidden: d.hidden, tf: d.style.transform }; });

const t0 = Date.now();
await page.goto(`http://127.0.0.1:${port}/?t=${token}`);
await page.evaluate(() => localStorage.clear());
await page.reload();
await page.waitForFunction(() => window.__jarvis?.S.connected === true, null, { timeout: 10000 });
const readyMs = Date.now() - t0;

await check("app starts on Home", async () => {
  assert(await view() === "home", "view is " + await view());
  assert(await visible('[data-ws="home"] .home-ask'), "home ask box missing");
  await page.waitForFunction(() => document.querySelector("#status").dataset.state === "IDLE", null, { timeout: 4000 });
  return { status: await J(() => document.querySelector("#status .status-label").textContent) };
});
await shot("01-home");

await check("sidebar: every workspace opens", async () => {
  const ids = await J(() => [...document.querySelectorAll("#nav [data-nav]")].map((b) => b.dataset.nav));
  assert(ids.length === 9, "expected 9 workspaces, got " + ids.length);
  for (const id of ids) {
    await page.click(`#nav [data-nav="${id}"]`);
    await waitView(id);
    const ok = await J((x) => { const s = document.querySelector(`[data-ws="${x}"]`); return !!s && !s.hidden && s.getBoundingClientRect().height > 100; }, id);
    assert(ok, id + " section not visible");
    assert(await J((x) => document.querySelector(`#nav [data-nav="${x}"]`).getAttribute("aria-selected") === "true", id), id + " not selected");
    await page.waitForTimeout(250);
    await shot(`02-ws-${id}`);
  }
  return { ids };
});

await check("companion: global, docked by default", async () => {
  await page.click('#nav [data-nav="home"]');
  const s = await cdState();
  assert(s.docked === "1" && !s.hidden, JSON.stringify(s));
  const slot = await J(() => { const r = document.getElementById("dockSlot").getBoundingClientRect(); return [r.left, r.top]; });
  const orb = await J(() => { const r = document.getElementById("cdOrb").getBoundingClientRect(); return [r.left, r.top]; });
  assert(Math.abs(slot[0] - orb[0]) < 2 && Math.abs(slot[1] - orb[1]) < 2, `orb ${orb} not on slot ${slot}`);
  assert(await J(() => document.querySelectorAll(".cdock").length === 1), "more than one companion");
});

await check("companion: drag, clamp, persist, survive navigation", async () => {
  await J(() => { document.getElementById("cdock").__marker = 42; });
  const o = await page.locator("#cdOrb").boundingBox();
  await page.mouse.move(o.x + o.width / 2, o.y + o.height / 2);
  await page.mouse.down();
  for (let i = 1; i <= 12; i++) await page.mouse.move(o.x + 20 + i * 70, o.y + 20 + i * 40);
  await page.mouse.move(5000, 5000);                      // far outside: must be clamped
  await page.mouse.up();
  await page.waitForTimeout(200);
  let b = await page.locator("#cdOrb").boundingBox();
  assert(b.x + b.width <= 1440 && b.y + b.height <= 900 && b.x >= 0 && b.y >= 0, "outside viewport " + JSON.stringify(b));
  assert((await cdState()).docked === "0", "still docked");
  const stored = await J(() => JSON.parse(localStorage.getItem("jarvis.companion")));
  assert(stored && stored.docked === false && stored.anchor.h === "right" && stored.anchor.v === "bottom", "stored " + JSON.stringify(stored));
  // move to the middle (no snap there)
  await page.mouse.move(b.x + 20, b.y + 20); await page.mouse.down();
  await page.mouse.move(b.x - 200, b.y - 150, { steps: 6 }); await page.mouse.move(700, 450, { steps: 6 }); await page.mouse.up();
  await page.waitForTimeout(200);
  const mid = await page.locator("#cdOrb").boundingBox();
  for (const id of ["tasks", "telegram", "files", "chat", "home"]) { await page.click(`#nav [data-nav="${id}"]`); await waitView(id); }
  assert(await J(() => document.getElementById("cdock").__marker === 42), "companion element was re-created on navigation");
  const after = await page.locator("#cdOrb").boundingBox();
  assert(Math.abs(after.x - mid.x) < 2 && Math.abs(after.y - mid.y) < 2, "moved during navigation");
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.waitForTimeout(250);
  const rs = await page.locator("#cdOrb").boundingBox();
  assert(rs.x + rs.width <= 1280 && rs.y + rs.height <= 720, "outside after resize");
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.reload();
  await page.waitForFunction(() => window.__jarvis?.S.connected === true);
  await page.waitForTimeout(250);
  const re = await page.locator("#cdOrb").boundingBox();
  assert(Math.abs(re.x - mid.x) < 3 && Math.abs(re.y - mid.y) < 3, `position not restored: ${JSON.stringify(re)} vs ${JSON.stringify(mid)}`);
  await shot("03-companion-floating");
  // drop near its slot: docks again
  const slot = await J(() => { const r = document.getElementById("dockSlot").getBoundingClientRect(); return [r.left, r.top]; });
  await page.mouse.move(re.x + 26, re.y + 26); await page.mouse.down();
  await page.mouse.move(400, 300, { steps: 5 }); await page.mouse.move(slot[0] + 24, slot[1] + 22, { steps: 8 }); await page.mouse.up();
  await page.waitForTimeout(300);
  assert((await cdState()).docked === "1", "did not dock again");
  return { floating: mid, restored: re };
});

await check("companion: expand, pin, hide, restore with Ctrl+J", async () => {
  await page.click("#cdOrb");
  assert((await cdState()).expanded === "1", "not expanded on click");
  await page.waitForTimeout(400);
  await shot("04-companion-card");
  await page.click('[data-cd="pin"]');
  assert(await J(() => JSON.parse(localStorage.getItem("jarvis.companion")).pinned === true), "pin not stored");
  await page.click('[data-cd="pin"]');
  await page.click('[data-cd="hide"]');
  assert((await cdState()).hidden === true, "not hidden");
  await page.keyboard.press("Control+j");
  const s = await cdState();
  assert(!s.hidden && s.expanded === "1", "Ctrl+J did not restore " + JSON.stringify(s));
  assert(await J(() => document.activeElement?.closest(".cd-ask") != null), "ask field not focused");
  await page.keyboard.press("Escape");
  assert((await cdState()).expanded === "0", "Esc did not collapse");
});

await check("chat: local command renders a real result", async () => {
  await page.click('#nav [data-nav="chat"]');
  await page.fill("#input", "Состояние системы");
  await page.keyboard.press("Enter");
  await page.waitForSelector("#thread .stats", { timeout: 6000 });
  const msgs = await J(() => window.__jarvis.S.messages.map((m) => m.role));
  assert(msgs.includes("user") && msgs.includes("assistant"), "messages " + msgs);
  await page.waitForTimeout(400);
  await shot("05-chat-system");
});

await check("streaming renderer shows text as it arrives", async () => {
  // Rendering check: feeds the UI the same events the core sends (assistant.delta) for an existing turn.
  const tid = await J(() => window.__jarvis.S.messages.find((m) => m.role === "user")?.task_id);
  const r = await J(async (tid) => {
    const { S, link } = window.__jarvis;
    const t = S.tasks[tid];
    const saved = JSON.parse(JSON.stringify(t));
    link.onEvent({ kind: "task.updated", task_id: tid, task: { ...t, state: "running", finished_at: null } });
    const keepReplies = S.messages.filter((m) => m.task_id === tid && m.role === "assistant");
    S.messages = S.messages.filter((m) => !(m.task_id === tid && m.role === "assistant"));
    window.__jarvis.render();
    const seen = [];
    for (const piece of ["Первая ", "часть ", "ответа"]) {
      link.onEvent({ kind: "assistant.delta", task_id: tid, text: piece });
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
      seen.push(document.querySelector(`[data-stream="${tid}"]`)?.textContent || "");
    }
    const mode = document.getElementById("cdock").dataset.mode;
    S.messages.push(...keepReplies);
    link.onEvent({ kind: "task.updated", task_id: tid, task: saved });
    delete S.streaming[tid];
    window.__jarvis.render();
    return { seen, mode };
  }, tid);
  assert(r.seen[0] === "Первая" || r.seen[0].startsWith("Первая"), "first chunk " + r.seen[0]);
  assert(r.seen[2].includes("Первая часть ответа"), "final " + r.seen[2]);
  assert(r.mode === "THINKING", "companion mode while streaming: " + r.mode);
  return r;
});

await check("confirmation: companion asks, approve runs the real tool", async () => {
  await page.click('#nav [data-nav="files"]');
  await waitView("files");
  const tid = await J(async () => (await window.__jarvis.link.request("submit", { text: "запиши заметку: проверка WorkOS", source: "text" })).task_id);
  await page.waitForFunction(() => document.getElementById("cdock").dataset.mode === "WAITING_CONFIRMATION", null, { timeout: 6000 });
  const s = await cdState();
  assert(s.expanded === "1", "companion did not open for the decision");
  assert(await visible('[data-cd="approve"]'), "no approve button");
  assert(await J(() => document.querySelector("#status").dataset.state === "WAITING_CONFIRMATION"), "top bar not waiting");
  assert(await J(() => !document.getElementById("approvalsSection").hidden), "side panel has no decision");
  await page.waitForTimeout(400);
  await shot("06-confirmation");
  await page.click('[data-cd="approve"]');
  await page.waitForFunction((t) => window.__jarvis.S.tasks[t]?.state === "completed", tid, { timeout: 8000 });
  const mode = await J(() => document.getElementById("cdock").dataset.mode);
  return { tid, modeAfter: mode };
});

await check("cancel: a waiting task is stopped from the Task Center", async () => {
  const tid = await J(async () => (await window.__jarvis.link.request("submit", { text: "запиши заметку: эту отменить", source: "text" })).task_id);
  await page.waitForFunction((t) => window.__jarvis.S.tasks[t]?.state === "waiting_for_approval", tid, { timeout: 6000 });
  await J((t) => window.__jarvis.nav("tasks", { task: t }), tid);
  await page.waitForSelector('#tcDetail [data-t="cancel"]', { timeout: 6000 });
  await shot("07-task-center-waiting");
  await page.click('#tcDetail [data-t="cancel"]');
  await page.waitForFunction((t) => window.__jarvis.S.tasks[t]?.state === "cancelled", tid, { timeout: 6000 });
  await page.waitForTimeout(500);
  const tl = await J(() => [...document.querySelectorAll("#tcDetail .tl-ev")].map((e) => e.dataset.kind));
  assert(tl.includes("task.created") && tl.includes("confirmation.required") && tl.includes("task.cancelled"), "timeline " + tl);
  await shot("08-task-center-cancelled");
  return { timeline: tl };
});

await check("task center: completed task has plan and timeline", async () => {
  await page.click('[data-filter="all"]');
  const n = await J(() => document.querySelectorAll("#tcList .tc-card").length);
  assert(n >= 3, "tasks listed: " + n);
  await page.locator("#tcList .tc-card", { hasText: "проверка WorkOS" }).first().click();
  await page.waitForTimeout(400);
  const marks = await J(() => [...document.querySelectorAll("#tcDetail .plan-row")].map((r) => r.dataset.mark));
  const tl = await J(() => [...document.querySelectorAll("#tcDetail .tl-ev")].map((e) => e.dataset.kind));
  assert(marks.includes("done"), "plan " + marks);
  assert(tl.includes("confirmation.decided") && tl.includes("tool.completed") && tl.includes("task.completed"), "timeline " + tl);
  await shot("09-task-center-done");
  return { marks, timeline: tl };
});

await check("companion: ask from another workspace, answer pops up there", async () => {
  await page.click('#nav [data-nav="files"]');
  await waitView("files");
  await page.keyboard.press("Control+j");
  await page.keyboard.type("Сколько места на диске");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector("#cdock .cd-reply")?.textContent.length > 5, null, { timeout: 8000 });
  assert(await view() === "files", "left the workspace");
  await page.waitForTimeout(300);
  await shot("10b-companion-answer");
  const text = await J(() => document.querySelector("#cdock .cd-reply").textContent);
  await page.mouse.click(900, 700);                       // click elsewhere: folds away
  await page.waitForTimeout(150);
  assert((await cdState()).expanded === "0", "card did not fold on outside click");
  return { reply: text.slice(0, 120) };
});

await check("Esc stops the waiting task (after closing the card)", async () => {
  const tid = await J(async () => (await window.__jarvis.link.request("submit", { text: "запиши заметку: отменить клавишей", source: "text" })).task_id);
  await page.waitForFunction((t) => window.__jarvis.S.tasks[t]?.state === "waiting_for_approval", tid, { timeout: 6000 });
  await page.waitForFunction(() => document.getElementById("cdock").dataset.expanded === "1", null, { timeout: 3000 });
  await J(() => document.activeElement?.blur());
  await page.keyboard.press("Escape");                    // 1: fold the card
  assert((await cdState()).expanded === "0", "first Esc did not fold");
  await page.keyboard.press("Escape");                    // 2: stop the task
  await page.waitForFunction((t) => window.__jarvis.S.tasks[t]?.state === "cancelled", tid, { timeout: 5000 });
});

await check("palette: Ctrl+K finds and runs commands", async () => {
  await page.click('#nav [data-nav="home"]');
  await page.keyboard.press("Control+k");
  assert(await visible(".palette .pal"), "palette not open");
  await page.keyboard.type("файл");
  await page.waitForTimeout(300);
  await shot("10-palette");
  await page.keyboard.press("Enter");
  await waitView("files");
  await page.keyboard.press("Control+k");
  await page.keyboard.type("какая погода завтра");
  const last = await J(() => [...document.querySelectorAll(".pal-item .t")].pop()?.textContent || "");
  assert(last.startsWith("Спросить JARVIS"), "no ask fallback: " + last);
  await page.keyboard.press("Escape");
  assert(!(await visible(".palette .pal")), "Esc did not close");
});

await check("navigation: typed command (window) and core route (voice path)", async () => {
  await page.click('#nav [data-nav="chat"]');
  const before = await J(() => window.__jarvis.S.messages.length);
  await page.fill("#input", "перейди в задачи");
  await page.keyboard.press("Enter");
  await waitView("tasks");
  assert(await J(() => window.__jarvis.S.messages.length) === before, "typed navigation reached the core");
  await J(() => window.__jarvis.link.request("submit", { text: "Джарвис, перейди в браузер", source: "text" }));   // core router path
  await waitView("browser");
});

await check("files: browse, preview, ask JARVIS about a file", async () => {
  await page.click('#nav [data-nav="files"]');
  await page.waitForSelector("#fxList .fx-row", { timeout: 6000 });
  await page.locator("#fxList .fx-row", { hasText: "Документы" }).click();
  await page.locator("#fxList .fx-row", { hasText: "план.md" }).click();
  await page.waitForFunction(() => document.querySelector("#fxPrev pre")?.textContent.includes("позвонить фабрике"), null, { timeout: 6000 });
  await shot("11-files-preview");
  await page.click('#fxPrev [data-fx="ask"]');
  await waitView("chat");
  const att = await J(() => [...document.querySelectorAll("#attachments .att span")].map((s) => s.textContent));
  assert(att.includes("план.md"), "attachment " + att);
  await page.click("[data-att-del]");
  const tasksBefore = await J(() => Object.values(window.__jarvis.S.tasks).filter((t) => t.route === "ui_read").length);
  return { attachment: att, browsingTasksInChat: tasksBefore };
});

await check("telegram: honest connection state and composer", async () => {
  await page.click('#nav [data-nav="telegram"]');
  await page.waitForSelector("#cwConnect .pcard", { timeout: 6000 });
  const pill = await J(() => document.querySelector('[data-ws="telegram"] .ws-head .cpill')?.textContent);
  assert(pill === "Не подключено", "pill " + pill);
  await page.click('[data-ws="telegram"] .ws-tools summary');
  const tools = await J(() => [...document.querySelectorAll('[data-ws="telegram"] .tool-row')].map((r) => r.dataset.state));
  assert(tools.includes("needs_connection") && tools.includes("missing") && !tools.every((t) => t === "ready"), "tools " + tools);
  await shot("12-telegram");
  await page.click('[data-ws="telegram"] .ws-tools summary');
  await page.click("#cwNew");
  assert(await visible("#cwTo"), "no recipient field");
  return { pill, tools };
});

await check("persistence: workspace and sidebar survive reload", async () => {
  await page.click("#sbToggle");
  const wide = await J(() => document.getElementById("app").classList.contains("sb-wide"));
  await page.click('#nav [data-nav="browser"]');
  await page.reload();
  await page.waitForFunction(() => window.__jarvis?.S.connected === true);
  assert(await view() === "browser", "workspace not restored: " + await view());
  assert(await J(() => document.getElementById("app").classList.contains("sb-wide")) === wide, "sidebar state not restored");
  await shot("13-browser-wide-sidebar");
  await page.click("#sbToggle");
});

await check("settings (moved module) work: privacy, theme", async () => {
  await page.click('[data-nav="settings"]');
  await waitView("settings");
  await page.waitForSelector("#viewSettings .agency, #viewSettings [data-privacy]", { timeout: 5000 });
  await page.waitForFunction(() => (document.getElementById("voicePicker")?.textContent || "").length > 20, null, { timeout: 5000 });
  const vp = await J(() => document.getElementById("voicePicker").textContent);
  assert(!/Неизвестный запрос/.test(vp) && !/Неизвестный запрос/.test(await J(() => document.getElementById("viewSettings").textContent)), "settings request failed: " + vp.slice(0, 100));
  const before = await J(() => window.__jarvis.S.core.privacy);
  await page.click('[data-privacy="PRIVATE_LOCAL"]');
  await page.waitForFunction(() => window.__jarvis.S.core.privacy === "PRIVATE_LOCAL", null, { timeout: 3000 });
  assert(await J(() => !!document.querySelector("#chips .chip.warn")), "restricted privacy not shown in the top bar");
  await page.click(`[data-privacy="${before}"]`);
  await page.click('[data-theme-set="light"]');
  assert(await J(() => document.documentElement.dataset.theme === "light"), "theme not applied");
  await J(() => window.__jarvis.nav("home"));
  await page.waitForTimeout(300);
  await shot("14-light-home");
  await J(() => window.__jarvis.nav("tasks"));
  await page.waitForTimeout(300);
  await shot("15-light-tasks");
  await J(() => window.__jarvis.nav("settings"));
  await page.click('[data-theme-set=""]');
});

await check("layout: no overflow or overlap at 1280×720, 1440×900, 1920×1080", async () => {
  const report = {};
  for (const [w, h] of [[1280, 720], [1440, 900], [1920, 1080]]) {
    await page.setViewportSize({ width: w, height: h });
    for (const id of ["home", "chat", "tasks", "browser", "telegram", "files", "flora", "settings"]) {
      await J((x) => window.__jarvis.nav(x), id);
      await page.waitForTimeout(220);
      const r = await J(() => {
        const sec = [...document.querySelectorAll("#workspaces > section")].find((s) => !s.hidden);
        const over = document.documentElement.scrollWidth > innerWidth + 1;
        const secOver = sec && sec.scrollWidth > sec.clientWidth + 1;
        const orb = document.getElementById("cdOrb").getBoundingClientRect();
        const inView = orb.left >= 0 && orb.top >= 0 && orb.right <= innerWidth && orb.bottom <= innerHeight;
        const clipped = [...document.querySelectorAll(".topbar > *")].filter((e) => e.getBoundingClientRect().right > innerWidth + 1).map((e) => e.className);
        const label = document.querySelector("#status .status-label");
        const statusCut = label.scrollWidth > label.clientWidth + 1;
        const crumb = document.getElementById("crumb");
        const wrapped = crumb.scrollHeight > 26 || document.querySelector(".topbar").scrollHeight > 56;
        const chipsCut = [...document.querySelectorAll("#chips .chip")].some((c) => c.getBoundingClientRect().right > document.getElementById("paletteBtn").getBoundingClientRect().left + 1);
        return { over, secOver, inView, clipped, statusCut, wrapped, chipsCut };
      });
      if (r.over || r.secOver || !r.inView || r.clipped.length || r.statusCut || r.wrapped || r.chipsCut) report[`${w}x${h}:${id}`] = r;
      if (["home", "chat", "tasks", "telegram"].includes(id)) await shot(`20-${w}x${h}-${id}`);
    }
  }
  assert(!Object.keys(report).length, JSON.stringify(report));
  await page.setViewportSize({ width: 1440, height: 900 });
});

await check("performance: long chat renders fast", async () => {
  await J(() => window.__jarvis.nav("chat"));
  const r = await J(async () => {
    const { S, render } = window.__jarvis;
    const saved = S.messages;
    const now = Date.now() / 1000;
    S.messages = Array.from({ length: 600 }, (_, i) => ({ id: "perf" + i, conversation_id: S.convId, role: i % 2 ? "assistant" : "user",
      text: (i % 2 ? "Ответ **номер** " : "Вопрос ") + i + " — " + "текст ".repeat(30), task_id: null, meta: {}, created_at: now - 600 + i }));
    const t0 = performance.now(); render(); const full = performance.now() - t0;
    S.messages.push({ id: "perf-x", conversation_id: S.convId, role: "user", text: "ещё", task_id: null, meta: {}, created_at: now });
    const t1 = performance.now(); window.__jarvis.bus.emit("messages"); await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    const incr = performance.now() - t1;
    S.messages = saved; render();
    return { full_render_ms: Math.round(full), incremental_ms: Math.round(incr) };
  });
  assert(r.full_render_ms < 800, "full render slow " + r.full_render_ms);
  return r;
});

if (process.env.LIVE === "1") {
  await check("LIVE: Claude answer streams into the companion and chat", async () => {
    await J(() => window.__jarvis.nav("home"));
    const states = [];
    await J(() => { window.__modes = []; const d = document.getElementById("cdock"); new MutationObserver(() => window.__modes.push(d.dataset.mode)).observe(d, { attributes: true, attributeFilter: ["data-mode"] }); });
    const tid = await J(async () => (await window.__jarvis.link.request("submit", { text: "Ответь одним коротким предложением: что такое API?", source: "text" })).task_id);
    await page.waitForFunction((t) => ["completed", "failed"].includes(window.__jarvis.S.tasks[t]?.state), tid, { timeout: 90000 });
    states.push(...await J(() => window.__modes));
    const reply = await J((t) => window.__jarvis.S.messages.filter((m) => m.task_id === t && m.role === "assistant").map((m) => m.text).join(" "), tid);
    await page.waitForTimeout(300);
    await shot("30-live-companion-reply");
    assert(reply.length > 5, "no reply");
    return { modes: [...new Set(states)], reply: reply.slice(0, 160), state: await J((t) => window.__jarvis.S.tasks[t].state, tid) };
  });
}

const ok = results.every((r) => r.ok) && !errors.length;
console.log(JSON.stringify({ ok, ready_ms: readyMs, errors, results }, null, 1));
await browser.close();
process.exit(ok ? 0 : 1);
