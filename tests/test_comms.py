"""Communication agent: Telegram / WhatsApp / e-mail through one abstraction, confirmation, outbox, scheduling.
Providers here are test doubles (VERIFIED MOCK); real-service checks live in test_comms_live.py."""
import asyncio
import datetime as dt
import time

import pytest

from jarvis.comms.base import CommsError, Contact, Provider, Receipt, Status
from jarvis.comms.contacts import match_score
from jarvis.voice.speech import SpeechManager

from conftest import wait_done, wait_for


class FakeMessenger(Provider):
    def __init__(self, pid, people, connected=True):
        self.id = pid
        self.title = {"telegram": "Telegram", "whatsapp": "WhatsApp", "email": "Почта"}[pid]
        self.caps = {"can_send_text": True, "can_schedule": True, "can_search_contacts": True}
        self.people = people
        self.on = connected
        self.sent, self.fail = [], None

    def status(self):
        return Status("connected", account="test") if self.on else Status("not_connected", requirement="Подключите тест")

    async def search(self, name):
        return [Contact(self.id, pid, nm, addr, "test", match_score(name, nm)) for pid, nm, addr in self.people
                if match_score(name, nm)]

    async def send(self, contact, body, subject, op_id):
        if not self.on:
            raise CommsError(f"{self.title} не подключён", "not_connected", retryable=True)
        if self.fail:
            raise self.fail
        self.sent.append((contact.id, body, subject, op_id))
        return Receipt(f"r{len(self.sent)}", "принято")

    async def verify(self, contact, body, receipt):
        ok = any(s[0] == contact.id and s[1] == body for s in self.sent)
        return ("verified", "есть в чате") if ok else ("failed", "нет в чате")


class Intents:
    """Stands in for Gemini's structured call: returns scripted intents and records what it was given."""

    def __init__(self, *intents):
        self.queue, self.seen = list(intents), []
        self.last_structured_model = "test-lite"

    async def structured(self, system, user, schema, **kw):
        self.seen.append(user)
        d = self.queue.pop(0)
        base = {"recipients": [], "message": "", "subject": "", "when": ""}
        return {**base, **d}


class FakeTTS:
    name = "fake"
    cache = {}

    def __init__(self):
        self.calls = []

    async def synth(self, text):
        self.calls.append(text)
        return b"RIFF" + text.encode()

    async def prewarm(self):
        pass

    async def stop(self):
        pass


PEOPLE = {"telegram": [("u:1:11", "Иван Петров", "@ivanp"), ("u:2:22", "Иван Сидоров", "@sid"),
                       ("u:3:33", "Сергей Орлов", "@orlov"), ("u:4:44", "Алина Смирнова", "@alina")],
          "whatsapp": [("phone:79990001122", "Иван Петров", "+79990001122"), ("chat:Алина", "Алина", "чат WhatsApp")],
          "email": [("sergey@example.com", "Сергей Орлов", "sergey@example.com")]}


@pytest.fixture
def hub(core):
    h = core.comms
    h.providers = {pid: FakeMessenger(pid, PEOPLE[pid]) for pid in ("telegram", "whatsapp", "email")}
    core.director.speech = SpeechManager(core.db, core.bus, FakeTTS())
    return h


def iso(minutes=60):
    return (dt.datetime.now() + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")


def intents(core, *items):
    """Scripted Gemini intents: these tests exercise the Gemini path, so the instant local reading is switched off."""
    core.director.comms.gemini = Intents(*items)
    core.director.comms.local_first = False
    return core.director.comms.gemini


def last_text(core):
    return core.director.messages(core.director.conversation_id())[-1]["text"]


async def pending_send(core):
    await wait_for(lambda: [a for a in core.approvals.pending() if a["tool_id"] == "message_send"])
    return [a for a in core.approvals.pending() if a["tool_id"] == "message_send"][0]


# ---------------------------------------------------------------- milestone 1: voice → Telegram → schedule → real send
async def test_voice_schedule_telegram_then_executor_sends_at_time(core, hub):
    g = intents(core, {"action": "send", "recipients": [{"name": "Иван Петров", "platform": "telegram"}],
                       "message": "Привет, буду через час.", "when": iso(90)})
    tid = await core.director.submit("Джарвис, отправь Ивану Петрову в Telegram в 13:00: Привет, буду через час.", "voice")
    a = await pending_send(core)
    assert a["args"]["deliveries"][0]["to_id"] == "u:1:11" and a["args"]["due_at"] > time.time()
    assert g.seen == ["Джарвис, отправь Ивану Петрову в Telegram в 13:00: Привет, буду через час."]   # only the command
    tts = core.director.speech.engine
    await wait_for(lambda: any(c.endswith("Запланировать?") for c in tts.calls))   # the question is spoken
    assert not hub.providers["telegram"].sent
    await core.director.submit("Да.", "voice")
    await wait_done(core, tid)
    assert last_text(core).startswith("Готово. Запланировал")
    await wait_for(lambda: any("Запланировал" in c for c in tts.calls))             # the result is spoken too
    job = hub.outbox.scheduled()[0]
    assert job["status"] == "scheduled" and job["auth"].startswith("approval:")
    assert not hub.providers["telegram"].sent                                         # nothing sent before the time
    # The time arrives.
    core.db.execute("UPDATE comm_jobs SET due_at=? WHERE id=?", (time.time() - 1, job["id"]))
    hub.outbox.start()
    try:
        await wait_for(lambda: hub.outbox.get(job["id"])["status"] == "sent", 5)
        await wait_for(lambda: "отправлено" in last_text(core).lower())
    finally:
        await hub.outbox.stop()
    assert hub.providers["telegram"].sent == [("u:1:11", "Привет, буду через час.", None, job["id"])]
    assert last_text(core) == "Отправлено по расписанию: Иван Петров, Telegram."
    assert hub.outbox.get(job["id"])["verified"].startswith("verified")


async def test_claim_prevents_double_send_and_crash_is_never_resent(core, hub):
    c = Contact("telegram", "u:1:11", "Иван Петров")
    j = hub.outbox.create(c, "Привет", None, time.time() - 1, task_id="t", step_id=None, auth="approval:x")
    assert hub.outbox.claim(j["id"]) is True
    assert hub.outbox.claim(j["id"]) is False          # second executor / second loop iteration
    # JARVIS crashed while the job was "sending": on start it becomes "unknown", never re-sent.
    hub.outbox.start()
    await hub.outbox.stop()
    assert hub.outbox.get(j["id"])["status"] == "unknown"
    assert not hub.providers["telegram"].sent


async def test_missed_while_off_is_reported_not_sent(core, hub):
    c = Contact("telegram", "u:1:11", "Иван Петров")
    j = hub.outbox.create(c, "Доброе утро", None, time.time() - 3600, task_id="t", step_id=None, auth="approval:x")
    hub.outbox.start()
    try:
        await wait_for(lambda: hub.outbox.get(j["id"])["status"] == "missed")
    finally:
        await hub.outbox.stop()
    assert not hub.providers["telegram"].sent
    assert "не отправлено" in last_text(core)


async def test_scheduled_jobs_survive_restart(core, hub, cfg):
    from jarvis.app import build
    from jarvis.config import Paths
    c = Contact("telegram", "u:1:11", "Иван Петров")
    j = hub.outbox.create(c, "Привет", None, time.time() + 3600, task_id="t", step_id=None, auth="approval:x")
    core2 = build(Paths.detect(), cfg)
    try:
        assert core2.comms.outbox.get(j["id"])["status"] == "scheduled"
    finally:
        core2.db.close()


# ---------------------------------------------------------------- milestone 2: WhatsApp immediately
async def test_whatsapp_immediate_send_after_yes(core, hub):
    intents(core, {"action": "send", "recipients": [{"name": "Иван", "platform": "whatsapp"}], "message": "Привет."})
    tid = await core.director.submit("Джарвис, отправь Ивану в WhatsApp: Привет.", "voice")
    a = await pending_send(core)
    assert "WhatsApp" in a["summary"]["action"] and "Привет." in a["summary"]["action"]
    await core.director.submit("да", "voice")
    await wait_done(core, tid)
    assert hub.providers["whatsapp"].sent[0][:2] == ("phone:79990001122", "Привет.")
    assert last_text(core) == "Готово. Отправил: Иван Петров, WhatsApp."
    step = core.tasks.get(tid)["steps"][-1]
    assert step["verification"]["status"] == "verified"


# ---------------------------------------------------------------- milestone 3: e-mail with a generated subject
async def test_email_with_subject_and_text_confirmation(core, hub):
    intents(core, {"action": "send", "recipients": [{"name": "Сергей", "platform": "email"}],
                   "message": "Сергей, документы готовы.", "subject": "Документы готовы"})
    tid = await core.director.submit("Напиши Сергею на почту, что документы готовы.")
    a = await pending_send(core)
    assert a["args"]["subject"] == "Документы готовы"
    await core.director.submit("Да")                     # typed "да" in the window works too
    await wait_done(core, tid)
    assert hub.providers["email"].sent[0][:3] == ("sergey@example.com", "Сергей, документы готовы.", "Документы готовы")
    assert last_text(core) == "Готово. Отправил: Сергей Орлов, почта."


# ---------------------------------------------------------------- honesty and safety
async def test_not_connected_is_reported_and_nothing_is_faked(core, hub):
    hub.providers["telegram"].on = False
    intents(core, {"action": "send", "recipients": [{"name": "Иван Петров", "platform": "telegram"}], "message": "Привет"})
    tid = await core.director.submit("Отправь Ивану Петрову в Telegram: Привет")
    await wait_done(core, tid)
    assert "не подключён" in last_text(core) and "Подключите тест" in last_text(core)
    assert not core.approvals.pending() and not hub.outbox.list()


async def test_provider_failure_is_never_called_sent(core, hub):
    hub.providers["telegram"].fail = CommsError("Telegram не позволяет написать этому получателю", "rejected", "403")
    intents(core, {"action": "send", "recipients": [{"name": "Иван Петров", "platform": "telegram"}], "message": "Привет"})
    tid = await core.director.submit("Отправь Ивану Петрову в Telegram: Привет")
    await pending_send(core)
    await core.director.submit("да")
    await wait_done(core, tid)
    assert last_text(core).startswith("Не отправлено") and "не позволяет" in last_text(core)
    job = hub.outbox.list()[0]
    assert job["status"] == "failed" and "403" in job["error_detail"]


async def test_no_means_nothing_is_sent(core, hub):
    intents(core, {"action": "send", "recipients": [{"name": "Иван Петров", "platform": "telegram"}], "message": "Привет"})
    tid = await core.director.submit("Отправь Ивану Петрову в Telegram: Привет", "voice")
    await pending_send(core)
    await core.director.submit("Нет.", "voice")
    await wait_done(core, tid)
    assert last_text(core) == "Хорошо, не отправляю." and not hub.providers["telegram"].sent and not hub.outbox.list()


async def test_yes_from_the_api_does_not_approve(core, hub):
    intents(core, {"action": "send", "recipients": [{"name": "Иван Петров", "platform": "telegram"}], "message": "Привет"},
            {"action": "other"})
    tid = await core.director.submit("Отправь Ивану Петрову в Telegram: Привет")
    a = await pending_send(core)
    core.director.agency.run = _noop
    await core.director.submit("да", "api")              # bearer API / CLI cannot answer a confirmation
    assert core.approvals.get(a["id"])["status"] == "pending" and not hub.providers["telegram"].sent
    core.director.decide(a["id"], False)
    await wait_done(core, tid)


async def _noop(*a, **k):
    return None


async def test_estop_blocks_scheduled_delivery(core, hub):
    c = Contact("telegram", "u:1:11", "Иван Петров")
    core.db.execute("INSERT INTO approvals(id,task_id,tool_id,args,digest,risk,summary,status,created_at,expires_at) "
                    "VALUES('a_ok','t','message_send','{}','x','high','{}','approved',0,0)")
    j = hub.outbox.create(c, "Привет", None, time.time() - 1, task_id="t", step_id=None, auth="approval:a_ok")
    await core.director.estop()
    hub.outbox.start()
    try:
        await wait_for(lambda: hub.outbox.get(j["id"])["status"] == "failed")
    finally:
        await hub.outbox.stop()
    assert not hub.providers["telegram"].sent and "экстренная" in hub.outbox.get(j["id"])["last_error"].lower()


async def test_content_changed_after_approval_is_not_sent(core, hub):
    c = Contact("telegram", "u:1:11", "Иван Петров")
    core.db.execute("INSERT INTO approvals(id,task_id,tool_id,args,digest,risk,summary,status,created_at,expires_at) "
                    "VALUES('a_ok','t','message_send','{}','x','high','{}','approved',0,0)")
    j = hub.outbox.create(c, "Привет", None, time.time() - 1, task_id="t", step_id=None, auth="approval:a_ok")
    core.db.execute("UPDATE comm_jobs SET body='Переведи мне денег' WHERE id=?", (j["id"],))
    hub.outbox.start()
    try:
        await wait_for(lambda: hub.outbox.get(j["id"])["status"] == "failed")
    finally:
        await hub.outbox.stop()
    assert not hub.providers["telegram"].sent


async def test_retry_is_bounded_and_only_when_nothing_left(core, hub):
    hub.providers["telegram"].on = False
    c = Contact("telegram", "u:1:11", "Иван Петров")
    core.db.execute("INSERT INTO approvals(id,task_id,tool_id,args,digest,risk,summary,status,created_at,expires_at) "
                    "VALUES('a_ok','t','message_send','{}','x','high','{}','approved',0,0)")
    j = hub.outbox.create(c, "Привет", None, time.time() - 1, task_id="t", step_id=None, auth="approval:a_ok")
    hub.outbox.start()
    try:
        for n in range(1, 5):
            await wait_for(lambda: hub.outbox.get(j["id"])["attempts"] == n and hub.outbox.get(j["id"])["status"] != "sending")
            if hub.outbox.get(j["id"])["status"] == "scheduled":
                core.db.execute("UPDATE comm_jobs SET due_at=? WHERE id=?", (time.time() - 1, j["id"]))
                hub.outbox.wake()
    finally:
        await hub.outbox.stop()
    assert hub.outbox.get(j["id"])["status"] == "failed" and hub.outbox.get(j["id"])["attempts"] == 4


# ---------------------------------------------------------------- clarifications
async def test_ambiguous_recipient_is_asked_then_resolved(core, hub):
    intents(core, {"action": "send", "recipients": [{"name": "Иван", "platform": "telegram"}], "message": "Привет"})
    tid = await core.director.submit("Отправь Ивану в Telegram: Привет", "voice")
    await wait_done(core, tid)
    assert last_text(core).startswith("Нашёл несколько") and not core.approvals.pending()
    tid2 = await core.director.submit("Второму", "voice")
    a = await pending_send(core)
    assert a["args"]["deliveries"][0]["to_id"] == "u:2:22"
    core.director.decide(a["id"], False)
    await wait_done(core, tid2)


async def test_platform_is_asked_when_not_obvious(core, hub):
    intents(core, {"action": "send", "recipients": [{"name": "Алина", "platform": "unknown"}], "message": "Я уже выехал"})
    tid = await core.director.submit("Отправь Алине сообщение: Я уже выехал")
    await wait_done(core, tid)
    assert "Telegram, WhatsApp или на почту" in last_text(core)
    tid2 = await core.director.submit("В ватсап")
    a = await pending_send(core)
    assert a["args"]["deliveries"][0] == {"channel": "whatsapp", "to_id": "chat:Алина", "to_name": "Алина",
                                          "to_address": "чат WhatsApp"}
    core.director.decide(a["id"], False)
    await wait_done(core, tid2)


async def test_this_person_uses_the_last_contact(core, hub):
    intents(core, {"action": "send", "recipients": [{"name": "Сергей", "platform": "telegram"}], "message": "Привет"},
            {"action": "send", "recipients": [{"name": "__last__", "platform": "unknown"}], "message": ""})
    t1 = await core.director.submit("Отправь Сергею в Telegram: Привет")
    core.director.decide((await pending_send(core))["id"], True)
    await wait_done(core, t1)
    t2 = await core.director.submit("Отправь этому человеку сообщение.")
    await wait_done(core, t2)
    assert last_text(core) == "Что написать Сергей Орлов?"
    t3 = await core.director.submit("Перезвони мне вечером")
    a = await pending_send(core)
    assert a["args"]["deliveries"][0]["to_id"] == "u:3:33" and a["args"]["body"] == "Перезвони мне вечером"
    core.director.decide(a["id"], False)
    await wait_done(core, t3)


async def test_multi_channel_needs_one_confirmation(core, hub):
    intents(core, {"action": "send", "recipients": [{"name": "Иван Петров", "platform": "telegram"},
                                                     {"name": "Иван Петров", "platform": "whatsapp"}],
                   "message": "Я уже приехал."})
    tid = await core.director.submit("Отправь Ивану Петрову в Telegram и WhatsApp одно и то же сообщение: Я уже приехал.")
    a = await pending_send(core)
    assert len(a["args"]["deliveries"]) == 2
    core.director.decide(a["id"], True)
    await wait_done(core, tid)
    assert hub.providers["telegram"].sent and hub.providers["whatsapp"].sent
    assert len(core.approvals.pending()) == 0


async def test_one_ambiguous_recipient_blocks_the_whole_send(core, hub):
    intents(core, {"action": "send", "recipients": [{"name": "Сергей", "platform": "telegram"},
                                                     {"name": "Иван", "platform": "telegram"}],
                   "message": "Встреча переносится."})
    tid = await core.director.submit("Отправь Сергею и Ивану в Telegram: Встреча переносится.")
    await wait_done(core, tid)
    assert last_text(core).startswith("Нашёл несколько") and not hub.providers["telegram"].sent
    tid2 = await core.director.submit("Петрову")
    a = await pending_send(core)
    assert [d["to_id"] for d in a["args"]["deliveries"]] == ["u:3:33", "u:1:11"]
    core.director.decide(a["id"], False)
    await wait_done(core, tid2)


# ---------------------------------------------------------------- cancel / edit / status
async def _schedule(core, hub, name_id, name, body, minutes):
    c = Contact("telegram", name_id, name)
    return hub.outbox.create(c, body, None, time.time() + minutes * 60, task_id="t", step_id=None, auth="approval:x")


async def test_cancel_one_and_ask_when_several_match(core, hub):
    j1 = await _schedule(core, hub, "u:1:11", "Иван Петров", "Привет", 60)
    j2 = await _schedule(core, hub, "u:1:11", "Иван Петров", "Доброе утро", 600)
    j3 = await _schedule(core, hub, "u:4:44", "Алина Смирнова", "Пока", 60)
    intents(core, {"action": "cancel", "target": {"name": "Алина"}},
            {"action": "cancel", "target": {"name": "Иван"}})
    t = await core.director.submit("Отмени сообщение Алине")
    await wait_done(core, t)
    assert hub.outbox.get(j3["id"])["status"] == "cancelled" and last_text(core).startswith("Отменено")
    t = await core.director.submit("Отмени сообщение Ивану")
    await wait_done(core, t)
    assert last_text(core).startswith("Какое сообщение отменить?")
    assert hub.outbox.get(j1["id"])["status"] == hub.outbox.get(j2["id"])["status"] == "scheduled"
    t = await core.director.submit("Второе")
    await wait_done(core, t)
    assert hub.outbox.get(j2["id"])["status"] == "cancelled" and hub.outbox.get(j1["id"])["status"] == "scheduled"


async def test_edit_scheduled_message_needs_a_new_confirmation(core, hub):
    j = await _schedule(core, hub, "u:1:11", "Иван Петров", "Привет", 60)
    intents(core, {"action": "edit", "target": {"name": "Иван"}, "changes": {"message": "Буду через два часа"}})
    t = await core.director.submit("Измени сообщение Ивану: буду через два часа")
    a = await pending_send(core)
    assert a["args"]["replaces"] == [j["id"]] and a["args"]["body"] == "Буду через два часа"
    assert hub.outbox.get(j["id"])["status"] == "scheduled"           # untouched until confirmed
    core.director.decide(a["id"], True)
    await wait_done(core, t)
    assert hub.outbox.get(j["id"])["status"] == "cancelled"
    new = hub.outbox.scheduled()[0]
    assert new["body"] == "Буду через два часа" and abs(new["due_at"] - j["due_at"]) < 1


async def test_status_answers_are_spoken_summaries(core, hub):
    await _schedule(core, hub, "u:1:11", "Иван Петров", "Привет", 60)
    await _schedule(core, hub, "u:4:44", "Алина Смирнова", "Доброе утро", 60 * 20)
    intents(core, {"action": "list_scheduled"})
    t = await core.director.submit("Какие сообщения у меня запланированы?", "voice")
    await wait_done(core, t)
    assert last_text(core).startswith("Запланировано два сообщения: Иван Петров в Telegram")
    assert " и Алина Смирнова в Telegram" in last_text(core)


# ---------------------------------------------------------------- revise in the approval card
async def test_revising_the_text_asks_again(core, hub):
    intents(core, {"action": "send", "recipients": [{"name": "Иван Петров", "platform": "telegram"}], "message": "Привт"})
    tid = await core.director.submit("Отправь Ивану Петрову в Telegram: Привт")
    a = await pending_send(core)
    await core.director.comms.revise(core.director, a["id"], "Привет!", None)
    await wait_for(lambda: [x for x in core.approvals.pending() if x["id"] != a["id"]])
    a2 = core.approvals.pending()[0]
    assert a2["args"]["body"] == "Привет!" and core.approvals.get(a["id"])["status"] == "expired"
    core.director.decide(a2["id"], True)
    await wait_done(core, tid)
    assert hub.providers["telegram"].sent[0][1] == "Привет!"


# ---------------------------------------------------------------- trusted automation
async def test_trust_rule_skips_confirmation_until_revoked(core, hub):
    intents(core, {"action": "trust", "recipients": [{"name": "Иван Петров", "platform": "telegram"}]},
            {"action": "send", "recipients": [{"name": "Иван Петров", "platform": "telegram"}], "message": "Привет"},
            {"action": "send", "recipients": [{"name": "Иван Петров", "platform": "telegram"}], "message": "Ещё раз"})
    t = await core.director.submit("Для сообщений Ивану Петрову в Telegram подтверждение больше не спрашивай")
    await wait_for(lambda: core.approvals.pending())
    a = core.approvals.pending()[0]
    assert a["tool_id"] == "trust_rule_add"
    await core.director.submit("да", "voice")                  # a rule needs a click, not a spoken "да"
    assert core.approvals.get(a["id"])["status"] == "pending"
    core.director.decide(a["id"], True)
    await wait_done(core, t)
    rule = hub.rules()[0]
    t = await core.director.submit("Отправь Ивану Петрову в Telegram: Привет")
    await wait_done(core, t)
    assert hub.providers["telegram"].sent and hub.outbox.list()[0]["auth"] == "rule:" + rule["id"]
    hub.revoke_rule(rule["id"])
    t = await core.director.submit("Отправь Ивану Петрову в Telegram: Ещё раз")
    a = await pending_send(core)                              # asks again after revoking
    core.director.decide(a["id"], False)
    await wait_done(core, t)


async def test_wake_word_alone_answers_yes(core, hub):
    t = await core.director.submit("Джарвис.", "voice")
    await wait_done(core, t)
    assert last_text(core) == "Да?"


async def test_new_command_replaces_an_unanswered_question(core, hub):
    intents(core, {"action": "send", "recipients": [{"name": "Иван Петров", "platform": "telegram"}], "message": "Первое"},
            {"action": "send", "recipients": [{"name": "Алина", "platform": "whatsapp"}], "message": "Второе"})
    t1 = await core.director.submit("Отправь Ивану Петрову в Telegram: Первое", "voice")
    a1 = await pending_send(core)
    t2 = await core.director.submit("Отправь Алине в WhatsApp: Второе", "voice")
    await wait_done(core, t1)
    assert core.approvals.get(a1["id"])["status"] == "expired"
    a2 = await pending_send(core)
    assert a2["args"]["body"] == "Второе"
    await core.director.submit("Да.", "voice")          # unambiguous: only one open question
    await wait_done(core, t2)
    assert hub.providers["whatsapp"].sent and not hub.providers["telegram"].sent


def test_spoken_question_is_clear_without_declension(core, hub):
    q = core.director.comms.question({"deliveries": [{"channel": "email", "to_id": "s@x.ru", "to_name": "Сергей Орлов"}],
                                      "body": "Документы готовы.", "subject": "Документы готовы"})
    assert q == "Кому: Сергей Орлов, почта. Тема: «Документы готовы». Текст: «Документы готовы». Отправить?"


async def test_cancel_message_to_self(core, hub):
    c = Contact("telegram", "self", "Избранное (вы сами)")
    j = hub.outbox.create(c, "тест", None, time.time() + 600, task_id="t", step_id=None, auth="approval:x")
    intents(core, {"action": "cancel", "target": {"name": "себе"}})
    t = await core.director.submit("Отмени сообщение себе")
    await wait_done(core, t)
    assert hub.outbox.get(j["id"])["status"] == "cancelled"


async def test_relative_time_is_exact_not_rounded(core, hub):
    intents(core, {"action": "send", "recipients": [{"name": "Иван Петров", "platform": "telegram"}],
                   "message": "Через минуту", "in_minutes": 1})
    t0 = time.time()
    tid = await core.director.submit("Отправь Ивану Петрову в Telegram через 1 минуту: Через минуту")
    a = await pending_send(core)
    assert 55 <= a["args"]["due_at"] - t0 <= 65
    core.director.decide(a["id"], True)
    await wait_done(core, tid)
    assert hub.outbox.scheduled() and not hub.providers["telegram"].sent


async def test_similar_name_is_offered_and_asked(core, hub):
    hub.providers["telegram"].people = PEOPLE["telegram"] + [("u:9:99", "Любимая ❤️", "")]
    intents(core, {"action": "send", "recipients": [{"name": "любимое", "platform": "telegram"}], "message": "Я тебя люблю"})
    tid = await core.director.submit("Отправь сообщение в Telegram любимое: я тебя люблю", "voice")
    a = await pending_send(core)
    d = a["args"]["deliveries"][0]
    assert d["to_id"] == "u:9:99" and d["guess"] is True
    assert core.director.comms.question(a["args"]).startswith("Похоже, вы имеете в виду контакт «Любимая ❤️»")
    await core.director.submit("Нет", "voice")
    await wait_done(core, tid)
    assert "Назовите контакт точнее" in last_text(core) and not hub.providers["telegram"].sent


async def test_guessed_recipient_ignores_trust_rules(core, hub):
    hub.providers["telegram"].people = [("u:9:99", "Любимая ❤️", "")]
    hub.add_rule("telegram", "u:9:99", "Любимая ❤️")
    intents(core, {"action": "send", "recipients": [{"name": "любимые", "platform": "telegram"}], "message": "Привет"})
    tid = await core.director.submit("Отправь любимые в Telegram: Привет")
    a = await pending_send(core)                  # a rule exists, but the name was only similar: still asked
    core.director.decide(a["id"], True)
    await wait_done(core, tid)
    assert hub.providers["telegram"].sent


def test_cut_off_phrases_are_detected():
    from jarvis.server import is_fragment
    assert is_fragment("Отправь.") and is_fragment("Отправь сообщение в") and is_fragment("Отправь моей")
    assert not is_fragment("Отправь Ивану в Telegram: привет") and not is_fragment("Да.") and not is_fragment("Привет.")


async def test_without_gemini_simple_commands_still_work(core, hub):
    class Down:
        last_structured_model = ""

        async def structured(self, *a, **k):
            raise RuntimeError("429 quota")
    core.director.comms.gemini = Down()
    tid = await core.director.submit("Говорю, отправь сообщение в Telegram моему контакту Иван Петров. Напиши: «Я скоро».")
    a = await pending_send(core)
    assert a["args"]["deliveries"][0]["to_id"] == "u:1:11" and a["args"]["body"] == "Я скоро."
    core.director.decide(a["id"], False)
    await wait_done(core, tid)


async def test_follow_up_to_the_same_person_is_a_message(core, hub):
    from jarvis.router import route
    cid = core.director.conversation_id()
    ctx = core.director.ctx(cid)
    ctx.update(last_contact={"channel": "telegram", "to_id": "u:1:11", "to_name": "Иван Петров"}, last_contact_at=time.time())
    assert route("Отправь ей теперь, что я её люблю", ctx).kind == "comms"
    assert route("Напиши любимая, контакту, что я тебя люблю", {}).kind == "comms"
    assert route("Как дела?", ctx) is None


async def test_provider_crash_during_search_is_a_clear_message(core, hub):
    async def boom(name):
        raise TimeoutError("ElementHandle.click: Timeout 30000ms exceeded")
    hub.providers["whatsapp"].search = boom
    intents(core, {"action": "send", "recipients": [{"name": "Рома", "platform": "whatsapp"}], "message": "Привет"})
    tid = await core.director.submit("Отправь Роме в WhatsApp: Привет")
    await wait_done(core, tid)
    assert "не получилось найти контакт" in last_text(core) and "Ничего не отправлено" in last_text(core)
    assert not hub.providers["whatsapp"].sent


async def test_clear_commands_are_read_locally_without_gemini(core, hub):
    class NoGemini:
        last_structured_model = ""
        calls = 0

        async def structured(self, *a, **k):
            NoGemini.calls += 1
            raise AssertionError("Gemini must not be needed for a clear command")
    hub.providers["whatsapp"].people = [("chat:Мама ❤️", "Мама ❤️", "чат WhatsApp")]
    core.director.comms.gemini = NoGemini()
    t0 = time.perf_counter()
    tid = await core.director.submit("Отправь сообщение контакту мама в WhatsApp привет.")
    a = await pending_send(core)
    assert time.perf_counter() - t0 < 1.0 and NoGemini.calls == 0
    assert a["args"]["deliveries"][0]["to_id"] == "chat:Мама ❤️" and a["args"]["body"] == "Привет."
    core.director.decide(a["id"], False)
    await wait_done(core, tid)


def test_local_reading_leaves_unclear_commands_to_gemini():
    from jarvis.comms.agent import CommsAgent, local_parse
    for q in ["Отправь Сергею и Ивану в Telegram: Встреча переносится.", "Напиши маме в вотсап, что я задержусь",
              "Отправь ей теперь, что я её люблю", "Отправь сообщение в WhatsApp"]:
        d = local_parse(q)
        assert not (d and CommsAgent._confident(d)), q
    d = local_parse("Отправь маме в WhatsApp завтра вечером: привет")
    assert d["time_unclear"] and d["recipients"][0]["name"] == "маме"      # «завтра вечером» is never sent right away
