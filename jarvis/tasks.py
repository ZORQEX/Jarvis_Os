"""Persistent tasks with an enforced state machine."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .db import Database, dumps, loads, new_id, now
from .events import EventBus


class S(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_for_approval"
    WAITING_DEPENDENCY = "waiting_for_dependency"
    PAUSED = "paused"
    RETRYING = "retrying"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


TERMINAL = {S.COMPLETED, S.FAILED, S.CANCELLED}
_ANY_ACTIVE_EXIT = {S.FAILED, S.CANCELLED, S.BLOCKED, S.PAUSED}

TRANSITIONS: dict[S, set[S]] = {
    S.CREATED: {S.QUEUED, S.PLANNING, S.RUNNING, S.CANCELLED, S.FAILED},
    S.QUEUED: {S.PLANNING, S.RUNNING, S.CANCELLED, S.PAUSED, S.FAILED},
    S.PLANNING: {S.RUNNING, S.WAITING_APPROVAL, S.COMPLETED} | _ANY_ACTIVE_EXIT,
    S.RUNNING: {S.PLANNING, S.WAITING_APPROVAL, S.WAITING_DEPENDENCY, S.VERIFYING, S.RETRYING, S.COMPLETED}
               | _ANY_ACTIVE_EXIT,
    S.WAITING_APPROVAL: {S.RUNNING, S.PLANNING} | _ANY_ACTIVE_EXIT,
    S.WAITING_DEPENDENCY: {S.RUNNING, S.PLANNING} | _ANY_ACTIVE_EXIT,
    S.PAUSED: {S.RUNNING, S.PLANNING, S.WAITING_APPROVAL, S.CANCELLED, S.FAILED},
    S.RETRYING: {S.RUNNING, S.PLANNING} | _ANY_ACTIVE_EXIT,
    S.VERIFYING: {S.COMPLETED, S.RUNNING, S.PLANNING} | _ANY_ACTIVE_EXIT,
    S.BLOCKED: {S.RUNNING, S.PLANNING, S.CANCELLED, S.FAILED},
    S.COMPLETED: set(),
    S.FAILED: set(),
    S.CANCELLED: set(),
}


class IllegalTransition(Exception):
    pass


@dataclass
class TaskView:
    id: str
    state: S
    request: str


class TaskStore:
    def __init__(self, db: Database, bus: EventBus):
        self.db, self.bus = db, bus

    # -- tasks -------------------------------------------------------------
    def create(self, request: str, conversation_id: str | None, route: str | None = None,
               level: int | None = None, priority: int = 5) -> str:
        tid = new_id("t_")
        t = now()
        self.db.execute(
            "INSERT INTO tasks(id,conversation_id,request,state,route,level,priority,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)", (tid, conversation_id, request, S.CREATED, route, level, priority, t, t))
        self.bus.publish("task.created", {"task": self.get(tid)}, tid)
        return tid

    def get(self, tid: str) -> dict[str, Any] | None:
        r = self.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not r:
            return None
        d = dict(r)
        d["result"] = loads(d["result"])
        d["checkpoint"] = loads(d["checkpoint"])
        d["decision"] = loads(d.get("decision"))
        d["steps"] = self.steps(tid)
        return d

    def state(self, tid: str) -> S:
        r = self.db.one("SELECT state FROM tasks WHERE id=?", (tid,))
        if not r:
            raise KeyError(tid)
        return S(r["state"])

    def transition(self, tid: str, new: S, **fields: Any) -> None:
        with self.db.tx() as c:
            cur = S(c.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()["state"])
            if new != cur and new not in TRANSITIONS[cur]:
                raise IllegalTransition(f"{cur} -> {new}")
            sets = {"state": new.value, "updated_at": now(), **fields}
            if new in TERMINAL:
                sets["finished_at"] = now()
            for k in ("result", "checkpoint"):
                if k in sets and not isinstance(sets[k], str):
                    sets[k] = dumps(sets[k])
            cols = ",".join(f"{k}=?" for k in sets)
            c.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*sets.values(), tid))
        self.bus.publish("task.updated", {"task": self.get(tid)}, tid)

    def update(self, tid: str, **fields: Any) -> None:
        for k in ("result", "checkpoint"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = dumps(fields[k])
        fields["updated_at"] = now()
        cols = ",".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), tid))

    def active(self) -> list[dict]:
        rows = self.db.all("SELECT id FROM tasks WHERE state NOT IN ('completed','failed','cancelled')"
                           " ORDER BY created_at")
        return [self.get(r["id"]) for r in rows]

    def recent(self, limit: int = 30, conversation_id: str | None = None, exclude_route: str | None = None) -> list[dict]:
        if conversation_id:
            rows = self.db.all("SELECT id FROM tasks WHERE conversation_id=? ORDER BY created_at DESC LIMIT ?",
                               (conversation_id, limit))
        elif exclude_route:
            rows = self.db.all("SELECT id FROM tasks WHERE route IS NULL OR route != ? ORDER BY created_at DESC LIMIT ?",
                               (exclude_route, limit))
        else:
            rows = self.db.all("SELECT id FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self.get(r["id"]) for r in rows]

    # -- steps -------------------------------------------------------------
    def add_step(self, tid: str, tool_id: str, args: dict, risk: str, why: str) -> str:
        sid = new_id("s_")
        seq = self.db.one("SELECT COALESCE(MAX(seq),0)+1 AS n FROM task_steps WHERE task_id=?", (tid,))["n"]
        self.db.execute("INSERT INTO task_steps(id,task_id,seq,tool_id,args,state,risk,why) VALUES(?,?,?,?,?,?,?,?)",
                        (sid, tid, seq, tool_id, dumps(args), "planned", risk, why))
        self.bus.publish("step.updated", {"step": self.step(sid)}, tid)
        return sid

    def update_step(self, sid: str, **fields: Any) -> None:
        for k in ("result", "verification", "args"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = dumps(fields[k])
        cols = ",".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE task_steps SET {cols} WHERE id=?", (*fields.values(), sid))
        st = self.step(sid)
        self.bus.publish("step.updated", {"step": st}, st["task_id"])

    def step(self, sid: str) -> dict:
        d = dict(self.db.one("SELECT * FROM task_steps WHERE id=?", (sid,)))
        for k in ("args", "result", "verification"):
            d[k] = loads(d[k])
        return d

    def steps(self, tid: str) -> list[dict]:
        out = []
        for r in self.db.all("SELECT * FROM task_steps WHERE task_id=? ORDER BY seq", (tid,)):
            d = dict(r)
            for k in ("args", "result", "verification"):
                d[k] = loads(d[k])
            out.append(d)
        return out

    # -- recovery ----------------------------------------------------------
    def recover_after_restart(self) -> list[str]:
        """Called at startup. A task that was mid-execution when the process died
        cannot be assumed finished or safe to replay, so it is marked blocked with the
        reason. Tasks waiting for approval keep waiting — the approval is persisted."""
        touched = []
        for r in self.db.all("SELECT id,state FROM tasks WHERE state IN "
                             "('created','queued','planning','running','verifying','retrying','paused')"):
            self.db.execute("UPDATE task_steps SET state='interrupted', error=? WHERE task_id=? AND state='running'",
                            ("Прервано перезапуском JARVIS", r["id"]))
            self.db.execute("UPDATE tasks SET state=?, error=?, updated_at=? WHERE id=?",
                            (S.BLOCKED, "Прервано перезапуском. Проверьте результат и повторите при необходимости.",
                             now(), r["id"]))
            touched.append(r["id"])
        return touched
