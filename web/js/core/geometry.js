// Companion placement: keep it inside the window, snap to edges, and remember where it is relative to the nearest
// corner so a window resize keeps it where the person put it (a companion left at the bottom-right stays there).

/** @typedef {{x:number, y:number}} Pt  @typedef {{w:number, h:number}} Size */

/** Keep the box fully visible with a margin. */
export function clamp(p, size, view, margin = 8) {
  const maxX = Math.max(margin, view.w - size.w - margin);
  const maxY = Math.max(margin, view.h - size.h - margin);
  return { x: Math.min(maxX, Math.max(margin, p.x)), y: Math.min(maxY, Math.max(margin, p.y)) };
}

/** Pull the box to an edge when it is dropped close to it. */
export function snap(p, size, view, threshold = 28, margin = 12) {
  let { x, y } = p;
  if (x < margin + threshold) x = margin;
  else if (view.w - (x + size.w) < margin + threshold) x = view.w - size.w - margin;
  if (y < margin + threshold) y = margin;
  else if (view.h - (y + size.h) < margin + threshold) y = view.h - size.h - margin;
  return clamp({ x, y }, size, view, margin);
}

/** Position → distances from the nearest horizontal and vertical edges. */
export function toAnchor(p, size, view) {
  const right = view.w - (p.x + size.w), bottom = view.h - (p.y + size.h);
  return {
    h: p.x <= right ? "left" : "right", dx: Math.round(p.x <= right ? p.x : right),
    v: p.y <= bottom ? "top" : "bottom", dy: Math.round(p.y <= bottom ? p.y : bottom),
  };
}

/** Anchor → position in a (possibly resized) window, clamped. */
export function fromAnchor(a, size, view, margin = 8) {
  const x = a.h === "right" ? view.w - size.w - a.dx : a.dx;
  const y = a.v === "bottom" ? view.h - size.h - a.dy : a.dy;
  return clamp({ x, y }, size, view, margin);
}

/** Dropped near its home slot in the top bar → dock it again. */
export function nearDock(p, dock, threshold = 44) {
  return Math.hypot(p.x - dock.x, p.y - dock.y) <= threshold;
}

/** Validate a stored companion record; anything odd falls back to the docked default. */
export function readStored(raw) {
  const def = { docked: true, anchor: null, expanded: false, pinned: false, hidden: false };
  if (!raw || typeof raw !== "object") return def;
  const a = raw.anchor;
  const okAnchor = a && ["left", "right"].includes(a.h) && ["top", "bottom"].includes(a.v)
    && Number.isFinite(a.dx) && Number.isFinite(a.dy) && a.dx >= 0 && a.dy >= 0 && a.dx < 10000 && a.dy < 10000;
  return {
    docked: raw.docked !== false || !okAnchor,
    anchor: okAnchor ? { h: a.h, v: a.v, dx: a.dx, dy: a.dy } : null,
    expanded: !!raw.expanded, pinned: !!raw.pinned, hidden: false,   // «hide» is temporary: never survives a reload
  };
}
