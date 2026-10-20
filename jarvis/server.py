"""Local server: static UI, WebSocket for the UI, small bearer-token API for
the CLI and the Waybar module. Binds to loopback only."""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import mimetypes
import os
import secrets as pysecrets
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from . import secrets
from .app import Core, build
from .events import redact

WEB = Path(__file__).resolve().parent.parent / "web"
COOKIE = "jarvis_session"
UI_TOOLS = {"open_path"}
CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob: data:; "
       "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
ALLOWED_PRIVACY = ("AUTHORIZED_CLOUD", "PRIVATE_LOCAL", "STRICT_OFFLINE")


def load_token(core: Core) -> str:
    f = core.paths.token_file
    if f.exists():
        t = f.read_text().strip()
        if len(t) >= 32:
            return t
    t = pysecrets.token_urlsafe(32)
    f.write_text(t)
    f.chmod(0o600)
    return t


class Server:
    def __init__(self, core: Core):
        self.core = core
        self.token = load_token(core)
        self.port = core.config.port
        self.app = web.Application(middlewares=[self.guard], client_max_size=12 * 1024 * 1024)
        r = self.app.router
        r.add_get("/", self.index)
        r.add_get("/ws", self.ws)
        r.add_get("/health", self.health)
        r.add_get("/api/state", self.api_state)
        r.add_get("/api/events", self.api_events)
        r.add_post("/api/control", self.api_control)
        r.add_post("/api/submit", self.api_submit)
        r.add_post("/api/tts", self.api_tts)
        r.add_get("/artifact", self.artifact)
        r.add_get("/static/{path:.+}", self.static)
        self.app.on_startup.append(self.on_startup)
        self.app.on_cleanup.append(self.on_cleanup)
        self.sockets: set[web.WebSocketResponse] = set()

    # ------------------------------------------------------------------ security
    def _hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def _cookie_ok(self, req: web.Request) -> bool:
        return hmac.compare_digest(req.cookies.get(COOKIE, ""), self.token)

    def _bearer_ok(self, req: web.Request) -> bool:
        h = req.headers.get("Authorization", "")
        return h.startswith("Bearer ") and hmac.compare_digest(h[7:], self.token)

    def _origin_ok(self, req: web.Request) -> bool:
        o = req.headers.get("Origin")
        return o is None or o in {f"http://{h}" for h in self._hosts()}

    @web.middleware
    async def guard(self, req: web.Request, handler):
        # DNS-rebinding protection: only our own loopback host names.
        if req.host not in self._hosts():
            return web.Response(status=403, text="forbidden host")
        if req.path == "/health":
            return await handler(req)
        if req.path == "/" and req.query.get("t"):
            if hmac.compare_digest(req.query["t"], self.token):
                resp = web.Response(status=302, headers={"Location": "/", "Cache-Control": "no-store"})
                resp.set_cookie(COOKIE, self.token, httponly=True, samesite="Strict", path="/", max_age=86400 * 30)
                return resp
            return web.Response(status=403, text="bad token")
        if req.path.startswith("/api/"):
            if not (self._bearer_ok(req) or (self._cookie_ok(req) and self._origin_ok(req))):
                return web.json_response({"error": "unauthorized"}, status=401)
            if req.method == "POST" and not self._bearer_ok(req) and req.headers.get("X-Jarvis") != "1":
                return web.json_response({"error": "csrf"}, status=403)
            return await handler(req)
        if not self._cookie_ok(req):
            return web.Response(status=401, content_type="text/html", text=UNAUTH_PAGE)
        if req.path == "/ws" and not self._origin_ok(req):
            return web.Response(status=403, text="bad origin")
        return await handler(req)

    # ------------------------------------------------------------------ lifecycle
    async def on_startup(self, app):
        self.core.scheduler.start()
        self._fwd = asyncio.create_task(self._forward())
        if self.core.gemini.configured():
            asyncio.create_task(self._check_provider())

    async def on_cleanup(self, app):
        self._fwd.cancel()
        await self.core.scheduler.stop()
        for ws in list(self.sockets):
            await ws.close()
        self.core.audit.write("core.stop")

    async def _check_provider(self):
        await self.core.gemini.check()
        self.core.bus.publish("provider", {"provider": self.core.gemini.describe()})
        self.core.director._touch()

    async def _forward(self):
        q = self.core.bus.subscribe()
        while True:
            ev = await q.get()
            dead = []
            payload = json.dumps({"type": "event", **ev}, ensure_ascii=False, default=str)
            for ws in list(self.sockets):
                try:
                    await ws.send_str(payload)
                except (ConnectionResetError, RuntimeError):
                    dead.append(ws)
            for ws in dead:
                self.sockets.discard(ws)

    # ------------------------------------------------------------------ handlers
    async def health(self, req):
        return web.json_response({"ok": True, "app": "jarvis-workos"})

    async def index(self, req):
        return web.FileResponse(WEB / "index.html", headers={
            "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": CSP})

    async def static(self, req):
        p = (WEB / req.match_info["path"]).resolve()
        if not p.is_relative_to(WEB) or not p.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Cache-Control": "no-cache"})

    async def artifact(self, req):
        cfg = self.core.config
        roots = [cfg.expand(cfg.screenshot_dir), cfg.expand(cfg.write_dir), self.core.paths.artifacts]
        if self.core.director.flora:
            from .flora.mail import FIXTURES
            roots.append(FIXTURES.resolve())
        try:
            p = Path(req.query.get("path", "")).resolve()
        except (OSError, ValueError):
            raise web.HTTPNotFound()
        if not any(p.is_relative_to(r) for r in roots) or not p.is_file():
            raise web.HTTPNotFound()
        ctype = mimetypes.guess_type(p.name)[0] or ""
        if not (ctype.startswith("image/") or ctype in ("application/pdf", "text/markdown", "text/plain")):
            raise web.HTTPForbidden()
        # Files are untrusted content: sandbox them so e.g. an SVG opened directly can never run script.
        return web.FileResponse(p, headers={"Cache-Control": "private, max-age=60", "Content-Type": ctype,
                                            "X-Content-Type-Options": "nosniff",
                                            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'"})

    async def api_state(self, req):
        return web.json_response(self.core.director.core_state())

    async def api_events(self, req):
        """Server-sent events of core state, for the Waybar module."""
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(req)
        q = self.core.bus.subscribe()
        try:
            await resp.write(f"data: {json.dumps(self.core.director.core_state())}\n\n".encode())
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 25)
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
                    continue
                if ev["kind"] in ("core.state", "approval.created", "approval.updated", "reminder.fired"):
                    await resp.write(f"data: {json.dumps(self.core.director.core_state())}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.core.bus.unsubscribe(q)
        return resp

    async def api_control(self, req):
        body = await req.json()
        op = body.get("op")
        if op not in ("estop", "estop_clear", "pause_all", "resume_all", "stop_speech"):
            return web.json_response({"error": "unknown op"}, status=400)
        msg = await self.core.director.control(op)
        return web.json_response({"ok": True, "message": msg})

    async def api_submit(self, req):
        body = await req.json()
        tid = await self.core.director.submit(str(body.get("text", "")), source=body.get("source", "api"))
        return web.json_response({"ok": True, "task_id": tid})

    async def api_tts(self, req):
        body = await req.json()
        text = str(body.get("text", ""))[:1500]
        if self.core.director.privacy_profile() != "AUTHORIZED_CLOUD":
            return web.json_response({"error": "Озвучивание через облако выключено режимом приватности"}, status=403)
        try:
            wav = await self.core.gemini.speak(text)
        except Exception as e:   # noqa: BLE001
            return web.json_response({"error": str(e)}, status=502)
        return web.Response(body=wav, content_type="audio/wav")

    # ------------------------------------------------------------------ websocket
    async def ws(self, req):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=12 * 1024 * 1024)
        await ws.prepare(req)
        self.sockets.add(ws)
        await ws.send_str(json.dumps({"type": "snapshot", "data": self.core.director.snapshot()},
                                     ensure_ascii=False, default=str))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                asyncio.create_task(self._handle(ws, data))
        finally:
            self.sockets.discard(ws)
        return ws

    async def _handle(self, ws, data: dict):
        rid = data.get("req_id")
        d = self.core.director
        typ = data.get("type")
        self.core.audit.write("ui." + str(typ)[:40], **{k: v for k, v in redact(data).items()
                                                        if k in ("type", "id", "task_id", "profile", "op")})
        try:
            if typ == "submit":
                res = {"task_id": await d.submit(str(data.get("text", "")), data.get("source", "text"))}
            elif typ == "approve":
                edited = data.get("edited")
                res = {"approval": d.decide(data["id"], True, edited if isinstance(edited, dict) else None,
                                            bool(data.get("remember")))}
            elif typ == "deny":
                res = {"approval": d.decide(data["id"], False)}
            elif typ in ("cancel", "pause", "resume"):
                res = {"message": await d.control(typ, data.get("task_id"))}
            elif typ in ("estop", "estop_clear", "stop_speech", "new_conversation", "pause_all", "resume_all"):
                res = {"message": await d.control(typ)}
            elif typ == "snapshot":
                res = d.snapshot()
            elif typ == "privacy.set":
                if data.get("profile") not in ALLOWED_PRIVACY:
                    raise ValueError("Неизвестный режим")
                self.core.db.set_kv("privacy_profile", data["profile"])
                self.core.audit.write("privacy.changed", profile=data["profile"])
                d._touch()
                res = {"profile": data["profile"]}
            elif typ == "grant.revoke":
                self.core.policy.session_grants.discard(str(data.get("tool_id")))
                res = {"session_grants": sorted(self.core.policy.session_grants)}
            elif typ == "gemini.set_key":
                await asyncio.to_thread(secrets.store, "gemini", str(data.get("key", "")), "JARVIS — Gemini API key")
                self.core.gemini.reset()
                self.core.audit.write("provider.key_stored", provider="gemini")
                res = {"provider": await self.core.gemini.check()}
                d._touch()
            elif typ == "gemini.clear_key":
                await asyncio.to_thread(secrets.clear, "gemini")
                self.core.gemini.reset()
                self.core.audit.write("provider.key_removed", provider="gemini")
                res = {"provider": self.core.gemini.describe()}
                d._touch()
            elif typ == "gemini.check":
                res = {"provider": await self.core.gemini.check()}
                d._touch()
            elif typ == "voice.transcribe":
                if d.privacy_profile() != "AUTHORIZED_CLOUD":
                    raise ValueError("Распознавание голоса идёт через Gemini — в текущем режиме приватности оно выключено")
                audio = base64.b64decode(data.get("audio_b64", ""), validate=True)
                if not 1000 < len(audio) < 10 * 1024 * 1024:
                    raise ValueError("Запись слишком короткая или слишком длинная")
                mime = str(data.get("mime", "audio/webm")).split(";")[0]
                if mime not in ("audio/webm", "audio/ogg", "audio/wav", "audio/mp4"):
                    raise ValueError("Неподдерживаемый формат записи")
                t0 = time.perf_counter()
                text = await self.core.gemini.transcribe(audio, mime)
                self.core.audit.write("voice.transcribed", bytes=len(audio), chars=len(text),
                                      ms=round((time.perf_counter() - t0) * 1000))
                res = {"text": text, "ms": round((time.perf_counter() - t0) * 1000)}
            elif typ == "tool.run":
                # UI buttons (e.g. "open" on a file row). Same gateway/policy as everything else.
                if data.get("tool") not in UI_TOOLS:
                    raise ValueError("Это действие нельзя запустить из интерфейса")
                res = {"task_id": await d.run_ui_tool(data["tool"], dict(data.get("args") or {}))}
            elif typ == "tasks.list":
                res = self.core.tasks.recent(min(200, int(data.get("limit", 50))))
            elif typ == "flora.action" and d.flora:
                res = await d.flora.ui_action(d, data)
            else:
                raise ValueError(f"Неизвестный запрос: {typ}")
            await ws.send_str(json.dumps({"type": "reply", "req_id": rid, "ok": True, "data": res},
                                         ensure_ascii=False, default=str))
        except Exception as e:   # noqa: BLE001
            try:
                await ws.send_str(json.dumps({"type": "reply", "req_id": rid, "ok": False, "error": str(e)},
                                             ensure_ascii=False))
            except (ConnectionResetError, RuntimeError):
                pass


UNAUTH_PAGE = """<!doctype html><meta charset=utf-8><title>JARVIS</title>
<body style="font:15px system-ui;background:#0e0f12;color:#ddd;display:grid;place-items:center;height:100vh">
<div>Откройте JARVIS через ярлык или команду <code>jarvis</code>.</div></body>"""


def run(host: str | None = None, port: int | None = None) -> None:
    core = build()
    if port:
        core.config.port = port
    srv = Server(core)
    host = host or core.config.host
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("JARVIS слушает только loopback-адрес")
    print(f"JARVIS WorkOS on http://{host}:{srv.port}", flush=True)
    web.run_app(srv.app, host=host, port=srv.port, print=None, handle_signals=True, access_log=None)
