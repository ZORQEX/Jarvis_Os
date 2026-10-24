"""Inside JARVIS's window (added for JARVIS, 2026-10-25): a page loaded in JARVIS's «Заказы» frame is part of JARVIS.

JARVIS's colours and type (static/jarvis.css); no second app around the page — the top bar, the «Fashion AI» bar,
the guide, the tab bar and the footer are JARVIS's own (its sections are tabs in JARVIS's workspace header, its
assistant is JARVIS); the assistant page leads back to the orders. The same pages opened directly (not in a frame)
stay the app as it was. Embedded = FASHION_JARVIS_ORIGIN is set and the browser says the page is a frame.
"""
from __future__ import annotations

import re

from app.config import settings

REDIRECT = {"/assistant": "/board", "/settings/ai": "/settings", "/settings/voice": "/settings"}


def embedded(request) -> bool:
    return bool(settings.jarvis_origin.strip()) and request.headers.get("sec-fetch-dest") == "iframe"


def embed_html(text: str) -> str:
    text = re.sub(r"<html\b([^>]*)>", lambda m: f"<html{m.group(1)} data-jarvis>", text, count=1)
    text = text.replace("</head>", '<link rel="stylesheet" href="/static/jarvis.css?v=2">\n</head>', 1)

    def body(m):
        attrs = m.group(1)
        if re.search(r'\bclass="', attrs):
            return "<body" + re.sub(r'\bclass="', 'class="in-jarvis ', attrs, count=1) + ">"
        return f'<body class="in-jarvis"{attrs}>'
    return re.sub(r"<body\b([^>]*)>", body, text, count=1)
