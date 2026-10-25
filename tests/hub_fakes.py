"""Stand-ins for OpenClaw's gateway (websocket, protocol 4) and Hermes's API server, for tests that must never need the
real agents (nor the person's Claude). Frames and payloads follow what the real OpenClaw 2026.10.6 and Hermes 0.19.0
sent on this computer (2026-10-25, dev core on ports 18889 / 18742)."""
from __future__ import annotations

import asyncio
import json
import socket
import uuid

from aiohttp import WSMsgType, web


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def skill(name: str, eligible: bool = True, path: str = "", **extra) -> dict:
    return {"name": name, "description": f"{name} skill", "source": "openclaw-bundled", "bundled": True,
            "filePath": path or f"/nonexistent/{name}/SKILL.md", "baseDir": f"/nonexistent/{name}", "skillKey": name,
            "disabled": False, "eligible": eligible,
            "missing": {"bins": [] if eligible else ["qpdf"], "env": []}, "requirements": {}, **extra}


class FakeGateway:
    def __init__(self, token: str) -> None:
        self.token = token
        self.port = free_port()
        self.requests: list[tuple[str, dict]] = []
        self.runs: dict[str, dict] = {}
        self.answer = "Голубое."
        self.skills = [skill("github", description="GitHub operations via gh CLI: issues, pull requests, CI runs."),
                       skill("pdf-editor", eligible=False), skill("weather")]
        self.methods = {
            "health": lambda p: {"ok": True, "plugins": {"loaded": ["browser"], "errors": []}},
            "skills.status": lambda p: {"skills": self.skills},
            "plugins.list": lambda p: {"plugins": [
                {"id": "telegram", "name": "Telegram", "installed": True, "enabled": True, "categories": ["channel"],
                 "category": "channel", "version": "2026.10.6", "origin": "bundled", "packageName": "@openclaw/telegram",
                 "runtime": {"state": "loaded"}}]},
            "channels.status": lambda p: {"channelOrder": ["telegram"], "channelLabels": {"telegram": "Telegram"},
                                          "channelAccounts": {"telegram": []}},
            "cron.list": lambda p: {"jobs": [
                {"id": "hb", "displayName": "Heartbeat (main)", "declarationKey": "heartbeat:main", "enabled": False,
                 "schedule": {"everyMs": 1800000}, "state": {}},
                {"id": "j2", "name": "Сводка", "enabled": True, "schedule": {"expr": "0 9 * * 1-5"},
                 "state": {"nextRunAtMs": 1792980000000}}]},
            "cron.update": lambda p: {"ok": True},
            "cron.run": lambda p: {"ok": True},
            "agent": self._agent, "agent.wait": self._agent_wait, "chat.abort": self._abort,
            "chat.history": lambda p: {"messages": []},
            "config.get": lambda p: {"hash": "h1"}, "config.patch": lambda p: {"ok": True},
        }
        self.ws: set = set()
        self.runner = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def start(self) -> "FakeGateway":
        app = web.Application()
        app.router.add_get("/", self._handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        return self

    async def stop(self) -> None:
        for ws in list(self.ws):
            await ws.close()
        await self.runner.cleanup()

    async def _handler(self, req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.ws.add(ws)
        await ws.send_str(json.dumps({"type": "event", "event": "connect.challenge", "payload": {"nonce": "n"}}))
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            f = json.loads(msg.data)
            method, params = f.get("method"), f.get("params") or {}
            self.requests.append((method, params))
            if method == "connect":
                if (params.get("auth") or {}).get("token") != self.token:
                    await ws.send_str(json.dumps({"type": "res", "id": f["id"], "ok": False,
                                                  "error": {"code": "unauthorized", "message": "auth token mismatch"}}))
                    continue
                await ws.send_str(json.dumps({"type": "res", "id": f["id"], "ok": True, "payload": {
                    "server": {"version": "2026.10.6", "buildId": "fake"},
                    "features": {"methods": list(self.methods) + ["connect", "skills.search", "skills.detail",
                                                                  "skills.install", "skills.update", "skills.skillCard",
                                                                  "skills.securityVerdicts"],
                                 "events": ["agent"]}, "auth": {"scopes": ["operator.read"]},
                    "policy": {"tickIntervalMs": 30000}}}))
                continue
            asyncio.ensure_future(self._answer(ws, f, method, params))     # like the real one: requests overlap
        self.ws.discard(ws)
        return ws

    async def _answer(self, ws, f, method, params):
        fn = self.methods.get(method)
        if fn is None:
            await ws.send_str(json.dumps({"type": "res", "id": f["id"], "ok": False,
                                          "error": {"code": "unknown", "message": f"no {method}"}}))
            return
        out = fn(params)
        if asyncio.iscoroutine(out):
            out = await out
        await ws.send_str(json.dumps({"type": "res", "id": f["id"], "ok": True, "payload": out}))

    async def _agent(self, p):
        rid = uuid.uuid4().hex
        self.runs[rid] = {"message": p.get("message"), "aborted": False}
        return {"runId": rid}

    async def _agent_wait(self, p):
        rid = p["runId"]
        for ws in list(self.ws):
            await ws.send_str(json.dumps({"type": "event", "event": "agent", "payload": {
                "runId": rid, "stream": "assistant", "data": {"text": self.answer}}}))
        await asyncio.sleep(float(self.runs[rid].get("delay", 0)))
        return {"status": "aborted" if self.runs[rid]["aborted"] else "ok"}

    def _abort(self, p):
        if p.get("runId") in self.runs:
            self.runs[p["runId"]]["aborted"] = True
        return {"ok": True}


class FakeHermes:
    """Hermes's API server: /health, /v1/models, /v1/runs, its SSE events, /approval, /stop."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.port = free_port()
        self.started: list[dict] = []
        self.answers: dict[str, str] = {}
        self.stopped: list[str] = []
        self.ask_approval = False
        self.output = "Готово: 391."
        self.runner = None
        self._approved: dict[str, asyncio.Future] = {}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _auth(self, req) -> bool:
        return req.headers.get("Authorization") == f"Bearer {self.key}"

    async def start(self) -> "FakeHermes":
        app = web.Application()
        app.router.add_get("/health", self._health)
        app.router.add_get("/v1/models", self._models)
        app.router.add_post("/v1/runs", self._run)
        app.router.add_get("/v1/runs/{rid}/events", self._events)
        app.router.add_post("/v1/runs/{rid}/approval", self._approval)
        app.router.add_post("/v1/runs/{rid}/stop", self._stop)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        return self

    async def stop(self) -> None:
        await self.runner.cleanup()

    async def _health(self, req):
        return web.json_response({"status": "ok", "version": "0.19.0"})

    async def _models(self, req):
        if not self._auth(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"data": []})

    async def _run(self, req):
        if not self._auth(req):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await req.json()
        rid = "run_" + uuid.uuid4().hex[:8]
        self.started.append({"id": rid, **body})
        self._approved[rid] = asyncio.get_running_loop().create_future()
        return web.json_response({"run_id": rid}, status=202)

    async def _events(self, req):
        rid = req.match_info["rid"]
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(req)

        async def send(ev):
            await resp.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
        await send({"event": "tool.started", "tool": "read_file"})
        await send({"event": "tool.completed", "tool": "read_file"})
        if self.ask_approval:
            await send({"event": "approval.request", "description": "удалить временный файл", "command": "rm /tmp/x"})
            choice = await self._approved[rid]
            if choice != "once":
                await send({"event": "run.completed", "output": "Шаг запрещён — остановился."})
                return resp
        await send({"event": "message.delta", "delta": self.output})
        await send({"event": "run.completed", "output": self.output, "usage": {"input_tokens": 10}})
        return resp

    async def _approval(self, req):
        rid = req.match_info["rid"]
        choice = (await req.json()).get("choice")
        self.answers[rid] = choice
        fut = self._approved.get(rid)
        if fut and not fut.done():
            fut.set_result(choice)
        return web.json_response({"ok": True})

    async def _stop(self, req):
        self.stopped.append(req.match_info["rid"])
        return web.json_response({"ok": True})
