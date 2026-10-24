# ADR-006: WorkOS window — workspaces, global companion, adapters

Date: 2026-10-23 · Status: accepted

## Context
The window was one conversation page with a small rail (chat, history, Flora, messages, settings). The product brief
asks for a desktop AI operating interface: a persistent companion over workspaces (Home, Browser, Telegram, WhatsApp,
mail, Files, Tasks, Flora), a task center with an execution timeline, confirmations only for consequential actions,
stop/cancel everywhere, a command palette, and adapters so new services plug in without rewriting the core.
The brief mentions React; the existing UI is a no-build ES-module app under a strict CSP that works and is tested.

## Decision
- **Keep the stack.** No framework, no build step, no second router or state manager. One state object in `app.js`
  plus a small topic bus (`core/store.js`); a part redraws only for its topics. Typed by JSDoc where it matters.
- **Pure logic is separate and unit-tested in Node** (`web/js/core/*`, `workspaces/registry.js`):
  `agent-state.js` (one companion mode from real state), `tasks-model.js` (status, plan rows, timeline from persisted
  times), `geometry.js` (clamp/snap/anchor), `intent.js` (window-level commands), `fuzzy.js` (palette).
- **Global companion** (`ui/companion-dock.js`): created once, appended to `<body>`, never inside a workspace, so it
  survives navigation. Docked in a slot at the top-left of the top bar by default (no content is covered); dragged out
  it floats (pointer events + rAF + `transform`), snaps to edges, and is stored as an anchor to the nearest corner so a
  resize keeps it in place. Collapse/expand, pin, hide-until-next-event. It auto-opens only for a decision, an error,
  or an answer to something asked outside the chat, and folds away when that reason is gone or on a click elsewhere.
- **Workspaces** are registry entries (`workspaces/registry.js`) with tools whose status is computed from real state
  (core link, messenger connections). Each module exposes `{el, show(params), hide(), context()}`.
  Telegram / WhatsApp / mail share `CommunicationWorkspace` with per-service adapters (`adapters/channels.js`).
- **Adapters** keep the UI unaware of models and OS: `AgentStream` (send, cancel, pause, confirm, deny, task detail),
  `DesktopAdapter` (open URL/path, list/search/read files — implemented by the core's Linux tools today).
- **Core additions (small):** `ui.read` runs whitelisted read-only tools (`folder_list`, `search_files`, `read_file`)
  through the same gateway without chat messages (route `ui_read`, ignored by core state); `files.roots`;
  `task.detail` (task + its approvals for the timeline); router intents «перейди в задачи / домой / открой раздел
  файлы» → `ui.navigate` (voice navigation, no AI). «Открой Telegram» still opens the application.

## What is deliberately not faked
- Incoming Telegram/WhatsApp messages: the core does not read conversations; the workspace shows what JARVIS sent
  (outbox with real delivery status) and says so. Reading chats is a separate privacy decision.
- Website content inside the window: sites refuse framing and the window CSP stays `default-src 'self'`; links and
  searches open in the person's browser, website jobs go to Claude Code's isolated browser. Back/forward/reload of the
  person's browser are shown as not connected.
- Local wake word: still the planned openWakeWord milestone; the voice pipeline UI shows real phases
  (listening → transcribing on this computer → understanding → executing).

## Consequences
- `node --test tests/ui/unit/` (37 tests) and `node tests/ui/workos.mjs <port> <token> <out>` (21 end-to-end checks
  incl. layout at 1280×720 / 1440×900 / 1920×1080) join `uv run pytest`.
- Idle CPU of the window unchanged (0 frames while idle; companion motion only in active states).
- A Windows or Quickshell desktop integration replaces the core's desktop tools behind the same requests; the window
  code does not change.

Update 2026-10-24: the «Флора» workspace (synthetic demo project) was removed at the user's request.
