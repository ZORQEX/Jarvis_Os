// Live check of the desktop companion with a recorded «microphone»: Chromium plays a WAV as the mic input
// (e.g. typing + «Джарвис, который час?» + a second command while JARVIS answers) against a dev core; prints the voice
// states. Then read voice_turns in the dev DB. Usage: [PREVIEW=aidar] [WAIT_MS=15000] node voice-mic.mjs <port> <tokenFile> <mic.wav>
import { chromium } from "playwright-core";
import fs from "node:fs";
const [port, tokenFile, wav] = process.argv.slice(2);
const token = fs.readFileSync(tokenFile, "utf8").trim();
const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", headless: true,
  args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", `--use-file-for-fake-audio-capture=${wav}`,
         "--autoplay-policy=no-user-gesture-required"] });
const page = await browser.newPage();
const log = [];
page.on("console", (m) => log.push(m.text()));
await page.goto(`http://127.0.0.1:${port}/overlay?t=${token}`);
await page.waitForFunction(() => window.__overlay?.S.connected);
const t0 = Date.now();
await page.evaluate(() => window.__overlay.link.request("overlay.settings", { wake: true }));
// PREVIEW=<voice id>: the main window plays its voice sample at the same time (the companion must not take it as a command)
if (process.env.PREVIEW) await page.evaluate((v) => window.__overlay.link.request("voice.preview", { voice: v }), process.env.PREVIEW);
const states = [];
await page.exposeFunction("rec", (s) => states.push([Date.now() - t0, s]));
await page.evaluate(() => { const v = window.__overlay.voice; const o = v.onState; v.onState = (s) => { window.rec(s); o(s); }; });
await page.waitForTimeout(+(process.env.WAIT_MS || 15000));
console.log(JSON.stringify({ states, own: [...(await page.evaluate(() => [...window.__overlay.S.own]))] }));
await page.evaluate(() => window.__overlay.link.request("overlay.settings", { wake: false }));
await browser.close();
