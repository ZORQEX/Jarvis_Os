"""Tool registry and gateway — the single execution path for every tool call,
whether it came from the deterministic router or from a model."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .db import now
from .events import AuditLog, EventBus
from .policy import Approvals, PolicyEngine
from .tasks import S, TaskStore
from .tools.base import Tool, ToolContext, ToolError, ToolInputError, ToolResult, Verification


class Registry:
    def __init__(self, tools: list[Tool]):
        self.tools: dict[str, Tool] = {t.spec.id: t for t in tools}

    def get(self, tool_id: str) -> Tool | None:
        return self.tools.get(tool_id)

    def model_tools(self) -> list[Tool]:
        return [t for t in self.tools.values() if t.spec.model_callable]

    def describe(self) -> list[dict]:
        return [{"id": t.spec.id, "title": t.spec.title, "risk": t.spec.risk, "side_effects": t.spec.side_effects,
                 "timeout_s": t.spec.timeout_s, "verification": t.spec.verification}
                for t in self.tools.values()]


@dataclass
class Outcome:
    status: str                      # ok | denied | failed | cancelled | invalid
    step_id: str | None = None
    result: ToolResult | None = None
    verification: Verification | None = None
    error: str | None = None
    data: dict = field(default_factory=dict)

    def for_model(self) -> dict:
        """Compact, bounded payload returned to a model as the function response."""
        if self.status != "ok":
            return {"status": self.status, "error": self.error}
        d = dict(self.result.data)
        text = d.get("text")
        out = {"status": "ok", "summary": self.result.summary,
               "verification": self.verification.as_dict() if self.verification else None,
               "data": {k: v for k, v in d.items() if k != "text" and not k.startswith("_")}}
        if text is not None:
            # File/email content is data, not instructions.
            out["untrusted_content"] = "<<<UNTRUSTED_DATA\n" + text[:20000] + "\nUNTRUSTED_DATA>>>"
        return out


def _bounded(d: dict, max_chars: int = 20000) -> dict:
    out = {}
    for k, v in d.items():
        if k.startswith("_"):
            continue
        if isinstance(v, str) and len(v) > max_chars:
            v = v[:max_chars] + "…"
        out[k] = v
    return out


class Gateway:
    def __init__(self, registry: Registry, policy: PolicyEngine, approvals: Approvals, tasks: TaskStore,
                 bus: EventBus, audit: AuditLog, config):
        self.registry, self.policy, self.approvals = registry, policy, approvals
        self.tasks, self.bus, self.audit, self.config = tasks, bus, audit, config

    async def execute(self, task_id: str, tool_id: str, args: dict, why: str, *,
                      conversation: dict | None = None,
                      gate: Callable[[], Awaitable[None]] | None = None,
                      cancelled: Callable[[], bool] = lambda: False,
                      preapproved: str | None = None) -> Outcome:
        tool = self.registry.get(tool_id)
        if tool is None:
            return Outcome("invalid", error=f"Неизвестный инструмент: {tool_id}")
        spec = tool.spec
        sid = self.tasks.add_step(task_id, tool_id, args, spec.risk, why)
        ctx = ToolContext(config=self.config, task_id=task_id, cancelled=cancelled,
                          conversation=conversation if conversation is not None else {},
                          progress=lambda msg, frac=None: self.bus.publish(
                              "step.progress", {"step_id": sid, "message": msg, "fraction": frac}, task_id))
        # 1. validate
        try:
            args = tool.validate(dict(args), ctx)
        except ToolInputError as e:
            self.tasks.update_step(sid, state="invalid", error=str(e), finished_at=now())
            self.audit.write("tool.invalid", task_id=task_id, tool=tool_id, error=str(e))
            return Outcome("invalid", sid, error=str(e))
        self.tasks.update_step(sid, args=args, title=tool.describe(args))

        # 2. policy
        decision = self.policy.evaluate(spec, args)
        # A prior approval can only answer the "ask" — it never overrides a policy denial (e.g. emergency stop).
        if decision.action == "ask" and preapproved and self.approvals.is_valid_for(preapproved, tool_id, args, task_id):
            decision.action, decision.reason = "allow", "Подтверждено вами"
        if decision.action == "deny":
            self.tasks.update_step(sid, state="denied", error=decision.reason, finished_at=now())
            self.audit.write("tool.denied", task_id=task_id, tool=tool_id, reason=decision.reason)
            return Outcome("denied", sid, error=decision.reason)
        if decision.action == "ask":
            outcome = await self._ask(task_id, sid, tool, args, decision.reason)
            if outcome.status != "ok":
                return outcome
            args = outcome.data["args"]
            if self.policy.estop:     # stop pressed while the card was open
                self.tasks.update_step(sid, state="denied", error="Экстренная остановка", finished_at=now())
                return Outcome("denied", sid, error="Экстренная остановка")

        # 3. pause point between steps
        if gate:
            await gate()

        # 4. run
        t0 = time.perf_counter()
        self.tasks.update_step(sid, state="running", started_at=now())
        self.audit.write("tool.start", task_id=task_id, tool=tool_id, args=args, why=why)
        try:
            result = await asyncio.wait_for(tool.run(args, ctx), spec.timeout_s)
            self.tasks.update_step(sid, state="verifying")
            verification = await asyncio.wait_for(tool.verify(args, result, ctx), max(5.0, spec.timeout_s))
        except asyncio.CancelledError:
            self.tasks.update_step(sid, state="cancelled", finished_at=now(),
                                   duration_ms=(time.perf_counter() - t0) * 1000, error="Отменено")
            self.audit.write("tool.cancelled", task_id=task_id, tool=tool_id)
            raise
        except asyncio.TimeoutError:
            return self._fail(sid, task_id, tool_id, t0, f"Превышено время ожидания ({spec.timeout_s:.0f} с)")
        except (ToolError, OSError, ValueError, KeyError) as e:
            return self._fail(sid, task_id, tool_id, t0, str(e) or e.__class__.__name__)
        ms = (time.perf_counter() - t0) * 1000
        state = "failed" if verification.status == "failed" else "succeeded"
        self.tasks.update_step(sid, state=state, finished_at=now(), duration_ms=round(ms, 1),
                               result={"summary": result.summary, "card": result.card, "data": _bounded(result.data),
                                       "artifacts": result.artifacts},
                               verification=verification.as_dict())
        self.audit.write("tool.done", task_id=task_id, tool=tool_id, ms=round(ms, 1),
                         verification=verification.status)
        if state == "failed":
            return Outcome("failed", sid, result, verification, error=verification.detail)
        return Outcome("ok", sid, result, verification)

    def _fail(self, sid, task_id, tool_id, t0, msg) -> Outcome:
        self.tasks.update_step(sid, state="failed", finished_at=now(),
                               duration_ms=round((time.perf_counter() - t0) * 1000, 1), error=msg)
        self.audit.write("tool.failed", task_id=task_id, tool=tool_id, error=msg)
        return Outcome("failed", sid, error=msg)

    async def _ask(self, task_id, sid, tool, args, reason) -> Outcome:
        spec = tool.spec
        summary = {"title": spec.title, "action": tool.describe(args), "reason": reason, "impact": spec.impact,
                   "risk": spec.risk,
                   "side_effects": spec.side_effects, "editable": _editable_fields(spec),
                   "session_grant_allowed": spec.risk == "moderate"}
        prev = self.tasks.state(task_id)
        self.tasks.update_step(sid, state="awaiting_approval")
        a = self.approvals.create(task_id, sid, spec, args, summary)
        self.tasks.transition(task_id, S.WAITING_APPROVAL)
        a = await self.approvals.wait(a["id"])
        if self.tasks.state(task_id) == S.WAITING_APPROVAL:
            self.tasks.transition(task_id, prev if prev in (S.RUNNING, S.PLANNING) else S.RUNNING)
        if a["status"] != "approved":
            msg = "Вы отклонили действие" if a["status"] == "denied" else "Срок подтверждения истёк"
            self.tasks.update_step(sid, state="denied", error=msg, finished_at=now())
            return Outcome("denied", sid, error=msg)
        final_args = a["decided_args"] or args
        if final_args != args:
            try:
                final_args = tool.validate(dict(final_args), ToolContext(self.config, task_id))
            except ToolInputError as e:
                self.tasks.update_step(sid, state="invalid", error=str(e), finished_at=now())
                return Outcome("invalid", sid, error=str(e))
            self.tasks.update_step(sid, args=final_args, title=tool.describe(final_args))
        if not self.approvals.is_valid_for(a["id"], spec.id, final_args, task_id):
            self.tasks.update_step(sid, state="denied", error="Подтверждение не соответствует действию")
            return Outcome("denied", sid, error="Подтверждение не соответствует действию")
        return Outcome("ok", sid, data={"args": final_args, "approval_id": a["id"]})


def _editable_fields(spec) -> list[str]:
    """String parameters the user may edit in the approval card (content, not targets)."""
    if spec.side_effects == "external":
        return []   # external content is edited in its own editor; any edit needs a fresh approval
    return [k for k, v in spec.params.get("properties", {}).items()
            if v.get("type") == "string" and k in ("text", "title", "body")]
