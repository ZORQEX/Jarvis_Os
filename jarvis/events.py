"""Event bus (live UI updates, persisted with monotonic seq) and audit log."""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from .db import Database, dumps, now

# Fields whose values never reach logs.
_SECRET_KEYS = re.compile(r"(api[_-]?key|token|secret|password|passwd|cookie|authorization|audio_b64)", re.I)
_SECRET_VALUES = re.compile(r"(AIza[0-9A-Za-z_\-]{20,}|sk-[A-Za-z0-9]{20,}|ya29\.[0-9A-Za-z_\-]+)")


def redact(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: ("<redacted>" if _SECRET_KEYS.search(str(k)) else redact(x)) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [redact(x) for x in v]
    if isinstance(v, str):
        return _SECRET_VALUES.sub("<redacted>", v)
    return v


class AuditLog:
    """Append-only JSONL audit trail, separate from task state. Rotates at max_bytes."""

    def __init__(self, path: Path, max_bytes: int = 5_000_000, keep: int = 3):
        self.path, self.max_bytes, self.keep = path, max_bytes, keep

    def write(self, kind: str, **fields: Any) -> None:
        rec = {"ts": round(time.time(), 3), "kind": kind, **redact(fields)}
        try:
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                for i in range(self.keep - 1, 0, -1):
                    src = self.path.with_suffix(f".jsonl.{i}")
                    if src.exists():
                        src.rename(self.path.with_suffix(f".jsonl.{i + 1}"))
                self.path.rename(self.path.with_suffix(".jsonl.1"))
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            self.path.chmod(0o600)
        except OSError:
            pass  # auditing must never take the assistant down


class EventBus:
    # Event kinds that are UI-only noise and need not be stored.
    TRANSIENT = {"assistant.delta", "step.progress", "core.state", "voice.level", "speech.audio", "speech.end",
                 "speech.cancelled", "speech.error"}

    def __init__(self, db: Database):
        self.db = db
        self._subs: set[asyncio.Queue] = set()
        self.listeners: list = []   # sync callbacks(kind) inside the core

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, kind: str, payload: dict[str, Any] | None = None, task_id: str | None = None) -> dict:
        payload = payload or {}
        ev = {"kind": kind, "ts": now(), "task_id": task_id, **payload}
        if kind not in self.TRANSIENT:
            cur = self.db.execute("INSERT INTO events(ts,kind,task_id,payload) VALUES(?,?,?,?)",
                                  (ev["ts"], kind, task_id, dumps(redact(payload))))
            ev["seq"] = cur.lastrowid
        for fn in self.listeners:
            try:
                fn(kind, ev)
            except Exception:   # noqa: BLE001 — a listener must not break publishing
                pass
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass  # slow client; it will resync from snapshot on reconnect
        return ev
