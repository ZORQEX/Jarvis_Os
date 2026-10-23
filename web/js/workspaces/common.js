// Shared pieces of workspace pages: header with the workspace's tools and their real status, empty states, avatars.
import { ICON, esc } from "../render.js";
import { toolStatus } from "./registry.js";

const TOOL_STATE = { ready: "готово", needs_connection: "нужно подключение", offline: "нет связи с ядром", missing: "не подключено" };

/** «Инструменты · 4 из 6» with a list of every tool and why it is or is not available. */
export function toolsDisclosure(ws, env) {
  const tools = toolStatus(ws, env);
  if (!tools.length) return "";
  const ready = tools.filter((t) => t.state === "ready").length;
  return `<details class="ws-tools"><summary>${ICON.bolt.replace("<svg", '<svg style="width:14px;height:14px"')} Инструменты · ${ready} из ${tools.length}</summary>
    <div class="tl">${tools.map((t) => `<div class="tool-row" data-state="${t.state}"><span class="d"></span>
      <span>${esc(t.label)} <code>${esc(t.id)}</code><span class="w">${esc(TOOL_STATE[t.state])}${t.why ? " — " + esc(t.why) : ""}</span></span></div>`).join("")}</div></details>`;
}

export function header(ws, env, { title, sub = "", right = "" } = {}) {
  return `<div class="ws-head"><h1>${esc(title || ws.title)}</h1>${sub ? `<span class="sub">${sub}</span>` : ""}
    <span class="grow"></span>${right}${toolsDisclosure(ws, env)}</div>`;
}

/** @param {{icon?:string, title:string, text?:string, tries?:string[], actions?:string}} o */
export function emptyState(o) {
  return `<div class="empty-state"><div class="ico">${ICON[o.icon || "spark"]}</div><h3>${esc(o.title)}</h3>
    ${o.text ? `<p>${o.text}</p>` : ""}
    ${o.tries?.length ? `<div class="try">${o.tries.map((t) => `<button class="say" data-suggest="${esc(t)}">«${esc(t)}»</button>`).join("")}</div>` : ""}
    ${o.actions || ""}</div>`;
}

/** Initials from the words that have letters («Избранное (вы сами)» → «ИВ», «Мама ❤️» → «М»); a number → person icon. */
export function avatar(name) {
  const letters = String(name || "").split(/[\s()\[\]«»"',.-]+/).map((w) => w.match(/\p{L}/u)?.[0]).filter(Boolean)
    .slice(0, 2).join("").toUpperCase();
  return `<span class="av" aria-hidden="true">${letters ? esc(letters) : ICON.user.replace("<svg", '<svg style="width:16px;height:16px"')}</span>`;
}

export const ago = (ts) => {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "только что";
  if (s < 3600) return `${Math.floor(s / 60)} мин назад`;
  const d = new Date(ts * 1000), n = new Date();
  if (d.toDateString() === n.toDateString()) return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
};
export const clock = (ts) => new Date(ts * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
