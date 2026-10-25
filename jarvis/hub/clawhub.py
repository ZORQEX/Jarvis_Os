"""ClawHub discovery for the «Возможности» panel: what exists, what is new, what changed. Read only
(ported from morph-rq/jarvis-os).

Searching uses OpenClaw's own `skills.search` (results are exactly what OpenClaw can install); the periodic sync reads
ClawHub's listing directly (GET /api/v1/skills?sort=createdAt|updated), the same endpoints the clawhub CLI uses. Nothing
found here is installed or run: installing is a separate step through OpenClaw, after the person approves it.

Four facts are kept apart for each item: published (the registry's date), discovered (when JARVIS first saw it — the
first sync is the baseline, nothing is "new" then), installed (OpenClaw has it), enabled (OpenClaw may use it).
Registry text (names, summaries, changelogs) is untrusted: it is shown, never followed.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import aiohttp

DEFAULT = "https://clawhub.ai"
NEW_DAYS = 14


def _ms(value) -> float | None:
    return value / 1000 if isinstance(value, (int, float)) and value > 1e11 else (value or None)


def _clean(text: Any, n: int = 400) -> str:
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(text or ""))[:n]


class ClawHub:
    def __init__(self, db, base_url: str = "", openclaw=None) -> None:
        self.db = db
        self.base = (base_url or DEFAULT).rstrip("/")
        self.oc = openclaw
        self.last_sync = float(db.get_kv("clawhub_last_sync") or 0)
        self.last_error = ""
        self.installed: dict[str, str] = {}     # slug -> installed version (from updates.Lifecycle.installed)

    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(self.base + path, params=params, allow_redirects=False) as r:
                r.raise_for_status()
                text = await r.text()
                return json.loads(text) if "json" in r.headers.get("content-type", "json") else text

    # ------------------------------------------------------------ search
    async def search(self, query: str, limit: int = 10) -> list[dict]:
        """Through OpenClaw when it is connected (its own search and trust rules), else ClawHub directly."""
        if self.oc is not None and self.oc.c.connected:
            rows = await self.oc.skill_search(query, limit)
        else:
            body = await self._get("/api/v1/search", {"q": query, "limit": str(limit)})
            rows = (body or {}).get("results") or []
        out = []
        for r in rows[:limit]:
            slug = str(r.get("slug", ""))
            item = self.db.one("SELECT verdict FROM catalog_items WHERE slug = ?", (slug,))
            out.append({"slug": slug, "ref": r.get("installRef") or f"@{r.get('ownerHandle', '')}/{slug}".replace("@/", ""),
                        "title": _clean(r.get("displayName") or slug, 80), "summary": _clean(r.get("summary")),
                        "version": str(r.get("version") or ""), "owner": _clean(r.get("ownerHandle"), 60),
                        "updated": _ms(r.get("updatedAt")), "installed": slug in self.installed,
                        "verdict": item["verdict"] if item else "", "source": "clawhub"})
        return out

    # ------------------------------------------------------------ sync
    async def sync(self, limit: int = 100) -> dict:
        """Read the newest and recently updated skills; record what JARVIS had not seen before."""
        now = time.time()
        first = self.db.one("SELECT 1 FROM catalog_items LIMIT 1") is None
        seen: dict[str, dict] = {}
        try:
            for sort in ("createdAt", "updated"):
                body = await self._get("/api/v1/skills", {"sort": sort, "limit": str(limit)})
                for it in (body or {}).get("items") or []:
                    seen.setdefault(str(it.get("slug", "")), it)
        except Exception as exc:   # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            return {"ok": False, "error": self.last_error}
        new, updated = [], []
        for slug, it in seen.items():
            if not slug:
                continue
            latest = it.get("latestVersion") or {}
            version = str(latest.get("version") or (it.get("tags") or {}).get("latest") or "")
            verdict = (it.get("moderation") or {}).get("verdict") or ""
            row = self.db.one("SELECT version FROM catalog_items WHERE slug = ?", (slug,))
            fields = (_clean(it.get("displayName") or slug, 80), _clean(it.get("summary")),
                      _clean((it.get("owner") or {}).get("handle"), 60), version, _ms(it.get("createdAt")),
                      _ms(latest.get("createdAt") or it.get("updatedAt")), _clean(latest.get("changelog"), 2000), verdict,
                      int((it.get("stats") or {}).get("downloads") or 0))
            if row is None:
                self.db.execute(
                    "INSERT INTO catalog_items(slug, kind, title, summary, owner, version, published, updated, first_seen, "
                    "last_seen, baseline, changelog, verdict, downloads) VALUES (?, 'skill', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (slug, *fields[:6], now, now, int(first), *fields[6:]))
                if not first:
                    new.append(slug)
                    self.event("discovered", slug, version)
            else:
                if row["version"] != version:
                    updated.append(slug)
                    self.event("published", slug, version, fields[6][:300])
                self.db.execute(
                    "UPDATE catalog_items SET title=?, summary=?, owner=?, version=?, published=?, updated=?, last_seen=?, "
                    "changelog=?, verdict=?, downloads=? WHERE slug=?", (*fields[:6], now, *fields[6:], slug))
        self.last_sync, self.last_error = now, ""
        self.db.set_kv("clawhub_last_sync", now)
        return {"ok": True, "seen": len(seen), "new": new, "updated": updated, "baseline": first}

    def event(self, kind: str, slug: str, version: str = "", detail: str = "") -> None:
        self.db.execute("INSERT INTO catalog_events(ts, kind, slug, version, detail) VALUES (?, ?, ?, ?, ?)",
                        (time.time(), kind, slug, version, detail[:1000]))

    def items(self, *, view: str = "new", limit: int = 40) -> list[dict]:
        """Catalog rows for the panel. view: new (discovered after the baseline), recent (published lately), all."""
        if view == "new":
            rows = self.db.all("SELECT * FROM catalog_items WHERE baseline = 0 ORDER BY first_seen DESC LIMIT ?", (limit,))
        elif view == "recent":
            rows = self.db.all("SELECT * FROM catalog_items WHERE published > ? ORDER BY published DESC LIMIT ?",
                               (time.time() - NEW_DAYS * 86400, limit))
        else:
            rows = self.db.all("SELECT * FROM catalog_items ORDER BY updated DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["installed"] = d["slug"] in self.installed
            d["new"] = not d["baseline"]
            out.append(d)
        return out

    def events(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.all("SELECT ts, kind, slug, version, detail FROM catalog_events "
                                             "ORDER BY id DESC LIMIT ?", (limit,))]
