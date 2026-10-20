// The JARVIS companion: an original small character (rounded lens-shaped body,
// two capsule eyes, a light halo). Every visual state is driven by real runtime
// state passed in from the core or the voice module — nothing is decorative.

const SVG = (id) => `
<svg class="companion" viewBox="0 0 120 120" role="img" aria-label="JARVIS">
  <defs>
    <radialGradient id="cbody-${id}" cx="38%" cy="30%" r="80%">
      <stop offset="0" stop-color="var(--c-body-1)"/><stop offset="1" stop-color="var(--c-body-2)"/>
    </radialGradient>
  </defs>
  <circle class="glow" cx="60" cy="60" r="40"/>
  <circle class="halo" cx="60" cy="60" r="52"/>
  <circle class="ring" cx="60" cy="60" r="47"/>
  <g class="orbit"><circle cx="60" cy="9" r="3.2"/></g>
  <g class="body">
    <path class="shell" style="fill:url(#cbody-${id})" d="M60 18c25 0 40 15 40 40 0 27-16 44-40 44S20 85 20 58c0-25 15-40 40-40z"/>
    <path class="face" d="M60 34c17 0 27 9 27 24 0 16-10 27-27 27S33 74 33 58c0-15 10-24 27-24z"/>
    <ellipse class="sheen" cx="46" cy="30" rx="14" ry="6" transform="rotate(-18 46 30)"/>
    <g class="eyes">
      <rect class="eye l" x="45" y="50" width="9" height="15" rx="4.5"/>
      <rect class="eye r" x="66" y="50" width="9" height="15" rx="4.5"/>
      <path class="happy" d="M44.5 60q5-6 10 0M65.5 60q5-6 10 0"/>
    </g>
    <rect class="lid" x="38" y="44" width="44" height="24" rx="6"/>
  </g>
  <g class="badge"><circle cx="94" cy="26" r="9"/>
    <path d="M90 22l8 8" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/></g>
</svg>`;

let counter = 0;
const instances = new Set();
let reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", (e) => { reduced = e.matches; });

export function mount(container) {
  const id = ++counter;
  container.innerHTML = SVG(id);
  const el = container.querySelector("svg");
  el.querySelector(".shell").setAttribute("style", `fill:url(#cbody-${id})`);
  const inst = { el, container };
  instances.add(inst);
  el.dataset.state = current.state;
  el.dataset.muted = current.muted ? "1" : "0";
  return inst;
}

export function unmountAll(container) {
  for (const i of instances) if (i.container === container) instances.delete(i);
}

const current = { state: "OFFLINE", muted: false };

export function setState(state, { muted } = {}) {
  current.state = state;
  if (muted !== undefined) current.muted = muted;
  for (const i of instances) {
    if (!i.el.isConnected) { instances.delete(i); continue; }
    if (i.el.dataset.state !== state) i.el.dataset.state = state;
    i.el.dataset.muted = current.muted ? "1" : "0";
  }
}

export function setLevel(v) {
  const x = Math.max(0, Math.min(1, v)).toFixed(3);
  for (const i of instances) i.el.style.setProperty("--lvl", x);
}

// Gaze: eyes follow the pointer subtly (max ~3px), only while the pointer is in the window.
let raf = 0, px = 0, py = 0;
addEventListener("pointermove", (e) => {
  px = e.clientX; py = e.clientY;
  if (!raf) raf = requestAnimationFrame(updateGaze);
}, { passive: true });
document.addEventListener("pointerleave", () => {
  for (const i of instances) { i.el.style.setProperty("--gx", "0px"); i.el.style.setProperty("--gy", "0px"); }
});

function updateGaze() {
  raf = 0;
  if (reduced || document.hidden) return;
  for (const i of instances) {
    if (!["IDLE", "SPEAKING", "COMPLETED", "PROCESSING"].includes(current.state)) {
      i.el.style.removeProperty("--gx"); i.el.style.removeProperty("--gy"); continue;
    }
    const r = i.el.getBoundingClientRect();
    if (!r.width) continue;
    const dx = px - (r.left + r.width / 2), dy = py - (r.top + r.height / 2);
    const d = Math.hypot(dx, dy) || 1;
    const k = Math.min(1, d / 400) * 3.2;
    i.el.style.setProperty("--gx", `${(dx / d * k).toFixed(2)}px`);
    i.el.style.setProperty("--gy", `${(dy / d * k).toFixed(2)}px`);
  }
}

// Blinking: occasional, irregular; skipped when hidden or reduced motion.
function scheduleBlink() {
  setTimeout(() => {
    if (!reduced && !document.hidden && ["IDLE", "SPEAKING", "PROCESSING"].includes(current.state)) {
      for (const i of instances) {
        i.el.querySelectorAll(".eye").forEach((e) => {
          e.classList.remove("blink"); void e.getBBox(); e.classList.add("blink");
        });
      }
    }
    scheduleBlink();
  }, 2800 + Math.random() * 4200);
}
scheduleBlink();

// Pause all CSS animation while the window is hidden: zero idle GPU cost.
document.addEventListener("visibilitychange", () => {
  document.documentElement.classList.toggle("anim-off", document.hidden);
});
