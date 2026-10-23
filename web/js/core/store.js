// A tiny topic bus. The app owns the state object; after it changes a slice it emits the slice's topic, and only
// the components that listen to that topic redraw (a waveform tick or a streamed token never redraws the shell).
// Topics: link · core · tasks · approvals · messages · stream · voice · workspace · providers · reminders

export function createBus() {
  const subs = new Map();
  return {
    /** @returns {() => void} unsubscribe */
    on(topics, fn) {
      const list = Array.isArray(topics) ? topics : [topics];
      for (const t of list) { if (!subs.has(t)) subs.set(t, new Set()); subs.get(t).add(fn); }
      return () => { for (const t of list) subs.get(t)?.delete(fn); };
    },
    emit(topic, data) {
      for (const fn of [...(subs.get(topic) || [])]) {
        try { fn(data, topic); } catch (e) { console.error(`bus ${topic}:`, e); }
      }
    },
  };
}

/** Coalesce many emits in one frame into a single call (e.g. a burst of step updates). */
export function frameBatch(fn) {
  let raf = 0;
  return () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; fn(); }); };
}

/** localStorage that never throws (private windows, blocked storage) and never stores secrets. */
export const prefs = {
  get(key, def = null) {
    try { const v = localStorage.getItem("jarvis." + key); return v == null ? def : JSON.parse(v); } catch { return def; }
  },
  set(key, value) {
    try { localStorage.setItem("jarvis." + key, JSON.stringify(value)); } catch { /* storage unavailable */ }
  },
};
