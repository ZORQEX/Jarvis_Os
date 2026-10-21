# ADR-004: Communication agent (Telegram, WhatsApp, e-mail)

Date: 2026-10-21 · Status: accepted

## Flow
voice/text → router (`is_comms`) → `CommsAgent` → Gemini structured intent (`providers/gemini.py: structured`,
flash-lite, temperature 0, JSON schema, plausibility retry) → recipient resolution (provider search + local address
book, Russian case-tolerant matching) → clarification (platform / which person / text / time) or →
`message_send` (Gateway: validate → policy → **approval** → run → verify → audit) → provider → outbox row.

Scheduled: `message_send` with `due_at` stores an outbox row (`comm_jobs`) bound to the approved content digest and the
approval id. `Outbox` loop claims a due row atomically (`scheduled → sending` in one UPDATE) and runs `message_deliver`
through the Gateway (estop and rule revocation still apply). A row found in `sending` at startup becomes `unknown`
and is never resent. Bounded retry (1, 5, 15 min) only for errors where the message certainly did not leave.
More than 15 min late (JARVIS was off) → `missed`, reported, not sent.

## Providers (one contract: status, capabilities, search, send, verify)
| Service | Integration | Auth | Verification |
|---|---|---|---|
| Telegram | MTProto, user's own account (Telethon 1.45, MIT) | user's api_id/api_hash + login code (+2FA) | message re-read by id in the chat |
| WhatsApp | WhatsApp Web in JARVIS's own Chromium profile (Playwright 1.64, Apache-2.0) | QR code scanned by the user | outgoing bubble with a sent/delivered tick |
| E-mail | SMTP + IMAP of the user's mailbox | app password | message found in "Sent" by Message-ID; otherwise SMTP acceptance |

No official API exists for personal WhatsApp accounts; the Business Cloud API cannot message arbitrary personal
contacts. WhatsApp discourages automation, which is stated in the UI. No cookies or sessions are read from other
browsers; the WhatsApp profile is separate and deletable ("Отвязать").

## Security
- Secrets (Telegram session/api_hash, mail password) only in the OS keyring (`secrets.Namespaced`; a dev/test
  JARVIS_HOME gets separate entries so it can never reuse — and break — the real session).
- Gemini sees only the spoken/typed command (and, for STT, up to 15 first names the user messaged or saved).
  Never credentials, contact lists, chat or mail contents.
- Every send needs an approval bound to the exact recipients, text, subject and time (digest). Voice/typed "да" in
  the window answers it only when exactly one message confirmation is open; the bearer API cannot.
- Trusted rules (`trust_rules`) are created only on request and only by a click; revocable; scheduled jobs
  re-check the rule at delivery time.
- The agent never says "sent" unless the provider returned success; failures are recorded with the technical detail.

## Not done yet
Attachments (files/photos), replies/forwarding, Telegram native scheduled messages, Google OAuth for Gmail,
other networks (the provider contract is ready for them; nothing is shown as supported until it works).
