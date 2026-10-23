// DesktopAdapter: OS-level actions behind one interface. The window is portable (any Chromium); the operating-system
// side lives in the core (jarvis/tools/desktop.py — Linux/Hyprland today; a Windows or Quickshell implementation
// would replace the core tools, not this file). Every call goes through the core's gateway (policy + audit).

export class DesktopAdapter {
  constructor(link) { this.link = link; this.platform = "core"; }
  /** Opens in the person's default browser. Recorded in the conversation as an action. */
  openUrl(url) { return this.link.request("tool.run", { tool: "open_url", args: { url } }); }
  /** Opens a file or folder in its usual application. */
  openPath(path) { return this.link.request("tool.run", { tool: "open_path", args: { path } }); }
  /** Read-only, not recorded in the conversation (a folder click is not a conversation turn). */
  async listFolder(path, hidden = false) { return this.#read("folder_list", { path, hidden }); }
  async searchFiles(query, folder = null) { return this.#read("search_files", folder ? { query, folder, limit: 50 } : { query, limit: 50 }); }
  async readText(path, maxChars = 6000) { return this.#read("read_file", { path, max_chars: maxChars }); }
  roots() { return this.link.request("files.roots"); }
  async #read(tool, args) {
    const r = await this.link.request("ui.read", { tool, args }, 30000);
    if (r.status !== "ok") throw new Error(r.error || "Не получилось");
    return r.data;
  }
}
