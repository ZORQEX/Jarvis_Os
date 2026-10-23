// AgentStream: the only way the UI talks to the agent. The UI knows sendMessage / cancelTask / confirmAction /
// denyAction and a stream of events; it never knows which model (Claude through Claude Code today) is behind the
// core, and holds no credentials. Streaming is real: the core pushes assistant.delta, step.updated, approval.*.
//
// Event kinds the UI renders (from the core): task.created · task.updated · step.updated · step.progress ·
// approval.created · approval.updated · assistant.delta · message · core.state · speech.* · ui.navigate

export class AgentStream {
  /** @param {import("../net.js").Link} link */
  constructor(link) { this.link = link; }

  /** @returns {Promise<string>} task id ("" when the core answered without a task, e.g. «стоп») */
  async sendMessage(text, { source = "text", speak = false } = {}) {
    const r = await this.link.request("submit", { text, source, speak }, 15000);
    return r?.task_id || "";
  }
  cancelTask(taskId) { return this.link.request("cancel", { task_id: taskId }); }
  pauseTask(taskId) { return this.link.request("pause", { task_id: taskId }); }
  resumeTask(taskId) { return this.link.request("resume", { task_id: taskId }); }
  /** @param {object|null} edited  changed arguments (content fields only); null = as proposed */
  confirmAction(approvalId, edited = null, remember = false) { return this.link.request("approve", { id: approvalId, edited, remember }); }
  denyAction(approvalId) { return this.link.request("deny", { id: approvalId }); }
  control(op) { return this.link.request(op); }                     // estop · estop_clear · pause_all · resume_all · stop_speech
  taskDetail(taskId) { return this.link.request("task.detail", { task_id: taskId }); }
  recentTasks(limit = 100) { return this.link.request("tasks.list", { limit }); }
}
