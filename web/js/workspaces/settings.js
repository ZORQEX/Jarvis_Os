// Settings: AI agency, privacy, voice, site publishing, permissions, interface. Moved out of app.js unchanged in
// behaviour; it talks to the core through ctx.link and tells the shell to redraw through ctx.onChange().
import { esc } from "../render.js";
import { agencyPanel, wireAgency } from "../agency-ui.js";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
let ctx = null;

export function outputProblem(r) {
  if (!r?.available) return "";
  if (r.sink_muted) return "Звук компьютера выключен";
  if (r.stream_found && r.muted) return "Звук JARVIS выключен в системном микшере";
  if (r.stream_found && r.volume_pct != null && r.volume_pct < 20) return `Громкость JARVIS в системе всего ${r.volume_pct} %`;
  if (r.sink_volume_pct != null && r.sink_volume_pct < 10) return `Общая громкость компьютера всего ${r.sink_volume_pct} %`;
  return "";
}

export function create(c) {
  ctx = c;
  return { el: $("#viewSettings"), show: renderSettings, hide() {}, renderAudioOut };
}

function renderSettings() {
  const el = $("#viewSettings");
  const p = ctx.S.provider || { models: [] };
  const prof = ctx.S.core.privacy;
  const choice = (id, title, text) => `<button class="choice" role="radio" aria-checked="${prof === id}" data-privacy="${id}"><b>${title}</b><span>${text}</span></button>`;
  const statusRu = !p.configured ? "не подключён" : { ready: "готов", error: "ошибка", unknown: "проверка…", unconfigured: "не подключён" }[p.status] || p.status;
  el.innerHTML = `<div class="page">
    <h1>Настройки</h1>
    ${agencyPanel(ctx.S)}
    <div class="panel">
      <h2>Приватность</h2>
      <div class="muted" style="font-size:13.5px">Что можно отправлять во внешний ИИ. Локальные действия работают в любом режиме.</div>
      <div class="choices" role="radiogroup" aria-label="Режим приватности">
        ${choice("AUTHORIZED_CLOUD", "Облако разрешено", "Свободные запросы уходят в Claude (через Claude Code) без дополнительного вопроса. Файлы — только те, что нужны для задачи.")}
        ${choice("PRIVATE_LOCAL", "Приватно", "Перед каждой отправкой в облако JARVIS спросит разрешение.")}
        ${choice("STRICT_OFFLINE", "Строго офлайн", "Ничего не уходит в облако. Голос, команды, сообщения работают; свободные вопросы к ИИ — нет.")}
      </div>
    </div>
    <div class="panel">
      <h2>Голос</h2>
      <div class="muted" style="font-size:13.5px">Речь распознаётся и озвучивается на этом компьютере (GigaAM, Parakeet, Silero, Kokoro) — без интернета и лимитов;
        в Claude уходит только текст вопроса, если на него нужен ответ ИИ. Микрофон слушает только когда включён (кнопка или разговор).</div>
      <h2 style="font-size:13.5px;margin:14px 0 2px">Голос JARVIS</h2>
      <div class="faint" style="font-size:12.5px">Голоса Silero работают на этом компьютере: без интернета, без лимитов, фраза готова за ~0,25 с.
        Модели Silero — для личного некоммерческого использования.</div>
      <div id="voicePicker" class="vpick"></div>
      <div class="field"><span style="font-size:13.5px">Скорость речи:</span>
        <input type="range" id="rateRange" min="0.8" max="1.3" step="0.05" style="flex:1;min-width:160px">
        <span class="faint" id="rateVal"></span></div>
      <label class="toggle"><input type="checkbox" id="speakToggle" ${ctx.voice.speakReplies ? "checked" : ""}> Озвучивать ответы и на текстовые запросы</label>
      <label class="toggle"><input type="checkbox" id="muteToggle" ${ctx.voice.muted ? "checked" : ""}> Выключить микрофон</label>
      <div class="field"><span style="font-size:13.5px">Пауза, после которой фраза считается законченной:</span>
        <input type="range" id="silenceRange" min="600" max="3000" step="100" value="${ctx.voice.silenceMs}" style="flex:1;min-width:160px">
        <span class="faint" id="silenceVal">${(ctx.voice.silenceMs / 1000).toFixed(2).replace(".", ",")} с</span></div>
      <div class="field" style="margin-top:10px">
        <button class="btn" id="soundTest">Проверить звук</button>
        <span id="audioOut" class="faint" style="font-size:13px"></span>
      </div>
      <div id="voiceTurns" style="margin-top:12px"></div>
    </div>
    <div class="panel">
      <h2>Самоулучшение</h2>
      <div class="muted" style="font-size:13.5px">JARVIS сам находит по своим записям, где работает плохо, и улучшает свой код: Claude Code
        правит его в отдельной ветке, JARVIS прогоняет все тесты. Применяется только после вашего «да», с перезапуском.
        Можно и голосом: «Джарвис, улучши себя».</div>
      <div id="improveBox" style="margin-top:10px"><span class="faint">Ищу проблемы…</span></div>
    </div>
    <div class="panel">
      <h2>Публикация сайтов</h2>
      <div class="muted" style="font-size:13.5px">Готовые сайты JARVIS может выложить в интернет по ссылке (GitHub Pages, бесплатно) —
        через ваш аккаунт GitHub. Вход подтверждаете вы в своём браузере одноразовым кодом; пароль и токен JARVIS не видит.</div>
      <div id="publishBox" style="margin-top:10px"><span class="faint">Проверяю…</span></div>
    </div>
    <div class="panel">
      <h2>Разрешения</h2>
      <div class="muted" style="font-size:13.5px">Каждое действие проходит через единую политику. Средний и высокий риск требуют вашего решения.</div>
      <label class="toggle"><input type="checkbox" id="fullAccess" ${ctx.S.core.access === "full" ? "checked" : ""}> <span><b>Полный доступ к ноутбуку</b> —
        чтение и поиск по всей домашней папке, обычные действия без вопросов, «да» голосом на любое подтверждение, сообщения себе без вопроса.
        По-прежнему спрашиваю один раз: сообщения другим людям, изменения кода, правила «без подтверждения».
        Никогда не открываю ключи, пароли и данные браузеров.</span></label>
      <table class="tbl"><thead><tr><th>Действие</th><th>Риск</th><th>Проверка результата</th></tr></thead><tbody>
        ${ctx.S.tools.filter((t) => t.id !== "cloud_send").map((t) => `<tr><td>${esc(t.title)}${ctx.S.grants.includes(t.id) ? ` <button class="btn ghost" data-revoke="${esc(t.id)}" style="height:24px;font-size:12px">отозвать разрешение</button>` : ""}</td>
          <td><span class="risk ${esc(t.risk)}">${esc({ low: "низкий", moderate: "средний", high: "высокий" }[t.risk] || t.risk)}</span></td>
          <td class="faint">${esc(t.verification)}</td></tr>`).join("")}
      </tbody></table>
    </div>
    <div class="panel">
      <h2>Компаньон на рабочем столе</h2>
      <div class="muted" style="font-size:13.5px">Маленький JARVIS поверх всех окон и рабочих столов: нажмите — откроется карточка,
        перетащите — переместится в любое место экрана. Если включить микрофон, достаточно сказать «Джарвис, …» —
        речь распознаётся на этом компьютере, фразы без «Джарвис» сразу отбрасываются и нигде не сохраняются.</div>
      <div class="field"><button class="btn primary" id="ovLaunch">Показать компаньон</button></div>
      <label class="toggle"><input type="checkbox" id="ovWakeT"> Слушать «Джарвис» (микрофон включён постоянно)</label>
      <label class="toggle"><input type="checkbox" id="ovAuto"> Открывать компаньон вместе с окном JARVIS</label>
    </div>
    <div class="panel">
      <h2>Интерфейс</h2>
      <div class="field">
        <button class="btn ${!document.documentElement.dataset.theme ? "primary" : ""}" data-theme-set="">Как в системе</button>
        <button class="btn ${document.documentElement.dataset.theme === "dark" ? "primary" : ""}" data-theme-set="dark">Тёмная</button>
        <button class="btn ${document.documentElement.dataset.theme === "light" ? "primary" : ""}" data-theme-set="light">Светлая</button>
      </div>
      <label class="toggle"><input type="checkbox" id="motionToggle" ${localStorage.getItem("jarvis.reduceMotion") === "1" ? "checked" : ""}> Без анимации</label>
      <label class="toggle"><input type="checkbox" id="devToggle" ${ctx.S.dev ? "checked" : ""}> Режим разработчика: показывать решения директора (исполнители, причины, кандидаты)</label>
    </div>
  </div>`;
  ctx.link.request("overlay.state").then((st) => {
    const w = $("#ovWakeT", el), au = $("#ovAuto", el);
    if (!w) return;
    w.checked = !!st.wake; au.checked = st.enabled !== false;
    w.onchange = () => ctx.link.request("overlay.settings", { wake: w.checked }).then(() => w.checked && ctx.link.request("overlay.launch"))
      .catch((er) => ctx.toast(er.message, "err"));
    au.onchange = () => ctx.link.request("overlay.settings", { enabled: au.checked }).catch((er) => ctx.toast(er.message, "err"));
  }).catch(() => {});
  $("#ovLaunch", el).addEventListener("click", () => ctx.link.request("overlay.launch")
    .then(() => ctx.toast("Компаньон появится в правом верхнем углу", "ok")).catch((er) => ctx.toast(er.message, "err")));
  wireAgency(el, { link: ctx.link, S: ctx.S, toast: ctx.toast, rerender: () => { renderSettings(); ctx.onChange(); } });
  renderPublish();
  renderImprove();
  $$("[data-privacy]", el).forEach((b) => b.addEventListener("click", async () => {
    try { await ctx.link.request("privacy.set", { profile: b.dataset.privacy }); ctx.S.core.privacy = b.dataset.privacy; } catch (er) { ctx.toast(er.message, "err"); }
    renderSettings(); ctx.onChange();
  }));
  $("#fullAccess", el).addEventListener("change", async (e) => {
    try { const r = await ctx.link.request("access.set", { level: e.target.checked ? "full" : "standard" }); ctx.S.core.access = r.access;
      ctx.toast(r.access === "full" ? "Полный доступ включён" : "Обычный доступ: снова спрашиваю перед изменениями"); }
    catch (er) { ctx.toast(er.message, "err"); e.target.checked = !e.target.checked; }
  });
  $$("[data-revoke]", el).forEach((b) => b.addEventListener("click", async () => {
    const r = await ctx.link.request("grant.revoke", { tool_id: b.dataset.revoke }); ctx.S.grants = r.session_grants; renderSettings();
  }));
  $$("[data-theme-set]", el).forEach((b) => b.addEventListener("click", () => {
    const v = b.dataset.themeSet;
    if (v) { document.documentElement.dataset.theme = v; localStorage.setItem("jarvis.theme", v); }
    else { delete document.documentElement.dataset.theme; localStorage.removeItem("jarvis.theme"); }
    renderSettings();
  }));
  $("#speakToggle", el).addEventListener("change", (e) => { ctx.voice.speakReplies = e.target.checked; localStorage.setItem("jarvis.speak", e.target.checked ? "1" : "0"); });
  $("#muteToggle", el).addEventListener("change", (e) => { ctx.voice.setMuted(e.target.checked); ctx.onChange(); });
  $("#silenceRange", el).addEventListener("input", (e) => {
    ctx.voice.silenceMs = +e.target.value; localStorage.setItem("jarvis.silenceMs2", String(ctx.voice.silenceMs));
    $("#silenceVal", el).textContent = (ctx.voice.silenceMs / 1000).toFixed(2).replace(".", ",") + " с";
  });
  renderVoiceTurns($("#voiceTurns", el));
  renderAudioOut();
  renderVoicePicker();
  $("#soundTest", el).addEventListener("click", async () => {
    try { await ctx.link.request("voice.test"); } catch (er) { ctx.toast(er.message, "err"); return; }
    setTimeout(() => ctx.voice.checkOutput(true), 900);
  });
  $("#devToggle", el).addEventListener("change", (e) => {
    ctx.S.dev = e.target.checked; localStorage.setItem("jarvis.dev", ctx.S.dev ? "1" : "0"); ctx.onDevToggle();
  });
  $("#motionToggle", el).addEventListener("change", (e) => {
    localStorage.setItem("jarvis.reduceMotion", e.target.checked ? "1" : "0");
    document.documentElement.classList.toggle("anim-off", e.target.checked);
  });
}

async function renderPublish(poll = 0) {
  const box = $("#publishBox");
  if (!box) return;
  let st;
  try { st = await ctx.link.request("publish.state"); } catch (e) { box.innerHTML = `<span class="faint">${esc(e.message)}</span>`; return; }
  if (!box.isConnected) return;           // the page was redrawn while waiting: the newer call fills the new box
  if (!st.available) { box.innerHTML = `<span class="faint">Нужен GitHub CLI (gh) — установите: «Установи gh в систему».</span>`; return; }
  if (st.connected) {
    box.innerHTML = `<div class="field" style="margin-top:0"><span class="cpill ok">GitHub подключён</span> <b>${esc(st.login)}</b>
      <button class="btn ghost" id="ghLogout">Отключить</button></div>
      <div class="faint" style="font-size:12.5px;margin-top:6px">Скажите, например: «Опубликуй сайт кафе» — JARVIS спросит подтверждение и даст ссылку.</div>`;
    $("#ghLogout", box).onclick = async () => { try { await ctx.link.request("publish.github.logout"); } catch (e) { ctx.toast(e.message, "err"); } renderPublish(); };
    return;
  }
  if (st.stage === "code") {
    box.innerHTML = `<div style="font-size:13.5px">Откройте <b>github.com/login/device</b> и введите код:</div>
      <div class="gh-code">${esc(st.code)}</div>
      <div class="field" style="margin-top:6px"><button class="btn primary" id="ghOpen">Открыть GitHub</button>
        <span class="faint" style="font-size:12.5px">После подтверждения в браузере здесь появится «подключён».</span></div>`;
    $("#ghOpen", box).onclick = () => ctx.link.request("tool.run", { tool: "open_url", args: { url: st.url } }).catch((e) => ctx.toast(e.message, "err"));
    if (ctx.currentView() === "settings" && poll < 300) setTimeout(() => renderPublish(poll + 1), 3000);
    return;
  }
  box.innerHTML = `${st.error ? `<div class="cerr" style="margin-bottom:6px">${esc(st.error)}</div>` : ""}
    <div class="field" style="margin-top:0"><span class="cpill">не подключён</span><button class="btn primary" id="ghBegin">Подключить GitHub</button></div>`;
  $("#ghBegin", box).onclick = async (e) => {
    e.target.disabled = true;
    try { await ctx.link.request("publish.github.begin", {}, 30000); } catch (er) { ctx.toast(er.message, "err"); }
    renderPublish();
  };
}

async function renderImprove() {
  const box = $("#improveBox");
  if (!box) return;
  let d;
  try { d = await ctx.link.request("improve.issues", { days: 7 }); } catch (e) { box.innerHTML = `<div class="faint">${esc(e.message)}</div>`; return; }
  const rows = d.issues.map((x, i) => `<div class="row" style="grid-template-columns:minmax(0,1fr) auto;align-items:start">
      <span class="t"><div><b>${esc(x.title)}</b> <span class="faint">— ${x.count} раз за неделю${x.last_text ? `, последний ${esc(x.last_text)}` : ""}</span></div>
        ${x.examples.length ? `<div class="p">${x.examples.slice(0, 3).map(esc).join(" · ")}</div>` : ""}</span>
      <button class="btn" data-improve="${i}">Исправить</button></div>`).join("");
  box.innerHTML = (d.issues.length ? `<div class="rows">${rows}</div>` : `<div class="faint">За неделю проблем в записях не найдено.</div>`)
    + (d.branches.length ? `<div class="faint" style="font-size:12.5px;margin-top:8px">Готовы к применению: ${d.branches.map(esc).join(", ")} — скажите «примени улучшение».</div>` : "");
  $$("[data-improve]", box).forEach((b) => b.addEventListener("click", () => {
    const x = d.issues[+b.dataset.improve];
    ctx.submit(`Улучши себя: ${x.title}. Доказательства: ${x.evidence}`, { stay: true });
    ctx.toast("JARVIS разбирается — подтвердите запуск в карточке", "ok");
  }));
}

async function renderVoicePicker() {
  const box = $("#voicePicker");
  if (!box) return;
  let d;
  try { d = await ctx.link.request("voice.voices"); } catch (e) { box.innerHTML = `<div class="faint">${esc(e.message)}</div>`; return; }
  const best = new Set(["supertonic:M4", "supertonic:F1"]);      // the highest measured naturalness of each gender
  const card = (v) => `<div class="vcard ${v.id === d.current ? "on" : ""} ${v.available ? "" : "off"}">
      <div class="vh"><b>${esc(v.name)}</b><span class="faint">${v.gender === "ж" ? "женский" : "мужской"}</span>${best.has(v.id) ? `<span class="cpill ok">лучший</span>` : ""}</div>
      <div class="faint vd">${esc(v.desc)}</div>
      <div class="va"><button class="btn ghost" data-vprev="${esc(v.id)}" ${v.available ? "" : "disabled"}>▶ Послушать</button>
        ${v.id === d.current ? `<span class="cpill ok">выбран</span>` : `<button class="btn" data-vset="${esc(v.id)}" ${v.available ? "" : "disabled"}>Выбрать</button>`}</div></div>`;
  const st = d.voices.filter((v) => v.engine === "supertonic"), ru = d.voices.filter((v) => v.engine !== "supertonic");
  box.innerHTML = `<h2 style="font-size:13.5px;margin:4px 0 0">Один голос для всех языков</h2>
    <div class="faint" style="font-size:12.5px">Supertonic 3, на этом компьютере. Один и тот же голос говорит по-русски, по-английски,
      по-украински, по-немецки и на других языках. Оценка — измеренная естественность звучания (из 5).</div>
    ${d.supertonic_installed ? "" : `<div class="cerr" style="margin:6px 0">Supertonic не установлен — запустите scripts/install.sh.</div>`}
    <div class="vgrid" style="margin-top:8px">${st.map(card).join("")}</div>
    <h2 style="font-size:13.5px;margin:16px 0 0">Только русский</h2>
    ${d.installed ? "" : `<div class="cerr" style="margin:6px 0">Голос Silero не установлен — запустите scripts/install.sh.</div>`}
    <div class="vgrid" style="margin-top:8px">${ru.map(card).join("")}</div>
    ${d.last_fallback ? `<div class="cerr" style="margin-top:6px">Последний ответ прозвучал запасным голосом: выбранный не сработал (${esc(d.last_fallback)})</div>` : ""}
    <h2 style="font-size:13.5px;margin:16px 0 0">Кто отвечает голосом</h2>
    <div class="choices" role="radiogroup" aria-label="Модель для ответов голосом">
      <button class="choice" role="radio" aria-checked="${d.ai_model !== "quality"}" data-vai="fast"><b>Быстро</b>
        <span>${esc(d.ai_models?.fast || "")}: первые слова через ~1–1,5 с, без долгих пауз. Подходит для разговора.</span></button>
      <button class="choice" role="radio" aria-checked="${d.ai_model === "quality"}" data-vai="quality"><b>Подробнее</b>
        <span>${esc(d.ai_models?.quality || "")}: обычно ~1 с, но иногда задумывается на 4–10 с.</span></button>
    </div>
    <div class="faint" style="font-size:12px;margin-top:4px">Написанные текстом запросы всегда обрабатывает ${esc(d.ai_models?.quality || "основная модель")}. Беседа общая — контекст не теряется.</div>
    <h2 style="font-size:13.5px;margin:16px 0 0">Языки</h2>
    <div class="faint" style="font-size:12.5px">Говорите на любом из них — JARVIS ответит на том же языке${d.all_languages ? " тем же голосом" : " и прочитает каждое предложение голосом своего языка"}.
      Сейчас говорит: ${d.languages.map((x) => esc(x.name)).join(", ")}.</div>
    <div class="choices" role="radiogroup" aria-label="Распознавание речи">
      <button class="choice" role="radio" aria-checked="${d.stt_mode !== "ru"}" data-stt="auto" ${d.stt_multi ? "" : "disabled"}><b>Русский и другие языки</b>
        <span>${d.stt_multi ? "Русскую речь слышит GigaAM (~0,4 с); английскую и ещё 24 европейских языка — Parakeet (+~0,9 с). ~1,9 ГБ памяти." : "Модель Parakeet не скачана — scripts/install.sh"}</span></button>
      <button class="choice" role="radio" aria-checked="${d.stt_mode === "ru"}" data-stt="ru"><b>Только русский</b>
        <span>Только GigaAM: меньше памяти (~0,5 ГБ), фразы на других языках не распознаются.</span></button>
    </div>
    ${d.all_languages ? "" : `<h2 style="font-size:13.5px;margin:14px 0 2px">Английский голос (для голосов «Только русский»)</h2>
    ${d.en_installed ? "" : `<div class="cerr" style="margin:6px 0">Kokoro не установлен — запустите scripts/install.sh. Пока английский читает русский голос.</div>`}
    <div class="vgrid" style="margin-top:8px">${d.en_voices.map((v) => `<div class="vcard ${v.id === d.en_current ? "on" : ""} ${v.available ? "" : "off"}">
      <div class="vh"><b>${esc(v.name)}</b><span class="faint">${v.gender === "ж" ? "женский" : "мужской"}</span></div>
      <div class="faint vd">${esc(v.desc)}</div>
      <div class="va"><button class="btn ghost" data-vprev-en="${esc(v.id)}" ${v.available ? "" : "disabled"}>▶ Послушать</button>
        ${v.id === d.en_current ? `<span class="cpill ok">выбран</span>` : `<button class="btn" data-vset-en="${esc(v.id)}" ${v.available ? "" : "disabled"}>Выбрать</button>`}</div></div>`).join("")}</div>`}`;
  $$("[data-stt]", box).forEach((b) => b.addEventListener("click", async () => {
    try { await ctx.link.request("voice.stt_mode", { mode: b.dataset.stt }); renderVoicePicker(); } catch (er) { ctx.toast(er.message, "err"); }
  }));
  $$("[data-vprev-en]", box).forEach((b) => b.addEventListener("click", () => {
    ctx.voice.stopSpeaking();
    ctx.link.request("voice.preview", { voice: b.dataset.vprevEn, en: true }).catch((er) => ctx.toast(er.message, "err"));
  }));
  $$("[data-vset-en]", box).forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await ctx.link.request("voice.set_en", { voice: b.dataset.vsetEn }); ctx.toast("Голос выбран"); renderVoicePicker(); }
    catch (er) { ctx.toast(er.message, "err"); b.disabled = false; }
  }));
  $$("[data-vai]", box).forEach((b) => b.addEventListener("click", async () => {
    try { await ctx.link.request("voice.ai_model", { pref: b.dataset.vai }); renderVoicePicker(); } catch (er) { ctx.toast(er.message, "err"); }
  }));
  $$("[data-vprev]", box).forEach((b) => b.addEventListener("click", () => {
    ctx.voice.stopSpeaking();
    ctx.link.request("voice.preview", { voice: b.dataset.vprev }).catch((er) => ctx.toast(er.message, "err"));
  }));
  $$("[data-vset]", box).forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await ctx.link.request("voice.set", { voice: b.dataset.vset }); ctx.toast("Голос выбран"); renderVoicePicker(); }
    catch (er) { ctx.toast(er.message, "err"); b.disabled = false; }
  }));
  const r = $("#rateRange"), rv = $("#rateVal");
  if (r) {
    r.value = d.rate; rv.textContent = `×${(+d.rate).toFixed(2).replace(".", ",")}`;
    r.onchange = async () => {
      rv.textContent = `×${(+r.value).toFixed(2).replace(".", ",")}`;
      try { await ctx.link.request("voice.set", { voice: d.current, rate: +r.value }); } catch (er) { ctx.toast(er.message, "err"); }
    };
  }
}

function renderAudioOut() {
  const box = $("#audioOut");
  if (!box) return;
  const r = ctx.S.audioOut;
  if (!r) { box.textContent = "Нажмите — JARVIS скажет тестовую фразу и проверит громкость своего звука."; return; }
  if (!r.available) { box.textContent = "Системный микшер недоступен — проверьте громкость вручную."; return; }
  const w = outputProblem(r);
  box.innerHTML = `${esc(r.sink || "Устройство вывода")}: общая громкость ${r.sink_volume_pct ?? "?"} %${r.sink_muted ? " (выключен)" : ""}`
    + (r.stream_found ? ` · JARVIS ${r.volume_pct ?? "?"} %${r.muted ? " (выключен)" : ""}` : " · поток JARVIS сейчас не играет")
    + (w ? ` <b style="color:var(--warn)">— ${esc(w)}</b> <button class="btn primary" id="audioFix" style="height:28px">Сделать громче</button>` : " ✓");
  $("#audioFix")?.addEventListener("click", async () => {
    try { await ctx.link.request("voice.test"); await new Promise((r) => setTimeout(r, 700)); ctx.S.audioOut = await ctx.link.request("voice.output", { fix: true }); }
    catch (er) { ctx.toast(er.message, "err"); }
    renderAudioOut();
  });
}

async function renderVoiceTurns(box) {
  let turns = [];
  try { turns = await ctx.link.request("voice.turns", { limit: 10 }); } catch { return; }
  if (!turns.length) { box.innerHTML = '<div class="faint" style="font-size:12.5px">Замеры появятся после первых голосовых фраз.</div>'; return; }
  const s = (v) => v == null ? "—" : (v / 1000).toFixed(1).replace(".", ",");
  box.innerHTML = `<h2 style="font-size:13.5px;margin:0 0 4px">Задержка последних голосовых ходов, с (от конца вашей фразы)</h2>
    <table class="tbl"><thead><tr><th>Фраза</th><th>Распознано</th><th>Первое слово ответа</th><th>Первый звук</th><th>Ответ готов</th></tr></thead><tbody>
    ${turns.map((t) => `<tr><td>${esc((t.transcript || "—").slice(0, 46))}</td><td>${s(t.ms.stt_end)}</td><td>${s(t.ms.ttft)}</td>
      <td><b>${s(t.ms.first_audio_played ?? t.ms.first_audio_sent)}</b></td><td>${s(t.ms.answer_done)}</td></tr>`).join("")}</tbody></table>`;
}
