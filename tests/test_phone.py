"""JARVIS from the phone: the private Telegram bot against a fake Bot API (VERIFIED MOCK — no real Telegram)."""
import asyncio
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from jarvis.phone import Phone, chunks

from conftest import wait_for

TOKEN = "123456789:" + "A" * 35
OWNER, STRANGER = 111, 222


class FakeTelegram:
    """The few Bot API methods the bot uses; updates are handed out by getUpdates like the real long poll."""

    def __init__(self):
        self.updates: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.files = {"f1": b"%PDF-1.4 proforma", "v1": b"OggS voice"}
        self.next_id, self.mid = 1, 100

    def push(self, **u):
        self.updates.append({"update_id": self.next_id, **u})
        self.next_id += 1

    def text(self, chat, text):
        self.push(message={"chat": {"id": chat, "type": "private"}, "from": {"username": "u"}, "text": text})

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]

    async def method(self, req):
        name = req.match_info["method"]
        if req.content_type == "multipart/form-data":
            form = await req.post()
            params = {k: (v.filename if hasattr(v, "filename") else v) for k, v in form.items()}
        else:
            params = await req.json() if req.can_read_body else {}
        self.calls.append((name, params))
        if req.match_info["token"] != "bot" + TOKEN:
            return web.json_response({"ok": False, "description": "Unauthorized"})
        if name == "getMe":
            return web.json_response({"ok": True, "result": {"id": 1, "username": "my_jarvis_bot"}})
        if name == "getUpdates":
            off = params.get("offset", 0)
            for _ in range(20):
                out = [u for u in self.updates if u["update_id"] >= off]
                if out:
                    return web.json_response({"ok": True, "result": out})
                await asyncio.sleep(0.02)
            return web.json_response({"ok": True, "result": []})
        if name == "getFile":
            return web.json_response({"ok": True, "result": {"file_path": "docs/" + params["file_id"],
                                                             "file_size": len(self.files[params["file_id"]])}})
        if name in ("sendMessage", "sendDocument"):
            self.mid += 1
            return web.json_response({"ok": True, "result": {"message_id": self.mid}})
        return web.json_response({"ok": True, "result": True})

    async def file(self, req):
        return web.Response(body=self.files[req.match_info["path"].split("/")[-1]])

    def app(self):
        a = web.Application()
        a.router.add_get("/file/{token}/{path:.+}", self.file)
        a.router.add_post("/{token}/{method}", self.method)
        return a


class Store(dict):
    def store(self, account, value, label):
        self[account] = value

    def clear(self, account):
        self.pop(account, None)


@pytest.fixture
async def tg():
    fake = FakeTelegram()
    server = TestServer(fake.app())
    await server.start_server()
    fake.base = str(server.make_url("")).rstrip("/")
    yield fake
    await server.close()


@pytest.fixture
async def phone(core, tg, home):
    p = Phone(core.director, core.db, core.bus, core.audit, Store(), inbox=home / "inbox", api_base=tg.base)
    yield p
    await p.stop()


async def paired(phone, tg):
    await phone.set_token(TOKEN)
    tg.text(OWNER, phone.pair_code())
    await wait_for(lambda: phone.owner() == OWNER)
    await wait_for(lambda: any("ваш JARVIS" in m["text"] for m in tg.sent()))


def to(tg, chat):
    return [m["text"] for m in tg.sent() if m["chat_id"] == chat]


async def test_a_wrong_token_is_refused_before_anything_is_stored(phone, tg):
    with pytest.raises(ValueError):
        await phone.set_token("not a token")
    assert not phone.token() and not tg.calls


async def test_only_the_paired_phone_is_answered(phone, tg):
    await phone.set_token(TOKEN)
    assert phone.state()["bot"] == "my_jarvis_bot" and phone.running()
    tg.text(STRANGER, "000000")                            # a guessed code is not pairing
    tg.text(STRANGER, "который час")
    await wait_for(lambda: to(tg, STRANGER))
    await paired(phone, tg)
    tg.text(STRANGER, "покажи диск")
    tg.text(STRANGER, "ещё раз")
    tg.push(message={"chat": {"id": -5, "type": "group"}, "text": "который час"})
    tg.text(OWNER, "/help")
    await wait_for(lambda: any("Пишите как в окне" in t for t in to(tg, OWNER)))
    assert to(tg, STRANGER) == ["Это личный бот JARVIS."]      # one line, then silence; nothing ran for them
    assert not to(tg, -5)
    assert not core_tasks_with(phone, "покажи диск")


def core_tasks_with(phone, text):
    return phone.db.all("SELECT id FROM tasks WHERE request LIKE ?", (f"%{text}%",))


async def test_an_expired_code_does_not_pair(phone, tg):
    await phone.set_token(TOKEN)
    code = phone.pair_code()
    phone.db.set_kv("phone_pair", {"code": code, "until": time.time() - 1})
    tg.text(OWNER, code)
    await wait_for(lambda: any(m == "getUpdates" and p.get("offset", 0) > 1 for m, p in tg.calls))
    assert phone.owner() is None


async def test_a_question_from_the_phone_is_answered_in_the_chat(phone, tg):
    await paired(phone, tg)
    tg.text(OWNER, "который час")
    await wait_for(lambda: any(":" in t and "который" not in t for t in to(tg, OWNER)[1:]))
    msgs = phone.db.all("SELECT role, text, meta FROM messages ORDER BY created_at")
    assert any(m["role"] == "user" and m["text"] == "который час" and '"phone"' in m["meta"] for m in msgs)


async def test_an_action_is_approved_with_the_buttons_and_its_file_comes_back(phone, tg, home):
    await paired(phone, tg)
    tg.text(OWNER, "запиши заметку: позвонить на фабрику")
    await wait_for(lambda: any(m.get("reply_markup") for m in tg.sent()))
    card = next(m for m in tg.sent() if m.get("reply_markup"))
    assert card["text"].startswith("Подтвердите")
    data = card["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    tg.push(callback_query={"id": "q1", "data": data, "message": {"chat": {"id": OWNER}}})
    await wait_for(lambda: tg.sent("sendDocument"))
    note = list((home / "out").rglob("*"))
    assert any("позвонить на фабрику" in p.read_text() for p in note if p.is_file())
    assert tg.sent("sendDocument")[0]["chat_id"] == str(OWNER)
    answered = [p for m, p in tg.calls if m == "answerCallbackQuery"]
    assert answered[0]["text"] == "Разрешено."
    assert [p for m, p in tg.calls if m == "editMessageReplyMarkup"]    # the buttons go away once decided


async def test_a_stranger_cannot_press_the_owners_button(phone, tg):
    await paired(phone, tg)
    tg.text(OWNER, "запиши заметку: секрет")
    await wait_for(lambda: any(m.get("reply_markup") for m in tg.sent()))
    data = next(m for m in tg.sent() if m.get("reply_markup"))["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    tg.push(callback_query={"id": "q2", "data": data, "message": {"chat": {"id": STRANGER}}})
    await wait_for(lambda: any(m == "answerCallbackQuery" for m, _ in tg.calls))
    assert [p for m, p in tg.calls if m == "answerCallbackQuery"][0]["text"] == "Нет доступа"
    assert all(a["status"] == "pending" for a in phone.director.approvals.pending())


async def test_a_typed_yes_decides_the_one_waiting_action(phone, tg, home):
    await paired(phone, tg)
    tg.text(OWNER, "запиши заметку: купить ткань")
    await wait_for(lambda: any(m.get("reply_markup") for m in tg.sent()))
    tg.text(OWNER, "да")
    await wait_for(lambda: any(p.is_file() and "купить ткань" in p.read_text() for p in (home / "out").rglob("*")))


async def test_high_risk_is_approved_only_at_the_computer(phone, core):
    a = {"id": "a_x", "status": "pending", "task_id": "t_x", "risk": "high", "tool_id": "power"}
    phone.tasks.add("t_x")
    phone.director.approvals.get = lambda aid: a
    assert phone.computer_only(a)
    assert await phone._decide("a_x", True) == "Это действие подтверждается только на компьютере."
    assert phone.computer_only({"risk": "moderate", "tool_id": "trust_rule_add"})      # durable permission: click only
    assert not phone.computer_only({"risk": "moderate", "tool_id": "create_note"})


async def test_a_factory_file_is_saved_and_other_files_too(phone, tg, home):
    await paired(phone, tg)
    tg.push(message={"chat": {"id": OWNER, "type": "private"},
                     "document": {"file_id": "f1", "file_name": "../../проформа.pdf"}})
    await wait_for(lambda: (home / "inbox").exists() and list((home / "inbox").iterdir()))
    saved = list((home / "inbox").iterdir())
    assert len(saved) == 1 and saved[0].parent == home / "inbox" and saved[0].read_bytes().startswith(b"%PDF")
    await wait_for(lambda: any("Сохранил" in t for t in to(tg, OWNER)))    # no «Заказы» in this core: just saved


async def test_voice_without_recognition_asks_for_text(phone, tg):
    await paired(phone, tg)
    tg.push(message={"chat": {"id": OWNER, "type": "private"}, "voice": {"file_id": "v1"}})
    await wait_for(lambda: any("напишите текстом" in t for t in to(tg, OWNER)))


async def test_telegram_down_is_shown_not_hidden(phone, tg):
    phone.store.store("phone_bot_token", "999:" + "B" * 35, "x")      # revoked token: Telegram says Unauthorized
    await phone.start()
    await wait_for(lambda: phone.state()["error"])
    assert "не принял токен" in phone.state()["error"]


def test_long_answers_are_split_on_lines():
    parts = chunks(("строка\n" * 1500).strip())
    assert len(parts) > 1 and all(len(p) <= 3800 for p in parts) and not parts[1].startswith("\n")
