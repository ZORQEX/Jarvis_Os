// «Заказы» answered in JARVIS's chat: a typed request about orders → the module's answer (its rules, or JARVIS's
// conversation on the module's tools) with the «Подтвердить» card in the chat → the click → saved, card closed, the
// module page reloaded with the new figures. Against a dev core whose orders module has data (synthetic).
// Usage: node orders-card.mjs <port> <tokenFile> <outDir> "<request>" [decide=commit|cancel|none]
import { chromium } from "playwright-core";
import fs from "node:fs";
const [port, tokenFile, out, request, decide = "commit"] = process.argv.slice(2);
const token = fs.readFileSync(tokenFile, "utf8").trim();
fs.mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", headless: true });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "ru-RU", colorScheme: "dark" });
const win = await ctx.newPage();
const errors = [];
win.on("pageerror", (e) => errors.push("window: " + e));
await win.goto(`http://127.0.0.1:${port}/?t=${token}`);
await win.waitForFunction(() => window.__jarvis?.S.connected);
await win.evaluate(() => window.__jarvis.nav("chat"));
const t0 = Date.now();
await win.fill("#input", request);
await win.press("#input", "Enter");
const report = { request, errors };
const last = () => win.evaluate(() => { const m = window.__jarvis.S.messages.filter((x) => x.role === "assistant").pop();
  return m ? { id: m.id, text: m.text, route: m.meta?.route, fashion: m.meta?.fashion, by: m.meta?.by } : null; });
await win.waitForFunction((n) => window.__jarvis.S.messages.filter((x) => x.role === "assistant").length > n,
  await win.evaluate(() => window.__jarvis.S.messages.filter((x) => x.role === "assistant").length), { timeout: 240000 });
report.answer_s = (Date.now() - t0) / 1000;
report.answer = await last();
await win.waitForTimeout(400);
await win.screenshot({ path: `${out}/card-1-answer.png` });
const btn = win.locator(`[data-order-decide="${decide}"]`).last();
report.card = await btn.count() > 0;
if (report.card && decide !== "none") {
  await btn.click();
  await win.waitForFunction((d) => { const m = window.__jarvis.S.messages.filter((x) => x.meta?.fashion?.card).pop();
    return m && m.meta.fashion.status === (d === "commit" ? "committed" : "cancelled"); }, decide, { timeout: 60000 });
  await win.waitForTimeout(800);
  report.after = await last();
  report.toast = await win.evaluate(() => [...document.querySelectorAll(".toast")].map((t) => t.textContent.trim()).slice(-2));
  await win.screenshot({ path: `${out}/card-2-decided.png` });
}
console.log(JSON.stringify(report, null, 1));
await browser.close();
