# Active milestone: 3 — connected productivity (start)

Done: milestone 1 (working JARVIS) and milestone 2 (Flora on synthetic data). See TEST_MATRIX.md.

## Next, in order
1. **Gemini live check** (needs the client's or developer's API key via Settings or `jarvis setup-gemini`):
   run `gemini.check`, one chat with a tool call, one voice transcription, one TTS; record latency/cost;
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
