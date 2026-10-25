"""«Возможности» — OpenClaw, Hermes, skills and connections inside JARVIS (from morph-rq/jarvis-os; ADR-008).

JARVIS stays the one assistant: its voice, window, approvals, memory and task list. Two upstream agents run beside it
as supervised local services and reach the person only through JARVIS:

- OpenClaw (gateway on 127.0.0.1:18789, token): chat channels it connects (Telegram, WhatsApp, Slack, Discord…), its
  plugins and skills, ClawHub installs, its automations. It thinks with Claude through the `claude` program.
- Hermes Agent (API server on 127.0.0.1:18642, key): longer multi-step work, writing and improving skills. It thinks
  with Claude through JARVIS's /llm/v1 endpoint (hub/llm.py).

This module owns them (backends.py), the one capability registry over everything (registry.py, select.py), ClawHub
discovery (clawhub.py), the skill lifecycle (updates.py), self-extension (extend.py), and the panel's operations. The
model reaches it through the tools in hub/tools.py; the window through the ws requests `hub.*` (server.py).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets as pysecrets
import time

from ..events import spawn
from . import registry as reg
from .backends import Backends, research_bridge
from .registry import Capability, Registry
from .select import plan

PHONE_TOKEN = "phone_bot_token"      # jarvis/phone.py TOKEN_KEY: JARVIS's own phone bot

# How each chat channel is connected through OpenClaw, said plainly. `flow` is what JARVIS can drive itself.
CHANNELS = {
    "telegram": {"label": "Telegram", "flow": "token",
                 "needs": "токен бота от @BotFather (в Telegram отправьте @BotFather команду /newbot и вставьте токен)"},
    "whatsapp": {"label": "WhatsApp", "flow": "terminal", "plugin": "whatsapp",
                 "needs": "плагин WhatsApp в OpenClaw и QR-код, отсканированный телефоном (WhatsApp → Связанные устройства)",
                 "command": "openclaw channels login --channel whatsapp"},
    "discord": {"label": "Discord", "flow": "terminal", "plugin": "discord",
                "needs": "плагин Discord в OpenClaw и токен бота с портала разработчиков Discord",
                "command": "openclaw channels add --channel discord"},
    "slack": {"label": "Slack", "flow": "terminal", "plugin": "slack",
              "needs": "плагин Slack в OpenClaw и приложение Slack с токенами бота и приложения",
              "command": "openclaw channels add --channel slack"},
    "signal": {"label": "Signal", "flow": "terminal", "plugin": "signal",
               "needs": "плагин Signal в OpenClaw и signal-cli, связанный с вашим телефоном",
               "command": "openclaw channels add --channel signal"},
}
# Served by JARVIS itself: never connected a second time through OpenClaw.
JARVIS_CHANNELS = {"telegram-account": "Telegram (ваш аккаунт)", "whatsapp-account": "WhatsApp (ваш аккаунт)",
                   "email": "Почта"}


class Hub:
    def __init__(self, config, paths, db, bus, audit, store, gateway, tasks) -> None:
        self.cfg, self.paths, self.db, self.bus, self.audit, self.store = config, paths, db, bus, audit, store
        self.gateway, self.tasks = gateway, tasks
        self.backends = Backends(config, paths, db, bus, audit, store)
        self.registry = Registry(db, paths.data / "hub" / "registry.json")
        self.clawhub = None
        self.lifecycle = None
        self.llm = None              # ClaudeModel for Hermes (set by app.build)
        self.director = None
        self.notify = None           # (kind, title, body) -> None: Proactive.say
        self.apps = lambda: []       # JARVIS's own connections for the registry (set by app.build)
        self.installed: dict[str, str] = {}
        self.delegations: dict[str, object] = {}   # task id -> the step that hands work to an agent (research MCP)
        self._task: asyncio.Task | None = None
        self._inflight: dict[str, asyncio.Future] = {}
        self._refresh_pending: set[str] = set()
        self.research_secret = pysecrets.token_hex(24)
        self.events: list[dict] = []               # recent activity for the panel (in memory)
        r = self.registry
        r.add_source("jarvis", self._src_jarvis)
        r.add_source("apps", self._src_apps)
        r.add_source("openclaw", self._src_openclaw)
        r.add_source("hermes", self._src_hermes)
        r.add_source("shared", self._src_shared)

    def enabled(self) -> bool:
        return bool(self.cfg.openclaw_enabled or self.cfg.hermes_enabled)

    @property
    def oc(self):
        b = self.backends
        return b.openclaw if b.openclaw is not None and b.openclaw.c.connected else None

    @property
    def hermes(self):
        return self.backends.hermes

    async def need_oc(self):
        """OpenClaw, started first when it sleeps (on demand). None when it is off, not installed or did not start."""
        b = self.backends
        if b.openclaw is None:
            return None
        if not b.openclaw.c.connected and not await b.wake("openclaw"):
            return None
        b.touch("openclaw")
        return self.oc

    async def _src_openclaw(self) -> list[Capability]:
        if self.oc is None and self.backends.openclaw is not None and self.backends.on_demand("openclaw"):
            raise reg.Sleeping("OpenClaw спит до первой задачи")
        return await reg.openclaw_caps(self.oc, set(self.cfg.hub_trusted))

    async def _src_jarvis(self) -> list[Capability]:
        return reg.jarvis_tools(self.gateway.registry) + reg.jarvis_skills(self.cfg)

    async def _src_apps(self) -> list[Capability]:
        return reg.jarvis_apps(self.apps())

    async def _src_hermes(self) -> list[Capability]:
        h = self.hermes
        if h is None:
            return []
        if h.state == "unknown":
            await h.status()
        return reg.hermes_caps(h)

    async def _src_shared(self) -> list[Capability]:
        return reg.shared_caps(self.backends.layout.shared_skills)

    # ------------------------------------------------------------ lifecycle
    def write_research_bridge(self) -> None:
        """The file the agents' MCP server reads: the core's URL, a per-run key, and only the web_search tool."""
        tool = self.gateway.registry.get("web_search")
        path = research_bridge(self.backends.layout)
        if tool is None or not self.cfg.hub_share_research:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        spec = tool.spec
        data = {"url": f"http://127.0.0.1:{self.cfg.port}/mcp/research", "secret": self.research_secret, "timeout_s": 120,
                "tools": [{"name": "web_search", "description": spec.description, "inputSchema": spec.params}]}
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    async def start(self) -> None:
        from .clawhub import ClawHub
        from .updates import Lifecycle
        self.write_research_bridge()
        if self.enabled():
            await self.backends.start()
        oc = self.backends.openclaw
        self.clawhub = ClawHub(self.db, self.cfg.clawhub_url, oc)
        self.lifecycle = Lifecycle(self.db, oc, self, backups=self.paths.data / "hub" / "backups",
                                   policy={"trusted": self.cfg.hub_trusted,
                                           "auto_update_skills": self.cfg.hub_auto_update_skills},
                                   busy=self._busy, notify=lambda t, b: self._notice("hub_update", t, b))
        self.backends.on_change(self._backend_changed)
        await self.registry.refresh("jarvis", "apps", "shared")
        self._task = spawn(self._loop(), "hub")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.backends.stop()

    def _busy(self) -> bool:
        return bool(self.tasks.active())

    def _notice(self, kind: str, title: str, body: str = "") -> None:
        if self.notify:
            try:
                self.notify(kind, title, body)
            except Exception:   # noqa: BLE001
                pass

    def event(self, kind: str, slug: str, version: str = "", detail: str = "") -> None:
        if self.clawhub is not None:
            self.clawhub.event(kind, slug, version, detail)
        self.bus.publish("hub.event", {"what": kind, "slug": slug, "version": version, "detail": detail[:300]})

    def _backend_changed(self, name: str, what: str) -> None:
        if name in self._refresh_pending:
            return
        self._refresh_pending.add(name)

        async def later():
            await asyncio.sleep(0.5)   # coalesce bursts of change events
            self._refresh_pending.discard(name)
            await self.registry.refresh(name)
            self.bus.publish("hub.catalog", {"summary": self.registry.summary()})
        spawn(later(), f"hub-refresh-{name}")

    async def _loop(self) -> None:
        await asyncio.sleep(5)
        b = self.backends
        if (b.openclaw is not None and b.on_demand("openclaw") and b.layout.installed("openclaw")
                and not self.registry.list(provider="openclaw", include_duplicates=True)):
            await b.wake("openclaw")          # the first catalog of its skills; it sleeps again when idle
        await self.registry.refresh("openclaw", "hermes")
        self.bus.publish("hub.catalog", {"summary": self.registry.summary()})
        next_sync = next_check = 0.0
        while True:
            now = time.time()
            try:
                if now >= next_sync and self.enabled():
                    result = await self.clawhub.sync()           # ClawHub's own listing: no OpenClaw needed
                    next_sync = now + self.cfg.hub_sync_hours * 3600
                    if result.get("ok") and result.get("new"):
                        self._notice("hub_new", f"Новых навыков в ClawHub: {len(result['new'])}",
                                     ", ".join(result["new"][:5]))
                has_clawhub = any(c.origin == "clawhub" for c in self.registry.list(provider="openclaw"))
                if now >= next_check and not has_clawhub:
                    next_check = now + self.cfg.hub_update_check_hours * 3600
                elif now >= next_check and await self.need_oc() is not None:
                    found = await self.lifecycle.check()
                    done = await self.lifecycle.auto_apply()
                    waiting = [f for f in found if f["needs_approval"]]
                    if waiting:
                        self._notice("hub_update", f"Обновлений ждут вашего решения: {len(waiting)}",
                                     ", ".join(f"{w['component']} {w['latest']}" for w in waiting[:4]))
                    if any(d.get("ok") for d in done):
                        await self.registry.refresh("openclaw")
                    next_check = now + self.cfg.hub_update_check_hours * 3600
            except Exception as exc:   # noqa: BLE001 — the loop must keep going
                self.audit.write("hub.loop_error", error=str(exc)[:200])
            await asyncio.sleep(300)

    # ------------------------------------------------------------ queries
    def find(self, text: str) -> dict:
        t0 = time.perf_counter()
        p = plan(self.registry, text)
        p["ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return p

    async def search(self, text: str) -> dict:
        """Installed first; then what ClawHub offers (through OpenClaw), marked as not installed."""
        p = self.find(text)
        remote = []
        if self.clawhub is not None:
            try:
                remote = await self.clawhub.search(text, 6)
            except Exception as exc:   # noqa: BLE001
                p["registry_error"] = str(exc)[:200]
        p["registry"] = remote
        return p

    def snapshot(self) -> dict:
        caps = self.registry.list(include_duplicates=True)
        return {"summary": self.registry.summary(),
                "tools": [c.brief() for c in caps if c.kind == "tool"],
                "skills": [c.brief() for c in caps if c.kind == "skill"],
                "plugins": [c.brief() for c in caps if c.kind == "plugin"],
                "channels": [c.brief() for c in caps if c.kind == "channel"],
                "apps": [c.brief() for c in caps if c.kind == "app"]}

    # ------------------------------------------------------------ connections
    def channel_owner(self, channel: str) -> str:
        """openclaw, unless JARVIS's own phone bot uses the same Telegram bot (two programs polling one bot fail)."""
        if channel == "telegram":
            own = self.store.get(PHONE_TOKEN)
            theirs = self.store.get("hub-openclaw-telegram")
            if own and theirs and own == theirs:
                return "jarvis"
        return "openclaw"

    async def connect_plan(self, service: str) -> dict:
        """What connecting `service` takes, without changing anything."""
        s = reg.norm(service).replace("телеграм", "telegram").replace("вотсап", "whatsapp").replace("ватсап", "whatsapp")
        s = s.replace("дискорд", "discord").replace("слак", "slack").replace("сигнал", "signal").split()[0] if s else ""
        if s in CHANNELS:
            info = CHANNELS[s]
            out = {"service": s, "label": info["label"], "via": "openclaw", "flow": info["flow"], "needs": info["needs"]}
            if self.backends.openclaw is None or not self.backends.layout.installed("openclaw"):
                out["blocked"] = "OpenClaw не установлен или выключен (Возможности → Обзор)."
            if info["flow"] == "terminal":
                out["command"] = info["command"]
            return out
        return {"service": service, "via": "unknown",
                "needs": "Готового подключения нет. Могу поискать навык или плагин в ClawHub или написать новый навык."}

    async def connect_telegram(self, token: str) -> dict:
        """Hand a bot token to OpenClaw through its environment (kept in the keyring), enable the channel with
        OpenClaw's pairing policy for direct messages, restart the gateway and check that it connects."""
        b = self.backends
        if await self.need_oc() is None:
            return {"ok": False, "message": "OpenClaw не запустился."}
        b.set_keep(True)                     # a channel must be listened to: OpenClaw stays running from now on
        own = self.store.get(PHONE_TOKEN)
        if own and own == token:
            return {"ok": False, "message": "Это токен бота, через которого JARVIS уже отвечает на телефоне — "
                                            "второй раз его подключать нельзя (два чтеца одного бота мешают друг другу)."}
        await asyncio.to_thread(self.store.store, "hub-openclaw-telegram", token, "JARVIS: бот Telegram в OpenClaw")
        await b.openclaw.config_patch({"channels": {"telegram": {"enabled": True, "dmPolicy": "pairing"}}})
        await b.restart("openclaw")
        if not await b.openclaw.c.wait_ready(90):
            return {"ok": False, "message": "OpenClaw не вернулся после перезапуска."}
        state = await self.wait_channel("telegram", ("connected",), 45)
        await self.registry.refresh("openclaw")
        self.audit.write("hub.channel_connected", channel="telegram", state=state)
        if state == "connected":
            return {"ok": True, "state": state, "message": "Telegram подключён через OpenClaw. Новые чаты сначала "
                    "проходят сопряжение: человек пишет боту, вы подтверждаете код."}
        return {"ok": False, "state": state, "message": f"Telegram не подключился ({state}). Проверьте токен."}

    async def wait_channel(self, channel: str, states: tuple[str, ...], timeout: float) -> str:
        deadline = time.monotonic() + timeout
        state = "unknown"
        while time.monotonic() < deadline:
            try:
                rows = [c for c in await self.backends.openclaw.channels() if c["channel"] == channel]
                state = rows[0]["state"] if rows else "not_configured"
                if state in states:
                    return state
            except Exception as exc:   # noqa: BLE001
                state = f"недоступен: {str(exc)[:80]}"
            await asyncio.sleep(1)
        return state

    async def disconnect_channel(self, channel: str) -> dict:
        oc = await self.need_oc()
        if oc is None:
            return {"ok": False, "message": "OpenClaw не запущен."}
        try:
            await oc.c.request("channels.logout", {"channel": channel}, timeout=30)
        except Exception:   # noqa: BLE001 — not logged in: still disable it
            pass
        await oc.config_patch({"channels": {channel: {"enabled": False}}})
        if channel == "telegram":
            await asyncio.to_thread(self.store.clear, "hub-openclaw-telegram")
        await self.registry.refresh("openclaw")
        self.audit.write("hub.channel_disconnected", channel=channel)
        return {"ok": True, "message": f"{channel.title()} отключён, его токен удалён."}

    # ------------------------------------------------------------ invoking
    async def invoke(self, cap_id: str, request: str, *, approve=None, progress=None) -> dict:
        """Run an upstream skill (OpenClaw or Hermes). One executor per capability: duplicates resolve to the one kept,
        and an identical request already running is joined, not started again."""
        if self.backends.lockdown:
            return {"ok": False, "message": "Агенты остановлены вами — сначала «возобнови агентов»."}
        cap = self.registry.get(cap_id) or self.registry.find(cap_id)
        if cap is None:
            return {"ok": False, "message": f"Возможности {cap_id} нет."}
        if cap.duplicate_of:
            cap = self.registry.get(cap.duplicate_of) or cap
        if not cap.usable:
            return {"ok": False, "message": f"{cap.title} сейчас недоступен ({cap.state}, {cap.health})."}
        k = hashlib.sha256(f"{cap.id}|{request.strip().lower()}".encode()).hexdigest()
        if k in self._inflight:
            result = await asyncio.shield(self._inflight[k])
            return {**result, "joined": True}
        fut = asyncio.get_running_loop().create_future()
        self._inflight[k] = fut
        t0 = time.perf_counter()
        try:
            result = await self._run(cap, request, approve=approve, progress=progress)
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except Exception as exc:   # noqa: BLE001
            result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"[:400]}
        finally:
            self._inflight.pop(k, None)
        ms = (time.perf_counter() - t0) * 1000
        self.registry.record(cap.id, bool(result.get("ok")), ms, "" if result.get("ok") else str(result.get("message", "")))
        result.update(capability=cap.id, executor=result.get("executor", cap.provider), ms=round(ms))
        if not fut.done():
            fut.set_result(result)
        return result

    async def _run(self, cap: Capability, request: str, *, approve=None, progress=None) -> dict:
        executor = cap.requires.get("executor", "hermes") if cap.provider == "shared" else cap.provider
        if executor == "openclaw" and cap.kind == "skill":
            oc = await self.need_oc()
            if oc is None:
                return {"ok": False, "message": "OpenClaw не запустился."}
            r = await oc.run_agent(f"Use the {cap.name} skill for this request: {request}", timeout=300, title=cap.title)
            return {"ok": r["status"] == "ok", "output": r["text"], "executor": "openclaw", "run_id": r["run_id"],
                    "message": r["text"][:2000] or r.get("error", "")}
        if executor == "hermes" and cap.kind == "skill":
            h = self.hermes
            if h is None:
                return {"ok": False, "message": "Hermes не запущен."}
            r = await h.delegate(request, title=cap.title, approve=approve, progress=progress,
                                 instructions=f"Load and follow your skill '{cap.name}' (skill_view) for this task. "
                                              "Report the result plainly, in the language of the request.",
                                 timeout=self.cfg.hermes_timeout_s or None)
            return {"ok": r["ok"], "output": r["output"], "executor": "hermes", "run_id": r["run_id"],
                    "tools": r["tools"], "message": r["output"][:2000] or r["error"]}
        return {"ok": False, "message": f"{cap.title} — это собственная возможность JARVIS ({cap.name}); "
                                        "её вызывают напрямую, не через агентов."}

    # ------------------------------------------------------------ research for the agents (MCP)
    async def research_call(self, name: str, args: dict) -> dict:
        """web_search asked by OpenClaw or Hermes through the MCP bridge. Runs through the Gateway like any tool: under
        the task that handed them the work, or under a small task of its own («OpenClaw: поиск …»)."""
        if name != "web_search":
            return {"status": "invalid", "error": "Агентам доступен только web_search"}
        if self.backends.lockdown:
            return {"status": "denied", "error": "Агенты остановлены"}
        active = [t for t in self.delegations if self.tasks.get(t)]
        if active:
            tid, own = active[-1], False
        else:
            from ..tasks import S
            tid, own = self.tasks.create(f"Агент: поиск «{str(args.get('query', ''))[:80]}»", None, route="agent_research"), True
            self.tasks.transition(tid, S.RUNNING)
        out = await self.gateway.execute(tid, "web_search", args, "Поиск по просьбе агента (OpenClaw / Hermes)")
        if own:
            from ..tasks import S
            self.tasks.transition(tid, S.COMPLETED if out.status == "ok" else S.FAILED,
                                  result={"summary": out.result.summary if out.result else out.error})
        return out.for_model()

    # ------------------------------------------------------------ the panel (ws «hub.*»)
    async def panel(self) -> dict:
        """Everything «Возможности» shows when it opens: caches plus two quick gateway reads."""
        b = self.backends
        fresh = [n for n in ("openclaw", "hermes") if b.enabled(n) and n not in b.health and not b.on_demand(n)]
        if fresh:                                   # opened right after start: check now, don't show «unknown»
            await asyncio.gather(*(b.refresh(n) for n in fresh), return_exceptions=True)
        automations, runs = [], []
        oc = self.oc
        if oc is not None:
            try:
                automations = await asyncio.wait_for(oc.automations(), 3)
                if any(a.get("enabled") and not a.get("system") for a in automations) and not b.keep_openclaw:
                    b.set_keep(True)         # the person's own automation runs on its schedule only while OpenClaw runs
            except Exception as exc:   # noqa: BLE001
                automations = [{"error": str(exc)[:120]}]
            runs += [{"agent": "openclaw", "id": k, "title": v.get("title", ""), "started": v["started"]}
                     for k, v in oc.runs.items()]
        if b.hermes is not None:
            runs += [{"agent": "hermes", "id": k, **{x: v[x] for x in ("title", "started", "last_event", "tools") if x in v}}
                     for k, v in b.hermes.runs.items()]
        return {"backends": b.snapshot(), **self.snapshot(), "automations": automations, "runs": runs,
                "updates": self.lifecycle.available() if self.lifecycle else [],
                "update_history": self.lifecycle.history(30) if self.lifecycle else [],
                "discover": self.clawhub.items(view="new", limit=30) if self.clawhub else [],
                "recent": self.clawhub.items(view="recent", limit=30) if self.clawhub else [],
                "events": self.clawhub.events(40) if self.clawhub else [],
                "clawhub": {"last_sync": self.clawhub.last_sync if self.clawhub else 0,
                            "error": self.clawhub.last_error if self.clawhub else ""},
                "llm": self.llm.stats if self.llm else {},
                "models": {"openclaw": self.cfg.openclaw_model, "hermes": self.cfg.hermes_model},
                "channels_info": {k: {"label": v["label"], "needs": v["needs"], "flow": v["flow"],
                                      "command": v.get("command", ""), "owner": self.channel_owner(k)}
                                  for k, v in CHANNELS.items()},
                "installed": {n: b.layout.installed(n) for n in ("openclaw", "hermes")},
                "install_command": "scripts/install-agents.sh"}

    async def op(self, data: dict) -> dict:
        """Panel requests that read or manage the agents themselves. Anything that installs, updates or runs a skill
        goes through the tools (hub/tools.py), as a task with the usual approval card."""
        action = str(data.get("action") or "snapshot")
        b = self.backends
        if action == "snapshot":
            return await self.panel()
        if action in ("find", "search"):
            q = str(data.get("query") or "")[:300]
            return self.find(q) if action == "find" else await self.search(q)
        if action == "discover":
            return {"items": self.clawhub.items(view=str(data.get("view") or "new")) if self.clawhub else [],
                    "last_sync": self.clawhub.last_sync if self.clawhub else 0}
        if action == "sync":
            return await self.clawhub.sync()
        if action == "check_updates":
            if await self.need_oc() is None:
                return {"ok": False, "message": "OpenClaw не запустился."}
            return {"ok": True, "updates": await self.lifecycle.check()}
        if action == "review":
            # What the install card will show (kept for skill_install, which refuses a release that changed since).
            if self.lifecycle is None or await self.need_oc() is None:
                return {"ok": False, "message": "OpenClaw не запустился."}
            from .tools import REVIEWS
            ref = str(data.get("ref") or "")[:200]
            r = await self.lifecycle.review(ref, str(data.get("version") or "")[:40])
            REVIEWS[r["ref"]] = REVIEWS[ref] = r
            return {"ok": True, "review": r}
        if action == "release":
            return self.lifecycle.release(str(data.get("target") or ""), str(data.get("version") or ""))
        if action == "connect_plan":
            return await self.connect_plan(str(data.get("service") or ""))
        if action == "connect_token":
            # Typed into the connection card in the window: it never reaches a model or a log.
            service, token = str(data.get("service") or ""), str(data.get("token") or "").strip()
            if service != "telegram":
                return {"ok": False, "message": f"{service} подключается в терминале (см. карточку)."}
            if not token or len(token) > 200 or any(ch.isspace() for ch in token) or ":" not in token:
                return {"ok": False, "message": "Это не похоже на токен бота (вида 123456:ABC…)."}
            return await self.connect_telegram(token)
        if action in ("automation_pause", "automation_resume", "automation_run"):
            oc = await self.need_oc()
            if oc is None:
                return {"ok": False, "message": "OpenClaw не запустился."}
            job = str(data.get("target") or "")
            known = {a["id"]: a for a in await oc.automations()}
            if job not in known:
                return {"ok": False, "message": "Такой автоматизации нет."}
            if known[job]["system"] and action != "automation_run":
                return {"ok": False, "message": f"«{known[job]['name']}» — служебная задача OpenClaw, её не приостановить."}
            if action == "automation_run":
                await oc.run_automation(job)
            else:
                await oc.set_automation(job, action == "automation_resume")
            self.audit.write("hub." + action, job=job)
            return {"ok": True, "automations": await oc.automations()}
        if action == "restart":
            name = str(data.get("name") or "")
            if name not in ("openclaw", "hermes"):
                raise ValueError("name: openclaw или hermes")
            self.audit.write("hub.restart", agent=name)
            return await b.restart(name)
        if action == "wake":
            name = str(data.get("name") or "openclaw")
            ok = await b.wake(name) if name in ("openclaw", "hermes") else False
            label = {"openclaw": "OpenClaw", "hermes": "Hermes"}.get(name, name)
            return {"ok": ok, "message": f"{label} запущен." if ok else f"{label} не запустился — см. журнал агента."}
        if action == "keep":
            b.set_keep(bool(data.get("on")))
            self.audit.write("hub.keep_openclaw", on=bool(data.get("on")))
            if b.keep_openclaw:
                await b.wake("openclaw")
            return {"ok": True, "backends": b.snapshot()}
        if action == "stop_all":
            return {"ok": True, **await b.stop_all()}
        if action == "resume":
            b.lift()
            return {"ok": True}
        if action == "refresh":
            report = await self.registry.refresh()
            self.bus.publish("hub.catalog", {"summary": self.registry.summary()})
            return {"ok": True, "report": report}
        raise ValueError(f"Неизвестное действие: {action}")
