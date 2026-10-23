# Active milestone: 3 — connected productivity (start)

Done: milestone 1 (working JARVIS), milestone 2 (Flora on synthetic data), WorkOS window milestone 1
(workspaces, global companion, task center, palette — ADR-006). See TEST_MATRIX.md.

## Next, in order
1. **Claude live check** (the Claude Code sign-in on this computer; `JARVIS_LIVE=1 uv run pytest tests/test_live.py`):
   one chat with a tool call over MCP, one web search, one screenshot reading; record latency;
   update TEST_MATRIX statuses from MOCK/UNVERIFIED to LIVE.
2. **Gmail read-only adapter** behind `MailProvider` (OAuth installed-app flow with PKCE, scope
   `gmail.readonly`, token in keyring). Needs: a Google Cloud OAuth client and a test account. Then draft-only
   (`gmail.compose`), sending last and only with approval + Sent-folder reconciliation.
3. **XLSX edit with versioned output** (spec 27E acceptance): change a range, keep formulas, write `…_v2.xlsx`,
   show itemized diff; never touch the original.
4. **PDF drawings** in the viewer (pdftotext/pdftoppm render), same annotation sidecars.
5. **Local wake word** (openWakeWord) + PipeWire capture in the core, replacing browser push-to-talk.

## Pending user decisions
- Apply Omarchy integration (`scripts/omarchy-integrate.sh apply`): Waybar indicator + Super+H / Super+Shift+H.
- Enable the core as a user service (`scripts/install.sh --service`).

## Window: next
- Reading Telegram/WhatsApp conversations into their workspaces (needs the user's privacy decision first).
- Workspace context passed to the core with each request (today it is explicit in the phrase, e.g. messages).
- Browser adapter for the person's own browser window (back / forward / current page) — e.g. Chromium CDP on request.
- Memory panel (what JARVIS knows) once the core has a memory store.
