# Test matrix — milestone 1 (2026-10-20) and later updates

Environment: Omarchy 3.8.3, Hyprland 0.56.2, Chromium 154, Python 3.14.8. Tests: `uv run pytest -q` → 266 passed, 6 skipped (live, opt-in); `JARVIS_LIVE=1 uv run pytest tests/test_live.py` → 6 passed (2026-10-22).

| Requirement | Status | Evidence |
|---|---|---|
| Real window on Omarchy, Russian UI, companion states from runtime state | VERIFIED LOCAL | grim captures of the real window (session report); `tests/ui/drive.mjs smoke` 0 console errors |
| Russian command → local tool → verified result, no AI call | VERIFIED LOCAL | typed into real window: «Джарвис, состояние системы» 38 ms; bench p50 2–17 ms |
| App launch verified by new Hyprland window | VERIFIED LOCAL | «открой калькулятор» → `org.gnome.Calculator` window detected, 274 ms |
| Volume set + read-back | VERIFIED LOCAL | wpctl read-back 60% |
| File search scoped; path traversal / symlink escape refused; executables not opened | VERIFIED LOCAL | test_core.py |
| Moderate/high risk require approval; digest-bound; edit-before-approve executes edited args only | VERIFIED LOCAL | test_core.py, test_comms.py |
| Approval expiry; approval persisted across restart and resumes once | VERIFIED LOCAL | test_approval_expires, test_persisted_approval_survives_restart |
| Pause holds next step; cancel kills running step; emergency stop blocks side effects until cleared | VERIFIED LOCAL | test_pause_holds_next_step, test_cancel_running_task, test_estop_* |
| Restart marks interrupted tasks blocked (no blind replay) | VERIFIED LOCAL | test_recover_after_restart_blocks_running |
| Reminders persisted; missed reminders reported late | VERIFIED LOCAL | test_missed_reminder_reported_late |
| Server: token, cookie, Origin/Host checks, CSP, CSRF header | VERIFIED LOCAL | test_server.py |
| Secrets redacted from audit/events | VERIFIED LOCAL | test_redaction |
| Gemini streaming + tool calls through gateway; errors reported honestly; PRIVATE_LOCAL asks first | VERIFIED MOCK | test_ai_loop.py (scripted provider, real SDK types) |
| Gemini live call / model availability | BLOCKED | no API key configured |
| Voice push-to-talk → Gemini transcription; Gemini TTS | IMPLEMENTED UNVERIFIED | needs key; LISTENING shown only while a real mic stream is open |
| Wake word, PipeWire-native capture | NOT IMPLEMENTED | next milestones |
| Gmail / Calendar / Drive | NOT IMPLEMENTED | — |
| Waybar indicator | VERIFIED LOCAL (temporary bar instance, copy of user config) | not installed into the user's bar |
| Hyprland bindings Super+H / Super+Shift+H | IMPLEMENTED UNVERIFIED | `config/omarchy/hypr-jarvis.conf`, applied only on request |
| systemd user service | IMPLEMENTED UNVERIFIED | `scripts/install.sh --service` |

## WorkOS window (2026-10-23)
Tests: `uv run pytest -q` → 289 passed, 6 skipped; `node --test tests/ui/unit/` → 37 passed;
`node tests/ui/workos.mjs` against an isolated core → 21/21 checks, 0 console errors (with `LIVE=1`, one real Claude answer).

| Requirement | Status | Evidence |
|---|---|---|
| Shell: sidebar with 9 workspaces (chat first; no Home, task center or activity panel since 2026-10-25), top bar with real status, workspace persisted | VERIFIED LOCAL | workos.mjs «app starts on the chat», «sidebar», «persistence» |
| Global companion: one instance, survives navigation, docked top-left by default | VERIFIED LOCAL | workos.mjs (element identity kept across 5 workspaces) |
| Companion drag: clamped to window, snaps to edges, position restored after reload and resize, re-docks near its slot | VERIFIED LOCAL | workos.mjs «drag, clamp, persist» |
| Companion expand / pin / hide until next event / Ctrl+J restore; folds on outside click | VERIFIED LOCAL | workos.mjs |
| Companion state machine (IDLE, LISTENING, THINKING, PLANNING, EXECUTING, WAITING_CONFIRMATION, SPEAKING, SUCCESS, ERROR, CANCELLED, PAUSED, OFFLINE, STANDBY) from real state only | VERIFIED LOCAL | unit tests (agent-state); LIVE run: THINKING → EXECUTING → SUCCESS |
| Chat: local command result card; stop on a running turn; retry on failed/cancelled | VERIFIED LOCAL | workos.mjs «chat», screenshots |
| Streaming: assistant.delta rendered incrementally; companion shows «Claude отвечает…» | VERIFIED LIVE / LOCAL | LIVE run (2,5 s answer); renderer check with core events |
| Confirmation: companion card «Разрешить / Изменить / Отклонить», card in the chat, top bar; approve runs the real tool | VERIFIED LOCAL | workos.mjs «confirmation» (note written, verified) |
| Cancel: «Подробнее» jumps to the task's turn in the chat, «Остановить» there and Esc stop a task | VERIFIED LOCAL | workos.mjs «cancel», «Esc stops» |
| «Ошибки»: one list of problems (failed tasks by error, repeats, slow voice answers…) with «Исправить»; a new failure marks the sidebar item | VERIFIED LOCAL | workos.mjs «errors»; screenshot with a copy of the real records (dev core) |
| Command palette (Ctrl+K): workspaces, global commands, recent tasks, «Спросить JARVIS» fallback | VERIFIED LOCAL | workos.mjs «palette» |
| Window-level commands handled instantly (navigation); voice navigation through the core router (no AI) | VERIFIED LOCAL | workos.mjs «navigation», test_workspaces.py |
| Files: roots, browse, search, text/PDF preview through the gateway without chat noise; «Спросить JARVIS» attaches the path | VERIFIED LOCAL | workos.mjs «files», test_workspaces.py |
| Telegram / WhatsApp / mail workspaces: honest connection state, connect cards, outbox per person, compose → JARVIS asks before sending | VERIFIED LOCAL (not connected in the test home) | workos.mjs «telegram»; sending path is the existing comms agent |
| Reading incoming Telegram/WhatsApp messages | NOT IMPLEMENTED | needs a privacy decision; shown as «не подключено» |
| Browser workspace: open link / search in the person's browser; website job → Claude Code isolated browser | IMPLEMENTED UNVERIFIED in this run | open_url not clicked in tests (would open the real browser) |
| Browser back / forward / reload of the person's browser | NOT IMPLEMENTED | shown disabled with the reason |
| Layout at 1280×720, 1440×900, 1920×1080: no overflow, no clipped status/chips, companion in view | VERIFIED LOCAL | workos.mjs «layout», screenshots |
| Light theme | VERIFIED LOCAL | screenshots 14-light-home, 15-light-tasks |
| Idle cost | VERIFIED LOCAL | idlecpu.mjs: task 0.14 % (previous UI 0.22 %), 0 frames idle |
| Long chat (600 messages) | VERIFIED LOCAL | full render 4–6 ms script, next message 64–74 ms incl. layout |

## Act, don't explain (2026-10-23)
Tests: `uv run pytest -q` → 309 passed, 6 skipped. Live: 5 questions to the real Claude on an isolated core whose
desktop launchers were replaced by loggers (nothing opened on the screen).

| Requirement | Status | Evidence |
|---|---|---|
| «Подключи Telegram / WhatsApp / почту» → the connect card is shown, WhatsApp QR started; no AI | VERIFIED LOCAL | test_workspaces.py (connect phrases, QR start, window opened if closed) |
| Sending to a service that is not connected → JARVIS opens its connect card (not «зайдите в раздел…») | VERIFIED MOCK | test_not_connected_is_reported_and_nothing_is_faked |
| «Открой WhatsApp Web / Telegram Web / настройки звука / вайфай / блютуз» → opened locally, no AI | VERIFIED LOCAL | router tests; Omarchy launchers audio/wifi/bluetooth |
| «Что нужно, чтоб ты увидел страницу WhatsApp?» → window_show whatsapp + QR started, one step asked | VERIFIED LIVE | live run 4.6 s; before: a how-to answer with no action |
| «Как подключить Telegram?» → Telegram section + my.telegram.org/apps opened | VERIFIED LIVE | live run (open_url logged) |
| «Как подключить почту?» → Mail section + Google app-passwords page opened | VERIFIED LIVE | live run (open_url logged) |
| «Где посмотреть мои задачи?» → Task Center shown | REPLACED 2026-10-25 | no task center; «покажи задачи» = what is running (router reply) |
| Weather → answered from web search with Russian «Источники:» | VERIFIED LIVE | live run 13 s |

## Desktop companion, wake word, skills, no limits (2026-10-23)
Tests: `uv run pytest -q` → 336 passed, 6 skipped; `node tests/ui/workos.mjs` → 21/21.

| Requirement | Status | Evidence |
|---|---|---|
| Companion above every workspace and app (pinned Hyprland window, round, top-right under Waybar) | VERIFIED LIVE | `jarvis overlay` on the real desktop: at 1204,38, 64×64, pinned, focus returned to the previous window |
| Drag anywhere on the screen (core follows the cursor via Hyprland IPC), place remembered | VERIFIED LIVE | cursor 600,300 → window 568,268 … 700,400 → 668,368; saved and restored |
| Card opens away from the nearest edges and stays on screen; collapses back to the same spot | VERIFIED LIVE | top-right orb → card 360×470 at 908,38, rounding 18; back to 1204,38 |
| «Джарвис, …» wakes it: speech recognised on this computer, other speech dropped and never stored | VERIFIED LIVE | phrase played through the speakers: «Джарвес, который час.» → answer spoken by the companion, card opened; unaddressed phrase ignored (dev core, real STT) |
| Wake word tolerant to the speaker→mic spelling («Джарвес», «Жарвис»), not to «Дарвин» | VERIFIED LOCAL | test_wake_word_survives_speaker_to_mic_spelling |
| Answers to the companion are spoken only by it (no double voice with the main window); it yields the mic to the window's conversation mode | VERIFIED LOCAL | test_speech_of_the_companion_is_marked; voice.presence |
| Starts at login, Super+Shift+J brings it back, Super+H opens JARVIS (Omarchy integration applied, backups kept) | IMPLEMENTED (login autostart takes effect at next login) | ~/.config/hypr/hyprland.conf sources config/omarchy/hypr-jarvis.conf |
| WhatsApp «напиши маме…»: a «what's new» popover covered the search box | VERIFIED LIVE (search, nothing sent) | real profile: «Мама ❤️» found; tests/test_whatsapp_dom.py |
| «Научись делать QR-код…» → skill built by Claude Code (venv, wrapper, manifest, scanned), used at once; next request reuses it | VERIFIED LIVE | dev core: S1 71.6 s (skill_list → skill_create → skill_run → open_path), S2 3.9 s |
| A word like «код» / «терминал» without a project folder is not a code job: goes to the conversation with all tools | VERIFIED LOCAL | test_requests_without_a_project_go_to_the_conversation_with_tools (before: read-only look into ~/Documents) |
| Companion cost with the mic always on | VERIFIED LIVE | 8.3% of a core / 371 MB → 4.2% / 262 MB (no idle redraws, 100 ms frame batches, 16 kHz, browser audio processing off while waiting; mic ignored while it speaks) |
| WhatsApp send after the popover fix | VERIFIED LIVE | «Отправь себе в WhatsApp: …» → sent, verified in the chat |
| No limits on thinking time / budget / steps (stop any time) | VERIFIED LOCAL | test_no_time_budget_or_turn_limits_by_default |
| Skills: JARVIS builds, keeps and runs its own tools (GitHub/pip/npm/cargo), confirmed once, verified by manifest | VERIFIED MOCK | tests/test_skills.py (Claude Code scripted) |

## Languages, barge-in, deaf companion fix, skill phrases (2026-10-24)
Tests: `uv run pytest -q` → 352 passed, 6 skipped; `node --test tests/ui/unit/` → 43/43; `node tests/ui/workos.mjs` → 21/21.

| Requirement | Status | Evidence |
|---|---|---|
| English and other languages are spoken by a voice of that language (Russian voice no longer spells English: «Done. I've sent…» was «днме. и уе ремс…») | VERIFIED LOCAL (real voices) | Kokoro-82M int8 for en/es/fr/it/pt, Silero de/uk; TTS → STT round trip: en «Done, I've sent the message to your mom on WhatsApp.», de, uk, es, mixed ru+en all heard back word for word; English sentence ~1.1 s on this CPU |
| Each sentence gets its language's voice; Russian with a brand («Открыл YouTube Music») stays Russian | VERIFIED LOCAL | tests/test_languages.py (runs/detect) |
| English (and 24 more European languages) speech is understood; Russian stays as fast as before | VERIFIED LOCAL (real models) | GigaAM first; a phrase that does not read as Russian (word list) is re-heard by Parakeet v3: 10/10 English test phrases right (1.1 s), Russian 0.25–0.7 s unchanged; on 155 real past voice phrases only 6 re-heard (2 of them were English) |
| Hold phrase / «не расслышал» / «Да?» in the language the person speaks | VERIFIED LOCAL | test_english_question_gets_english_hold_phrase_and_voice, test_bare_wake_word_is_answered_in_its_language |
| Companion «does not understand, have to repeat»: a phrase recorded for 8.6 s / 30 s | FIXED, VERIFIED (Chromium fake microphone, real STT/TTS, dev core) | cause: keyboard clicks counted as voice, so the pause never came (room 0.002 RMS, typing peaks 0.37). New VAD (core/vad.js): voice = lasting and low-frequency; noise floor = quietest 3 s. Typing + «Джарвис, который час?» + typing: recording 4.3→6.3 s (phrase 4.1→5.6), answer audio 1.26 s after speech end; tests/ui/unit/vad.test.mjs |
| Talking over JARVIS: it turns its voice down at once, stops after 0.4 s of speech and does the new request (no «Джарвис» needed); the work in progress is not cancelled | VERIFIED (fake microphone, dev core) | answer playing → «Покажи состояние системы» → recording, new task, answer 0.94 s after speech; voice.barge stops speech only (test_barge_in_stops_the_voice_not_the_work) |
| JARVIS's own voice heard through the speakers is not taken as a command | VERIFIED LOCAL | echo check against what it said in the last 30 s (test_cutting_in_needs_no_wake_word_but_echo_is_ignored); echo cancellation back on |
| «…другой разговор… Джарвис, который час» in one recording → the command after the name | VERIFIED LOCAL | test_wake_word_in_the_middle_of_a_recording |
| Early answer in wake mode (0.6 s pause), only for phrases to JARVIS | VERIFIED LOCAL | test_early_answer_only_for_phrases_to_jarvis |
| Say less: spoken AI answers 1–2 sentences, ≤450 characters, hold phrase only after 3.5 s | IMPLEMENTED | CHAT_SYSTEM, director, agency |
| Skill phrases: a learned skill runs at once on one of its phrases — no AI call, no tokens; result file opened | VERIFIED LOCAL | test_skill_phrase_runs_it_without_ai, test_skill_result_file_is_opened; installed qr_code skill got «сделай qr код для …» / «make a qr code for …» |
| Memory of the voice processes | MEASURED | recognition 1.9 GB (GigaAM + Parakeet; «Только русский» in Settings → 0.5 GB), Silero 0.7 GB, Kokoro 0.3 GB |

## Performance (this laptop)
- Core cold start to /health: 223 ms. Launcher returns in ~280 ms. Page ready ~100 ms.
- Core idle: 0.0% CPU, ~39 MB PSS. Window (Chromium, 10 processes): ~380 MB PSS, idle 1.9% of one core
  (was 25% before removing infinite idle animations).
- Local commands (p50/p95 ms, 40 runs): system 16.6/26.8, windows 8.3/10.0, file search 4.3/5.5,
  time 1.9/2.1.

## AI agency (2026-10-21)

| Requirement | Status | Evidence |
|---|---|---|
| Claude Code adapter (restricted CLI, inspect/change modes) | VERIFIED LIVE | test_live.py; UI run: inspect $0.05, change $0.04 in a scratch git repo |
| Code change needs high-risk approval; JARVIS lists changed files (git) and reruns pytest itself | VERIFIED LIVE | scratch repo: `a - b` → `a + b`, JARVIS pytest: 2 passed |
| Cancellation kills the Claude Code process | VERIFIED LOCAL | test_cancel_kills_claude_code_process |
| Gemini: writing, vision, model fallback chain, director research → letter | VERIFIED LIVE | test_live.py (6/6) |
| Gemini web search with sources | BLOCKED by key quota | Google Search grounding returns 429 for this key → honest no-search fallback, marked unverified |
| Gemini → Claude Code chain (research → save file, with approval) | VERIFIED LIVE | UI run `drive.mjs two`: file written, 0 console errors |
| Two-specialist routing (former GPT work → Gemini; computer work → Claude Code) | VERIFIED MOCK | test_agency.py |
| Multi-AI chain, context isolation (no unrelated history), provenance + usage ledger | VERIFIED MOCK | test_multi_ai_context_isolation_and_provenance |
| No GPT, no silent substitution when Gemini is unavailable | VERIFIED MOCK | test_gemini_unavailable_is_reported_never_substituted |
| Privacy: HIGHLY_SENSITIVE never to cloud; per-provider sensitivity limit; STRICT_OFFLINE | VERIFIED MOCK | test_privacy_*, test_provider_sensitivity_setting…, test_strict_offline_blocks_all_cloud |
| Invented numbers flagged; source outranks AI in disagreements | VERIFIED LOCAL | test_invented_numbers_are_flagged, test_source_outranks_ai_opinion |
| LLM planner cannot add a send or lower risk | VERIFIED MOCK | test_llm_planner_cannot_add_send_or_lower_risk |
| Gmail read/send | NOT IMPLEMENTED | routed to `mail_read`/`email_send` → reported as not connected, no AI call |
| AI-agency settings page, plan card, developer routing inspector | VERIFIED LOCAL | headless UI run `drive.mjs agency`, 0 console errors |

## Voice conversation (2026-10-21)

| Requirement | Status | Evidence |
|---|---|---|
| Fast path without AI (arithmetic, %, conversions, time/date) | VERIFIED LOCAL | test_voice.py; «25% от 800» first audio 1.8 s |
| Streamed speech while Gemini writes (sentence buffer → Piper → ordered chunks) | VERIFIED LIVE | scripts/voice_latency.py; first audio 1.9–2.9 s |
| Hands-free conversation in the window (local VAD, auto end of phrase, keeps listening) | VERIFIED LOCAL (Chromium, fake microphone file) | drive.mjs voiceui: listening→recording→transcribing→speaking→listening |
| Barge-in while JARVIS speaks | VERIFIED LOCAL (Chromium, fake microphone) | state «interrupted» then recording |
| «стоп / хватит / подожди» stop speech and cancel answers, not actions or approvals | VERIFIED MOCK | test_interrupt_stops_speech_and_cancels_answer_but_not_actions |
| Immediate local acknowledgement for long tasks | VERIFIED LIVE | voice_latency.py: audio at STT end (1.3–1.7 s) |
| Follow-up context («А сколько ему лет?») | VERIFIED LIVE | answered about Elon Musk |
| Voice → Claude Code routing («открой проект и проверь ошибку») | VERIFIED MOCK (planner) | live STT of the synthetic phrase was garbled |
| Real microphone on this desktop | NOT VERIFIED | needs the user's voice and a one-time microphone permission |

## Voice output fix (2026-10-21)

| Requirement | Status | Evidence |
|---|---|---|
| Answers are audible in the real window | VERIFIED LOCAL | cause found: Chromium's PipeWire stream volume was 4 % (−84 dB); JARVIS window now has its own `application.id=jarvis-workos` stream (100 %); speaker monitor peak 32 701 during a spoken answer |
| Honest states (synthesizing / speaking only when sound starts / error) | VERIFIED LOCAL | voice.js, app.js; test_speech_failure_is_reported_and_text_stays |
| TTS failure keeps the text and says so | VERIFIED MOCK | test_tts_falls_back_and_reports_failure |
| Sound check + "make JARVIS louder" (only JARVIS's own stream) | IMPLEMENTED | Settings → Голос |

## Communications (2026-10-21)

| Requirement | Status | Evidence |
|---|---|---|
| One provider contract, capabilities, honest CONNECTED / NOT CONNECTED / ERROR | VERIFIED LOCAL | Messages view, drive.mjs comms |
| Voice → Gemini intent → confirmation spoken → «да» → provider → result spoken | VERIFIED (real STT, Gemini, Piper; provider = test double) | scratch voice_e2e.py |
| Telegram real send / schedule | VERIFIED LIVE (2026-10-21, to Saved Messages) | immediate №167986; scheduled «через 1 минуту» sent 17:01:11 exactly, re-read by id; schedule + cancel by command |
| WhatsApp real send | VERIFIED LIVE (2026-10-21, to own number) | delivered ✓✓; status now read from our bubble's ticks (old `.message-out` markup gone — first two sends were delivered but reported «unknown», fixed) |
| E-mail real send | VERIFIED LIVE (2026-10-21, Gmail to self) | found in «Отправленные» by Message-ID |
| Persistent schedule, atomic claim (no double send), crash → unknown, missed, bounded retry | VERIFIED MOCK | test_comms.py |
| Cancel / edit (new confirmation) / status by voice | VERIFIED MOCK | test_comms.py |
| Ambiguous recipient or platform asked before anything is sent | VERIFIED MOCK | test_comms.py |
| Multi-channel / multi-recipient with one confirmation | VERIFIED MOCK | test_comms.py |
| Trusted rules (explicit, revocable, click-only) | VERIFIED MOCK | test_trust_rule_skips_confirmation_until_revoked |
| Emergency stop blocks scheduled delivery; content tamper blocks delivery | VERIFIED MOCK | test_comms.py |
| Attachments | NOT IMPLEMENTED | architecture ready (caps.can_send_file=false) |

## All AI through Claude Code — Gemini removed (2026-10-21)

| Requirement | Status | Evidence |
|---|---|---|
| Conversation keeps context, streams, warm process | VERIFIED LIVE | test_live: context («А сколько ему лет?»); dev JARVIS: 2.5 s short answers |
| Claude calls JARVIS tools through MCP, approvals unchanged | VERIFIED LIVE | dev JARVIS: search_files via MCP (4.8 s); create_note → approval card → file created |
| «Стоп» interrupts without restarting | VERIFIED LIVE | stopped at once; next answer 1.3 s |
| Web research with sources, letters, screenshot/image reading | VERIFIED LIVE | test_live.py |
| Message commands: local first, Claude JSON otherwise | VERIFIED LIVE | 4.3 s with claude-haiku-5-5 |
| No Gemini code or dependency left | VERIFIED LOCAL | test_no_gemini_code_left_in_the_application |
| Tests never call the real Claude | VERIFIED LOCAL | conftest: providers «not installed» unless scripted |

## Full audit (2026-10-22)

| Requirement | Status | Evidence |
|---|---|---|
| Static analysis clean (pyflakes, bugbear loop/raise rules, invisible characters, fire-and-forget tasks) | VERIFIED LOCAL | `uvx ruff@0.15.6 check jarvis tests scripts --select F,RUF006,B023,PLE,B904` → 0 |
| Background tasks are kept referenced and their errors are printed, never lost | VERIFIED LOCAL | `events.spawn` used for every background task |
| Claude process killed → next question still answered (one automatic restart) | VERIFIED LIVE (dev JARVIS) | SIGKILL of the conversation process, next answer «Да.» in 3.2 s; before: failed in 13 ms |
| Output of an old Claude process never fails the new question | VERIFIED LOCAL | test_an_old_process_ending_never_fails_the_new_question |
| Cost of the long-lived conversation recorded per answer (Claude reports a running total) | VERIFIED LIVE + LOCAL | 10 answers: ledger now 0.023 $ instead of 0.13 $; test_session_cost_is_recorded_per_answer_not_cumulative |
| Token ledger counts cached context too | VERIFIED LIVE | conversation context 6.2k → 7.4k tokens over 10 turns (MAX_TURNS 40 kept) |
| Single letter / translation / web question goes to the warm conversation (no new process) | VERIFIED LIVE + MOCK | «столица Японии» ~1–1.4 s; «сделай короче» edits the letter; numbers and sources still checked |
| Several conversational clauses = one Claude message, no planning call | VERIFIED LIVE + MOCK | «Запомни число… Ответь одним словом» 1.3 s (was +2.7–3.5 s) |
| Silero / speech recognition / Piper restart once when their process dies | VERIFIED LOCAL | test_silero_restarts_once…, test_local_stt_restarts_once… (real subprocesses) |
| No temporary-file leaks | VERIFIED LOCAL | 7 124 empty dirs found in /tmp and removed; Silero returns audio through the pipe; Piper dir is created on use and pid-tagged; tests use one auto-removed dir |
| Speech recognition int8 model | VERIFIED LOCAL | 20 noisy synthetic commands: identical transcripts to the full model, RAM 1417 → 523 MB; multi-length warm-up: first phrase 2.1 s → 0.5 s |
| Phrase cache bounded | VERIFIED LOCAL | test_silero_phrase_cache_is_bounded (300 phrases max) |
| Core stops quickly with the window open; window reconnects | VERIFIED LOCAL | SIGTERM → stopped in 155–309 ms (was 15–35 s); all workers gone |
| «Отправь себе в WhatsApp» never searches contacts | VERIFIED LIVE | before: matched another person's chat containing «Себя» (approval card, denied, nothing sent); now own number read from WhatsApp Web, sent to «Вы (заметки)» in 6 s, ✓ |
| Real sends after the audit | VERIFIED LIVE (to self) | Telegram №168012; e-mail found in «Отправленные»; WhatsApp «прочитано»; schedule + cancel by command |
| Reminder fires on time; STRICT_OFFLINE blocks Claude, local commands work | VERIFIED LIVE (dev JARVIS) | «через 1 минуту» fired at the minute; offline: windows list answered locally |
| UI scenarios | VERIFIED LOCAL | drive.mjs smoke, approval-deny, view, comms, commsqr, two, voiceui, agency → 0 console errors |

## Action history, voice confirmations, speed (2026-10-22)

| Requirement | Status | Evidence |
|---|---|---|
| Request history removed from the window («Задачи» view, «Недавно» list) | VERIFIED LOCAL | drive.mjs smoke: `#recentTasks` absent, 0 console errors |
| «История действий»: only lasting changes (Claude Code changes — sites, code, files, installs; notes) with folder, files, result, time, cost; messages, volume, windows, questions are not kept | VERIFIED LOCAL + LIVE (dev JARVIS) | test_history.py (7); smoke: only the note listed; drive.mjs two: «…сохрани результаты в отдельный файл» listed with the new file |
| Entry removal keeps files; existing tasks back-filled once | VERIFIED LOCAL | test_deleting_an_entry_keeps_the_files; backfill on start |
| Any confirmation by voice («да», «разрешаю», «выполняй», «можно»… / «нет», «не разрешаю», «отклонить»…); trusted-send rules stay click-only | VERIFIED LOCAL | test_access.py; real STT of 10 synthetic answers → 10/10 understood |
| Two waiting actions: a bare «да» picks none and says so | VERIFIED LOCAL | test_two_waiting_actions_are_never_picked_at_random |
| Bearer API cannot pose as the window to answer confirmations | VERIFIED LOCAL | test_bearer_api_cannot_answer_confirmations (was possible via `source`) |
| Single-phrase mic stays open while JARVIS waits for an answer (≤45 s) | IMPLEMENTED (browser logic) | voice.js `holdOpen` |
| Spoken answers on the fast model in the same conversation (Settings → Голос → «Быстро / Подробнее») | VERIFIED LIVE | context kept across switch; Haiku 30 answers 0.9–1.9 s, no stalls; Sonnet stalls 4–10 s in 1–4 of 10 |
| Early end of phrase: recognised after 0.6 s of silence, finished phrases answered at once, cut-off ones wait | VERIFIED LOCAL + browser | transcript ready 0.78 s after speech end (was ~1.6 s); test_voice.py probe tests; drive.mjs voiceui 0 errors |
| Open window reloads itself when JARVIS restarts with a new interface | VERIFIED LOCAL | `ui_build` in snapshot |

## «Сделай сайт»: the conversation can always act (2026-10-22)

| Requirement | Status | Evidence |
|---|---|---|
| Root cause of «нет инструмента»: «мне нужен сайт…» was a conversation step; the conversation had no tool that creates files | FOUND | real task t_d3643ba76cdf485c |
| `computer_task`: the conversation hands any make/build/fix/install job to Claude Code (new folder in ~/Projects or an existing project), high-risk approval (voice «да» works), changed-files check, tests, history entry | VERIFIED LIVE (dev JARVIS) | «мне нужен сайт для кафешки…» → card in 6.9 s → «Да, разрешаю» → 252 s → `cafe-site/` index.html + style.css + script.js, opened in the browser, 0 JS errors; test_site_request_goes_to_claude_code_after_a_spoken_yes |
| Executor may look things up (WebSearch/WebFetch), installs user-level tools only when the task allows, never sudo/pacman | VERIFIED LOCAL | test_executor_can_look_things_up_but_never_escalates |
| New project folders never reuse a folder with files; tracked by a local git from the start | VERIFIED LOCAL | test_new_project_folder_never_reuses_a_folder_with_files |

## Extra abilities chosen by the user (2026-10-22)

| Requirement | Status | Evidence |
|---|---|---|
| Claude Code may work in any home folder; keys, passwords, sessions, browser profiles, Claude/JARVIS data stay closed (workdir refused; Read/Edit/Write denied) | VERIFIED LOCAL | test_home_wide_workdirs_but_secret_folders_stay_closed, test_claude_code_flags |
| System programs (`system_install`): official Arch packages only (checked with `pacman -Si`), already installed skipped, `pkexec` → the system's own password dialog; JARVIS never sees the password; verified with `pacman -Q` | VERIFIED LOCAL + LIVE up to the card | test_capabilities.py; dev JARVIS «Установи в систему программу cowsay» → card in 2.2 s → «Нет» → nothing installed (password dialog not shown to keep the user's session undisturbed) |
| Browser control for Claude Code tasks the user allows: Microsoft Playwright MCP 0.0.83 (pinned), headless, in-memory profile (no user logins/cookies) | VERIFIED LIVE | example.com read, summary + screenshot saved in 17 s ($0.17) |
| Site publishing (`site_publish`): GitHub Pages via the user's account; device-code login from Settings (no password/token seen); secret-looking files refuse publishing; push with a per-command credential helper; checks the public link | VERIFIED LOCAL + LIVE (login code shown) | test_capabilities.py; Settings → «Подключить GitHub» → code XXXX-XXXX shown, login cancelled cleanly; real publishing needs the user's GitHub login |
| «Установи программу X» goes to the conversation (picks packages → system_install), project dependencies still to Claude Code | VERIFIED LOCAL + LIVE | planner check; dev JARVIS run |

## Full-system QA (2026-10-22) — driven end to end (`tests/ui/qa.mjs`), real Claude, real accounts (self only)

| Area | Status | Evidence (measured) |
|---|---|---|
| Conversation, streaming, context | VERIFIED LIVE | first text 0.8–4.7 s (Sonnet, median ~2.4 s); «А кто придумал этот термин?» answered in context |
| Voice cycle mic → STT → Claude → text + speech | VERIFIED LIVE (synthetic voice through Chromium's fake microphone; the real microphone needs the user) | «25 × 4»: transcript 0.85 s after speech end, text + first sound 1.0 s; long answer streams text and speech |
| Spoken answer speed (steady) | VERIFIED LIVE | first text ~1.0 s, first sound 1.2–1.7 s after the request (+0.9 s recognition); first question after a model switch 2.5 s |
| Speech on the real device | VERIFIED LIVE | PipeWire stream `jarvis-workos` playing 0.3 s after the request, 100 %, not muted |
| «Стоп» while JARVIS speaks | FIXED → VERIFIED | short «Стоп» had ≤ 6 loud frames in a row (needed 10): now 8 of 15; interrupted at once |
| «Стоп» during a long job | FIXED → VERIFIED | task cancelled in 27 ms, Claude Code process killed, core back to IDLE |
| Files: folder on the desktop, file, write, open, list | FIXED → VERIFIED | 3.1–4.7 s each (was 9–17 s via Claude Code and no ~/Desktop); contents checked on disk |
| Read-only terminal | VERIFIED LOCAL + LIVE | shell_read allow-list; `rm -rf`, pipes, secrets refused |
| Website: create, add catalog, button, dark theme (same project) | VERIFIED LIVE | 151–175 s per job; catalog first failed (executor race, fixed), retest added 10 cards |
| «Создай сайт, запусти и открой» | FIXED → VERIFIED LIVE | first: cut at 600 s; then Claude session limit (external); then 151 s, served on 127.0.0.1:8000, opened |
| Browser: «найди информацию о мебели» | FIXED → VERIFIED LIVE | the final step now gets the research results (before: answered from an unrelated file) |
| Telegram / WhatsApp / e-mail (to self) | VERIFIED LIVE | Telegram 0.3 s, WhatsApp 5.3 s, e-mail 4.1 s found in «Отправленные»; contact search via contact_find |
| Message to another person | VERIFIED LIVE | asks which «Мама» / confirmation card; declined, nothing sent |
| Pending «Кому отправить?» hijacked by «Сколько будет 25 × 4?» | FIXED (P0) | option «2» matched the start of «25» and a send card appeared (declined, nothing sent) |
| Scheduled actions: create, run on time, survive restart, cancel, move | ADDED → VERIFIED LIVE | fired 02:37:51 to the second after a restart; cancelled one never ran; moved one ran at the new time (43 s later while the conversation was busy) |
| Permissions: delete, dangerous command, send | VERIFIED LIVE | Trash deletion asks (HIGH); `rm -rf ~/Documents` not run |
| Honest errors | VERIFIED LIVE | missing file → «Файл не найден»; session limit and failures reported, never «Готово» |

## Tasks in a row, «стоп» for one answer, windows that do not hear each other, Flora removed (2026-10-24)

Found in the real JARVIS's records (voice_turns, audit.jsonl) of the same day.

| Problem / requirement | Status | Evidence |
|---|---|---|
| «Джарвис, расскажи что-нибудь» got no answer (3 times): split at the hyphen it ended on «нибудь», was held as a cut-off phrase and dropped after 15 s | FIXED → VERIFIED LOCAL | test_tell_me_something_is_a_whole_phrase |
| A cut-off phrase with no continuation was silently dropped | FIXED: done as it is after 4 s of quiet (not while the person still talks) | test_cut_off_phrase_with_no_continuation_is_done_after_a_pause, test_the_rest_said_in_time_is_joined |
| «Первое… А далее… и далее…» did only the message part (the whole request went to the messaging route) | FIXED: split on sequence words into tasks run one after another; plan shown in the chat | test_a_request_with_several_tasks_is_split_in_order (the user's exact phrase), test_tasks_said_together_run_one_after_another |
| Never split a message text, quotes, «найди файлы сначала в загрузках, потом в документах» | VERIFIED LOCAL | test_a_single_task_is_not_split (7 cases) |
| Esc / stop while one answer played cancelled every running answer (audit 15:30:11: two tasks, incl. the 3-part one) | FIXED: only the answer being heard stops; other tasks run on | test_silencing_one_answer_keeps_other_tasks_running |
| «Стоп» during a series: the current task and the rest of the series | VERIFIED LOCAL | test_stop_during_a_series_does_not_start_the_rest |
| Task cancelled before its first step stayed «created» forever | FIXED → VERIFIED LOCAL | same test (cancel cleans up a task that never started) |
| Two tasks' answers spoken sentence by sentence mixed | FIXED: one stream speaks at a time | test_answers_of_two_tasks_are_not_mixed |
| JARVIS starts a sentence while the person talks | FIXED: windows report `voice.active`; speech waits | test_jarvis_waits_while_the_person_is_talking |
| Companion heard the main window's voice sample «Привет! Я Джарвис…» and took it as a command (15:33:37) | FIXED: the core knows what plays when and drops JARVIS's own sentences heard during the phrase | test_the_other_windows_voice_is_not_a_command, test_echo_sentences_are_cut_and_the_person_is_kept |
| Companion started after the main window opened its mic kept listening too | FIXED: `overlay.state.window_listening`; a closed window frees the mic | test_companion_knows_the_main_window_has_the_microphone |
| «Флора» (synthetic demo project) | REMOVED at the user's request: package, fixtures, workspace, tools, planner step, openpyxl | full suite green |

## One natural voice for every language; recognition that does not burn the CPU (2026-10-24)

Measured on this laptop (i5-1235U, 23 GB, no GPU); samples and scripts in the session scratchpad.

| Requirement | Status | Evidence |
|---|---|---|
| Candidates for one local voice in all languages incl. Russian | RESEARCHED | Supertonic 3 (99M, ONNX, 31 languages), Qwen3-TTS 0.6B (x86 CPU real-time unconfirmed), Chatterbox Multilingual (impractical on CPU), Kokoro (no Russian) |
| Naturalness (UTMOS, 8 phrases × 6 languages) | VERIFIED LOCAL | Supertonic M4 3.95, F1 3.99 vs Silero+Kokoro 2.98 (ru 3.94 vs 2.98, uk 4.01 vs 1.82, en 4.49 vs 4.12) |
| Intelligibility (spoken → recognised back) | VERIFIED LOCAL | M4: ru/en/de/es word for word; brand names in a Russian sentence read correctly without transliteration |
| Speed | VERIFIED LOCAL | 0.85 s for 2.45 s of speech, 2 threads; first sound of a spoken answer 3.45 s after the phrase (AI 2.6 s + voice 0.47 s) on the dev core |
| Supertonic is the voice; Silero/Kokoro fallback, reported; one-time switch to M4 | VERIFIED LOCAL | test_supertonic.py (7 incl. the real model); real core: `tts_voice=supertonic:M4`, voice memory 518 MB vs 1035 MB |
| Recognition CPU (ONNX: 4 threads, no spinning) | VERIFIED LOCAL | GigaAM 709 → 293 ms and 7.9 → 1.9 CPU-s per phrase; Parakeet 975 → 511 ms, 10.3 → 2.7 CPU-s |
| French / German speech came back empty («не расслышал») | FIXED → VERIFIED LOCAL | GigaAM empty on real speech → Parakeet: «C'est fait, j'ai ouvert le navigateur…», «Fertig. Ich habe…» |
| Waiting for «Джарвис»: the second recogniser ran on room noise | FIXED | only for a phrase with a wake-like word (test_wake_like_words_only_run_the_second_recogniser) |
| «Джарвис— …» (dash after the name) dropped by the companion | FIXED → VERIFIED LIVE (recorded voice) | test_name_followed_by_a_dash_is_addressed; three tasks in one phrase done in order |

## Self-improvement and living speech (2026-10-24)

| Requirement | Status | Evidence |
|---|---|---|
| Problem journal from JARVIS's own records (repeats within 2 min, failed tasks by error, spoken answers > 6 s, not heard, «не понял») | VERIFIED LOCAL | test_journal_finds_repeats_failures_and_slow_answers; real data: 32 slow answers (worst 42.8 s, messages before the local parser), 4 WhatsApp contact-search failures |
| Change on a separate branch/worktree, the running copy untouched, JARVIS runs the suite itself | VERIFIED MOCK | test_change_happens_on_a_separate_tested_branch_and_applies_only_on_request (scripted Claude Code, temporary repo) |
| Failing tests are never applied; only `improve/…` branches | VERIFIED MOCK | test_failing_tests_are_never_applied |
| Both steps need the person's confirmation (HIGH risk) | VERIFIED LOCAL | test_tools_need_the_persons_confirmation |
| Living speech → local command (no AI, no wait) | VERIFIED LOCAL | test_living_speech.py (7 phrasings; negation, «стоп», messages unchanged) |
| «Открой папку с колледжем» opens the folder itself, any grammatical case; unknown name → the conversation | VERIFIED LOCAL | test_the_folder_itself_opens_not_a_folder_inside_it |
| Long-term memory: remember / recall / forget by voice, facts in every AI request, near-duplicates replaced | VERIFIED LOCAL | test_memory.py (4) |
| End of a phrase by the voice (Smart Turn v3.2, 23 languages incl. Russian, 93.5 % in the authors' benchmark) | VERIFIED LOCAL | features = transformers' Whisper extractor to 1e-5; ~63 ms on this CPU; finished phrases 0.85–0.99 over 5 syntheses (never held); a phrase cut mid-thought is caught at some cut points only (0.01–0.99 by where it is cut) — only confident answers are used, otherwise behaviour as before (test_turn.py, test_a_voice_that_trails_off_waits_for_the_rest) |
| Self-improvement end to end with the real Claude (dev core) | VERIFIED LIVE | «Улучши себя: определитель языка…» → improve_issues → self_improve (approved) → branch with a test, JARVIS's own run 389 passed + UI unit in 202 s → «Примени» → merged after re-testing the merged code (394 + 42), restarted itself in 91 s (commit 0cc5171). Review found «друга»/«доброго» shared with Russian — fixed in f487e7f |

## WhatsApp without blind waits; a journal of problems that are still there (2026-10-24 night)

Found in the real JARVIS's records: the 4 «WhatsApp Web не дал найти контакт» failures (9–11 s to the answer) were all on
23.10 before the popover fix; the same request worked on 24.10. Sends since: 3.7–6.2 s.

| Requirement | Status | Evidence |
|---|---|---|
| Search / open chat wait for the results instead of 1.5 s each (twice per message) | VERIFIED LOCAL (Chromium, page copying WhatsApp's list, results late and in parts) | test_search_waits_for_all_results_not_a_fixed_pause (both «Рома» found, 1.3 s incl. a 0.7 s late result), test_opening_a_chat_stops_waiting_when_it_is_listed (< 1 s) |
| The next message to the same person does not search again; another open chat is never taken for it | VERIFIED LOCAL | test_the_chat_already_open_is_not_searched_again, test_another_open_chat_is_never_taken_for_the_one_asked |
| A covering pop-up is closed before clicking (was: 5 s click attempt first) | VERIFIED LOCAL | popover test 5.8 → < 1.3 s; unknown layer 9.1 → 4.1 s |
| Real WhatsApp send with the new code | NOT VERIFIED yet | needs the real core restarted and a message to self |
| Problem journal: a request done fine later is not reported | VERIFIED LOCAL | test_a_problem_that_went_away_is_not_in_the_journal; real records (read-only): WhatsApp 4 → 1, e-mail failure gone |

## «Заказы»: the fashion-orders app inside JARVIS (2026-10-25)

The client's Excel assistant (factories, customers AA / IDS / SS, markups, deposits, documents) is
[morph-rq/alina-fashion-ai](https://github.com/morph-rq/alina-fashion-ai), vendored as `fashion/` (git subtree, squashed;
`git subtree pull --prefix=fashion … main --squash` brings its updates). Its own suite runs in its own venv:
`cd fashion && ~/.local/share/jarvis/fashion/.venv/bin/python -m pytest -q -o addopts=""`.

| Requirement (from the client's brief) | Status | Evidence |
|---|---|---|
| Factory Excel → items, photos, sizes, prices, customers, percentages; a newer file compared with the ledger | VERIFIED LOCAL (synthetic factory workbook) | fashion suite 396 passed, 11 skipped (the 11 need the client's real workbook, not on this machine); live upload → review → «Сохранить в учёт» in the browser |
| Factory PDF (proforma) creates orders like Excel: items, photos, sizes, customers, the factory's % | VERIFIED LOCAL (synthetic proforma) | test_pdf_order.py: the PDF gives exactly the Excel fixture's analysis; upload → review (photos) → saved; a scan stays a reference |
| Orders split per customer (separate Excel + PDF each, only that customer's lines, leak-checked) | VERIFIED LOCAL | test_customer_documents_contain_only_that_customer_and_match (PDF had black boxes for Cyrillic on Linux → Noto/Liberation/DejaVu fonts added) |
| Customer markups independent of the factory's; 50 % deposits of customer and factory sums; what the owner adds | VERIFIED LOCAL | fashion settlement tests; live: «IDS 35 %» → deposit €1 125 → €1 518.75, owner €1 215 → €821.25 |
| Add / cancel items, sizes, readiness, shipments, payments | VERIFIED LOCAL (upstream) | fashion suite (ledger actions, Ready lists, shipments, payments) |
| Assistant without an API key: Claude Code (the user's subscription), the app's typed tools over MCP, nothing saved without «Подтвердить» | VERIFIED MOCK + LIVE | test_claude_code.py (5, scripted CLI speaks real MCP); live: «Какая вещь у AA самая дорогая и сколько AA должна внести предоплаты?» → 2 tool calls, exact sums, 8.4 s |
| JARVIS starts / restarts / stops the app; data outside the code; window may frame only that origin | VERIFIED LOCAL | test_fashion.py (the real app on a free port; CSP frame-src) |
| Voice through JARVIS (local STT/TTS instead of ElevenLabs): phrase about orders → the app's assistant, same dialog as the workspace, short answer spoken | VERIFIED LIVE (recorded voice into the companion, dev core) | «Джарвис, поставь ай ди эс 35 %» → card in «Заказы» (window switched by itself); «Джарвис, да, сохрани» → saved, page shows the new figures |
| Customer codes as the recogniser writes them («СС», «ИДС», «ID», «S», «Excel EPDF») | VERIFIED LOCAL | test_what_the_local_recogniser_writes (+ sizes and «Apple ID» left alone) |
| «АА» spoken as letters | NOT RELIABLE | GigaAM wrote «эй эй» as «я ей» and «А-А» as nothing (synthetic voice); a customer alias the owner says is the workaround |
| Its own JARVIS commands stay JARVIS's («открой папку заказы», «который час») | VERIFIED LOCAL | test_jarvis_keeps_its_own_commands |
| With the real owner's voice and real factory files | NOT VERIFIED | needs the client |
| Russian interface of the whole orders app | VERIFIED LOCAL | test_i18n_ru.py; all 43 pages scanned: 998 English strings → 79 (data, the browser's «No file chosen», a few JS labels) |

## The computer and the desk, from morph-rq/jarvis-os (2026-10-25)

| Requirement | Status | Evidence |
|---|---|---|
| Weather (Open-Meteo, no key; city from memory), daily briefing (weather, reminders, scheduled messages, decisions waiting, «Заказы» proposals, failed task, low battery) | VERIFIED LIVE (read-only) | Москва / Лиссабон answered in 0.8 s; «в Москве» → Москва by population (was the hamlet Москверо) |
| What slows the computer (programs summed), end a program (MODERATE; never session/system/JARVIS processes) | VERIFIED LOCAL | test_ending_a_program_never_touches_session_or_jarvis (a real child process; zombies count as ended) |
| Disk space + what can be cleaned; cleanup of caches only (MODERATE; Trash only when asked; links not followed) | VERIFIED LOCAL | test_cleanup_empties_only_the_chosen_caches; live scan: 3.9 GB of caches here |
| Health check: CPU, memory, disks, battery wear, temperatures, failed services, journal errors | VERIFIED LIVE (read-only) | 0.5 s on this laptop |
| Brightness and keyboard backlight, theme light/dark/by name, wallpaper, do-not-disturb, night light, speakers/headphones/microphone, internet/Wi-Fi/Bluetooth, power profile, sleep/reboot/power off (HIGH), music, clipboard (password-manager copies refused), local OCR of the screen | VERIFIED LOCAL (parsers on real output; read-only calls live) | test_machine.py (41); changing the real desktop is not done in tests |
| A locked screen is said at once (screenshot, OCR, dictation never types into the lock screen) | VERIFIED LOCAL | test_locked_screen_is_said_at_once, test_never_types_into_the_lock_screen (grim hung > 10 s while hyprlock was on) |
| Dictation into the window in focus (Super+Alt+D, «Джарвис, диктовка»), commands still work, no Enter in terminals | VERIFIED MOCK | test_dictation.py (10) through the companion's real voice path; not typed on the real desktop yet |
| Calendar: own events with a reminder before each, Russian dates said and parsed, subscribed iCal links (read-only), the day's events in the briefing | VERIFIED LOCAL | test_agenda.py (russian dates, phrases, reminder + removal, a subscribed feed, a broken feed counted not fatal); a real Google iCal link not tried |
| JARVIS from the phone: a private Telegram bot (token from @BotFather in Settings → «Телефон», a 6-digit pairing code shown only in the window) — text and voice notes go to the same director, answers and made files (notes, screenshots) come back, factory files → «Заказы», others → ~/Downloads/JARVIS | VERIFIED MOCK (fake Bot API) + LOCAL UI | test_phone.py (12): strangers get one line then silence, groups ignored, expired/guessed codes don't pair, buttons only from the owner's chat, a typed «да» decides the one waiting action, high risk and durable permission changes only at the computer, revoked token shown in Russian. Dev core window: section renders, a malformed token is refused, a made-up one is refused by the real Telegram and not stored. A real bot with a real phone NOT VERIFIED (needs the owner's token) |
| Proactive notices: low/critical battery, almost full disk, memory nearly exhausted, a newly failed service, internet lost/back — chat line + desktop notification + toast; spoken only when urgent; cooldowns, 4 an hour, quiet hours, do-not-disturb | VERIFIED LOCAL (fake readings) + LIVE readings | test_proactive.py (15); the readers on this laptop: battery 79 % not charging, memory 37 %, no failed units, online, 0.5 s per check |
| Watches «скажи, когда …»: CPU/memory/disk above, battery below, a site down/up, a coin price (CoinGecko) — fire once, spoken; without the AI | VERIFIED LOCAL | test_proactive.py (phrases, fire once, list/remove); site and price checks not tried against the real internet |
| Smart home through Home Assistant: status, lights/switches/fans/media/climate/scenes/blinds (LOW, ≤ 5 devices), locks/alarms/garage/gate/door/scripts/«весь свет» (HIGH, click only — no voice «да»); each change read back | VERIFIED MOCK (fake Home Assistant) | test_home.py (14): token checked before storing, allowlisted services only, refusals, a stuck device → failed. No Home Assistant in this house: a real one NOT VERIFIED |
| Meeting notes: «запиши встречу» (MODERATE, asked) → PipeWire records the mic + what the speakers play → local GigaAM transcript with times → Claude summary (only in «Облако разрешено»), raw audio deleted; window shows «запись встречи»; auto stop after 3 h | VERIFIED MOCK (fake recorder / recognition / Claude) + LOCAL recording | test_meeting.py (7: private mode, a Claude limit, no recognition → audio kept, cancel); pw-record captured 2.9 s of the mic and of the speaker monitor here; mixing + cutting an hour: 5.4 + 0.7 s. A real meeting end to end NOT VERIFIED |
| Not ported yet | NOT IMPLEMENTED | maps, documents engine, Gemini Live (JARVIS is Claude-only, ADR-005) |

## One JARVIS: the friend's fast search and «Заказы» inside JARVIS's own architecture (2026-10-25)

Before: web questions went to Claude's own WebSearch (a model call first, then its searches); the jarvis-os research
engine (SearXNG + Morphic + Gemini) was not ported. «Заказы» was a second app in a frame: its own top bar and
navigation, its own assistant («Спросите Fashion AI»: a separate Claude Code process, own system prompt, own dialog),
its own tour and AI settings, a light theme. After: one assistant (JARVIS), one task list, one conversation memory.

| Requirement | Status | Evidence |
|---|---|---|
| Fast web search in the core (jarvis/research.py): local SearXNG child (pinned commit jarvis-os tested, own venv, loopback), engines raced one by one with a soft deadline, a failing engine rests, ru/en/news groups + fallback group, de-dup + ranking, cache (fresh 10 min / 6 h / 12 h), the same question twice at once = one search, SSRF-safe page reading (public addresses only, checked at connect and on every redirect), passages about the question, text addressed to an AI removed, invented links removed from the answer | VERIFIED LIVE (search) + MOCK (agency) | test_research.py (17). Live on the dev core: «поставщики картонных коробок Алматы» DuckDuckGo + Brave 1.1–1.5 s, 18 sources, 3 pages read 0.8–3.2 s; repeat from the cache 0.1 ms. Google / Bing web / Yandex / Qwant refused this laptop (403 / CAPTCHA) — fallback only |
| «Какая сейчас цена Bitcoin?» / «курс доллара» — a data source, no search, no AI | VERIFIED LIVE | market_quote (CoinGecko / ExchangeRate-API): 0.35–0.41 s with the source and the time; router phrases tested (нефть, iPhone, «курсовая» are not quotes) |
| «Найди …» = fast search (a tool, no Claude start) → Claude writes the answer from the numbered sources [n]; numbers checked against the sources | VERIFIED MOCK + LIVE search | test_find_info_is_a_fast_search_then_one_answer_no_claude_code; live while the Claude account was at its limit: the found pages with links in 1.9 s and an honest «⚠ Шаг «ответ» не выполнен» |
| Search → development: the results go to Claude Code as context; «используй найденную информацию в проекте» later in the conversation | VERIFIED MOCK | test_search_then_code_gives_claude_code_the_results, test_follow_up_uses_what_was_found_without_repeating (computer_task attaches the conversation's last search when it is the same task, referred to, or shares its key words) |
| Search down → Claude's own WebSearch, said so in the decision | VERIFIED MOCK | test_search_down_falls_back_to_claude_web_search_honestly |
| «Заказы» pages are part of JARVIS: JARVIS's colours, no second app chrome (top bar, «Fashion AI», tour, tab bar, footer hidden only inside JARVIS's frame), sections are tabs in JARVIS's header, the assistant page leads back to the orders | VERIFIED LIVE (dev core, synthetic data) + LOCAL | tests/ui/orders-look.mjs: 5 tabs, embedded on each, no chrome visible; fashion tests test_a_page_in_jarvis_frame_is_part_of_jarvis |
| One assistant for orders: the module's rules answer at once; what they cannot answer is handed back and answered by JARVIS's own conversation with the module's typed tools (`orders`, through the Gateway; changes only as proposals) — the module never starts its own Claude inside JARVIS | VERIFIED MOCK (both sides) | test_no_rule_answer_means_jarvis_own_conversation_answers_with_the_module_tools; fashion test_no_rule_answer_is_handed_to_jarvis_and_its_tools_make_a_proposal (its own Claude Code turn would fail the test) |
| «Подтвердить» on the card in JARVIS's chat (was: in the module's dock) | VERIFIED LIVE (dev core) | tests/ui/orders-card.mjs: «Поставь BL 40 процентов» → rule answer 0.29 s, card with before/after → click → «Сохранено в заказах», card closed |

Live with the real Claude (dev cores, 2026-10-25 04:05–04:20, after the account's limit reset):

| Scenario (the owner's directive) | Status | Evidence |
|---|---|---|
| 1. «Найди актуальную информацию о Bitcoin» | VERIFIED LIVE | web_search 1.4 s → Claude answer; total 4.6 s, first words 2.3 s; CoinGecko price as source [0] |
| 2. «Найди поставщиков картонных коробок в Алматы» | VERIFIED LIVE | 9.4 s (search 4.0 s with 3 pages read); 9 suppliers with [n] sources; «цен и телефонов в результатах нет» said, nothing invented |
| 3. Search + development: «Найди документацию Open-Meteo API и подключи его к моему приложению …» | VERIFIED LIVE | web_search 2.3 s → approval → Claude Code 55 s (app.py, tests/test_app.py) → JARVIS re-ran the tests: 6 passed, step verified |
| 4. «Создай небольшое приложение для управления заказами» | VERIFIED LIVE | the conversation → computer_task (Claude Code, approved) 106 s → orders-app (HTML/CSS/JS, statuses, totals) opened; no second JARVIS; a weak «заказ» word no longer sends it to the orders module |
| 5. Search, then «Теперь используй найденную информацию в моём проекте …» | VERIFIED LIVE | no second search; Claude Code got the search as context (ctx research_for = that task); air_quality() added, 17 tests passed |
| 6. A long search while the person keeps working | VERIFIED LIVE | during a 2.2 s search «который час» and «курс евро к тенге» answered in 0.11 s each |
| 7. Search module down (searxng_url → a dead port) | VERIFIED LIVE | web_search failed «SearXNG … не отвечает» → Claude WebSearch (decision says so, the answer says «Быстрый поиск сейчас недоступен…»); 34.6 s |
| 8. Window: workspaces, companion, chat, palette, files, approvals | VERIFIED LIVE | tests/ui/workos.mjs on a clean fixture home: 21/21 checks, 0 console errors |
| «Заказы» question the module's rules cannot answer → JARVIS's own conversation with the module's tools | VERIFIED LIVE | «Почему у клиента BL такая сумма… предложи 25 %»: 4 orders tool calls through the Gateway, exact figures, card «Подтвердить»; 7.4 s. First run found a real bug: «markup_percent: 25» was taken as 0 % — now refused by the module with the right names, and the turn carries the tools' exact parameters |
| Claude unavailable (session limit) | VERIFIED LIVE | search: the found pages with links in 1.9 s + an honest «⚠ Шаг «ответ» не выполнен»; orders: the module's full rules answered in 0.7 s («⚠ ИИ сейчас недоступен… Ответил по правилам модуля»), files as buttons |

Speed, the same three questions on the same machine, same Claude account, warm conversation (before = the previous path,
Claude's WebSearch; after = JARVIS's fast search + one Claude answer):

| Question | Before | After |
|---|---|---|
| «Найди актуальную информацию о Bitcoin» | 15.3 s | 4.6 s (first words 2.3 s) |
| «Найди поставщиков картонных коробок в Алматы» | 23.5 s (first words 22.8 s) | 9.4 s (first words 5.1 s) |
| «Что нового в мире 3D-принтеров? Найди в интернете» | 13.4 s | 4.9 s (first words 2.1 s) |
| «Какая сейчас цена Bitcoin?» | (a Claude turn) | 0.4 s, no AI |

One run each (no averaging); network and Claude latency vary. SearXNG: ~170 MB RSS while JARVIS runs.

## OpenClaw, Hermes and the coding agents, from morph-rq/jarvis-os (2026-10-25, ADR-008)
Tests: `uv run pytest -q` → 597 passed, 6 skipped (19 in `tests/test_hub.py`, stand-ins for both agents and Claude);
`node --test tests/ui/unit/` → 42 passed. Live checks on a dev core (own JARVIS_HOME, ports 8899 / 18889 / 18742).

| Requirement | Status | Evidence |
|---|---|---|
| OpenClaw 2026.10.6 + Hermes 0.19.0 installed pinned, user level | VERIFIED LOCAL | `scripts/install-agents.sh`: Node sha256, npm ci (344 packages), 78 hashed wheels |
| Both started by the core on loopback with their own keys; settings through their own tools | VERIFIED LIVE | dev core: OpenClaw 505 methods, 55 skills, 62 plugins; `<data>/hub/migrations.log` |
| OpenClaw thinks with Claude through the Claude Code sign-in (no key) | VERIFIED LIVE | agent turn via `claude-cli/claude-sonnet-5-5`, 7.6 s |
| Hermes thinks with Claude through `/llm/v1` (`claude -p`) | VERIFIED LIVE | answer 3.8 s; file read 9.4 s; 2.5–6 s per model turn |
| Hermes searches through JARVIS's fast search (MCP → Gateway) | VERIFIED LIVE | «Поручи Гермесу: … Лувр …» → web_search step on the same task, answer with source, 14.2 s |
| Hermes's risky steps become JARVIS approval cards | VERIFIED MOCK | test_hermes_steps_become_jarvis_approval_cards |
| Upstream skill runs only after a HIGH approval; deny = nothing runs | VERIFIED MOCK | test_an_upstream_skill_runs_only_after_a_yes |
| One catalog, duplicates hidden, Russian requests ranked | VERIFIED MOCK + LIVE | test_one_catalog…; live: 211 capabilities, 2 duplicates |
| ClawHub install only clean + reviewed, hash-checked; updates with rollback | IMPLEMENTED (ported), MOCK-tested pieces | `hub/updates.py`; not run against the real ClawHub (no skill installed) |
| Emergency stop reaches the agents; «останови агентов» | VERIFIED MOCK | test_emergency_stop_reaches_the_agents |
| OpenClaw on demand (sleeps, wakes for work, keeps when needed) | VERIFIED MOCK + LIVE | test_openclaw_sleeps…; live: asleep after start, woke from the panel in 23 s (its own start-up ~20 s) |
| A gateway left by a killed core is stopped, never the real one of another home | VERIFIED LOCAL + LIVE | test_a_gateway_left…; live audit `hub.stale_stopped` |
| Coding-agent hooks: merged with backup, idempotent, removable, unreadable file untouched | VERIFIED LOCAL | test_hooks_are_added_and_removed… |
| Relay never blocks an agent (no JARVIS / no window / no answer → terminal asks) | VERIFIED LOCAL | test_the_relay_reports_sessions… |
| Real Claude Code permission request answered from the window | VERIFIED LIVE | `claude -p` with hooks via `--settings` → card → «Разрешить» → Bash ran, file created, 10 s |
| «Возможности» workspace, 8 sections, 0 console errors | VERIFIED LOCAL | headless screenshots against the dev core |
| Telegram/WhatsApp/Slack/Discord/Signal through OpenClaw | IMPLEMENTED UNVERIFIED | needs the person's own bot tokens / QR |
| Hooks installed into the person's own Claude Code accounts | NOT DONE YET | waits for «Подключить» in «Возможности → На компьютере» |

## Claude Code as JARVIS's agent: routing, reliability, plain words (2026-10-25, ADR-009)
Tests: `uv run pytest -q` → 612 passed, 6 skipped (28 in `tests/test_claude_agent.py`); `node --test tests/ui/unit/` →
42 passed. Live: dev core (own JARVIS_HOME, port 8805, real Claude), the voice = Supertonic F1 WAVs played as the
companion's microphone (`tests/ui/voice-mic.mjs`), window events logged over the WebSocket.

| Requirement | Status | Evidence |
|---|---|---|
| Voice → STT → Claude → spoken answer | VERIFIED LIVE | «объясни в двух предложениях, почему небо голубое»: STT 0.52 s, first words 2.2 s, answer 2.8 s, 3 sentences spoken |
| Conversation context across turns | VERIFIED LIVE | «а почему тогда закат красный?» answered from the previous answer, first words 1.1 s |
| Research by voice: fast search, then Claude with sources | VERIFIED LIVE | «найди в интернете, в каком году построили Эйфелеву башню»: web_search 1.45 s + Claude 1.56 s, [n] sources |
| Multi-step request with JARVIS tools + approval by voice | VERIFIED LIVE | «узнай, сколько места на диске, и запиши это в заметку» → disk_usage → create_note → «Разрешить?» aloud → «Джарвис, да» → note with the real figures → «Записал…» |
| A quick command never drops the rest of a compound request | VERIFIED LIVE + MOCK | before: only disk_usage, note silently dropped (live); after: whole request to Claude; test_a_second_action_sends…, test_claude_does_both_parts… |
| Interruption by voice | VERIFIED LIVE | «расскажи подробно историю интернета» … «Джарвис, стоп» → speech.stop in the same second |
| Fallback model when the chosen one is unavailable | VERIFIED LIVE | main model = claude-sonnet-0-0 → answer from claude-haiku-5-5, progress «Основная модель перегружена — отвечает запасная» |
| CLI failures in plain words, on screen and by voice | VERIFIED LIVE (model unavailable) + MOCK (limits, sign-in, overload, network, hang) | live: «Выбранная модель Claude (claude-sonnet-0-0) сейчас недоступна…»; the real journal's «You've hit your session limit · resets 4am» → «Лимит Claude исчерпан — снова заработает в 4:00» / «…вернётся в 4 утра» |
| Limit remembered until it resets, then cleared | VERIFIED MOCK | test_limit_is_remembered_until_it_resets |
| What Claude is doing = the task's progress line, never raw tool calls | VERIFIED LIVE + MOCK | live: «Место на диске», «Создать заметку»; test_progress_lines_never_show_raw_tool_calls |
| API retries announced (screen; voice once) | VERIFIED MOCK | test_cli_events_reach_progress… (an overload cannot be caused on purpose) |
| A hung CLI process is ended, a long tool call / API back-off is not | VERIFIED MOCK | test_a_silent_process_is_ended…, test_a_long_jarvis_tool_or_an_api_retry_is_not_a_stall |
| «Ответь одним словом: …» is a question, not a draft | VERIFIED LIVE + MOCK | before: «Голубой.» + a line about a draft; after: «Зелёная.» |

## Window cleanup and the companion (2026-10-25, user's request)
Tests: `uv run pytest -q`, `node --test tests/ui/unit/`, `node tests/ui/workos.mjs` against a dev core (21/21, 0 console errors).

| Requirement | Status | Evidence |
|---|---|---|
| «Главная», «Задачи» and the right panel («Сейчас», privacy, reminders, decisions) removed; nothing shown twice (breadcrumb, Settings → «Самоулучшение», companion «Контекст») | VERIFIED LOCAL | workos.mjs «app starts on the chat»; screenshots |
| Reminders still visible: a top-bar chip with the next one (all in its tooltip) | IMPLEMENTED UNVERIFIED | no scheduled reminder in the dev run |
| Desktop companion: no Hyprland animation/border/shadow; resize+move+rounding atomic | VERIFIED LIVE | real window: `getprop no_anim` true, `border_size` 0, `no_shadow` true; `[[BATCH]]` answers ok×3 |
| Desktop companion idle after a finished task: no redraws (was every 0.8 s forever) | VERIFIED LOCAL | before: 118 DOM changes / 8 s; after: 0 / 2.5 s (workos.mjs «desktop companion») |
| Thinking shown on the face (three dots) | VERIFIED LOCAL | workos.mjs «desktop companion»: THINKING, dots opacity 1 |
| «Секунду, думаю.» when a spoken answer has not started after 2.5 s; none for quick answers | VERIFIED MOCK | test_slow_answer_says_it_is_thinking, test_quick_answer_gets_no_thinking_phrase |
| Glow blur → gradient | REJECTED | measured 0.6 % vs 0.5–0.6 % of a core while listening at 64 px: no gain, look kept |


## Excel and other tables (2026-10-26, user's request «проверь, как JARVIS работает с Excel»)
Ten requests typed into the chat of a dev core with the real Claude (`tests/ui/chat-run.mjs`, APPROVE=1), on test
workbooks with known answers: generated sales (240 rows + «Итого»), a family budget in blocks, a 20 000-row export,
a semicolon CSV, a factory order, and Microsoft's public «Financial Sample.xlsx». Before = the same ten requests on
f7ce0f4 (every question went to a Claude Code job). Tests: `tests/test_sheets.py`.

| # | Request | Before | After | Status |
|---|---|---|---|---|
| 1 | «Что в файле «Продажи 2026.xlsx»?» | ✗ refused («сохраните как CSV»), 12 s | sheets, columns, 240 data rows, the «Итого» row apart, 6.4 s | VERIFIED LIVE |
| 2 | total revenue and the best product | ✓ 52 s, 1 approval | 79 261 000 ₸, «Ноутбук» 47 700 000 ₸ — exact, 5.6 s, no approval | VERIFIED LIVE |
| 3 | Иванов in March | ✓ 31 s, 1 approval | 2 022 000 ₸ — exact, 4.3 s | VERIFIED LIVE |
| 4 | CSV: rows, clients from Алматы | ✓ 31 s, 1 approval (routed to «Заказы» first) | 150 / 22 — exact, 5.1 s, not the orders ledger | VERIFIED LIVE |
| 5 | Financial Sample: country with the most profit | ✓ 26 s, 1 approval | France 3 781 020,78 — exact, 5.1 s | VERIFIED LIVE |
| 6 | 20 000 rows: orders, average, cancelled | ✓ 31 s, 1 approval | 20 000 / 125 889 / 1 008 — exact, 9.4 s (reading 0.8 s) | VERIFIED LIVE |
| 7 | create «Бюджет на ноябрь» with formulas | ✓ 71 s, a Python project (venv, git) left next to it | one .xlsx, totals as formulas, 10–13 s, 1 approval | VERIFIED LIVE |
| 8 | add «Связь» 6 000 to the expenses | ✓ 25 s, 1 approval | row inserted above the total, SUM range grown, 356 000 / 794 000, backup kept, 8.2–8.5 s | VERIFIED LIVE |
| 9 | pivot: revenue by region and month, new file | ✓ 48 s, 1 approval | 3 × 6 table + totals as formulas, every cell equal to the known answer, 13–14 s | VERIFIED LIVE |
| 10 | load the factory file into «Заказы» | ✓ 7 s | unchanged: «Заказы» read it, waits for confirmation, 6.1 s | VERIFIED LIVE |

| Requirement | Status | Evidence |
|---|---|---|
| Reading and computing need no approval; creating / changing asks once | VERIFIED LIVE + MOCK | live: approvals only in 7, 8, 9; test_a_question_about_a_workbook_needs_no_approval, test_changing_a_workbook_asks_first |
| Numbers come from the file, not from the model's arithmetic | VERIFIED LIVE | every figure above equals the generator's / an independent openpyxl count |
| Inserted / deleted rows move formulas like Excel (other sheets too, a total right below grows) | VERIFIED MOCK + LIVE | test_formulas_follow_inserted_and_deleted_rows_like_excel; live #8 |
| Saved files keep the formulas' results (phone previews, Telegram, other programs see the totals) | VERIFIED LIVE + MOCK | live: openpyxl data_only reads 356 000 / 794 000 and the pivot totals; LibreOffice opens them; test_saved_files_keep_formula_results… |
| New formula cells shown as dates / numbers / text by what they compute | VERIFIED MOCK | test_saved_files_keep_formula_results… (=DATE → a date, not «46 327») |
| Files with charts / pivot tables / pictures are never edited in place | VERIFIED MOCK | test_edit_never_loses_charts_in_place |
| xls / ods via LibreOffice | IMPLEMENTED UNVERIFIED | no .xls / .ods sample in the run |
| While Claude's limit lasts, every request hears the same plain sentence | VERIFIED LIVE (found) + MOCK | live: 2nd–9th request said «нет доступного исполнителя … (RATE_LIMITED)»; test_while_the_limit_lasts… |

## Real accounting files from the internet (2026-10-26, user's request «найди в интернете бухгалтерские Excel и проверь все 4 инструмента»)
Downloaded: nalog-nalog.ru — cash book КО-4 sample (.xls), trial balance blank (.xls), payroll statement Т-49 filled
sample (.xls, 5 employees); buhexpert8.ru — Т-49 blank (.xlsx); glavkniga.ru — Т-49 blank (.xls); gov.uk — HM Treasury
«spending over £25,000» December 2025 and March 2026 (CSV, Windows-1252, 149 / 136 payments); the КО-4 also as .ods.
Independent answers: the Т-49 figures by hand, the ledgers with Python's csv module. Direct run of the four tools:
27/27 checks. Chat with the real Claude on a dev core (`tests/ui/chat-run.mjs`):

| # | Request | Tools | Result | Status |
|---|---|---|---|---|
| 1 | «Что в файле Т-49 (образец).xls?» | sheet_read | 5 employees, accrued 387 359, НДФЛ 50 357, advance 120 000, to pay 193 200 — 10.3 s | VERIFIED LIVE |
| 2 | totals and «сходится ли с шапкой» | (context) | 387 359 / 50 357 / 193 200, checked against the header's «Сто девяносто три тысячи двести» — 4.7 s | VERIFIED LIVE |
| 3 | who gets the most | (context) | Васильченко И.Д., 69 450 — 1.9 s | VERIFIED LIVE |
| 4 | UK ledger: payments, total, top-3 suppliers | sheet_read, sheet_query | 149, £27 495 125.11, NS&I / GPA / Royal Mint with exact sums — 11.7 s | VERIFIED LIVE |
| 5 | compare December and March (different column names) | sheet_query ×4 | £27 495 125.11 vs £25 624 008.59, both suppliers' change exact — 18.9 s (before the fix: a tool-less step answered with raw `<invoke>` text) | VERIFIED LIVE |
| 6 | new workbook: spending by type, sorted, total as a formula | sheet_query, sheet_create | 31 types, =SUM total 27 495 125.11 — 13.4 s, 1 approval | VERIFIED LIVE |
| 7 | fill the Т-49 blank (.xlsx): one employee, НДФЛ and to pay as formulas | sheet_read, sheet_edit | row 26 in the right graphs (checked as a PDF), =ROUND(DR26*0.13,0) → 11 700, to pay 78 300 — 22.5 s | VERIFIED LIVE |
| 8 | change one amount in the .xls sample | sheet_edit | new .xlsx next to it, the .xls untouched; JARVIS noticed the header no longer matches — 8.2 s | VERIFIED LIVE |
| 9 | «Что в файле … .ods?» | sheet_read ×2 | an empty КО-4 blank, both sheets described — 9.8 s | VERIFIED LIVE |
| 10 | fill the cash book's title (.xls) | sheet_read, sheet_edit | organisation in A7, year in AD16 on cover and title page (checked as a PDF) — 13.4 s | VERIFIED LIVE |
| 11 | the largest March expense type | sheet_query | Facilities Service Charges Recoverable, £5 784 736.64 in 8 payments — 12 s | VERIFIED LIVE |

| Found on the real files and fixed | Before | After | Evidence |
|---|---|---|---|
| Unified forms (graph-number row under 2–3 title rows) | not a table; a form's title row taken for a 134-column header | Т-49 = table, «к выплате (гр. 21)», signatures not data | test_a_unified_form_is_a_table_under_its_graph_numbers; live 1–3 |
| Forms on an 80–256-column grid | hundreds of empty « | » cells | only filled cells, «AC14=КАССОВАЯ КНИГА» | live 9, 10 |
| English ledger exports | «31,037.55» and «04-Dec-25» were text: no sum, no months | numbers and dates; cp1252; «(5 000)», «£», «1.234,56» | test_numbers_and_dates…, test_an_english_ledger_export… |
| Merged boxes of a form | writing into one crashed (openpyxl MergedCell) | set → the box's first cell; a new form row gets the boxes of the row above | test_a_form_is_filled_box_by_box |
| .xls / .ods blanks | could not be changed at all | filled into a new .xlsx next to the blank | test_an_old_format_blank_is_filled_into_a_new_xlsx; live 8, 10 |
| «Сравни …» about files | a tool-less one-off step, raw tool-call text shown | the conversation with the table tools | test_a_comparison_needing_files_or_the_web…; live 5 |
| Narration glued to the answer | «Сначала найду файлы. Суммы по месяцам есть. Теперь…» | the answer only (narration still shown live) | test_the_answer_keeps_no_narration…; live 11 |

## Two voices; the whole app checked again (2026-10-26, user's request)
Settings → Голос: only Supertonic M4 (male, UTMOS 3.95) and F1 (female, 3.99) — one model for every language; the ten
Supertonic voices, five Silero and seven Kokoro choices are gone (Silero / Kokoro / Piper remain the silent fallback).
Demo copy (own JARVIS_HOME, real Claude): `tests/ui/workos.mjs` 21/21; 30 typed requests (`chat-run.mjs`) 30/30 after
the fixes below; three spoken requests (`voice-mic.mjs`, Supertonic F1 as the speaker) ru / en / ru.

| Found | Before | After | Evidence |
|---|---|---|---|
| A weather-service timeout | «Не нашёл город «алматы»» after 10 s | asked again (5 s each); a silent service is said as such | test_a_silent_weather_service_is_not_an_unknown_city; direct: Алматы 1.1 s |
| Russian after English, by voice | «The total revenue for 2026 is 79 million…» | «Общая выручка за 2026 год — 79 261 000 тенге…» | `[Язык ответа: …]` with every message; test_every_message_says_the_language… ; live 11:53 |
| An English phrase while waiting for «Джарвис» | dropped: GigaAM wrote «How Mauch Free Space Is Loft On My Disk» (no name), Parakeet never ran | Latin output → Parakeet → «Jarvis, how much free space is left on my disc?» | test_a_phrase_written_in_latin…; live 11:58, 12:02 |
| JARVIS's mark echoed | «[voice] System disk has…» | «System disk has 29.2 GB free…» | test_jarvis_marks_are_never_repeated…; live 12:02 |
| Fast web search | — | DuckDuckGo CAPTCHA / Brave timeouts on this network today; JARVIS fell back to Claude's own search (correct answer, 26.6 s) | SearXNG `unresponsive_engines` |

## The microphone after sleep (2026-10-26, user's report «показывает, что слушает, но ответа нет» on «Джарвис, привет»)

Cause (measured on the real laptop, ASUS, Intel Alder Lake HDA + Realtek ALC256 digital mic): after waking from a
9-hour sleep (10:26) the capture side ran at 8 200 samples/s instead of 48 000 (kernel hw_ptr; pw-record 0.18 of real
time; direct `arecord -D hw:0,0` → «Input/output error»); speakers were fine. The recognisers were fine (the test phrase
recognised at 0, −20, −34 dB). Not JARVIS's code — but JARVIS kept saying nothing useful.

| Check | Before | After | Evidence |
|---|---|---|---|
| A phrase recorded with the broken mic | empty transcript, «Не расслышал» or nothing (wake mode) | mic measured (1.5 s) → «Микрофон работает с ошибкой — я не слышу слов. Напишите мне «почини микрофон».» + toast + companion card with a button + chat message | test_empty_phrase_with_a_broken_mic_says_how_to_fix, test_wake_listening_tells_a_broken_mic_once; dev core 8816 live 12:43 (speed 0.184) |
| «проверь / почини микрофон» | went to the AI | `mic_check`, no approval, 1.5 s: «…звук с него идёт в 5 раз медленнее… нужна перезагрузка компьютера» | test_check_says_broken_or_fine; dev core live |
| Repair without a reboot | — | tried on the real laptop 12:51 with the user's password: pkexec re-bound `snd_hda_intel` (full codec re-probe in the kernel log) → still 0.18; no PCI reset method; the controller's own D3hot neither. Not shipped: JARVIS offers the reboot (power_action, always asks) | live 12:47 / 12:51, kernel log |
| Silence in push-to-talk | Parakeet made up «Thank you.» (zeros) / «No, no» (−60 dB hiss) → a phantom request | not re-heard below −45 dBFS peak: empty | direct worker check: zeros, −70…−30 dB noise → ''; the test phrase still recognised at −34 dB |

## «Джарвис» right after opening the laptop; every task said aloud (2026-10-26, user's request)
«Сделай так, чтобы джарвис срабатывал на фразу «Джарвис» и начинал слушать задачу … и любую выполненную задачу
проговаривал, независимо, сказал я это в микрофон или написал.» Live checks: dev core 8825 (own JARVIS_HOME), headless
companion page with a Silero-recorded WAV as the microphone.

| Check | Before | After | Evidence |
|---|---|---|---|
| Wake listening | off unless switched on (the real JARVIS had `wake_listen=false`) | on by default (Settings / companion button still turn it off) | test_settings_persist_and_are_announced; live: a fresh companion opened its mic by itself |
| Bare «Джарвис.» | «Да?» after the full 1.3 s pause (1.6 s after the word); a quick name (220 ms voiced by the VAD) was close to the 200 ms «noise» bar | «Слушаю.» (pre-synthesized) 0.83 s after the word: the VAD counts the frames that started the phrase, the short-pause check runs from 0.3 s of speech, a bare name that sounds finished (Smart Turn ≥ 0.30; measured 0.81 / 0.51, «Джарвис, открой» 0.05) is answered at once | test_a_bare_name_that_sounds_finished…, vad.test.mjs «a quick «Джарвис»…»; live voice_turns |
| The task after «Слушаю.» without the name | — (already worked: 20 s follow-up window) | «Который час?» → answered and spoken by the companion | live 13:21 |
| Typed request (main window, companion, `jarvis …` in a terminal) | spoken only with Settings → «Озвучивать ответы и на текстовые запросы» (off, per browser) | spoken by default (core setting `speak_typed`, same toggle); the chat keeps its full answer style (voice style only for spoken requests: `Runtime.spoken`); phone / API stay silent | test_typed_requests_are_spoken_unless_turned_off, test_typed_answers_are_spoken…; live: «17 × 3» typed in the companion → spoken by the companion |
| Reminder at its time | chat message only | also said aloud | test_a_reminder_is_said |
| Only one of the two windows open | the other window's answers went silent | the open one plays them (`Server._speaker`, `/ws?client=overlay`) | test_the_open_window_speaks_when_the_other_is_closed |
| Mic stream dies (device gone, sound server restart, sleep) | the companion showed «слушаю» and heard nothing | reopened on the next tick (track ended / no frames 3 s / digital zeros 3 s, at most every 30 s); keeps retrying while no mic | live: track stopped → reopened in 0.1 s (`voice.mic_reopened why=ended`) |
| Lid opened after sleep | broken mic noticed only after a phrase came back empty | the companion sees the clock jump → `voice.woke` → mic measured twice (2 s + 3 s) → said at once if broken | test_waking_from_sleep_checks_the_mic_twice; live: clock moved 1 h → two checks 0.18 / 0.20 → told |
| The real microphone today | — | **BROKEN in hardware, not fixed by a normal reboot**: after the 13:00 reboot pw-record 0.19–0.20; direct `arecord -D hw:0,0` 48 kHz: 2 s of sound took 9.9 s, 44.1 kHz: I/O error; playback 48 000 frames/s (fine). JARVIS now advises a full power-off, not a reboot; companion button «Выключить компьютер» (asks). Wake listening cannot hear anything until the mic works | measured 13:22–13:30 |

## JARVIS Architect: a 2D plan → parametric BIM → 3D, edited by voice (2026-10-26, ADR-010, user's request)
Engines installed for it: FreeCAD 1.1.4 + Open CASCADE 7.9.3, Blender 5.2.2 (pacman, the user typed the password),
drawing venv (OpenCV 4.12, pdfium, ezdxf 1.4.2), tesseract 5.5.3 (was there). Pascal Editor: not installed, not used.
Test drawings with known answers: `tests/architect/make_plans.py` → `tests/fixtures/architect` (9 × 6 m house:
Кухня 12 m², Гостиная 15 m², Спальня 27 m² by wall axes; 3 doors, 3 windows). Automatic: `tests/test_architect.py`
37 passed (real OpenCV / FreeCAD / OCCT / Blender; the vision answer there is scripted — VERIFIED MOCK);
`tests/ui/unit/architect.test.mjs` 5 passed. Live: Claude Vision through the user's Claude Code, dev core 8840
(own JARVIS_HOME), `tests/ui/architect.mjs` (headless window), `tests/ui/voice-mic.mjs` (Silero WAV as the mic).

| # | Test | Result | Evidence |
|---|---|---|---|
| 1 | Simple house, 3 rooms (`simple3.png`) | 6 walls (exterior 31 px ≈ 300 mm, interior 13 px), 3 rooms; scale from the written sizes | test_drawing_worker_reads_every_test_drawing; local run 2.8 s |
| 2 | Doors and windows (`plan.png`, `doors_windows.png`) | VERIFIED LIVE (Claude): 3 doors 920/1020/920 mm (truth 900/1000/900), 3 windows 1400/1800/1500 mm (exact), rooms on both sides right; locally (no cloud) 3 doors + 3 windows by glazing lines in the gap | live 20.8 s; test_drawing_worker… (kinds 3 + 3) |
| 3 | House with furniture (`furniture.png`) | VERIFIED LIVE: sofa → Гостиная, table → Кухня, bed → Спальня; 17 procedural 3D furniture kinds in Blender (`FURN_001_VIS`) | live 20.6 s |
| 4 | Hand sketch (`sketch.png`, the task's СПАЛЬНЯ / КУХНЯ / ВАННА) | VERIFIED LIVE: drawing_kind hand_sketch, 6 single-line walls, rooms Спальня / Кухня / Ванна (bathroom); no sizes → asks for the scale | live 8.6 s; local OCR reads the names too |
| 5 | Plan with sizes (`plan.png`) | scale 10.0 mm/px, confidence 0.95 (2 of 2 sizes agree); areas exact, clear floor (OCCT) 24.27 / 13.36 / 10.57 m² = by hand | live; test_plan_to_cad_to_blender_edit_export |
| 6 | Plan without a scale (`noscale.png`) | blocking question «Масштаб на чертеже не найден. Использовать предполагаемый: 1 пиксель = 7.4 мм (по ширине дверей ≈ 90 см)?»; «ширина дома 9 метров» → rescaled, built, areas within 4 % | test_no_scale_asks_and_the_answer_builds |
| 7 | Bad picture (`bad.jpg`: blur, noise, 1.2° turn, low contrast, JPEG 55) | OpenCV still 6 walls (denoise + median background); VERIFIED LIVE: scale 9.94 (0.6 % off), 3 doors + 3 windows, areas within 2 %; without Claude: walls + doors, names not readable → «Помещение N» and the scale question | live 14.5 s; local run |
| 8 | Change a wall | «Убери стену между кухней и гостиной» → only the shared stretch removed (a longer wall is split), its door goes too, rooms merged as «Кухня-гостиная» (bigger id kept); thickness change → the wall, its neighbours' joins, its doors | test_remove_wall_between…, test_remove_only_the_shared_stretch…, test_only_what_changed_is_rebuilt |
| 9 | Change a door | «Перемести дверь на 40 см вправо» with several doors → «Какую дверь: 1) … 2) …?» (never a guess; doors in vertical walls are not offered for «вправо»); FreeCAD rebuilt 1 door, re-cut its wall: 0.15 s, Blender 2 objects 0.04 s | test_several_doors_fit_ask_never_guess; pipeline run |
| 10 | Add furniture | «Добавь диван в гостиную» → against the longest free wall, not across a door; «Поставь телевизор напротив дивана» → on the opposite wall, facing it; FreeCAD 1 box 0.18 s, Blender 1 object 0.014 s | test_furniture_against…; window run 3 |
| 11 | FreeCAD / OCCT validation | every element an Arch object (IfcType Wall / Door / Window / Slab / Roof / Column, Building Storey); every touched solid: isValid + BRepCheck + BOP per solid + volume; wall clashes by OCCT common: 0; repair path (ShapeFix / plain OCCT wall with boolean openings) | test_plan_to_cad… (inspect: all ids, valid, IfcType); first run found Arch windows failing a compound BOP check → checks per solid |
| 12 | Blender synchronisation | one object per element `<ID>_VIS`, glTF extras jarvis_id / type / level; a changed element replaces only its object; floor material → Blender only (3 floors, 10 ms, no FreeCAD) | test_plan_to_cad… (inspect .blend), window run 2 |
| 13 | Export | GLB, OBJ, .blend, FCStd, STEP (≥ 8 solids read back), STL, DXF (ezdxf read back), JSON — each file re-read by format; DWG input refused with how to get DXF/PDF | test_plan_to_cad… export block |
| 14 | Voice command changing the model | VERIFIED LIVE: «Джарвис, убери стену между кухней и гостиной» (Silero WAV) → GigaAM «Джорвис, убери стену…» → route architect (no AI) → model 6 → 5 walls, rooms Спальня + Кухня-гостиная; edit + FreeCAD + Blender 0.14 s; the answer started 0.16 s after the transcript | voice_turns (route architect), project log 15:41; test_voice_command_through_the_director |
| — | Acceptance: `plan.png` + «Джарвис, сделай из этого 3D-модель» | VERIFIED LIVE 19.1 s: window shown on «Архитектура», architect_build verified («Мастер-модель ревизии 0 сохранена, FreeCAD и 3D-превью обновлены»), answer with rooms, areas, doors, windows, OCCT result, scale source | dev core task t_92c1f3d5cb3c4b67 |
| — | Window | drop a plan → stages → 3D (three.js, draws on change only) + 2D plan with the drawing as underlay; one selection in 2D and 3D; top (orthographic) / inside (eye height, WASD) views; inspector edits; 0 console errors | `tests/ui/architect.mjs` runs 1–3; found and fixed: FreeCAD worker started with the core's venv Python under `uv run` («No module named 'PySide6'»), orbit controls pulling the walking camera back |
| — | Not done (honest) | DWG input, IFC export (no IfcOpenShell), edits made inside FreeCAD's own GUI read back into the model, curved walls, pitched roofs, multi-storey from one drawing, textures in GLB, a real furniture model library (hook only) | ADR-010 |
