"""Persistent reminders. Uses the system timezone (never hardcoded).

After downtime a reminder whose time passed while JARVIS was not running is
reported as missed (with the original time), not silently fired as if on time.
"""
from __future__ import annotations

import asyncio
import time

from .db import Database, new_id, now
from .events import AuditLog, EventBus
from .tools.base import Risk, Tool, ToolResult, ToolSpec, Verification, run_cmd

GRACE_S = 120   # fired up to 2 minutes late still counts as on time


def fmt_time(ts: float) -> str:
    lt = time.localtime(ts)
    today = time.localtime()
    hm = time.strftime("%H:%M", lt)
    if lt.tm_yday == today.tm_yday and lt.tm_year == today.tm_year:
        return hm
    return time.strftime("%d.%m %H:%M", lt)


class Scheduler:
    def __init__(self, db: Database, bus: EventBus, audit: AuditLog):
        self.db, self.bus, self.audit = db, bus, audit
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.on_fire = None   # callback(reminder dict, late: bool)

    def add(self, text: str, due_at: float, task_id: str | None) -> dict:
        rid = new_id("r_")
        self.db.execute("INSERT INTO reminders(id,text,due_at,created_at,status,task_id) VALUES(?,?,?,?,?,?)",
                        (rid, text, due_at, now(), "scheduled", task_id))
        self._wake.set()
        r = self.get(rid)
        self.bus.publish("reminder.created", {"reminder": r}, task_id)
        return r

    def get(self, rid: str) -> dict:
        return dict(self.db.one("SELECT * FROM reminders WHERE id=?", (rid,)))

    def upcoming(self) -> list[dict]:
        return [dict(r) for r in self.db.all("SELECT * FROM reminders WHERE status='scheduled' ORDER BY due_at")]

    def cancel(self, rid: str) -> bool:
        cur = self.db.execute("UPDATE reminders SET status='cancelled' WHERE id=? AND status='scheduled'", (rid,))
        self._wake.set()
        return cur.rowcount > 0

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            nxt = self.db.one("SELECT * FROM reminders WHERE status='scheduled' ORDER BY due_at LIMIT 1")
            if nxt is None:
                await self._wake.wait()
                continue
            delay = nxt["due_at"] - now()
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), min(delay, 3600))
                except asyncio.TimeoutError:
                    pass
                continue
            await self._fire(dict(nxt))

    async def _fire(self, r: dict) -> None:
        late = now() - r["due_at"] > GRACE_S
        status = "missed" if late else "fired"
        # Claim it first so a crash mid-notification never fires it twice.
        cur = self.db.execute("UPDATE reminders SET status=?, fired_at=? WHERE id=? AND status='scheduled'",
                              (status, now(), r["id"]))
        if cur.rowcount == 0:
            return
        title = "Пропущенное напоминание" if late else "Напоминание"
        body = r["text"] + (f" (было назначено на {fmt_time(r['due_at'])})" if late else "")
        try:
            await run_cmd(["notify-send", "-a", "JARVIS", "-u", "critical" if not late else "normal",
                           "--", title, body], timeout=3)
        except Exception:
            pass   # UI still gets the event below
        self.audit.write("reminder." + status, reminder_id=r["id"])
        self.bus.publish("reminder.fired", {"reminder": self.get(r["id"]), "late": late, "title": title, "body": body},
                         r.get("task_id"))
        if self.on_fire:
            self.on_fire(self.get(r["id"]), late)


class ReminderCreate(Tool):
    def __init__(self, scheduler: Scheduler):
        self.scheduler = scheduler
        self.spec = ToolSpec(
            id="reminder_create", title="Напоминание",
            description="Schedule a local reminder that shows a desktop notification after the given number of "
                        "minutes (0.1–10080). Runs only while JARVIS is running; missed reminders are reported late.",
            params={"type": "object", "required": ["text", "minutes"], "properties": {
                "text": {"type": "string", "minLength": 1, "maxLength": 300},
                "minutes": {"type": "number", "minimum": 0.1, "maximum": 10080}}},
            risk=Risk.LOW, side_effects="local", timeout_s=3, idempotent=False,
            verification="Напоминание записано в базу и стоит в очереди планировщика",
        )

    def describe(self, args):
        return f"Напомнить через {args['minutes']:g} мин: {args['text']}"

    async def run(self, args, ctx):
        due = now() + args["minutes"] * 60
        r = self.scheduler.add(args["text"], due, ctx.task_id)
        return ToolResult({"id": r["id"], "text": r["text"], "due_at": due, "due_local": fmt_time(due)},
                          f"Напомню в {fmt_time(due)}: {args['text']}.", card="reminder")

    async def verify(self, args, result, ctx):
        r = self.scheduler.get(result.data["id"])
        ok = r["status"] == "scheduled"
        return Verification("verified" if ok else "failed", self.spec.verification if ok else "Не найдено в очереди")
