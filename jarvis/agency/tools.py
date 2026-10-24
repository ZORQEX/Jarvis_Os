"""Gateway tools for AI steps. Running provider work as tools means the central
policy, approvals, emergency stop, timeouts, cancellation and audit apply to
every specialist exactly as to local actions.

Persisted args are metadata only (provider, capability, instruction, labels of
the context, sizes). The prompt context itself travels in ctx.payload."""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path

from ..providers.base import Cap, ProviderFailure
from ..config import limit
from ..tools.base import (Risk, Tool, ToolContext, ToolInputError, ToolResult, ToolSpec, Verification,
                          run_cmd)
from .verifier import verify_step

PROVIDER_NAMES = {"claude": "Claude", "claude_code": "Claude Code"}
PERM_RU = {"install": "установка зависимостей и инструментов", "download": "скачивание файлов из интернета",
           "browser": "управление браузером (сайты, кнопки, формы)"}


def _in_workspaces(path: str, config) -> Path:
    from .planner import claude_roots
    p = Path(os.path.expanduser(path)).resolve()
    roots = claude_roots(config)
    if not p.is_dir():
        raise ToolInputError(f"Папка не найдена: {p}")
    if not any(p == r or p.is_relative_to(r) for r in roots):
        raise ToolInputError("Папка вне разрешённых рабочих областей: " + str(p))
    from ..tools.base import is_secret_path
    if is_secret_path(p):
        raise ToolInputError("Эта папка закрыта: ключи, пароли, сессии и профили браузеров не трогаем")
    return p


class AgentGenerate(Tool):
    def __init__(self, manager):
        self.manager = manager
        self.spec = ToolSpec(
            id="agent_generate", title="ИИ-шаг",
            description="Internal: run one step on an AI specialist.",
            params={"type": "object", "required": ["provider", "action", "instruction"], "properties": {
                "provider": {"type": "string", "enum": ["claude"]},
                "action": {"type": "string", "enum": ["research", "analyze", "write", "translate", "vision_screen"]},
                "instruction": {"type": "string", "maxLength": 2000},
                "context_labels": {"type": "array"}, "context_chars": {"type": "integer"},
                "sensitivity": {"type": "string"}}},
            risk=Risk.LOW, side_effects="none", timeout_s=None, model_callable=False,   # research takes what it needs
            verification="Результат не пустой; у поиска есть источники; числа в тексте есть во входных данных")

    def validate(self, args, ctx):
        a = super().validate(args, ctx)
        p = self.manager.get(a["provider"])
        cap = Cap(ctx.payload["req"].capability) if ctx.payload else None
        if not p or (cap and cap not in p.capabilities):
            raise ToolInputError("Исполнитель не поддерживает этот шаг")
        return a

    def describe(self, args):
        return f"{PROVIDER_NAMES.get(args['provider'], args['provider'])} · " + {
            "research": "поиск в интернете", "analyze": "анализ", "write": "текст", "translate": "перевод",
            "vision_screen": "анализ изображения"}.get(args["action"], args["action"])

    async def run(self, args, ctx: ToolContext):
        pl = ctx.payload or {}
        p = self.manager.get(args["provider"])
        req = pl["req"]
        t0 = time.perf_counter()
        try:
            r = await p.generate(req, pl.get("on_delta"), lambda m: ctx.progress(m, None))
        except ProviderFailure as e:
            self.manager.record(p.id, p.model_for(req.capability), ctx.task_id, ctx.step_id, req.capability.value,
                                None, None, (time.perf_counter() - t0) * 1000, False, e.kind)
            raise
        self.manager.record(p.id, r.model, ctx.task_id, ctx.step_id, req.capability.value, r.usage, r.cost_usd,
                            r.total_ms, True)
        inputs = [c.get("text", "") for c in req.context] + [req.instruction]
        return ToolResult({"text": r.text, "sources": r.sources, "provider": r.provider, "model": r.model,
                           "grounding": r.meta.get("grounding", True), "note": r.meta.get("note"),
                           "truncated": r.meta.get("truncated", False),
                           "usage": r.usage, "cost_usd": r.cost_usd, "first_token_ms": r.first_token_ms,
                           "total_ms": r.total_ms, "_inputs": inputs, "action": args["action"]},
                          f"{PROVIDER_NAMES.get(r.provider, r.provider)}: готово", card="agent_result")

    async def verify(self, args, result, ctx):
        d = result.data
        status, detail = verify_step(args["action"], d["text"], d["sources"], d.pop("_inputs", []))
        if d.get("note"):
            status, detail = "unverified", d["note"] + "; источников нет, данные могут быть устаревшими"
        if d.get("truncated"):
            status, detail = "unverified", "Ответ обрезан по длине — часть текста могла не поместиться"
        return Verification(status, detail)


class ClaudeInspect(Tool):
    def __init__(self, manager, config):
        self.manager, self.config = manager, config
        self.spec = ToolSpec(
            id="claude_code_inspect", title="Claude Code · просмотр",
            description="Internal: Claude Code reads a project folder (no edits, no commands).",
            params={"type": "object", "required": ["instruction", "workdir"], "properties": {
                "instruction": {"type": "string", "maxLength": 2000}, "workdir": {"type": "string", "maxLength": 500}}},
            risk=Risk.LOW, side_effects="none", timeout_s=limit(config.claude_timeout_s) and config.claude_timeout_s + 30, model_callable=False,
            verification="Claude Code завершил работу без ошибки; использовались только инструменты чтения")

    def validate(self, args, ctx):
        a = super().validate(args, ctx)
        a["workdir"] = str(_in_workspaces(a["workdir"], self.config))
        return a

    def describe(self, args):
        return f"Claude Code · просмотр в {args['workdir'].replace(os.path.expanduser('~'), '~')}"

    async def run(self, args, ctx):
        return await _run_claude(self.manager, ctx, args, "inspect")

    async def verify(self, args, result, ctx):
        used = result.data.get("tools_used", [])
        writes = [u for u in used if u.split(" ")[0].rstrip(":") in ("Edit", "Write", "Bash")]
        if writes:
            return Verification("failed", "В режиме просмотра обнаружены изменяющие действия: " + ", ".join(writes))
        return Verification("verified" if result.data["text"] else "failed",
                            f"Только чтение; действий: {len(used)}")


class ClaudeChange(Tool):
    def __init__(self, manager, config):
        self.manager, self.config = manager, config
        self.spec = ToolSpec(
            id="claude_code_change", title="Claude Code · изменения",
            description="Internal: Claude Code edits files and runs commands in an approved folder.",
            params={"type": "object", "required": ["instruction", "workdir"], "properties": {
                "instruction": {"type": "string", "maxLength": 2000}, "workdir": {"type": "string", "maxLength": 500},
                "permissions": {"type": "array", "default": []}}},
            risk=Risk.HIGH, side_effects="local", timeout_s=limit(config.claude_timeout_s) and config.claude_timeout_s + 330, model_callable=False,
            verification="Список изменённых файлов по git; JARVIS сам перезапускает тесты проекта",
            impact="Claude Code сможет менять файлы и запускать команды в этой папке (без sudo, системных пакетов и "
                   "git push). Если папка не под git, JARVIS сначала сделает резервную копию. Потом покажет изменённые "
                   "файлы и, если есть тесты, сам их перезапустит.")

    def validate(self, args, ctx):
        a = Tool.validate(self, args, ctx)
        a["workdir"] = str(_in_workspaces(a["workdir"], self.config))
        perms = a.get("permissions") or []
        if not isinstance(perms, list) or any(p not in PERM_RU for p in perms):
            raise ToolInputError("Недопустимое разрешение")
        a["permissions"] = sorted(set(perms))
        return a

    def describe(self, args):
        extra = ("; разрешено: " + ", ".join(PERM_RU[p] for p in args.get("permissions", []))) \
            if args.get("permissions") else ""
        return f"Claude Code: «{args['instruction'][:100]}» в {args['workdir'].replace(os.path.expanduser('~'), '~')}{extra}"

    async def run(self, args, ctx):
        wd = args["workdir"]
        before = await _git_state(wd)
        snap = None
        if before is None:
            snap = _snapshot(wd)
            res_backup = await asyncio.to_thread(_backup, wd, ctx.config)
        else:
            res_backup = None
        if (ctx.payload or {}).get("req") is not None:
            ctx.payload["req"].permissions = args.get("permissions", [])
        res = await _run_claude(self.manager, ctx, args, "change")
        if before is not None:
            after = await _git_state(wd)
            res.data["git"] = {"repo": True, "changed": _changed(before, after)}
        else:
            res.data["git"] = {"repo": False, "changed": _diff_snapshot(snap, _snapshot(wd)), "backup": res_backup}
        ctx.progress("JARVIS проверяет тесты", None)
        res.data["tests"] = await _run_tests(wd)
        return res

    async def verify(self, args, result, ctx):
        g, t = result.data.get("git", {}), result.data.get("tests", {})
        files = ", ".join(g.get("changed", [])[:10]) or "нет"
        if g.get("backup"):
            files += f" (резервная копия: {g['backup'].replace(os.path.expanduser('~'), '~')})"
        if t.get("ran"):
            ok = t.get("code") == 0
            return Verification("verified" if ok else "failed",
                                f"Изменённые файлы: {files}. Тесты ({t['cmd']}): {'прошли' if ok else 'не прошли'} — "
                                f"{t.get('tail', '')[-160:]}")
        why = t.get("error") or "тесты не найдены"
        return Verification("unverified", f"Изменённые файлы: {files}. Независимая проверка не выполнялась: {why}")


_SLUG = __import__("re").compile(r"[^a-z0-9._-]+")


def new_project_dir(name: str, config) -> Path:
    """A fresh folder for a new project in the first code workspace (~/Projects/<name>), never an existing one."""
    import re as _re
    tr = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
                        "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
                        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
                        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya", " ": "-"})
    slug = _SLUG.sub("-", (name or "").strip().lower().translate(tr)).strip("-._")[:50]
    slug = _re.sub(r"-{2,}", "-", slug) or time.strftime("task-%Y%m%d-%H%M")
    root = Path(os.path.expanduser(config.code_workspaces[0] if config.code_workspaces else "~/Projects")).resolve()
    p, i = root / slug, 2
    while p.exists() and any(p.iterdir()):
        p, i = root / f"{slug}-{i}", i + 1
    return p


class ComputerTask(ClaudeChange):
    """The conversation's universal «do it» tool: whatever the user asks to make or change on the computer (a site,
    a program, a script, files, settings of a project) is handed to Claude Code — after the user's confirmation, with
    the same backup / changed-files / tests checks as any code change. The conversation never has to say «no tool»."""

    def __init__(self, manager, config):
        super().__init__(manager, config)
        self.spec = ToolSpec(
            id="computer_task", title="Задача на компьютере (Claude Code)",
            description=(
                "Do real work on this computer through Claude Code (files, code, terminal, web lookup): create "
                "websites, apps, scripts, games, documents and other files; edit or fix code; run and test programs; "
                "install a project's dependencies or user-level tools; download files; set up projects. Use it "
                "whenever the user asks to make, build, create, write (code), fix, install, convert or set up "
                "something — never answer that there is no tool for it. Give a complete, detailed instruction in the "
                "user's language (what to build, style, content, technology). For a new thing set new_folder to a "
                "short latin name (it is created in ~/Projects); for an existing folder set folder (any folder in "
                "the home directory). Installing user-level tools (pip/uv/npm/cargo, GitHub releases) and downloading are "
                "always allowed; put 'browser' in allow if websites must be opened, clicked, filled or read like a "
                "person would (a separate browser without the user's logins). The user "
                "confirms first; work may take several minutes; the result lists changed files."),
            params={"type": "object", "required": ["instruction"], "properties": {
                "instruction": {"type": "string", "maxLength": 4000},
                "new_folder": {"type": "string", "maxLength": 60},
                "folder": {"type": "string", "maxLength": 500},
                "allow": {"type": "array", "items": {"type": "string", "enum": sorted(PERM_RU)}}}},
            risk=Risk.HIGH, side_effects="local", timeout_s=limit(config.claude_timeout_s) and config.claude_timeout_s + 330, model_callable=True,
            verification="Список изменённых/созданных файлов; JARVIS сам перезапускает тесты проекта, если они есть",
            impact="Claude Code создаст или изменит файлы в этой папке и сможет запускать там команды (без sudo и "
                   "системных пакетов). Существующая папка без git сначала копируется в резервную копию.")

    def validate(self, args, ctx):
        if "workdir" in args:                  # already-checked args (e.g. resumed after a restart): check again
            a = dict(args)
            wd = Path(str(a["workdir"])).resolve()
            if a.get("new"):
                root = new_project_dir("x", self.config).parent
                if wd.parent != root or (wd.exists() and any(wd.iterdir())):
                    raise ToolInputError("Новая папка должна быть пустой и лежать в " + str(root))
            else:
                wd = _in_workspaces(str(wd), self.config)
            perms = a.get("permissions") or []
            if not isinstance(perms, list) or any(p not in PERM_RU for p in perms) or not str(a.get("instruction", "")).strip():
                raise ToolInputError("Неверное задание")
            return {"instruction": str(a["instruction"])[:4000], "permissions": sorted(set(perms)),
                    "workdir": str(wd), "new": bool(a.get("new"))}
        a = Tool.validate(self, args, ctx)
        perms = a.get("allow") or []
        if not isinstance(perms, list) or any(p not in PERM_RU for p in perms):
            raise ToolInputError("Недопустимое разрешение")
        # Getting the tools a job needs is part of doing it: user-level installs and downloads are always allowed
        # (no sudo, no system packages); the person confirms the whole job once.
        out = {"instruction": a["instruction"].strip(), "permissions": sorted(set(perms) | {"install", "download"})}
        if not out["instruction"]:
            raise ToolInputError("Пустое задание")
        if a.get("folder"):
            out["workdir"], out["new"] = str(_in_workspaces(a["folder"], self.config)), False
        else:
            out["workdir"], out["new"] = str(new_project_dir(a.get("new_folder") or "", self.config)), True
        return out

    def describe(self, args):
        extra = ("; разрешено: " + ", ".join(PERM_RU[p] for p in args.get("permissions", []))) \
            if args.get("permissions") else ""
        where = args["workdir"].replace(os.path.expanduser("~"), "~") + (" (новая папка)" if args.get("new") else "")
        return f"Claude Code: «{args['instruction'][:120]}» в {where}{extra}"

    async def run(self, args, ctx):
        if args.get("new"):
            Path(args["workdir"]).mkdir(parents=True, exist_ok=True)
            if shutil.which("git"):               # a new project is tracked from the start (local only, no commits)
                await run_cmd(["git", "init", "-q", args["workdir"]])
        from ..providers.base import StepRequest
        found = research_context(ctx.conversation, ctx.task_id, args["instruction"])
        ctx.payload = {**(ctx.payload or {}), "req": StepRequest(Cap.CODE, args["instruction"],
                                                               context=[found] if found else [])}
        res = await super().run(args, ctx)
        if found:
            res.data["research"] = {"query": found["query"], "sources": len(found["sources"])}
        return res


def research_context(conv: dict, task_id: str, instruction: str) -> dict | None:
    """What JARVIS's web search found, for a Claude Code job about the same thing: found in this task («найди … и
    сделай …»), referred to («используй найденное»), or sharing the search's key words within the hour. Claude Code
    reads it as data; the person does not repeat it."""
    lr = (conv or {}).get("last_research")
    if not lr or time.time() - float(lr.get("at") or 0) > 6 * 3600:
        return None
    from ..research import terms
    from .agency import REFERS_RESEARCH
    shared = set(terms(lr.get("query", ""))) & set(terms(instruction))
    if not (lr.get("task_id") == task_id or REFERS_RESEARCH.search(instruction)
            or (len(shared) >= 2 and time.time() - float(lr["at"]) < 3600)):
        return None
    return {"label": f"Найдено JARVIS в интернете (поиск «{lr.get('query', '')}»)", "text": lr.get("text", ""),
            "sources": lr.get("sources", []), "query": lr.get("query", "")}


async def _run_claude(manager, ctx: ToolContext, args: dict, mode: str) -> ToolResult:
    p = manager.get("claude_code")
    req = (ctx.payload or {}).get("req")
    from ..providers.base import StepRequest
    if req is None:
        req = StepRequest(Cap.CODE, args["instruction"])
    req.workdir, req.mode = args["workdir"], mode
    t0 = time.perf_counter()
    try:
        r = await p.generate(req, None, lambda m: ctx.progress(m, None))
    except ProviderFailure as e:
        manager.record("claude_code", None, ctx.task_id, ctx.step_id, "code", None, None,
                       (time.perf_counter() - t0) * 1000, False, e.kind)
        raise
    manager.record("claude_code", r.model, ctx.task_id, ctx.step_id, "code", r.usage, r.cost_usd, r.total_ms, True)
    return ToolResult({"text": r.text, "provider": "claude_code", "model": r.model, "usage": r.usage,
                       "cost_usd": r.cost_usd, "total_ms": r.total_ms, "tools_used": r.meta.get("tools_used", []),
                       "turns": r.meta.get("turns"), "permission_denials": len(r.meta.get("permission_denials", [])),
                       "action": "change_code" if mode == "change" else "inspect_code", "sources": []},
                      "Claude Code: готово", card="agent_result")


def _snapshot(wd: str, limit: int = 20000) -> dict:
    out = {}
    for i, p in enumerate(Path(wd).rglob("*")):
        if i > limit:
            break
        if p.is_file() and ".git" not in p.parts:
            try:
                st = p.stat()
                out[str(p.relative_to(wd))] = (st.st_size, int(st.st_mtime_ns))
            except OSError:
                pass
    return out


def _diff_snapshot(a: dict | None, b: dict) -> list[str]:
    a = a or {}
    changed = [f"+ {k}" for k in b if k not in a] + [f"~ {k}" for k in b if k in a and a[k] != b[k]] + \
        [f"- {k}" for k in a if k not in b]
    return sorted(changed)


def _backup(wd: str, config, max_bytes: int = 50_000_000) -> str | None:
    """Copy a non-git folder before Claude Code changes it (skipped for very large folders)."""
    total = sum(p.stat().st_size for p in Path(wd).rglob("*") if p.is_file())
    if total > max_bytes:
        return None
    dest = Path(os.path.expanduser("~/.local/share/jarvis/backups")) / (time.strftime("%Y%m%d-%H%M%S-")
                                                                      + Path(wd).name)
    if os.environ.get("JARVIS_HOME"):
        dest = Path(os.environ["JARVIS_HOME"]) / "data" / "backups" / dest.name
    shutil.copytree(wd, dest, symlinks=True, ignore=shutil.ignore_patterns(".venv", "node_modules", "__pycache__"))
    return str(dest)


async def _git_state(wd: str) -> dict | None:
    if not shutil.which("git"):
        return None
    r = await run_cmd(["git", "-C", wd, "status", "--porcelain", "--untracked-files=all"], timeout=15)
    if r.code != 0:
        return None
    d = await run_cmd(["git", "-C", wd, "diff", "HEAD", "--stat"], timeout=15)
    return {"status": r.stdout, "diff": d.stdout}


def _changed(before: dict | None, after: dict | None) -> list[str]:
    if after is None:
        return []
    b = set((before or {}).get("status", "").splitlines())
    lines = [ln for ln in after["status"].splitlines() if ln not in b] or after["status"].splitlines()
    out = []
    for ln in lines:
        if len(ln) > 3:
            code = ln[:2]
            mark = "+ " if code in ("??", "A ", "AM") else "- " if "D" in code else ""
            out.append(mark + ln[3:])                 # «+ new», «- deleted», plain = changed
    return out


async def _run_tests(wd: str) -> dict:
    p = Path(wd)
    if (p / "tests").is_dir() or list(p.glob("test_*.py")) or (p / "pytest.ini").exists():
        py = p / ".venv" / "bin" / "python"
        exe = str(py) if py.exists() else "python3"
        probe = await run_cmd([exe, "-c", "import pytest"], timeout=20)
        if probe.code != 0:
            return {"ran": False, "error": "pytest не установлен для этого проекта — тесты не запускались"}
        cmd = [exe, "-m", "pytest", "-q", "-x", "--no-header"]
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=wd, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            out, _ = await asyncio.wait_for(proc.communicate(), 300)
            return {"ran": True, "cmd": "pytest -q", "code": proc.returncode,
                    "tail": out.decode("utf-8", "replace").strip()[-600:]}
        except asyncio.TimeoutError:
            return {"ran": True, "cmd": "pytest -q", "code": -1, "tail": "тесты не уложились в 300 с"}
        except OSError as e:
            return {"ran": False, "error": str(e)}
    return {"ran": False}


class OpenUrl(Tool):
    spec = ToolSpec(
        id="open_url", title="Открыть ссылку",
        description="Open an http(s) link in the default browser.",
        params={"type": "object", "required": ["url"], "properties": {"url": {"type": "string", "maxLength": 2000}}},
        risk=Risk.LOW, side_effects="local", timeout_s=8, model_callable=True,
        verification="Ссылка передана браузеру (код 0)")

    def validate(self, args, ctx):
        a = super().validate(args, ctx)
        if not a["url"].startswith(("https://", "http://")) or any(c in a["url"] for c in " \n\r\t"):
            raise ToolInputError("Можно открывать только ссылки http(s)")
        return a

    def describe(self, args):
        return "Открыть " + args["url"][:80]

    async def run(self, args, ctx):
        launcher = ["uwsm-app", "--"] if shutil.which("uwsm-app") else []
        await run_cmd(launcher + ["xdg-open", args["url"]], detach=True)
        return ToolResult({"url": args["url"]}, "Открываю ссылку в браузере.")

    async def verify(self, args, result, ctx):
        return Verification("unverified", "Ссылка передана браузеру; загрузка страницы не проверялась")
