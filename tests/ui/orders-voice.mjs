// Live check of «Заказы» by voice: the main window (on «Главная») and the desktop companion with a recorded
// «microphone» (Chromium plays a WAV as the mic) against a dev core whose orders app has data. Prints, over time, the
// window's workspace, what the orders app's dock shows and the core's messages. Usage:
//   [WAIT_MS=60000] node orders-voice.mjs <port> <tokenFile> <mic.wav> <outDir>
import { chromium } from "playwright-core";
import fs from "node:fs";
const [port, tokenFile, wav, out] = process.argv.slice(2);
const token = fs.readFileSync(tokenFile, "utf8").trim();
const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", headless: true,
  args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", `--use-file-for-fake-audio-capture=${wav}`,
         "--autoplay-policy=no-user-gesture-required"] });
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const win = await ctx.newPage();
const errors = [];
win.on("pageerror", (e) => errors.push("window: " + e));
await win.goto(`http://127.0.0.1:${port}/?t=${token}`);
await win.waitForFunction(() => window.__jarvis?.S.connected);
await win.evaluate(() => window.__jarvis.nav("home"));
const comp = await ctx.newPage();
comp.on("pageerror", (e) => errors.push("companion: " + e));
await comp.goto(`http://127.0.0.1:${port}/overlay?t=${token}`);
await comp.waitForFunction(() => window.__overlay?.S.connected);
const t0 = Date.now();
await comp.evaluate(() => window.__overlay.link.request("overlay.settings", { wake: true }));
const heard = [];                          // what the companion sent and what the core answered (probe / commit)
await comp.exposeFunction("heard", (x) => heard.push([((Date.now() - t0) / 1000).toFixed(1) + "s", x]));
await comp.evaluate(() => { const l = window.__overlay.link; const r = l.request.bind(l);
  l.request = async (type, data) => { const out = await r(type, data);
    if (/^voice\.(probe|commit|transcribe)/.test(type)) window.heard(type + " " + JSON.stringify(out).slice(0, 160)); return out; }; });
const timeline = [];
let last = "";
const until = Date.now() + +(process.env.WAIT_MS || 60000);
let shot = 0;
while (Date.now() < until) {
  await win.waitForTimeout(1000);
  const view = await win.evaluate(() => window.__jarvis.S.view);
  const frame = win.frames().find((f) => f.url().includes(":8798") || f.url().includes("/board"));
  const dock = frame ? await frame.evaluate(() => document.querySelector("#copilot-messages")?.innerText || "").catch(() => "") : "";
  const card = frame ? await frame.evaluate(() => [...document.querySelectorAll("[data-proposal]")].map((c) => c.dataset.status || "pending").join(",")).catch(() => "") : "";
  const now = `${view} | card:${card} | dock:${dock.replace(/\s+/g, " ").slice(-160)}`;
  if (now !== last) { timeline.push([((Date.now() - t0) / 1000).toFixed(0) + "s", now]); last = now; await win.screenshot({ path: `${out}/orders-voice-${++shot}.png` }); }
}
const msgs = await win.evaluate(() => window.__jarvis.S.messages.slice(-6).map((m) => `${m.role}: ${m.text.slice(0, 140)}${m.meta?.route ? " [" + m.meta.route + "]" : ""}`));
console.log(JSON.stringify({ heard, timeline, msgs, errors }, null, 1));
await comp.evaluate(() => window.__overlay.link.request("overlay.settings", { wake: false }));
await browser.close();
