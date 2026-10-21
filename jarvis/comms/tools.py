"""Gateway tools for communication. None of them is callable by a model: the Communication agent builds
the exact arguments from a resolved recipient, and the Gateway asks the user before anything leaves."""
from __future__ import annotations


from ..db import new_id, now
from ..tools.base import Risk, Tool, ToolError, ToolInputError, ToolResult, ToolSpec, Verification
from .base import CHANNEL_TO, Contact, content_digest
from .outbox import _short, when_ru

DELIVERY = {"type": "object", "required": ["channel", "to_id", "to_name"], "properties": {
    "channel": {"type": "string", "enum": ["telegram", "whatsapp", "email"]},
    "to_id": {"type": "string", "minLength": 1, "maxLength": 300},
    "to_name": {"type": "string", "minLength": 1, "maxLength": 200},
    "to_address": {"type": "string", "maxLength": 300},
    "guess": {"type": "boolean"}}}      # recipient found by a similar, not exact, name: always asked


def _contact(d: dict) -> Contact:
    return Contact(d["channel"], d["to_id"], d["to_name"], d.get("to_address") or "")


def _validate_deliveries(v) -> list[dict]:
    from ..tools.base import validate_schema
    if not isinstance(v, list) or not 1 <= len(v) <= 10:
        raise ToolInputError("deliveries: от 1 до 10 получателей")
    out, seen = [], set()
    for i, d in enumerate(v):
        d = validate_schema(DELIVERY, d, f"deliveries[{i}]")
        key = (d["channel"], d["to_id"])
        if key in seen:
            raise ToolInputError("Один и тот же получатель указан дважды")
        seen.add(key)
        out.append(d)
    return out


class _CommsTool(Tool):
    def __init__(self, hub):
        self.hub = hub     # CommsHub: providers, outbox, address book, rules


class MessageSend(_CommsTool):
    """Send now, or put into the outbox for a time. One approval covers the whole action (several channels or
    recipients). The approval is bound to the exact recipients, text, subject and time."""

    def __init__(self, hub):
        super().__init__(hub)
        self.spec = ToolSpec(
            id="message_send", title="Отправка сообщения",
            description="Send or schedule a message through a connected service (internal; not model-callable).",
            params={"type": "object", "required": ["deliveries", "body"], "properties": {
                "deliveries": {"type": "array"},
                "body": {"type": "string", "minLength": 1, "maxLength": 4000},
                "subject": {"type": "string", "maxLength": 200},
                "due_at": {"type": "number"},
                "replaces": {"type": "array"}}},
            risk=Risk.HIGH, side_effects="external", timeout_s=90, idempotent=False, model_callable=False,
            verification="Сервис подтвердил сообщение (Telegram — по id сообщения, почта — приём сервером, "
                         "WhatsApp — отметка об отправке); запланированное — стоит в очереди",
            impact="Сообщение уйдёт от вашего имени. Отправленное отозвать нельзя.")

    def validate(self, args, ctx):
        from ..tools.base import validate_schema
        a = validate_schema(self.spec.params, args)
        a["deliveries"] = _validate_deliveries(a["deliveries"])
        if any(d["channel"] == "email" for d in a["deliveries"]) and not (a.get("subject") or "").strip():
            raise ToolInputError("У письма нет темы")
        if a.get("due_at") is not None:
            if a["due_at"] < now() - 60:
                raise ToolInputError("Время отправки уже прошло")
            if a["due_at"] > now() + 366 * 86400:
                raise ToolInputError("Можно запланировать не больше чем на год вперёд")
        for jid in a.get("replaces") or []:
            j = self.hub.outbox.get(str(jid))
            if not j or j["status"] != "scheduled":
                raise ToolInputError("Изменяемое сообщение уже не запланировано")
        for d in a["deliveries"]:
            p = self.hub.provider(d["channel"])
            if a.get("due_at") and not p.caps.get("can_schedule"):
                raise ToolInputError(f"{p.title}: отправка по расписанию не поддерживается")
        return a

    def describe(self, args):
        to = ", ".join(f"{d['to_name']}{' (' + d['to_address'] + ')' if d.get('to_address') else ''} "
                       f"{CHANNEL_TO[d['channel']]}" for d in args["deliveries"])
        when = f" — {when_ru(args['due_at'])}" if args.get("due_at") else ""
        subj = f" · тема «{args['subject']}»" if args.get("subject") else ""
        verb = "Изменить запланированное: " if args.get("replaces") else ("Запланировать: " if args.get("due_at") else "Отправить: ")
        return f"{verb}{to}{when}{subj}: «{_short(args['body'], 300)}»"

    def _auth(self, args, ctx) -> str:
        row = self.hub.db.one("SELECT id FROM approvals WHERE step_id=? AND status='approved' ORDER BY decided_at DESC "
                              "LIMIT 1", (ctx.step_id,)) if ctx.step_id else None
        if row:
            return "approval:" + row["id"]
        rules = [self.hub.rule_for(d["channel"], d["to_id"]) for d in args["deliveries"]]
        if all(rules):
            return "rule:" + ",".join(r["id"] for r in rules)
        if self.hub.full_access() and all(self.hub.is_self(d) for d in args["deliveries"]):
            return "full:self"
        raise ToolError("Нет подтверждения на эту отправку")

    async def run(self, args, ctx):
        auth = self._auth(args, ctx)
        group = new_id("g_")
        for jid in args.get("replaces") or []:
            if not self.hub.outbox.cancel(str(jid), replaced_by=group):
                raise ToolError("Изменяемое сообщение уже не запланировано — ничего не изменено")
        jobs = []
        due = args.get("due_at")
        if due is not None and due < now() + 5:
            due = None   # "in 0 minutes" is simply now
        for d in args["deliveries"]:
            jobs.append(self.hub.outbox.create(_contact(d), args["body"], args.get("subject"), due,
                                               task_id=ctx.task_id, step_id=ctx.step_id, auth=auth, group_id=group))
        if due:
            return ToolResult({"jobs": [j["id"] for j in jobs], "scheduled": True, "due_at": due},
                              f"Запланировано {when_ru(due)}: " + "; ".join(self.hub.outbox.describe(j, False) for j in jobs) + ".",
                              card="comms")
        results = []
        for j in jobs:
            if ctx.cancelled():
                self.hub.outbox.update(j["id"], status="cancelled", last_error="Отменено до отправки")
                continue
            results.append(await self.hub.send_job(j))
        sent = [r for r in results if r["status"] == "sent"]
        if not sent:
            raise ToolError("; ".join(r["error"] for r in results if r.get("error")) or "Не отправлено")
        return ToolResult({"jobs": [j["id"] for j in jobs], "scheduled": False, "results": results},
                          "; ".join(r["summary"] for r in results) + ".", card="comms")

    async def verify(self, args, result, ctx):
        jobs = [self.hub.outbox.get(j) for j in result.data["jobs"]]
        if result.data["scheduled"]:
            ok = all(j and j["status"] == "scheduled" for j in jobs)
            return Verification("verified" if ok else "failed",
                                "Сообщения стоят в очереди отправки" if ok else "Сообщение пропало из очереди")
        return await self.hub.verify_jobs(jobs)


class MessageDeliver(_CommsTool):
    """Executor of a scheduled job at its time. Authorised by the approval/rule recorded when it was scheduled;
    the content must still match exactly what was approved."""

    def __init__(self, hub):
        super().__init__(hub)
        self.spec = ToolSpec(
            id="message_deliver", title="Отправка по расписанию",
            description="Deliver a scheduled outbox job (internal).",
            params={"type": "object", "required": ["job_id"], "properties": {"job_id": {"type": "string", "maxLength": 40}}},
            risk=Risk.LOW, side_effects="external", timeout_s=90, idempotent=False, model_callable=False,
            verification="Сервис подтвердил сообщение",
            impact="Отправляет сообщение, которое вы подтвердили при планировании.")

    def validate(self, args, ctx):
        from ..tools.base import validate_schema
        a = validate_schema(self.spec.params, args)
        j = self.hub.outbox.get(a["job_id"])
        if not j or j["status"] != "sending":
            raise ToolInputError("Задание не готово к отправке (уже отправлено, отменено или не найдено)")
        if j["digest"] != content_digest(j["channel"], j["to_id"], j["subject"], j["body"]):
            raise ToolInputError("Текст или получатель изменились после подтверждения")
        if not self.hub.auth_valid(j):
            raise ToolInputError("Подтверждение на эту отправку отозвано или недействительно")
        return a

    def describe(self, args):
        j = self.hub.outbox.get(args["job_id"])
        return "Отправить по расписанию: " + (self.hub.outbox.describe(j, False) if j else args["job_id"])

    async def run(self, args, ctx):
        j = self.hub.outbox.get(args["job_id"])
        r = await self.hub.send_job(j)
        if r["status"] != "sent":
            raise ToolError(r["error"])
        return ToolResult({"jobs": [j["id"]], "results": [r]}, r["summary"] + ".", card="comms")

    async def verify(self, args, result, ctx):
        return await self.hub.verify_jobs([self.hub.outbox.get(args["job_id"])])


class MessageCancel(_CommsTool):
    def __init__(self, hub):
        super().__init__(hub)
        self.spec = ToolSpec(
            id="message_cancel", title="Отмена запланированного сообщения",
            description="Cancel scheduled outbox jobs (internal).",
            params={"type": "object", "required": ["job_ids"], "properties": {"job_ids": {"type": "array"}}},
            risk=Risk.LOW, side_effects="local", timeout_s=5, model_callable=False,
            verification="Задание в очереди помечено как отменённое")

    def validate(self, args, ctx):
        from ..tools.base import validate_schema
        a = validate_schema(self.spec.params, args)
        if not a["job_ids"] or len(a["job_ids"]) > 20:
            raise ToolInputError("Не указано, что отменять")
        for jid in a["job_ids"]:
            j = self.hub.outbox.get(str(jid))
            if not j or j["status"] != "scheduled":
                raise ToolInputError("Это сообщение уже не запланировано")
        return a

    def describe(self, args):
        return "Отменить: " + "; ".join(self.hub.outbox.describe(self.hub.outbox.get(j)) for j in args["job_ids"])

    async def run(self, args, ctx):
        done = [j for j in args["job_ids"] if self.hub.outbox.cancel(str(j))]
        if not done:
            raise ToolError("Сообщение уже не запланировано")
        jobs = [self.hub.outbox.get(j) for j in done]
        return ToolResult({"jobs": done}, "Отменено: " + "; ".join(self.hub.outbox.describe(j) for j in jobs) + ".",
                          card="comms")

    async def verify(self, args, result, ctx):
        ok = all(self.hub.outbox.get(j)["status"] == "cancelled" for j in result.data["jobs"])
        return Verification("verified" if ok else "failed", self.spec.verification if ok else "Не отменилось")


class TrustRuleAdd(_CommsTool):
    def __init__(self, hub):
        super().__init__(hub)
        self.spec = ToolSpec(
            id="trust_rule_add", title="Правило: отправлять без подтверждения",
            description="Create a trusted-automation rule (internal).",
            params={"type": "object", "required": ["channel", "to_id", "to_name"], "properties": DELIVERY["properties"]},
            risk=Risk.HIGH, side_effects="local", timeout_s=5, model_callable=False,
            verification="Правило записано и видно в разделе «Сообщения»",
            impact="Сообщения этому получателю в этом сервисе будут уходить без вопроса, пока вы не отмените правило.")

    def describe(self, args):
        return f"Больше не спрашивать подтверждение для сообщений: {args['to_name']} {CHANNEL_TO[args['channel']]}"

    async def run(self, args, ctx):
        r = self.hub.add_rule(args["channel"], args["to_id"], args["to_name"])
        return ToolResult({"rule": r}, f"Готово: сообщения {args['to_name']} {CHANNEL_TO[args['channel']]} буду "
                                       "отправлять без подтверждения. Отменить можно в разделе «Сообщения».")

    async def verify(self, args, result, ctx):
        ok = self.hub.rule_for(args["channel"], args["to_id"]) is not None
        return Verification("verified" if ok else "failed", self.spec.verification)


class ContactSave(_CommsTool):
    def __init__(self, hub):
        super().__init__(hub)
        self.spec = ToolSpec(
            id="contact_save", title="Контакт в адресной книге",
            description="Save a contact to JARVIS's local address book (internal).",
            params={"type": "object", "required": ["name"], "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 120},
                "email": {"type": "string", "maxLength": 200}, "phone": {"type": "string", "maxLength": 40},
                "telegram": {"type": "string", "maxLength": 100}}},
            risk=Risk.LOW, side_effects="local", timeout_s=3, model_callable=False,
            verification="Контакт записан в адресную книгу JARVIS")

    def describe(self, args):
        parts = [args.get("email"), args.get("phone"), args.get("telegram")]
        return f"Запомнить контакт: {args['name']} — " + ", ".join(p for p in parts if p)

    async def run(self, args, ctx):
        try:
            c = self.hub.book.save(args["name"], args.get("email"), args.get("phone"), args.get("telegram"))
        except ValueError as e:
            raise ToolError(str(e)) from None
        details = ", ".join(x for x in (c["email"], c["phone"], c["telegram"]) if x)
        return ToolResult({"contact": c}, f"Запомнил: {c['name']} — {details}.")

    async def verify(self, args, result, ctx):
        ok = self.hub.db.one("SELECT 1 FROM contacts WHERE id=?", (result.data["contact"]["id"],)) is not None
        return Verification("verified" if ok else "failed", self.spec.verification)
