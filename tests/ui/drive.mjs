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
  await say("Джарвис, проверь почту. Что важного пришло по Флоре?", 1500);
  await shot("flora-01-chat");
  let s = Date.now();
  await page.click('text=Открыть проект');
  await page.waitForSelector(".mail-view h2", { timeout: 5000 });
  timings.flora_open_ms = Date.now() - s;
  await page.waitForTimeout(300);
  await shot("flora-02-decisions");
  await page.click('[data-fl-item]:has-text("W-3")');
  await page.waitForTimeout(200);
  await page.click(".fact >> nth=0");
  await page.waitForTimeout(500);
  await shot("flora-03-window-item");
  await page.click('[data-fl-tab="drawing"]');
  await page.waitForTimeout(700);
  await shot("flora-04-drawing-compare");
  await page.click('[data-fl-mode="C"]');
  await page.waitForTimeout(400);
  await page.click('[data-fl-tool="rect"]');
  const box = await page.locator("#viewer").boundingBox();
  const at = (fx, fy) => [box.x + box.width * fx, box.y + box.height * fy];
  let [x1, y1] = at(0.32, 0.72), [x2, y2] = at(0.62, 0.86);
  await page.mouse.move(x1, y1); await page.mouse.down(); await page.mouse.move(x2, y2, { steps: 6 }); await page.mouse.up();
  await page.click('[data-fl-tool="arrow"]');
  [x1, y1] = at(0.75, 0.55); [x2, y2] = at(0.6, 0.76);
  await page.mouse.move(x1, y1); await page.mouse.down(); await page.mouse.move(x2, y2, { steps: 6 }); await page.mouse.up();
  await page.click('[data-fl-tool="text"]');
  [x1, y1] = at(0.66, 0.5);
  await page.mouse.click(x1, y1);
  await page.keyboard.type("Высота 2100 или 2000?");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(900);
  await shot("flora-05-annotated");
  await page.click('[data-fl-tab="quotes"]');
  await page.waitForTimeout(400);
  await shot("flora-06-quotes");
  await page.click('[data-fl-tab="decisions"]');
  await page.click('[data-fl-item]:has-text("W-3")');
  await page.click('[data-fl-compose="clarify"]');
  await page.waitForSelector("#drBody");
  await page.click("#drBody");
  await page.keyboard.press("Control+End");
  await page.keyboard.type("\nМарина");
  await page.waitForTimeout(1200);
  await shot("flora-07-draft");
  await page.click('[data-fl="send"]');
  await page.waitForSelector(".approval [data-approve]", { timeout: 5000 });
  await page.waitForTimeout(300);
  await shot("flora-08-approval");
  await page.click(".flora .approval [data-approve]");
  await page.waitForTimeout(1500);
  await shot("flora-09-sent");
  await page.click('[data-fl-tab="journal"]');
  await page.waitForTimeout(400);
  await shot("flora-10-journal");
}

console.log(JSON.stringify({ errors, timings, state: await page.evaluate(() => window.__jarvis.S.core) }, null, 1));
await browser.close();
