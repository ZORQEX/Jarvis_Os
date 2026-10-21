// «AI-агентство» settings: one JARVIS, several internal specialists.
// Shows real health (from checks), roles, models, capabilities, usage and errors. Never shows secrets.
import { esc, fmtMs, fmtWhen } from "./render.js";

const HEALTH_RU = {
  CONNECTED: ["Подключён", "ok"], DEGRADED: ["Не проверен / ограничен", "warn"], RATE_LIMITED: ["Лимит запросов", "warn"],
  AUTH_ERROR: ["Ошибка доступа", "err"], OFFLINE: ["Нет связи", "err"], DISABLED: ["Выключен", ""], NOT_CONFIGURED: ["Не подключён", ""],
};
const SENS_RU = { PUBLIC: "только публичное", INTERNAL: "до внутреннего", CONFIDENTIAL: "до конфиденциального" };

export function agencyPanel(S) {
  const ps = S.providers || [];
  const active = ps.filter((p) => ["CONNECTED", "DEGRADED"].includes(p.health)).length;
  return `<div class="panel agency">
    <h2>AI-агентство</h2>
    <div class="muted" style="font-size:13.5px">Вы говорите с одним JARVIS. Директор сам решает, нужен ли ИИ вообще, кто из
      специалистов справится лучше и в каком порядке, — с теми же разрешениями и подтверждениями, что и для любых действий.
      Весь ИИ — Claude через Claude Code на этом компьютере (ваш вход Claude): «Claude» ведёт беседу, ищет в интернете,
      пишет тексты и действует через инструменты JARVIS; «Claude Code» работает с файлами проектов и кодом.</div>
    <div class="director"><span class="hdot ok"></span><b>Директор</b><span class="faint">активен · специалистов доступно: ${active} из ${ps.length}
      · простые команды выполняются без ИИ</span></div>
    <div class="pcards">${ps.map(card).join("")}</div>
  </div>`;
}

function card(p) {
  const [hru, hcls] = HEALTH_RU[p.health] || [p.health, ""];
  const u = p.usage || {};
  const st = p.settings || {};
  const models = (p.models || []).filter((m) => m.role !== "tts");
  const verified = p.verified_at ? `проверен вызовом ${fmtWhen(p.verified_at)}` : p.checked_at ? `проверка ${fmtWhen(p.checked_at)}` : "ещё не проверялся";
  return `<div class="pcard" data-provider="${esc(p.id)}">
    <div class="ph"><span class="hdot ${hcls}"></span><b>${esc(p.name)}</b><span class="hstate ${hcls}">${esc(hru)}</span>
      <label class="toggle mini" title="Включить или выключить специалиста"><input type="checkbox" data-ag="enabled" ${st.enabled ? "checked" : ""}></label></div>
    <div class="role">${esc(p.role)}</div>
    <div class="caps">${(p.capabilities || []).map((c) => `<span class="cap">${esc(c.ru)}</span>`).join("")}</div>
    <dl class="kv small">
      <dt>Доступ</dt><dd>${esc(p.auth || "")}${p.key_source ? ` · ключ из ${p.key_source === "env" ? "переменной окружения" : "хранилища"}` : ""}</dd>
      <dt>Модель</dt><dd>${models.map((m) => `<code>${esc(m.model)}</code><span class="faint"> ${esc(m.role)}</span>`).join(", ") || "—"}</dd>
      <dt>Состояние</dt><dd>${esc(verified)}${p.last_error ? ` · <span style="color:var(--err)">${esc(p.last_error)}</span>` : ""}</dd>
      <dt>Использование</dt><dd>${u.calls ? `${u.calls} вызовов, успешно ${u.ok}` : "не использовался"}${u.latency_p50_ms ? ` · медиана ${fmtMs(u.latency_p50_ms)}` : ""}${u.cost_usd != null ? ` · $${u.cost_usd}` : ""}</dd>
      <dt>Данные</dt><dd><select data-ag="max_sensitivity" class="input sel">${["PUBLIC", "INTERNAL", "CONFIDENTIAL"].map((s) => `<option value="${s}" ${st.max_sensitivity === s ? "selected" : ""}>${SENS_RU[s]}</option>`).join("")}</select></dd>
      <dt>Резерв</dt><dd><label class="toggle mini inline"><input type="checkbox" data-ag="fallback_eligible" ${st.fallback_eligible ? "checked" : ""}> может подменять недоступного специалиста</label></dd>
    </dl>
    <div class="field">
      ${p.key_settable ? `<form class="keyform" data-key="${esc(p.id)}" autocomplete="off" style="display:flex;gap:8px;flex:1;flex-wrap:wrap">
        <input class="input" type="password" placeholder="${p.configured ? "Заменить ключ…" : "Вставьте API-ключ"}" aria-label="API-ключ ${esc(p.name)}" spellcheck="false">
        <button class="btn primary" type="submit">Сохранить</button>
        ${p.configured ? `<button class="btn danger" type="button" data-clear="${esc(p.id)}">Удалить ключ</button>` : ""}</form>` : ""}
      <button class="btn" data-check="${esc(p.id)}">${p.id === "claude_code" ? "Проверить вызовом" : "Проверить"}</button>
    </div>
  </div>`;
}

export function wireAgency(root, { link, S, toast, rerender }) {
  const apply = (r) => { if (r?.providers) S.providers = r.providers; rerender(); };
  root.querySelectorAll(".pcard").forEach((c) => {
    const pid = c.dataset.provider;
    c.querySelectorAll("[data-ag]").forEach((inp) => inp.addEventListener("change", async () => {
      const v = inp.type === "checkbox" ? inp.checked : inp.value;
      try { apply(await link.request("agency.settings", { provider: pid, [inp.dataset.ag]: v })); }
      catch (e) { toast(e.message, "err"); }
    }));
    c.querySelector("[data-check]")?.addEventListener("click", async (e) => {
      const b = e.currentTarget; b.disabled = true; b.textContent = "Проверяю…";
      try { apply(await link.request("agency.check", { provider: pid, live: pid === "claude_code" }, 120000)); }
      catch (er) { toast(er.message, "err"); rerender(); }
    });
    c.querySelector("[data-clear]")?.addEventListener("click", async () => {
      try { apply(await link.request("provider.clear_key", { provider: pid })); toast("Ключ удалён из хранилища"); }
      catch (er) { toast(er.message, "err"); }
    });
    c.querySelector("form.keyform")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const inp = e.currentTarget.querySelector("input");
      const key = inp.value.trim(); inp.value = "";
      if (!key) return;
      toast("Сохраняю ключ и проверяю доступ…");
      try { apply(await link.request("provider.set_key", { provider: pid, key }, 60000)); }
      catch (er) { toast(er.message, "err"); }
    });
  });
}
