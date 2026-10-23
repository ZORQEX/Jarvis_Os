# Test matrix — milestone 1 + Flora (2026-10-20)

Environment: Omarchy 3.8.3, Hyprland 0.56.2, Chromium 154, Python 3.14.8. Tests: `uv run pytest -q` → 266 passed, 6 skipped (live, opt-in); `JARVIS_LIVE=1 uv run pytest tests/test_live.py` → 6 passed (2026-10-22).

| Requirement | Status | Evidence |
|---|---|---|
| Real window on Omarchy, Russian UI, companion states from runtime state | VERIFIED LOCAL | grim captures of the real window (session report); `tests/ui/drive.mjs smoke` 0 console errors |
| Russian command → local tool → verified result, no AI call | VERIFIED LOCAL | typed into real window: «Джарвис, состояние системы» 38 ms; bench p50 2–17 ms |
| App launch verified by new Hyprland window | VERIFIED LOCAL | «открой калькулятор» → `org.gnome.Calculator` window detected, 274 ms |
| Volume set + read-back | VERIFIED LOCAL | wpctl read-back 60% |
| File search scoped; path traversal / symlink escape refused; executables not opened | VERIFIED LOCAL | test_core.py |
| Moderate/high risk require approval; digest-bound; edit-before-approve executes edited args only | VERIFIED LOCAL | test_core.py, test_flora.py |
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
| Flora triage: marketing «СРОЧНО» excluded, injection quarantined, decisions with source quotes | VERIFIED MOCK | test_triage_excludes_marketing_and_injection |
| Quote compare: line totals recomputed, wrong total formula detected (F10), EUR quote «not comparable» | VERIFIED MOCK | test_numbers_are_recomputed_not_invented |
| Contradiction (W-3P height 2100 vs 2000) shown with both sources, unresolved | VERIFIED MOCK | test_cost_claim_and_contradiction_detected |
| Draft from evidence; send needs high-risk approval; edit after request invalidates; no duplicate send | VERIFIED MOCK | test_send_*, test_edit_after_approval_request_invalidates |
| Drawing revisions B/C compare, zoom/pan, annotations as sidecar (sources unchanged) | VERIFIED MOCK | test_sources_never_modified…, UI run flora-05 (3 shapes saved) |
| Gmail / Calendar / Drive | NOT IMPLEMENTED | provider interface in `jarvis/flora/mail.py` |
| Waybar indicator | VERIFIED LOCAL (temporary bar instance, copy of user config) | not installed into the user's bar |
| Hyprland bindings Super+H / Super+Shift+H | IMPLEMENTED UNVERIFIED | `config/omarchy/hypr-jarvis.conf`, applied only on request |
| systemd user service | IMPLEMENTED UNVERIFIED | `scripts/install.sh --service` |

## WorkOS window (2026-10-23)
Tests: `uv run pytest -q` → 289 passed, 6 skipped; `node --test tests/ui/unit/` → 37 passed;
`node tests/ui/workos.mjs` against an isolated core → 21/21 checks, 0 console errors (with `LIVE=1`, one real Claude answer).

| Requirement | Status | Evidence |
|---|---|---|
| Shell: sidebar with 9 workspaces, top bar with real status, activity panel, workspace persisted | VERIFIED LOCAL | workos.mjs «sidebar», «persistence» |
| Global companion: one instance, survives navigation, docked top-left by default | VERIFIED LOCAL | workos.mjs (element identity kept across 5 workspaces) |
| Companion drag: clamped to window, snaps to edges, position restored after reload and resize, re-docks near its slot | VERIFIED LOCAL | workos.mjs «drag, clamp, persist» |
| Companion expand / pin / hide until next event / Ctrl+J restore; folds on outside click | VERIFIED LOCAL | workos.mjs |
| Companion state machine (IDLE, LISTENING, THINKING, PLANNING, EXECUTING, WAITING_CONFIRMATION, SPEAKING, SUCCESS, ERROR, CANCELLED, PAUSED, OFFLINE, STANDBY) from real state only | VERIFIED LOCAL | unit tests (agent-state); LIVE run: THINKING → EXECUTING → SUCCESS |
| Chat: local command result card; stop on a running turn; retry on failed/cancelled | VERIFIED LOCAL | workos.mjs «chat», screenshots |
| Streaming: assistant.delta rendered incrementally; companion shows «Claude отвечает…» | VERIFIED LIVE / LOCAL | LIVE run (2,5 s answer); renderer check with core events |
| Confirmation: companion card «Разрешить / Изменить / Отклонить», side panel list, top bar; approve runs the real tool | VERIFIED LOCAL | workos.mjs «confirmation» (note written, verified) |
| Cancel: Task Center button and Esc stop a task; timeline records it | VERIFIED LOCAL | workos.mjs «cancel», «Esc stops» |
| Task Center: active/all/problems, plan ✓●○, execution timeline from persisted step and approval times | VERIFIED LOCAL | workos.mjs, unit tests (timeline) |
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
| «Где посмотреть мои задачи?» → Task Center shown | VERIFIED LIVE | live run |
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

## Performance (this laptop)
- Core cold start to /health: 223 ms. Launcher returns in ~280 ms. Page ready ~100 ms.
- Core idle: 0.0% CPU, ~39 MB PSS. Window (Chromium, 10 processes): ~380 MB PSS, idle 1.9% of one core
  (was 25% before removing infinite idle animations).
- Local commands (p50/p95 ms, 40 runs): system 16.6/26.8, windows 8.3/10.0, file search 4.3/5.5,
  time 1.9/2.1, Flora triage 14.2/20.7, quote compare 12.1/15.5.

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
| UI scenarios | VERIFIED LOCAL | drive.mjs smoke, approval-deny, view, flora, comms, commsqr, two, voiceui, agency → 0 console errors |

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

