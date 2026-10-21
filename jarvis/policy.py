"""Central permission policy and approvals.

Policy is code, not a prompt: no tool exposes a way to change it, and model
output never reaches these functions except as tool arguments to be judged.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass

from .db import Database, dumps, loads, new_id, now
from .events import AuditLog, EventBus
from .tools.base import Risk, ToolSpec


@dataclass
class Decision:
    action: str   # allow | ask | deny
    reason: str


class PolicyEngine:
    def __init__(self):
        self.estop = False
        self.session_grants: set[str] = set()   # tool ids the user allowed for this run
        self.hooks: list = []    # explicit user rules (e.g. trusted messaging) that may answer "ask" — never "deny"
        self.full_access = False  # «Полный доступ», включается пользователем только в окне JARVIS

    def evaluate(self, spec: ToolSpec, args: dict) -> Decision:
        if spec.risk == Risk.FORBIDDEN:
            return Decision("deny", "Действие запрещено политикой безопасности")
        if self.estop and spec.side_effects != "none":
            return Decision("deny", "Включена экстренная остановка — действия с последствиями заблокированы")
        for hook in self.hooks:
            d = hook(spec, args)
            if d is not None and d.action == "allow":
                return d
        if spec.risk == Risk.HIGH:
            return Decision("ask", "Высокий риск: нужно явное подтверждение")
        if spec.risk == Risk.MODERATE:
            if self.full_access and spec.side_effects != "external":
                return Decision("allow", "Полный доступ (включён вами)")
            if spec.id in self.session_grants:
                return Decision("allow", "Разрешено вами до конца сеанса")
            return Decision("ask", "Действие меняет данные на компьютере или касается приватности")
        return Decision("allow", "Низкий риск")

    def grant_session(self, spec: ToolSpec) -> bool:
        if spec.risk != Risk.MODERATE:
            return False   # high-risk actions are never granted in bulk
        self.session_grants.add(spec.id)
        return True


def action_digest(tool_id: str, args: dict, task_id: str) -> str:
    canon = json.dumps({"tool": tool_id, "args": args, "task": task_id}, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()


class ApprovalError(Exception):
    pass


class Approvals:
    def __init__(self, db: Database, bus: EventBus, audit: AuditLog, ttl_s: int = 900):
        self.db, self.bus, self.audit, self.ttl = db, bus, audit, ttl_s
        self._waiters: dict[str, asyncio.Future] = {}

    def create(self, task_id: str, step_id: str | None, spec: ToolSpec, args: dict, summary: dict) -> dict:
        aid = new_id("a_")
        t = now()
        self.db.execute(
            "INSERT INTO approvals(id,task_id,step_id,tool_id,args,digest,risk,summary,status,created_at,expires_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (aid, task_id, step_id, spec.id, dumps(args), action_digest(spec.id, args, task_id), spec.risk,
             dumps(summary), "pending", t, t + self.ttl))
        a = self.get(aid)
        self.audit.write("approval.requested", approval_id=aid, task_id=task_id, tool=spec.id, risk=spec.risk)
        self.bus.publish("approval.created", {"approval": a}, task_id)
        return a

    def get(self, aid: str) -> dict | None:
        r = self.db.one("SELECT * FROM approvals WHERE id=?", (aid,))
        if not r:
            return None
        d = dict(r)
        for k in ("args", "summary", "decided_args"):
            d[k] = loads(d[k])
        return d

    def pending(self) -> list[dict]:
        self.expire_old()
        return [self.get(r["id"]) for r in
                self.db.all("SELECT id FROM approvals WHERE status='pending' ORDER BY created_at")]

    def expire_old(self) -> None:
        for r in self.db.all("SELECT id,task_id FROM approvals WHERE status='pending' AND expires_at<?", (now(),)):
            if self.get(r["id"])["status"] == "pending":   # may have been closed by a nested call
                self._finish(r["id"], "expired", None)

    async def wait(self, aid: str) -> dict:
        a = self.get(aid)
        if a["status"] != "pending":
            return a
        fut = asyncio.get_running_loop().create_future()
        self._waiters[aid] = fut
        try:
            remaining = max(0.0, a["expires_at"] - now())
            return await asyncio.wait_for(fut, remaining)
        except asyncio.TimeoutError:
            self._finish(aid, "expired", None)
            return self.get(aid)
        finally:
            self._waiters.pop(aid, None)

    def decide(self, aid: str, approve: bool, edited_args: dict | None = None) -> dict:
        a = self.get(aid)
        if not a:
            raise ApprovalError("Запрос не найден")
        if a["status"] != "pending":
            raise ApprovalError(f"Запрос уже закрыт ({a['status']})")
        if a["expires_at"] < now():
            self._finish(aid, "expired", None)
            raise ApprovalError("Срок подтверждения истёк — повторите команду")
        return self._finish(aid, "approved" if approve else "denied", edited_args if approve else None)

    def cancel_all(self, reason: str) -> int:
        n = 0
        for r in self.db.all("SELECT id FROM approvals WHERE status='pending'"):
            self._finish(r["id"], "denied", None, reason=reason)
            n += 1
        return n

    def _finish(self, aid: str, status: str, decided_args: dict | None, reason: str | None = None) -> dict:
        a = self.get(aid)
        final_args = decided_args if decided_args is not None else a["args"]
        digest = action_digest(a["tool_id"], final_args, a["task_id"])
        self.db.execute("UPDATE approvals SET status=?, decided_at=?, decided_args=?, digest=? WHERE id=?",
                        (status, now(), dumps(final_args), digest, aid))
        a = self.get(aid)
        self.audit.write("approval." + status, approval_id=aid, task_id=a["task_id"], tool=a["tool_id"],
                         edited=decided_args is not None, reason=reason)
        self.bus.publish("approval.updated", {"approval": a}, a["task_id"])
        fut = self._waiters.get(aid)
        if fut and not fut.done():
            fut.set_result(a)
        return a

    def is_valid_for(self, aid: str, tool_id: str, args: dict, task_id: str) -> bool:
        """Execution-time check: approval exists, was approved, and covers exactly these args."""
        a = self.get(aid)
        return bool(a and a["status"] == "approved" and a["tool_id"] == tool_id and a["task_id"] == task_id
                    and a["digest"] == action_digest(tool_id, args, task_id))
