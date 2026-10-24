"""This app inside JARVIS (its «Заказы» workspace): JARVIS hears the person and asks here whether a phrase is about
orders; if so it sends the phrase to the same assistant (``/api/agent/message``) and speaks the short answer itself.

- ``POST /api/jarvis/claims {text, conversation?}`` → ``{claim: "strong"|"weak"|null, why, normalized}``.
  *strong*: a customer code/alias, brand, season or factory-money word, or an answer to this conversation's pending
  proposal/question — JARVIS sends it here. *weak*: generic words («заказ», «клиент») — JARVIS sends it here only when
  it has no command of its own for the phrase (so «открой папку с заказами» stays a file search).
- ``GET /api/jarvis/state`` → what is loaded (seasons, counts), for JARVIS's status line and settings.

Loopback only (TrustedHost), and cross-site writes are refused, like every other route of the app.
"""
from __future__ import annotations

import json
import re

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

STRONG = re.compile(r"(?<!\w)(фабрик\w*|предоплат\w*|наценк\w*|депозит\w*|factory|prepayment|deposit|markup)",
                    re.I)
WEAK = re.compile(r"(?<!\w)(заказ(?:ы|а|ов|ам|ами|ах|е|у)?|бренд\w*|коллекци\w*|сезон\w*|клиент\w*|позици\w*)(?!\w)",
                  re.I)
ANSWER = re.compile(r"^\s*(да|нет|не надо|сохрани\w*|подтвер\w*|отмен\w*|давай|верно|правильно|оставь|ок|окей)\b",
                    re.I)


def vocabulary(db: Session) -> dict:
    from app.models import Brand, Customer, CustomerAlias, Season
    return {"codes": [c.code for c in db.scalars(select(Customer)) if c.code],
            "aliases": [a.alias for a in db.scalars(select(CustomerAlias)) if a.alias],
            "names": [c.name for c in db.scalars(select(Customer)) if c.name and len(c.name) >= 3],
            "brands": [b.name for b in db.scalars(select(Brand)) if b.name],
            "seasons": [s.name for s in db.scalars(select(Season)) if s.name]}


def _word(term: str, text: str, case: bool = False) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text, 0 if case else re.I) is not None


def pending_in(db: Session, conversation: str) -> dict | None:
    """The newest assistant message of the conversation, when it waits for an answer (a proposal or a question)."""
    from app.models import AgentMessage, AgentProposal
    if not re.fullmatch(r"[a-f0-9]{16}", conversation or ""):
        return None
    row = db.scalar(select(AgentMessage).where(AgentMessage.conversation == conversation, AgentMessage.role == "assistant")
                    .order_by(AgentMessage.id.desc()).limit(1))
    if row is None:
        return None
    data = json.loads(row.data_json or "{}")
    proposal = data.get("proposal") or {}
    if proposal.get("id"):
        current = db.scalar(select(AgentProposal).where(AgentProposal.public_id == proposal["id"]))
        if current is not None and current.status == "pending":
            return {"proposal": proposal["id"]}
    if data.get("question"):
        return {"question": data["question"]}
    return None


def claims(db: Session, text: str, conversation: str = "") -> dict:
    from app.voice import normalize_transcript
    vocab = vocabulary(db)
    codes = vocab["codes"] + vocab["aliases"]
    normalized = normalize_transcript(text or "", codes, vocab["brands"])
    pending = pending_in(db, conversation)
    if pending and ANSWER.search(normalized):
        return {"claim": "strong", "why": "ответ на предложение или вопрос ассистента", "normalized": normalized}
    if pending and pending.get("question"):
        options = [str(o) for o in pending["question"].get("options") or []]
        if any(o and o.casefold() == normalized.strip(" .!").casefold() for o in options):
            return {"claim": "strong", "why": "выбран вариант ответа", "normalized": normalized}
    for code in sorted({c.upper() for c in codes}, key=len, reverse=True):
        if len(code) >= 2 and _word(code, normalized, case=True):
            return {"claim": "strong", "why": f"клиент {code}", "normalized": normalized}
    for kind, terms in (("бренд", vocab["brands"]), ("сезон", vocab["seasons"]), ("клиент", vocab["names"])):
        for term in sorted(terms, key=len, reverse=True):
            if _word(term, normalized):
                return {"claim": "strong", "why": f"{kind} {term}", "normalized": normalized}
    if STRONG.search(normalized):
        return {"claim": "strong", "why": STRONG.search(normalized).group(1).lower(), "normalized": normalized}
    if WEAK.search(normalized):
        return {"claim": "weak", "why": WEAK.search(normalized).group(1).lower(), "normalized": normalized}
    return {"claim": None, "why": "", "normalized": normalized}


def install_jarvis(app, get_db):
    @app.post("/api/jarvis/claims")
    async def jarvis_claims(request: Request, db: Session = Depends(get_db)):
        body = await request.json()
        return JSONResponse(claims(db, str(body.get("text") or "")[:2000], str(body.get("conversation") or "")),
                            headers={"Cache-Control": "no-store"})

    # JARVIS's own AI answers what the rules cannot (app/jarvis_ai.py): the rules and tools, a tool call, the end.
    @app.get("/api/jarvis/ai")
    def jarvis_ai_spec():
        from app import jarvis_ai
        return JSONResponse(jarvis_ai.spec(), headers={"Cache-Control": "no-store"})

    @app.post("/api/jarvis/ai/tool")
    async def jarvis_ai_tool(request: Request, db: Session = Depends(get_db)):
        from starlette.concurrency import run_in_threadpool
        from app import jarvis_ai
        body = await request.json()
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        try:
            out = await run_in_threadpool(jarvis_ai.call, db, str(body.get("turn") or ""), str(body.get("conversation") or ""),
                                          str(body.get("name") or ""), params, str(body.get("request") or "")[:4000])
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        return JSONResponse(json.loads(json.dumps(out, ensure_ascii=False, default=str)), headers={"Cache-Control": "no-store"})

    @app.post("/api/jarvis/ai/finish")
    async def jarvis_ai_finish(request: Request, db: Session = Depends(get_db)):
        from starlette.concurrency import run_in_threadpool
        from app import jarvis_ai
        body = await request.json()
        out = await run_in_threadpool(jarvis_ai.finish, db, str(body.get("turn") or ""), str(body.get("reply") or "")[:8000])
        if out.get("reply"):
            from app.models import Customer
            from app.voice import to_speech
            out["speech"] = to_speech(out["reply"], [c.code for c in db.scalars(select(Customer))])
        return JSONResponse(json.loads(json.dumps(out, ensure_ascii=False, default=str)), headers={"Cache-Control": "no-store"})

    @app.get("/api/jarvis/state")
    def jarvis_state(db: Session = Depends(get_db)):
        from app.models import AgentProposal, Customer, OrderItem, Season
        seasons = [s.name for s in db.scalars(select(Season).order_by(Season.id.desc()))]
        return JSONResponse({"seasons": seasons,
                             "customers": db.scalar(select(func.count(Customer.id))) or 0,
                             "items": db.scalar(select(func.count(OrderItem.id))) or 0,
                             "pending": db.scalar(select(func.count(AgentProposal.id))
                                                  .where(AgentProposal.status == "pending")) or 0},
                            headers={"Cache-Control": "no-store"})
