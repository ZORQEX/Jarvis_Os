// Notification layer: small, bottom-right, auto-dismissing. Errors stay longer and can be closed; an optional action
// button (e.g. «Повторить», «Открыть») runs a real handler. Never more than four at once.
import { ICON, esc } from "../render.js";

const ICONS = { ok: ICON.check, warn: ICON.alert, err: ICON.x, "": ICON.dot };
const TTL = { ok: 3200, "": 3800, warn: 6000, err: 8000 };

export function createNotifier(host) {
  return function notify(msg, kind = "", opts = {}) {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.setAttribute("role", kind === "err" ? "alert" : "status");
    el.innerHTML = `<span class="ti" aria-hidden="true">${ICONS[kind] || ICONS[""]}</span><span class="tx">${esc(msg)}</span>`
      + (opts.action ? `<span class="ta"><button class="btn ghost">${esc(opts.action.label)}</button></span>` : "")
      + `<button class="icon-btn" aria-label="Закрыть" style="width:24px;height:24px">${ICON.x}</button>`;
    const close = () => { el.classList.add("out"); setTimeout(() => el.remove(), 220); };
    el.querySelector(".ta button")?.addEventListener("click", () => { close(); opts.action.run(); });
    el.querySelector(".icon-btn").addEventListener("click", close);
    host.appendChild(el);
    let timer = setTimeout(close, opts.ttl || TTL[kind] || 3800);
    el.addEventListener("mouseenter", () => clearTimeout(timer));
    el.addEventListener("mouseleave", () => { timer = setTimeout(close, 2000); });
    while (host.children.length > 4) host.firstChild.remove();
    return close;
  };
}
