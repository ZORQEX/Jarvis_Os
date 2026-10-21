# ADR-005: All AI through Claude Code (Gemini removed)

Date: 2026-10-21 · Status: accepted · Supersedes the Gemini parts of ADR-002/ADR-003

## Why
The free Gemini key could not carry the assistant: 20 requests/day on the flash models, 10/day per TTS model,
503 «high demand» and 13–24 s answers at peak (all measured 2026-10-21). The user asked to remove Gemini completely.

## What runs where
| Job | How | Measured |
|---|---|---|
| Speech → text | GigaAM v3 (local, ADR-003) | ~0.65 s per phrase |
| Text → speech | Silero (local) | ~0.25 s per sentence |
| Conversation, tool use | one long-lived `claude -p --input-format stream-json` (claude-sonnet-5-5); JARVIS tools via MCP (`jarvis/mcp_bridge.py` → `/mcp/call` → Gateway) | first words ~1.0–1.5 s warm; 2.5 s full short answer; tool round ~3–5 s |
| Web research | one-off `claude -p` with WebSearch/WebFetch | ~10–20 s |
| Letters, translation, documents, screenshots | one-off `claude -p` (Read for the image file only) | ~4–5 s |
| Message commands | local reading first; else `claude -p --json-schema` (claude-haiku-5-5) | instant / ~3.7–4.3 s |
| Code & project files | Claude Code executor (unchanged) | — |

## Safety
- Every Claude process: `--restricted --strict-mcp-config --permission-mode dontAsk`, an explicit `--tools` list, an empty
  private working folder; no shell, no user/project settings or hooks.
- MCP tool calls: own random secret per core run (0600 file), loopback only; the core accepts only model-callable tools,
  at most `max_tool_rounds` per request, and runs each through the Gateway (policy → approval → run → verify → audit).
- «Стоп» sends a control interrupt (measured: stops at once; the next answer in ~1.3 s, no restart).
- Tests never call the real Claude (providers look «not installed» unless a scripted one is installed); live checks
  are opt-in (`JARVIS_LIVE=1`).

## Trade-offs
- Uses the user's Claude subscription limits instead of a Gemini quota.
- Personal use on the user's own sign-in; distributing JARVIS needs the recipient's own Claude access.
