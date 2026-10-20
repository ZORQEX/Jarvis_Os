// Measure browser CPU while the page is idle, with and without CSS animations.
import { chromium } from "playwright-core";
import fs from "node:fs";
const [port, tokenFile] = process.argv.slice(2);
const token = fs.readFileSync(tokenFile, "utf8").trim();
const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", headless: true });
const page = await (await browser.newContext({ viewport: { width: 1177, height: 638 } })).newPage();
await page.goto(`http://127.0.0.1:${port}/?t=${token}`);
await page.waitForFunction(() => window.__jarvis?.S.connected);
const cdp = await page.context().newCDPSession(page);
await cdp.send("Performance.enable");
async function measure(label) {
  await page.waitForTimeout(1500);
  const m0 = Object.fromEntries((await cdp.send("Performance.getMetrics")).metrics.map((m) => [m.name, m.value]));
  await page.waitForTimeout(8000);
  const m1 = Object.fromEntries((await cdp.send("Performance.getMetrics")).metrics.map((m) => [m.name, m.value]));
  const busy = (k) => ((m1[k] - m0[k]) / 8 * 100).toFixed(2);
  console.log(`${label}: script ${busy("ScriptDuration")}% layout ${busy("LayoutDuration")}% style ${busy("RecalcStyleDuration")}% task ${busy("TaskDuration")}% frames ${m1.Frames - m0.Frames}`);
}
await measure("idle (animations on)");
await page.evaluate(() => document.documentElement.classList.add("anim-off"));
await measure("idle (animations off)");
await browser.close();
