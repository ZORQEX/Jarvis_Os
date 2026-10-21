"""Communication agent: one abstraction over messaging services (Telegram, WhatsApp, e-mail…).

Gemini understands the request; providers perform the real action; every send goes through the
Gateway (validation → policy → approval → run → verify → audit). Nothing here pretends to send.
"""
