"""OpenClaw's gateway as one persistent connection from the JARVIS core (ported from morph-rq/jarvis-os).

The gateway (OpenClaw 2026.10.6) speaks JSON frames over a websocket on 127.0.0.1:18789 (protocol 4): the server opens
with a `connect.challenge` event, the client answers with a `connect` request, and from then on requests are
{type:"req", id, method, params}, answers {type:"res", id, ok, payload|error} and pushes {type:"event", event, payload}.
JARVIS connects as OpenClaw's `gateway-client` in `backend` mode with the gateway token on loopback — the documented way
for a local backend to get operator scopes without pairing a device (docs/gateway/protocol/auth.md).

One connection serves every request (a CLI process per call costs seconds of Node start-up). The client reconnects with
backoff when the socket drops or ticks stop arriving, fails pending requests with a retryable error, and never retries
an authentication failure (that needs the person). Everything that comes back is untrusted data.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import time
import uuid
from typing import Any, Awaitable, Callable

import aiohttp

PROTOCOL = 4
DEFAULT_URL = "ws://127.0.0.1:18789"
SCOPES = ["operator.read", "operator.write", "operator.admin", "operator.approvals"]
BACKOFF = (1, 2, 5, 15, 30)
MAX_FRAME = 25 * 1024 * 1024


class GatewayError(Exception):
    def __init__(self, message: str, code: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.code, self.retryable = code, retryable


class AuthError(GatewayError):
    pass


class GatewayClient:
    """A websocket RPC client for OpenClaw's gateway. One instance per core."""

    def __init__(self, url: str = DEFAULT_URL, token: Callable[[], str | None] | str | None = None, *,
                 connect_timeout: float = 15, request_timeout: float = 30, version: str = "0.1.0") -> None:
        self.url = url
        self._token = token
        self.connect_timeout, self.request_timeout, self.version = connect_timeout, request_timeout, version
        self.state = "disconnected"        # disconnected, connecting, connected, auth_failed, stopped
        self.error = ""
        self.server: dict = {}
        self.methods: set[str] = set()
        self.events_supported: set[str] = set()
        self.scopes: list[str] = []
        self.connected_at = 0.0
        self.last_frame = 0.0
        self.reconnects = 0
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}
        self._handlers: dict[str, list[Callable[[dict], Any]]] = {}
        self._ready = asyncio.Event()
        self._runner: asyncio.Task | None = None
        self._challenge: asyncio.Future | None = None
        self._tick_s = 30.0
        self._closing = False

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Connect in the background and keep the connection up until `close`."""
        if self._runner is None or self._runner.done():
            self._closing = False
            self._runner = asyncio.get_running_loop().create_task(self._run(), name="openclaw-gateway")

    async def close(self) -> None:
        self._closing = True
        self.state = "stopped"
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:   # noqa: BLE001
                pass
        if self._runner is not None:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
            self._runner = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._fail_pending(GatewayError("соединение закрыто", retryable=True))

    async def wait_ready(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def connected(self) -> bool:
        return self.state == "connected" and self._ready.is_set()

    def on(self, event: str, handler: Callable[[dict], Any]) -> None:
        """Call `handler(frame)` for every gateway event named `event` ("*" for all)."""
        self._handlers.setdefault(event, []).append(handler)

    def off(self, event: str, handler: Callable[[dict], Any]) -> None:
        try:
            self._handlers.get(event, []).remove(handler)
        except ValueError:
            pass

    async def _run(self) -> None:
        attempt = 0
        while not self._closing:
            started = time.monotonic()
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except AuthError as exc:
                self.state, self.error = "auth_failed", str(exc)
                self._emit_local("jarvis.state", {"state": self.state, "error": self.error})
                return   # the person has to fix the token; reconnecting would only repeat it
            except Exception as exc:   # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"[:300]
            finally:
                self._ready.clear()
                self._ws = None
                self._fail_pending(GatewayError("соединение со шлюзом OpenClaw оборвалось", retryable=True))
            if self._closing:
                return
            if time.monotonic() - started > 120:
                attempt = 0
            self.state = "disconnected"
            self._emit_local("jarvis.state", {"state": self.state, "error": self.error})
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            attempt += 1
            self.reconnects += 1
            await asyncio.sleep(delay)

    async def _connect_once(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=False)      # loopback: never through a proxy
        self.state = "connecting"
        async with self._session.ws_connect(self.url, max_msg_size=MAX_FRAME, heartbeat=20,
                                            timeout=aiohttp.ClientWSTimeout(ws_close=5),
                                            autoping=True) as ws:
            self._ws = ws
            reader = asyncio.get_running_loop().create_task(self._read(ws), name="openclaw-read")
            try:
                hello = await asyncio.wait_for(self._handshake(), self.connect_timeout)
                self._accept(hello)
                watchdog = asyncio.get_running_loop().create_task(self._watchdog(ws), name="openclaw-watchdog")
                try:
                    await reader
                finally:
                    watchdog.cancel()
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

    async def _handshake(self) -> dict:
        challenge = asyncio.get_running_loop().create_future()
        self._challenge = challenge
        await challenge   # the server speaks first
        token = self._token() if callable(self._token) else self._token
        params = {"minProtocol": PROTOCOL, "maxProtocol": PROTOCOL,
                  "client": {"id": "gateway-client", "displayName": "JARVIS", "version": self.version,
                             "platform": "linux", "mode": "backend"},
                  "caps": [], "role": "operator", "scopes": SCOPES}
        if token:
            params["auth"] = {"token": token}
        try:
            return await self._call("connect", params, self.connect_timeout)
        except GatewayError as exc:
            text = f"{exc.code} {exc}".lower()
            if any(w in text for w in ("auth", "token", "unauthor", "forbidden", "password")):
                raise AuthError(str(exc), exc.code) from exc
            raise

    def _accept(self, hello: dict) -> None:
        hello = hello or {}
        features = hello.get("features") or {}
        self.server = dict(hello.get("server") or {})
        self.methods = set(features.get("methods") or [])
        self.events_supported = set(features.get("events") or [])
        self.scopes = list((hello.get("auth") or {}).get("scopes") or [])
        tick = (hello.get("policy") or {}).get("tickIntervalMs")
        if isinstance(tick, (int, float)) and tick > 0:
            self._tick_s = tick / 1000
        self.state, self.error = "connected", ""
        self.connected_at = time.time()
        self._ready.set()
        self._emit_local("jarvis.state", {"state": self.state, "server": self.server})

    async def _watchdog(self, ws) -> None:
        """No frame (event, tick, answer) for three tick intervals: the gateway is wedged; reconnect."""
        while True:
            await asyncio.sleep(max(5.0, self._tick_s))
            if time.monotonic() - self.last_frame > max(60.0, self._tick_s * 3):
                self.error = "шлюз OpenClaw молчит"
                await ws.close()
                return

    async def _read(self, ws) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
                continue
            self.last_frame = time.monotonic()
            try:
                frame = json.loads(msg.data)
            except ValueError:
                continue
            kind = frame.get("type")
            if kind == "res":
                fut = self._pending.pop(str(frame.get("id")), None)
                if fut is not None and not fut.done():
                    if frame.get("ok"):
                        fut.set_result(frame.get("payload"))
                    else:
                        err = frame.get("error") or {}
                        fut.set_exception(GatewayError(str(err.get("message") or "запрос не выполнен"),
                                                       str(err.get("code") or ""), bool(err.get("retryable"))))
            elif kind == "event":
                if frame.get("event") == "connect.challenge":
                    ch = self._challenge
                    if ch is not None and not ch.done():
                        ch.set_result(frame.get("payload"))
                    continue
                if frame.get("event") == "shutdown":
                    self.error = "шлюз OpenClaw останавливается"
                self._dispatch(frame)
            elif kind == "hello-ok":   # some builds answer connect with a bare hello frame
                fut = self._pending.pop(next(iter(self._pending), ""), None)
                if fut is not None and not fut.done():
                    fut.set_result(frame)

    def _dispatch(self, frame: dict) -> None:
        for handler in list(self._handlers.get(frame.get("event", ""), [])) + list(self._handlers.get("*", [])):
            try:
                out = handler(frame)
                if asyncio.iscoroutine(out):
                    asyncio.get_running_loop().create_task(out)
            except Exception:   # noqa: BLE001 — one bad handler never breaks the connection
                pass

    def _emit_local(self, event: str, payload: dict) -> None:
        self._dispatch({"type": "event", "event": event, "payload": payload})

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)

    # ------------------------------------------------------------ requests
    async def _call(self, method: str, params: dict | None, timeout: float) -> Any:
        ws = self._ws
        if ws is None:
            raise GatewayError("нет соединения с OpenClaw", retryable=True)
        rid = f"j{next(self._ids)}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await ws.send_str(json.dumps({"type": "req", "id": rid, "method": method, "params": params or {}}))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as exc:
            raise GatewayError(f"OpenClaw не ответил на {method} за {timeout:.0f} с", "timeout", True) from exc
        finally:
            self._pending.pop(rid, None)

    async def request(self, method: str, params: dict | None = None, *, timeout: float | None = None,
                      wait: float = 10) -> Any:
        """One gateway method. Waits up to `wait` s for a connection that is coming up."""
        if not self._ready.is_set():
            if self.state == "auth_failed":
                raise AuthError(self.error or "OpenClaw не принял токен JARVIS")
            if not await self.wait_ready(wait):
                raise GatewayError(f"OpenClaw недоступен ({self.error or self.state})", "unavailable", True)
        if self.methods and method not in self.methods:
            raise GatewayError(f"у этой версии OpenClaw нет метода {method}", "unknown_method")
        return await self._call(method, params, timeout or self.request_timeout)


# ---------------------------------------------------------------- adapter

def _schedule(sched: dict) -> str:
    """A schedule in Russian words where it's a common shape; the raw expression otherwise."""
    every = sched.get("everyMs")
    if every:
        mins = int(every / 60000)
        return (f"раз в {mins // 1440} дн." if mins % 1440 == 0 and mins > 1440 else "каждый день" if mins == 1440
                else f"раз в {mins // 60} ч" if mins % 60 == 0 and mins >= 120 else f"раз в {mins} мин")
    expr = str(sched.get("expr") or "")
    parts = expr.split()
    if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit() and parts[2:4] == ["*", "*"]:
        at = f"{int(parts[1]):02d}:{int(parts[0]):02d}"
        days = {"*": "каждый день", "1-5": "по будням", "0,6": "по выходным", "1": "по понедельникам",
                "2": "по вторникам", "3": "по средам", "4": "по четвергам", "5": "по пятницам", "6": "по субботам",
                "0": "по воскресеньям", "7": "по воскресеньям"}.get(parts[4])
        if days:
            return f"{days} в {at}"
    if sched.get("kind") == "at":
        return f"один раз, {sched.get('at', '')}"
    return expr or str(sched.get("kind", ""))


def _channel_state(acc: dict) -> str:
    """One word for an account: connected, connecting, disconnected, error, not_configured, disabled."""
    if acc.get("enabled") is False:
        return "disabled"
    if not acc.get("configured", True):
        return "not_configured"
    if acc.get("lastError") and not acc.get("connected"):
        return "error"
    if acc.get("connected") or (acc.get("running") and acc.get("linked", True)):
        return "connected"
    if acc.get("running"):
        return "connecting"
    return "disconnected"


class OpenClaw:
    """What JARVIS uses OpenClaw for, over one GatewayClient."""

    name = "openclaw"

    def __init__(self, client: GatewayClient) -> None:
        self.c = client
        self.runs: dict[str, dict] = {}     # agent runs JARVIS started: runId -> {session, started, title}

    async def status(self) -> dict:
        c = self.c
        out = {"name": "openclaw", "label": "OpenClaw", "state": c.state, "error": c.error,
               "version": c.server.get("version", ""), "build": c.server.get("buildId", ""),
               "methods": len(c.methods), "reconnects": c.reconnects,
               "connected_since": c.connected_at if c.connected else 0, "active_runs": len(self.runs)}
        if c.connected:
            try:
                h = await c.request("health", timeout=10) or {}
                out.update(healthy=bool(h.get("ok", True)),
                           plugins_loaded=len((h.get("plugins") or {}).get("loaded") or []),
                           plugin_errors=(h.get("plugins") or {}).get("errors") or [])
            except GatewayError as exc:
                out.update(healthy=False, error=str(exc))
        else:
            out["healthy"] = False
        return out

    async def channels(self, probe: bool = False) -> list[dict]:
        p = await self.c.request("channels.status", {"probe": probe}, timeout=30 if probe else 10) or {}
        labels = p.get("channelLabels") or {}
        out = []
        order = p.get("channelOrder") or sorted((p.get("channelAccounts") or {}).keys())
        for ch in order:
            accounts = (p.get("channelAccounts") or {}).get(ch) or []
            if isinstance(accounts, dict):
                accounts = list(accounts.values())
            if not accounts:
                out.append({"channel": ch, "label": labels.get(ch, ch), "account": "", "state": "not_configured"})
            for acc in accounts:
                out.append({"channel": ch, "label": labels.get(ch, ch), "account": str(acc.get("accountId", "")),
                            "state": _channel_state(acc), "error": str(acc.get("lastError") or "")[:200],
                            "dm_policy": acc.get("dmPolicy", ""), "last_inbound": acc.get("lastInboundAt")})
        return out

    async def skills(self) -> list[dict]:
        p = await self.c.request("skills.status", {}, timeout=20) or {}
        out = []
        for s in p.get("skills") or []:
            missing = s.get("missing") or {}
            needs = [f"{k}: {', '.join(v)}" for k, v in missing.items() if v]
            out.append({"name": s.get("name", ""), "key": s.get("skillKey") or s.get("name", ""),
                        "description": s.get("description", ""), "source": s.get("source", ""),
                        "bundled": bool(s.get("bundled")), "eligible": bool(s.get("eligible")),
                        "disabled": bool(s.get("disabled")), "path": s.get("filePath", ""),
                        "homepage": s.get("homepage", ""), "missing": needs,
                        "requirements": s.get("requirements") or {}, "version": s.get("version", ""),
                        "emoji": s.get("emoji", ""),
                        # Installed from ClawHub (OpenClaw's origin record and lock agree), so JARVIS may manage it.
                        "clawhub": (s.get("clawhub") or {}).get("status") == "linked",
                        "installed_version": (s.get("clawhub") or {}).get("installedVersion", "")})
        return out

    async def plugins(self) -> list[dict]:
        p = await self.c.request("plugins.list", {}, timeout=20) or {}
        return [{"id": x.get("id", ""), "name": x.get("name") or x.get("id", ""), "description": x.get("description", ""),
                 "version": x.get("version", ""), "installed": bool(x.get("installed")), "enabled": bool(x.get("enabled")),
                 "state": x.get("state", ""), "origin": x.get("origin", ""), "package": x.get("packageName", ""),
                 "category": x.get("category", ""), "categories": x.get("categories") or [],
                 "runtime": (x.get("runtime") or {}).get("state", ""), "removable": bool(x.get("removable"))}
                for x in p.get("plugins") or []]

    async def automations(self) -> list[dict]:
        # Paused jobs too: otherwise a paused automation disappears and can never be resumed.
        p = await self.c.request("cron.list", {"includeDisabled": True, "limit": 200}, timeout=15) or {}
        out = []
        for j in p.get("jobs") or []:
            sched = j.get("schedule") or {}
            st = j.get("state") or {}
            out.append({"id": j.get("id", ""), "name": j.get("displayName") or j.get("name", ""),
                        "enabled": bool(j.get("enabled")), "schedule": _schedule(sched),
                        "next_run": (st.get("nextRunAtMs") or 0) / 1000, "last_run": (st.get("lastRunAtMs") or 0) / 1000,
                        "last_status": st.get("lastStatus", ""), "agent": j.get("agentId", ""),
                        # OpenClaw's own jobs (heartbeat, memory upkeep) carry a declaration key and can't be edited.
                        "system": bool(j.get("declarationKey"))})
        return out

    async def set_automation(self, job_id: str, enabled: bool) -> dict:
        return await self.c.request("cron.update", {"id": job_id, "patch": {"enabled": enabled}}, timeout=15)

    async def run_automation(self, job_id: str) -> dict:
        return await self.c.request("cron.run", {"id": job_id, "mode": "force"}, timeout=30)

    async def run_agent(self, message: str, *, session: str = "agent:main:jarvis", timeout: float = 300,
                        title: str = "", on_event: Callable[[dict], Awaitable[None] | None] | None = None) -> dict:
        """One agent turn in OpenClaw (its skills and plugins), waited for. Returns {status, text, run_id, error}."""
        accepted = await self.c.request("agent", {"message": message, "sessionKey": session,
                                                  "idempotencyKey": uuid.uuid4().hex}, timeout=30) or {}
        run_id = str(accepted.get("runId") or "")
        self.runs[run_id] = {"session": session, "started": time.time(), "title": title or message[:80]}
        texts: list[str] = []

        def collect(frame: dict) -> None:
            p = frame.get("payload") or {}
            if str(p.get("runId", "")) != run_id:
                return
            data = p.get("data") or {}
            if p.get("stream") == "assistant" and isinstance(data.get("text"), str):
                texts.append(data["text"])
            if on_event:
                out = on_event(p)
                if asyncio.iscoroutine(out):
                    asyncio.get_running_loop().create_task(out)
        self.c.on("agent", collect)
        try:
            done = await self.c.request("agent.wait", {"runId": run_id, "timeoutMs": int(timeout * 1000)},
                                        timeout=timeout + 15) or {}
        finally:
            self.c.off("agent", collect)
            self.runs.pop(run_id, None)
        text = texts[-1] if texts else ""
        if not text:
            try:
                hist = await self.c.request("chat.history", {"sessionKey": session, "limit": 4}, timeout=15) or {}
                for m in reversed(hist.get("messages") or []):
                    if m.get("role") == "assistant":
                        content = m.get("content")
                        text = content if isinstance(content, str) else " ".join(
                            str(p.get("text", "")) for p in content or [] if isinstance(p, dict))
                        break
            except GatewayError:
                pass
        return {"status": done.get("status", "ok"), "text": text, "run_id": run_id, "error": done.get("error", "")}

    async def abort_all(self) -> int:
        n = 0
        for run_id, run in list(self.runs.items()):
            try:
                await self.c.request("chat.abort", {"sessionKey": run["session"], "runId": run_id}, timeout=10)
                n += 1
            except GatewayError:
                pass
        return n

    # -- skills and plugins lifecycle (all through OpenClaw's own install path) --
    async def skill_search(self, query: str, limit: int = 12) -> list[dict]:
        p = await self.c.request("skills.search", {"query": query, "limit": limit}, timeout=30) or {}
        return list(p.get("results") or p.get("skills") or [])

    async def skill_detail(self, slug: str, version: str = "") -> dict:
        params = {"slug": slug}
        if version:
            params["version"] = version
        return await self.c.request("skills.detail", params, timeout=30) or {}

    async def skill_install(self, slug: str, version: str = "") -> dict:
        params: dict = {"source": "clawhub", "slug": slug}
        if version:
            params["version"] = version
        return await self.c.request("skills.install", params, timeout=180) or {}

    async def skill_update(self, slug: str, force: bool = False) -> dict:
        params: dict = {"source": "clawhub", "slug": slug}
        if force:
            params["force"] = True
        return await self.c.request("skills.update", params, timeout=180) or {}

    async def skill_enable(self, key: str, enabled: bool) -> dict:
        return await self.c.request("skills.update", {"skillKey": key, "enabled": enabled}, timeout=30)

    async def plugin_enable(self, plugin_id: str, enabled: bool) -> dict:
        return await self.c.request("plugins.setEnabled", {"pluginId": plugin_id, "enabled": enabled}, timeout=60)

    async def config_patch(self, patch: dict) -> None:
        current = await self.c.request("config.get", {}, timeout=15) or {}
        base = current.get("hash") or current.get("baseHash") or ""
        params = {"raw": json.dumps(patch)}
        if base:
            params["baseHash"] = base
        await self.c.request("config.patch", params, timeout=30)
