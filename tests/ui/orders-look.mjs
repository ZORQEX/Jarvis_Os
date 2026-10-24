// «Заказы» inside the JARVIS window: every section as the person sees it (JARVIS's header tabs + the orders pages in
// JARVIS's look, no second app chrome). Against a dev core whose orders app has data. Prints a JSON report.
// Usage: node orders-look.mjs <port> <tokenFile> <outDir> [light]
import { chromium } from "playwright-core";
import fs from "node:fs";
const [port, tokenFile, out, scheme] = process.argv.slice(2);
const token = fs.readFileSync(tokenFile, "utf8").trim();
fs.mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", headless: true });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "ru-RU", colorScheme: scheme || "dark" });
const win = await ctx.newPage();
const errors = [];
win.on("pageerror", (e) => errors.push("window: " + e));
await win.goto(`http://127.0.0.1:${port}/?t=${token}`);
await win.waitForFunction(() => window.__jarvis?.S.connected);
await win.evaluate(() => window.__jarvis.nav("orders"));
const frame = async () => { for (let i = 0; i < 100; i++) { const f = win.frames().find((x) => x !== win.mainFrame());
  if (f) return f; await win.waitForTimeout(100); } throw new Error("no orders frame"); };
await win.waitForSelector(".orders-frame", { timeout: 15000 });
const report = { sections: [], errors };
const tabs = await win.evaluate(() => [...document.querySelectorAll("[data-ord-tab]")].map((b) => b.dataset.ordTab));
for (const tab of tabs.length ? tabs : ["/board"]) {
  if (tabs.length) await win.click(`[data-ord-tab="${tab}"]`);
  await win.waitForTimeout(1500);
  const f = await frame();
  const info = await f.evaluate(() => ({
    path: location.pathname, embedded: document.documentElement.hasAttribute("data-jarvis"),
    chrome: [".shell-bar", "#copilot", ".tabbar", ".shell-foot"].filter((s) => { const e = document.querySelector(s);
      return e && getComputedStyle(e).display !== "none"; }),
    bg: getComputedStyle(document.body).backgroundColor, h1: document.querySelector("main h1")?.textContent?.trim() || "",
  })).catch((e) => ({ error: String(e) }));
  const active = await win.evaluate(() => document.querySelector("[data-ord-tab][aria-current='page']")?.dataset.ordTab || "");
  report.sections.push({ tab, active, ...info });
  await win.screenshot({ path: `${out}/orders-${(tab.replace(/\W+/g, "") || "overview")}.png` });
}
console.log(JSON.stringify(report, null, 1));
await browser.close();
