# ADR-002: AI agency — one JARVIS, several specialists

Date: 2026-10-21 · Status: accepted

## Decision
- **Provider contract** (`jarvis/providers/base.py`): `AIProvider` with explicit capabilities, modalities, health
  (CONNECTED, DEGRADED, RATE_LIMITED, AUTH_ERROR, OFFLINE, DISABLED, NOT_CONFIGURED), `generate()` (normalized step)
  and, where supported, `run_tool_loop()` (function calling through the JARVIS gateway).
- **Adapters**: Gemini (google-genai; Google Search grounding for research), OpenAI (openai SDK, Responses API,
  `store=False`), Claude Code (local `claude` CLI headless, `--restricted`, no MCP, no session persistence,
  bounded turns/budget/time; inspect = Read/Glob/Grep; change = +Edit/Write/Bash with a deny list).
- **Registry/manager** (`providers/registry.py`): one place for model IDs (from config), per-provider user settings
  (enabled, fallback eligibility, max data sensitivity — HIGHLY_SENSITIVE can never be allowed), usage ledger.
- **Agency** (`jarvis/agency/`): deterministic planner (clause split + action classification, domain carry-over),
  optional LLM planner for uncertain compound requests (schema-validated, can only raise risk/sensitivity, cannot add a
  send), routing with hard constraints then score, controlled fallback, explicit override without silent
  substitution, per-step minimal context, verification, provenance in the final answer.
- **Every specialist step is a gateway tool** (`agent_generate`, `claude_code_inspect`, `claude_code_change`), so
  policy, approvals, emergency stop, timeouts, cancellation and audit apply. Prompt context is passed in memory only.

## Why the deterministic planner first
Most requests are unambiguous; a planning model call for each would add latency and cost. The model planner is
used only when the deterministic plan is uncertain and a planner-capable provider is permitted.

## Claude Code distinction
JARVIS uses the installed Claude Code CLI with the user's own sign-in, not a generic Anthropic API call. Distributing
JARVIS to a client requires the client's own Anthropic credentials.
