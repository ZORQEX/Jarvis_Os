// Communication adapters: Telegram, WhatsApp and mail behind one interface for the CommunicationWorkspace.
// Each adapter describes what the core can really do for that service. Sending always goes through JARVIS
// (the core's comms agent resolves the person, drafts, and asks for confirmation before anything leaves).
// Reading other people's messages is not implemented in the core — the adapters say so instead of faking a chat.

/** @typedef {{id:string, name:string, address:string, last:number, preview:string, jobs:object[]}} Person */

class ChannelAdapter {
  /** @param {string} channel core channel id @param {string} title @param {string} toPhrase «в Telegram» */
  constructor(channel, title, toPhrase) {
    this.channel = channel; this.title = title; this.toPhrase = toPhrase;
    this.canReadChats = false;              // the core does not read incoming conversations yet
  }
  /** The phrase the core's comms agent understands («Отправь Ивану в Telegram: …»). */
  composePhrase(name, text, when = "") {
    return `Отправь ${name} ${this.toPhrase}${when ? " " + when : ""}: ${text}`;
  }
  /** Address book entries usable on this channel. */
  contactAddress(c) { return ""; }
  /**
   * People for the conversation list: the JARVIS address book plus everyone JARVIS has sent to on this channel.
   * @param {object[]} contacts comms.state contacts  @param {object[]} jobs comms.jobs
   * @returns {Person[]}
   */
  people(contacts, jobs) {
    const map = new Map();
    const key = (name) => name.trim().toLowerCase();
    for (const c of contacts || []) {
      const addr = this.contactAddress(c);
      if (!addr) continue;
      map.set(key(c.name), { id: "c:" + c.id, name: c.name, address: addr, last: 0, preview: "", jobs: [] });
    }
    // An edited message replaces its earlier version (replaced_by): only the current one is shown.
    for (const j of (jobs || []).filter((x) => x.channel === this.channel && !x.replaced_by)) {
      const k = key(j.to_name);
      const p = map.get(k) || { id: "j:" + j.to_id, name: j.to_name, address: j.to_address || "", last: 0, preview: "", jobs: [] };
      p.jobs.push(j);
      const ts = j.sent_at || j.due_at || j.created_at;
      if (ts > p.last) { p.last = ts; p.preview = j.body; }
      if (!p.address && j.to_address) p.address = j.to_address;
      map.set(k, p);
    }
    for (const p of map.values()) p.jobs.sort((a, b) => (a.sent_at || a.due_at || a.created_at) - (b.sent_at || b.due_at || b.created_at));
    return [...map.values()].sort((a, b) => b.last - a.last || a.name.localeCompare(b.name, "ru"));
  }
}

class TelegramAdapter extends ChannelAdapter {
  constructor() { super("telegram", "Telegram", "в Telegram"); }
  contactAddress(c) { return c.telegram || ""; }
}
class WhatsAppAdapter extends ChannelAdapter {
  constructor() { super("whatsapp", "WhatsApp", "в WhatsApp"); }
  contactAddress(c) { return c.phone || ""; }
}
class MailAdapter extends ChannelAdapter {
  constructor() { super("email", "Почта", "на почту"); }
  contactAddress(c) { return c.email || ""; }
}

export const ADAPTERS = { telegram: new TelegramAdapter(), whatsapp: new WhatsAppAdapter(), email: new MailAdapter() };
