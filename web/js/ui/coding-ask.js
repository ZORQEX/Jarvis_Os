// A coding agent (Claude Code / Codex) asks for permission and the person turned on «Отвечать из окна JARVIS»: a card
// with «Разрешить / Запретить» on whatever view is open. The card is acknowledged only while this window is visible —
// otherwise the core gives up within 2 s and the agent asks in its own terminal (JARVIS never holds an agent nobody
// can see, and never answers for the person). The answer is a click only.
import { ICON, esc } from "../render.js";

const RISK_RU = { low: "чтение", moderate: "изменение", high: "команда" };

export function createCodingAsk(link, toast) {
  const host = document.createElement("div");
  host.className = "coding-asks";
  host.setAttribute("aria-live", "assertive");
  document.body.appendChild(host);
  const shown = new Map();

  function card(r) {
    const el = document.createElement("div");
    el.className = "coding-ask";
    el.setAttribute("role", "alertdialog");
    el.setAttribute("aria-label", `${r.label} просит разрешения`);
    const input = r.input?.command || r.input?.file_path || r.input?.url || "";
    el.innerHTML = `<div class="ca-h">${ICON.shield}<b>${esc(r.label)}</b>${r.project ? `<span class="muted">· ${esc(r.project)}</span>` : ""}
        <span class="ca-risk" data-risk="${esc(r.risk)}">${esc(RISK_RU[r.risk] || r.risk)}</span></div>
      <div class="ca-line">${esc(r.line)}</div>
      ${input && String(input).length > 60 ? `<pre class="ca-in">${esc(String(input).slice(0, 1200))}</pre>` : ""}
      <div class="ca-actions"><span class="ca-left muted"></span>
        <button class="btn danger" data-no>Запретить</button><button class="btn primary" data-yes>Разрешить</button></div>`;
    const left = el.querySelector(".ca-left");
    const tick = () => { const s = Math.max(0, Math.round(r.expires - Date.now() / 1000)); left.textContent = s ? `ждёт ${s} с, потом спросит в терминале` : ""; };
    tick();
    const timer = setInterval(tick, 1000);
    el._stop = () => clearInterval(timer);
    const decide = async (allow) => {
      el.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      try { await link.request("coding.decide", { id: r.id, allow }); toast(allow ? "Разрешено" : "Запрещено", allow ? "ok" : ""); }
      catch (e) { toast(e.message, "warn"); }
    };
    el.querySelector("[data-yes]").addEventListener("click", () => decide(true));
    el.querySelector("[data-no]").addEventListener("click", () => decide(false));
    return el;
  }

  return function update(requests = []) {
    for (const r of requests) {
      if (shown.has(r.id)) continue;
      const el = card(r);
      shown.set(r.id, el);
      host.appendChild(el);
      if (document.visibilityState === "visible") link.request("coding.ack", { id: r.id }).catch(() => {});
    }
    for (const [id, el] of shown) {
      if (!requests.some((r) => r.id === id)) { el._stop(); el.remove(); shown.delete(id); }
    }
  };
}
