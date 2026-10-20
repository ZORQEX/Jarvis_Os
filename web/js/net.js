// WebSocket link to the local core with request/response and auto-reconnect.
export class Link {
  constructor({ onSnapshot, onEvent, onStatus }) {
    this.onSnapshot = onSnapshot;
    this.onEvent = onEvent;
    this.onStatus = onStatus;
    this.pending = new Map();
    this.seq = 0;
    this.backoff = 500;
    this.connect();
  }

  connect() {
    const ws = new WebSocket(`ws://${location.host}/ws`);
    this.ws = ws;
    ws.onopen = () => { this.backoff = 500; this.onStatus(true); };
    ws.onmessage = (e) => {
      let m;
      try { m = JSON.parse(e.data); } catch { return; }
      if (m.type === "snapshot") this.onSnapshot(m.data);
      else if (m.type === "event") this.onEvent(m);
      else if (m.type === "reply") {
        const p = this.pending.get(m.req_id);
        if (p) { this.pending.delete(m.req_id); m.ok ? p.resolve(m.data) : p.reject(new Error(m.error)); }
      }
    };
    ws.onclose = () => {
      this.onStatus(false);
      for (const p of this.pending.values()) p.reject(new Error("Нет связи с ядром JARVIS"));
      this.pending.clear();
      setTimeout(() => this.connect(), this.backoff);
      this.backoff = Math.min(this.backoff * 2, 8000);
    };
  }

  get open() { return this.ws && this.ws.readyState === WebSocket.OPEN; }

  request(type, payload = {}, timeoutMs = 60000) {
    return new Promise((resolve, reject) => {
      if (!this.open) return reject(new Error("Нет связи с ядром JARVIS"));
      const req_id = ++this.seq;
      const timer = setTimeout(() => {
        if (this.pending.delete(req_id)) reject(new Error("Ядро не ответило вовремя"));
      }, timeoutMs);
      this.pending.set(req_id, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      this.ws.send(JSON.stringify({ type, req_id, ...payload }));
    });
  }
}
