// One request typed into JARVIS's chat, the way the person does it, against a dev core: approval cards are answered
// with «Разрешить» (APPROVE=0 leaves them), the final answer, the task's steps with their times, screenshots.
// Usage: [WAIT_MS=600000] [APPROVE=1] node chat-run.mjs <port> <tokenFile> <outDir> "<request>"
import { chromium } from "playwright-core";
import fs from "node:fs";
const [port, tokenFile, out, request] = process.argv.slice(2);
const token = fs.readFileSync(tokenFile, "utf8").trim();
fs.mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", headless: true });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: "ru-RU", colorScheme: "dark" });
const win = await ctx.newPage();
const errors = [];
win.on("pageerror", (e) => errors.push("window: " + e));
await win.goto(`http://127.0.0.1:${port}/?t=${token}`);
await win.waitForFunction(() => window.__jarvis?.S.connected);
await win.evaluate(() => window.__jarvis.nav("chat"));
const before = await win.evaluate(() => window.__jarvis.S.messages.length);
const t0 = Date.now();
await win.fill("#input", request);
await win.press("#input", "Enter");
const until = Date.now() + +(process.env.WAIT_MS || 600000);
const approvals = [];
let tid = null, firstDelta = null;
while (Date.now() < until) {
  await win.waitForTimeout(250);
  const st = await win.evaluate((n) => { const S = window.__jarvis.S; const u = S.messages.slice(n).find((m) => m.role === "user");
    const t = u && S.tasks[u.task_id]; const streaming = u && S.streaming[u.task_id];
    return { tid: u?.task_id, state: t?.state, streaming: !!streaming,
      pending: Object.values(S.approvals).filter((a) => a.status === "pending" && a.task_id === u?.task_id).map((a) => ({ id: a.id, tool: a.tool_id, what: a.summary?.action })) }; }, before);
  tid = st.tid || tid;
  if (st.streaming && firstDelta === null) firstDelta = (Date.now() - t0) / 1000;
  if (st.pending.length && process.env.APPROVE !== "0") {
    for (const a of st.pending) {
      approvals.push({ ...a, at_s: (Date.now() - t0) / 1000 });
      await win.screenshot({ path: `${out}/run-approval-${approvals.length}.png` });
      await win.click(`[data-approve="${a.id}"]`).catch(() => {});
    }
  }
  if (["completed", "failed", "cancelled"].includes(st.state)) break;
}
const total = (Date.now() - t0) / 1000;
await win.waitForTimeout(500);
const res = await win.evaluate((id) => { const S = window.__jarvis.S; const t = S.tasks[id];
  const m = S.messages.filter((x) => x.task_id === id && x.role === "assistant").pop();
  return { state: t?.state, route: t?.route, steps: (t?.steps || []).map((s) => ({ tool: s.tool_id, state: s.state, ms: s.duration_ms,
    title: s.title, ver: s.verification?.status, detail: (s.verification?.detail || s.error || "").slice(0, 160) })),
    answer: m?.text, meta: m ? { route: m.meta?.route, providers: m.meta?.providers, fashion: m.meta?.fashion?.status } : null }; }, tid);
await win.screenshot({ path: `${out}/run-final.png`, fullPage: false });
console.log(JSON.stringify({ request, total_s: total, first_words_s: firstDelta, approvals, ...res, errors }, null, 1));
await browser.close();
