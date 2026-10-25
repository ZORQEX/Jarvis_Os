"""Choosing how to do something, in two stages (ported from morph-rq/jarvis-os).

Stage 1 is JARVIS's own router (jarvis/router.py): known local actions resolve to a tool call without any model.
Stage 2, here, is for everything else that asks «what can do this?». It retrieves capabilities whose metadata match
the request (an inverted index over names, aliases and descriptions, with Russian intent words mapped to the English
the agents' skills are written in), drops the ones that can't run (disabled, failed, duplicates, not installed), and
ranks the rest by relevance × method × health × reliability × authentication × latency.

Method order, when the evidence is equal: official API, native integration, verified skill, browser, desktop,
screen/vision, a delegated agent, building something new. It is a weight, not a rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .registry import METHOD_WEIGHT, Capability, Registry, stem, terms, words

HEALTH = {"ok": 1.0, "degraded": 0.6, "disconnected": 0.35}
AUTH = {"none": 1.0, "connected": 1.0, "needs_auth": 0.75, "not_configured": 0.5}
LATENCY = {"instant": 1.0, "fast": 0.95, "slow": 0.85}

STOP = {"the", "a", "an", "to", "of", "and", "or", "in", "on", "for", "my", "me", "with", "from", "by", "is", "it",
        "this", "that", "these", "those", "some", "any", "all", "do", "does", "how", "what", "be", "are", "can", "you",
        "please", "jarvis", "want", "i", "use", "tool", "add", "что", "мне", "мой", "мою", "это", "и", "в", "на", "с",
        "по", "для", "пожалуйста", "джарвис", "можешь", "сделай", "сделать", "нужно", "надо", "хочу", "как", "мои"}

# Russian words → the English the upstream skills describe themselves with (stems are matched too).
INTENT_WORDS = {
    "почт": "email mail gmail inbox", "письм": "email mail", "сообщен": "message send chat", "напиш": "write message",
    "телеграм": "telegram", "вотсап": "whatsapp", "ватсап": "whatsapp", "дискорд": "discord", "слак": "slack",
    "сигнал": "signal", "календар": "calendar event", "встреч": "meeting calendar", "погод": "weather forecast",
    "музык": "music spotify player", "песн": "song music", "видео": "video youtube", "ютуб": "youtube video",
    "картинк": "image picture generate", "изображен": "image picture", "фото": "photo image", "рисун": "image draw",
    "поиск": "search web", "найд": "search find", "интернет": "web internet", "новост": "news",
    "файл": "file files", "документ": "document docs", "таблиц": "spreadsheet sheet excel", "презентац": "slides presentation",
    "pdf": "pdf document", "перевед": "translate", "перевод": "translate translation", "заметк": "note notes notion",
    "задач": "task todo", "напомн": "reminder", "github": "github git", "гитхаб": "github git", "код": "code",
    "сайт": "website web browser", "браузер": "browser web", "карт": "maps map directions", "маршрут": "route directions",
    "звон": "call phone voice", "голос": "voice speech tts", "озвуч": "tts speech", "расшифр": "transcribe",
    "крипт": "crypto bitcoin", "курс": "exchange rate price", "цен": "price", "акци": "stock market",
    "заказ": "order orders", "фабрик": "factory supplier", "погод": "weather", "умн": "smart home",
    "свет": "light lights home", "дом": "home house", "свод": "summary summarize", "кратк": "summary",
    "обнов": "update", "установ": "install", "навык": "skill skills", "автоматиз": "automation cron schedule",
    "расписан": "schedule cron", "трелло": "trello", "ноушн": "notion", "обсидиан": "obsidian",
}


def expand(text: str) -> list[str]:
    """Every term the request can match: its own stems and the English words its Russian words map to."""
    return list(dict.fromkeys(t for group in groups(text) for t in group))


def groups(text: str) -> list[list[str]]:
    """One group per word of the request: its stem, then its translations. A word counts once however it matched."""
    out: list[list[str]] = []
    for w in words(text):
        if w in STOP:
            continue
        g = [stem(w)]
        for k, v in INTENT_WORDS.items():
            if w.startswith(k):
                g.extend(stem(x) for x in v.split())
        out.append(list(dict.fromkeys(g)))
    return out


@dataclass
class Choice:
    cap: Capability
    score: float
    relevance: float
    reasons: list[str] = field(default_factory=list)

    def public(self) -> dict:
        c = self.cap
        return {"id": c.id, "name": c.name, "title": c.title, "provider": c.provider, "kind": c.kind,
                "method": c.method, "score": round(self.score, 3), "health": c.health, "auth": c.auth,
                "state": c.state, "latency": c.latency, "risk": c.risk, "reasons": self.reasons,
                "description": c.description[:240], "providers": c.providers}


def relevance(cap: Capability, query: list[list[str]]) -> float:
    if not query:
        return 0.0
    name = set(terms(cap.name)) | set(terms(cap.title))
    alias = set(w for a in cap.aliases for w in terms(a))
    desc = set(terms(cap.description[:600]))
    # A hit on a long name covers less of it: «issues» names github's subject, not improve_issues's.
    name_w = 3 * min(1.0, 1.5 / max(1, len(set(terms(cap.name)))))
    s = 0.0
    for group in query:
        best = 0.0
        for w in group:
            if w in name:
                best = max(best, name_w)
            elif w in alias:
                best = max(best, 2.5)
            elif w in desc:
                best = max(best, 1.0)
            elif len(w) >= 5 and any(n.startswith(w[:5]) for n in name | desc):
                best = max(best, 0.6)
        s += best
    # A request names its subject in a few words; extra words shouldn't sink a match.
    return s / (3 * min(len(query), 3))


def rank(registry: Registry, text: str, *, k: int = 5, kinds: tuple[str, ...] = (),
         include_unusable: bool = False) -> list[Choice]:
    query = groups(text)
    out = []
    for cid in registry.candidates(expand(text)):
        cap = registry.caps[cid]
        if kinds and cap.kind not in kinds:
            continue
        if not include_unusable and not cap.usable:
            continue
        rel = relevance(cap, query)
        if rel <= 0.05:
            continue
        reasons = [f"совпадение {rel:.2f}", f"способ {cap.method}"]
        h = HEALTH.get(cap.health, 0.2)
        if cap.health != "ok":
            reasons.append(f"состояние {cap.health}")
        a = AUTH.get(cap.auth, 0.5)
        if cap.auth in ("needs_auth", "not_configured"):
            reasons.append("нужно подключить")
        r = 0.6 + 0.8 * cap.reliability       # 0.6..1.4; unknown 1.0
        if cap.ok + cap.failed >= 3:
            reasons.append(f"успешно {cap.ok} из {cap.ok + cap.failed}")
        score = rel * METHOD_WEIGHT.get(cap.method, 0.5) * h * a * r * LATENCY.get(cap.latency, 0.9)
        if cap.verified:
            score *= 1.05
        out.append(Choice(cap, score, rel, reasons))
    out.sort(key=lambda c: -c.score)
    return out[:k]


def plan(registry: Registry, text: str) -> dict:
    """What JARVIS would do for a request: use, connect, enable, or search further (ClawHub / build)."""
    best = rank(registry, text, k=6)
    if best and best[0].relevance >= 0.3:
        top = best[0]
        action = "connect" if top.cap.auth in ("needs_auth", "not_configured") else "use"
        return {"action": action, "choice": top.public(), "alternatives": [c.public() for c in best[1:4]]}
    near = [c for c in rank(registry, text, k=4, include_unusable=True) if not c.cap.duplicate_of]
    if near and near[0].relevance >= 0.3:
        return {"action": "enable", "choice": near[0].public(), "alternatives": [c.public() for c in near[1:]]}
    return {"action": "search", "choice": None, "alternatives": [c.public() for c in best[:3]]}
