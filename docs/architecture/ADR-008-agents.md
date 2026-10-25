# ADR-008 — OpenClaw, Hermes and the coding agents inside JARVIS (2026-10-25)

## Context
The owner asked to install morph-rq/jarvis-os and bring in all of its features — panels, Coucou, fast research, Hermes
and OpenClaw — keeping JARVIS's UI and everything that already works. Earlier ports (ADR-007 and before) had taken the
computer/desk tools, calendar, phone bot, proactive notices, smart home, meeting notes and the fast search. What was left
was jarvis-os's newest part (commits 2108454…04b85b2): OpenClaw and Hermes Agent as backends behind one capability
registry, the Capabilities Hub panel, and the coding-agent monitor whose design came from Coucou (louis-cfm/coucou).

jarvis-os ran both agents on Gemini with API keys and showed the hub in a Quickshell (QML) panel. JARVIS is Claude only
(ADR-005), has no API keys on purpose, and its UI is the WorkOS web window (ADR-006). Installing jarvis-os as a whole
would have meant a second assistant (`jarvisd`, its own `jarvis` command, Caelestia shell) — the opposite of ADR-007.

## Decision
1. **The agents are JARVIS's children, not a second assistant.** `scripts/install-agents.sh` installs the versions
   jarvis-os pinned (Node 24.22.1 sha256-checked, OpenClaw 2026.10.6 from its npm lock with install scripts off, Hermes
   0.19.0 from its hash-locked wheels) under `~/.local/share/jarvis/agents/<name>/<ver>` with a `current` link. The core
   starts them on loopback (OpenClaw 18789, Hermes 18642), each with a key it keeps in the keyring and passes only in
   the child's environment (inherited `*_KEY/_TOKEN/_SECRET` are stripped). Their own data stays in `~/.openclaw` and
   `~/.hermes` (a dev/test `JARVIS_HOME` gets its own folders). JARVIS changes only what it needs, through their own
   config tools, and logs every change in `<data>/hub/migrations.log`.
2. **Both think with Claude through the person's own Claude Code sign-in.**
   - OpenClaw: its documented `claude-cli/…` route runs the installed `claude` program. OpenClaw never touches the
     login tokens.
   - Hermes: it could read Claude Code's saved OAuth token and present itself as Claude Code. That is credential reuse
     outside the official program, so it is not used. JARVIS serves `/llm/v1/chat/completions` on loopback instead,
     behind its own key, and answers each turn with one locked-down `claude -p` run: no tools, no settings or hooks, an
     empty folder, the prompt on stdin.
   - Hermes's tools come back as `tool_calls`. They are requested as JSON "actions" in plain text, not with
     `--json-schema`, because the schema tool made Claude also try to call the agent's tools itself (measured +10 s and
     a confused answer).
   - Measured: 2.5–6 s per turn.
3. **One catalog of everything** (`jarvis/hub/registry.py`, `select.py`).
   - It covers JARVIS's tools, skills and connections, OpenClaw's skills, plugins and channels, Hermes's skills, and
     the shared skills folder. It deduplicates by content hash; an upstream skill JARVIS serves itself (weather, web
     search) is marked a duplicate and never routed to.
   - Ranking weighs relevance × method × health × reliability × authentication × latency. Russian words map to the
     English the skills are written in.
   - The router stays stage 1 (no model). The model sees a few tools, not hundreds.
4. **One risk per tool**, so jarvis-os's single `capability` tool is split:
   - LOW: `capability_find` (find / search ClawHub / review / assess / updates), `service_connect` (shows the card),
     `agents_stop`, `coding_agents`.
   - MODERATE: `specialist` (hand work to Hermes), `skill_manage`, `skill_build`, `service_disconnect`,
     `agents_resume`.
   - HIGH: `capability_use` (OpenClaw's agent may run commands), `skill_install` (only clean, reviewed releases; the
     installed SKILL.md must hash to the reviewed card), `skill_share`.
   - Every step Hermes marks risky becomes a JARVIS approval card (`hermes_step`, HIGH) on the same task.
   - Agent output is untrusted data.
5. **The agents search through JARVIS's fast search.** The MCP bridge (`jarvis/mcp_bridge.py`) uses a bridge file that
   names only `web_search`, with its own key, at `/mcp/research`. Each call goes through the Gateway, under the task
   that delegated the work, or under a small visible task of its own («Агент: поиск …»).
6. **OpenClaw on demand.**
   - Measured: OpenClaw keeps ~1.5 GB resident (gateway + workers) and needs 10–25 s to be ready; Hermes uses 166 MB.
   - Hermes stays on. OpenClaw starts when a skill, ClawHub or the panel needs it, and sleeps after
     `agents_idle_min` (20). While it sleeps its skills stay in the catalog, marked `sleeping`.
   - It stays up when a channel was connected through it, the person's own automation exists, or «держать запущенным»
     is on.
   - Its recurring heartbeat (a Claude call every 30–60 min) is set to `0m`, but only if the person set no cadence of
     their own.
7. **Coding agents (Coucou's idea).**
   - `bin/jarvis-agent-hook` is the relay: standard library only, always exit 0. It is registered as a hook in every
     Claude Code account (`~/.claude`, `~/.claude-N`) and in Codex, only when the person presses «Подключить». The
     settings are merged: backup first, only our entries touched, an unreadable file never overwritten.
   - The relay talks to an owner-only socket (`$XDG_RUNTIME_DIR/jarvis/hooks.sock`, 0700/0600). JARVIS shows each
     session's state, current step and result.
   - Answering permission requests from the window is a separate opt-in. A card shows on any view and is acknowledged
     only while the window is visible. If it is not acknowledged within 2 s, or not answered within `wait_s`, the agent
     asks in its terminal as before. JARVIS never answers for the person.
   - Claude runs that JARVIS's own agents start are not reported.
8. **«Возможности» is a JARVIS workspace** (Ctrl+0), in the window's own look, with eight sections: overview, skills,
   channels, ClawHub, automations, updates, «На компьютере», journal. Buttons that change something run the same
   tools (and approval cards) as a spoken request. Tokens are typed only there.

## Consequences
- One assistant still: JARVIS's voice, window, approvals, memory and task list. Hermes and OpenClaw work appears as
  steps of JARVIS tasks.
- Disk: ~0.9 GB for the agents. Memory: Hermes ~170 MB always; OpenClaw ~1.5 GB only while awake.
- Subscription use: Hermes turns and OpenClaw turns count against the person's Claude limits, like JARVIS's own.
  OpenClaw's daily «Memory Dreaming» job stays (it is visible and can be run or inspected in the panel).
- Not ported: jarvis-os's Gemini Live voice, its Quickshell/Caelestia shell and island (JARVIS has its window and
  desktop companion), its own gateway for chat apps (JARVIS has its comms module and phone bot; OpenClaw now adds bot
  channels).
- Verified live on this laptop (dev core): OpenClaw connected (505 methods, 55 skills, 62 plugins); an agent turn via
  `claude-cli` (7.6 s); Hermes answering, reading a file and searching through JARVIS (3.8–14 s); «Поручи Гермесу …»
  end to end through the approval card; a real Claude Code `Bash` permission request answered from the window.
