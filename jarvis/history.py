"""Action history: only what JARVIS really did on the computer — a site or program written, code changed, files
created, a program installed, a note saved. Messages, volume, windows, questions and reminders are NOT kept here
(they have their own places: the conversation and «Сообщения»).

An entry is recorded when a task ends, from the verified results of its steps (never from what a model claims)."""
from __future__ import annotations

import os
import re

from .db import Database, dumps, loads, new_id, now
from .events import EventBus

# tool -> kind of lasting change
LASTING = {"claude_code_change": "change", "computer_task": "change", "create_note": "note", "file_write": "file",
           "folder_create": "file", "sheet_create": "file", "sheet_edit": "file",
           "system_install": "install", "site_publish": "publish"}
TERMINAL = {"completed", "failed", "cancelled", "blocked"}
_WAKE = re.compile(r"^\s*(джарвис|jarvis)[\s,!.:—-]*", re.I)


def _title(request: str) -> str:
    from .router import strip_wake
    t = strip_wake(request or "").strip().rstrip(".")
    t = re.sub(r"\s+", " ", t)
    return (t[:1].upper() + t[1:])[:140] or "Действие"


def _summary(text: str) -> str:
    """First meaningful sentence of the specialist's report, without Markdown (headings like «Отчёт» are skipped)."""
    for line in (text or "").splitlines():
        if line.lstrip().startswith("#"):
            continue
        s = re.sub(r"[*_`>]+", "", line).strip(" -•\t")
        if len(s) > 3 and (len(s.split()) > 3 or s.endswith((".", "!"))):
            if len(s) <= 220:
                return s
            cut = s[:220]
            end = max(cut.rfind(". "), cut.rfind("! "))
            return cut[:end + 1] if end > 60 else cut[:cut.rfind(" ")].rstrip(",;:—-") + "…"
    return ""


def _file(entry: str) -> dict:
    """«+ name» new, «~ name» changed, «- name» deleted (folder snapshot); plain names come from git."""
    op = {"+": "new", "~": "edit", "-": "del"}.get(entry[:1]) if entry[1:2] == " " else None
    return {"p": entry[2:] if op else entry, "op": op or "edit"}


def _short(path: str) -> str:
    home = os.path.expanduser("~")
    return path.replace(home, "~", 1) if path and path.startswith(home) else path


class ActionHistory:
    def __init__(self, db: Database, bus: EventBus):
        self.db, self.bus = db, bus
        bus.listeners.append(self._on_event)

    # ---------------------------------------------------------------- recording
    def _on_event(self, kind: str, ev: dict | None = None) -> None:
        if kind != "task.updated" or not ev:
            return
        task = ev.get("task") or {}
        if task.get("state") in TERMINAL:
            self.record(task)

    def entry_for(self, task: dict) -> dict | None:
        """The history entry a finished task deserves, or None (nothing lasting happened)."""
        steps = [s for s in task.get("steps") or []
                 if s.get("tool_id") in LASTING and s.get("state") in ("succeeded", "failed")]
        if not steps:
            return None
        ok = [s for s in steps if s["state"] == "succeeded"]
        main = (ok or steps)[-1]
        kind = LASTING[main["tool_id"]]
        data = ((main.get("result") or {}).get("data") or {})
        ver = main.get("verification") or {}
        files: list[str] = []
        folder = ""
        if kind == "change":
            folder = (main.get("args") or {}).get("workdir", "")
            for s in steps:
                files += (((s.get("result") or {}).get("data") or {}).get("git") or {}).get("changed") or []
            summary = _summary(data.get("text", "")) or (main.get("error") or "")
        elif kind == "install":
            summary = "Установлено в систему: " + ", ".join((main.get("args") or {}).get("packages") or [])
        elif kind == "publish":
            folder = (main.get("args") or {}).get("folder", "")
            summary = ("Опубликовано: " + data["url"]) if data.get("url") else (main.get("error") or "")
        else:
            path = data.get("path", "")
            folder, files = os.path.dirname(path), [os.path.basename(path)] if path else []
            summary = ""                       # the title and the file name say it all
        files = [_file(f) for f in dict.fromkeys(files)]
        tests = data.get("tests") or {}
        details = {
            "verification": {"status": ver.get("status"), "detail": (ver.get("detail") or "")[:400]},
            "tests": {"ran": bool(tests.get("ran")), "ok": tests.get("code") == 0} if tests else None,
            "backup": _short(((data.get("git") or {}).get("backup")) or "") or None,
            "model": data.get("model"), "cost_usd": data.get("cost_usd"),
            "duration_ms": round(sum(s.get("duration_ms") or 0 for s in steps)) or None,
            "error": main.get("error") if main["state"] == "failed" else None,
            "url": data.get("url"),
        }
        return {"task_id": task["id"], "kind": kind, "title": _title(task.get("request", "")),
                "status": "done" if ok else "failed", "summary": summary, "folder": folder,
                "files": files[:50], "files_total": len(files), "details": details,
                "created_at": task.get("finished_at") or task.get("updated_at") or now()}

    def record(self, task: dict) -> dict | None:
        e = self.entry_for(task)
        if not e:
            return None
        row = self.db.one("SELECT id FROM actions WHERE task_id=?", (e["task_id"],))
        e["id"] = row["id"] if row else new_id("h_")
        self.db.execute(
            "INSERT OR REPLACE INTO actions(id, task_id, created_at, kind, title, status, summary, folder, files, details)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (e["id"], e["task_id"], e["created_at"], e["kind"], e["title"], e["status"], e["summary"], e["folder"],
             dumps({"list": e["files"], "total": e["files_total"]}), dumps(e["details"])))
        self.bus.publish("action.recorded", {"action": self.get(e["id"])})
        return e

    def backfill(self, tasks) -> int:
        """Once: build the history from tasks finished before this feature existed."""
        if self.db.get_kv("actions_backfilled"):
            return 0
        n = 0
        ids = [r["task_id"] for r in self.db.all(
            "SELECT DISTINCT task_id FROM task_steps WHERE tool_id IN (%s)" % ",".join("?" * len(LASTING)),
            tuple(LASTING))]
        for tid in ids:
            t = tasks.get(tid)
            if t and t.get("state") in TERMINAL and self.entry_for(t):
                e = self.entry_for(t)
                e["id"] = new_id("h_")
                self.db.execute(
                    "INSERT OR IGNORE INTO actions(id, task_id, created_at, kind, title, status, summary, folder, files,"
                    " details) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (e["id"], e["task_id"], e["created_at"], e["kind"], e["title"], e["status"], e["summary"],
                     e["folder"], dumps({"list": e["files"], "total": e["files_total"]}), dumps(e["details"])))
                n += 1
        self.db.set_kv("actions_backfilled", True)
        return n

    # ---------------------------------------------------------------- reading
    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        f = loads(d.pop("files"), {}) or {}
        d["files"], d["files_total"] = f.get("list", []), f.get("total", 0)
        d["details"] = loads(d["details"], {}) or {}
        d["folder_short"] = _short(d["folder"] or "")
        return d

    def get(self, aid: str) -> dict | None:
        r = self.db.one("SELECT * FROM actions WHERE id=?", (aid,))
        return self._row(r) if r else None

    def list(self, limit: int = 100) -> list[dict]:
        return [self._row(r) for r in self.db.all("SELECT * FROM actions ORDER BY created_at DESC LIMIT ?",
                                                   (max(1, min(500, int(limit))),))]

    def delete(self, aid: str) -> bool:
        """Removes the entry only — the files on disk are not touched."""
        n = self.db.execute("DELETE FROM actions WHERE id=?", (aid,)).rowcount
        if n:
            self.bus.publish("action.deleted", {"id": aid})
        return bool(n)
