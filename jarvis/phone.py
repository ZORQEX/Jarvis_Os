"""JARVIS from the phone: a private Telegram bot (the idea and the safety rules from morph-rq/jarvis-os's messaging
gateway, written for JARVIS's director, approvals and local speech recognition).

Setup: create a bot with @BotFather (/newbot), put its token in Settings → «Телефон». Pairing: the window shows a
6-digit code for 15 minutes; sending it to the bot from the phone makes that chat the owner. Nobody else is
answered (one line «это личный бот», then silence), groups are ignored, and only the window can pair.

From the owner: text → the same director as typing in the window (the conversation is shared); a voice note →
recognised on this computer (GigaAM), then the same; an Excel / PDF factory file → «Заказы» (read and compared, saved
only after «Подтвердить» on the computer); photos and other files → ~/Downloads/JARVIS. Answers come back to the chat;
an action that needs approval comes with «Да» / «Нет» buttons — except high-risk ones (power off, deleting,
installing), which are approved on the computer only.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets as pysecrets
import shutil
import tempfile
import time
from pathlib import Path

import aiohttp

from .events import spawn

TOKEN_KEY = "phone_bot_token"
PAIR_TTL = 15 * 60
FACTORY_FILES = {".xlsx", ".xlsm", ".xls", ".pdf"}
MAX_FILE = 20 * 1024 * 1024          # the Bot API's own download limit
YES = re.compile(r"^\s*(да|ага|разрешаю|подтверждаю|давай|ок|окей|yes)\b", re.I)
NO = re.compile(r"^\s*(нет|не надо|отмена|отклоняю|no)\b", re.I)


class PhoneError(RuntimeError):
    pass


class BotAPI:
    def __init__(self, token: str, base: str = "https://api.telegram.org"):
        self.token, self.base = token, base.rstrip("/")

    async def call(self, method: str, wait: float = 30, **params) -> object:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=wait)) as s:
            async with s.post(f"{self.base}/bot{self.token}/{method}", json=params) as r:
                body = await r.json(content_type=None)
        if not body.get("ok"):
            raise PhoneError(telegram_error(body.get("description"), method))
        return body.get("result")

    async def download(self, file_id: str) -> tuple[bytes, str]:
        info = await self.call("getFile", file_id=file_id)
        if (info.get("file_size") or 0) > MAX_FILE:
            raise PhoneError("файл больше 20 МБ — Telegram не отдаёт ботам такие файлы")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
            async with s.get(f"{self.base}/file/bot{self.token}/{info['file_path']}") as r:
                r.raise_for_status()
                return await r.read(), info["file_path"]

    async def send_file(self, chat_id: int, path: Path, caption: str = "") -> None:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if caption:
            form.add_field("caption", caption[:1000])
        form.add_field("document", path.read_bytes(), filename=path.name)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
            async with s.post(f"{self.base}/bot{self.token}/sendDocument", data=form) as r:
                body = await r.json(content_type=None)
        if not body.get("ok"):
            raise PhoneError(telegram_error(body.get("description"), "sendDocument"))


def telegram_error(desc: object, method: str = "") -> str:
    d = str(desc or "")
    if "Unauthorized" in d:
        return "Telegram не принял токен бота — скопируйте его заново из @BotFather"
    if "Conflict" in d:
        return "Этот бот уже подключён в другом месте (другой JARVIS или программа) — там его нужно отключить"
    return ("Telegram: " + d if d else f"Telegram: {method} не выполнен")[:200]


def chunks(text: str, size: int = 3800) -> list[str]:
    out, text = [], text.strip()
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        out.append(text[:cut])
        text = text[cut:].lstrip()
    return out + ([text] if text else [])


class Phone:
    def __init__(self, director, db, bus, audit, store, inbox: Path | None = None, api_base: str | None = None):
        self.director, self.db, self.bus, self.audit, self.store = director, db, bus, audit, store
        self.inbox = inbox or Path.home() / "Downloads" / "JARVIS"
        self.api_base = api_base or "https://api.telegram.org"
        self.loop_task: asyncio.Task | None = None
        self.fwd_task: asyncio.Task | None = None
        self.bot: dict | None = None
        self.error = ""
        self.tasks: set[str] = set()              # tasks started from the phone: their answers go back there
        self.cards: dict[str, int] = {}           # approval id → message id of its card in the chat
        self.told: set[int] = set()               # strangers already told «this is a private bot»

    # ------------------------------------------------------------------ state
    def token(self) -> str | None:
        return self.store.get(TOKEN_KEY)

    def api(self) -> BotAPI:
        return BotAPI(self.token() or "", self.api_base)

    def owner(self) -> int | None:
        v = self.db.get_kv("phone_owner_chat")
        return int(v) if v else None

    def running(self) -> bool:
        return self.loop_task is not None and not self.loop_task.done()

    def state(self) -> dict:
        pair = self.db.get_kv("phone_pair") or {}
        return {"configured": bool(self.token()), "bot": (self.bot or {}).get("username"), "paired": bool(self.owner()),
                "running": self.running(), "error": self.error,
                "code": pair.get("code") if pair.get("until", 0) > time.time() else None}

    async def set_token(self, token: str) -> dict:
        token = (token or "").strip()
        if not re.fullmatch(r"\d{5,12}:[A-Za-z0-9_-]{30,50}", token):
            raise ValueError("Это не похоже на токен бота: он выглядит как 123456789:AA…, его выдаёт @BotFather")
        me = await BotAPI(token, self.api_base).call("getMe")
        self.store.store(TOKEN_KEY, token, "JARVIS: бот Telegram для связи с телефона")
        self.bot = me
        self.audit.write("phone.bot_set", bot=me.get("username"))
        await self.start()
        return self.state()

    def pair_code(self) -> str:
        code = f"{pysecrets.randbelow(10 ** 6):06d}"
        self.db.set_kv("phone_pair", {"code": code, "until": time.time() + PAIR_TTL})
        return code

    def unpair(self) -> None:
        self.db.set_kv("phone_owner_chat", None)
        self.audit.write("phone.unpaired")

    async def forget(self) -> None:
        await self.stop()
        self.store.clear(TOKEN_KEY)
        self.unpair()
        self.bot = None

    # ------------------------------------------------------------------ loops
    async def start(self) -> None:
        if not self.token() or self.running():
            return
        self.loop_task = asyncio.create_task(self._poll(), name="phone-poll")
        if self.fwd_task is None or self.fwd_task.done():
            self.fwd_task = asyncio.create_task(self._forward(), name="phone-forward")

    async def stop(self) -> None:
        for t in (self.loop_task, self.fwd_task):
            if t:
                t.cancel()
        self.loop_task = self.fwd_task = None

    async def _poll(self) -> None:
        api = self.api()
        backoff = 2
        while True:
            try:
                if self.bot is None:
                    self.bot = await api.call("getMe")
                    await api.call("deleteWebhook", drop_pending_updates=False)
                offset = int(self.db.get_kv("phone_offset") or 0)
                updates = await api.call("getUpdates", wait=40, offset=offset, limit=50, timeout=25,
                                         allowed_updates=["message", "callback_query"])
                self.error, backoff = "", 2
                for u in updates or []:
                    self.db.set_kv("phone_offset", u["update_id"] + 1)
                    try:
                        await self._handle(u)
                    except Exception as e:   # noqa: BLE001 — one odd update never stops the bot
                        self.audit.write("phone.update_failed", error=repr(e)[:200])
            except asyncio.CancelledError:
                raise
            except Exception as e:   # noqa: BLE001 — offline, Telegram down, token revoked
                self.error = str(e)[:200]
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)

    async def send(self, text: str, buttons: list[list[dict]] | None = None) -> int | None:
        chat = self.owner()
        if not chat or not self.token():
            return None
        mid = None
        parts = chunks(text) or ["…"]
        for i, part in enumerate(parts):
            params = {"chat_id": chat, "text": part, "disable_web_page_preview": True}
            if buttons and i == len(parts) - 1:
                params["reply_markup"] = {"inline_keyboard": buttons}
            res = await self.api().call("sendMessage", **params)
            mid = res.get("message_id")
        return mid

    # ------------------------------------------------------------------ incoming
    async def _handle(self, u: dict) -> None:
        if "callback_query" in u:
            return await self._callback(u["callback_query"])
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") != "private":
            return                                              # groups and channels: never
        cid, text = chat.get("id"), (msg.get("text") or "").strip()
        owner = self.owner()
        code = re.fullmatch(r"(?:/start\s+)?(\d{6})", text)
        if code and cid != owner:
            pair = self.db.get_kv("phone_pair") or {}
            if pair.get("code") == code.group(1) and pair.get("until", 0) > time.time():
                self.db.set_kv("phone_owner_chat", cid)
                self.db.set_kv("phone_pair", None)
                self.audit.write("phone.paired", user=(msg.get("from") or {}).get("username"))
                self.bus.publish("phone.state", self.state())
                await self.send("Готово: это ваш JARVIS. Пишите или присылайте голосовые, файлы фабрик — я в «Заказы».")
                return
        if cid != owner:
            if cid not in self.told:
                self.told.add(cid)
                await self.api().call("sendMessage", chat_id=cid, text="Это личный бот JARVIS.")
            return
        if msg.get("voice") or msg.get("audio"):
            return await self._voice(msg)
        if msg.get("document") or msg.get("photo"):
            return await self._file(msg)
        if text.startswith("/start") or text == "/help":
            return await self.send("Это ваш JARVIS. Пишите как в окне: «какая погода», «посчитай предоплаты», "
                                   "«что у меня завтра». Голосовые тоже понимаю. Файл фабрики — пришлите, загружу в «Заказы».")
        if text:
            await self._ask(text)

    async def _ask(self, text: str) -> None:
        pending = [a for a in self.director.approvals.pending() if a["task_id"] in self.tasks]
        if pending and (YES.match(text) or NO.match(text)) and len(pending) == 1:
            return await self._decide(pending[0]["id"], bool(YES.match(text)))
        tid = await self.director.submit(text[:4000], source="phone")
        if tid:
            self.tasks.add(tid)

    async def _voice(self, msg: dict) -> None:
        stt = getattr(self.director, "stt", None)
        if not stt or not stt.available() or not shutil.which("ffmpeg"):
            return await self.send("Голосовые пока не распознаются на компьютере — напишите текстом.")
        media = msg.get("voice") or msg.get("audio")
        data, _ = await self.api().download(media["file_id"])
        with tempfile.TemporaryDirectory(prefix="jarvis-phone-") as tmp:
            src, wav = Path(tmp) / "in.ogg", Path(tmp) / "out.wav"
            src.write_bytes(data)
            proc = await asyncio.create_subprocess_exec("ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(src),
                                                        "-ac", "1", "-ar", "16000", str(wav),
                                                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), 120)
            if proc.returncode != 0 or not wav.exists():
                return await self.send("Не получилось прочитать голосовое.")
            text = (await stt.transcribe(wav.read_bytes())).strip()
        if not text:
            return await self.send("Не расслышал — повторите или напишите.")
        await self.send(f"🎙 «{text}»")
        await self._ask(text)

    async def _file(self, msg: dict) -> None:
        if msg.get("document"):
            doc = msg["document"]
            name = re.sub(r"[^\w.() -]+", "_", doc.get("file_name") or "файл")[:120]
            data, _ = await self.api().download(doc["file_id"])
        else:
            photo = max(msg["photo"], key=lambda p: p.get("file_size") or 0)
            data, path = await self.api().download(photo["file_id"])
            name = time.strftime("Фото %Y-%m-%d %H-%M-%S") + Path(path).suffix
        self.inbox.mkdir(parents=True, exist_ok=True)
        target = self.inbox / name
        n = 2
        while target.exists():
            target = self.inbox / f"{Path(name).stem} ({n}){Path(name).suffix}"
            n += 1
        target.write_bytes(data)
        self.audit.write("phone.file", name=target.name, size=len(data))
        if target.suffix.lower() in FACTORY_FILES and self.director.gw.registry.get("fashion_attach"):
            tid = await self.director.run_ui_tool("fashion_attach", {"path": str(target)})
            self.tasks.add(tid)
            return await self.send(f"Получил «{target.name}», читаю в «Заказах»…")
        caption = (msg.get("caption") or "").strip()
        await self.send(f"Сохранил: ~/Downloads/JARVIS/{target.name}")
        if caption:
            await self._ask(f"{caption} (файл: {target})")

    # ------------------------------------------------------------------ approvals
    async def _callback(self, q: dict) -> None:
        cid = ((q.get("message") or {}).get("chat") or {}).get("id")
        api = self.api()
        m = re.fullmatch(r"ap:([\w-]+):([yn])", q.get("data") or "")
        if cid != self.owner() or not m:
            return await api.call("answerCallbackQuery", callback_query_id=q["id"], text="Нет доступа")
        msg = await self._decide(m.group(1), m.group(2) == "y")
        await api.call("answerCallbackQuery", callback_query_id=q["id"], text=msg[:190])

    def computer_only(self, a: dict) -> bool:
        """High risk (power, deleting, installing) and durable permission changes are decided at the computer."""
        return str(a.get("risk")) == "high" or not self.director.voice_approvable(a.get("tool_id") or "")

    async def _decide(self, aid: str, approve: bool) -> str:
        from .policy import ApprovalError
        a = self.director.approvals.get(aid)
        if not a or a["status"] != "pending" or a["task_id"] not in self.tasks:
            text = "Это решение уже не ждёт ответа."
        elif self.computer_only(a):
            text = "Это действие подтверждается только на компьютере."
        else:
            try:
                self.director.decide(aid, approve)
                self.audit.write("approval.by_phone", approval_id=aid, approve=approve)
                text = "Разрешено." if approve else "Отклонено."
            except ApprovalError as e:
                text = str(e)
        mid = self.cards.pop(aid, None)
        if mid and self.owner():
            try:
                await self.api().call("editMessageReplyMarkup", chat_id=self.owner(), message_id=mid,
                                      reply_markup={"inline_keyboard": []})
            except PhoneError:
                pass
        if text not in ("Разрешено.", "Отклонено."):
            await self.send(text)
        return text

    # ------------------------------------------------------------------ answers back to the phone
    async def _forward(self) -> None:
        q = self.bus.subscribe()
        while True:
            ev = await q.get()
            try:
                await self._event(ev)
            except asyncio.CancelledError:
                raise
            except Exception as e:   # noqa: BLE001
                self.audit.write("phone.send_failed", error=repr(e)[:200])

    async def _event(self, ev: dict) -> None:
        kind, tid = ev.get("kind"), ev.get("task_id")
        if kind == "message":
            m = ev.get("message") or {}
            if m.get("role") == "assistant" and m.get("task_id") in self.tasks:
                await self.send(m.get("text") or "Готово.")
                await self._files(m["task_id"])
        elif kind == "approval.created":
            a = ev.get("approval") or {}
            if a.get("task_id") not in self.tasks:
                return
            what = (a.get("summary") or {}).get("action") or a.get("tool_id")
            if self.computer_only(a):
                await self.send(f"Нужно подтверждение: {what}. Это подтверждается только на компьютере.")
                return
            mid = await self.send(f"Подтвердите: {what}", [[{"text": "✅ Да", "callback_data": f"ap:{a['id']}:y"},
                                                            {"text": "✖ Нет", "callback_data": f"ap:{a['id']}:n"}]])
            if mid:
                self.cards[a["id"]] = mid
        elif kind in ("task.updated",) and tid in self.tasks:
            t = ev.get("task") or {}
            if t.get("state") in ("completed", "failed", "cancelled"):
                spawn(self._forget(tid), "phone-forget")

    async def _forget(self, tid: str) -> None:
        await asyncio.sleep(60)                     # late answers (a card decided later) still arrive
        if tid not in self.director.rt:
            self.tasks.discard(tid)

    async def _files(self, tid: str) -> None:
        """A screenshot or a note the task made is sent as a file too."""
        for s in self.director.db.all("SELECT tool_id, result FROM task_steps WHERE task_id=? AND state='succeeded'", (tid,)):
            if s["tool_id"] not in ("screenshot", "create_note"):
                continue
            try:
                path = Path((json.loads(s["result"] or "{}").get("data") or {}).get("path") or "")
            except ValueError:
                continue
            if path.is_file() and path.stat().st_size < MAX_FILE:
                await self.api().send_file(self.owner(), path, path.name)
