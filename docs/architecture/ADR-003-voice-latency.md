# ADR-003: Voice conversation and latency

Date: 2026-10-21 · Status: accepted

## Measured before (real requests, this key/laptop)
- STT (Gemini flash-lite): 1.2–2.0 s.
- Answer: the main model (gemini-3.8-flash) was overloaded / rate-limited (503/429) for this key; JARVIS waited up to the
  first-token timeout (12–20 s) before falling back → a chat answer took **22 s**; a letter **65 s**.
- TTS: Gemini TTS 2.3–4.7 s per short phrase, started only after the complete answer.
- Router bug: any «сколько …» was answered with system status.

## Decision
- **Fast path** (`jarvis/calc.py`): arithmetic, percentages, roots, unit conversions — local, no AI.
- **Voice mode** uses the fastest model first (`model_voice`, flash-lite, minimal thinking), a 6 s first-token cap and a
  dedicated short spoken-style instruction; compact history (6 recent turns + a digest of earlier questions).
- **Streaming speech** (`jarvis/voice/speech.py`): text deltas → sentence buffer (first segment early) → TTS → ordered
  audio chunks to the window while the model still writes. Long answers are capped in speech («Остальное — на экране»).
- **Local TTS**: Piper (separate GPL process, voice «denis», CC0 data): 0.07–0.3 s per sentence, no quota, offline.
  Voice «dmitri» was rejected: much lower intelligibility (measured by STT round-trip).
- **Acknowledgements** for long tasks are pre-synthesized at startup (no AI call).
- **Window**: local VAD in an AudioWorklet (16 kHz, 400 ms pre-roll, end-of-phrase pause 0.65 s configurable),
  hands-free conversation mode, Web Audio playback queue, barge-in (higher threshold while JARVIS talks), Esc.
- **Interrupt** («стоп», «хватит», «подожди»): stops speech and cancels answers that change nothing; actions and pending
  approvals are untouched. Emergency stop stays explicit («экстренная остановка», «стоп всё», button).
- **STT tail latency**: if transcription is not back within 2 s, an identical second request is sent (first wins).
- Connection warm-up when speech starts (`models.list`, no generation quota). Telemetry per turn in `voice_turns`.

## Measured after (from the end of the user's phrase)
- Fast-path question: first audio **1.8–2.1 s** (all of it STT).
- General question (Gemini): first token 1.8–2.6 s, **first audio 1.9–2.9 s** (server path and real Chromium window).
- Long request: acknowledgement audio right after STT (1.3–1.7 s), then the streamed answer.

## Remaining bottleneck
STT (1.1–2.0 s, cloud) plus the 0.65 s end-of-phrase pause. Next: streaming recognition while the user speaks
(Gemini Live input transcription) or local Whisper; this also removes the dependency on the free-tier quota for STT.
