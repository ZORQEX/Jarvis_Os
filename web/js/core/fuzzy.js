// Command palette search: every query word must match the start of a word in the title or keywords
// (or appear inside them); earlier and title matches rank higher. Small and fast — the list is a few dozen items.

export const fold = (s) => String(s || "").toLowerCase().replace(/ё/g, "е");

/** @returns {number} 0 = no match; higher = better */
export function score(query, title, keywords = "") {
  const q = fold(query).split(/\s+/).filter(Boolean);
  if (!q.length) return 1;
  const t = fold(title), k = fold(keywords);
  const words = (t + " " + k).split(/[\s/·:,()«»—-]+/).filter(Boolean);
  let total = 0;
  for (const part of q) {
    let best = 0;
    if (t.startsWith(part)) best = 100;
    else if (t.split(/\s+/).some((w) => w.startsWith(part))) best = 60;
    else if (words.some((w) => w.startsWith(part))) best = 40;
    else if (t.includes(part)) best = 20;
    else if (k.includes(part)) best = 10;
    if (!best) return 0;
    total += best;
  }
  return total - Math.min(t.length, 60) / 100;     // shorter titles win ties
}

/** Rank items {title, keywords} for a query, best first; items that do not match are dropped. */
export function rank(query, items, limit = 50) {
  return items.map((it) => ({ it, s: score(query, it.title, it.keywords) }))
    .filter((x) => x.s > 0).sort((a, b) => b.s - a.s).slice(0, limit).map((x) => x.it);
}
