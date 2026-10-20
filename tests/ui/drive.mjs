// UI driver: opens the real JARVIS page in system Chromium (headless), runs a
// scenario, captures screenshots and console errors.
// Usage: node drive.mjs <port> <tokenFile> <outDir> [scenario] [width] [height] [scheme]
import { chromium } from "playwright-core";
import fs from "node:fs";

const [port, tokenFile, outDir, scenario = "smoke", w = "1240", h = "800", scheme = "dark"] = process.argv.slice(2);
const token = fs.readFileSync(tokenFile, "utf8").trim();
fs.mkdirSync(outDir, { recursive: true });
const exe = ["/usr/bin/chromium", "/usr/bin/google-chrome-stable"].find((p) => fs.existsSync(p));
const browser = await chromium.launch({ executablePath: exe, headless: true, args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"] });
const ctx = await browser.newContext({ viewport: { width: +w, height: +h }, colorScheme: scheme, deviceScaleFactor: 1.5, locale: "ru-RU" });
const page = await ctx.newPage();
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

const t0 = Date.now();
await page.goto(`http://127.0.0.1:${port}/?t=${token}`);
await page.waitForFunction(() => window.__jarvis?.S.connected === true, null, { timeout: 8000 });
const ready = Date.now() - t0;
const shot = async (name) => page.screenshot({ path: `${outDir}/${name}.png` });
const say = async (text, waitMs = 1500) => {
  await page.fill("#input", text);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(waitMs);
};
const timings = { ready_ms: ready };

if (scenario === "smoke") {
  await page.waitForTimeout(400);
  await shot("01-empty");
  let s = Date.now();
  await say("Состояние системы", 50);
  await page.waitForSelector(".stats", { timeout: 5000 });
  timings.system_info_visible_ms = Date.now() - s;
  await page.waitForTimeout(600);
  await shot("02-system");
  await say("найди файл jarvis", 1200);
  await shot("03-search");
  await say("запиши заметку: позвонить архитектору по окнам до пятницы", 1200);
  await shot("04-approval");
  await page.click("[data-approve]");
  await page.waitForTimeout(1200);
  await shot("05-approved");
  await say("какие окна открыты", 1000);
  await say("расскажи о погоде", 1000);
  await shot("06-more");
  await page.click('[data-nav="tasks"]');
  await page.waitForTimeout(600);
  await shot("07-tasks");
  await page.click('[data-nav="settings"]');
  await page.waitForTimeout(400);
  await shot("08-settings");
}
if (scenario === "approval-deny") {
  await say("запиши заметку: это не должно сохраниться", 1000);
  await page.click("[data-deny]");
  await page.waitForTimeout(800);
  await shot("deny");
}
if (scenario === "view") {
  await page.waitForTimeout(800);
  await shot("view");
}
if (scenario === "flora") {
  await say("Джарвис, проверь почту. Что важного пришло по Флоре?", 2500);
  await shot("flora-01-inbox");
  await page.click('[data-nav="flora"]');
  await page.waitForTimeout(800);
  await shot("flora-02-view");
}

console.log(JSON.stringify({ errors, timings, state: await page.evaluate(() => window.__jarvis.S.core) }, null, 1));
await browser.close();
