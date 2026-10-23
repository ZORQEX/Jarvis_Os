// Global JARVIS companion: one instance for the whole window, outside every workspace, so switching workspaces never
// re-creates it. Docked in its slot at the top-left of the top bar by default; drag it anywhere (clamped, snaps to
// edges, drop it near the slot to dock again). Collapse / expand, pin, hide until the next event; the place, pin
// and size survive a reload. Everything it shows comes from deriveAgent() — the real state.
import * as companion from "../companion.js";
import { deriveAgent, PHASES } from "../core/agent-state.js";
import { clamp, snap, toAnchor, fromAnchor, nearDock, readStored } from "../core/geometry.js";
import { planRows, isUserTask } from "../core/tasks-model.js";
import { prefs } from "../core/store.js";
import { ICON, esc, md } from "../render.js";

const ORB_FLOAT = 52;
const CARD_W = 340, CARD_H_EST = 380;
const AUTO_COLLAPSE_MS = 7000;
const MARK = { done: "✓", active: "●", waiting: "!", failed: "✕", skipped: "–", pending: "○" };

/**
 * @param {object} ctx
 *  S, bus, agent, voice, nav(id), toast, submit(text,{stay}), agentInput(): object, context(): {title, detail},
 *  openApproval(id), openTask(id), lastReply(): {text, taskId, at}|null, onLayout(docked)
 */
export function createCompanionDock(ctx) {
  const st = { ...readStored(prefs.get("companion")), autoOpened: false, autoReason: null, lastAttn: null, pos: { x: 0, y: 0 },
    drag: null, suppressClick: false, sig: "", collapseTimer: 0, hover: false, agent: null, seenReply: null };
  const el = document.createElement("div");
  el.className = "cdock";
  el.id = "cdock";
  el.innerHTML = `
    <button class="cd-orb" id="cdOrb" aria-haspopup="dialog" aria-expanded="false"><span class="cd-face"></span><span class="cd-badge" hidden></span></button>
    <div class="cd-pill" aria-hidden="true"><b></b><span></span><button class="icon-btn cd-stop" data-cd="stop" aria-label="Остановить" hidden>${ICON.stop}</button></div>
    <div class="cd-card" role="dialog" aria-label="JARVIS">
      <div class="cd-head"><span class="dot"></span><span class="cd-label"></span>
        <div class="cd-tools">
          <button class="icon-btn" data-cd="pin" aria-pressed="false" aria-label="Закрепить открытым" title="Закрепить открытым">${ICON.pin}</button>
          <button class="icon-btn" data-cd="dock" aria-label="Вернуть на место" title="Вернуть на место (в верхний левый угол)">${ICON.dock}</button>
          <button class="icon-btn" data-cd="hide" aria-label="Скрыть до следующего события" title="Скрыть до следующего события (вернуть: Ctrl+J)">${ICON.eyeOff}</button>
          <button class="icon-btn" data-cd="collapse" aria-label="Свернуть" title="Свернуть (Esc)">${ICON.minus}</button>
        </div></div>
      <div class="cd-detail"></div>
      <div class="cd-phases phases" hidden></div>
      <div class="cd-body"></div>
      <div class="cd-ctx"></div>
      <form class="cd-ask" autocomplete="off">
        <input type="text" aria-label="Спросить JARVIS" placeholder="Спросить JARVIS…  Ctrl+J">
        <button type="button" class="icon-btn mic" data-cd="mic" aria-label="Сказать фразу" title="Сказать фразу (Ctrl+Space)">${ICON.mic}</button>
        <button type="submit" class="icon-btn go" aria-label="Отправить" disabled>${ICON.arrowUp}</button>
      </form>
    </div>`;
  document.body.appendChild(el);
  companion.mount(el.querySelector(".cd-face"));
  const orb = el.querySelector(".cd-orb"), card = el.querySelector(".cd-card"), input = el.querySelector(".cd-ask input");
  const go = el.querySelector(".cd-ask .go");
  const dockSlot = document.getElementById("dockSlot");

  // ---------------------------------------------------------------- geometry
  const view = () => ({ w: innerWidth, h: innerHeight });
  const size = () => (st.docked ? { w: 40, h: 40 } : { w: ORB_FLOAT, h: ORB_FLOAT });
  const dockPos = () => { const r = dockSlot.getBoundingClientRect(); return { x: Math.round(r.left), y: Math.round(r.top) }; };
  function place(p) {
    st.pos = p;
    el.style.transform = `translate3d(${p.x}px, ${p.y}px, 0)`;
    const v = view();
    el.dataset.flipX = p.x + CARD_W + 16 > v.w ? "1" : "0";
    el.dataset.flipY = p.y + ORB_FLOAT + 8 + Math.min(CARD_H_EST, card.offsetHeight || CARD_H_EST) > v.h - 8 && p.y > v.h / 2 ? "1" : "0";
  }
  function relayout() {
    if (st.drag) return;
    el.dataset.docked = st.docked ? "1" : "0";
    ctx.onLayout?.(st.docked && !st.hidden);
    // The slot animates its width; read it on the next frame.
    requestAnimationFrame(() => place(st.docked ? dockPos() : fromAnchor(st.anchor || { h: "left", v: "top", dx: 96, dy: 72 }, size(), view())));
  }
  function persist() {
    prefs.set("companion", { docked: st.docked, anchor: st.anchor, expanded: st.expanded && st.pinned, pinned: st.pinned });
  }

  // ---------------------------------------------------------------- drag (pointer events, rAF, transform only)
  let raf = 0, next = null;
  orb.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    orb.setPointerCapture(e.pointerId);
    st.drag = { px: e.clientX, py: e.clientY, x: st.pos.x, y: st.pos.y, moved: false, wasDocked: st.docked };
  });
  orb.addEventListener("pointermove", (e) => {
    const d = st.drag;
    if (!d) return;
    const dx = e.clientX - d.px, dy = e.clientY - d.py;
    if (!d.moved && Math.hypot(dx, dy) < 5) return;
    if (!d.moved) {
      d.moved = true;
      el.dataset.dragging = "1";
      if (st.docked) { st.docked = false; el.dataset.docked = "0"; ctx.onLayout?.(false); d.x -= 6; d.y -= 6; }
    }
    next = clamp({ x: d.x + dx, y: d.y + dy }, size(), view());
    if (!raf) raf = requestAnimationFrame(() => { raf = 0; if (next) place(next); });
  });
  const endDrag = () => {
    const d = st.drag;
    st.drag = null;
    if (!d || !d.moved) return;
    st.suppressClick = true;
    delete el.dataset.dragging;
    if (nearDock(st.pos, dockPos())) st.docked = true;
    else { st.anchor = toAnchor(snap(st.pos, size(), view()), size(), view()); st.docked = false; }
    persist();
    relayout();
  };
  orb.addEventListener("pointerup", endDrag);
  orb.addEventListener("pointercancel", endDrag);
  orb.addEventListener("click", () => {
    if (st.suppressClick) { st.suppressClick = false; return; }
    setExpanded(!st.expanded, true);
  });
  orb.addEventListener("keydown", (e) => {
    const k = { ArrowLeft: [-24, 0], ArrowRight: [24, 0], ArrowUp: [0, -24], ArrowDown: [0, 24] }[e.key];
    if (!k) return;
    e.preventDefault();
    const from = st.docked ? dockPos() : st.pos;
    st.docked = false;
    const p = clamp({ x: from.x + k[0], y: from.y + k[1] }, size(), view());
    st.anchor = toAnchor(p, size(), view());
    persist(); relayout();
  });
  addEventListener("resize", () => relayout());
  new ResizeObserver(() => { if (st.docked) relayout(); }).observe(document.querySelector(".topbar"));

  // ---------------------------------------------------------------- open / close / pin / hide
  function setExpanded(v, byUser = false, reason = null) {
    st.expanded = v;
    if (byUser || !v) { st.autoOpened = false; st.autoReason = null; }
    if (!byUser && v) { st.autoOpened = true; st.autoReason = reason; }
    el.dataset.expanded = v ? "1" : "0";
    orb.setAttribute("aria-expanded", String(v));
    if (v) { place(st.pos); render(true); }
    persist();
  }
  function setHidden(v) {
    st.hidden = v;
    el.hidden = v;
    ctx.onLayout?.(st.docked && !v);
    if (!v) relayout();
  }
  // A click anywhere else folds the card away (unless pinned): it must never sit on top of the work.
  document.addEventListener("pointerdown", (e) => {
    if (!st.expanded || st.pinned || st.drag || e.target.closest(".cdock, #status, .palette")) return;
    setExpanded(false);
  }, true);
  el.addEventListener("pointerenter", () => { st.hover = true; });
  el.addEventListener("pointerleave", () => { st.hover = false; scheduleCollapse(); });
  card.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); setExpanded(false, true); orb.focus(); } });

  el.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-cd]");
    if (!b) return;
    const a = b.dataset.cd, agent = st.agent;
    if (a === "pin") { st.pinned = !st.pinned; if (st.pinned) setExpanded(true, true); render(true); persist(); return; }
    if (a === "dock") { st.docked = true; persist(); relayout(); return; }
    if (a === "hide") { setExpanded(false, true); setHidden(true); ctx.toast("JARVIS скрыт до следующего события. Вернуть: Ctrl+J или кнопка статуса вверху."); return; }
    if (a === "collapse") { setExpanded(false, true); return; }
    if (a === "mic") { ctx.voice.listenOnce(); return; }
    if (a === "stop") return stop();
    if (a === "approve" || a === "deny") {
      b.disabled = true;
      try { a === "approve" ? await ctx.agent.confirmAction(b.dataset.id) : await ctx.agent.denyAction(b.dataset.id); }
      catch (er) { ctx.toast(er.message, "err"); b.disabled = false; }
      return;
    }
    if (a === "edit") { ctx.openApproval(b.dataset.id); return; }
    if (a === "task") { ctx.openTask(b.dataset.id); return; }
    if (a === "chat") { ctx.nav("chat"); return; }
    if (a === "estop_clear") { try { await ctx.agent.control("estop_clear"); } catch (er) { ctx.toast(er.message, "err"); } return; }
    if (a === "pause" && agent?.taskId) { try { await ctx.agent.pauseTask(agent.taskId); } catch (er) { ctx.toast(er.message, "err"); } return; }
    if (a === "resume" && agent?.taskId) { try { await ctx.agent.resumeTask(agent.taskId); } catch (er) { ctx.toast(er.message, "err"); } }
  });

  async function stop() {
    const a = st.agent;
    if (!a) return;
    if (a.mode === "SPEAKING" || ctx.voice.audio) ctx.voice.stopAll();
    if (a.taskId && a.mode !== "SPEAKING") {
      try { await ctx.agent.cancelTask(a.taskId); } catch (er) { ctx.toast(er.message, "err"); }
    }
  }

  // ---------------------------------------------------------------- ask from any workspace
  input.addEventListener("input", () => renderGo());
  el.querySelector(".cd-ask").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) { if (st.agent?.canStop) stop(); return; }
    input.value = ""; renderGo();
    st.seenReply = null;
    ctx.submit(text, { stay: true });
  });
  function renderGo() {
    const stopMode = !input.value.trim() && st.agent?.canStop && st.agent.mode !== "WAITING_CONFIRMATION";   // there: «Отклонить»
    go.disabled = !input.value.trim() && !stopMode;
    go.innerHTML = stopMode ? ICON.stop : ICON.arrowUp;
    go.setAttribute("aria-label", stopMode ? "Остановить" : "Отправить");
    go.classList.toggle("cd-stop", !!stopMode);
    go.classList.toggle("go", !stopMode);
  }

  // ---------------------------------------------------------------- render
  function scheduleCollapse() {
    clearTimeout(st.collapseTimer);
    if (!st.expanded || st.pinned || !st.autoOpened) return;
    // Opened for a decision: once it is decided, fold away right after the short reaction.
    const decided = st.autoReason === "approval" && st.agent?.mode !== "WAITING_CONFIRMATION";
    st.collapseTimer = setTimeout(() => {
      // A long job keeps running without the card: its state stays in the top bar and the pill.
      if (!st.hover && !st.pinned && st.autoOpened && document.activeElement?.closest?.(".cdock") == null) setExpanded(false);
      else scheduleCollapse();
    }, decided ? 1600 : AUTO_COLLAPSE_MS);
  }

  function attention(a) {
    // Hidden or collapsed companion comes back for what needs the person: a decision, an error, a fresh reply.
    const key = a.mode === "WAITING_CONFIRMATION" ? "a:" + a.approvalId : a.mode === "ERROR" ? "e:" + a.taskId : null;
    if (!key || key === st.lastAttn) return;
    st.lastAttn = key;
    if (st.hidden) setHidden(false);
    if (!st.expanded && a.mode === "WAITING_CONFIRMATION") setExpanded(true, false, "approval");
  }

  function body(a) {
    const S = ctx.S;
    if (a.mode === "STANDBY") {
      return `<div class="cd-actions"><button class="btn" data-cd="estop_clear">${ICON.play} Снять остановку</button></div>`;
    }
    if (a.mode === "WAITING_CONFIRMATION") {
      const ap = S.approvals[a.approvalId];
      const quote = a.quote ? `<div class="cd-quote">${esc(a.quote)}</div>` : "";
      const more = a.count > 1 ? `<div class="faint" style="font-size:12px">Ещё ждут решения: ${a.count - 1} — в панели «Активность».</div>` : "";
      return `${quote}<div class="cd-actions">
        <button class="btn warn" data-cd="approve" data-id="${esc(a.approvalId)}">${ICON.check} ${a.send ? "Отправить" : "Разрешить"}</button>
        ${ap?.tool_id === "message_send" || (ap?.summary?.editable || []).length ? `<button class="btn" data-cd="edit" data-id="${esc(a.approvalId)}">Изменить</button>` : ""}
        <button class="btn ghost" data-cd="deny" data-id="${esc(a.approvalId)}">Отклонить</button>
        </div><div class="faint" style="font-size:12px;display:flex;gap:5px;align-items:center">${ICON.mic.replace("<svg", '<svg style="width:13px;height:13px"')} или скажите «да» / «нет»</div>${more}`;
    }
    const task = a.taskId ? S.tasks[a.taskId] || ctx.findTask?.(a.taskId) : null;
    if (a.busy && task && isUserTask(task)) {
      const rows = planRows(task).slice(-5);
      const plan = rows.length ? `<div class="cd-plan">${rows.map((r) => `<div class="plan-row" data-mark="${r.mark}"><span class="m">${MARK[r.mark]}</span><span title="${esc(r.label)}">${esc(r.label)}</span></div>`).join("")}</div>` : "";
      return `${plan}<div class="cd-actions">
        ${a.canStop ? `<button class="btn danger" data-cd="stop">${ICON.stop} Остановить</button>` : ""}
        ${task.state === "paused" ? `<button class="btn" data-cd="resume">${ICON.play} Продолжить</button>`
          : ["running", "planning"].includes(task.state) ? `<button class="btn ghost" data-cd="pause">${ICON.pause} Пауза</button>` : ""}
        <button class="btn ghost" data-cd="task" data-id="${esc(task.id)}">Подробнее</button></div>`;
    }
    if (a.mode === "SPEAKING" && a.canStop) return `<div class="cd-actions"><button class="btn danger" data-cd="stop">${ICON.stop} Замолчать</button></div>`;
    const r = ctx.lastReply();
    if (r && r.text && Date.now() - r.at < 5 * 60000 && ctx.currentView() !== "chat") {
      return `<div class="cd-reply">${md(r.text.length > 900 ? r.text.slice(0, 900) + "…" : r.text)}</div>
        <div class="cd-actions"><button class="btn ghost" data-cd="chat">Открыть в беседе ${ICON.arrowRight.replace("<svg", '<svg style="width:14px;height:14px"')}</button></div>`;
    }
    return "";
  }

  function render(force = false) {
    const a = deriveAgent(ctx.agentInput());
    st.agent = a;
    const reply = ctx.lastReply();
    const c = ctx.context();
    const sig = JSON.stringify([a, st.expanded, st.pinned, st.docked, c, reply?.at, ctx.currentView(),
      a.taskId && planRows(ctx.S.tasks[a.taskId] || ctx.findTask?.(a.taskId) || { steps: [] })]);
    attention(a);
    // A fresh answer while the person is in another workspace: show it once in the companion.
    if (reply?.pop && reply.at !== st.seenReply && Date.now() - reply.at < 15000 && ctx.currentView() !== "chat" && !st.hidden) {
      st.seenReply = reply.at;
      if (!st.expanded) { setExpanded(true, false, "reply"); return; }
    }
    if (!force && sig === st.sig) return;
    st.sig = sig;
    el.dataset.mode = a.mode;
    el.dataset.quiet = a.mode === "IDLE" ? "1" : "0";
    companion.setState(a.mode, { muted: ctx.voice.muted });
    orb.setAttribute("aria-label", `JARVIS: ${a.label}. Нажмите, чтобы ${st.expanded ? "свернуть" : "развернуть"}; перетащите или используйте стрелки, чтобы переместить`);
    orb.title = `JARVIS · ${a.label}${a.detail ? " — " + a.detail : ""}`;
    const pending = Object.values(ctx.S.approvals).filter((x) => x.status === "pending").length;
    const badge = el.querySelector(".cd-badge");
    badge.hidden = !(pending && !st.expanded);
    badge.textContent = pending;
    el.querySelector(".cd-pill b").textContent = a.label;
    el.querySelector(".cd-pill span").textContent = a.detail || "";
    el.querySelector(".cd-pill .cd-stop").hidden = !a.canStop;
    if (!st.expanded) { renderGo(); return; }
    el.querySelector(".cd-label").textContent = a.label + (a.count > 1 ? ` (${a.count})` : "");
    el.querySelector(".cd-detail").textContent = a.detail || "";
    const ph = el.querySelector(".cd-phases");
    const idx = PHASES.findIndex(([k]) => k === a.phase);
    ph.hidden = idx < 0 || a.mode === "SPEAKING";
    if (!ph.hidden) ph.innerHTML = PHASES.map(([k, l], i) => `<span class="${i < idx ? "done" : i === idx ? "on" : ""}">${l}</span>`).join("<i></i>");
    el.querySelector(".cd-body").innerHTML = body(a);
    el.querySelector(".cd-ctx").innerHTML = `Контекст: <b>${esc(c.title)}</b>${c.detail ? ` · ${esc(c.detail)}` : ""}`;
    const pin = el.querySelector('[data-cd="pin"]');
    pin.setAttribute("aria-pressed", String(st.pinned));
    pin.title = st.pinned ? "Открепить (сворачивать сам)" : "Закрепить открытым";
    el.querySelector('[data-cd="dock"]').hidden = st.docked;
    el.querySelector('[data-cd="mic"]').classList.toggle("recording", ctx.voice.recording);
    el.querySelector('[data-cd="mic"]').disabled = ctx.voice.muted || !ctx.voice.supported;
    renderGo();
    place(st.pos);
    scheduleCollapse();
  }

  // initial
  el.dataset.expanded = st.expanded ? "1" : "0";
  el.dataset.docked = st.docked ? "1" : "0";
  relayout();
  render(true);
  // A short reaction (SUCCESS/ERROR/CANCELLED) must end on time even without new events.
  setInterval(() => { if (st.agent && ["SUCCESS", "ERROR", "CANCELLED"].includes(st.agent.mode)) render(); }, 700);

  return {
    el, render, relayout,
    focus() { if (st.hidden) setHidden(false); setExpanded(true, true); input.focus(); },
    toggle() { if (st.hidden) { setHidden(false); return; } setExpanded(!st.expanded, true); },
    show() { setHidden(false); },
    hide() { setExpanded(false, true); setHidden(true); },
    redock() { st.docked = true; persist(); relayout(); },
    togglePin() { st.pinned = !st.pinned; if (st.pinned) setExpanded(true, true); render(true); persist(); },
    get state() { return { ...st, drag: null }; },
    get agent() { return st.agent; },
    stop,
  };
}
