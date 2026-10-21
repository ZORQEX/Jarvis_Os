"""E-mail through the user's own mailbox: SMTP to send, IMAP to verify ("Sent") and to find addresses.

Authentication is the mail provider's own (an app password for Gmail/Yandex/Mail.ru). The password lives only in
the OS keyring. Mail headers read for address lookup are used locally and never sent to Gemini."""
from __future__ import annotations

import asyncio
import email.utils
import imaplib
import re
import smtplib
import socket
import ssl
import time
from email.header import decode_header, make_header
from email.message import EmailMessage

from .. import secrets
from ..db import Database
from .base import CommsError, Contact, Provider, Receipt, Status
from .contacts import AddressBook, is_self, match_score

K_PASS = "email_password"
HOSTS = {  # domain → (smtp host, port, mode, imap host)
    "gmail.com": ("smtp.gmail.com", 465, "ssl", "imap.gmail.com"),
    "googlemail.com": ("smtp.gmail.com", 465, "ssl", "imap.gmail.com"),
    "yandex.ru": ("smtp.yandex.ru", 465, "ssl", "imap.yandex.ru"),
    "ya.ru": ("smtp.yandex.ru", 465, "ssl", "imap.yandex.ru"),
    "yandex.com": ("smtp.yandex.com", 465, "ssl", "imap.yandex.com"),
    "mail.ru": ("smtp.mail.ru", 465, "ssl", "imap.mail.ru"),
    "bk.ru": ("smtp.mail.ru", 465, "ssl", "imap.mail.ru"),
    "inbox.ru": ("smtp.mail.ru", 465, "ssl", "imap.mail.ru"),
    "list.ru": ("smtp.mail.ru", 465, "ssl", "imap.mail.ru"),
    "icloud.com": ("smtp.mail.me.com", 587, "starttls", "imap.mail.me.com"),
    "me.com": ("smtp.mail.me.com", 587, "starttls", "imap.mail.me.com"),
    "yahoo.com": ("smtp.mail.yahoo.com", 465, "ssl", "imap.mail.yahoo.com"),
}
REQUIREMENT = ("Нужны адрес почты и пароль приложения (для Gmail: myaccount.google.com/apppasswords при включённой "
               "двухэтапной проверке; Яндекс и Mail.ru — «пароли приложений» в настройках безопасности).")
EMAIL_RE = re.compile(r"[^@\s<>\"']+@[^@\s<>\"']+\.[A-Za-z]{2,}")


class EmailProvider(Provider):
    id = "email"
    title = "Почта"
    caps = {"can_send_text": True, "can_schedule": True, "can_search_contacts": True, "can_open_app": False,
            "can_send_file": False, "can_send_image": False, "can_reply": False}

    def __init__(self, db: Database, store=secrets):
        self.db, self.store = db, store
        self.book = AddressBook(db)
        self.state, self.detail = "not_connected", ""
        self._dir: tuple[float, list] | None = None
        self._lock = asyncio.Lock()

    @property
    def account(self) -> dict | None:
        return self.db.get_kv("email_account")

    def status(self) -> Status:
        acc = self.account
        if not acc or not self.store.get(K_PASS):
            return Status("not_connected", "Почта не подключена", requirement=REQUIREMENT)
        if self.state == "error":
            return Status("error", self.detail, acc["address"], requirement=REQUIREMENT)
        return Status("connected", f"SMTP {acc['smtp_host']} · IMAP {acc.get('imap_host') or '—'}", acc["address"])

    # ---------------------------------------------------------------- connect (user-driven)
    async def connect(self, address: str, password: str, smtp_host: str | None = None, smtp_port: int | None = None,
                      imap_host: str | None = None) -> dict:
        address = address.strip()
        if not EMAIL_RE.fullmatch(address):
            raise ValueError("Адрес почты выглядит неверно")
        password = "".join(password.split())        # Google shows app passwords in groups of four
        dom = address.rsplit("@", 1)[1].lower()
        host, port, mode, imap = HOSTS.get(dom, (smtp_host or "", smtp_port or 465, "ssl", imap_host or ""))
        if smtp_host:
            host, port = smtp_host, smtp_port or port
            mode = "starttls" if port == 587 else "ssl"
        if imap_host is not None and imap_host != "":
            imap = imap_host
        if not host:
            raise ValueError("Для этого домена укажите SMTP-сервер")
        acc = {"address": address, "smtp_host": host, "smtp_port": port, "smtp_mode": mode, "imap_host": imap}
        await asyncio.to_thread(_smtp_login_check, acc, password)
        if imap:
            try:
                await asyncio.to_thread(_imap_login_check, acc, password)
            except CommsError:
                acc["imap_host"] = ""            # sending still works; verification falls back to SMTP acceptance
        self.store.store(K_PASS, password, "JARVIS: пароль приложения почты")
        self.db.set_kv("email_account", acc)
        self.state, self.detail, self._dir = "connected", "", None
        return {"account": address, "imap": bool(acc["imap_host"])}

    async def logout(self) -> None:
        self.store.clear(K_PASS)
        self.db.set_kv("email_account", None)
        self.state, self._dir = "not_connected", None

    # ---------------------------------------------------------------- contacts
    async def search(self, name: str) -> list[Contact]:
        acc = self.account
        if is_self(name) and acc:
            return [Contact("email", acc["address"].lower(), "Вы", acc["address"], "ваш ящик")]
        m = EMAIL_RE.search(name)
        if m:
            return [Contact("email", m.group(0).lower(), m.group(0), m.group(0), "адрес из команды")]
        out = self.book.find(name, "email")
        if acc and acc.get("imap_host") and self.store.get(K_PASS):
            try:
                people = await self._directory()
            except CommsError:
                people = []
            for c in people:
                sc = match_score(name, c.name)
                if sc:
                    out.append(Contact(c.channel, c.id, c.name, c.address, c.source, sc))
        return out

    async def _directory(self) -> list[Contact]:
        if self._dir and time.time() - self._dir[0] < 600:
            return self._dir[1]
        acc = self.account
        people = await asyncio.to_thread(_imap_people, acc, self.store.get(K_PASS))
        mine = acc["address"].lower()
        out = [Contact("email", addr, nm, addr, "переписка в почте") for addr, nm in people.items() if addr != mine and nm]
        self._dir = (time.time(), out)
        return out

    # ---------------------------------------------------------------- send / verify
    async def send(self, contact: Contact, body: str, subject: str | None, op_id: str) -> Receipt:
        acc, pw = self.account, self.store.get(K_PASS)
        if not acc or not pw:
            raise CommsError("Почта не подключена. " + REQUIREMENT, "not_connected", retryable=True)
        msg = EmailMessage()
        msg["From"] = acc["address"]
        msg["To"] = email.utils.formataddr((contact.name, contact.id)) if contact.name and "@" not in contact.name \
            else contact.id
        msg["Subject"] = subject or "Без темы"
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(idstring=op_id, domain=acc["address"].rsplit("@", 1)[1])
        msg.set_content(body)
        async with self._lock:
            await asyncio.to_thread(_smtp_send, acc, pw, msg)
        self.state = "connected"
        return Receipt(msg["Message-ID"], f"Сервер {acc['smtp_host']} принял письмо для {contact.id}")

    async def verify(self, contact: Contact, body: str, receipt: Receipt) -> tuple[str, str]:
        acc, pw = self.account, self.store.get(K_PASS)
        if acc and acc.get("imap_host") and pw:
            for _ in range(4):
                try:
                    if await asyncio.to_thread(_imap_find_sent, acc, pw, receipt.remote_id):
                        return "verified", "Письмо лежит в папке «Отправленные»"
                except CommsError:
                    break
                await asyncio.sleep(1.5)
        # SMTP 250 for the recipient is a real postcondition, but weaker than seeing it in "Sent".
        return "verified", f"Почтовый сервер {acc['smtp_host'] if acc else ''} принял письмо (в «Отправленных» пока не видно)"


# ---------------------------------------------------------------- blocking helpers (run in a thread)
def _smtp(acc: dict):
    ctx = ssl.create_default_context()
    try:
        if acc["smtp_mode"] == "ssl":
            s = smtplib.SMTP_SSL(acc["smtp_host"], acc["smtp_port"], timeout=20, context=ctx)
        else:
            s = smtplib.SMTP(acc["smtp_host"], acc["smtp_port"], timeout=20)
            s.starttls(context=ctx)
        return s
    except (OSError, smtplib.SMTPException) as e:
        raise CommsError(f"Нет связи с почтовым сервером {acc['smtp_host']}", "network", repr(e)[:200],
                         retryable=True) from None


def _login(s, acc, password):
    try:
        s.login(acc["address"], password)
    except smtplib.SMTPAuthenticationError as e:
        s.close()
        raise CommsError("Почтовый сервер не принял адрес и пароль приложения", "auth", repr(e)[:200],
                         retryable=False) from None


def _smtp_login_check(acc, password) -> None:
    s = _smtp(acc)
    _login(s, acc, password)
    s.quit()


def _smtp_send(acc, password, msg) -> None:
    s = _smtp(acc)
    _login(s, acc, password)
    try:
        refused = s.send_message(msg)
    except smtplib.SMTPRecipientsRefused as e:
        raise CommsError("Почтовый сервер отказался принять адрес получателя", "rejected", repr(e)[:200]) from None
    except smtplib.SMTPSenderRefused as e:
        raise CommsError("Почтовый сервер отклонил отправителя", "rejected", repr(e)[:200]) from None
    except smtplib.SMTPDataError as e:
        raise CommsError("Почтовый сервер отклонил письмо", "rejected", repr(e)[:200]) from None
    except (OSError, smtplib.SMTPException) as e:
        # The DATA phase may have completed: unknown, never retried automatically.
        raise CommsError("Связь с почтовым сервером оборвалась во время отправки — статус неизвестен", "unknown",
                         repr(e)[:200]) from None
    finally:
        try:
            s.quit()
        except Exception:   # noqa: BLE001
            pass
    if refused:
        raise CommsError("Сервер не принял письмо для: " + ", ".join(refused), "rejected", repr(refused)[:200])


def _imap(acc, password):
    try:
        m = imaplib.IMAP4_SSL(acc["imap_host"], 993, timeout=20)
        m.login(acc["address"], password)
        return m
    except (OSError, imaplib.IMAP4.error, socket.timeout) as e:
        raise CommsError("IMAP недоступен", "network", repr(e)[:200]) from None


def _imap_login_check(acc, password) -> None:
    _imap(acc, password).logout()


def _sent_folder(m) -> str | None:
    typ, data = m.list()
    for raw in data or []:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        if "\\Sent" in line:
            mm = re.search(r'"([^"]+)"\s*$', line) or re.search(r"\s(\S+)\s*$", line)
            if mm:
                return mm.group(1)
    return None


def _imap_find_sent(acc, password, message_id: str) -> bool:
    m = _imap(acc, password)
    try:
        box = _sent_folder(m)
        if not box:
            return False
        m.select('"' + box + '"', readonly=True)
        typ, data = m.uid("SEARCH", None, "HEADER", "Message-ID", '"' + message_id.strip("<>") + '"')
        return bool(data and data[0] and data[0].split())
    finally:
        try:
            m.logout()
        except Exception:   # noqa: BLE001
            pass


def _dec(v: str) -> str:
    try:
        return str(make_header(decode_header(v)))
    except Exception:   # noqa: BLE001
        return v


def _imap_people(acc, password, per_box: int = 300) -> dict[str, str]:
    """address → display name, from the headers of recent mail (INBOX senders, Sent recipients)."""
    m = _imap(acc, password)
    people: dict[str, str] = {}
    try:
        boxes = [("INBOX", "FROM CC")]
        sent = _sent_folder(m)
        if sent:
            boxes.append(('"' + sent + '"', "TO CC"))
        for box, fields in boxes:
            if m.select(box, readonly=True)[0] != "OK":
                continue
            typ, data = m.uid("SEARCH", None, "ALL")
            uids = (data[0].split() if data and data[0] else [])[-per_box:]
            if not uids:
                continue
            typ, rows = m.uid("FETCH", b",".join(uids).decode(), f"(BODY.PEEK[HEADER.FIELDS ({fields})])")
            for r in rows or []:
                if not isinstance(r, tuple):
                    continue
                hdr = r[1].decode("utf-8", "replace")
                hdr = re.sub(r"\r?\n[ \t]+", " ", hdr)
                for line in hdr.splitlines():
                    if ":" not in line:
                        continue
                    for nm, addr in email.utils.getaddresses([_dec(line.split(":", 1)[1])]):
                        addr = addr.lower().strip()
                        if EMAIL_RE.fullmatch(addr) and nm and addr not in people:
                            people[addr] = nm.strip().strip('"')
    finally:
        try:
            m.logout()
        except Exception:   # noqa: BLE001
            pass
    return people
