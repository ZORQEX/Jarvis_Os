"""Local address book and Russian-friendly name matching (Иван ↔ Ивану ↔ Иван Петров)."""
from __future__ import annotations

import re

from ..db import Database, new_id, now
from .base import Contact

SELF_WORDS = {"себе", "мне", "себя", "я", "избранное", "самому себе", "сам себе"}


def norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"[^\w@.+ ]+", " ", s).strip()


def tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[\s._]+", norm(s)) if t and not t.startswith("@")] + \
           [t.lstrip("@") for t in re.split(r"\s+", norm(s)) if t.startswith("@")]


def _stem_eq(a: str, b: str) -> bool:
    """Same word up to a Russian case ending: Иван/Ивану/Ивана, Сергей/Сергею, Алина/Алине."""
    if a == b:
        return True
    n = min(len(a), len(b))
    if n < 3:
        return False
    common = 0
    while common < n and a[common] == b[common]:
        common += 1
    return common >= max(3, max(len(a), len(b)) - 2) and common >= n - 1


def _loose_eq(a: str, b: str) -> bool:
    """Two different last letters: adjectives used as names (любимая/любимое/любимые), родная/родной."""
    n = min(len(a), len(b))
    common = 0
    while common < n and a[common] == b[common]:
        common += 1
    return n >= 6 and common >= n - 2


GUESS = 0.5     # scores below 0.6 are guesses: JARVIS asks «вы имеете в виду …?»


def _guess(query: str, name: str) -> bool:
    """Close but not certain: a typo or speech-recognition slip (любимые / Любимая ❤️, Сережа / Серёжа)."""
    import difflib
    q, n = norm(query), norm(name)
    if len(q) < 3 or not n:
        return False
    if len(q) >= 4 and (q in n or n in q):
        return True
    return difflib.SequenceMatcher(None, q, n).ratio() >= 0.72 or any(
        difflib.SequenceMatcher(None, q, w).ratio() >= 0.8 for w in tokens(name) if len(w) >= 4)


def match_score(query: str, *names: str) -> float:
    """0 = no match; 1 = exact; GUESS (0.5) = only similar. Every word of the query must match a word of the name."""
    q = tokens(query)
    if not q:
        return 0.0
    best = 0.0
    for name in names:
        if not name:
            continue
        c = tokens(name)
        if not c:
            continue
        if norm(query) == norm(name):
            return 1.0
        if all(any(t == w for w in c) for t in q):
            best = max(best, 0.9 if len(q) == len(c) else 0.8)
        elif all(any(_stem_eq(t, w) for w in c) for t in q):
            best = max(best, 0.7)
        elif all(any(_stem_eq(t, w) or _loose_eq(t, w) for w in c) for t in q):
            best = max(best, 0.6)          # probable, but JARVIS asks «это нужный контакт?»
    if not best and any(n and _guess(query, n) for n in names):
        best = GUESS
    return best


def is_self(name: str) -> bool:
    return norm(name) in SELF_WORDS


class AddressBook:
    def __init__(self, db: Database):
        self.db = db

    def all(self) -> list[dict]:
        return [dict(r) for r in self.db.all("SELECT * FROM contacts ORDER BY name")]

    def save(self, name: str, email: str | None = None, phone: str | None = None, telegram: str | None = None) -> dict:
        name = name.strip()[:120]
        if not name:
            raise ValueError("Пустое имя контакта")
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
            raise ValueError("Адрес почты выглядит неверно: " + email)
        phone = re.sub(r"[^\d+]", "", phone or "") or None
        row = self.db.one("SELECT * FROM contacts WHERE lower(name)=lower(?)", (name,))
        if row:
            self.db.execute("UPDATE contacts SET email=coalesce(?,email), phone=coalesce(?,phone), "
                            "telegram=coalesce(?,telegram), updated_at=? WHERE id=?",
                            (email, phone, telegram, now(), row["id"]))
            cid = row["id"]
        else:
            cid = new_id("c_")
            self.db.execute("INSERT INTO contacts(id,name,email,phone,telegram,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                            (cid, name, email, phone, telegram, now(), now()))
        return dict(self.db.one("SELECT * FROM contacts WHERE id=?", (cid,)))

    def delete(self, cid: str) -> bool:
        return self.db.execute("DELETE FROM contacts WHERE id=?", (cid,)).rowcount > 0

    def find(self, query: str, channel: str) -> list[Contact]:
        out = []
        for r in self.all():
            sc = match_score(query, r["name"])
            if not sc:
                continue
            if channel == "email" and r["email"]:
                out.append(Contact("email", r["email"].lower(), r["name"], r["email"], "адресная книга JARVIS", sc))
            elif channel == "whatsapp" and r["phone"]:
                digits = re.sub(r"\D", "", r["phone"])
                out.append(Contact("whatsapp", "phone:" + digits, r["name"], "+" + digits, "адресная книга JARVIS", sc))
        return out


def best(cands: list[Contact]) -> list[Contact]:
    """Deduplicate and keep only the best-scoring group (an exact match beats stem matches)."""
    seen, uniq = set(), []
    for c in sorted(cands, key=lambda c: -c.score):
        if (c.channel, c.id) in seen:
            continue
        seen.add((c.channel, c.id))
        uniq.append(c)
    if not uniq:
        return []
    top = uniq[0].score
    return [c for c in uniq if c.score >= top - 0.05]
