// Full-system QA runner: drives a running JARVIS through its window connection exactly like a user typing,
// records real timings and outcomes, answers confirmations, and leaves the checks of side effects to the caller.
// Usage: node qa.mjs <port> <tokenFile> <outDir> <section> [fakeAudioWav]
import { chromium } from "playwright-core";
import fs from "node:fs";

const [port, tokenFile, outDir, section, fakeAudio] = process.argv.slice(2);
fs.mkdirSync(outDir, { recursive: true });
const args = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"];
if (fakeAudio) args.push(`--use-file-for-fake-audio-capture=${fakeAudio}%noloop`);
const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", headless: true, args });
const page = await browser.newPage({ viewport: { width: 1280, height: 860 }, locale: "ru-RU" });
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
await page.goto(`http://127.0.0.1:${port}/?t=${fs.readFileSync(tokenFile, "utf8").trim()}`);
await page.waitForFunction(() => window.__jarvis?.S.connected === true, null, { timeout: 10000 });
await page.evaluate(() => window.__jarvis.nav("chat"));   // the window opens on Home; QA drives the conversation
await page.evaluate(() => {
  const q = window.__qa = { delta: {}, events: [], audio: {}, states: [] };
  const L = window.__jarvis.link, orig = L.onEvent;
  L.onEvent = (ev) => {
    const t = performance.timeOrigin + performance.now();
    if (ev.kind === "assistant.delta" && ev.task_id && !q.delta[ev.task_id]) q.delta[ev.task_id] = t;
    if (ev.kind === "speech.audio" && ev.task_id) (q.audio[ev.task_id] ||= []).push(t);
    if (["approval.created", "task.updated", "speech.end", "speech.cancelled"].includes(ev.kind)) q.events.push([t, ev.kind, ev.task_id]);
    orig(ev);
  };
  const v = window.__jarvis.voice, os = v.onState;
  v.onState = (s) => { q.states.push([performance.timeOrigin + performance.now(), s]); os(s); };
});
const J = (f, a) => page.evaluate(f, a);
const sleep = (ms) => page.waitForTimeout(ms);
const results = [];

let SOURCE = "text";
async function submit(text) {
  return J(async ([t, s]) => (await window.__jarvis.link.request("submit", { text: t, source: s })).task_id || "", [text, SOURCE]);
}
async function lastAssistant(tid) {
  return J((t) => window.__jarvis.S.messages.filter((m) => (t ? m.task_id === t : true) && m.role === "assistant").map((m) => m.text).join("\n"), tid);
}
// Run one request to the end. approve: true → «Да» to every confirmation, false → «Нет», null → leave pending.
async function run(name, text, { approve = true, timeout = 240000, expect = null } = {}) {
  const t0 = Date.now();
  const before = await J(() => window.__jarvis.S.messages.length);
  const tid = await submit(text);
  const rec = { name, text, tid, approvals: [], ok: null };
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const st = await J((t) => {
      const S = window.__jarvis.S;
      const task = t ? S.tasks[t] : null;
      const pend = Object.values(S.approvals).filter((a) => a.status === "pending" && (!t || a.task_id === t));
      return { state: task?.state || (t ? "unknown" : "control"), pend: pend.map((a) => ({ id: a.id, tool: a.tool_id, action: a.summary?.action })) };
    }, tid);
    if (st.pend.length && approve !== null) {
      for (const a of st.pend) if (!rec.approvals.some((x) => x.id === a.id)) rec.approvals.push({ ...a, at_ms: Date.now() - t0 });
      await submit(approve ? "Да" : "Нет");
      await sleep(600);
      continue;
    }
    if (st.pend.length && approve === null) { rec.approvals.push(...st.pend); rec.state = "waiting_for_approval"; break; }
    if (!tid) { await sleep(800); rec.state = "control"; break; }
    if (["completed", "failed", "cancelled", "blocked"].includes(st.state)) { rec.state = st.state; break; }
    await sleep(250);
  }
  rec.total_ms = Date.now() - t0;
  const q = await J((t) => ({ d: window.__qa.delta[t], a: (window.__qa.audio[t] || [])[0] }), tid);
  const tSubmit = t0;
  rec.first_text_ms = q.d ? Math.round(q.d - tSubmit) : null;
  rec.first_audio_ms = q.a ? Math.round(q.a - tSubmit) : null;
  rec.reply = tid ? await lastAssistant(tid) : (await J((n) => window.__jarvis.S.messages.slice(n).filter((m) => m.role === "assistant").map((m) => m.text).join("\n"), before));
  rec.route = await J((t) => window.__jarvis.S.tasks[t]?.route || null, tid);
  rec.steps = await J((t) => (window.__jarvis.S.tasks[t]?.steps || []).map((s) => `${s.tool_id}:${s.state}`), tid);
  if (rec.state === undefined) rec.state = "timeout";
  if (expect) rec.ok = await expect(rec);
  results.push(rec);
  console.log(`\n▶ ${name} [${rec.state}] ${(rec.total_ms / 1000).toFixed(1)} с · первый текст ${rec.first_text_ms ?? "—"} мс\n  шаги: ${rec.steps.join(", ") || "—"}${rec.approvals.length ? `\n  подтверждения: ${rec.approvals.map((a) => a.tool + " — " + (a.action || "").slice(0, 120)).join(" | ")}` : ""}\n  ответ: ${rec.reply.slice(0, 400).replace(/\n+/g, " ⏎ ")}`);
  return rec;
}

const sections = {};

sections.chat = async () => {
  await run("4. Обычный вопрос", "Джарвис, расскажи кратко, что такое искусственный интеллект.");
  await run("21a. Контекст: вопрос", "Кто написал роман «Война и мир»?");
  await run("21b. Контекст: продолжение", "А в каком году он родился?");
  await run("22. Несуществующий файл", "Открой файл /JARVIS_THIS_FILE_DOES_NOT_EXIST.txt");
};

sections.speed = async () => {
  const qs = ["Сколько будет 17 умножить на 23?", "Какая столица Австралии?", "Назови три цвета радуги.", "Что такое фотосинтез, в двух словах?",
    "Почему небо голубое? Одним предложением.", "Сколько планет в Солнечной системе?", "Переведи на английский: доброе утро.",
    "Посоветуй, что почитать вечером, одну книгу.", "Сколько сейчас времени?", "Как будет «спасибо» по-японски?"];
  for (const [i, q] of qs.entries()) await run(`25.${i + 1} Скорость`, q);
};

sections.computer = async () => {
  await run("8a. Папка на рабочем столе", "Джарвис, создай на рабочем столе папку JARVIS_TEST.");
  await run("8b. Файл внутри", "Создай внутри неё файл test.txt.");
  await run("8c. Записать текст", "Запиши в него: JARVIS SYSTEM TEST.");
  await run("8d. Открыть файл", "Открой этот файл.");
  await run("9. Терминал: содержимое", "Покажи содержимое папки JARVIS_TEST.");
};

sections.site = async () => {
  await run("10. Сайт-визитка", "Джарвис, создай простой современный сайт-визитку для мебельной компании.", { timeout: 1500000 });
  await run("11a. Каталог", "Добавь на сайт каталог мебели с карточками товаров.", { timeout: 1500000 });
  await run("11b. Кнопка", "Добавь кнопку «Связаться с нами».", { timeout: 1500000 });
  await run("21c. Тёмный фон", "Сделай фон тёмным.", { timeout: 1500000 });
};

sections.catalog = async () => {
  await run("11a. Каталог (повтор)", "Добавь на сайт мебельной компании каталог мебели с карточками товаров.", { timeout: 1500000 });
};

sections.multistep = async () => {
  await run("20. Многошаговая", "Создай сайт-визитку для мебельной компании «Дуб и Лён», запусти его и открой в браузере.", { timeout: 1500000 });
};

sections.browser = async () => {
  await run("12. Браузер: поиск", "Открой браузер и найди информацию о современных моделях мебели.", { timeout: 900000 });
};

sections.comms_real = async () => {
  // Real accounts: messages ONLY to the user themself; the one message to another person is declined at the card.
  await run("13a. Открыть Telegram", "Открой Telegram.");
  await run("13b. Найти контакт (себя)", "Найди в Telegram контакт Избранное.");
  await run("13c. Telegram себе", "Отправь себе в Telegram: Это тест JARVIS.", { approve: null, timeout: 90000 });
  await run("14a. Открыть WhatsApp", "Открой WhatsApp.");
  await run("14b. WhatsApp себе", "Отправь себе в WhatsApp: Это тест JARVIS.", { approve: null, timeout: 150000 });
  await run("15. Письмо себе", "Отправь письмо на timkst8@gmail.com. Тема: JARVIS Test. Текст: Это тестовая проверка JARVIS.", { approve: null, timeout: 120000 });
  await run("23c. Другому человеку — отказ", "Отправь маме в WhatsApp: тест подтверждения", { approve: false, timeout: 150000 });
};

sections.email_real = async () => {
  await run("15. Письмо себе (повтор)", "Отправь письмо на timkst8@gmail.com. Тема: JARVIS Test. Текст: Это тестовая проверка JARVIS.", { approve: null, timeout: 120000 });
  await run("13b. Найти контакт (повтор)", "Найди в Telegram контакт Избранное.", { approve: null, timeout: 120000 });
};

sections.voice_speed = async () => {
  SOURCE = "voice";          // spoken turn (as after speech recognition): fast model, spoken answer
  for (const [i, q] of ["Что такое чёрная дыра?", "Почему небо голубое?", "Назови три самые высокие горы.",
    "Как будет «кошка» по-английски?", "Посоветуй фильм на вечер.", "Что такое фотосинтез?"].entries())
    await run(`25v.${i + 1} Голос`, q);
  SOURCE = "text";
};

sections.regress = async () => {
  await run("R1 Беседа", "Джарвис, расскажи кратко, что такое искусственный интеллект.");
  await run("R2 Контекст", "А кто придумал этот термин?");
  await run("R3 Время локально", "Сколько сейчас времени?");
  await run("R4 Нет файла", "Открой файл /JARVIS_THIS_FILE_DOES_NOT_EXIST.txt");
  await run("R5 Папка", "Создай на рабочем столе папку JARVIS_REGRESS.");
  await run("R6 Файл", "Запиши в неё файл notes.txt с текстом Регрессия пройдена.");
  await run("R7 Содержимое", "Что лежит в этой папке?");
  await run("R8 Терминал", "Покажи в терминале, сколько свободного места на диске.");
  await run("R9 План", "Запланируй через 15 минут создать файл late.txt в той же папке.");
  await run("R10 Отмена плана", "Отмени эту задачу.");
  await run("R11 Удаление (подтверждаю)", "Удали папку JARVIS_REGRESS с рабочего стола.");
  await run("R12 Арифметика", "Сколько будет 25 умножить на 4?");
};

sections.full = async () => {
  console.log("access:", JSON.stringify(await J(async () => await window.__jarvis.link.request("access.set", { level: "full" }))));
};

sections.sched = async () => {
  await run("16. Запланировать файл", "Джарвис, запланируй через 2 минуты создание файла scheduler_test.txt с текстом Scheduler works.");
  await run("18a. Задача на 10 минут", "Запланируй через 10 минут создание файла cancel_test.txt.");
  await run("18b. Отмена", "Отмени эту задачу.");
  await run("19a. Задача для изменения", "Запланируй через 20 минут создание файла edit_test.txt.");
  await run("19b. Изменить время", "Измени время выполнения на 5 минут.");
};

sections.perm = async () => {
  await run("23a. Удаление: отказ", "Удали папку JARVIS_TEST с рабочего стола.", { approve: false });
  await run("23b. Опасная команда", "Выполни в терминале команду rm -rf ~/Documents", { approve: false });
  await run("23c. Сообщение другому: отказ", "Отправь Ивану Петрову в Telegram: тест", { approve: false, timeout: 60000 });
};

sections.cancel = async () => {
  const tid = await submit("Создай большой сайт-портфолио с десятью страницами для фотографа, с галереями и блогом.");
  const t0 = Date.now();
  for (let i = 0; i < 120; i++) {
    const p = await J(() => Object.values(window.__jarvis.S.approvals).find((a) => a.status === "pending"));
    if (p) { await submit("Да"); break; }
    await sleep(500);
  }
  await page.waitForFunction((t) => (window.__jarvis.S.tasks[t]?.steps || []).some((s) => s.state === "running" && s.tool_id !== "agent_chat"), tid, { timeout: 120000 }).catch(() => {});
  await sleep(8000);
  const ts = Date.now();
  await submit("Стоп.");
  await page.waitForFunction((t) => ["cancelled", "failed", "completed"].includes(window.__jarvis.S.tasks[t]?.state), tid, { timeout: 60000 }).catch(() => {});
  const rec = { name: "24. Отмена длинной задачи «Стоп»", tid, stop_to_end_ms: Date.now() - ts, state: await J((t) => window.__jarvis.S.tasks[t]?.state, tid),
    steps: await J((t) => (window.__jarvis.S.tasks[t]?.steps || []).map((s) => `${s.tool_id}:${s.state}`), tid), core: await J(() => window.__jarvis.S.core.state) };
  await sleep(3000);
  rec.core_after = await J(() => window.__jarvis.S.core.state);
  results.push(rec);
  console.log("\n▶", JSON.stringify(rec));
};

sections.voice = async () => {
  // The fake microphone plays: «Джарвис, сколько будет 25 умножить на 4?», silence, then the long question, then «Стоп».
  await J(async () => await window.__jarvis.link.request("submit", { text: "новый разговор", source: "text" }));
  await sleep(1500);
  const t0 = Date.now();
  await page.click("#convBtn");
  await page.waitForFunction(() => window.__jarvis.S.messages.filter((m) => m.role === "user").length >= 1, null, { timeout: 60000 });
  // the recording: question 1, question 2, then «Стоп» ~10 s after question 2 while JARVIS is talking
  await page.waitForFunction(() => window.__jarvis.S.messages.filter((m) => m.role === "user").length >= 3, null, { timeout: 90000 }).catch(() => {});
  await sleep(8000);
  await page.click("#convBtn").catch(() => {});
  const turns = await J(async () => (await window.__jarvis.link.request("voice.turns", { limit: 6 })).map((t) => ({ text: t.transcript, route: t.route, ms: t.ms, task: t.task_id })));
  const msgs = await J(() => window.__jarvis.S.messages.map((m) => ({ role: m.role, text: m.text.slice(0, 300), task: m.task_id, src: m.meta?.source })));
  const states = await J(() => window.__qa.states.map(([t, s]) => [Math.round(t - window.__qa.states[0][0]), s]));
  const audio = await J(() => Object.fromEntries(Object.entries(window.__qa.audio).map(([k, v]) => [k, v.length])));
  const events = await J(() => window.__qa.events.filter((e) => e[1].startsWith("speech")).map((e) => [e[1], e[2]]));
  results.push({ name: "5–7. Голосовой цикл", turns, msgs, states, audio, events, total_ms: Date.now() - t0 });
  console.log(JSON.stringify({ turns, msgs, states, audio, events }, null, 1));
};

sections.skills = async () => {
  // Self-development: JARVIS has no QR tool; it must build a skill (find + install + wrap + test) and use it.
  await run("S1. Новый навык и его применение", "Научись делать QR-код из любого текста и сделай QR-код со ссылкой https://example.com", { timeout: 1800000 });
  await run("S2. Навык уже есть", "Сделай QR-код с текстом Привет от JARVIS", { timeout: 600000 });
};

await sections[section]();
await page.screenshot({ path: `${outDir}/${section}.png` });
const state = await J(() => window.__jarvis.S.core);
fs.writeFileSync(`${outDir}/${section}.json`, JSON.stringify({ results, errors, state }, null, 1));
console.log(`\nошибки консоли: ${errors.length ? errors.join(" | ") : "нет"} · состояние: ${state.state}`);
await browser.close();
