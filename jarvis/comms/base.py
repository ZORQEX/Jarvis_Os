"""Provider contract shared by every messaging service."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

CAPABILITIES = ("can_send_text", "can_schedule", "can_send_file", "can_send_image", "can_reply",
                "can_search_contacts", "can_open_app")

CHANNEL_RU = {"telegram": "Telegram", "whatsapp": "WhatsApp", "email": "почта"}
CHANNEL_TO = {"telegram": "в Telegram", "whatsapp": "в WhatsApp", "email": "на почту"}

UNSUPPORTED = "В этом сервисе я пока не могу выполнить такую операцию."


@dataclass
class Contact:
    channel: str
    id: str                 # provider-stable id: Telegram "user_id:access_hash" / "self", e-mail address, phone digits
    name: str
    address: str = ""       # what the user recognises: @username, +7…, name@mail
    source: str = ""        # where it was found (Telegram contacts, mail history, address book)
    score: float = 1.0

    def label(self) -> str:
        return f"{self.name} ({self.address})" if self.address and self.address != self.name else self.name

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Receipt:
    remote_id: str
    detail: str


class CommsError(Exception):
    """kind: not_connected | unsupported | not_found | auth | network | rejected | unknown.
    retryable=True only when the message certainly did NOT leave (so a retry cannot duplicate it)."""

    def __init__(self, user_msg: str, kind: str = "rejected", detail: str = "", retryable: bool = False):
        super().__init__(user_msg)
        self.kind, self.detail, self.retryable = kind, detail, retryable


@dataclass
class Status:
    state: str              # connected | not_connected | connecting | error
    detail: str = ""
    account: str = ""
    requirement: str = ""   # exact configuration needed when not connected
    extra: dict = field(default_factory=dict)


class Provider:
    id = "?"
    title = "?"
    caps: dict = {}

    def status(self) -> Status:
        return Status("not_connected")

    def connected(self) -> bool:
        return self.status().state == "connected"

    def describe(self) -> dict:
        st = self.status()
        return {"id": self.id, "title": self.title, "state": st.state, "detail": st.detail, "account": st.account,
                "requirement": st.requirement, "caps": {k: bool(self.caps.get(k)) for k in CAPABILITIES}, **st.extra}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def search(self, name: str) -> list[Contact]:
        return []

    async def send(self, contact: Contact, body: str, subject: str | None, op_id: str) -> Receipt:
        raise CommsError(UNSUPPORTED, "unsupported")

    async def verify(self, contact: Contact, body: str, receipt: Receipt) -> tuple[str, str]:
        """('verified'|'unverified'|'failed', detail) — checks the real postcondition at the service."""
        return "unverified", "Сервис не позволяет проверить доставку"


def content_digest(channel: str, to_id: str, subject: str | None, body: str) -> str:
    canon = json.dumps([channel, to_id, subject or "", body], ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()
