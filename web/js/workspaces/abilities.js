// «Возможности»: what JARVIS can use beyond its own tools — OpenClaw and Hermes (from morph-rq/jarvis-os), their skills,
// ClawHub, channels connected through OpenClaw, automations and updates — plus the coding agents on this computer
// (Claude Code, both accounts, and Codex; the Coucou idea). Everything shown comes from the core (ws «hub.*» and
// «coding.*»); buttons that change something run the same tools as a spoken request, so they get the same approval
// card in the conversation. Tokens typed here go to the core only (never to a model or a log).
import { ICON, esc } from "../render.js";
import { byId } from "./registry.js";
import { header, emptyState, ago } from "./common.js";

const SECTIONS = [
  ["overview", "Обзор"], ["skills", "Навыки"], ["connect", "Каналы"], ["discover", "ClawHub"],
  ["automations", "Автоматизации"], ["updates", "Обновления"], ["coding", "На компьютере"], ["activity", "Журнал"],
];
const STATE_RU = { connected: "работает", starting: "запускается", sleeping: "спит", connecting: "подключается", disconnected: "нет связи",
  not_installed: "не установлен", auth_failed: "ключ не принят", off: "выключен", error: "ошибка", unknown: "проверяю…" };
const PROVIDER_RU = { jarvis: "JARVIS", app: "JARVIS", openclaw: "OpenClaw", hermes: "Hermes", shared: "Общий" };
const HEALTH_RU = { ok: "готов", degraded: "не всё есть", disconnected: "агент недоступен", failed: "сбой", disabled: "выключен",
  not_installed: "не установлен" };
const CODING_RU = { idle: "ждёт задачу", thinking: "думает", working: "работает", waiting: "ждёт вас", permission: "ждёт разрешения",
  question: "задал вопрос", done: "закончил", error: "ошибка" };
const EVENT_RU = { discovered: "новый в ClawHub", published: "новая версия", installed: "установлен", updated: "обновлён",
  rolled_back: "откачен", quarantined: "в карантине", released: "карантин снят", blocked: "заблокирован", uninstalled: "удалён",
  enabled: "включён", disabled: "выключен", failed: "ошибка", rejected: "отклонён" };

const KIND_ICON = { skill: "spark", plugin: "box", channel: "send", app: "globe", tool: "bolt" };
const KIND_RU = { skill: "навык", plugin: "плагин", channel: "канал", app: "подключение", tool: "инструмент" };
const chipOf = (ok, text) => `<span class="chip ${ok === true ? "ok" : ok === false ? "err" : "warn"}">${esc(text)}</span>`;

export function create(ctx) {
  const ws = byId("abilities");
  const el = document.createElement("section");
  el.className = "view view-ws view-abilities";
  el.dataset.ws = "abilities";
  el.setAttribute("aria-label", "Возможности");
  el.hidden = true;
  const st = { section: "overview", data: null, coding: null, filter: "all", found: null, remote: null, review: null,
    connect: null, busy: false, timer: null };

  async function load() {
    try { st.data = await ctx.link.request("hub.snapshot"); } catch (e) { st.data = { error: e.message }; }
    try { st.coding = await ctx.link.request("coding.state"); } catch { st.coding = null; }
    draw();
    clearTimeout(st.timer);
    const b = st.data?.backends;
    if (b && ["openclaw", "hermes"].some((n) => b[n]?.enabled && b[n]?.installed && !b[n]?.healthy)) st.timer = setTimeout(load, 4000);
  }

  // ---------------------------------------------------------------- actions
  async function runTool(tool, args, what) {
    try {
      const r = await ctx.link.request("tool.run", { tool, args });
      ctx.toast(`${what}: подтвердите в беседе`, "", { ttl: 8000, action: { label: "Открыть", run: () => ctx.openTask(r.task_id) } });
    } catch (e) { ctx.toast(e.message, "err"); }
  }
  async function hub(action, extra = {}, quiet = false) {
    st.busy = true; draw();
    try {
      const r = await ctx.link.request("hub." + action, extra);
      if (!quiet && r?.message) ctx.toast(r.message, r.ok === false ? "warn" : "ok");
      return r;
    } catch (e) { ctx.toast(e.message, "err"); return null; } finally { st.busy = false; }
  }

  // ---------------------------------------------------------------- sections
  function agentCard(name) {
    const a = st.data.backends?.[name] || {};
    const model = st.data.models?.[name] || "";
    const llm = name === "hermes" && st.data.llm?.turns ? ` · ходов модели: ${st.data.llm.turns}, в среднем ${(st.data.llm.ms / 1000).toFixed(1)} с` : "";
    const what = name === "openclaw"
      ? "Каналы (Telegram-боты, WhatsApp, Slack, Discord), плагины, навыки ClawHub, автоматизации."
      : "Долгие многошаговые поручения, навыки и память; пишет новые навыки. Ищет через быстрый поиск JARVIS.";
    let body;
    if (a.installed && a.state === "sleeping") {
      const keep = name === "openclaw" ? `<label class="toggle" style="display:flex;gap:8px;align-items:center;font-size:13px">
          <input type="checkbox" data-keep ${st.data.backends?.keep_openclaw ? "checked" : ""}> Держать запущенным (нужно для каналов и своих автоматизаций)</label>` : "";
      body = `<div class="muted" style="font-size:12.5px">${esc(what)}</div>
        <div class="muted" style="font-size:12.5px">Запускается сам, когда нужен (10–25 с), и засыпает после ${esc(String(st.data.backends?.idle_min ?? 20))} мин без дела — так он не держит ~1,5 ГБ памяти. Навыки остаются в каталоге.</div>
        <div class="tc-ctl"><button class="btn" data-wake="${name}">${ICON.play} Запустить сейчас</button></div>${keep}`;
    } else if (!a.installed) {
      body = `<p class="muted">Не установлен. Установка (без root, всё закреплено по версиям): <code>${esc(st.data.install_command || "scripts/install-agents.sh")}</code></p>`;
    } else {
      body = `<div class="muted" style="font-size:12.5px">${esc(what)}</div>
        <div class="muted" style="font-size:12.5px">Версия ${esc(a.version || "?")} · модель ${esc(model)}${a.restarts ? ` · перезапусков: ${a.restarts}` : ""}${a.active_runs ? ` · сейчас работает: ${a.active_runs}` : ""}${esc(llm)}</div>
        ${a.error && !a.healthy ? `<div class="muted" style="color:var(--warn);font-size:12.5px">${esc(a.error)}</div>` : ""}
        ${a.setup_error ? `<div class="muted" style="color:var(--warn);font-size:12.5px">Настройки: ${esc(a.setup_error)}</div>` : ""}
        <div class="tc-ctl"><button class="btn ghost" data-restart="${name}">${ICON.refresh} Перезапустить</button></div>
        ${name === "openclaw" && st.data.backends?.openclaw?.enabled ? `<label class="toggle" style="display:flex;gap:8px;align-items:center;font-size:13px">
          <input type="checkbox" data-keep ${st.data.backends?.keep_openclaw ? "checked" : ""}> Держать запущенным (иначе засыпает после ${esc(String(st.data.backends?.idle_min ?? 20))} мин без дела)</label>` : ""}`;
    }
    return `<section class="blk"><div class="blk-h">${esc(a.label || name)} ${chipOf(a.healthy ? true : a.installed ? (["starting", "sleeping"].includes(a.state) ? null : false) : null,
      STATE_RU[a.state] || a.state || "…")}</div>${body}</section>`;
  }

  function overview() {
    const d = st.data, s = d.summary || {}, bp = s.by_provider || {};
    const count = (p) => Object.values(bp[p] || {}).reduce((x, y) => x + y, 0);
    const lock = d.backends?.lockdown;
    const runs = d.runs || [];
    const found = st.found;
    return `<div class="home-grid"><div class="home-col">${agentCard("openclaw")}${agentCard("hermes")}</div>
      <div class="home-col">
        <section class="blk"><div class="blk-h">Каталог</div>
          <div class="muted" style="font-size:13px">Всего ${s.total || 0}, готовы ${s.usable || 0}${s.duplicates ? `, повторов скрыто: ${s.duplicates}` : ""}.
            JARVIS: ${count("jarvis") + count("app")} · OpenClaw: ${count("openclaw")} · Hermes: ${count("hermes")} · общих: ${count("shared")}.</div>
          <form class="home-ask" id="abFind" autocomplete="off" style="padding:4px 4px 4px 14px">
            <input id="abQ" placeholder="Что нужно уметь? Например: «github issues» или «погода»" aria-label="Найти возможность">
            <button class="icon-btn go" aria-label="Найти">${ICON.search}</button></form>
          ${found ? findResults(found) : ""}
        </section>
        <section class="blk"><div class="blk-h">Сейчас работают агенты</div>
          ${runs.length ? runs.map((r) => `<div class="act-row"><span>${esc(r.agent === "hermes" ? "Hermes" : "OpenClaw")}: ${esc(r.title || "")}</span>
            <span class="w">${esc(ago(r.started))}</span></div>`).join("") : `<div class="muted" style="font-size:13px">Ничего не выполняется.</div>`}
          <div class="tc-ctl">${lock
            ? `<span class="chip warn">Агенты остановлены</span><button class="btn" data-resume>Возобновить</button>`
            : `<button class="btn danger" data-stopall>${ICON.stop} Остановить агентов</button>`}</div>
        </section>
      </div></div>`;
  }

  function findResults(p) {
    const rows = [p.choice, ...(p.alternatives || [])].filter(Boolean);
    const say = { use: "Подходит", connect: "Подойдёт после подключения", enable: "Есть, но не готов", search: "Установленного подходящего нет" }[p.action];
    return `<div class="muted" style="font-size:12.5px">${esc(say || "")}${p.ms != null ? ` · ${p.ms} мс` : ""}</div>
      <div class="rows">${rows.map((c) => capRow(c)).join("")}</div>
      ${p.action === "search" ? `<div class="tc-ctl"><button class="btn ghost" data-goto="discover">Искать в ClawHub</button></div>` : ""}`;
  }

  function capRow(c, actions = "") {
    const health = HEALTH_RU[c.health] || c.health;
    const miss = (c.requires?.missing || []).join("; ");
    return `<div class="row"><span class="n" title="${esc(KIND_RU[c.kind] || c.kind)}">${ICON[KIND_ICON[c.kind] || "dot"]}</span>
      <span class="t"><div>${esc(c.title || c.name)} <span class="muted" style="font-size:12px">${esc(PROVIDER_RU[c.provider] || c.provider)} · ${esc(KIND_RU[c.kind] || c.kind)}${c.version ? " · " + esc(c.version) : ""}${c.state === "testing" ? " · в проверке" : ""}</span></div>
        <div class="p">${esc(c.description || "")}${miss ? ` — нужно: ${esc(miss)}` : ""}</div></span>
      <span class="r">${actions || esc(c.usable ? "готов" : health)}</span></div>`;
  }

  function skills() {
    const d = st.data;
    const all = [...(d.skills || []), ...(d.plugins || [])].filter((c) => !c.duplicate_of);
    const filters = [["all", "Все"], ["openclaw", "OpenClaw"], ["hermes", "Hermes"], ["shared", "Общие"], ["jarvis", "JARVIS"]];
    const list = all.filter((c) => st.filter === "all" || c.provider === st.filter)
      .sort((a, b) => Number(b.usable) - Number(a.usable) || String(a.title).localeCompare(String(b.title)));
    const rows = list.map((c) => {
      let act = "";
      const slug = c.id.split(":").pop();
      if (c.provider === "openclaw" && c.origin === "clawhub") {
        act = `<button class="btn ghost" data-manage="${c.state === "disabled" ? "enable" : "disable"}" data-id="${esc(slug)}">${c.state === "disabled" ? "Включить" : "Выключить"}</button>
          <button class="btn ghost" data-manage="uninstall" data-id="${esc(slug)}">Удалить</button>`;
      } else if (c.provider === "shared") {
        act = `<button class="btn ghost" data-manage="unshare" data-id="${esc(slug)}">Убрать у агентов</button>`;
      }
      return capRow(c, act);
    }).join("");
    return `<div class="seg-tabs" role="tablist" aria-label="Чьи навыки">${filters.map(([k, l]) =>
      `<button role="tab" data-filter="${k}" aria-selected="${st.filter === k}">${esc(l)}<span class="n">${k === "all" ? all.length : all.filter((c) => c.provider === k).length}</span></button>`).join("")}</div>
      <section class="card"><div class="rows">${rows || `<div class="row"><span></span><span class="t muted">Пусто</span><span></span></div>`}</div></section>
      <p class="muted" style="font-size:12.5px">Навыки OpenClaw выполняет его агент (может запускать команды) — каждый запуск JARVIS спрашивает у вас. Навыки, которые пишет Hermes, становятся общими только после вашей проверки.</p>`;
  }

  function connect() {
    const d = st.data;
    const info = d.channels_info || {};
    const chans = (d.channels || []);
    const card = st.connect && info[st.connect] ? connectCard(st.connect, info[st.connect]) : "";
    return `<div class="home-grid"><div class="home-col">
        <section class="blk"><div class="blk-h">Каналы через OpenClaw</div>
          ${Object.entries(info).map(([k, v]) => {
            const c = chans.find((x) => x.name === k && x.id.startsWith("openclaw:channel:"));
            const on = c && c.auth === "connected";
            return `<div class="conn" style="grid-template-columns:minmax(0,1fr) auto auto"><span>${esc(v.label)} <span class="muted" style="font-size:12px">${esc(on ? "подключён" : c ? (HEALTH_RU[c.health] || c.health) : "не подключён")}</span></span>
              ${on ? `<button class="btn ghost" data-disconnect="${k}">Отключить</button>` : `<button class="btn" data-connect="${k}">Подключить</button>`}<span></span></div>`;
          }).join("")}
          <p class="muted" style="font-size:12.5px">Ваши собственные Telegram, WhatsApp и почта подключаются в их разделах — JARVIS не подключает один и тот же аккаунт или бота дважды.</p>
        </section>${card}</div>
      <div class="home-col"><section class="blk"><div class="blk-h">Подключения JARVIS</div>
        ${(d.apps || []).map((a) => `<div class="act-row"><span>${esc(a.title)}</span><span class="w">${a.auth === "connected" ? "подключено" : "не подключено"}</span></div>`).join("")}
      </section></div></div>`;
  }

  function connectCard(k, v) {
    if (v.flow === "token") {
      return `<section class="blk" id="abConnect"><div class="blk-h">Подключить ${esc(v.label)}</div>
        <p style="margin:0;font-size:13.5px">Нужен ${esc(v.needs)}.</p>
        <form id="abToken" class="home-ask" autocomplete="off" style="padding:4px 4px 4px 14px">
          <input id="abTokenIn" type="password" placeholder="123456789:AA…" aria-label="Токен бота" autocomplete="off">
          <button class="btn primary" ${st.busy ? "disabled" : ""}>Подключить</button></form>
        <div class="tc-ctl"><button class="btn ghost" data-open-url="https://t.me/BotFather">Открыть @BotFather</button></div>
        <p class="muted" style="font-size:12px">Токен хранится в связке ключей и передаётся OpenClaw только при запуске — в файлы и журналы он не попадает.</p></section>`;
    }
    return `<section class="blk" id="abConnect"><div class="blk-h">Подключить ${esc(v.label)}</div>
      <p style="margin:0;font-size:13.5px">Нужен ${esc(v.needs)}. Это делается в терминале — QR-код или токены вводите там:</p>
      <pre class="preview-text" style="padding:8px 10px">${esc(v.command || "")}</pre></section>`;
  }

  function discover() {
    const d = st.data;
    const items = st.remote ? st.remote : (d.discover?.length ? d.discover : d.recent || []);
    const rv = st.review;
    const review = rv ? `<section class="blk"><div class="blk-h">Проверка «${esc(rv.title)}» ${chipOf(rv.scan === "clean", "проверка: " + rv.scan)}</div>
        <div class="muted" style="font-size:13px">${esc(rv.ref)} · версия ${esc(rv.version)} · автор ${esc(rv.owner || "неизвестен")}</div>
        <div style="font-size:13.5px">${esc(rv.summary || "")}</div>
        <div class="muted" style="font-size:12.5px">Нужно: ${esc((rv.requirements || []).join(", ") || "ничего дополнительно")}${rv.warnings?.length ? ` · предупреждения: ${esc(rv.warnings.join("; "))}` : ""}</div>
        <div class="tc-ctl">${rv.scan === "clean" ? `<button class="btn primary" data-install="${esc(rv.ref)}" data-version="${esc(rv.version)}">Установить…</button>`
          : `<span class="chip warn">Не «clean» — JARVIS не установит</span>`}<button class="btn ghost" data-unreview>Закрыть</button></div></section>` : "";
    const meta = d.clawhub || {};
    return `<form class="home-ask" id="abHub" autocomplete="off" style="padding:4px 4px 4px 14px">
        <input id="abHubQ" placeholder="Поиск навыков в ClawHub (по-английски: «pdf», «notion», «trello»)" aria-label="Поиск в ClawHub">
        <button class="icon-btn go" aria-label="Искать">${ICON.search}</button></form>
      ${review}
      <section class="card"><div class="card-head"><strong>${st.remote ? "Найдено" : d.discover?.length ? "Новое в ClawHub" : "Недавно вышло"}</strong>
        <span class="grow" style="flex:1"></span><span class="muted" style="font-size:12px">${meta.last_sync ? "сверено " + esc(ago(meta.last_sync)) : "ещё не сверялось"}${meta.error ? " · " + esc(meta.error) : ""}</span>
        <button class="btn ghost" data-sync>${ICON.refresh} Обновить</button></div>
        <div class="rows">${items.length ? items.map((r) => `<div class="row"><span class="n">${r.installed ? "✓" : ""}</span>
          <span class="t"><div>${esc(r.title || r.slug)} <span class="muted" style="font-size:12px">${esc(r.owner || "")} · ${esc(r.version || "")}${r.verdict ? " · " + esc(r.verdict) : ""}</span></div>
            <div class="p">${esc(r.summary || "")}</div></span>
          <span class="r">${r.installed ? "установлен" : `<button class="btn ghost" data-review="${esc(r.ref || r.slug)}">Проверить</button>`}</span></div>`).join("")
          : `<div class="row"><span></span><span class="t muted">${st.data.backends?.openclaw?.healthy ? "Пусто — нажмите «Обновить» или поищите" : "Нужен работающий OpenClaw"}</span><span></span></div>`}</div></section>
      <p class="muted" style="font-size:12.5px">Тексты из ClawHub — данные, а не инструкции. Устанавливаются только версии с проверкой «clean», и только после вашего подтверждения; установленный файл сверяется с показанным.</p>`;
  }

  function automations() {
    if (st.data.backends?.openclaw?.state === "sleeping") {
      return emptyState({ icon: "clock", title: "OpenClaw спит", text: "Автоматизации живут в OpenClaw. Запустите его, чтобы увидеть и изменить их; свои автоматизации держат его запущенным.",
        actions: `<button class="btn" data-wake="openclaw">${ICON.play} Запустить OpenClaw</button>` });
    }
    const list = (st.data.automations || []).filter((a) => !a.error);
    const err = (st.data.automations || []).find((a) => a.error);
    return `<section class="card"><div class="rows">${list.length ? list.map((a) => `<div class="row"><span class="n">${a.enabled ? "●" : "○"}</span>
      <span class="t"><div>${esc(a.name)} ${a.system ? `<span class="muted" style="font-size:12px">служебная</span>` : ""}</div>
        <div class="p">${esc(a.schedule)}${a.enabled && a.next_run ? " · следующий запуск " + esc(new Date(a.next_run * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })) : ""}${a.last_status ? " · последний: " + esc(a.last_status) : ""}</div></span>
      <span class="r">${a.system ? "" : `<button class="btn ghost" data-auto="${a.enabled ? "automation_pause" : "automation_resume"}" data-id="${esc(a.id)}">${a.enabled ? "Пауза" : "Продолжить"}</button>`}
        <button class="btn ghost" data-auto="automation_run" data-id="${esc(a.id)}">Запустить</button></span></div>`).join("")
      : `<div class="row"><span></span><span class="t muted">${err ? esc(err.error) : "Автоматизаций нет"}</span><span></span></div>`}</div></section>
      <p class="muted" style="font-size:12.5px">Регулярный «heartbeat» OpenClaw JARVIS выключил: каждый его запуск расходовал бы ваш лимит Claude. У JARVIS свои предупреждения.</p>`;
  }

  function updates() {
    const ups = st.data.updates || [], hist = st.data.update_history || [];
    return `<div class="tc-ctl"><button class="btn" data-check>${ICON.refresh} Проверить обновления</button></div>
      <section class="card"><div class="card-head"><strong>Доступно</strong></div><div class="rows">${ups.length ? ups.map((u) => `<div class="row"><span class="n">${u.needs_approval ? "!" : "✓"}</span>
        <span class="t"><div>${esc(u.component)} ${esc(u.current)} → ${esc(u.latest)}</div><div class="p">${esc((u.reasons || []).join("; ") || "без новых требований")}${u.changelog ? " · " + esc(u.changelog.slice(0, 160)) : ""}</div></span>
        <span class="r"><button class="btn ghost" data-manage="update" data-id="${esc(u.component)}">Обновить</button>
          ${(u.reasons || []).some((r) => r.includes("карантин")) ? `<button class="btn ghost" data-release="${esc(u.component)}" data-version="${esc(u.latest)}">Разрешить снова</button>` : ""}</span></div>`).join("")
        : `<div class="row"><span></span><span class="t muted">Всё обновлено</span><span></span></div>`}</div></section>
      <section class="card"><div class="card-head"><strong>История</strong></div><div class="rows">${hist.length ? hist.map((h) => `<div class="row"><span class="n"></span>
        <span class="t"><div>${esc(h.component)} ${esc(h.from_version || "")}${h.to_version ? " → " + esc(h.to_version) : ""} · ${esc(EVENT_RU[h.status] || h.status)}${h.auto ? " (сам)" : ""}</div><div class="p">${esc((h.detail || "").slice(0, 200))}</div></span>
        <span class="r">${esc(ago(h.ts))}${h.status === "applied" ? ` <button class="btn ghost" data-manage="rollback" data-id="${esc(h.component)}">Откатить</button>` : ""}</span></div>`).join("")
        : `<div class="row"><span></span><span class="t muted">Пока пусто</span><span></span></div>`}</div></section>`;
  }

  function coding() {
    const c = st.coding;
    if (!c) return emptyState({ icon: "command", title: "Наблюдение за агентами выключено", text: "В настройках ядра выключено coding_agents_enabled." });
    const hooks = Object.entries(c.hooks || {});
    const any = hooks.some(([, h]) => h.events?.length);
    const names = { claude: "Claude Code", codex: "Codex" };
    const label = (k) => (names[k.split(":")[0]] || k) + (k.includes(":") ? ` (аккаунт ${k.split(":")[1]})` : "");
    const sessions = c.sessions || [];
    return `<div class="home-grid"><div class="home-col">
      <section class="blk"><div class="blk-h">Сессии сейчас</div>
        ${sessions.length ? sessions.map((s) => `<div class="mini-task"><div class="top"><span class="pill ${s.state === "waiting" || s.state === "permission" ? "waiting" : ""}">${esc(CODING_RU[s.state] || s.state)}</span>
          <span class="req">${esc(s.label)}${s.project ? " · " + esc(s.project) : ""}</span><span class="muted" style="font-size:12px">${esc(ago(s.updated))}</span></div>
          ${s.activity ? `<div class="muted" style="font-size:12.5px;overflow-wrap:anywhere">${esc(s.activity)}</div>` : ""}
          ${s.state === "done" && s.summary ? `<div style="font-size:12.5px">${esc(s.summary)}</div>` : ""}
          ${s.prompt && s.state !== "done" ? `<div class="muted" style="font-size:12px">Задача: ${esc(s.prompt)}</div>` : ""}</div>`).join("")
          : `<div class="muted" style="font-size:13px">${any ? "Ни одна сессия пока не сообщала о работе." : "Агенты ещё не подключены к JARVIS."}</div>`}
      </section></div>
      <div class="home-col"><section class="blk"><div class="blk-h">Подключение</div>
        ${hooks.map(([k, h]) => `<div class="act-row"><span>${esc(label(k))}</span><span class="w">${h.events?.length ? "сообщает JARVIS" : h.readable ? "не подключён" : "файл настроек не читается"}</span></div>`).join("")}
        <div class="tc-ctl">${any ? `<button class="btn ghost" data-hooks="off">Отключить</button><button class="btn ghost" data-hooks="on">Обновить подключение</button>`
          : `<button class="btn primary" data-hooks="on">Подключить агентов к JARVIS</button>`}</div>
        <p class="muted" style="font-size:12.5px">JARVIS добавляет в настройки агентов (с резервной копией) короткую команду, которая сообщает ему, что они делают. Если JARVIS не запущен, она сразу завершается и ничего не меняет.</p>
        <label class="toggle" style="display:flex;gap:8px;align-items:center;font-size:13.5px"><input type="checkbox" id="abAnswer" ${c.answer_permissions ? "checked" : ""}>
          Отвечать на запросы разрешений Claude Code из окна JARVIS</label>
        <div class="muted" style="font-size:12.5px">Карточка «Разрешить / Запретить» появится здесь. Пока вы не ответили (до ${Math.round(c.wait_s)} с), агент ждёт; без ответа он спросит в терминале как обычно. JARVIS никогда не отвечает за вас.</div>
      </section></div></div>`;
  }

  function activity() {
    const ev = st.data.events || [];
    return `<section class="card"><div class="rows">${ev.length ? ev.map((e) => `<div class="row"><span class="n"></span>
      <span class="t"><div>${esc(e.slug)} ${e.version ? esc(e.version) : ""} · ${esc(EVENT_RU[e.kind] || e.kind)}</div><div class="p">${esc((e.detail || "").slice(0, 200))}</div></span>
      <span class="r">${esc(ago(e.ts))}</span></div>`).join("") : `<div class="row"><span></span><span class="t muted">Пока пусто</span><span></span></div>`}</div></section>`;
  }

  // ---------------------------------------------------------------- page
  function draw() {
    const d = st.data;
    const tabs = `<div class="seg-tabs" role="tablist" aria-label="Разделы возможностей">${SECTIONS.map(([k, l]) =>
      `<button role="tab" data-sec="${k}" aria-selected="${st.section === k}">${esc(l)}${k === "coding" && st.coding?.requests?.length ? `<span class="n">${st.coding.requests.length}</span>` : ""}</button>`).join("")}</div>`;
    let body;
    if (!d) body = `<div class="muted">Загружаю…</div>`;
    else if (d.error && !d.backends) body = emptyState({ icon: "alert", title: "Нет связи с «Возможностями»", text: esc(d.error) });
    else body = { overview, skills, connect, discover, automations, updates, coding, activity }[st.section]();
    el.innerHTML = `<div class="ws">${header(ws, ctx.env(), { title: "Возможности", sub: "OpenClaw · Hermes · навыки · агенты на компьютере" })}
      ${tabs}<div class="ab-body" style="display:flex;flex-direction:column;gap:var(--s4)">${body}</div></div>`;
  }

  el.addEventListener("click", async (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    const ds = t.dataset;
    if (ds.sec) { st.section = ds.sec; draw(); if (ds.sec === "coding" || ds.sec === "overview") load(); return; }
    if (ds.filter) { st.filter = ds.filter; draw(); return; }
    if (ds.goto) { st.section = ds.goto; draw(); return; }
    if (ds.restart) { await hub("restart", { name: ds.restart }); setTimeout(load, 1500); return; }
    if (ds.wake) { ctx.toast("Запускаю — 10–25 секунд…", ""); await hub("wake", { name: ds.wake }); load(); return; }
    if (ds.stopall !== undefined) { await runTool("agents_stop", {}, "Остановить агентов"); setTimeout(load, 800); return; }
    if (ds.resume !== undefined) { await runTool("agents_resume", {}, "Возобновить агентов"); return; }
    if (ds.manage) { await runTool("skill_manage", { action: ds.manage, id: ds.id }, "Навык " + ds.id); return; }
    if (ds.disconnect) { await runTool("service_disconnect", { service: ds.disconnect }, "Отключить " + ds.disconnect); return; }
    if (ds.connect) { st.connect = ds.connect; draw(); el.querySelector("#abTokenIn")?.focus(); return; }
    if (ds.openUrl) { ctx.desktop.openUrl(ds.openUrl).catch((er) => ctx.toast(er.message, "err")); return; }
    if (ds.sync !== undefined) { const r = await hub("sync", {}, true); if (r) { ctx.toast(r.ok ? `ClawHub: просмотрено ${r.seen}, новых ${r.new?.length || 0}` : (r.error || r.message), r.ok ? "ok" : "warn"); } st.remote = null; load(); return; }
    if (ds.review) { const r = await hub("review", { ref: ds.review }, true); if (r?.review) { st.review = r.review; draw(); } else if (r?.message) ctx.toast(r.message, "warn"); return; }
    if (ds.unreview !== undefined) { st.review = null; draw(); return; }
    if (ds.install) { await runTool("skill_install", { ref: ds.install, version: ds.version }, "Установка " + ds.install); return; }
    if (ds.auto) { const r = await hub(ds.auto, { target: ds.id }); if (r?.automations) { st.data.automations = r.automations; draw(); } return; }
    if (ds.check !== undefined) { const r = await hub("check_updates", {}, true); if (r) ctx.toast(r.ok ? `Обновлений: ${r.updates?.length || 0}` : r.message, r.ok ? "ok" : "warn"); load(); return; }
    if (ds.release) { await hub("release", { target: ds.release, version: ds.version }); load(); return; }
    if (ds.hooks) {
      try { st.coding = await ctx.link.request("coding.hooks", { on: ds.hooks === "on" }); draw();
        ctx.toast(ds.hooks === "on" ? "Агенты подключены: JARVIS видит, что они делают" : "Агенты отключены от JARVIS", "ok");
      } catch (er) { ctx.toast(er.message, "err"); }
    }
  });
  el.addEventListener("change", async (e) => {
    if (e.target.dataset.keep !== undefined) {
      await hub("keep", { on: e.target.checked }, true); load(); return;
    }
    if (e.target.id === "abAnswer") {
      try { st.coding = { ...st.coding, ...(await ctx.link.request("coding.settings", { answer: e.target.checked })) }; draw(); }
      catch (er) { ctx.toast(er.message, "err"); }
    }
  });
  el.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (e.target.id === "abFind") {
      const q = el.querySelector("#abQ").value.trim();
      if (q) { st.found = await hub("find", { query: q }, true); draw(); el.querySelector("#abQ").value = q; }
    } else if (e.target.id === "abHub") {
      const q = el.querySelector("#abHubQ").value.trim();
      if (q) { const r = await hub("search", { query: q }, true); st.remote = r?.registry || []; if (r?.registry_error) ctx.toast(r.registry_error, "warn"); draw(); }
    } else if (e.target.id === "abToken") {
      const inp = el.querySelector("#abTokenIn"), token = inp.value.trim();
      inp.value = "";
      if (!token) return;
      ctx.toast("Подключаю Telegram через OpenClaw — это до минуты…", "");
      const r = await hub("connect_token", { service: st.connect, token });
      if (r?.ok) { st.connect = null; load(); }
    }
  });

  return {
    el,
    show(params = {}) { if (params.section) st.section = params.section; draw(); load(); },
    hide() { clearTimeout(st.timer); },
    onEvent(ev) {
      if (ev.kind === "hub.connect") { st.section = "connect"; st.connect = ev.plan?.service || null; }
      if (ev.kind === "coding.agents") { st.coding = { ...(st.coding || {}), sessions: ev.sessions, requests: ev.requests }; }
      if (el.hidden) return;
      if (ev.kind === "coding.agents" && st.section !== "coding" && st.section !== "overview") return;
      if (ev.kind === "hub.backends" || ev.kind === "hub.catalog" || ev.kind === "hub.event") { load(); return; }
      draw();
    },
  };
}
