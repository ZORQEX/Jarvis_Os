"""Claude Code adapter — the coding / terminal / project-files specialist.

Runs the locally installed `claude` CLI headless (`-p`, stream-json) under the
user's existing Claude Code login. Always `--restricted` (no user/project
settings or hooks, file tools confined to the working directory), no MCP
servers, no session persistence, bounded turns, budget and time.

Note: this uses the person's own Claude Code sign-in on their own computer.
Distributing JARVIS to someone else requires their own Anthropic credentials.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile
import time

from ..db import Database
from .base import AIProvider, Cap, GenResult, Health, ProviderFailure, StepRequest, usage_of

INSPECT_TOOLS = ["Read", "Glob", "Grep"]
CHANGE_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]
# Never allowed, even after approval: privilege escalation, system packages, remote shells, publishing.
DENY = ["Bash(sudo:*)", "Bash(su:*)", "Bash(doas:*)", "Bash(pkexec:*)", "Bash(pacman:*)", "Bash(yay:*)",
        "Bash(paru:*)", "Bash(ssh:*)", "Bash(scp:*)", "Bash(git push:*)", "Bash(rm -rf /:*)", "Bash(rm -rf ~:*)",
        "Bash(chmod 777:*)", "Bash(systemctl enable:*)", "Bash(systemctl disable:*)"]
# Allowed only when the approved task asked for it.
GATED = {"install": ["Bash(pip install:*)", "Bash(uv add:*)", "Bash(uv pip install:*)", "Bash(npm install:*)",
                     "Bash(pnpm add:*)", "Bash(cargo add:*)"],
         "download": ["Bash(curl:*)", "Bash(wget:*)"]}

SYSTEM = ("Ты работаешь как исполнитель внутри ассистента JARVIS. Работай только в текущей папке. "
          "Не используй sudo и системные пакетные менеджеры. Устанавливать зависимости проекта и скачивать файлы можно, "
          "только если это явно разрешено для задачи; иначе остановись и напиши, что требуется. В конце дай краткий отчёт по-русски: что сделано, какие "
          "файлы изменены, какие проверки (тесты) запускались и с каким результатом.")


def _clean_env() -> dict:
    """Environment for the child: no parent Claude session variables, no API secrets."""
    out = {}
    for k, v in os.environ.items():
        u = k.upper()
        if u.startswith("CLAUDE") or any(s in u for s in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            continue
        out[k] = v
    return out


def build_argv(prompt: str, mode: str, model: str = "", budget: float = 2.0, max_turns: int = 30,
               allow: set[str] | None = None) -> list[str]:
    tools = CHANGE_TOOLS if mode == "change" else INSPECT_TOOLS
    argv = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--restricted",
            "--strict-mcp-config", "--no-session-persistence", "--permission-mode", "dontAsk",
            "--tools", ",".join(tools), "--allowedTools", *tools, "--max-turns", str(max_turns),
            "--max-budget-usd", f"{budget:.2f}", "--append-system-prompt", SYSTEM]
    if mode == "change":
        deny = list(DENY)
        for perm, rules in GATED.items():
            if perm not in (allow or set()):
                deny += rules
        argv += ["--disallowedTools", *deny]
    if model:
        argv += ["--model", model]
    return argv


def summarize_tool(name: str, inp: dict) -> str:
    if name in ("Read", "Edit", "Write"):
        return f"{name} {os.path.basename(str(inp.get('file_path', '')))}"
    if name == "Bash":
        return "Bash: " + str(inp.get("command", ""))[:80]
    if name in ("Glob", "Grep"):
        return f"{name} {str(inp.get('pattern', ''))[:60]}"
    return name


class ClaudeCodeProvider(AIProvider):
    id = "claude_code"
    display_name = "Claude Code"
    role_ru = "Компьютер · терминал · файлы · код · отладка · установка · система"
    capabilities = {Cap.CODE, Cap.TERMINAL, Cap.FILESYSTEM}
    modalities = {"text"}
    cloud = True     # the CLI sends the work to Anthropic

    def __init__(self, config, db: Database):
        self.config, self.db = config, db
        self.logged_in: bool | None = None
        self.account: str | None = None
        self.last_error: str | None = None
        self.last_error_kind: str | None = None
        self.checked_at: float | None = None

    def configured(self) -> bool:
        return shutil.which("claude") is not None

    @property
    def verified_at(self) -> float | None:
        return self.db.get_kv("claude_code_verified_at")

    def health(self) -> Health:
        if not self.configured():
            return Health.NOT_CONFIGURED
        if self.logged_in is False or self.last_error_kind == "auth":
            return Health.AUTH_ERROR
        if self.last_error_kind == "quota":
            return Health.RATE_LIMITED
        v = self.verified_at
        if v and time.time() - v < 7 * 86400 and self.logged_in is not False:
            return Health.CONNECTED
        return Health.DEGRADED   # installed, not verified by a live call yet

    async def auth_status(self) -> bool:
        try:
            p = await asyncio.create_subprocess_exec("claude", "auth", "status", "--json", env=_clean_env(),
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 20)
            d = json.loads(out or b"{}")
            self.logged_in = bool(d.get("loggedIn"))
            self.account = d.get("authMethod")
        except (OSError, ValueError, asyncio.TimeoutError):
            self.logged_in = None
        return bool(self.logged_in)

    async def health_check(self, live: bool = False) -> dict:
        self.checked_at = time.time()
        if not self.configured():
            return self.describe()
        if not await self.auth_status():
            self.last_error, self.last_error_kind = "Claude Code: вход не выполнен (claude auth login)", "auth"
            return self.describe()
        if live:
            with tempfile.TemporaryDirectory(prefix="jarvis-cc-") as d:
                try:
                    r = await self.generate(StepRequest(Cap.CODE, "Ответь одним словом: ГОТОВ", workdir=d))
                    if "ГОТОВ" in r.text.upper():
                        self.db.set_kv("claude_code_verified_at", time.time())
                        self.last_error = self.last_error_kind = None
                except ProviderFailure as e:
                    self.last_error, self.last_error_kind = str(e), e.kind
        return self.describe()

    def model_for(self, cap: Cap) -> str:
        return self.config.claude_model or "default"

    async def generate(self, req: StepRequest, on_delta=None, on_progress=None) -> GenResult:
        if not self.configured():
            raise ProviderFailure("unavailable", "Claude Code не установлен")
        if not req.workdir or not os.path.isdir(req.workdir):
            raise ProviderFailure("invalid", "Не задана рабочая папка для Claude Code")
        from .claude_chat import context_block
        prompt = (context_block(req.context) + "\n\n" if req.context else "") + req.instruction
        argv = build_argv(prompt, req.mode, self.config.claude_model, self.config.claude_budget_usd,
                          allow=set(getattr(req, "permissions", []) or []))
        t0 = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(*argv, cwd=req.workdir, env=_clean_env(),
                                                    stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                    start_new_session=True, limit=8 * 1024 * 1024)
        model, result, tools_used, first = "default", None, [], None
        try:
            async def read():
                nonlocal model, result, first
                async for raw in proc.stdout:
                    try:
                        d = json.loads(raw)
                    except ValueError:
                        continue
                    t = d.get("type")
                    if t == "system" and d.get("subtype") == "init":
                        model = d.get("model") or model
                    elif t == "assistant":
                        for c in d.get("message", {}).get("content", []):
                            if c.get("type") == "tool_use":
                                s = summarize_tool(c.get("name", ""), c.get("input") or {})
                                tools_used.append(s)
                                if on_progress:
                                    on_progress("Claude Code: " + s)
                            elif c.get("type") == "text" and c.get("text"):
                                first = first or (time.perf_counter() - t0) * 1000
                    elif t == "result":
                        result = d
            await asyncio.wait_for(read(), self.config.claude_timeout_s)
            err = (await proc.stderr.read()).decode("utf-8", "replace")[:500]
            await proc.wait()
        except (asyncio.CancelledError, asyncio.TimeoutError) as e:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            if isinstance(e, asyncio.TimeoutError):
                raise ProviderFailure("network", f"Claude Code не уложился в {self.config.claude_timeout_s:.0f} с") from None
            raise
        if result is None or result.get("is_error") or proc.returncode not in (0, None):
            msg = (result or {}).get("result") or err or f"код выхода {proc.returncode}"
            low = msg.casefold()
            kind = "auth" if ("login" in low or "auth" in low) else "quota" if ("limit" in low or "budget" in low) \
                else "invalid"
            self.last_error, self.last_error_kind = "Claude Code: " + msg[:300], kind
            raise ProviderFailure(kind, "Claude Code: " + msg[:300])
        self.last_error = self.last_error_kind = None
        self.db.set_kv("claude_code_verified_at", time.time())
        usage = result.get("usage") or {}
        return GenResult(result.get("result", "").strip(), "claude_code", model, [],
                         usage_of(usage),
                         result.get("total_cost_usd"), first, (time.perf_counter() - t0) * 1000,
                         {"turns": result.get("num_turns"), "tools_used": tools_used, "mode": req.mode,
                          "permission_denials": result.get("permission_denials") or []})

    def describe(self) -> dict:
        return {"configured": self.configured(), "key_settable": False,
                "auth": "Вход Claude Code на этом компьютере (claude auth)" + (f" · {self.account}" if self.account else ""),
                "logged_in": self.logged_in, "verified_at": self.verified_at, "last_error": self.last_error,
                "checked_at": self.checked_at,
                "limits": {"budget_usd": self.config.claude_budget_usd, "timeout_s": self.config.claude_timeout_s,
                           "workspaces": self.config.code_workspaces}}
