// UI driver: opens the real JARVIS page in system Chromium (headless), runs a
// scenario, captures screenshots and console errors.
// Usage: node drive.mjs <port> <tokenFile> <outDir> [scenario] [width] [height] [scheme]
import { chromium } from "playwright-core";
import fs from "node:fs";

const [port, tokenFile, outDir, scenario = "smoke", w = "1240", h = "800", scheme = "dark"] = process.argv.slice(2);
const token = fs.readFileSync(tokenFile, "utf8").trim();
fs.mkdirSync(outDir, { recursive: true });
const exe = ["/usr/bin/chromium", "/usr/bin/google-chrome-stable"].find((p) => fs.existsSync(p));
const fakeAudio = process.env.FAKE_AUDIO ? [`--use-file-for-fake-audio-capture=${process.env.FAKE_AUDIO}`] : [];
const browser = await chromium.launch({ executablePath: exe, headless: true,
  args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required", ...fakeAudio] });
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
  await page.click('[data-nav="history"]');
  await page.waitForTimeout(600);
  timings.history = await page.$$eval(".action .action-title b", (els) => els.map((e) => e.textContent));
  timings.recent_list_present = !!(await page.$("#recentTasks"));
  await shot("07-history");
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

if (scenario === "agency") {
  const dir = process.env.CALC_DIR;
  await page.click('[data-nav="settings"]');
  await page.waitForSelector(".pcard");
  await page.waitForTimeout(300);
  await shot("ag-01-settings");
  let s = Date.now();
  await page.click('.pcard[data-provider="claude_code"] [data-check]');
  await page.waitForFunction(() => window.__jarvis.S.providers.find((p) => p.id === "claude_code")?.health === "CONNECTED", null, { timeout: 120000 });
  timings.claude_live_check_ms = Date.now() - s;
  await page.waitForTimeout(400);
  await page.locator('.pcard[data-provider="claude_code"]').screenshot({ path: `${outDir}/ag-02-claude-connected.png` });
  await page.click('[data-nav="chat"]');
  s = Date.now();
  await say("Claude, посмотри код в calc и объясни, почему падает тест", 500);
  await page.waitForFunction(() => { const S = window.__jarvis.S; const t = Object.values(S.tasks).at(-1); return t && ["completed", "failed"].includes(t.state); }, null, { timeout: 180000 });
  timings.claude_inspect_ms = Date.now() - s;
  await page.waitForTimeout(500);
  await shot("ag-03-inspect");
  s = Date.now();
  await say("Исправь ошибку в коде в calc и запусти тесты", 800);
  await page.waitForSelector(".approval [data-approve]", { timeout: 20000 });
  await page.waitForTimeout(300);
  await shot("ag-04-approval");
  await page.click(".approval [data-approve]");
  await page.waitForFunction(() => { const S = window.__jarvis.S; const t = Object.values(S.tasks).at(-1); return t && ["completed", "failed"].includes(t.state); }, null, { timeout: 300000 });
  timings.claude_change_ms = Date.now() - s;
  await page.waitForTimeout(700);
  await shot("ag-05-change-done");
  await say("Найди в интернете поставщиков мрамора и напиши им письмо", 1500);
  await shot("ag-06-no-cloud-providers");
  await page.evaluate(() => { localStorage.setItem("jarvis.dev", "1"); });
  await page.reload();
  await page.waitForFunction(() => window.__jarvis?.S.connected === true);
  await page.waitForTimeout(800);
  await shot("ag-07-dev-inspector");
}

if (scenario === "two") {
  const waitDone = async (ms = 240000) => page.waitForFunction(() => { const t = Object.values(window.__jarvis.S.tasks).at(-1);
    return t && ["completed", "failed", "cancelled"].includes(t.state); }, null, { timeout: ms });
  await page.click('[data-nav="settings"]');
  await page.waitForSelector(".pcard");
  await page.waitForTimeout(400);
  await shot("two-01-settings");
  timings.provider_cards = await page.locator(".pcard").count();
  await page.click('[data-nav="chat"]');
  let s = Date.now();
  await say("Привет! Расскажи в двух предложениях, что ты умеешь.", 300);
  await waitDone(); timings.chat_ms = Date.now() - s;
  s = Date.now();
  await say("Напиши письмо поставщику мрамора с просьбой прислать прайс-лист", 300);
  await waitDone(); timings.letter_ms = Date.now() - s;
  await page.waitForTimeout(400);
  await shot("two-02-letter");
  s = Date.now();
  await say("Найди в интернете информацию о компании Basecamp и сохрани результаты в отдельный файл", 500);
  await page.waitForSelector(".approval [data-approve]", { timeout: 180000 });
  timings.research_until_approval_ms = Date.now() - s;
  await page.waitForTimeout(300);
  await shot("two-03-approval");
  await page.click(".approval [data-approve]");
  await waitDone(); timings.research_and_file_ms = Date.now() - s;
  await page.waitForTimeout(600);
  await shot("two-04-file-saved");
}

if (scenario === "comms") {
  // Honest states without accounts: the view, the connect forms, and a send command that must NOT pretend.
  await page.click('[data-nav="comms"]');
  await page.waitForSelector("#caccounts .pcard", { timeout: 5000 });
  await page.waitForTimeout(300);
  await shot("comms-01-view");
  timings.cards = await page.$$eval("#caccounts .pcard", (els) => els.map((e) => e.innerText.split("\n").slice(0, 2).join(" | ")));
  await page.click('[data-nav="chat"]');
  let s = Date.now();
  await say("Отправь Ивану в Telegram: привет", 50);
  await page.waitForFunction(() => /не подключён/.test(document.querySelector("#thread")?.innerText || ""), null, { timeout: 15000 });
  timings.not_connected_reply_ms = Date.now() - s;
  await page.waitForTimeout(300);
  await shot("comms-02-not-connected");
  // Render check of the confirmation card for a message (UI only; no send happens here).
  await page.evaluate(() => {
    const { S } = window.__jarvis;
    const m = S.messages[S.messages.length - 1];
    const a = { id: "a_ui", task_id: m.task_id, tool_id: "message_send", status: "pending", risk: "high",
      args: { deliveries: [{ channel: "telegram", to_id: "u:1:1", to_name: "Иван Петров", to_address: "@ivan" }],
              body: "Привет, буду через час.", due_at: Date.now() / 1000 + 3600 },
      summary: { title: "Отправка сообщения", action: "Запланировать: Иван Петров (@ivan) в Telegram", impact: "Сообщение уйдёт от вашего имени.",
                 side_effects: "external" } };
    S.approvals[a.id] = a;
    window.__jarvis.render();
  });
  await page.waitForSelector(".msgcard", { timeout: 3000 });
  await page.click(".revise summary");
  await page.waitForTimeout(200);
  await page.locator(".msgcard").scrollIntoViewIfNeeded();
  await shot("comms-03-confirm-card");
}

if (scenario === "commsqr") {
  // WhatsApp linking: JARVIS opens WhatsApp Web in its own profile and shows the real QR code (not scanned here).
  await page.click('[data-nav="comms"]');
  await page.waitForSelector('[data-ca="wa-link"]', { timeout: 5000 });
  const s = Date.now();
  await page.click('[data-ca="wa-link"]');
  await page.waitForSelector("img.qr", { timeout: 60000 });
  timings.qr_visible_ms = Date.now() - s;
  await page.locator("img.qr").scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  await shot("comms-04-whatsapp-qr");
}

if (scenario === "voiceui") {
  // The fake microphone plays FAKE_AUDIO (a spoken question followed by silence) in a loop.
  await page.evaluate(() => {
    window.__voiceLog = [];
    const J = window.__jarvis;
    const orig = J.voice.onState;
    J.voice.onState = (s) => { window.__voiceLog.push([Math.round(performance.now()), s]); orig(s); };
  });
  const t0 = Date.now();
  await page.click("#convBtn");
  await page.waitForFunction(() => window.__voiceLog.some(([, s]) => s === "speaking"), null, { timeout: 60000 });
  timings.until_speaking_ms = Date.now() - t0;
  await page.waitForTimeout(400);
  await shot("voice-01-speaking");
  // The loop repeats the question while JARVIS talks: this exercises barge-in.
  await page.waitForFunction(() => window.__voiceLog.some(([, s]) => s === "interrupted"), null, { timeout: 30000 }).catch(() => {});
  await page.waitForTimeout(600);
  await shot("voice-02-after");
  await page.click("#convBtn");
  timings.voice_states = await page.evaluate(() => window.__voiceLog.map(([t, s]) => `${t}:${s}`).join(" "));
  timings.user_msgs = await page.evaluate(() => window.__jarvis.S.messages.filter((m) => m.role === "user").map((m) => m.text));
  timings.turns = await page.evaluate(async () => (await window.__jarvis.link.request("voice.turns", { limit: 3 })).map((t) => ({ text: t.transcript, ms: t.ms })));
}

console.log(JSON.stringify({ errors, timings, state: await page.evaluate(() => window.__jarvis.S.core) }, null, 1));
await browser.close();
