"""One capability registry over everything JARVIS can use (ported from morph-rq/jarvis-os): its own tools and skills,
its connections (messengers, mail, phone bot, smart home, «Заказы»), OpenClaw's skills, plugins and channels, Hermes's
skills, and the shared skills both agents read.

Every capability has one record (id, provider, kind, method, permissions, auth, latency, side effects, health, version,
source, requirements, telemetry). The model never sees this list wholesale: `select.py` retrieves the few that fit.

Duplicates:
- the same skill reached through two agents (the shared folder, or the same content hash) is one capability with
  several `providers` and one executor;
- an upstream capability JARVIS already serves itself (an agent's web-search skill — JARVIS's fast search is the one
  search path; a weather skill — JARVIS has `weather`) is kept in the catalog but marked `duplicate_of`, so routing never
  picks it and nothing runs twice.

The registry is a cache of what the sources report, saved after each refresh and loaded at start, so capabilities stay
listed while an agent restarts (their health says "disconnected" until it is back).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# How a capability gets things done, best first when the evidence is equal (select.py weighs it).
METHOD_WEIGHT = {"official_api": 1.0, "local_service": 1.0, "native": 1.0, "skill": 0.85, "browser": 0.7, "desktop": 0.6,
                 "vision": 0.5, "agent": 0.55, "build": 0.2}

# JARVIS's own tools that are not plain local actions.
TOOL_METHOD = {
    "web_search": "local_service", "weather": "official_api", "market_quote": "official_api",
    "message_send": "official_api", "message_deliver": "official_api", "home_status": "official_api",
    "home_control": "official_api", "home_secure": "official_api", "open_url": "browser", "computer_task": "desktop",
    "screenshot": "native", "screen_text": "vision", "agent_generate": "agent", "claude_change": "agent",
    "claude_inspect": "agent", "skill_create": "build", "site_publish": "official_api", "orders": "local_service",
    "specialist": "agent", "capability_use": "agent",
}
LATENCY_OF = {"local_service": "fast", "official_api": "fast", "browser": "slow", "desktop": "slow", "vision": "slow",
              "agent": "slow", "build": "slow"}
RISK_OF = {"low": "safe", "moderate": "confirm", "high": "danger", "forbidden": "danger"}

# Upstream skills JARVIS serves itself, so they are listed but never routed to.
NATIVE_DUPLICATES = {
    "weather": "jarvis:tool:weather", "jarvis_research": "jarvis:tool:web_search", "searxng": "jarvis:tool:web_search",
    "web-search": "jarvis:tool:web_search", "web_search": "jarvis:tool:web_search",
    "screenshot": "jarvis:tool:screenshot", "apple-reminders": "jarvis:tool:reminder_create",
    "calendar": "jarvis:tool:calendar_show",
}


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text or "")).casefold()).strip()


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[\w]+", norm(text).replace("_", " ").replace("-", " ")) if len(w) > 1]


SUFFIXES = ("ations", "ation", "ings", "ing", "ers", "er", "ies", "ed", "ly", "s",
            "ами", "ями", "ого", "ему", "ов", "ев", "ах", "ях", "ой", "ый", "ий", "ая", "ое", "ые", "ы", "и", "а",
            "у", "е", "ю", "я")


def stem(w: str) -> str:
    """Crude suffix stripping, enough for "words"~"word", "файлы"~"файл"."""
    for suf in SUFFIXES:
        if len(w) - len(suf) >= 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def terms(text: str) -> list[str]:
    return [stem(w) for w in words(text)]


@dataclass
class Capability:
    id: str
    name: str
    title: str
    description: str
    provider: str                 # jarvis | app | openclaw | hermes | shared
    kind: str                     # tool | skill | app | plugin | channel
    method: str = "native"
    permissions: list[str] = field(default_factory=list)
    risk: str = "safe"            # safe | confirm | danger
    side_effects: str = "none"    # none | local | external
    auth: str = "none"            # none | connected | needs_auth | not_configured
    latency: str = "instant"
    health: str = "ok"            # ok | degraded | disconnected | failed | disabled | not_installed
    state: str = "enabled"        # enabled | disabled | available | quarantined | testing
    version: str = ""
    source: str = ""
    origin: str = ""              # builtin | bundled | clawhub | workspace | shared | dev | hermes
    requires: dict = field(default_factory=dict)
    providers: list[str] = field(default_factory=list)
    duplicate_of: str = ""
    content_hash: str = ""
    aliases: list[str] = field(default_factory=list)
    verified: bool = False
    trusted: bool = False
    updated: float = 0.0
    ok: int = 0                   # telemetry, from capability_stats
    failed: int = 0
    ms: float = 0.0

    @property
    def usable(self) -> bool:
        return (not self.duplicate_of and self.state in ("enabled", "testing")
                and self.health not in ("failed", "disabled", "not_installed"))

    @property
    def reliability(self) -> float:
        n = self.ok + self.failed
        return (self.ok + 1) / (n + 2)   # Laplace: unknown = 0.5, never 0 or 1 from a single run

    def brief(self) -> dict:
        """What a list in the hub needs (short descriptions)."""
        return {"id": self.id, "name": self.name, "title": self.title, "description": self.description[:240],
                "provider": self.provider, "providers": self.providers, "kind": self.kind, "method": self.method,
                "state": self.state, "health": self.health, "auth": self.auth, "usable": self.usable,
                "verified": self.verified, "trusted": self.trusted, "origin": self.origin, "version": self.version,
                "requires": self.requires, "risk": self.risk, "duplicate_of": self.duplicate_of,
                "reliability": round(self.reliability, 2), "ok": self.ok, "failed": self.failed}

    def public(self) -> dict:
        d = asdict(self)
        d["usable"] = self.usable
        d["reliability"] = round(self.reliability, 2)
        return d


class Sleeping(Exception):
    """The agent is stopped on purpose (on demand): its capabilities stay listed and wake it when used."""


def skill_hash(text: str) -> str:
    body = re.sub(r"\s+", " ", text.strip())
    return hashlib.sha256(body.encode()).hexdigest()[:16]


class Registry:
    def __init__(self, db=None, path: Path | None = None) -> None:
        self.db = db
        self.path = path
        self.caps: dict[str, Capability] = {}
        self._index: dict[str, set[str]] = {}
        self.refreshed: dict[str, float] = {}
        self.sources: dict[str, Any] = {}      # name -> async callable returning list[Capability]
        self._lock = asyncio.Lock()
        self.load()

    # ------------------------------------------------------------ persistence
    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        known = set(Capability.__dataclass_fields__)
        for row in data.get("capabilities", []):
            cap = Capability(**{k: v for k, v in row.items() if k in known})
            if cap.provider in ("openclaw", "hermes") and cap.health == "ok":
                cap.health = "disconnected"   # until the agent answers again
            self.caps[cap.id] = cap
        self.refreshed = data.get("refreshed", {})
        self._reindex()

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"saved": time.time(), "refreshed": self.refreshed,
                                       "capabilities": [asdict(c) for c in self.caps.values()]}, ensure_ascii=False))
            tmp.replace(self.path)
        except OSError:
            pass

    # ------------------------------------------------------------ refresh
    def add_source(self, name: str, fn) -> None:
        self.sources[name] = fn

    async def refresh(self, *names: str) -> dict:
        """Ask the named sources (all when none) again; replace what each one owns. A source that fails keeps its
        previous capabilities, marked "disconnected"."""
        async with self._lock:
            report = {}
            for name in names or tuple(self.sources):
                fn = self.sources.get(name)
                if fn is None:
                    continue
                try:
                    found = await fn()
                except Exception as exc:   # noqa: BLE001
                    asleep = isinstance(exc, Sleeping)
                    for cap in self.caps.values():
                        if cap.source == name and cap.health not in ("disabled", "failed"):
                            cap.health = "sleeping" if asleep else "disconnected"
                    report[name] = {"ok": False, "error": str(exc)[:200], "sleeping": asleep}
                    continue
                for cid in [c.id for c in self.caps.values() if c.source == name]:
                    del self.caps[cid]
                for cap in found:
                    cap.source = name
                    cap.updated = time.time()
                    self.caps[cap.id] = cap
                self.refreshed[name] = time.time()
                report[name] = {"ok": True, "count": len(found)}
            self._dedupe()
            self._load_stats()
            self._reindex()
            self.save()
            return report

    def _dedupe(self) -> None:
        for cap in self.caps.values():
            cap.duplicate_of = ""
            cap.providers = [cap.provider]
        # 1. Upstream skills JARVIS serves itself.
        for cap in self.caps.values():
            if cap.kind == "skill" and cap.provider in ("openclaw", "hermes"):
                target = NATIVE_DUPLICATES.get(cap.name)
                if target and target in self.caps:
                    cap.duplicate_of = target
        # 2. One skill seen through several agents: same content, or the same name and no content to compare.
        by_key: dict[str, list[Capability]] = {}
        for c in self.caps.values():
            if c.kind == "skill" and not c.duplicate_of:
                by_key.setdefault(c.content_hash or f"name:{norm(c.name)}", []).append(c)
        rank = {"shared": 0, "jarvis": 1, "openclaw": 2, "hermes": 3}
        for group in by_key.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda c: (rank.get(c.provider, 9), not c.verified))
            keep = group[0]
            for other in group[1:]:
                other.duplicate_of = keep.id
                if other.provider not in keep.providers:
                    keep.providers.append(other.provider)

    def _load_stats(self) -> None:
        if self.db is None:
            return
        for row in self.db.all("SELECT id, ok, failed, ms FROM capability_stats"):
            cap = self.caps.get(row["id"])
            if cap:
                cap.ok, cap.failed, cap.ms = row["ok"], row["failed"], row["ms"]

    def _reindex(self) -> None:
        index: dict[str, set[str]] = {}
        for cap in self.caps.values():
            text = " ".join([cap.name, cap.title, cap.description[:400], " ".join(cap.aliases)])
            for w in set(terms(text)):
                index.setdefault(w, set()).add(cap.id)
        self._index = index

    # ------------------------------------------------------------ telemetry
    def record(self, cap_id: str, ok: bool, ms: float, error: str = "") -> None:
        cap = self.caps.get(cap_id)
        if cap:
            cap.ok += ok
            cap.failed += not ok
            cap.ms = ms if not cap.ms else cap.ms * 0.7 + ms * 0.3
        if self.db is None:
            return
        self.db.execute(
            "INSERT INTO capability_stats(id, ok, failed, ms, last_used, last_error) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET ok = ok + excluded.ok, failed = failed + excluded.failed, "
            "ms = CASE WHEN ms = 0 THEN excluded.ms ELSE ms * 0.7 + excluded.ms * 0.3 END, "
            "last_used = excluded.last_used, last_error = excluded.last_error",
            (cap_id, int(ok), int(not ok), ms, time.time(), error[:300]))

    # ------------------------------------------------------------ lookup
    def get(self, cap_id: str) -> Capability | None:
        return self.caps.get(cap_id)

    def find(self, name: str, provider: str = "") -> Capability | None:
        """By id, or by name (the usable one first)."""
        if name in self.caps:
            return self.caps[name]
        n = norm(name)
        hits = [c for c in self.caps.values() if (norm(c.name) == n or n in [norm(a) for a in c.aliases])
                and (not provider or c.provider == provider)]
        hits.sort(key=lambda c: (not c.usable, bool(c.duplicate_of)))
        return hits[0] if hits else None

    def candidates(self, query_words: list[str]) -> set[str]:
        out: set[str] = set()
        for w in query_words:
            out |= self._index.get(w, set())
            if len(w) >= 5:   # cheap stemming: "merging" ~ "merge", "документы" ~ "документ"
                for k, ids in self._index.items():
                    if k.startswith(w[:5]):
                        out |= ids
        return out

    def list(self, *, provider: str = "", kind: str = "", include_duplicates: bool = False) -> list[Capability]:
        return [c for c in self.caps.values() if (not provider or c.provider == provider) and (not kind or c.kind == kind)
                and (include_duplicates or not c.duplicate_of)]

    def summary(self) -> dict:
        by: dict[str, dict[str, int]] = {}
        for c in self.caps.values():
            d = by.setdefault(c.provider, {})
            d[c.kind] = d.get(c.kind, 0) + 1
        return {"total": len(self.caps), "usable": sum(c.usable for c in self.caps.values()),
                "duplicates": sum(bool(c.duplicate_of) for c in self.caps.values()), "by_provider": by,
                "refreshed": self.refreshed}


# ---------------------------------------------------------------- sources

def jarvis_tools(registry) -> list[Capability]:
    out = []
    for t in registry.tools.values():
        s = t.spec
        method = TOOL_METHOD.get(s.id, "native")
        out.append(Capability(
            id=f"jarvis:tool:{s.id}", name=s.id, title=s.title, description=s.description, provider="jarvis",
            kind="tool", method=method, risk=RISK_OF.get(str(s.risk), "confirm"), side_effects=s.side_effects,
            latency=LATENCY_OF.get(method, "instant"), verified=True, trusted=True, origin="builtin",
            state="enabled" if s.model_callable else "available"))
    return out


def jarvis_skills(config) -> list[Capability]:
    from ..skills import list_skills
    out = []
    for m in list_skills(config):
        out.append(Capability(
            id=f"jarvis:skill:{m['name']}", name=m["name"], title=m.get("title") or m["name"],
            description=str(m.get("description") or ""), provider="jarvis", kind="skill", method="skill",
            risk="confirm", side_effects="local", latency="fast", verified=True, trusted=True, origin="builtin",
            aliases=[str(x) for x in (m.get("say") or [])][:6]))
    return out


def jarvis_apps(apps: list[dict]) -> list[Capability]:
    """JARVIS's own connections: {id, title, description, connected, available, requirement}."""
    out = []
    for a in apps:
        out.append(Capability(
            id=f"app:{a['id']}", name=a["id"], title=a["title"], description=a["description"], provider="app",
            kind="app", method="official_api", side_effects="external", risk="confirm", latency="fast",
            auth="connected" if a.get("connected") else ("needs_auth" if a.get("available", True) else "not_configured"),
            health="ok" if a.get("connected") else "disconnected", state="enabled" if a.get("connected") else "available",
            requires={"setup": a.get("requirement", "")} if not a.get("connected") else {},
            aliases=list(a.get("aliases") or [a["id"], a["title"]]), verified=True, trusted=True, origin="builtin"))
    return out


async def openclaw_caps(oc, trusted: set[str]) -> list[Capability]:
    if oc is None:
        return []
    out = []
    for s in await oc.skills():
        text = ""
        try:
            if s.get("path"):
                text = Path(s["path"]).read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:
            pass
        bundled = s.get("bundled")
        out.append(Capability(
            id=f"openclaw:skill:{s['key']}", name=s["name"], title=s["name"].replace("-", " "),
            description=s["description"], provider="openclaw", kind="skill", method="skill",
            risk="danger", side_effects="external", latency="slow",
            health="ok" if s["eligible"] else ("disabled" if s["disabled"] else "degraded"),
            state="disabled" if s["disabled"] else ("enabled" if s["eligible"] else "available"),
            version=s.get("installed_version") or s.get("version", ""),
            origin="clawhub" if s.get("clawhub") else ("bundled" if bundled else "workspace"),
            requires={"missing": s["missing"]} if s["missing"] else {},
            content_hash=skill_hash(text) if text else "", trusted=bool(bundled) or s["name"] in trusted,
            verified=bool(bundled), aliases=[s["key"]]))
    for p in await oc.plugins():
        if not p["installed"]:
            continue
        channel = "channel" in p["categories"] or p["category"] == "channel"
        out.append(Capability(
            id=f"openclaw:plugin:{p['id']}", name=p["id"], title=p["name"], description=p["description"],
            provider="openclaw", kind="channel" if channel else "plugin",
            method="official_api" if channel else "native", risk="confirm", side_effects="external", latency="fast",
            health="ok" if p["enabled"] and p["runtime"] != "error" else ("failed" if p["runtime"] == "error" else "disabled"),
            state="enabled" if p["enabled"] else "disabled", version=p["version"],
            trusted=p["origin"] == "bundled" or p["id"] in trusted, verified=p["origin"] == "bundled",
            origin="bundled" if p["origin"] == "bundled" else "clawhub", aliases=[p["package"]] if p["package"] else []))
    for ch in await oc.channels():
        cid = f"openclaw:channel:{ch['channel']}" + (f":{ch['account']}" if ch["account"] else "")
        out.append(Capability(
            id=cid, name=ch["channel"], title=ch["label"], description=f"{ch['label']} через OpenClaw: сообщения туда и обратно.",
            provider="openclaw", kind="channel", method="official_api", risk="confirm", side_effects="external",
            auth="connected" if ch["state"] == "connected" else ("not_configured" if ch["state"] == "not_configured" else "needs_auth"),
            health={"connected": "ok", "connecting": "degraded", "error": "failed", "disabled": "disabled"}.get(ch["state"], "disconnected"),
            latency="fast", aliases=[ch["channel"], ch["label"]], verified=True, trusted=True, origin="bundled"))
    return out


def hermes_caps(hermes) -> list[Capability]:
    if hermes is None:
        return []
    out = []
    for s in hermes.skills():
        dev = s["category"] == "jarvis-dev"
        out.append(Capability(
            id=f"hermes:skill:{s['name']}", name=s["name"], title=s["name"].replace("-", " "), description=s["description"],
            provider="hermes", kind="skill", method="skill", risk="confirm", side_effects="local", latency="slow",
            health="ok" if hermes.state == "connected" else "disconnected", version=s.get("version", ""),
            content_hash=skill_hash(s["text"]), aliases=[s["category"]] if s["category"] else [],
            origin="dev" if dev else "hermes", state="testing" if dev else "enabled"))
    return out


def shared_caps(folder: Path) -> list[Capability]:
    """Skills the person approved into the shared folder; .jarvis.json beside SKILL.md says who runs it."""
    from .hermes import parse_frontmatter
    out = []
    if not folder.is_dir():
        return out
    for md in sorted(folder.glob("*/SKILL.md")):
        text = md.read_text(encoding="utf-8", errors="replace")[:20000]
        fm = parse_frontmatter(text)
        meta: dict = {}
        meta_path = md.parent / ".jarvis.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except ValueError:
                meta = {}
        name = fm.get("name") or md.parent.name
        out.append(Capability(
            id=f"shared:skill:{name}", name=name, title=name.replace("-", " "), description=fm.get("description", ""),
            provider="shared", kind="skill", method="skill", risk="confirm", side_effects="local", latency="slow",
            origin="shared", version=fm.get("version", ""), content_hash=skill_hash(text),
            verified=bool(meta.get("verified")), trusted=bool(meta.get("trusted")), state=meta.get("state", "enabled"),
            requires={"executor": meta.get("executor", "hermes")}, aliases=meta.get("aliases", [])))
    return out
