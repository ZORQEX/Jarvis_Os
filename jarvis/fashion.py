"""«Заказы»: the fashion-orders app (fashion/, a git subtree of morph-rq/alina-fashion-ai) as a JARVIS module.

JARVIS runs the app as its own process — own venv (~/.local/share/jarvis/fashion/.venv, scripts/install.sh) and own
data (<JARVIS data>/fashion: database, factory files, photos, documents, backups) — on 127.0.0.1:<fashion_port>,
shows it in the window's «Заказы» workspace, and hands spoken or typed requests about orders to the app's assistant:
its own rules answer in ~0.2 s, anything else goes to Claude through the local Claude Code CLI (the user's
subscription) with the app's typed tools. Nothing is saved without the owner's «Подтвердить» (or a spoken «да» for
the changes the app allows by voice). JARVIS speaks the app's short answer with its own local voice.

Which phrases are about orders is decided by the app (``/api/jarvis/claims``): it knows the customer codes, brands
and seasons in its database and how they sound when spoken («ай ди эс» → IDS).
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import signal
import subprocess
import time
from pathlib import Path

import aiohttp

from .tools.base import Risk, Tool, ToolInputError, ToolResult, ToolSpec, Verification

REPO = Path(__file__).resolve().parents[1]
CODE = REPO / "fashion"
RUNTIME = Path.home() / ".local/share/jarvis/fashion/.venv/bin/python"
FILE_TYPES = {".xlsx", ".xlsm", ".xls", ".pdf"}


class FashionService:
    """The app's process (start, health, restart after a crash, stop) and its HTTP API, for the director."""

    def __init__(self, config, paths, db=None, audit=None, runtime: Path | None = None, code: Path | None = None):
        self.config, self.paths, self.db, self.audit = config, paths, db, audit
        self.runtime, self.code = runtime or RUNTIME, code or CODE
        self.port = int(getattr(config, "fashion_port", 8797) or 8797)
        self.home = paths.data / "fashion"
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self.stopping = False
        self._watch: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.last_error = ""

    # ------------------------------------------------------------------ process
    def installed(self) -> bool:
        return self.runtime.exists() and (self.code / "app" / "main.py").exists()

    def enabled(self) -> bool:
        return bool(getattr(self.config, "fashion_enabled", True)) and self.installed()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("FASHION_")}
        env.update({"PYTHONPATH": str(self.code), "FASHION_HOME": str(self.home),
                    "FASHION_AI_PROVIDER": "claude-code", "FASHION_PUBLIC_BASE_URL": self.url,
                    "FASHION_JARVIS_ORIGIN": f"http://127.0.0.1:{self.config.port}",
                    "FASHION_CLAUDE_MODEL": str(getattr(self.config, "fashion_claude_model", "") or "sonnet"),
                    "FASHION_LANG": "ru",
                    "PYTHONUNBUFFERED": "1"})
        return env

    @property
    def pidfile(self) -> Path:
        return self.paths.state / "fashion.pid"

    def _kill_stale(self) -> None:
        """A copy left running by a JARVIS that crashed would hold the port and serve old code."""
        try:
            pid = int(self.pidfile.read_text().strip())
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
        except (OSError, ValueError):
            return
        if "app.main:app" in cmd and f"--port\0{self.port}" in cmd:
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            for _ in range(30):
                if not Path(f"/proc/{pid}").exists():
                    break
                time.sleep(0.1)

    async def start(self) -> bool:
        async with self._lock:
            if not self.enabled():
                return False
            if self.running() and await self.healthy():
                return True
            self.stopping = False
            self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
            await asyncio.to_thread(self._kill_stale)
            log = open(self.paths.state / "fashion.log", "ab")
            argv = [str(self.runtime), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                    "--port", str(self.port), "--no-access-log"]
            self.proc = subprocess.Popen(argv, cwd=str(self.home), env=self.env(), stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            log.close()
            self.pidfile.write_text(str(self.proc.pid))
            self.started_at = time.time()
            for _ in range(150):                         # measured: ~2–4 s on this laptop
                if self.proc.poll() is not None:
                    break
                if await self.healthy():
                    if self.audit:
                        self.audit.write("fashion.started", port=self.port, s=round(time.time() - self.started_at, 1))
                    if self._watch is None or self._watch.done():
                        self._watch = asyncio.create_task(self._watchdog(), name="fashion-watchdog")
                    return True
                await asyncio.sleep(0.2)
            self.last_error = "Приложение заказов не запустилось — подробности в fashion.log"
            if self.audit:
                self.audit.write("fashion.start_failed", code=self.proc.poll())
            return False

    async def _watchdog(self) -> None:
        """Restart after a crash (not after stop()), at most once a minute."""
        while not self.stopping:
            await asyncio.sleep(2)
            if self.stopping or self.proc is None or self.proc.poll() is None:
                continue
            if self.audit:
                self.audit.write("fashion.crashed", code=self.proc.poll())
            if time.time() - self.started_at < 60:
                await asyncio.sleep(60)
            if not self.stopping:
                await self.start()

    async def stop(self) -> None:
        self.stopping = True
        if self._watch:
            self._watch.cancel()
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            for _ in range(50):
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
        self.pidfile.unlink(missing_ok=True)

    # ------------------------------------------------------------------ HTTP
    async def _call(self, method: str, path: str, timeout: float, **kw) -> dict:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.request(method, self.url + path, **kw) as r:
                if r.status >= 400:
                    raise RuntimeError(f"приложение заказов ответило {r.status}")
                return await r.json(content_type=None)

    async def healthy(self) -> bool:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.5)) as s:
                async with s.get(self.url + "/health") as r:
                    return r.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False

    async def state(self) -> dict:
        out = {"installed": self.installed(), "enabled": self.enabled(), "running": self.running(),
               "url": self.url, "conversation": self.conversation(), "error": self.last_error}
        if self.running():
            try:
                out.update(await self._call("GET", "/api/jarvis/state", 3))
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError, ValueError):
                out["running"] = False
        return out

    def conversation(self) -> str:
        """The dialog the «Заказы» workspace shows; voice turns join it, so the screen and the voice are one talk."""
        c = str(self.db.get_kv("fashion_conversation") or "") if self.db else ""
        if not re.fullmatch(r"[a-f0-9]{16}", c):
            c = secrets.token_hex(8)
            if self.db:
                self.db.set_kv("fashion_conversation", c)
        return c

    def set_conversation(self, c: str) -> None:
        if re.fullmatch(r"[a-f0-9]{16}", c or "") and self.db:
            self.db.set_kv("fashion_conversation", c)

    async def claims(self, text: str) -> dict | None:
        """Is this phrase about orders? None when the app is off or slow (JARVIS then handles it as usual)."""
        if not self.running():
            return None
        try:
            return await self._call("POST", "/api/jarvis/claims", 1.5,
                                    json={"text": text, "conversation": self.conversation()})
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError, ValueError):
            return None

    async def ask(self, text: str, channel: str = "text", raw: str | None = None) -> dict:
        if not self.running() and not await self.start():
            raise RuntimeError(self.last_error or "приложение заказов не установлено")
        body = {"conversation": self.conversation(), "text": text[:6000], "channel": channel,
                "screen": {"area": "jarvis", "path": "/jarvis", "title": "Голос через JARVIS"}}
        if raw and raw != text:
            body["raw"] = raw[:6000]
        return await self._call("POST", "/api/agent/message", 600, json=body)

    async def attach(self, path: Path) -> dict:
        """A factory file from the computer, read like a file dropped onto the assistant bar (nothing is saved)."""
        if not self.running() and not await self.start():
            raise RuntimeError(self.last_error or "приложение заказов не установлено")
        conv = self.conversation()
        form = aiohttp.FormData()
        form.add_field("conversation", conv)
        form.add_field("files", path.read_bytes(), filename=path.name)
        up = await self._call("POST", "/api/agent/attach", 120, data=form, headers={"Accept": "application/json"})
        tokens = up.get("tokens") or ([up["token"]] if up.get("token") else [])
        if not tokens:
            raise RuntimeError(up.get("error") or "файл не принят")
        token = tokens[0]
        for _ in range(600):                              # analysis of a large workbook: up to ~2 min
            st = await self._call("GET", f"/factory-import/{token}/status", 10)
            if st.get("state") not in ("queued", "analysing"):
                break
            await asyncio.sleep(0.2)
        return await self._call("POST", "/api/agent/attachment", 300, json={"conversation": conv, "token": token})


class FashionAsk(Tool):
    """For JARVIS's own Claude: hand a request about orders to the orders assistant (when the phrase was not caught)."""

    def __init__(self, service: FashionService):
        self.service = service
        self.spec = ToolSpec(
            id="fashion_ask", title="Заказы: спросить ассистента",
            description=("The fashion-orders workspace «Заказы» (factory Excel/PDF files, customers like AA / IDS / SS, "
                         "brands, seasons, customer markups, factory and customer deposits/prepayments, payments, "
                         "statuses, customer Excel/PDF documents). Pass the user's request as said; its assistant "
                         "answers with exact figures from the ledger and prepares changes for the owner's confirmation "
                         "(nothing is saved by this tool). Relay its answer briefly."),
            params={"type": "object", "required": ["request"], "properties": {
                "request": {"type": "string", "minLength": 2, "maxLength": 4000}}},
            risk=Risk.LOW, side_effects="none", timeout_s=600, model_callable=True,
            verification="Ассистент заказов ответил")

    def describe(self, args):
        return f"Заказы: «{args['request'][:100]}»"

    async def run(self, args, ctx):
        out = await self.service.ask(args["request"], "text")
        if out.get("error") and not out.get("reply"):
            raise RuntimeError(str(out.get("reply") or "ассистент заказов не ответил"))
        return ToolResult({"reply": out.get("reply", ""), "proposal": out.get("proposal"),
                           "question": out.get("question")}, str(out.get("reply") or "")[:4000], untrusted=True)

    async def verify(self, args, result, ctx):
        return Verification("verified" if result.data.get("reply") else "failed", self.spec.verification)


class FashionAttach(Tool):
    """Put a factory Excel/PDF from the computer into «Заказы» (read and compared; saved only after «Подтвердить»)."""

    def __init__(self, service: FashionService, config):
        self.service, self.config = service, config
        self.spec = ToolSpec(
            id="fashion_attach", title="Заказы: загрузить файл фабрики",
            description=("Give a factory order file (Excel .xlsx/.xlsm/.xls or PDF) found on the computer to the «Заказы» "
                         "assistant: it reads sheets, photos, customers and totals, compares with the saved orders and "
                         "says what it found. Nothing enters the ledger until the owner confirms on screen. Find the "
                         "file first (search_files), pass its full path."),
            params={"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
            risk=Risk.LOW, side_effects="local", timeout_s=900, model_callable=True,
            verification="Файл прочитан приложением заказов")

    def validate(self, args, ctx):
        a = Tool.validate(self, args, ctx)
        p = self.config.expand(a["path"])
        if p.suffix.lower() not in FILE_TYPES:
            raise ToolInputError("Нужен файл Excel (.xlsx, .xlsm, .xls) или PDF")
        if not any(p.is_relative_to(r) for r in self.config.read_roots):
            raise ToolInputError("Этот файл вне папок, которые JARVIS может читать")
        if not p.is_file():
            raise ToolInputError(f"Файл не найден: {p}")
        a["path"] = str(p)
        return a

    def describe(self, args):
        return f"Загрузить в «Заказы»: {Path(args['path']).name}"

    async def run(self, args, ctx):
        out = await self.service.attach(Path(args["path"]))
        return ToolResult({"reply": out.get("reply", ""), "question": out.get("question")},
                          str(out.get("reply") or "Файл прочитан.")[:4000], untrusted=True)

    async def verify(self, args, result, ctx):
        return Verification("verified" if result.data.get("reply") else "failed", self.spec.verification)
