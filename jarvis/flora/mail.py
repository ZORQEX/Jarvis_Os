"""Mail provider boundary.

DemoMailbox serves the SYNTHETIC Flora fixtures and keeps a local "Sent" folder.
A Gmail adapter will implement the same methods (list/get/attachment/send/sent)
with OAuth; it is not implemented yet and nothing here talks to the network.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Protocol

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "flora"


class MailProvider(Protocol):
    name: str
    live: bool

    def list_messages(self) -> list[dict]: ...
    def get(self, msg_id: str) -> dict | None: ...
    def attachment_path(self, name: str) -> Path | None: ...
    def send(self, operation_id: str, message: dict) -> dict: ...
    def sent(self, operation_id: str) -> dict | None: ...


class DemoMailbox:
    name = "demo"
    live = False

    def __init__(self, state_dir: Path, fixtures: Path = FIXTURES):
        self.fixtures = fixtures
        self.outbox = state_dir / "demo-mail" / "sent"
        self.outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = json.loads((fixtures / "mailbox.json").read_text("utf-8"))
        self.account = data["account"]
        self._msgs = {m["id"]: m for m in data["messages"]}

    def list_messages(self) -> list[dict]:
        return sorted(self._msgs.values(), key=lambda m: m["date"], reverse=True)

    def get(self, msg_id: str) -> dict | None:
        return self._msgs.get(msg_id)

    def attachment_path(self, name: str) -> Path | None:
        p = (self.fixtures / name).resolve()
        return p if p.is_relative_to(self.fixtures.resolve()) and p.is_file() else None

    def _op_file(self, operation_id: str) -> Path:
        safe = hashlib.sha256(operation_id.encode()).hexdigest()[:32]
        return self.outbox / f"{safe}.json"

    def sent(self, operation_id: str) -> dict | None:
        f = self._op_file(operation_id)
        return json.loads(f.read_text("utf-8")) if f.exists() else None

    def send(self, operation_id: str, message: dict) -> dict:
        """Idempotent by operation_id: a repeated call returns the first result."""
        prior = self.sent(operation_id)
        if prior:
            return {**prior, "duplicate": True}
        rec = {"operation_id": operation_id, "provider_message_id": "demo-" + hashlib.sha256(
            (operation_id + str(time.time())).encode()).hexdigest()[:12], "sent_at": time.time(),
            "from": self.account, **message}
        tmp = self._op_file(operation_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self._op_file(operation_id))   # atomic: either fully sent or not at all
        return {**rec, "duplicate": False}

    def sent_all(self) -> list[dict]:
        out = []
        for f in sorted(self.outbox.glob("*.json")):
            try:
                out.append(json.loads(f.read_text("utf-8")))
            except ValueError:
                continue
        return sorted(out, key=lambda r: r["sent_at"])
