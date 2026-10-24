"""Inside JARVIS (added for JARVIS, 2026-10-25): the AI of an orders turn is JARVIS's own conversation, not a second one.

The app's rules still answer what they know in milliseconds (``agent.handle``: intercept + ``RuleInterpreter.fast``).
When they have no answer and the request came from JARVIS (``/api/agent/message`` with ``"jarvis": true``), the app no
longer starts its own Claude Code process: the turn is handed to JARVIS (``handoff``). JARVIS's Claude then calls the
same typed tools here (``/api/jarvis/ai/tool`` → ``agent.run_tool``, on this app's database, in this app's process) and
ends the turn (``/api/jarvis/ai/finish`` → ``agent._finish_turn``): a proposal is stored exactly as before and nothing
is saved until the owner confirms. One conversation, one memory, one task list — JARVIS's; this app keeps its data,
its rules and its proposals.

A turn is JARVIS's task id; pending changes of a turn live here until it ends (an hour at most).
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app import agent

TURN_TTL_S = 3600
SKIP = {"open_file_picker"}            # a Mac file dialog: JARVIS finds files on the computer itself (fashion_attach)


@dataclass
class Turn:
    conversation: str
    text: str
    screen: dict
    pending: agent._Pending = field(default_factory=agent._Pending)
    calls: list = field(default_factory=list)
    question: dict | None = None
    at: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)     # a tool call and the turn's end never overlap
    closed: bool = False


TURNS: dict[str, Turn] = {}
_LOCK = threading.Lock()


def _valid(turn: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{4,64}", turn or ""))


def open_turn(turn: str, conversation: str, text: str, screen: dict | None = None) -> Turn:
    with _LOCK:
        now = time.time()
        for k in [k for k, t in TURNS.items() if now - t.at > TURN_TTL_S]:
            TURNS.pop(k, None)
        t = TURNS.get(turn)
        if t is None:
            t = TURNS[turn] = Turn(conversation, text, screen or {})
        return t


def _close(turn: str) -> Turn | None:
    """The turn leaves the table and waits for a tool call still running in it, so its change is not lost."""
    with _LOCK:
        t = TURNS.pop(turn, None)
    if t is not None:
        with t.lock:
            t.closed = True
    return t


def spec() -> dict:
    """What JARVIS's Claude needs: the business rules and the typed tools (the same ones the app's own turn had)."""
    return {"system": agent.SYSTEM, "tools": [t for t in agent.TOOLS if t["name"] not in SKIP]}


def call(db: Session, turn: str, conversation: str, name: str, params: dict, request: str = "") -> dict:
    if not _valid(turn) or not re.fullmatch(r"[a-f0-9]{16}", conversation or ""):
        raise ValueError("Неверный ход или диалог")
    if name in SKIP or name not in {t["name"] for t in agent.TOOLS}:
        return {"error": f"Неизвестный инструмент {name}"}
    t = open_turn(turn, conversation, request)
    with t.lock:
        if t.closed:
            return {"error": "Этот ход уже завершён — ничего не подготовлено"}
        output = agent.run_tool(db, name, params or {}, t.pending, t.screen)
        t.calls.append({"name": name, "input": params, "output": output})
    if name == "ask_user" and "error" not in output:
        t.question = {"question": (params or {}).get("question"), "options": (params or {}).get("options") or []}
        output = output | {"note": "Вопрос показан владелице с вариантами. Закончи ответ этим вопросом, больше ничего не делай."}
    if output.get("pending_approval"):
        output = output | {"note": "Это только предложение: JARVIS покажет его владелице с кнопкой «Подтвердить». "
                                   "Не пиши, что сохранено."}
    return output


def finish(db: Session, turn: str, reply: str) -> dict:
    """The end of JARVIS's turn: the proposal (if any) is stored, the reply joins the app's dialog history."""
    t = _close(turn)
    if t is None:
        return {"reply": reply, "proposal": None, "question": None, "client": None}
    result = agent._finish_turn(db, t.conversation, t.text, t.screen, t.pending, t.calls, t.question, reply)
    db.add(agent.AgentMessage(conversation=t.conversation, role="assistant", text=result.reply[:8000],
                              data_json=json.dumps({"proposal": result.proposal, "question": result.question,
                                                    "tools": [c["name"] for c in result.tool_calls],
                                                    "steps": agent.describe_steps(result.tool_calls),
                                                    "context": result.data, "panel": result.panel, "by": "jarvis"},
                                                   ensure_ascii=False, default=str)))
    db.commit()
    return {"reply": result.reply, "proposal": result.proposal, "question": result.question, "panel": result.panel,
            "client": result.client, "steps": agent.describe_steps(result.tool_calls)}


def rules(db: Session, turn: str) -> dict:
    """JARVIS's AI is unavailable (a limit, offline): the turn ends with this app's full rules — what the app did when
    its own AI failed («Ответила без ИИ»). Nothing pending from the model is kept."""
    t = _close(turn)
    if t is None:
        return {"reply": "", "proposal": None, "question": None, "client": None}
    result = agent.RuleInterpreter(db, t.conversation, t.screen).run(t.text)
    db.add(agent.AgentMessage(conversation=t.conversation, role="assistant", text=result.reply[:8000],
                              data_json=json.dumps({"proposal": result.proposal, "question": result.question,
                                                    "tools": [c["name"] for c in result.tool_calls],
                                                    "steps": agent.describe_steps(result.tool_calls),
                                                    "context": result.data, "panel": result.panel, "speech": result.speech,
                                                    "by": "rules"}, ensure_ascii=False, default=str)))
    db.commit()
    return {"reply": result.reply, "proposal": result.proposal, "question": result.question, "panel": result.panel,
            "client": result.client, "speech": result.speech}
