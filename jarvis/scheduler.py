"""Persistent reminders. Uses the system timezone (never hardcoded).

After downtime a reminder whose time passed while JARVIS was not running is
reported as missed (with the original time), not silently fired as if on time.
"""
from __future__ import annotations

import asyncio
import time

from .db import Database, new_id, now
from .events import AuditLog, EventBus
from .tools.base import Risk, Tool, ToolInputError, ToolResult, ToolSpec, Verification, run_cmd

GRACE_S = 120   # fired up to 2 minutes late still counts as on time
COMMAND_GRACE_S = 900   # a scheduled ACTION missed by more than 15 min (JARVIS was off) is reported, never run late


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

    def add(self, text: str, due_at: float, task_id: str | None, command: str | None = None) -> dict:
        rid = new_id("r_")
        self.db.execute("INSERT INTO reminders(id,text,due_at,created_at,status,task_id,command) VALUES(?,?,?,?,?,?,?)",
                        (rid, text, due_at, now(), "scheduled", task_id, command))
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
        if cur.rowcount:
            self.bus.publish("reminder.updated", {"reminder": self.get(rid)})
        return cur.rowcount > 0

    def reschedule(self, rid: str, due_at: float) -> bool:
        cur = self.db.execute("UPDATE reminders SET due_at=? WHERE id=? AND status='scheduled'", (due_at, rid))
        self._wake.set()
        if cur.rowcount:
            self.bus.publish("reminder.updated", {"reminder": self.get(rid)})
        return cur.rowcount > 0

    def latest(self) -> dict | None:
        """The most recently created item that is still waiting («эту задачу», «его»)."""
        r = self.db.one("SELECT * FROM reminders WHERE status='scheduled' ORDER BY created_at DESC LIMIT 1")
        return dict(r) if r else None

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
        late = now() - r["due_at"] > (COMMAND_GRACE_S if r.get("command") else GRACE_S)
        status = "missed" if late else "fired"
        # Claim it first so a crash mid-notification never fires it twice.
        cur = self.db.execute("UPDATE reminders SET status=?, fired_at=? WHERE id=? AND status='scheduled'",
                              (status, now(), r["id"]))
        if cur.rowcount == 0:
            return
        title = ("Пропущенное действие" if late else "Действие по расписанию") if r.get("command") else \
            ("Пропущенное напоминание" if late else "Напоминание")
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


def _due(args) -> float:
    if args.get("at"):
        import datetime as dt
        h, m = (int(x) for x in str(args["at"]).split(":"))
        t = dt.datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
        if t.timestamp() <= now() + 5:
            t += dt.timedelta(days=1)
        return t.timestamp()
    return now() + float(args["minutes"]) * 60


_TIME = {"minutes": {"type": "number", "minimum": 0.1, "maximum": 10080},
         "at": {"type": "string", "pattern": "^([01]?[0-9]|2[0-3]):[0-5][0-9]$"}}


class TaskSchedule(Tool):
    """Do something later: the command is run at its time exactly as if the user said it then (same permissions)."""

    def __init__(self, scheduler: Scheduler):
        self.scheduler = scheduler
        self.spec = ToolSpec(
            id="task_schedule", title="Запланировать действие",
            description=("Schedule an ACTION to be done later by JARVIS (not just a reminder): e.g. create a file, "
                         "open an app, run a check. command = the full instruction in the user's language as they "
                         "would say it then, with names, paths and text. Give minutes from now OR at=HH:MM. At its "
                         "time JARVIS runs it with the usual permissions. Survives restarts."),
            params={"type": "object", "required": ["command"], "properties": {
                "command": {"type": "string", "minLength": 3, "maxLength": 1000}, **_TIME}},
            risk=Risk.LOW, side_effects="local", timeout_s=3, idempotent=False,
            verification="Действие записано в базу и стоит в очереди планировщика")

    def validate(self, args, ctx):
        a = Tool.validate(self, args, ctx)
        if not a.get("minutes") and not a.get("at"):
            raise ToolInputError("Нужно время: через сколько минут или во сколько (ЧЧ:ММ)")
        return a

    def describe(self, args):
        when = f"в {args['at']}" if args.get("at") else f"через {args['minutes']:g} мин"
        return f"Запланировать {when}: {args['command'][:160]}"

    async def run(self, args, ctx):
        due = _due(args)
        r = self.scheduler.add(args["command"], due, ctx.task_id, command=args["command"])
        return ToolResult({"id": r["id"], "command": args["command"], "due_at": due, "due_local": fmt_time(due)},
                          f"Запланировано на {fmt_time(due)}: {args['command'][:120]}.", card="reminder")

    async def verify(self, args, result, ctx):
        ok = self.scheduler.get(result.data["id"])["status"] == "scheduled"
        return Verification("verified" if ok else "failed", self.spec.verification if ok else "Не найдено в очереди")


class ScheduledChange(Tool):
    """Cancel or move a scheduled action or reminder («отмени эту задачу», «перенеси на 5 минут»)."""

    def __init__(self, scheduler: Scheduler):
        self.scheduler = scheduler
        self.spec = ToolSpec(
            id="scheduled_change", title="Изменить расписание",
            description=("Cancel or reschedule a scheduled action/reminder. id = its id from the list (omit for the "
                         "most recent one, «эта задача»). op = cancel | move; for move give minutes from now OR "
                         "at=HH:MM. Use op=list to see what is scheduled."),
            params={"type": "object", "required": ["op"], "properties": {
                "op": {"type": "string", "enum": ["cancel", "move", "list"]}, "id": {"type": "string", "maxLength": 40},
                **_TIME}},
            risk=Risk.LOW, side_effects="local", timeout_s=3, idempotent=False,
            verification="Статус и время прочитаны из базы после изменения")

    def validate(self, args, ctx):
        a = Tool.validate(self, args, ctx)
        if a["op"] == "list":
            return a
        r = self.scheduler.get(a["id"]) if a.get("id") and self.scheduler.db.one(
            "SELECT 1 FROM reminders WHERE id=?", (a["id"],)) else self.scheduler.latest()
        if not r or r["status"] != "scheduled":
            raise ToolInputError("Запланированных задач нет" if not r else "Эта задача уже не ждёт выполнения")
        if a["op"] == "move" and not a.get("minutes") and not a.get("at"):
            raise ToolInputError("На какое время перенести?")
        return {**a, "id": r["id"]}

    def describe(self, args):
        if args["op"] == "list":
            return "Что запланировано"
        r = self.scheduler.get(args["id"])
        what = (r.get("command") or r["text"])[:100]
        if args["op"] == "cancel":
            return f"Отменить: {what} ({fmt_time(r['due_at'])})"
        when = f"{args['at']}" if args.get("at") else f"через {args['minutes']:g} мин"
        return f"Перенести «{what}» на {when}"

    async def run(self, args, ctx):
        if args["op"] == "list":
            items = [{"id": r["id"], "what": r.get("command") or r["text"], "kind": "действие" if r.get("command")
                      else "напоминание", "when": fmt_time(r["due_at"])} for r in self.scheduler.upcoming()]
            return ToolResult({"items": items}, f"Запланировано: {len(items)}.")
        if args["op"] == "cancel":
            self.scheduler.cancel(args["id"])
            r = self.scheduler.get(args["id"])
            return ToolResult({"id": r["id"], "status": r["status"]}, f"Отменено: {(r.get('command') or r['text'])[:100]}.")
        due = _due(args)
        self.scheduler.reschedule(args["id"], due)
        r = self.scheduler.get(args["id"])
        return ToolResult({"id": r["id"], "due_at": r["due_at"], "due_local": fmt_time(r["due_at"])},
                          f"Перенесено на {fmt_time(r['due_at'])}: {(r.get('command') or r['text'])[:100]}.")

    async def verify(self, args, result, ctx):
        if args["op"] == "list":
            return Verification("verified", "Список прочитан из базы")
        r = self.scheduler.get(args["id"])
        ok = r["status"] == "cancelled" if args["op"] == "cancel" else (
            r["status"] == "scheduled" and abs(r["due_at"] - result.data["due_at"]) < 1)
        return Verification("verified" if ok else "failed", "В базе: " + r["status"] + ", " + fmt_time(r["due_at"]))

