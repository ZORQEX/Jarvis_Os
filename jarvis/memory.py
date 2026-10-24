"""Long-term memory: short facts about the person that JARVIS keeps across conversations and restarts — «Запомни, что
я учусь в колледже», «Моя девушка в Telegram — Любимая ❤️». Stored on this computer (SQLite); every request to the AI
carries them in one line, so the assistant knows them without being told again. «Что ты обо мне помнишь?», «Забудь,
что …» and Settings → «Память» show and remove them."""
from __future__ import annotations

import re

from .db import new_id, now
from .tools.base import Risk, Tool, ToolInputError, ToolResult, ToolSpec, Verification

MAX_FACTS = 200
LINE_CHARS = 1500


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"\w+", (s or "").casefold().replace("ё", "е")) if len(w) > 2}


def facts(db, limit: int = MAX_FACTS) -> list[dict]:
    return [dict(r) for r in db.all("SELECT id, text, source, created_at FROM memory ORDER BY created_at DESC LIMIT ?",
                                    (limit,))]


def add(db, text: str, source: str = "user") -> tuple[dict, bool]:
    """Save a fact; a fact that says nearly the same as an older one replaces it. → (fact, is_new)."""
    text = re.sub(r"\s+", " ", (text or "").strip(" .,;:—-"))[:500]
    if len(text) < 3:
        raise ToolInputError("Нечего запоминать")
    text = text[0].upper() + text[1:]
    w = _words(text)
    for f in facts(db):
        fw = _words(f["text"])
        if w and fw and len(w & fw) / max(len(w), len(fw)) >= 0.8:
            db.execute("UPDATE memory SET text=?, created_at=? WHERE id=?", (text, now(), f["id"]))
            return {**f, "text": text}, False
    fid = new_id("f_")
    db.execute("INSERT INTO memory(id, text, source, created_at) VALUES(?,?,?,?)", (fid, text, source, now()))
    extra = db.all("SELECT id FROM memory ORDER BY created_at DESC LIMIT -1 OFFSET ?", (MAX_FACTS,))
    for r in extra:
        db.execute("DELETE FROM memory WHERE id=?", (r["id"],))
    return {"id": fid, "text": text, "source": source}, True


def forget(db, query: str = "", fid: str = "") -> list[str]:
    """Remove the fact with this id, or the facts that best match the words of `query`."""
    if fid:
        row = db.one("SELECT text FROM memory WHERE id=?", (fid,))
        db.execute("DELETE FROM memory WHERE id=?", (fid,))
        return [row["text"]] if row else []
    q = _words(query)
    if not q:
        return []
    scored = []
    for f in facts(db):
        fw = _words(f["text"])
        hit = len(q & fw) + sum(1 for a in q for b in fw if a != b and len(a) >= 5 and (a.startswith(b[:5])))
        if hit:
            scored.append((hit, f))
    if not scored:
        return []
    best = max(s for s, _ in scored)
    gone = [f for s, f in scored if s == best]
    for f in gone:
        db.execute("DELETE FROM memory WHERE id=?", (f["id"],))
    return [f["text"] for f in gone]


def memory_line(db) -> str:
    """«[Память: …]» for the AI: the newest facts first, within a bounded size."""
    out, size = [], 0
    for f in facts(db):
        if size + len(f["text"]) > LINE_CHARS:
            break
        out.append(f["text"])
        size += len(f["text"]) + 2
    return f"[Память о пользователе: {'; '.join(out)}]\n" if out else ""


class MemorySave(Tool):
    def __init__(self, db):
        self.db = db
        self.spec = ToolSpec(
            id="memory_save", title="Запомнить",
            description="Remember a lasting fact about the user for all future conversations (name, people close to "
                        "them and how they are called in messengers, study / work, where their things are, "
                        "preferences). One short sentence in Russian, in the third person or as the user said it.",
            params={"type": "object", "required": ["text"], "properties": {
                "text": {"type": "string", "minLength": 3, "maxLength": 500}}},
            risk=Risk.LOW, side_effects="local", timeout_s=5, model_callable=True,
            verification="Факт сохранён в памяти JARVIS (прочитан обратно)")

    def describe(self, args):
        return f"Запомнить: «{args['text'][:120]}»"

    async def run(self, args, ctx):
        f, new = add(self.db, args["text"], "user" if ctx.conversation.get("source") != "ai" else "ai")
        return ToolResult({"fact": f, "new": new}, ("Запомнил: " if new else "Обновил в памяти: ") + f["text"])

    async def verify(self, args, result, ctx):
        ok = self.db.one("SELECT 1 FROM memory WHERE id=?", (result.data["fact"]["id"],))
        return Verification("verified" if ok else "failed", self.spec.verification)


class MemoryList(Tool):
    def __init__(self, db):
        self.db = db
        self.spec = ToolSpec(
            id="memory_list", title="Что я помню", description="List what JARVIS remembers about the user.",
            params={"type": "object", "properties": {}}, risk=Risk.LOW, side_effects="none", timeout_s=5,
            model_callable=True, verification="Прочитано из памяти JARVIS")

    async def run(self, args, ctx):
        fs = facts(self.db)
        if not fs:
            return ToolResult({"facts": []}, "Пока ничего не помню. Скажите, например: «Запомни, что я учусь в колледже».")
        return ToolResult({"facts": fs}, f"Помню ({len(fs)}):\n" + "\n".join(f"- {f['text']}" for f in fs[:30]))

    async def verify(self, args, result, ctx):
        return Verification("verified", self.spec.verification)


class MemoryForget(Tool):
    def __init__(self, db):
        self.db = db
        self.spec = ToolSpec(
            id="memory_forget", title="Забыть",
            description="Forget remembered facts that match the words given (what the user asked to forget).",
            params={"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string", "minLength": 2, "maxLength": 300}}},
            risk=Risk.LOW, side_effects="local", timeout_s=5, model_callable=True,
            verification="Факт удалён из памяти (проверено)")

    def describe(self, args):
        return f"Забыть: «{args['query'][:120]}»"

    async def run(self, args, ctx):
        gone = forget(self.db, args["query"])
        if not gone:
            return ToolResult({"forgotten": []}, "Такого в памяти нет.")
        return ToolResult({"forgotten": gone}, "Забыл: " + "; ".join(gone))

    async def verify(self, args, result, ctx):
        left = [t for t in result.data["forgotten"] if self.db.one("SELECT 1 FROM memory WHERE text=?", (t,))]
        return Verification("failed" if left else "verified", self.spec.verification)
