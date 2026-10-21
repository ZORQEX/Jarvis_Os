"""Outbox: every message JARVIS sends or will send, with its execution state.

A row is claimed (scheduled → sending) in one UPDATE before delivery, so the same job can never be
sent twice — not by two loops, not after a crash. A job found in `sending` at startup is marked
`unknown` (it may or may not have left) and is never retried automatically.
"""
from __future__ import annotations

import asyncio
import time

from ..db import Database, new_id, now
from ..events import AuditLog, EventBus
from .base import CHANNEL_RU, Contact, content_digest

RETRY_DELAYS = (60, 300, 900)   # bounded retry, only when the message certainly did not leave
LATE_OK_S = 15 * 60             # a scheduled message may still go out up to 15 min late (JARVIS was off)

STATUS_RU = {"scheduled": "запланировано", "sending": "отправляется", "sent": "отправлено", "failed": "не отправлено",
             "cancelled": "отменено", "unknown": "статус неизвестен", "missed": "пропущено"}


def when_ru(ts: float) -> str:
    lt, today = time.localtime(ts), time.localtime()
    hm = time.strftime("%H:%M", lt)
    days = (time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) -
            time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))) / 86400
    if round(days) == 0:
        return f"сегодня в {hm}"
    if round(days) == 1:
        return f"завтра в {hm}"
    if round(days) == 2:
        return f"послезавтра в {hm}"
    if round(days) == -1:
        return f"вчера в {hm}"
    return time.strftime("%d.%m", lt) + f" в {hm}"


class Outbox:
    def __init__(self, db: Database, bus: EventBus, audit: AuditLog):
        self.db, self.bus, self.audit = db, bus, audit
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.deliver = None          # async callback(job) -> None, set by the composition root (goes through Gateway)
        self.on_missed = None        # callback(job)

    # ---------------------------------------------------------------- rows
    def get(self, jid: str) -> dict | None:
        r = self.db.one("SELECT * FROM comm_jobs WHERE id=?", (jid,))
        return dict(r) if r else None

    def create(self, contact: Contact, body: str, subject: str | None, due_at: float | None, *, task_id: str,
               step_id: str | None, auth: str, group_id: str | None = None) -> dict:
        jid = new_id("m_")
        t = now()
        self.db.execute(
            "INSERT INTO comm_jobs(id,group_id,task_id,step_id,created_at,updated_at,channel,to_id,to_name,to_address,"
            "subject,body,due_at,status,digest,auth) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, group_id, task_id, step_id, t, t, contact.channel, contact.id, contact.name, contact.address,
             subject, body, due_at, "scheduled" if due_at else "sending",
             content_digest(contact.channel, contact.id, subject, body), auth))
        job = self.get(jid)
        self.audit.write("comms.job_created", job_id=jid, channel=contact.channel, scheduled=bool(due_at), auth=auth)
        self._publish(job)
        if due_at:
            self._wake.set()
        return job

    def update(self, jid: str, **kw) -> dict:
        kw["updated_at"] = now()
        cols = ",".join(f"{k}=?" for k in kw)
        self.db.execute(f"UPDATE comm_jobs SET {cols} WHERE id=?", (*kw.values(), jid))
        job = self.get(jid)
        self._publish(job)
        return job

    def claim(self, jid: str) -> bool:
        """scheduled → sending, atomically. False if someone else already took it or it was cancelled."""
        cur = self.db.execute("UPDATE comm_jobs SET status='sending', attempts=attempts+1, updated_at=? "
                              "WHERE id=? AND status='scheduled'", (now(), jid))
        if cur.rowcount:
            self._publish(self.get(jid))
        return cur.rowcount == 1

    def cancel(self, jid: str, replaced_by: str | None = None) -> bool:
        cur = self.db.execute("UPDATE comm_jobs SET status='cancelled', replaced_by=?, updated_at=? "
                              "WHERE id=? AND status='scheduled'", (replaced_by, now(), jid))
        if cur.rowcount:
            self.audit.write("comms.job_cancelled", job_id=jid, replaced_by=replaced_by)
            self._publish(self.get(jid))
            self._wake.set()
        return cur.rowcount == 1

    def list(self, statuses: tuple[str, ...] | None = None, since: float | None = None, limit: int = 100) -> list[dict]:
        q, args = "SELECT * FROM comm_jobs WHERE 1=1", []
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        if since:
            q += " AND coalesce(sent_at, updated_at) >= ?"
            args.append(since)
        q += " ORDER BY coalesce(due_at, created_at) " + ("ASC" if statuses == ("scheduled",) else "DESC") + " LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.all(q, tuple(args))]

    def scheduled(self) -> list[dict]:
        return self.list(("scheduled",))

    def _publish(self, job: dict | None) -> None:
        if job:
            self.bus.publish("comms.job", {"job": job}, job.get("task_id"))

    # ---------------------------------------------------------------- spoken summaries
    @staticmethod
    def describe(job: dict, with_time: bool = True) -> str:
        to = {"telegram": "в Telegram", "whatsapp": "в WhatsApp", "email": "на почту"}.get(job["channel"], job["channel"])
        s = f"{job['to_name']} {to}"
        if with_time and job.get("due_at"):
            s += " " + when_ru(job["due_at"])
        return s + f": «{_short(job['body'])}»"

    # ---------------------------------------------------------------- executor
    def start(self) -> None:
        # Crash recovery: a job left in "sending" may or may not have left — never resend it blindly.
        for r in self.db.all("SELECT id FROM comm_jobs WHERE status='sending'"):
            self.update(r["id"], status="unknown",
                        last_error="JARVIS остановился во время отправки — проверьте в приложении, дошло ли сообщение")
        self._task = asyncio.create_task(self._loop(), name="comms-outbox")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            nxt = self.db.one("SELECT * FROM comm_jobs WHERE status='scheduled' ORDER BY due_at LIMIT 1")
            if nxt is None:
                await self._wake.wait()
                continue
            delay = nxt["due_at"] - now()
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), min(delay, 600))
                except asyncio.TimeoutError:
                    pass
                continue
            job = dict(nxt)
            if now() - job["due_at"] > LATE_OK_S:
                # Sending "Доброе утро" at 3 pm is worse than not sending: report it instead.
                cur = self.db.execute("UPDATE comm_jobs SET status='missed', last_error=?, updated_at=? "
                                      "WHERE id=? AND status='scheduled'",
                                      (f"JARVIS не работал в {time.strftime('%H:%M', time.localtime(job['due_at']))} — "
                                       "сообщение не отправлено", now(), job["id"]))
                if cur.rowcount:
                    self._publish(self.get(job["id"]))
                    self.audit.write("comms.job_missed", job_id=job["id"])
                    if self.on_missed:
                        self.on_missed(self.get(job["id"]))
                continue
            if not self.claim(job["id"]):
                continue
            try:
                await self.deliver(self.get(job["id"]))
            except asyncio.CancelledError:
                raise
            except Exception as e:   # noqa: BLE001 — the loop must survive any delivery bug
                self.update(job["id"], status="unknown", last_error="Внутренняя ошибка при отправке",
                            error_detail=repr(e)[:500])
                self.audit.write("comms.deliver_crash", job_id=job["id"], error=repr(e)[:300])

    def reschedule_retry(self, job: dict, error: str, detail: str) -> bool:
        """Put a job that certainly did not leave back in the queue, with a bounded number of attempts."""
        n = job["attempts"]
        if n > len(RETRY_DELAYS):
            return False
        self.db.execute("UPDATE comm_jobs SET status='scheduled', due_at=?, last_error=?, error_detail=?, updated_at=? "
                        "WHERE id=? AND status='sending'", (now() + RETRY_DELAYS[n - 1], error, detail[:500], now(),
                                                             job["id"]))
        self._publish(self.get(job["id"]))
        self._wake.set()
        return True


def _short(s: str, n: int = 70) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def channel_ru(ch: str) -> str:
    return CHANNEL_RU.get(ch, ch)
