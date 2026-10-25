// «Ошибки»: what went wrong, from JARVIS's own records (failed tasks grouped by the error, requests the person had to
// repeat, slow spoken answers, phrases not heard…), kept as one short list for fixing later. «Исправить» hands the
// problem to self-improvement (Claude Code on a separate branch; nothing changes until the person says «да»).
// This replaced the task center and Settings → «Самоулучшение»: the list lives in one place only.
import { ICON, esc } from "../render.js";
import { byId } from "./registry.js";
import { header, emptyState } from "./common.js";

const DAYS = 30;
const KIND_ICON = { failed: "alert", repeat: "retry", slow: "clock", not_heard: "mic", voice_error: "micOff", not_understood: "chat" };

export function create(ctx) {
  const ws = byId("errors");
  const el = document.createElement("section");
  el.className = "view view-ws";
  el.dataset.ws = "errors";
  el.setAttribute("aria-label", "Ошибки");
  el.hidden = true;
  const st = { issues: [], branches: [], loaded: false, error: "", busy: false };

  async function load() {
    if (st.busy) return;
    st.busy = true;
    try {
      const d = await ctx.link.request("improve.issues", { days: DAYS });
      st.issues = d.issues || []; st.branches = d.branches || []; st.error = ""; st.loaded = true;
    } catch (e) { st.error = e.message; }
    st.busy = false;
    draw();
  }

  function row(x, i) {
    const examples = (x.examples || []).slice(0, 3);
    return `<div class="err-row">
      <span class="err-ico" aria-hidden="true">${ICON[KIND_ICON[x.kind]] || ICON.alert}</span>
      <div class="err-main">
        <div class="err-t"><b>${esc(x.title)}</b></div>
        <div class="err-m">${x.count} раз${x.last_text ? ` · последний ${esc(x.last_text)}` : ""}</div>
        ${examples.length ? `<div class="err-ex">${examples.map((e) => `<span>${esc(e)}</span>`).join("")}</div>` : ""}
      </div>
      <button class="btn" data-fix="${i}" title="JARVIS исправит свой код в отдельной ветке и применит только после вашего «да»">Исправить</button>
    </div>`;
  }

  function draw() {
    if (el.hidden) return;
    const body = st.error ? `<div class="callout err">${ICON.alert}<span>${esc(st.error)}</span></div>`
      : !st.loaded ? `<div class="empty-note">Загружаю…</div>`
      : st.issues.length ? `<div class="err-list">${st.issues.map(row).join("")}</div>`
      : `<div class="panel">${emptyState({ icon: "check", title: "Ошибок нет",
          text: `За ${DAYS} дней в записях JARVIS проблем не найдено. Сюда попадают задачи с ошибкой, просьбы, которые пришлось повторить, и слишком медленные ответы голосом.` })}</div>`;
    const ready = st.branches.length ? `<div class="callout">${ICON.check}<span>Готовы к применению: ${st.branches.map(esc).join(", ")}.
      <button class="btn ghost" data-apply style="height:26px;font-size:12.5px;margin-left:6px">Применить</button></span></div>` : "";
    el.innerHTML = `<div class="ws narrow">${header(ws, ctx.env(), { sub: `за ${DAYS} дней, самые частые сверху` })}
      ${ready}${body}</div>`;
  }

  el.addEventListener("click", (e) => {
    const fix = e.target.closest("[data-fix]");
    if (fix) {
      const x = st.issues[+fix.dataset.fix];
      if (!x) return;
      ctx.submit(`Улучши себя: ${x.title}. Доказательства: ${x.evidence}`, { stay: true });
      ctx.toast("JARVIS разбирается — подтвердите запуск в карточке", "ok");
      return;
    }
    if (e.target.closest("[data-apply]")) ctx.submit("Примени улучшение", { stay: true });
  });

  const offs = [];
  return {
    el,
    show() {
      draw(); load();
      // A task that failed while the list is open: read the records again (the core groups them).
      if (!offs.length) offs.push(ctx.bus.on("task", (t) => { if (t?.state === "failed") load(); }));
    },
    hide() { while (offs.length) offs.pop()(); },
  };
}
