"""CommsHub: the provider registry plus everything the tools and the agent share."""
from __future__ import annotations

import asyncio

from ..db import Database, new_id, now
from ..events import AuditLog, EventBus
from .base import CHANNEL_TO, CommsError, Contact, Provider, Receipt
from .contacts import AddressBook
from .outbox import Outbox


class CommsHub:
    def __init__(self, db: Database, bus: EventBus, audit: AuditLog, providers: list[Provider]):
        self.db, self.bus, self.audit = db, bus, audit
        self.providers = {p.id: p for p in providers}
        self.outbox = Outbox(db, bus, audit)
        self.book = AddressBook(db)

    def provider(self, channel: str) -> Provider:
        p = self.providers.get(channel)
        if not p:
            raise CommsError("Этот сервис не подключается к JARVIS", "unsupported")
        return p

    def describe(self) -> list[dict]:
        return [p.describe() for p in self.providers.values()]

    def publish_status(self) -> None:
        self.bus.publish("comms.providers", {"providers": self.describe()})

    async def start(self) -> None:
        await asyncio.gather(*(p.start() for p in self.providers.values()), return_exceptions=True)
        self.outbox.start()
        self.publish_status()

    async def stop(self) -> None:
        await self.outbox.stop()
        await asyncio.gather(*(p.stop() for p in self.providers.values()), return_exceptions=True)

    def vocabulary(self) -> list[str]:
        """First names the user actually messaged or saved (not the whole contact list) — a hint for speech recognition."""
        rows = self.db.all("SELECT to_name AS n FROM comm_jobs WHERE created_at > ? GROUP BY to_name "
                           "ORDER BY max(created_at) DESC LIMIT 15", (now() - 60 * 86400,))
        names = [r["n"].split()[0] for r in rows if r["n"] and r["n"][0].isalpha()]
        names += [c["name"].split()[0] for c in self.book.all()]
        out = []
        for n in names:
            if n not in out and len(n) > 1:
                out.append(n)
        return out[:15]

    # ---------------------------------------------------------------- trusted-automation rules
    def rules(self) -> list[dict]:
        return [dict(r) for r in self.db.all("SELECT * FROM trust_rules WHERE revoked_at IS NULL ORDER BY created_at")]

    def rule_for(self, channel: str, to_id: str) -> dict | None:
        r = self.db.one("SELECT * FROM trust_rules WHERE channel=? AND to_id=? AND revoked_at IS NULL", (channel, to_id))
        return dict(r) if r else None

    def add_rule(self, channel: str, to_id: str, to_name: str) -> dict:
        r = self.rule_for(channel, to_id)
        if r:
            return r
        rid = new_id("tr_")
        self.db.execute("INSERT INTO trust_rules(id,channel,to_id,to_name,created_at) VALUES(?,?,?,?,?)",
                        (rid, channel, to_id, to_name, now()))
        self.audit.write("comms.rule_added", rule_id=rid, channel=channel)
        self.bus.publish("comms.rules", {"rules": self.rules()})
        return self.rule_for(channel, to_id)

    def revoke_rule(self, rid: str) -> bool:
        n = self.db.execute("UPDATE trust_rules SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (now(), rid)).rowcount
        if n:
            self.audit.write("comms.rule_revoked", rule_id=rid)
            self.bus.publish("comms.rules", {"rules": self.rules()})
        return n > 0

    def policy_hook(self, spec, args):
        """Policy extension: an explicit user rule may answer the confirmation for a plain send/schedule."""
        from ..policy import Decision
        if spec.id != "message_send" or args.get("replaces"):
            return None
        rules = [self.rule_for(d["channel"], d["to_id"]) for d in args.get("deliveries") or []]
        if rules and all(rules):
            names = ", ".join(f"{r['to_name']} {CHANNEL_TO[r['channel']]}" for r in rules)
            return Decision("allow", f"По вашему правилу без подтверждения: {names}")
        return None

    def auth_valid(self, job: dict) -> bool:
        kind, _, ref = (job.get("auth") or "").partition(":")
        if kind == "approval":
            r = self.db.one("SELECT status FROM approvals WHERE id=?", (ref,))
            return bool(r and r["status"] == "approved")
        if kind == "rule":
            return all(self.db.one("SELECT 1 FROM trust_rules WHERE id=? AND revoked_at IS NULL", (x,))
                       for x in ref.split(",") if x)
        return False

    # ---------------------------------------------------------------- delivery
    async def send_job(self, job: dict) -> dict:
        """Send one claimed job (status 'sending') and record the outcome. Never raises for service errors."""
        p = self.provider(job["channel"])
        c = Contact(job["channel"], job["to_id"], job["to_name"], job.get("to_address") or "")
        who = f"{job['to_name']} {CHANNEL_TO.get(job['channel'], '')}"
        try:
            rc = await p.send(c, job["body"], job.get("subject"), job["id"])
        except CommsError as e:
            if e.retryable and job.get("due_at") and self.outbox.reschedule_retry(self.outbox.get(job["id"]), str(e), e.detail):
                return {"job": job["id"], "status": "retry", "error": f"{who}: {e} — повторю попытку позже",
                        "summary": f"{who}: не отправлено, повторю попытку позже"}
            status = "unknown" if e.kind == "unknown" else "failed"
            self.outbox.update(job["id"], status=status, last_error=str(e), error_detail=f"{e.kind}: {e.detail}"[:500])
            self.audit.write("comms.send_" + status, job_id=job["id"], channel=job["channel"], error_kind=e.kind)
            return {"job": job["id"], "status": status, "error": f"{who}: {e}", "summary": f"{who}: {e}"}
        except asyncio.CancelledError:
            self.outbox.update(job["id"], status="unknown", last_error="Отправка прервана — статус неизвестен")
            raise
        except Exception as e:   # noqa: BLE001
            self.outbox.update(job["id"], status="unknown", last_error="Сбой во время отправки — статус неизвестен",
                               error_detail=repr(e)[:500])
            self.audit.write("comms.send_unknown", job_id=job["id"], channel=job["channel"], error=repr(e)[:200])
            return {"job": job["id"], "status": "unknown", "error": f"{who}: сбой во время отправки, статус неизвестен",
                    "summary": f"{who}: статус неизвестен"}
        self.outbox.update(job["id"], status="sent", remote_id=rc.remote_id, sent_at=now(), last_error=None)
        self.audit.write("comms.sent", job_id=job["id"], channel=job["channel"])
        return {"job": job["id"], "status": "sent", "summary": f"{who}: отправлено", "detail": rc.detail}

    async def verify_jobs(self, jobs: list[dict]):
        from ..tools.base import Verification
        states, details = [], []
        for j in jobs:
            if not j:
                states.append("failed")
                continue
            if j["status"] != "sent":
                states.append("failed" if j["status"] in ("failed", "cancelled") else "unverified")
                details.append(f"{j['to_name']}: {j.get('last_error') or j['status']}")
                continue
            c = Contact(j["channel"], j["to_id"], j["to_name"], j.get("to_address") or "")
            try:
                st, d = await self.provider(j["channel"]).verify(c, j["body"], Receipt(j["remote_id"] or "", ""))
            except Exception as e:   # noqa: BLE001
                st, d = "unverified", "Проверка не удалась: " + e.__class__.__name__
            self.outbox.update(j["id"], verified=f"{st}: {d}"[:300])
            states.append(st)
            details.append(d)
        if states and all(s == "verified" for s in states):
            return Verification("verified", "; ".join(details))
        if states and all(s == "failed" for s in states):
            return Verification("failed", "; ".join(details) or "Не отправлено")
        return Verification("unverified", "; ".join(details) or "Часть сообщений не подтверждена")
