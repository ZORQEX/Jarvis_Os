# CLAUDE.md — JARVIS WorkOS

Product spec: `~/Downloads/JARVIS_WorkOS_Master_Prompt_v5.md`. Current plan: `docs/plan/ACTIVE_MILESTONE.md`.
Capability status with evidence: `docs/acceptance/TEST_MATRIX.md`. Architecture decision: `docs/architecture/ADR-001-stack.md`.

## Layout
- `jarvis/` Python core (aiohttp server, director, router, gateway, policy, tasks, tools, providers, comms, voice).
- `web/` no-build UI (ES modules, `css/app.css` tokens + `css/workos.css` shell). Strict CSP: no inline scripts.
  `js/app.js` shell + chat · `js/core/` pure logic (unit-tested in Node) · `js/ui/` companion dock, palette, toasts ·
  `js/workspaces/` registry + one module per workspace · `js/adapters/` AgentStream, DesktopAdapter, channels.
  Design decisions: `docs/architecture/ADR-006-workos-frontend.md`.
- `config/omarchy/` opt-in Waybar/Hyprland snippets; applied only by `scripts/omarchy-integrate.sh apply`.
- `tests/` pytest (core, server, comms, voice, AI loop with scripted provider); `tests/ui/` playwright-core driver.
  The «Флора» demo project was removed on 2026-10-24 at the user's request (git history has it).
- `fashion/` the «Заказы» app (factories, customers, deposits, documents): git subtree of morph-rq/alina-fashion-ai
  (FastAPI + SQLAlchemy, Jinja pages, CRLF in some files — keep them). Own venv ~/.local/share/jarvis/fashion/.venv,
  own tests (`cd fashion && <venv>/python -m pytest -q -o addopts=""`); JARVIS's pytest/ruff skip it.
  Keep changes small and marked so `git subtree pull --prefix=fashion <url> main --squash` still merges.

## Commands
- Tests: `uv run pytest -q` (tests pick a free port and an isolated JARVIS_HOME).
- Window: `node --test tests/ui/unit/`; end-to-end `node tests/ui/workos.mjs <port> <tokenFile> <outDir>` against a
  dev core (never the real one: it writes notes and drives the UI). `LIVE=1` adds one real Claude answer.
- Dev core: `scripts/dev-server.sh start|stop <JARVIS_HOME>`; the test config MUST set `write_dir`,
  `screenshot_dir`, `read_scopes` inside that home, or tools write into the real ~/Documents.
- Never `pkill -f "jarvis serve"` from a shell whose own command line contains that string (it kills the shell).
- Real app: `jarvis` (core on 127.0.0.1:8791, window app id `chrome-localhost__-Default`, title ends with JARVIS).
- Desktop companion: `jarvis overlay` (own Chromium profile, pinned Hyprland window titled «JARVIS · компаньон»;
  a dev core on another port uses «… :<port>», so it never moves the real one). Moves/resizes go through
  `jarvis/overlay.py` (Hyprland socket). Wake listening drops unaddressed speech before any turn or log.
- Skills: `jarvis/skills.py`, folder `~/.local/share/jarvis-skills` (config `skills_dir`); limits are 0 = none.
  A manifest's `say` phrases run the skill straight from the director (no AI); `open_result` opens the output file.
- Voice: STT worker = GigaAM, then Parakeet only for phrases that are not Russian or came back empty (`voice/lexicon.py`;
  waiting for «Джарвис» only if a wake-like word was heard). TTS = Supertonic 3 (`supertonic_worker.py`, one voice for
  every language, default M4); Silero (ru/de/uk) + Kokoro (en/es/fr/it/pt) are the fallback and start only when used.
  Numbers/symbols → words: `voice/textnorm.py` (loaded by path in the workers). Each runs in its own venv under
  `~/.local/share/jarvis/` (install.sh). ONNX sessions: few threads, no spinning (4–6× less CPU on this laptop).
  End of a phrase: Smart Turn v3.2 (`voice/turn.py`, numpy features = Whisper's, trailing silence trimmed to 0.2 s);
  p < 0.30 → no early answer, p < 0.15 → held like a cut-off phrase (`server.unfinished`). Browser VAD is `web/js/core/vad.js` (unit-tested; clicks ≠ voice);
  barge-in = `voice.barge` (stops speech only, never the work). `tests/ui/voice-mic.mjs` feeds a WAV as the mic.
- Self-improvement (`jarvis/improve.py`): `collect_issues` ranks problems from JARVIS's own records (repeats, failures,
  slow answers…); `self_improve` = Claude Code on a new branch `improve/…` in a worktree under ~/Projects/.jarvis-improve,
  then JARVIS runs the suite itself; `self_improve_apply` (second approval) merges into main and restarts. Settings →
  «Самоулучшение». Tests use a temporary repo (monkeypatch `REPO`/`WORK`) — never the real one.
- «Заказы» (`jarvis/fashion.py`): the core starts the module (127.0.0.1:`fashion_port` 8797, data <data>/fashion),
  the window frames its pages (CSP frame-src that origin only, `workspaces/orders.js`: its sections are tabs in JARVIS's
  header). Inside the frame (Sec-Fetch-Dest: iframe) the module drops its own chrome and takes JARVIS's colours
  (`fashion/app/jarvis_embed.py`, `static/jarvis.css`). ONE assistant: `/api/jarvis/claims` decides if a phrase is about
  orders → `Director._run_fashion` → `/api/agent/message` (jarvis=true, turn=task id): the module's rules answer, or it
  hands the turn back (`handoff`) → `Agency.run_orders` = JARVIS's conversation with the `orders` tool (Gateway →
  `/api/jarvis/ai/tool`, `fashion/app/jarvis_ai.py`) → `/api/jarvis/ai/finish` stores the proposal → card with
  «Подтвердить» in JARVIS's chat (`render.orderCard`, ws `fashion.decide`, UI click only). Never add a second
  assistant (the old `fashion_ask` was removed). Dev core: set another `fashion_port`.
  Factory PDFs: `fashion/app/pdf_order.py` (pdfplumber table + photos → factory-style workbook → analyze_workbook).
  Russian: `fashion/app/i18n_ru.py` middleware (FASHION_LANG=ru) — add phrases there, never edit upstream templates.
- Fast web search (`jarvis/research.py`, from jarvis-os research): local SearXNG child (~/.local/share/jarvis/searxng,
  pinned, `searxng_port` 8796; a healthy one on the port is reused, ours is adopted by pidfile and stopped with us).
  Agency research steps are the `web_search` tool (no AI start), then one Claude answer from the numbered sources; the
  conversation can call it too; its results stay in the conversation ctx (`last_research`) for Claude Code /
  follow-ups (`agency/tools.research_context`). Prices: `tools/market.py` (router, no AI). Tests: fake SearXNG
  (`tests/test_research.py`), `research_enabled=False` in conftest — never the real one from tests.
- The computer (from jarvis-os): `tools/machine.py` (processes, disk, diagnostics, brightness, appearance, audio
  devices, network, power), `tools/desk.py` (clipboard, media, OCR), `tools/daily.py` (weather, briefing) with fast
  routes in `router._caps_route`; one risk per tool (look = LOW, end/delete = MODERATE, power = HIGH). Never run the
  changing ones against the real desktop from tests; `screen_locked()` (hyprlock) blocks capture and typing.
- Dictation (`jarvis/dictation.py`): `jarvis dictate` / Super+Alt+D / «Джарвис, диктовка»; while on, `_wake_text`
  accepts every phrase and `_after_stt` types it (wtype) into the window recorded at start; «Джарвис, …» stays a command.
- Phone (`jarvis/phone.py`): the owner's private Telegram bot (Bot API long poll, token in the keyring, pairing code
  only from the window). Only tasks it started answer back; high risk and CLICK_ONLY approvals stay on the computer.
  Tests use a fake Bot API (`tests/test_phone.py`) — never a real bot token.
- Calendar (`jarvis/agenda.py`, `ics.py`): own events with reminders, read-only iCal subscriptions, in the briefing.
- Proactive (`jarvis/proactive.py`): notices once a minute (battery, disk, memory, failed units, internet) + watches
  (table watches, tool `watch`); readings are attributes so tests replace them — never wait for the real loop.
- Smart home (`tools/home.py`): Home Assistant REST; home_status/home_control LOW, home_secure HIGH and CLICK_ONLY.
  Router phrases only when connected (`ctx["home"]`). Tests use a fake HA (`tests/test_home.py`).
- Meetings (`jarvis/meeting.py`): pw-record mic + sink monitor → GigaAM chunks cut at pauses → Claude summary only in
  AUTHORIZED_CLOUD; no numpy in the core venv (pure-Python mix). Tests use a fake recorder script.
- Memory (`jarvis/memory.py`, table `memory`): «запомни, что …» / «что ты обо мне помнишь» / «забудь, что …»;
  `memory_line` goes into the state line of every AI request; the AI may save lasting facts itself (memory_save).
- Living speech: `router.simplify` strips fillers / «можешь» / infinitives so «Мне нужно открыть папку с колледжем»
  runs locally; only tool/search/reply/answer/ui routes are taken from the simplified phrase (never comms or control).
- Several tasks in one request: `router.split_tasks` (sequence words only: «первое / далее / потом / во-вторых», «1) 2)»;
  never inside quotes or a message body) → `Director._submit_batch` runs them one after another. «Стоп» / Esc / barge-in
  stop only the answer being heard (`SpeechManager.playlog` knows what plays when); other tasks keep running.
- Speech: one task speaks at a time (`take_floor`), never while the person talks (`voice.active` → `hold_until`).
  Echo: `_strip_echo` drops what JARVIS's voice said during the phrase (both windows; they are separate browsers, so
  neither's echo cancellation hears the other). A cut-off phrase is done as is after 4 s of quiet (`FRAGMENT_WAIT_S`).

## Rules
- Every action goes through `Gateway.execute` (validation → policy → approval → run → verify → audit).
  No other code path may run a tool. The model gets only `model_callable` tools.
- New tools: define `ToolSpec` (risk, side_effects, timeout, verification, impact), implement `verify()`
  that checks the real postcondition. Unverifiable → return `unverified`, never `verified`.
- Approvals are bound to a digest of exact args; external content is edited in its own editor and needs a
  fresh approval. Approvals are UI-only (no bearer API for approving).
- Do not invent numbers in UI/text: compute from sources and keep source references.
- Russian UI first; money uses narrow no-break space (`140 400 ₽`).
- Idle UI must not run infinite animations (measured: 25% → 1.9% CPU of one core after removing them).
- Label status honestly: VERIFIED LIVE / LOCAL / MOCK, IMPLEMENTED UNVERIFIED, NOT IMPLEMENTED.
- Act, don't explain: never tell the user to open / click / go somewhere JARVIS can open itself (apps → app_open,
  sites → open_url, window sections → window_show, connect a service → window_show connect=true). When only the person
  can do the next step (QR, password, code), JARVIS does everything up to it and asks for that one step.
  Rules live at the top of `providers/claude_chat.CHAT_SYSTEM`; each turn carries a `[Состояние JARVIS: …]` line.
- Window: the companion is one global instance (never inside a workspace); its mode comes only from
  `core/agent-state.js`. A workspace tool is «ready» only when the core can do it now (`registry.toolStatus`).
