# ADR-009: Claude Code as JARVIS's agent — routing, reliability, plain words

Date: 2026-10-25 · Status: accepted · Builds on ADR-005 (all AI through Claude Code) and ADR-002 (agency)

## Context
The request: Claude Code (the CLI already signed in on this computer) as the main intelligent agent behind the voice,
reusing the core, the voice pipeline, the window and the sign-in, with no second system. The audit found it already
was the brain (ADR-005): one long-lived `claude -p --input-format stream-json` conversation, JARVIS's tools over MCP →
Gateway, Claude Code jobs through `computer_task`. What was missing came from the real JARVIS's own journal and from
live voice runs:
- the CLI's English errors reached the person: «⚠ Шаг «ответ» не выполнен: Claude: You've hit your session limit ·
  resets 4am (Asia/Almaty)» (twice in the real journal), and the voice said a generic «Не удалось получить ответ»;
- an overloaded model meant silent API retries and then a failure, with no fallback model;
- while Claude worked, the task showed only «Claude · ответ…», nothing about what it was doing;
- a hung CLI process (no output at all) kept a request «thinking» forever (the user chose no time limit on work);
- a quick local command took a compound request and dropped the rest: «узнай, сколько места на диске, и запиши это в
  заметку» answered the disk part, never wrote the note and reported success (live, 2026-10-25); the same for
  «сделай скриншот и опиши…» and «погромче и включи музыку»;
- «Ответь одним словом: …» was planned as a message draft, so the answer got a line about a draft.

## Decision
One path, no second assistant:

```
voice → STT (GigaAM / Parakeet) → director → router
   the whole request is one quick command → local tool (no AI, ~0.3 s)
   anything else → agency → the Claude Code conversation (warm, stream-json)
        ├─ JARVIS tools over MCP → Gateway (policy → approval → run → verify → audit)
        ├─ computer_task → a Claude Code job (files, terminal, code), approval first
        ├─ web_search (local SearXNG), Hermes / OpenClaw (ADR-008) — tools like any other
        └─ streamed text → the screen + speech sentence by sentence (Supertonic)
```

1. **Routing.** A local route is taken only when it is the whole request. `router.more_to_do` («… и запиши …»,
   «… а потом открой …», at least two words before it) sends the request to Claude, which has the same tools
   (`disk_usage`, `screenshot`, `create_note`, …) and does every part in one turn. «Найди и открой файл» is one action.
2. **Plain words.** `providers/claude_chat.trouble()` turns the CLI's errors into what the person reads and hears:
   usage limits with the reset time in this computer's zone («снова заработает в 4:00» / «вернётся в 4 утра»), weekly
   and paid limits, sign-in, model unavailable, overload, network, a crash or a hang. An unknown error is shown as it
   came — a reason is never invented. The CLI's own words go to the audit journal (`ai.failed`). A limit is remembered
   until it resets (health RATE_LIMITED, `limited_until` in the provider card) and clears itself. The same wording is
   used for one-off steps, structured parsing and the Claude Code executor (limits, sign-in).
3. **Fallback model.** Every `claude -p` gets `--fallback-model` = the other configured models (`claude_fallback`,
   on by default); the CLI tries the chosen model again on the next question.
4. **Progress.** CLI events become the task's progress line (window, companion): tool calls (`progress_line`:
   «Ищу в интернете: …», «Читаю страницу …», the JARVIS tool's title — never arguments or terminal output),
   `api_retry` («Claude отвечает с задержкой — повторяю запрос (2 из 10)»), `model_fallback` («Основная модель
   перегружена — отвечает запасная»). By voice, at the third retry JARVIS says once «Claude отвечает с задержкой, жду.»
5. **Hang watchdog.** No output for `STALL_S` = 180 s while no JARVIS tool runs for the answer and no API retry was
   announced = hung: the process is ended; if nothing was said or done yet, the question goes once to a fresh process,
   otherwise the person hears «Claude завис. Повторите, пожалуйста, я запущу его заново.» Work itself still has no time
   limit: a tool call, the person's approval or a Claude Code job may take an hour.
6. **Planner.** «Ответь одним словом / коротко / честно …» is a question, not a draft.

Unchanged: context (one warm session per conversation; after 40 turns or a restart a fresh one gets the last 6
messages and a digest), interruptions («стоп» = control interrupt, barge-in = speech only), approvals (Gateway; «да» /
«нет» by voice for voice-approvable tools), sign-in (the CLI's own login, no API keys), safety flags of ADR-005.

## Configuration (`config.toml`, any section)
| Key | Default | What |
|---|---|---|
| `claude_chat_model` | `claude-sonnet-5-5` | typed conversation |
| `claude_voice_model` | `claude-haiku-5-5` | spoken turns (the same conversation, model switched) |
| `claude_fast_model` | `claude-haiku-5-5` | parsing message commands, planning |
| `claude_fallback` | `true` | `--fallback-model` = the other models above |
| `claude_timeout_s` | `0` | limit for one answer, 0 = none (the hang watchdog still applies) |
| `max_tool_rounds` | `0` | tool calls in one answer, 0 = no cap |
| `claude_model` | `""` | Claude Code jobs (`""` = the CLI's default) |

Sign-in once: `claude` in a terminal, then `/login`. Settings → the Claude card shows the session, the models, the
last error in plain words and the limit's reset time.

## Evidence
Live, dev core (own JARVIS_HOME, port 8805, real Claude; the voice is a Supertonic F1 WAV played as the companion's
microphone, `tests/ui/voice-mic.mjs`):
- «объясни в двух предложениях, почему небо голубое» → recognition 0.52 s, first words 2.2 s, spoken answer.
- «а почему тогда закат красный?» → answered in the context of the previous answer, first words 1.1 s.
- «найди в интернете, в каком году построили Эйфелеву башню» → web_search 1.45 s + Claude 1.56 s, sources [n].
- «узнай, сколько места на диске, и запиши это в заметку» → Claude: disk_usage → create_note → approval asked aloud →
  «Джарвис, да» → note file with the real figures → «Записал…» (before the fix: the disk part only).
- «расскажи подробно историю интернета» … «Джарвис, стоп» → speech stopped in the same second.
- main model set to one that does not exist → the answer came from the fallback model with «Основная модель
  перегружена — отвечает запасная»; fallback off → «Выбранная модель Claude (claude-sonnet-0-0) сейчас недоступна…».

Mock: `tests/test_claude_agent.py` — limit wording and reset times, the fallback flag on all three kinds of call, CLI
events → progress, a hang vs a long tool call or an announced API retry, compound requests, the spoken failure.

## Trade-offs
- The fallback model answers less thoroughly; the progress line says so and the next question tries the chosen one.
- A compound request now costs one Claude turn (~2–4 s) instead of a fast but wrong half-answer.
- 180 s of complete silence is the hang threshold: API back-off is announced and tool calls are excluded, so normal
  work never comes near it; a real hang costs at most three minutes instead of forever.
