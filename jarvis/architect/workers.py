"""Long-lived worker processes for the heavy engines: FreeCAD (CAD + OCCT), Blender (look, furniture, renders,
exports) and the drawing worker (OpenCV, pdfium, ezdxf). Each keeps its program loaded between requests (FreeCAD
~0.8 s and Blender ~1 s to start, measured 2026-10-26), so an edit costs only the work itself.

Protocol: one JSON request per stdin line {"id", "op", "args"}, replies {"id", "ok", "result" | "error"}, and
{"id", "progress": "…"} lines while working. A worker that dies is started again and the request retried once;
the reason (missing program, import error, crash) is kept for the person in plain words. Idle workers stop after
IDLE_S to give the memory back (Blender ~300 MB)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent / "workers"
DATA = Path(os.path.expanduser("~/.local/share/jarvis/architect"))
IDLE_S = 600.0


class WorkerError(RuntimeError):
    """A worker could not do the request (the text is for the person)."""


@dataclass
class EngineInfo:
    id: str
    title: str
    available: bool
    version: str = ""
    path: str = ""
    detail: str = ""
    python: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def system_python() -> str | None:
    """The system's own Python (FreeCAD's modules and PySide6 are built for it) — never JARVIS's venv, which comes
    first in PATH when the core runs under `uv run` (measured: «No module named 'PySide6'»)."""
    for p in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if Path(p).exists():
            return p
    venv = os.environ.get("VIRTUAL_ENV")
    path = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep) if not (venv and d.startswith(venv)))
    return shutil.which("python3", path=path)


def freecad_python() -> tuple[list[str] | None, str]:
    """How to run Python with FreeCAD's modules: the system Python with FreeCAD's lib folder on the path (Arch's
    freecad package is built for it), else freecadcmd. (argv prefix, why not)"""
    for lib in ("/usr/lib/freecad/lib", "/usr/lib/freecad-python3/lib", "/usr/local/lib/freecad/lib"):
        if Path(lib).is_dir() and any(Path(lib).glob("FreeCAD*.so")):
            py = system_python()
            if py:
                return [py, "-u", "-s"], lib
    exe = shutil.which("freecadcmd") or shutil.which("FreeCADCmd")
    if exe:
        return [exe], ""
    return None, "FreeCAD не установлен (pacman: freecad)"


def blender_exe() -> str | None:
    return shutil.which("blender")


def cv_python() -> str | None:
    p = DATA / ".venv" / "bin" / "python"
    return str(p) if p.exists() else None


class Worker:
    def __init__(self, name: str, title: str, argv_fn, env: dict | None = None, ready_timeout: float = 60.0):
        self.name, self.title = name, title
        self.argv_fn = argv_fn                 # () -> argv (or raises WorkerError with the reason)
        self.env = env or {}
        self.ready_timeout = ready_timeout
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.seq = 0
        self.last_used = 0.0
        self.last_error = ""
        self.info: dict = {}
        self.restarts = 0
        self.stderr_tail: list[str] = []
        self._idle: asyncio.Task | None = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        if self.running():
            return
        argv = self.argv_fn()
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))
               and k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "UV_RUN_RECURSION_DEPTH")}
        venv = os.environ.get("VIRTUAL_ENV")
        if venv:          # the core's venv must not leak into the engines' Pythons
            env["PATH"] = os.pathsep.join(d for d in env.get("PATH", "").split(os.pathsep) if not d.startswith(venv))
        env.update(self.env)
        env["PYTHONUNBUFFERED"] = "1"
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env, start_new_session=True, limit=64 * 1024 * 1024)
        except OSError as e:
            raise WorkerError(f"{self.title} не запустился: {e}") from None
        self.stderr_tail = []
        asyncio.get_running_loop().create_task(self._drain_stderr(self.proc))
        try:
            line = await asyncio.wait_for(self.proc.stdout.readline(), self.ready_timeout)
        except asyncio.TimeoutError:
            await self.stop()
            raise WorkerError(f"{self.title} не ответил за {self.ready_timeout:.0f} с после запуска") from None
        try:
            hello = json.loads(line or b"{}")
        except ValueError:
            hello = {}
        if not hello.get("ready"):
            await asyncio.sleep(0.2)
            why = self._why()
            await self.stop()
            raise WorkerError(f"{self.title} не запустился: {why}")
        self.info = hello
        self.last_used = time.time()
        if self._idle is None or self._idle.done():
            self._idle = asyncio.get_running_loop().create_task(self._idle_watch())

    def _why(self) -> str:
        tail = " ".join(self.stderr_tail[-6:])
        for key, text in (("ModuleNotFoundError", "не хватает модуля Python"), ("ImportError", "ошибка импорта"),
                          ("cannot open shared object", "не найдена системная библиотека"),
                          ("Segmentation fault", "аварийное завершение"), ("MemoryError", "не хватило памяти")):
            if key in tail:
                return f"{text} ({tail[-240:]})"
        return tail[-300:] or "нет ответа"

    async def _drain_stderr(self, proc) -> None:
        try:
            async for raw in proc.stderr:
                s = raw.decode("utf-8", "replace").strip()
                if s:
                    self.stderr_tail.append(s[:400])
                    if len(self.stderr_tail) > 40:
                        self.stderr_tail = self.stderr_tail[-30:]
        except (asyncio.CancelledError, ValueError):
            pass

    async def _idle_watch(self) -> None:
        while self.running():
            await asyncio.sleep(30)
            if time.time() - self.last_used > IDLE_S and not self.lock.locked():
                await self.stop()

    async def stop(self) -> None:
        p, self.proc = self.proc, None
        if p and p.returncode is None:
            try:
                p.stdin.close()
            except (OSError, AttributeError):
                pass
            try:
                await asyncio.wait_for(p.wait(), 3)
            except asyncio.TimeoutError:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                await p.wait()

    async def call(self, op: str, args: dict | None = None, timeout: float = 300.0, progress=None) -> dict:
        """One request; a crash or a dead pipe → restart and one retry. Errors from the work itself are not retried."""
        async with self.lock:
            for attempt in (1, 2):
                try:
                    await self.start()
                    return await self._call(op, args or {}, timeout, progress)
                except (BrokenPipeError, ConnectionResetError, EOFError, _Died) as e:
                    self.last_error = f"{self.title} аварийно завершился: {self._why() or e}"
                    self.restarts += 1
                    await self.stop()
                    if attempt == 2:
                        raise WorkerError(self.last_error) from None
                except asyncio.TimeoutError:
                    await self.stop()
                    self.last_error = f"{self.title} не уложился в {timeout:.0f} с ({op})"
                    raise WorkerError(self.last_error) from None
            raise WorkerError(self.last_error)

    async def _call(self, op: str, args: dict, timeout: float, progress) -> dict:
        self.seq += 1
        rid = self.seq
        self.last_used = time.time()
        self.proc.stdin.write((json.dumps({"id": rid, "op": op, "args": args}, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise asyncio.TimeoutError
            line = await asyncio.wait_for(self.proc.stdout.readline(), left)
            if not line:
                raise _Died()
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") != rid:
                continue
            if "progress" in msg:
                if progress:
                    progress(str(msg["progress"])[:200])
                continue
            self.last_used = time.time()
            if msg.get("ok"):
                return msg.get("result") or {}
            self.last_error = str(msg.get("error") or "ошибка")[:600]
            raise WorkerError(f"{self.title}: {self.last_error}")


class _Died(Exception):
    pass


def make_workers() -> dict[str, Worker]:
    def fc_argv():
        pre, lib = freecad_python()
        if pre is None:
            raise WorkerError(lib)
        return pre + [str(HERE / "freecad_worker.py")] + ([lib] if lib else [])

    def bl_argv():
        exe = blender_exe()
        if not exe:
            raise WorkerError("Blender не установлен (pacman: blender)")
        return [exe, "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(HERE / "blender_worker.py")]

    def cv_argv():
        py = cv_python()
        if not py:
            raise WorkerError("Нет окружения распознавания чертежей (~/.local/share/jarvis/architect/.venv — "
                              "scripts/install.sh его создаёт)")
        return [py, "-u", "-I", str(HERE / "cv_worker.py")]

    return {"freecad": Worker("freecad", "FreeCAD", fc_argv, ready_timeout=60),
            "blender": Worker("blender", "Blender", bl_argv, ready_timeout=90),
            "cv": Worker("cv", "Распознавание чертежей", cv_argv, ready_timeout=60)}


def engines() -> list[EngineInfo]:
    """What is installed on this computer (checked on disk, no process started)."""
    out = []
    pre, lib = freecad_python()
    fc_ver = ""
    try:
        import subprocess
        r = subprocess.run(["pacman", "-Q", "freecad", "opencascade", "blender"], capture_output=True, text=True, timeout=5)
        vers = dict(line.split()[:2] for line in r.stdout.splitlines() if len(line.split()) >= 2)
    except (OSError, ValueError, Exception):    # noqa: BLE001 — not Arch: versions come from the workers' ping
        vers = {}
    fc_ver = vers.get("freecad", "")
    out.append(EngineInfo("freecad", "FreeCAD (BIM / Arch)", pre is not None, fc_ver, lib or (pre or [""])[0],
                          "" if pre else lib, python=(pre or [""])[0]))
    out.append(EngineInfo("occt", "Open CASCADE (геометрическое ядро FreeCAD)", pre is not None,
                          vers.get("opencascade", ""), "/usr/lib/libTKernel.so" if Path("/usr/lib/libTKernel.so").exists() else "",
                          "" if pre else "идёт вместе с FreeCAD"))
    be = blender_exe()
    out.append(EngineInfo("blender", "Blender", bool(be), vers.get("blender", ""), be or "",
                          "" if be else "Blender не установлен (pacman: blender)"))
    cv = cv_python()
    out.append(EngineInfo("cv", "Распознавание чертежей (OpenCV, PDF, DXF)", bool(cv), "", cv or "",
                          "" if cv else "нет окружения ~/.local/share/jarvis/architect/.venv"))
    tess = shutil.which("tesseract")
    out.append(EngineInfo("ocr", "Локальное распознавание текста (tesseract)", bool(tess), "", tess or "",
                          "" if tess else "tesseract не установлен"))
    pascal = shutil.which("pascal-editor") or shutil.which("pascal")
    out.append(EngineInfo("pascal", "Pascal Editor", bool(pascal), "", pascal or "",
                          "" if pascal else "не установлен на этом компьютере — не используется"))
    out.append(EngineInfo("vision", "Claude Vision (через Claude Code)", bool(shutil.which("claude")), "",
                          shutil.which("claude") or "", "" if shutil.which("claude") else "Claude Code не установлен"))
    return out
