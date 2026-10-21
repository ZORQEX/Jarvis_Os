# Test matrix — milestone 1 + Flora (2026-10-20)

Environment: Omarchy 3.8.3, Hyprland 0.56.2, Chromium 154, Python 3.14.8. Tests: `uv run pytest -q` → 137 passed, 6 skipped (live, opt-in); `JARVIS_LIVE=1 uv run pytest tests/test_live.py` → 6 passed (2026-10-21).

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
