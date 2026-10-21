"""Tool contracts. Every action JARVIS can take is a Tool with a typed spec.

The Gateway (gateway.py) is the only code path that runs tools; it validates
input, consults the policy engine, handles approval, timeouts, cancellation,
verification and audit.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable


class Risk(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    FORBIDDEN = "forbidden"


class ToolInputError(ValueError):
    pass


class ToolError(RuntimeError):
    pass


@dataclass
class ToolSpec:
    id: str
    title: str                 # short Russian label for the UI
    description: str           # what the model sees
    params: dict               # JSON schema (object)
    risk: Risk
    side_effects: str          # "none" | "local" | "external"
    timeout_s: float = 10.0
    cancellable: bool = True
    idempotent: bool = True
    verification: str = ""     # how the postcondition is checked
    model_callable: bool = True
    impact: str = ""           # what changes outside JARVIS, shown in approval cards


@dataclass
class ToolResult:
    data: dict
    summary: str                         # one Russian sentence for the user
    card: str | None = None              # UI card type
    artifacts: list[str] = field(default_factory=list)
    untrusted: bool = False              # contains external/file content


@dataclass
class Verification:
    status: str          # verified | unverified | failed
    detail: str

    def as_dict(self) -> dict:
        return {"status": self.status, "detail": self.detail}


@dataclass
class ToolContext:
    config: Any
    task_id: str
    progress: Callable[[str, float | None], None] = lambda msg, frac=None: None
    cancelled: Callable[[], bool] = lambda: False
    conversation: dict = field(default_factory=dict)   # short-lived references (last results etc.)
    payload: dict | None = None    # in-memory only (e.g. prompt context); never persisted or audited
    step_id: str | None = None


class Tool:
    spec: ToolSpec

    def validate(self, args: dict, ctx: ToolContext) -> dict:
        return validate_schema(self.spec.params, args)

    def describe(self, args: dict) -> str:
        """Human description of the exact action, used in approval cards."""
        return self.spec.title

    def paths(self, args: dict) -> list[tuple[Path, str]]:
        """Paths this call touches, as (path, 'read'|'write'), for scope checks."""
        return []

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    async def verify(self, args: dict, result: ToolResult, ctx: ToolContext) -> Verification:
        return Verification("unverified", "Проверка результата для этого действия не предусмотрена")


# ---------------------------------------------------------------------------
# Minimal JSON-schema validation (the subset our tool specs use).

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}


def validate_schema(schema: dict, args: Any, where: str = "args") -> dict:
    if not isinstance(args, dict):
        raise ToolInputError(f"{where}: ожидался объект")
    props = schema.get("properties", {})
    out: dict = {}
    for k in args:
        if k not in props:
            raise ToolInputError(f"{where}: неизвестный параметр «{k}»")
    for name in schema.get("required", []):
        if name not in args or args[name] is None:
            raise ToolInputError(f"{where}: не указан параметр «{name}»")
    for name, ps in props.items():
        if name not in args or args[name] is None:
            if "default" in ps:
                out[name] = ps["default"]
            continue
        v = args[name]
        t = ps.get("type")
        if t == "integer" and isinstance(v, float) and v.is_integer():
            v = int(v)
        if t == "number" and isinstance(v, bool) or t == "integer" and isinstance(v, bool):
            raise ToolInputError(f"{name}: неверный тип")
        if t and not isinstance(v, _TYPES[t]):
            raise ToolInputError(f"{name}: ожидался тип {t}")
        if "enum" in ps and v not in ps["enum"]:
            raise ToolInputError(f"{name}: допустимо {ps['enum']}")
        if isinstance(v, str):
            if len(v) > ps.get("maxLength", 10_000):
                raise ToolInputError(f"{name}: слишком длинное значение")
            if len(v) < ps.get("minLength", 0):
                raise ToolInputError(f"{name}: слишком короткое значение")
            if "\x00" in v:
                raise ToolInputError(f"{name}: недопустимые символы")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if "minimum" in ps and v < ps["minimum"] or "maximum" in ps and v > ps["maximum"]:
                raise ToolInputError(f"{name}: значение вне диапазона {ps.get('minimum')}…{ps.get('maximum')}")
        out[name] = v
    return out


# ---------------------------------------------------------------------------
# Subprocess helper: argv only (never a shell string), bounded output,
# timeout, and kill-on-cancel of the whole process group.

@dataclass
class CmdResult:
    argv: list[str]
    code: int
    stdout: str
    stderr: str
    ms: float


async def run_cmd(argv: list[str], timeout: float = 10.0, max_out: int = 200_000,
                  env: dict | None = None, detach: bool = False) -> CmdResult:
    t0 = time.perf_counter()
    safe_env = {k: v for k, v in os.environ.items() if not _secretish(k)}
    if env:
        safe_env.update(env)
    if detach:
        # Launch a GUI app: new session so it outlives JARVIS; no output capture.
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL,
                                                    env=safe_env, start_new_session=True)
        return CmdResult(argv, 0, "", "", (time.perf_counter() - t0) * 1000)
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                env=safe_env, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        if isinstance(e, asyncio.TimeoutError):
            raise ToolError(f"Команда не завершилась за {timeout:.0f} с: {argv[0]}") from None
        raise
    return CmdResult(argv, proc.returncode or 0, out[:max_out].decode("utf-8", "replace"),
                     err[:max_out].decode("utf-8", "replace"), (time.perf_counter() - t0) * 1000)


def _secretish(name: str) -> bool:
    n = name.upper()
    return any(s in n for s in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))


# ---------------------------------------------------------------------------
# Path scoping.

# Never readable, whatever the access level: keys, passwords, browser profiles (cookies), JARVIS's own sessions.
SECRET_PATHS = [".ssh", ".gnupg", ".password-store", ".local/share/keyrings", ".config/chromium", ".config/google-chrome",
                ".config/BraveSoftware", ".mozilla", ".local/share/jarvis", ".local/state/jarvis", ".aws", ".kube",
                ".docker", ".netrc", ".git-credentials", ".config/gh", ".pki", ".config/Signal"]


def is_secret_path(p: Path) -> bool:
    home = Path.home().resolve()
    if not p.is_relative_to(home):
        return False
    rel = p.relative_to(home).as_posix()
    if any(rel == s or rel.startswith(s + "/") for s in SECRET_PATHS):
        return True
    return p.name in (".env", ".envrc") or p.suffix in (".pem", ".key", ".kdbx")


def resolve_in_scopes(raw: str, roots: list[Path]) -> Path:
    """Resolve a user/model-supplied path and require it to live inside one of
    the allowed roots (symlinks resolved, so ../ and link escapes are rejected)."""
    if not raw or "\x00" in raw:
        raise ToolInputError("Пустой или некорректный путь")
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        raise ToolInputError("Нужен полный путь")
    rp = p.resolve()
    if is_secret_path(rp):
        raise ToolInputError("Эта папка закрыта для JARVIS: там ключи, пароли или данные входа")
    for root in roots:
        if rp == root or rp.is_relative_to(root):
            return rp
    raise ToolInputError("Путь вне разрешённых папок: " + str(p))
