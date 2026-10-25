"""SQLite storage with ordered migrations (PRAGMA user_version)."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    # 1 — core schema
    """
    CREATE TABLE conversations(
        id TEXT PRIMARY KEY, title TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
    CREATE TABLE messages(
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
        role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
        text TEXT NOT NULL, task_id TEXT, meta TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL);
    CREATE INDEX messages_conv ON messages(conversation_id, created_at);
    CREATE TABLE tasks(
        id TEXT PRIMARY KEY, conversation_id TEXT, request TEXT NOT NULL, objective TEXT,
        state TEXT NOT NULL, route TEXT, level INTEGER, provider TEXT, risk TEXT,
        priority INTEGER NOT NULL DEFAULT 5, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        finished_at REAL, error TEXT, result TEXT, checkpoint TEXT, retry_count INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX tasks_state ON tasks(state);
    CREATE TABLE task_steps(
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), seq INTEGER NOT NULL,
        tool_id TEXT NOT NULL, args TEXT NOT NULL, state TEXT NOT NULL, risk TEXT, why TEXT,
        started_at REAL, finished_at REAL, duration_ms REAL, result TEXT, verification TEXT, error TEXT);
    CREATE INDEX steps_task ON task_steps(task_id, seq);
    CREATE TABLE approvals(
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL, step_id TEXT, tool_id TEXT NOT NULL,
        args TEXT NOT NULL, digest TEXT NOT NULL, risk TEXT NOT NULL, summary TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','approved','denied','expired')),
        created_at REAL NOT NULL, decided_at REAL, expires_at REAL NOT NULL, decided_args TEXT);
    CREATE INDEX approvals_status ON approvals(status);
    CREATE TABLE events(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
        task_id TEXT, payload TEXT NOT NULL);
    CREATE TABLE reminders(
        id TEXT PRIMARY KEY, text TEXT NOT NULL, due_at REAL NOT NULL, created_at REAL NOT NULL,
        fired_at REAL, status TEXT NOT NULL CHECK(status IN ('scheduled','fired','missed','cancelled')),
        task_id TEXT);
    CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    # 2 — human-readable step titles
    """
    ALTER TABLE task_steps ADD COLUMN title TEXT;
    """,
    # 3 — project workflow: drafts and decision ledger
    """
    CREATE TABLE drafts(
        id TEXT PRIMARY KEY, project TEXT NOT NULL, reply_to TEXT, to_name TEXT, to_addr TEXT NOT NULL,
        subject TEXT NOT NULL, body TEXT NOT NULL, option TEXT, version INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL CHECK(status IN ('draft','sending','sent','unknown_outcome')),
        operation_id TEXT, provider_message_id TEXT, sent_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
    CREATE TABLE project_decisions(
        id TEXT PRIMARY KEY, project TEXT NOT NULL, subject TEXT NOT NULL, status TEXT NOT NULL,
        note TEXT, source_refs TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL);
    """,
    # 4 — AI agency: decision record per task, provider usage ledger
    """
    ALTER TABLE tasks ADD COLUMN decision TEXT;
    CREATE TABLE provider_usage(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, provider TEXT NOT NULL, model TEXT,
        task_id TEXT, step_id TEXT, capability TEXT, input_tokens INTEGER, output_tokens INTEGER,
        cost_usd REAL, latency_ms REAL, ok INTEGER NOT NULL, error_kind TEXT);
    CREATE INDEX usage_provider ON provider_usage(provider, ts);
    """,
    # 5 — voice turn latency telemetry (epoch seconds per stage)
    """
    CREATE TABLE voice_turns(
        id TEXT PRIMARY KEY, created_at REAL NOT NULL, task_id TEXT, transcript TEXT, route TEXT,
        stt_model TEXT, llm_model TEXT, tts_engine TEXT, error TEXT,
        speech_start REAL, speech_end REAL, stt_start REAL, stt_end REAL, request_start REAL, ttft REAL,
        first_sentence REAL, tts_start REAL, first_audio_sent REAL, first_audio_played REAL, answer_done REAL,
        speech_done REAL);
    """,
    # 6 — communications: outbox (sent + scheduled messages), address book, trusted-automation rules
    """
    CREATE TABLE comm_jobs(
        id TEXT PRIMARY KEY, group_id TEXT, task_id TEXT, step_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        channel TEXT NOT NULL, to_id TEXT NOT NULL, to_name TEXT NOT NULL, to_address TEXT,
        subject TEXT, body TEXT NOT NULL, due_at REAL,
        status TEXT NOT NULL CHECK(status IN ('scheduled','sending','sent','failed','cancelled','unknown','missed')),
        digest TEXT NOT NULL, auth TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, error_detail TEXT, remote_id TEXT, sent_at REAL, verified TEXT, replaced_by TEXT);
    CREATE INDEX comm_jobs_status ON comm_jobs(status, due_at);
    CREATE TABLE contacts(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT, phone TEXT, telegram TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL);
    CREATE TABLE trust_rules(
        id TEXT PRIMARY KEY, channel TEXT NOT NULL, to_id TEXT NOT NULL, to_name TEXT NOT NULL,
        created_at REAL NOT NULL, revoked_at REAL);
    """,
    # 7 — action history: only lasting changes on the computer (sites, code, files, programs, notes)
    """
    CREATE TABLE actions(
        id TEXT PRIMARY KEY, task_id TEXT UNIQUE, created_at REAL NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('done','failed')), summary TEXT, folder TEXT, files TEXT, details TEXT);
    CREATE INDEX actions_time ON actions(created_at DESC);
    """,
    # 8 — scheduled actions: a reminder may carry a command that JARVIS runs at its time (same policy as typed)
    """
    ALTER TABLE reminders ADD COLUMN command TEXT;
    """,
    # 9 — long-term memory: facts the person asked JARVIS to remember (or told about themselves)
    """
    CREATE TABLE memory(
        id TEXT PRIMARY KEY, text TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'user', created_at REAL NOT NULL);
    CREATE INDEX memory_time ON memory(created_at DESC);
    """,
    # 10 — the calendar: JARVIS's own events (subscribed iCal feeds are read on demand, not stored)
    """
    CREATE TABLE calendar_events(
        id TEXT PRIMARY KEY, title TEXT NOT NULL, start_at REAL NOT NULL, end_at REAL NOT NULL,
        all_day INTEGER NOT NULL DEFAULT 0, location TEXT, notes TEXT, remind_min INTEGER, reminder_id TEXT,
        state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','cancelled')), created_at REAL NOT NULL);
    CREATE INDEX calendar_time ON calendar_events(state, start_at);
    """,
    # 11 — watches: «скажи, когда батарея будет ниже 30 %» (jarvis/proactive.py), each fires once
    """
    CREATE TABLE watches(
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', threshold REAL,
        condition TEXT NOT NULL, label TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
        fired_at REAL, last_value TEXT);
    """,
    # 12 — «Возможности» (jarvis/hub, from jarvis-os): capability telemetry, ClawHub discovery, skill updates
    """
    CREATE TABLE capability_stats(
        id TEXT PRIMARY KEY, ok INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
        ms REAL NOT NULL DEFAULT 0, last_used REAL, last_error TEXT);
    CREATE TABLE catalog_items(
        slug TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT, summary TEXT, owner TEXT, version TEXT,
        published REAL, updated REAL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
        baseline INTEGER NOT NULL DEFAULT 0, changelog TEXT, verdict TEXT, downloads INTEGER);
    CREATE TABLE catalog_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, slug TEXT NOT NULL,
        version TEXT, detail TEXT);
    CREATE TABLE update_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, component TEXT NOT NULL, kind TEXT NOT NULL,
        from_version TEXT, to_version TEXT, status TEXT NOT NULL, auto INTEGER NOT NULL DEFAULT 0, detail TEXT);
    CREATE TABLE update_available(
        component TEXT PRIMARY KEY, kind TEXT NOT NULL, current TEXT, latest TEXT, changelog TEXT, verdict TEXT,
        new_requirements TEXT, needs_approval INTEGER NOT NULL, reasons TEXT, checked REAL NOT NULL);
    CREATE TABLE update_quarantine(
        component TEXT NOT NULL, version TEXT NOT NULL, reason TEXT, ts REAL NOT NULL,
        PRIMARY KEY(component, version));
    """,
]


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:16]


def now() -> float:
    return time.time()


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str, separators=(",", ":"))


def loads(s: str | None, default: Any = None) -> Any:
    if s is None:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.migrate()

    def migrate(self) -> int:
        ver = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[ver:], start=ver + 1):
            self.conn.execute("BEGIN")
            try:
                for stmt in filter(None, (s.strip() for s in sql.split(";"))):
                    self.conn.execute(stmt)
                self.conn.execute(f"PRAGMA user_version={i}")
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def tx(self):
        return _Tx(self.conn)

    # kv settings ---------------------------------------------------------
    def get_kv(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return loads(r["value"], default) if r else default

    def set_kv(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, dumps(value)))

    def close(self) -> None:
        self.conn.close()


class _Tx:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
