// The 2D plan of the master model as SVG (pure: model in, markup out — unit-tested in Node). Millimetres, the plan's
// Y up drawn as SVG's Y down. Every element carries data-id, so a click selects the same element as in the 3D view.
// Optional underlay: the source drawing placed by the scale and origin the analysis found — the recognised walls
// should sit on the drawn ones (the 2D ↔ model link at a glance).

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const f1 = (v) => Math.round(v * 10) / 10;

const ROOM_TINT = {
  kitchen: "#e9b872", living: "#8fb8ff", kitchen_living: "#b7a3ff", bedroom: "#7fd1a5", children: "#f2a7c8",
  bathroom: "#6fd3e0", toilet: "#6fd3e0", hall: "#c9c2b2", corridor: "#c9c2b2", balcony: "#a3c97f",
  storage: "#b0b0b0", office: "#d8a6ff", dining: "#e9b872",
};

function unit(ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, n = Math.hypot(dx, dy) || 1;
  return [dx / n, dy / n];
}

/** Points of a wall's outline in plan coordinates. */
export function wallOutline(w) {
  const [ax, ay] = w.start, [bx, by] = w.end;
  const [ux, uy] = unit(ax, ay, bx, by);
  const nx = -uy * w.thickness / 2, ny = ux * w.thickness / 2;
  return [[ax + nx, ay + ny], [bx + nx, by + ny], [bx - nx, by - ny], [ax - nx, ay - ny]];
}

export function bounds(model) {
  const xs = [], ys = [];
  for (const w of model.walls || []) for (const p of [w.start, w.end]) { xs.push(p[0]); ys.push(p[1]); }
  for (const f of model.furniture || []) { xs.push(f.position[0]); ys.push(f.position[1]); }
  if (!xs.length) return { x0: 0, y0: 0, x1: 10000, y1: 8000 };
  return { x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys) };
}

const P = (p) => `${f1(p[0])},${f1(-p[1])}`;

/**
 * @param {object} model master model (architectural_model.json)
 * @param {{selected?:string, level?:string, underlay?:{url:string,w:number,h:number,mm_per_px:number,origin_px:number[]}, opacity?:number, lowConf?:number}} o
 */
export function planSvg(model, o = {}) {
  const lv = o.level || model.levels?.[0]?.id;
  const on = (e) => !lv || !e.level || e.level === lv;
  const b = bounds(model);
  const pad = 1400;
  const vb = [b.x0 - pad, -(b.y1 + pad), (b.x1 - b.x0) + 2 * pad, (b.y1 - b.y0) + 2 * pad].map(f1);
  const low = o.lowConf ?? 0.5;
  const sel = o.selected || "";
  const parts = [];
  const u = o.underlay;
  if (u && u.mm_per_px && u.origin_px) {
    const s = u.mm_per_px, [ox, oy] = u.origin_px;
    parts.push(`<image class="ap-under" href="${esc(u.url)}" x="${f1(-ox * s)}" y="${f1(-oy * s)}" width="${f1(u.w * s)}" height="${f1(u.h * s)}" opacity="${o.opacity ?? 0.45}" preserveAspectRatio="none"/>`);
  }
  for (const r of (model.rooms || []).filter(on)) {
    const tint = ROOM_TINT[r.kind] || "#9aa3ad";
    parts.push(`<polygon class="ap-room${r.id === sel ? " sel" : ""}" data-id="${esc(r.id)}" points="${r.polygon.map(P).join(" ")}" style="--tint:${tint}"/>`);
  }
  const walls = (model.walls || []).filter(on);
  const byId = Object.fromEntries(walls.map((w) => [w.id, w]));
  for (const w of walls) {
    const lowc = (w.confidence ?? 1) < low;
    parts.push(`<polygon class="ap-wall${w.kind === "exterior" ? " ext" : ""}${lowc ? " low" : ""}${w.id === sel ? " sel" : ""}" data-id="${esc(w.id)}" points="${wallOutline(w).map(P).join(" ")}"><title>${esc(w.id)} · ${Math.round(w.thickness)} мм${lowc ? " · уверенность " + (w.confidence ?? 0).toFixed(2) : ""}</title></polygon>`);
  }
  for (const op of [...(model.doors || []), ...(model.windows || [])].filter(on)) {
    const w = byId[op.host];
    if (!w) continue;
    const [ux, uy] = unit(w.start[0], w.start[1], w.end[0], w.end[1]);
    const nx = -uy, ny = ux, t = w.thickness / 2 + 2;
    const cx = w.start[0] + ux * op.offset, cy = w.start[1] + uy * op.offset, hw = op.width / 2;
    const a = [cx - ux * hw, cy - uy * hw], bb = [cx + ux * hw, cy + uy * hw];
    const gap = [[a[0] + nx * t, a[1] + ny * t], [bb[0] + nx * t, bb[1] + ny * t], [bb[0] - nx * t, bb[1] - ny * t], [a[0] - nx * t, a[1] - ny * t]];
    const cls = `${op.type === "door" ? "ap-door" : "ap-win"}${(op.confidence ?? 1) < low ? " low" : ""}${op.id === sel ? " sel" : ""}`;
    let g = `<g class="${cls}" data-id="${esc(op.id)}"><title>${esc(op.id)} · ${Math.round(op.width)} мм</title><polygon class="gap" points="${gap.map(P).join(" ")}"/>`;
    if (op.type === "window") {
      for (const k of [-w.thickness / 4, 0, w.thickness / 4]) g += `<line x1="${f1(a[0] + nx * k)}" y1="${f1(-(a[1] + ny * k))}" x2="${f1(bb[0] + nx * k)}" y2="${f1(-(bb[1] + ny * k))}"/>`;
    } else if (op.kind !== "opening") {
      const right = op.swing === "right";
      const h = right ? bb : a, other = right ? a : bb;
      const h0 = [h[0] + nx * t, h[1] + ny * t];
      const leaf = [h0[0] + nx * op.width, h0[1] + ny * op.width];
      const closed = [other[0] + nx * t, other[1] + ny * t];
      const sweep = right ? 1 : 0;
      g += `<line class="leaf" x1="${f1(h0[0])}" y1="${f1(-h0[1])}" x2="${f1(leaf[0])}" y2="${f1(-leaf[1])}"/>`;
      g += `<path class="swing" d="M${P(leaf)} A${f1(op.width)},${f1(op.width)} 0 0 ${sweep} ${P(closed)}"/>`;
    }
    parts.push(g + "</g>");
  }
  for (const c of (model.columns || []).filter(on)) {
    parts.push(`<rect class="ap-col${c.id === sel ? " sel" : ""}" data-id="${esc(c.id)}" x="${f1(c.center[0] - c.width / 2)}" y="${f1(-c.center[1] - c.depth / 2)}" width="${f1(c.width)}" height="${f1(c.depth)}"/>`);
  }
  for (const s of (model.stairs || []).filter(on)) {
    const a = (s.direction || 0) * Math.PI / 180, ux = Math.cos(a), uy = Math.sin(a), nx = -uy, ny = ux;
    const p0 = s.start, w2 = s.width / 2;
    const quad = [[p0[0] + nx * w2, p0[1] + ny * w2], [p0[0] + ux * s.length + nx * w2, p0[1] + uy * s.length + ny * w2],
      [p0[0] + ux * s.length - nx * w2, p0[1] + uy * s.length - ny * w2], [p0[0] - nx * w2, p0[1] - ny * w2]];
    let g = `<g class="ap-stair${s.id === sel ? " sel" : ""}" data-id="${esc(s.id)}"><polygon points="${quad.map(P).join(" ")}"/>`;
    for (let i = 1; i < s.steps; i++) {
      const d = s.length * i / s.steps;
      g += `<line x1="${f1(p0[0] + ux * d + nx * w2)}" y1="${f1(-(p0[1] + uy * d + ny * w2))}" x2="${f1(p0[0] + ux * d - nx * w2)}" y2="${f1(-(p0[1] + uy * d - ny * w2))}"/>`;
    }
    parts.push(g + "</g>");
  }
  for (const fu of (model.furniture || []).filter(on)) {
    const [w, d] = fu.size;
    parts.push(`<g class="ap-furn${fu.id === sel ? " sel" : ""}${(fu.confidence ?? 1) < low ? " low" : ""}" data-id="${esc(fu.id)}" transform="translate(${f1(fu.position[0])} ${f1(-fu.position[1])}) rotate(${f1(-(fu.rotation || 0))})"><title>${esc(fu.name)} ${esc(fu.id)}</title>` +
      `<rect x="${f1(-w / 2)}" y="${f1(-d / 2)}" width="${f1(w)}" height="${f1(d)}" rx="40"/><line class="front" x1="${f1(-w / 2 + 60)}" y1="${f1(d / 2 - 40)}" x2="${f1(w / 2 - 60)}" y2="${f1(d / 2 - 40)}"/></g>`);
  }
  for (const r of (model.rooms || []).filter(on)) {
    const [x, y] = r.label || r.polygon[0];
    const area = r.net_area_m2 ?? r.area_m2;
    parts.push(`<g class="ap-label" data-id="${esc(r.id)}"><text x="${f1(x)}" y="${f1(-y - 60)}">${esc(r.name)}</text><text class="a" x="${f1(x)}" y="${f1(-y + 300)}">${String(area).replace(".", ",")} м²</text></g>`);
  }
  // overall sizes along the bottom and the right side
  const W = b.x1 - b.x0, H = b.y1 - b.y0;
  if (W > 0 && H > 0) {
    const yb = b.y0 - 900, xr = b.x1 + 900;
    parts.push(`<g class="ap-dim"><line x1="${f1(b.x0)}" y1="${f1(-yb)}" x2="${f1(b.x1)}" y2="${f1(-yb)}"/><line x1="${f1(b.x0)}" y1="${f1(-yb - 150)}" x2="${f1(b.x0)}" y2="${f1(-yb + 150)}"/><line x1="${f1(b.x1)}" y1="${f1(-yb - 150)}" x2="${f1(b.x1)}" y2="${f1(-yb + 150)}"/><text x="${f1((b.x0 + b.x1) / 2)}" y="${f1(-yb - 120)}">${Math.round(W)}</text>` +
      `<line x1="${f1(xr)}" y1="${f1(-b.y0)}" x2="${f1(xr)}" y2="${f1(-b.y1)}"/><line x1="${f1(xr - 150)}" y1="${f1(-b.y0)}" x2="${f1(xr + 150)}" y2="${f1(-b.y0)}"/><line x1="${f1(xr - 150)}" y1="${f1(-b.y1)}" x2="${f1(xr + 150)}" y2="${f1(-b.y1)}"/><text transform="translate(${f1(xr + 260)} ${f1(-(b.y0 + b.y1) / 2)}) rotate(-90)">${Math.round(H)}</text></g>`);
  }
  return `<svg class="ap-svg" viewBox="${vb.join(" ")}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="План: ${esc(model.name || "")}">${parts.join("")}</svg>`;
}

/** Short lines about each element for the lists (Russian; numbers from the model only). */
export function describe(model, e) {
  const rooms = Object.fromEntries((model.rooms || []).map((r) => [r.id, r.name]));
  if (!e) return "";
  if (e.type === "wall") {
    const L = Math.hypot(e.end[0] - e.start[0], e.end[1] - e.start[1]);
    const side = (e.rooms || []).map((x) => rooms[x] || x).join(" / ") || "снаружи";
    return `${(L / 1000).toFixed(2)} м · ${Math.round(e.thickness)} мм · ${side}`;
  }
  if (e.type === "door" || e.type === "window") {
    const side = (e.rooms || []).map((x) => (x === "outside" ? "улица" : rooms[x] || x)).join(" — ");
    return `${Math.round(e.width)}×${Math.round(e.height)} мм · ${side}`;
  }
  if (e.type === "room") return `${String(e.net_area_m2 ?? e.area_m2).replace(".", ",")} м²`;
  if (e.type === "furniture") return `${rooms[e.room] || "вне помещений"} · ${e.size.map((v) => Math.round(v / 10)).join("×")} см`;
  return e.id;
}

/** Find an element of the model by id. */
export function findEl(model, id) {
  if (!model || !id) return null;
  for (const k of ["walls", "rooms", "doors", "windows", "furniture", "columns", "stairs", "slabs", "levels"]) {
    const e = (model[k] || []).find((x) => x.id === id);
    if (e) return e;
  }
  return model.roof && model.roof.id === id ? model.roof : null;
}
