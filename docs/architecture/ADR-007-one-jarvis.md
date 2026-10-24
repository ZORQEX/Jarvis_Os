# ADR-007 — One JARVIS: the friend's projects inside JARVIS's own architecture (2026-10-25)

## Context
Two projects of morph-rq were brought into JARVIS:
- **alina-fashion-ai** (`fashion/`, git subtree) — the orders business: factories, customers, markups, deposits,
  documents. It was shown in the «Заказы» workspace as a *second application*: its own top bar and navigation, its own
  assistant («Спросите Fashion AI»: a separate Claude Code process, its own prompt, its own dialog), its own tour and AI
  and voice settings, a light theme. Spoken order phrases were forwarded to that assistant.
- **jarvis-os** — the friend's own JARVIS. Its capabilities were ported earlier (computer, desk, calendar, phone,
  proactive, home, meetings); its **research engine** (Morphic's pipeline on a local SearXNG, answers by Gemini) — the
  fast internet search the owner wants — was not. JARVIS's web questions went to Claude's own WebSearch: a model call
  first, then its searches.

The owner asked for one JARVIS: one assistant, one memory, one task list, the friend's useful functions inside it,
Claude Code the main executor for development, fast search for simple lookups, the existing UI kept.

## Decision
1. **Fast search is a JARVIS capability, not a second assistant.** `jarvis/research.py` ports the engine's search
   side to Python in the core (SearXNG child, engines raced, failing engines rested, ranking, cache, SSRF-safe page
   reading, prompt-injection filter, invented-link removal). It is the `web_search` tool in the Gateway (validation,
   audit, verification like any tool). The answer model is JARVIS's own Claude (ADR-005: Claude only), not Gemini.
   The AI Director makes each research step that tool and adds one answer step; results stay in the conversation
   (`last_research`) and are given to Claude Code as context when the next work is code. Search down → Claude's
   WebSearch, recorded in the decision. Prices/rates (`market_quote`) need neither search nor AI.
2. **«Заказы» is a business module with no assistant of its own.** It keeps its process (its own dependencies), data,
   rules and proposals. JARVIS asks it first (its rules answer in ms); what the rules cannot answer is handed back
   (`handoff`) and answered by JARVIS's conversation with the module's typed tools (`orders` → `/api/jarvis/ai/tool`).
   Changes are still proposals; the owner confirms on a card in JARVIS's chat (UI click only) or by «да» for the
   changes the module allows by voice. Without JARVIS's AI the module's full rules answer, as its own fallback did.
3. **The module's pages are JARVIS's.** In JARVIS's frame (`Sec-Fetch-Dest: iframe`) the module drops its chrome and
   takes JARVIS's colours; its sections are tabs in JARVIS's workspace header. Opened directly, it is unchanged — so
   upstream updates (`git subtree pull`) still merge; every change in `fashion/` is small and marked «for JARVIS».

## Consequences
- One assistant to talk to; one place for approvals (JARVIS's chat); one task list with real steps (search → answer,
  search → Claude Code → tests, rules → card).
- Speed: a simple search does not start a model before results exist (measured in TEST_MATRIX); prices answer in
  ~0.4 s without a model.
- SearXNG costs ~140 MB of memory while JARVIS runs; only DuckDuckGo and Brave answer from this laptop today, the
  other engines refuse it (they stay as a resting fallback).
- Not ported from the research engine: its Node/TypeScript process, Gemini, its research card UI (JARVIS's chat
  shows the answer with numbered sources).
