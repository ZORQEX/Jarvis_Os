"""Local server: static UI, WebSocket for the UI, small bearer-token API for
the CLI and the Waybar module. Binds to loopback only."""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import mimetypes
import re
import secrets as pysecrets
import time
from pathlib import Path

from aiohttp import WSCloseCode, WSMsgType, web

from . import secrets
from .app import Core, build
from .events import redact, spawn

WEB = Path(__file__).resolve().parent.parent / "web"


def _ui_build() -> str:
    """Fingerprint of the window's files: an open window reloads itself when JARVIS restarts with a new interface."""
    import hashlib
    h = hashlib.sha256()
    for f in sorted(WEB.rglob("*")):
        if f.is_file():
            st = f.stat()
            h.update(f"{f.relative_to(WEB)}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()[:12]


UI_BUILD = _ui_build()
COOKIE = "jarvis_session"
UI_TOOLS = {"open_path", "open_url"}
KEY_PROVIDERS: dict[str, str] = {}      # no API keys: the AI is Claude through the local Claude Code sign-in
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
        r.add_get("/overlay", self.overlay_page)
        r.add_get("/api/overlay", self.api_overlay)
        r.add_get("/ws", self.ws)
        r.add_get("/health", self.health)
        r.add_get("/api/state", self.api_state)
        r.add_get("/api/events", self.api_events)
        r.add_post("/api/control", self.api_control)
        r.add_post("/api/submit", self.api_submit)
        r.add_get("/artifact", self.artifact)
        r.add_get("/static/{path:.+}", self.static)
        # Tool calls of the Claude conversation (jarvis/mcp_bridge.py). Own secret per run, loopback only; every call
        # still goes through the Gateway (policy, the user's approval, verification, audit).
        self.mcp_secret = pysecrets.token_urlsafe(32)
        r.add_post("/mcp/call", self.mcp_call)
        self.app.on_startup.append(self.on_startup)
        self.app.on_shutdown.append(self.on_shutdown)
        self.app.on_cleanup.append(self.on_cleanup)
        self.sockets: set[web.WebSocketResponse] = set()
        from .overlay import Overlay
        self.overlay = Overlay(core.db, core.config.port)
        self._mic: dict = {}                            # socket → its microphone is open (the main window)
        self._wake_until = 0.0                          # follow-up window after «Джарвис, …»
        wt = core.director.gw.registry.get("window_show")
        if wt:
            wt.presence = lambda: bool(self.sockets)      # is a JARVIS window connected right now

    @property
    def _speech_owner(self) -> dict[str, str]:
        """Task id → the window that plays its speech (kept by the director: a series of tasks keeps its window)."""
        return self.core.director.speech_owner

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
        if req.path == "/mcp/call":
            if req.method != "POST" or not hmac.compare_digest(req.headers.get("X-JARVIS-MCP", ""), self.mcp_secret):
                return web.json_response({"error": "unauthorized"}, status=401)
            return await handler(req)
        if req.path in ("/", "/overlay") and req.query.get("t"):
            if hmac.compare_digest(req.query["t"], self.token):
                resp = web.Response(status=302, headers={"Location": req.path, "Cache-Control": "no-store"})
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
        self.core.claude.bridge = {"url": f"http://127.0.0.1:{self.port}/mcp/call", "secret": self.mcp_secret}
        self.core.scheduler.start()
        if self.core.comms:
            spawn(self.core.comms.start(), "comms-start")
        if self.core.director.speech:
            spawn(self._prewarm_speech(), "speech-prewarm")
        self._fwd = asyncio.create_task(self._forward())
        spawn(self._check_providers(), "providers-check")
        if self.core.director.fashion:
            spawn(self.core.director.fashion.start(), "fashion-start")

    async def _prewarm_speech(self):
        try:
            await self.core.director.speech.engine.prewarm()
        except Exception as e:   # noqa: BLE001 — speech is optional; text keeps working
            self.core.audit.write("speech.prewarm_failed", error=repr(e)[:200])
        stt = getattr(self.core.director, "stt", None)
        if stt and stt.available():
            try:
                from .voice.stt import VOCAB
                stt.mode = "ru" if self.core.db.get_kv("stt_mode") == "ru" else "auto"
                names = [w for r in self.core.db.all("SELECT name FROM contacts") for w in str(r["name"]).split()]
                await stt.add_vocab([*VOCAB, *names])
                await stt.start()                 # load the recognition model before the first phrase
            except Exception as e:   # noqa: BLE001
                self.core.audit.write("stt.prewarm_failed", error=repr(e)[:200])

    async def on_shutdown(self, app):
        """Close the windows' live connections first: otherwise aiohttp waits for them (measured: 15–35 s stop)."""
        for ws in list(self.sockets):
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"JARVIS restarting")

    async def on_cleanup(self, app):
        self._fwd.cancel()
        if self.core.director.speech:
            await self.core.director.speech.engine.stop()
        if getattr(self.core.director, "stt", None):
            await self.core.director.stt.stop()
        await self.core.claude.stop()
        if getattr(self.core.director, "github", None):
            await self.core.director.github.cancel()     # an unfinished GitHub login does not outlive JARVIS
        await self.core.scheduler.stop()
        if self.core.comms:
            await self.core.comms.stop()
        if self.core.director.fashion:
            await self.core.director.fashion.stop()
        for ws in list(self.sockets):
            await ws.close()
        self.core.audit.write("core.stop")

    async def _warm(self) -> bool:
        """Called when the user starts speaking: make sure the Claude conversation process is running (no usage)."""
        now_ = time.time()
        if now_ - getattr(self, "_warmed_at", 0) < 20 or not self.core.claude.configured():
            return False
        self._warmed_at = now_
        try:
            await self.core.claude.warm()
            return True
        except Exception:   # noqa: BLE001
            return False

    def _voice_audio(self, data: dict) -> bytes:
        local = getattr(self.core.director, "stt", None)
        if not (local and local.available()):
            raise ValueError("Распознавание речи не установлено (scripts/install.sh — GigaAM, на этом компьютере)")
        audio = base64.b64decode(data.get("audio_b64", ""), validate=True)
        if not 1000 < len(audio) < 10 * 1024 * 1024:
            raise ValueError("Запись слишком короткая или слишком длинная")
        mime = str(data.get("mime", "audio/wav")).split(";")[0]
        if mime not in ("audio/webm", "audio/ogg", "audio/wav", "audio/mp4"):
            raise ValueError("Неподдерживаемый формат записи")
        if mime != "audio/wav":
            raise ValueError("Не удалось распознать речь: нужна запись WAV")
        return audio

    def _wake_text(self, text: str, data: dict) -> str | None:
        """Wake listening: the phrase to act on, or None if it was not for JARVIS. A recording may start with other
        speech («…телевизор… Джарвис, который час»): the command is what follows the name. While or right after JARVIS
        speaks, a phrase that repeats what it just said is its own voice through the speakers, not the person."""
        from .router import find_wake
        t = (text or "").strip()
        if not t:
            return None
        t = self._strip_echo(t, data)
        if not t:
            return None
        dic = getattr(self.core.director, "dictation", None)
        if data.get("barge") or self._addressed(t) or (dic and dic.on):     # dictation: every phrase is for JARVIS
            return t
        pos = find_wake(t)
        return t[pos:] if pos >= 0 else None

    def _strip_echo(self, text: str, data: dict) -> str:
        """Drop what the microphone heard of JARVIS's own voice — from this window or the other one (the companion
        and the main window run in separate browsers, so neither's echo cancellation knows the other's sound)."""
        sp = self.core.director.speech
        if not sp or not text:
            return text
        t0, t1 = data.get("speech_start"), data.get("speech_end")
        try:
            t0, t1 = (float(t0), float(t1)) if t0 and t1 else (None, None)
        except (TypeError, ValueError):
            t0 = t1 = None
        return sp.strip_echo(text, t0, t1).strip()

    def _voice_active(self, on: bool, data: dict) -> None:
        """The window heard the person start (or stop) talking: JARVIS does not start a sentence over them, and a
        cut-off phrase waits for its end."""
        sp = self.core.director.speech
        now_ = time.time()
        self._voice_until = now_ + 30 if on else 0.0
        if not on:
            self._voice_quiet_at = now_
        # The always-on companion hears a TV too: only a conversation with JARVIS holds its voice back.
        if sp and (not data.get("wake") or now_ < self._wake_until):
            sp.hold_until = now_ + 30 if on else 0.0

    def _addressed(self, text: str) -> bool:
        """Wake listening: is this phrase for JARVIS? «Джарвис…» at the start, an answer within 20 s of the last
        exchange, a «да / нет» to a pending confirmation, or the continuation of a cut-off command."""
        from .comms.agent import yes_no
        from .router import wake_len
        if wake_len(text or ""):
            return True
        if time.time() < self._wake_until:
            return True
        if getattr(self, "_fragment", None) and time.time() - self._fragment[1] < FRAGMENT_TTL_S:
            return True
        return bool(self.core.approvals.pending()) and yes_no(text) is not None

    async def _voice_wake(self, data: dict) -> dict:
        """Always-on listening from the desktop companion. Speech that is not addressed to JARVIS is recognised on this
        computer and dropped at once: no turn, no transcript stored, nothing sent anywhere."""
        audio = self._voice_audio(data)
        t0 = time.time()
        text = await self._transcribe(audio, data, turn=True)
        return await self._wake_turn(text, data, len(audio), t0, time.time())

    async def _transcribe(self, audio: bytes, data: dict, turn: bool = False) -> str:
        """Waiting for «Джарвис» (not a follow-up, not cutting in): the second recogniser runs only for a phrase that
        has something like the name in it. `turn`: also hear whether the person sounds finished (data["_turn"])."""
        stt = self.core.director.stt
        wake = bool(data.get("wake") and not data.get("barge") and time.time() >= self._wake_until
                    and not getattr(self, "_fragment", None))
        kw = {**({"wake": True} if wake else {}), **({"turn": True} if turn else {})}
        try:
            text = await stt.transcribe(audio, **kw) if kw else await stt.transcribe(audio)
        except TypeError:                         # a recogniser without these options
            text = await stt.transcribe(audio)
        data["_turn"] = getattr(stt, "last_turn", None) if turn else None
        return text

    async def _wake_turn(self, text: str, data: dict, nbytes: int, stt_start: float, stt_end: float) -> dict:
        text = self._wake_text(text, data)
        if text is None:
            return {"text": "", "ignored": True}
        self._wake_until = time.time() + 20
        self.core.bus.publish("wake.heard", {"text": text[:200]})
        sp = self.core.director.speech
        turn = sp.new_turn(data.get("speech_start"), data.get("speech_end")) if sp else None
        if sp:
            sp.mark(turn, "stt_start", stt_start)
            sp.mark(turn, "stt_end", stt_end)
        return await self._after_stt(text, turn, nbytes, data)

    async def _voice_turn(self, data: dict) -> dict:
        """One spoken request: STT → Director (same path as typed text) → streamed speech."""
        d = self.core.director
        self._voice_active(False, data)
        if data.get("wake"):
            return await self._voice_wake(data)
        audio = self._voice_audio(data)
        sp = d.speech
        turn = sp.new_turn(data.get("speech_start"), data.get("speech_end")) if sp else None
        if sp:
            sp.mark(turn, "stt_start")
        try:
            text = await self._transcribe(audio, data, turn=True)     # on this computer: no internet, no quota
        except Exception as e:   # noqa: BLE001
            if sp:
                sp.annotate(turn, error=str(e)[:200])
                sp.say("notice-" + (turn or "x"), "Не удалось распознать речь. Повтори, пожалуйста.", turn)
            raise ValueError("Не удалось распознать речь: " + str(e)[:150]) from None
        if sp:
            sp.mark(turn, "stt_end")
        return await self._after_stt(text, turn, len(audio), data)

    async def _voice_probe(self, data: dict) -> dict:
        """Early end of phrase: transcribe after a short pause (0.6 s). If the phrase is clearly complete the window
        commits it at once (saves the rest of the 1.3 s wait); a cut-off phrase («Отправь маме в…») keeps waiting."""
        audio = self._voice_audio(data)
        t0 = time.time()
        text = await self._transcribe(audio, data, turn=True)
        frag = getattr(self, "_fragment", None)
        joined = (frag[0] + " " + text) if frag and time.time() - frag[1] < FRAGMENT_TTL_S else text
        pid = pysecrets.token_hex(8)
        now_ = time.time()
        self._probes = {k: v for k, v in getattr(self, "_probes", {}).items() if now_ - v["at"] < 30}
        p = data.get("_turn")
        self._probes[pid] = {"text": text, "stt_start": t0, "stt_end": now_, "at": now_, "bytes": len(audio), "turn": p}
        # The words look finished, and the voice does not clearly say «I'm not done» (Smart Turn).
        complete = phrase_complete(joined) and (p is None or p >= TURN_VETO)
        if data.get("wake"):          # wake listening: only a phrase for JARVIS is answered early
            complete = complete and self._wake_text(text, data) is not None
        return {"probe_id": pid, "complete": complete, "chars": len(text)}

    async def _voice_commit(self, data: dict) -> dict:
        """The window confirms a probe: the user stayed silent, so its transcript is the phrase (no second STT)."""
        pr = getattr(self, "_probes", {}).pop(str(data.get("probe_id")), None)
        if not pr:
            raise ValueError("probe expired")
        self._voice_active(False, data)
        data["_turn"] = pr.get("turn")
        if data.get("wake"):
            return await self._wake_turn(pr["text"], data, pr["bytes"], pr["stt_start"], pr["stt_end"])
        sp = self.core.director.speech
        turn = sp.new_turn(data.get("speech_start"), data.get("speech_end")) if sp else None
        if sp:
            sp.mark(turn, "stt_start", pr["stt_start"])
            sp.mark(turn, "stt_end", pr["stt_end"])
        return await self._after_stt(pr["text"], turn, pr["bytes"], data)

    async def _after_stt(self, text: str, turn: str | None, nbytes: int, data: dict) -> dict:
        d = self.core.director
        sp = d.speech
        if sp:
            sp.annotate(turn, stt_model=getattr(d.stt, "last_model", None) or d.stt.name, transcript=text)
        self.core.audit.write("voice.transcribed", bytes=nbytes, chars=len(text), turn=turn)
        heard, text = text, self._strip_echo(text, data)
        if heard.strip() and not text:                 # only JARVIS's own voice through the speakers
            if sp:
                sp.annotate(turn, route="echo")
            return {"text": "", "ignored": True, "turn": turn}
        if not text.strip():
            if sp:
                from .voice.tts import NOT_HEARD_BY_LANG, phrase
                sp.say("notice-" + (turn or "x"), phrase(NOT_HEARD_BY_LANG, sp.lang), turn)
            return {"text": "", "turn": turn}
        dic = getattr(d, "dictation", None)
        if dic and dic.on and data.get("submit", True):
            from .router import find_wake, strip_wake
            if dic.is_stop(text):
                msg = await dic.stop()
                if sp:
                    sp.annotate(turn, route="control:dictation_off")
                    sp.say("notice-" + (turn or "x"), msg, turn)
                return {"text": text, "dictation": "stopped", "turn": turn}
            if not (find_wake(text) == 0 and strip_wake(text).strip()):     # «Джарвис, …» stays a command
                try:
                    typed = await dic.type(text)
                except (RuntimeError, OSError) as e:
                    await dic.stop()
                    if sp:
                        sp.say("notice-" + (turn or "x"), "Не получилось напечатать — диктовка выключена.", turn)
                    self.core.audit.write("dictation.failed", error=str(e)[:200])
                    return {"text": text, "dictation": "failed", "turn": turn}
                if sp:
                    sp.annotate(turn, route="dictation")
                return {"text": text, "dictation": True, "typed": typed, "turn": turn}
        # A phrase cut off mid-command («Отправь…», «…в», «…моей») is kept and joined with the next one, instead of
        # being answered on its own (measured: «Отправь» / «в Телеграме» / «любимые с сердечком» became 3 requests).
        frag = getattr(self, "_fragment", None)
        if frag and time.time() - frag[1] < FRAGMENT_TTL_S:
            text = frag[0] + " " + text
        self._fragment = None
        if data.get("submit", True) and (is_fragment(text) or unfinished(text, data.get("_turn"))):
            self._hold_fragment(text, turn, data)
            if sp:
                sp.annotate(turn, route="fragment")
            return {"text": text, "turn": turn, "pending": True}
        return await self._submit_turn(text, turn, data)

    async def _submit_turn(self, text: str, turn: str | None, data: dict) -> dict:
        d = self.core.director
        sp = d.speech
        task_id = None
        client = str(data.get("client") or "window")[:20]
        d._own("notice-" + str(turn), client)          # the companion's answers are spoken by the companion only
        if data.get("submit", True):
            if sp:
                sp.mark(turn, "request_start")
            task_id = await d.submit(text, "voice", turn=turn, client=client)
        if task_id or data.get("wake"):
            self._wake_until = time.time() + 20       # «…а ещё…» right after the answer needs no «Джарвис»
        return {"text": text, "task_id": task_id, "turn": turn}

    def _hold_fragment(self, text: str, turn: str | None, data: dict) -> None:
        """A cut-off phrase («Отправь маме в…») waits for its end — but not forever: when the person says nothing more
        it is done as it is (JARVIS asks what is missing). Before 2026-10-24 it was silently dropped after 15 s, and
        «Расскажи что-нибудь» (wrongly taken for cut off) got no answer at all."""
        self._fragment = (text.rstrip(" .,…"), time.time(), turn, {k: data.get(k) for k in ("client", "wake")})
        self._frag_gen = getattr(self, "_frag_gen", 0) + 1
        spawn(self._fragment_timeout(self._frag_gen), "voice-fragment")

    async def _fragment_timeout(self, gen: int) -> None:
        while True:
            await asyncio.sleep(0.25)
            frag = getattr(self, "_fragment", None)
            if not frag or gen != self._frag_gen:
                return                                   # the rest came: joined and answered already
            if time.time() < getattr(self, "_voice_until", 0):
                continue                                 # the person is saying the rest right now
            if time.time() - max(frag[1], getattr(self, "_voice_quiet_at", 0)) >= FRAGMENT_WAIT_S:
                break
        self._fragment = None
        text, _, turn, data = frag
        self.core.audit.write("voice.fragment_done", turn=turn)
        try:
            await self._submit_turn(text, turn, data)
        except Exception as e:   # noqa: BLE001
            self.core.audit.write("voice.fragment_failed", error=repr(e)[:200])

    async def _check_providers(self):
        """Startup health: Claude Code sign-in status (no usage); then the conversation process is started, so the
        first question does not pay the CLI start-up (measured ~3 s cold vs ~1–1.5 s warm)."""
        mgr = self.core.director.providers
        for pid, p in mgr.providers.items():
            if p.configured():
                try:
                    await p.health_check(live=False)
                except Exception as e:   # noqa: BLE001 — health must never crash startup
                    self.core.audit.write("provider.health_error", provider=pid, error=repr(e)[:200])
        if self.core.claude.configured() and self.core.claude.logged_in is not False:
            try:
                await self.core.claude.warm()
                self.core.claude.status = "ready"
            except Exception as e:   # noqa: BLE001
                self.core.audit.write("claude.warm_failed", error=repr(e)[:200])
        self._publish_providers()

    def _publish_providers(self):
        self.core.bus.publish("providers", {"providers": self.core.director.providers.describe(),
                                            "provider": self.core.claude.describe()})
        self.core.director._touch()

    async def _forward(self):
        q = self.core.bus.subscribe()
        while True:
            ev = await q.get()
            dead = []
            owner = self.core.director.speech_owner.get(ev.get("task_id") or "") \
                if str(ev.get("kind", "")).startswith("speech.") else None
            if owner == "overlay" and ev.get("kind") == "speech.end":
                # «…а ещё…» without «Джарвис» counts from when JARVIS stopped talking, not from the question.
                self._wake_until = max(self._wake_until, time.time() + 12)
            payload = json.dumps({"type": "event", **ev, **({"owner": owner} if owner else {})}, ensure_ascii=False, default=str)
            for ws in list(self.sockets):
                try:
                    await ws.send_str(payload)
                except (ConnectionResetError, RuntimeError):
                    dead.append(ws)
            for ws in dead:
                self.sockets.discard(ws)

    # ------------------------------------------------------------------ handlers
    async def mcp_call(self, req):
        try:
            body = await req.json()
        except ValueError:
            return web.json_response({"error": "bad json"}, status=400)
        name, args = str(body.get("name", ""))[:64], body.get("arguments") or {}
        self.core.audit.write("claude.tool_call", tool=name)
        res = await self.core.claude.mcp_call(name, args if isinstance(args, dict) else {})
        return web.json_response({"result": res})

    async def health(self, req):
        return web.json_response({"ok": True, "app": "jarvis-workos"})

    async def overlay_page(self, req):
        return web.FileResponse(WEB / "overlay.html", headers={
            "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": CSP})

    async def api_overlay(self, req):
        """For the launcher: is the desktop companion wanted, and where did the person leave it."""
        return web.json_response({"enabled": bool(self.core.db.get_kv("overlay_enabled", True)),
                                  "pos": self.overlay.saved(), "wake": bool(self.core.db.get_kv("wake_listen", False))})

    async def index(self, req):
        csp = CSP
        if self.core.director.fashion:      # «Заказы» shows the orders app (its own loopback origin) inside the window
            csp += f"; frame-src {self.core.director.fashion.url}"
        return web.FileResponse(WEB / "index.html", headers={
            "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": csp})

    async def static(self, req):
        p = (WEB / req.match_info["path"]).resolve()
        if not p.is_relative_to(WEB) or not p.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Cache-Control": "no-cache"})

    async def artifact(self, req):
        cfg = self.core.config
        roots = [cfg.expand(cfg.screenshot_dir), cfg.expand(cfg.write_dir), self.core.paths.artifacts]
        try:
            p = Path(req.query.get("path", "")).resolve()
        except (OSError, ValueError):
            raise web.HTTPNotFound() from None
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
        if op not in ("estop", "estop_clear", "pause_all", "resume_all", "stop_speech", "dictation_on", "dictation_off",
                      "dictation_toggle"):
            return web.json_response({"error": "unknown op"}, status=400)
        msg = await self.core.director.control(op)
        return web.json_response({"ok": True, "message": msg})

    async def api_submit(self, req):
        body = await req.json()
        # The bearer API can never act as the window («voice» / «text» answer confirmations): only api / cli.
        src = body.get("source") if body.get("source") in ("api", "cli") else "api"
        tid = await self.core.director.submit(str(body.get("text", "")), source=src, speak=bool(body.get("speak")))
        return web.json_response({"ok": True, "task_id": tid})

    # ------------------------------------------------------------------ websocket
    async def ws(self, req):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=12 * 1024 * 1024)
        await ws.prepare(req)
        self.sockets.add(ws)
        await ws.send_str(json.dumps({"type": "snapshot", "data": {**self.core.director.snapshot(), "ui_build": UI_BUILD}},
                                     ensure_ascii=False, default=str))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                spawn(self._handle(ws, data), "ws-request")
        finally:
            self.sockets.discard(ws)
            if self._mic.pop(ws, False) and not any(self._mic.values()):
                self.core.bus.publish("voice.presence", {"client": "window", "listening": False})
        return ws

    def _voices(self) -> dict:
        from .voice.lang import NAMES
        from .voice.tts import EN_VOICES, VOICES
        eng = self.core.director.speech.engine if self.core.director.speech else None
        ok = bool(getattr(eng, "silero", None) and eng.silero.available())
        st_ok = bool(getattr(eng, "supertonic", None) and eng.supertonic.available())
        en_ok = bool(getattr(eng, "kokoro", None) and eng.kokoro.available())
        stt = getattr(self.core.director, "stt", None)
        langs = eng.languages() if hasattr(eng, "languages") else ["ru"]
        return {"voices": [{**v, "available": st_ok if v.get("engine") == "supertonic" else ok} for v in VOICES],
                "current": getattr(eng, "voice_id", None), "supertonic_installed": st_ok,
                "all_languages": bool(st_ok and str(getattr(eng, "voice_id", "")).startswith("supertonic:")),
                "en_voices": [{**v, "available": en_ok} for v in EN_VOICES],
                "en_current": (getattr(eng, "en", None) or {}).get("id"), "en_installed": en_ok,
                "languages": [{"id": x, "name": NAMES.get(x, x)} for x in langs],
                "stt_mode": getattr(stt, "mode", "ru"), "stt_multi": bool(stt and stt.multilingual()),
                "rate": getattr(eng, "rate", 1.0), "installed": ok,
                "last_engine": getattr(eng, "last_engine", None), "last_fallback": getattr(eng, "last_fallback", None),
                "ai_model": self.core.db.get_kv("voice_ai_model") or "fast",
                "ai_models": {"fast": self.core.config.claude_voice_model, "quality": self.core.config.claude_chat_model}}

    async def _voice_preview(self, vid: str, tid: str, en: bool = False) -> None:
        """Speak a sample in any voice without changing the chosen one."""
        from .voice.tts import PREVIEW, PREVIEW_EN, en_voice, voice_info
        bus = self.core.bus
        v = en_voice(vid) if en else voice_info(vid)
        eng = self.core.director.speech.engine if self.core.director.speech else None
        try:
            audio = await (eng.preview_en(v["id"]) if en else eng.preview(v["id"]))
            self.core.director.speech.played(tid, PREVIEW_EN if en else PREVIEW, audio)    # for the echo check
            bus.publish("speech.audio", {"seq": 1, "text": PREVIEW_EN if en else PREVIEW, "mime": "audio/wav",
                                         "audio_b64": base64.b64encode(audio).decode()}, tid)
            bus.publish("speech.end", {"segments": 1}, tid)
        except Exception as e:   # noqa: BLE001
            bus.publish("speech.error", {"error": f"Голос «{v['name']}»: {str(e)[:150]}"}, tid)

    async def _comms(self, typ, data):
        return await _comms_action(self, typ, data)

    async def _handle(self, ws, data: dict):
        rid = data.get("req_id")
        d = self.core.director
        typ = data.get("type")
        if typ not in QUIET_UI:
            self.core.audit.write("ui." + str(typ)[:40], **{k: v for k, v in redact(data).items()
                                                            if k in ("type", "id", "task_id", "profile", "op")})
        try:
            if typ == "submit":
                res = {"task_id": await d.submit(str(data.get("text", "")), data.get("source", "text"),
                                                 speak=bool(data.get("speak")))}
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
            elif typ == "access.set":         # window only (cookie-authenticated UI), never the bearer API
                res = {"access": d.set_access(str(data.get("level")))}
            elif typ == "grant.revoke":
                self.core.policy.session_grants.discard(str(data.get("tool_id")))
                res = {"session_grants": sorted(self.core.policy.session_grants)}
            elif typ == "voice.transcribe":
                res = await self._voice_turn(data)
            elif typ == "voice.probe":
                res = await self._voice_probe(data)
            elif typ == "voice.commit":
                res = await self._voice_commit(data)
            elif typ == "voice.interrupt":        # Esc / the stop button while JARVIS talks: that answer only
                res = {"message": await d.control("quiet", task_ids=[str(x) for x in data.get("tasks") or []][:20])}
            elif typ == "voice.barge":            # the person started talking over JARVIS: stop the voice, not the work
                res = {"message": await d.control("stop_speech",
                                                  task_ids=[str(x) for x in data.get("tasks") or []][:20])}
            elif typ == "voice.active":           # the person started / stopped talking (before the phrase is sent)
                self._voice_active(bool(data.get("on")), data)
                res = {}
            elif typ == "voice.metric":
                if d.speech and data.get("field") == "first_audio_played":
                    d.speech.mark(str(data.get("turn")), "first_audio_played", float(data.get("ts") or time.time()))
                res = {}
            elif typ == "voice.warm":
                res = {"warmed": await self._warm()}
            elif typ == "voice.test":
                sp = getattr(self.core.director, "speech", None)
                if not sp:
                    raise ValueError("Озвучивание не настроено")
                tid = "test_" + pysecrets.token_hex(4)
                sp.say(tid, "Проверка звука. Если вы меня слышите, всё в порядке.")
                res = {"task_id": tid}
            elif typ == "voice.voices":
                res = self._voices()
            elif typ == "voice.ai_model":         # which Claude model answers spoken questions
                pref = data.get("pref")
                if pref not in ("fast", "quality"):
                    raise ValueError("Неизвестный режим")
                self.core.db.set_kv("voice_ai_model", pref)
                res = {"ai_model": pref}
            elif typ == "voice.set":
                from .voice.tts import voice_info
                eng = d.speech.engine if d.speech else None
                v = voice_info(str(data.get("voice")))
                st = getattr(eng, "supertonic", None)
                installed = (st and st.available()) if v.get("engine") == "supertonic" else \
                    (hasattr(eng, "silero") and eng.silero.available())
                if not (hasattr(eng, "set_voice") and installed):
                    raise ValueError("Этот голос не установлен — запустите scripts/install.sh")
                rate = min(1.4, max(0.7, float(data.get("rate", self.core.db.get_kv("tts_rate") or 1.0))))
                await eng.set_voice(v["id"], rate)
                self.core.db.set_kv("tts_voice", v["id"])
                self.core.db.set_kv("tts_rate", rate)
                self.core.audit.write("voice.selected", voice=v["id"], rate=rate)
                spawn(eng.prewarm(), "voice-prewarm")         # the next answer does not wait for the voice to load
                res = self._voices()
            elif typ == "voice.preview":
                tid = "preview_" + pysecrets.token_hex(4)
                spawn(self._voice_preview(str(data.get("voice")), tid, en=bool(data.get("en"))), "voice-preview")
                res = {"task_id": tid}
            elif typ == "voice.set_en":           # the English voice (Kokoro)
                eng = d.speech.engine if d.speech else None
                if not hasattr(eng, "set_en_voice"):
                    raise ValueError("Озвучивание не настроено")
                v = eng.set_en_voice(str(data.get("voice")))
                self.core.db.set_kv("tts_voice_en", v["id"])
                self.core.audit.write("voice.selected_en", voice=v["id"])
                res = self._voices()
            elif typ == "voice.stt_mode":         # «auto»: Russian + other languages; «ru»: Russian only (less memory)
                mode = "ru" if data.get("mode") == "ru" else "auto"
                self.core.db.set_kv("stt_mode", mode)
                if d.stt:
                    await d.stt.set_mode(mode)
                    if d.stt.available():
                        spawn(d.stt.start(), "stt-restart")      # load the models now, not on the next phrase
                res = self._voices()
            elif typ == "voice.output":
                res = await audio_output(fix=bool(data.get("fix")))
                if data.get("fix"):
                    self.core.audit.write("voice.output_fixed", volume=res.get("volume_pct"))
            elif typ in ("memory.list", "memory.forget"):    # Settings → Память
                from .memory import facts, forget
                if typ == "memory.forget":
                    gone = forget(self.core.db, fid=str(data.get("id") or ""))
                    self.core.audit.write("memory.forgotten", count=len(gone))
                res = {"facts": facts(self.core.db)}
            elif typ == "improve.issues":         # Settings → Самоулучшение: problems from JARVIS's own records
                from .improve import collect_issues, pending_branches
                res = {"issues": collect_issues(self.core.db, min(60.0, max(1.0, float(data.get("days") or 7)))),
                       "branches": pending_branches()}
            elif typ == "voice.turns":
                res = d.speech.turns(int(data.get("limit", 20))) if d.speech else []
            elif typ == "tool.run":
                # UI buttons (e.g. "open" on a file row). Same gateway/policy as everything else.
                if data.get("tool") not in UI_TOOLS:
                    raise ValueError("Это действие нельзя запустить из интерфейса")
                res = {"task_id": await d.run_ui_tool(data["tool"], dict(data.get("args") or {}))}
            elif typ == "ui.read":                # Files workspace: folder listing / search, read-only
                res = await d.run_ui_read(str(data.get("tool")), dict(data.get("args") or {}))
            elif typ == "overlay.drag":           # desktop companion: the core moves its window after the cursor
                ph = data.get("phase")
                res = await self.overlay.drag_start(data.get("ox", 32), data.get("oy", 32)) if ph == "start" \
                    else self.overlay.drag_move() if ph == "move" else await self.overlay.drag_end()
            elif typ == "ui.view":                # the workspace the window shows («Заказы» → order phrases go there)
                d.view = str(data.get("view") or "")[:24]
                res = {"view": d.view}
            elif typ in ("fashion.state", "fashion.start", "fashion.conversation"):     # «Заказы»
                f = d.fashion
                if f is None:
                    res = {"installed": False, "enabled": False, "running": False}
                else:
                    if typ == "fashion.conversation":
                        f.set_conversation(str(data.get("id") or ""))
                    elif typ == "fashion.start":
                        await f.start()
                    res = await f.state()
            elif typ == "voice.presence":         # the window's mic is on/off: the desktop companion yields to it
                if str(data.get("client", "window")) == "window":
                    self._mic[ws] = bool(data.get("listening"))
                self.core.bus.publish("voice.presence", {"client": str(data.get("client", "window"))[:20],
                                                         "listening": bool(data.get("listening"))})
                res = {}
            elif typ == "overlay.size":
                res = await self.overlay.resize(int(data.get("w", 64)), int(data.get("h", 64)))
            elif typ == "overlay.place":
                res = await self.overlay.place_default()
            elif typ == "overlay.close":
                res = await self.overlay.close()
            elif typ in ("overlay.state", "overlay.settings"):
                db = self.core.db
                if typ == "overlay.settings":
                    for k, key in (("wake", "wake_listen"), ("enabled", "overlay_enabled")):
                        if k in data:
                            db.set_kv(key, bool(data[k]))
                            self.core.audit.write("overlay." + key, on=bool(data[k]))
                res = {"wake": bool(db.get_kv("wake_listen", False)), "enabled": bool(db.get_kv("overlay_enabled", True)),
                       "window_listening": any(self._mic.values())}
                if typ == "overlay.settings":
                    self.core.bus.publish("overlay.settings", res)
            elif typ in ("overlay.launch", "window.open"):
                # The person pressed a button: open the desktop companion / the main window (never closes anything).
                import subprocess as _sp
                import sys as _sys
                _sp.Popen([_sys.executable, "-m", "jarvis", "overlay" if typ == "overlay.launch" else "open"],
                          cwd=str(WEB.parent), stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                          start_new_session=True)
                res = {"ok": True}
            elif typ == "files.roots":            # Files workspace: the folders JARVIS may read (paths only)
                cfg = self.core.config
                roots = [str(r) for r in cfg.read_roots if r.is_dir()]
                res = {"roots": roots, "write_dir": str(cfg.expand(cfg.write_dir)), "home": str(Path.home())}
            elif typ == "task.detail":            # Task Center timeline: the task with all its decisions
                t = self.core.tasks.get(str(data.get("task_id")))
                if not t:
                    raise ValueError("Задача не найдена")
                res = {"task": t, "approvals": self.core.approvals.for_task(t["id"])}
            elif typ == "tasks.list":
                res = self.core.tasks.recent(min(300, int(data.get("limit", 50))),
                                             exclude_route=None if data.get("include_ui_read") else "ui_read")
            elif typ == "publish.state":          # GitHub for site publishing (window only, user-driven)
                res = await d.github.state()
            elif typ == "publish.github.begin":
                res = await d.github.begin()
                self.core.audit.write("github.login_started")
            elif typ == "publish.github.logout":
                await d.github.logout()
                self.core.audit.write("github.logout")
                res = await d.github.state()
            elif typ == "history.list":          # action history: lasting changes only
                res = d.history.list(int(data.get("limit", 100)))
            elif typ == "history.delete":        # removes the entry only; files on disk stay as they are
                res = {"deleted": d.history.delete(str(data.get("id", "")))}
            elif typ == "agency.state":
                res = {"providers": d.providers.describe()}
            elif typ == "agency.settings":
                d.providers.update_settings(str(data.get("provider")), **{k: data[k] for k in
                                            ("enabled", "fallback_eligible", "max_sensitivity") if k in data})
                self.core.audit.write("provider.settings", provider=data.get("provider"),
                                      **{k: data[k] for k in ("enabled", "fallback_eligible", "max_sensitivity") if k in data})
                self._publish_providers()
                res = {"providers": d.providers.describe()}
            elif typ == "agency.check":
                p = d.providers.get(str(data.get("provider")))
                if not p:
                    raise ValueError("Неизвестный провайдер")
                await p.health_check(live=bool(data.get("live")))
                self._publish_providers()
                res = {"providers": d.providers.describe()}
            elif typ in ("provider.set_key", "provider.clear_key"):
                pid = str(data.get("provider"))
                if pid not in KEY_PROVIDERS:
                    raise ValueError("Для этого провайдера ключ не используется")
                if typ == "provider.set_key":
                    await asyncio.to_thread(secrets.store, pid, str(data.get("key", "")), KEY_PROVIDERS[pid])
                else:
                    await asyncio.to_thread(secrets.clear, pid)
                p = d.providers.get(pid)
                p.reset()
                self.core.audit.write("provider.key_" + ("stored" if typ.endswith("set_key") else "removed"), provider=pid)
                if typ == "provider.set_key":
                    await p.health_check()
                self._publish_providers()
                res = {"providers": d.providers.describe()}
            elif str(typ).startswith("comms.") and self.core.comms:
                res = await self._comms(typ, data)
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


QUIET_UI = {"voice.active", "ui.view", "fashion.state"}
TURN_HOLD = 0.15     # Smart Turn this sure the person is not done: wait for the rest (the int8 model is jumpy near 0.5)
TURN_VETO = 0.30     # …and below this no early answer after a short pause  # sent at every sound the always-on companion takes for speech: not worth an audit line
FRAGMENT_TTL_S = 15.0
FRAGMENT_WAIT_S = 4.0       # a cut-off phrase with no continuation after this much quiet is done as it is
_DANGLING = {"в", "во", "на", "и", "а", "но", "или", "что", "чтобы", "для", "к", "ко", "по", "с", "со", "у", "о", "об",
             "мне", "ему", "ей", "моей", "моему", "моим", "контакту", "сообщение", "письмо", "текст", "который", "которая",
             "этому", "этой", "про", "затем", "через", "до", "после", "из", "от", "за", "под", "над", "при", "без",
             "около", "если", "потому", "либо", "мой", "моя", "мое", "моё", "мои", "мою", "моих", "свой", "свою",
             "свои", "the", "to", "and"}
# «какую-нибудь» / «какой-то» wait for a noun; «что-нибудь», «кто-то» end a phrase («Расскажи что-нибудь.»).
_DANGLING_HYPHEN = re.compile(r"как(?:ой|ая|ое|ие|ую|ого|ому|им|их|ими)-(?:нибудь|то|либо)|"
                              r"ч(?:ей|ья|ьё|ье|ьи|ью)-(?:нибудь|то|либо)")
_VERB_ONLY = re.compile(r"(джарвис\s*)?(отправь|напиши|пошли|скажи|сообщи|передай|запланируй|напомни|открой|найди)"
                        r"(\s+(сообщение|письмо|мне|ему|ей|в|на))?")


def is_fragment(text: str) -> bool:
    """A phrase that obviously continues: a bare command verb, or ending on a preposition/conjunction/«моей»/«какую-то».
    Measured 2026-10-24: «Джарвис, расскажи что-нибудь.» was split at the hyphen, ended on «нибудь» and was held as
    cut off — JARVIS said nothing, three times in a row."""
    low = text.lower().replace("ё", "е")
    t = re.sub(r"[^\w\s:-]", " ", low)
    t = re.sub(r"(?<![\w])-|-(?![\w])", " ", t).strip()          # dashes, but not the hyphen inside a word
    if not t or len(t) > 1200:
        return False
    if text.rstrip().endswith((":", ",", "—", "-")):
        return True
    if text.rstrip().endswith("?"):
        return False                                             # a question is a whole phrase
    if _VERB_ONLY.fullmatch(re.sub(r"\s+", " ", t)):
        return True
    words = t.split()
    if not words:
        return False
    if re.search(r"(?:^|\s)(?:а|и|но)\s+(?:потом|затем|далее|дальше|ещ[её]|также|после этого)$", t):
        return True                                              # «Открой браузер, а потом…»
    if "-" in words[-1]:
        return bool(_DANGLING_HYPHEN.fullmatch(words[-1]))
    return words[-1] in _DANGLING


def unfinished(text: str, p: float | None) -> bool:
    """The voice clearly trails off mid-thought (Smart Turn), though the words do not show it: «Первое, найди папку
    колледж. [pause] Далее…». A question is never held."""
    t = (text or "").strip()
    return p is not None and p < TURN_HOLD and len(t.split()) >= 2 and not t.endswith("?")


_SEND_VERB = re.compile(r"^(джарвис[\s,]*)?(отправь|напиши|пошли|скажи|сообщи|передай|черкни|ответь)\b", re.I)


def phrase_complete(text: str) -> bool:
    """Is a phrase clearly finished after a short pause? Only then the window answers without the full wait.
    Conservative: cut-off phrases, a bare wake word and message commands without the text keep waiting."""
    from .comms.agent import local_parse
    from .router import strip_wake
    t = (text or "").strip()
    if not t or is_fragment(t) or not re.search(r"\w", strip_wake(t) or ""):
        return False
    if not t.endswith((".", "?", "!", "…")):
        return False
    if _SEND_VERB.match(t) and ":" not in t and not local_parse(t):
        return False                       # «Отправь маме в Телеграм» — the message itself may follow
    return True


COMMS_UI = {"comms.state", "comms.telegram.begin", "comms.telegram.finish", "comms.telegram.logout",
            "comms.email.connect", "comms.email.logout", "comms.whatsapp.link", "comms.whatsapp.logout",
            "comms.cancel", "comms.revise", "comms.rule.revoke", "comms.contact.save", "comms.contact.delete",
            "comms.announce", "comms.jobs"}


async def _comms_action(server, typ: str, data: dict) -> dict:
    """Account connection is always user-driven from the window. Passwords, codes and sessions pass through here
    to the provider / OS keyring only; they are never audited, logged or shown to any AI model."""
    core, d = server.core, server.core.director
    hub = core.comms
    if typ not in COMMS_UI:
        raise ValueError("Неизвестный запрос связи")
    tg, wa, em = hub.providers.get("telegram"), hub.providers.get("whatsapp"), hub.providers.get("email")
    if typ == "comms.telegram.begin":
        res = await tg.begin_login(str(data.get("api_id", "")), str(data.get("api_hash", "")), str(data.get("phone", "")))
        core.audit.write("comms.telegram_code_requested")
    elif typ == "comms.telegram.finish":
        res = await tg.finish_login(data.get("code"), data.get("password"))
        if res.get("stage") == "done":
            core.audit.write("comms.account_connected", channel="telegram")
    elif typ == "comms.telegram.logout":
        await tg.logout()
        core.audit.write("comms.account_disconnected", channel="telegram")
        res = {}
    elif typ == "comms.email.connect":
        res = await em.connect(str(data.get("address", "")), str(data.get("password", "")),
                               data.get("smtp_host") or None, int(data["smtp_port"]) if data.get("smtp_port") else None,
                               data.get("imap_host"))
        core.audit.write("comms.account_connected", channel="email")
    elif typ == "comms.email.logout":
        await em.logout()
        core.audit.write("comms.account_disconnected", channel="email")
        res = {}
    elif typ == "comms.whatsapp.link":
        res = await wa.begin_link()
        core.audit.write("comms.whatsapp_link_started")
    elif typ == "comms.whatsapp.logout":
        await wa.logout()
        core.audit.write("comms.account_disconnected", channel="whatsapp")
        res = {}
    elif typ == "comms.cancel":
        res = {"task_id": await d.run_ui_tool("message_cancel", {"job_ids": [str(data.get("job_id"))]})}
    elif typ == "comms.revise":
        await d.comms.revise(d, str(data.get("approval_id")), data.get("body"), data.get("subject"))
        res = {}
    elif typ == "comms.rule.revoke":
        res = {"revoked": hub.revoke_rule(str(data.get("id")))}
    elif typ == "comms.contact.save":
        res = {"contact": hub.book.save(str(data.get("name", "")), data.get("email") or None, data.get("phone") or None,
                                        data.get("telegram") or None)}
    elif typ == "comms.contact.delete":
        res = {"deleted": hub.book.delete(str(data.get("id")))}
    elif typ == "comms.announce":
        core.db.set_kv("comms_announce", bool(data.get("on")))
        res = {}
    elif typ == "comms.jobs":
        res = {"jobs": hub.outbox.list(limit=int(data.get("limit", 100)))}
    else:   # comms.state
        res = {}
    hub.publish_status()
    return {**res, "providers": hub.describe(), "rules": hub.rules(), "contacts": hub.book.all(),
            "announce": bool(core.db.get_kv("comms_announce", True))}


async def audio_output(fix: bool = False) -> dict:
    """State of JARVIS's own playback stream in PipeWire (application.id=jarvis-workos) and the default sink.
    With fix=True raises only JARVIS's stream to 100 % and unmutes it — other applications are not touched."""
    import json as _json
    import shutil as _sh
    if not _sh.which("pactl"):
        return {"available": False}
    async def run(*a):
        p = await asyncio.create_subprocess_exec("pactl", *a, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 5)
        return out.decode("utf-8", "replace")
    try:
        inputs = _json.loads(await run("-f", "json", "list", "sink-inputs") or "[]")
        sinks = _json.loads(await run("-f", "json", "list", "sinks") or "[]")
        default = (await run("get-default-sink")).strip()
    except (OSError, ValueError, asyncio.TimeoutError):
        return {"available": False}
    mine = [i for i in inputs if (i.get("properties") or {}).get("application.id") == "jarvis-workos"]
    def pct(vol):
        try:
            return round(max(int(str(v.get("value_percent", "0")).rstrip("%")) for v in (vol or {}).values()))
        except ValueError:
            return None
    sink = next((s for s in sinks if s.get("name") == default), None)
    res = {"available": True, "stream_found": bool(mine),
           "volume_pct": pct(mine[0].get("volume")) if mine else None, "muted": bool(mine and mine[0].get("mute")),
           "sink": (sink or {}).get("description"), "sink_volume_pct": pct((sink or {}).get("volume")),
           "sink_muted": bool(sink and sink.get("mute"))}
    if fix and mine:
        for i in mine:
            await run("set-sink-input-volume", str(i["index"]), "100%")
            await run("set-sink-input-mute", str(i["index"]), "0")
        res.update(volume_pct=100, muted=False, fixed=True)
    return res


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
    web.run_app(srv.app, host=host, port=srv.port, print=None, handle_signals=True, access_log=None,
                shutdown_timeout=5)
