"""Telegram through the official MTProto API with the user's own account (Telethon).

Authentication is Telegram's own: api_id/api_hash of the user's application (my.telegram.org) and a login
code sent by Telegram (plus the cloud password if 2FA is on). The resulting session is stored only in the
OS keyring; it is never logged, never put into prompts and never sent to any AI."""
from __future__ import annotations

import asyncio
import time

from .. import secrets
from .base import CommsError, Contact, Provider, Receipt, Status
from .contacts import is_self, match_score

K_ID, K_HASH, K_SESSION = "telegram_api_id", "telegram_api_hash", "telegram_session"
REQUIREMENT = ("Нужно своё приложение Telegram: войдите на my.telegram.org → «API development tools», создайте "
               "приложение и введите здесь api_id и api_hash, затем номер телефона и код из Telegram.")


class TelegramProvider(Provider):
    id = "telegram"
    title = "Telegram"
    caps = {"can_send_text": True, "can_schedule": True, "can_search_contacts": True, "can_open_app": True,
            "can_send_file": False, "can_send_image": False, "can_reply": False}

    def __init__(self, store=secrets):
        self.store = store
        self.client = None
        self.me: dict | None = None
        self.state, self.detail = "not_connected", ""
        self._login: dict | None = None
        self._dir: tuple[float, list] | None = None
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- status
    def status(self) -> Status:
        if self.state == "connected" and self.me:
            acc = self.me["name"] + (f" (@{self.me['username']})" if self.me.get("username") else "")
            return Status("connected", "Вход выполнен через официальный API Telegram", acc)
        if self._login:
            return Status("connecting", "Ждём код из Telegram" + (" и облачный пароль" if self._login.get("need_password") else ""),
                          extra={"login_stage": "password" if self._login.get("need_password") else "code"})
        if self.state == "error":
            return Status("error", self.detail, requirement=REQUIREMENT)
        return Status("not_connected", "Telegram не подключён", requirement=REQUIREMENT)

    def _make_client(self, session: str | None, api_id: int, api_hash: str):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        return TelegramClient(StringSession(session), api_id, api_hash, device_model="JARVIS WorkOS",
                              system_version="Linux", app_version="0.1", lang_code="ru", system_lang_code="ru",
                              receive_updates=False, request_retries=2, connection_retries=2, timeout=15)

    async def start(self) -> None:
        sess, api_id, api_hash = self.store.get(K_SESSION), self.store.get(K_ID), self.store.get(K_HASH)
        if not (sess and api_id and api_hash):
            return
        try:
            self.client = self._make_client(sess, int(api_id), api_hash)
            await asyncio.wait_for(self.client.connect(), 20)
            if not await self.client.is_user_authorized():
                self.state, self.detail = "error", "Сессия Telegram больше недействительна — войдите заново"
                return
            await self._load_me()
        except Exception as e:   # noqa: BLE001
            self.state, self.detail = "error", "Не удалось подключиться к Telegram: " + _brief(e)

    async def stop(self) -> None:
        for c in (self.client, (self._login or {}).get("client")):
            if c:
                try:
                    await c.disconnect()
                except Exception:   # noqa: BLE001
                    pass

    async def _load_me(self) -> None:
        u = await self.client.get_me()
        self.me = {"id": u.id, "name": " ".join(x for x in (u.first_name, u.last_name) if x) or "Вы",
                   "username": u.username}
        self.state, self.detail = "connected", ""

    # ---------------------------------------------------------------- login (user-driven)
    async def begin_login(self, api_id: str, api_hash: str, phone: str) -> dict:
        api_id, api_hash, phone = api_id.strip(), api_hash.strip(), "".join(phone.split())
        if not api_id.isdigit() or len(api_hash) != 32:
            raise ValueError("api_id — число, api_hash — 32 символа (my.telegram.org → API development tools)")
        from telethon.errors import ApiIdInvalidError, FloodWaitError, PhoneNumberInvalidError
        await self._drop_login()
        c = self._make_client(None, int(api_id), api_hash)
        await asyncio.wait_for(c.connect(), 20)
        try:
            sent = await c.send_code_request(phone)
        except ApiIdInvalidError:
            await c.disconnect()
            raise ValueError("Telegram не принял api_id/api_hash") from None
        except PhoneNumberInvalidError:
            await c.disconnect()
            raise ValueError("Telegram не принял номер телефона") from None
        except FloodWaitError as e:
            await c.disconnect()
            raise ValueError(f"Telegram просит подождать {e.seconds} с перед новой попыткой") from None
        self._login = {"client": c, "phone": phone, "hash": sent.phone_code_hash, "api_id": api_id,
                       "api_hash": api_hash, "need_password": False, "at": time.time()}
        return {"stage": "code"}

    async def finish_login(self, code: str | None = None, password: str | None = None) -> dict:
        from telethon.errors import (PasswordHashInvalidError, PhoneCodeExpiredError, PhoneCodeInvalidError,
                                     SessionPasswordNeededError)
        lg = self._login
        if not lg:
            raise ValueError("Сначала запросите код")
        c = lg["client"]
        try:
            if lg["need_password"]:
                await c.sign_in(password=password or "")
            else:
                await c.sign_in(lg["phone"], (code or "").strip(), phone_code_hash=lg["hash"])
        except SessionPasswordNeededError:
            lg["need_password"] = True
            return {"stage": "password"}
        except PhoneCodeInvalidError:
            raise ValueError("Неверный код") from None
        except PhoneCodeExpiredError:
            await self._drop_login()
            raise ValueError("Код устарел — запросите новый") from None
        except PasswordHashInvalidError:
            raise ValueError("Неверный облачный пароль") from None
        # Store credentials only after a successful login. The session string never leaves the keyring.
        self.store.store(K_ID, lg["api_id"], "JARVIS: Telegram api_id")
        self.store.store(K_HASH, lg["api_hash"], "JARVIS: Telegram api_hash")
        self.store.store(K_SESSION, c.session.save(), "JARVIS: Telegram session")
        if self.client:
            await self.client.disconnect()
        self.client, self._login = c, None
        await self._load_me()
        return {"stage": "done", "account": self.status().account}

    async def _drop_login(self) -> None:
        if self._login:
            try:
                await self._login["client"].disconnect()
            except Exception:   # noqa: BLE001
                pass
        self._login = None

    async def logout(self) -> None:
        await self._drop_login()
        if self.client:
            try:
                await self.client.log_out()
            except Exception:   # noqa: BLE001
                pass
        for k in (K_SESSION, K_ID, K_HASH):
            self.store.clear(k)
        self.client, self.me, self.state, self.detail, self._dir = None, None, "not_connected", "", None

    # ---------------------------------------------------------------- contacts
    async def _directory(self) -> list[Contact]:
        if self._dir and time.time() - self._dir[0] < 300:
            return self._dir[1]
        from telethon import functions
        out: list[Contact] = []
        res = await self.client(functions.contacts.GetContactsRequest(hash=0))
        for u in getattr(res, "users", []):
            if not u.bot and not u.deleted:
                out.append(_user(u, "контакты Telegram"))
        async for d in self.client.iter_dialogs(limit=300):
            e = d.entity
            if d.is_user and not getattr(e, "bot", False) and not getattr(e, "deleted", False):
                out.append(_user(e, "чаты Telegram"))
            elif d.is_group and not getattr(e, "left", False):
                cid = (f"ch:{e.id}:{e.access_hash}" if getattr(e, "access_hash", None) else f"c:{e.id}")
                out.append(Contact("telegram", cid, e.title, "группа", "чаты Telegram"))
        self._dir = (time.time(), out)
        return out

    async def search(self, name: str) -> list[Contact]:
        self._need()
        if is_self(name):
            return [Contact("telegram", "self", "Избранное (вы сами)", "Saved Messages", "ваш аккаунт")]
        try:
            people = await self._directory()
        except Exception as e:   # noqa: BLE001
            raise CommsError("Не удалось получить контакты Telegram", "network", _brief(e)) from None
        out = []
        for c in people:
            sc = match_score(name, c.name, c.address.lstrip("@"))
            if sc:
                out.append(Contact(c.channel, c.id, c.name, c.address, c.source, sc))
        return out

    # ---------------------------------------------------------------- send / verify
    def _need(self) -> None:
        if not (self.client and self.state == "connected"):
            raise CommsError("Telegram не подключён. " + REQUIREMENT, "not_connected", retryable=True)

    def _peer(self, cid: str):
        from telethon import types
        kind, *rest = cid.split(":")
        if kind == "self":
            return "me"
        if kind == "u":
            return types.InputPeerUser(int(rest[0]), int(rest[1]))
        if kind == "c":
            return types.InputPeerChat(int(rest[0]))
        if kind == "ch":
            return types.InputPeerChannel(int(rest[0]), int(rest[1]))
        raise CommsError("Неизвестный получатель Telegram", "not_found")

    async def send(self, contact: Contact, body: str, subject: str | None, op_id: str) -> Receipt:
        self._need()
        if not self.client.is_connected():
            try:
                await asyncio.wait_for(self.client.connect(), 15)
            except Exception as e:   # noqa: BLE001
                raise CommsError("Нет связи с Telegram", "network", _brief(e), retryable=True) from None
        from telethon import errors
        async with self._lock:
            try:
                msg = await self.client.send_message(self._peer(contact.id), body, link_preview=False)
            except errors.FloodWaitError as e:
                raise CommsError(f"Telegram ограничил частоту отправки (подождать {e.seconds} с)", "rejected",
                                 str(e), retryable=True) from None
            except (errors.UserIsBlockedError, errors.InputUserDeactivatedError, errors.ChatWriteForbiddenError,
                    errors.PeerIdInvalidError, errors.UserPrivacyRestrictedError) as e:
                raise CommsError("Telegram не позволяет написать этому получателю", "rejected", _brief(e)) from None
            except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError) as e:
                self.state, self.detail = "error", "Сессия Telegram отозвана — войдите заново"
                raise CommsError("Сессия Telegram отозвана — войдите заново в разделе «Сообщения»", "auth",
                                 _brief(e), retryable=True) from None
            except errors.RPCError as e:
                raise CommsError("Telegram отклонил сообщение", "rejected", _brief(e)) from None
            except (ConnectionError, asyncio.TimeoutError, OSError) as e:
                # The request may have reached Telegram: never retry blindly.
                raise CommsError("Связь с Telegram оборвалась во время отправки — статус неизвестен", "unknown",
                                 _brief(e)) from None
        return Receipt(str(msg.id), f"Telegram принял сообщение №{msg.id}")

    async def verify(self, contact: Contact, body: str, receipt: Receipt) -> tuple[str, str]:
        try:
            m = await self.client.get_messages(self._peer(contact.id), ids=int(receipt.remote_id))
        except Exception as e:   # noqa: BLE001
            return "unverified", "Не удалось перечитать чат: " + _brief(e)
        if m and m.out and (m.message or "").strip() == body.strip():
            return "verified", f"Сообщение №{m.id} есть в чате «{contact.name}»"
        return "failed", "Сообщение не найдено в чате после отправки"


def _user(u, source: str) -> Contact:
    name = " ".join(x for x in (u.first_name, u.last_name) if x) or (u.username or str(u.id))
    addr = "@" + u.username if u.username else ("+" + u.phone if getattr(u, "phone", None) else "")
    return Contact("telegram", f"u:{u.id}:{u.access_hash}", name, addr, source)


def _brief(e: Exception) -> str:
    return f"{e.__class__.__name__}: {str(e)[:160]}"
