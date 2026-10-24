// Live check of self-improvement against a dev core: ask JARVIS to improve itself, approve the run in the window,
// wait for the result (Claude Code + JARVIS's own test run), print the messages. Usage:
//   node improve-live.mjs <port> <tokenFile> "<request>" [apply]
import { chromium } from "playwright-core";
import fs from "node:fs";
const [port, tokenFile, request, apply] = process.argv.slice(2);
const token = fs.readFileSync(tokenFile, "utf8").trim();
const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });
await page.goto(`http://127.0.0.1:${port}/?t=${token}`);
await page.waitForFunction(() => window.__jarvis?.S?.connected !== false && window.__jarvis?.link);
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(0)}s]`, ...a);
async function ask(text) {
  const r = await page.evaluate((t) => window.__jarvis.link.request("submit", { text: t, source: "text" }, 30000), text);
  log("submitted", r.task_id);
  return r.task_id;
}
async function approveAndWait(tid, maxMs) {
  const until = Date.now() + maxMs;
  let approved = new Set();
  while (Date.now() < until) {
    const s = await page.evaluate(() => ({ tasks: window.__jarvis.S.tasks, approvals: window.__jarvis.S.approvals }));
    for (const a of Object.values(s.approvals)) {
      if (a.status === "pending" && !approved.has(a.id)) {
        log("approval:", a.tool_id, "—", (a.summary?.action || "").slice(0, 160));
        await page.evaluate((id) => window.__jarvis.link.request("approve", { id }), a.id);
        approved.add(a.id);
      }
    }
    const t = s.tasks[tid];
    if (t && ["completed", "failed", "cancelled"].includes(t.state)) return t;
    await page.waitForTimeout(3000);
  }
  return null;
}
const tid = await ask(request);
const t = await approveAndWait(tid, 40 * 60 * 1000);
log("task", t?.state);
const msgs = await page.evaluate((id) => window.__jarvis.S.messages.filter((m) => m.task_id === id).map((m) => `${m.role}: ${m.text}`), tid);
console.log(msgs.join("\n---\n"));
for (const st of t?.steps || []) log("step", st.tool_id, st.state, JSON.stringify(st.verification || {}).slice(0, 300));
if (apply && t?.state === "completed") {
  const tid2 = await ask("Примени улучшение");
  const t2 = await approveAndWait(tid2, 25 * 60 * 1000);
  log("apply task", t2?.state);
  const m2 = await page.evaluate((id) => window.__jarvis.S.messages.filter((m) => m.task_id === id).map((m) => `${m.role}: ${m.text}`), tid2);
  console.log(m2.join("\n---\n"));
}
await browser.close();
