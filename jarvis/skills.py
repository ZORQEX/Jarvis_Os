"""Skills: abilities JARVIS builds for itself. When it cannot do something, it asks Claude Code to find a ready tool
(GitHub, pip/pipx/uv, npm, cargo, a release binary), install it user-level, wrap it as a small command with a manifest,
and test it on a real example. The skill is then a tool like any other: listed for the model, run through the gateway
(policy, approval, audit), stoppable. JARVIS's own core is never rewritten by a skill.

Layout: <skills_dir>/<name>/skill.json + the files Claude Code made (run.sh, a venv, a binary…).
skill.json: {"name", "title", "description", "run": ["./run.sh"], "args": [{"name", "description"}], "example": [...]}
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from pathlib import Path

from .tools.base import Risk, Tool, ToolError, ToolInputError, ToolResult, ToolSpec, Verification

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
OUTPUT_CAP = 20000


def skills_root(config) -> Path:
    return Path(os.path.expanduser(getattr(config, "skills_dir", "~/.local/share/jarvis-skills"))).resolve()


def read_manifest(d: Path) -> dict | None:
    """A skill's manifest if it is complete and its command stays inside the skill folder; else None."""
    try:
        m = json.loads((d / "skill.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or m.get("name") != d.name or not NAME_RE.match(d.name):
        return None
    run = m.get("run")
    if not isinstance(run, list) or not run or not all(isinstance(x, str) and x for x in run):
        return None
    exe = run[0]
    if exe.startswith("./"):
        target = (d / exe).resolve()
        if not target.is_relative_to(d.resolve()) or not target.exists():
            return None
    args = m.get("args") or []
    if not isinstance(args, list) or not all(isinstance(a, dict) and a.get("name") for a in args):
        return None
    return {"name": d.name, "title": str(m.get("title") or d.name)[:80], "description": str(m.get("description") or "")[:600],
            "run": run, "args": [{"name": str(a["name"])[:40], "description": str(a.get("description") or "")[:200]} for a in args],
            "example": m.get("example") if isinstance(m.get("example"), list) else [], "dir": str(d)}


def list_skills(config) -> list[dict]:
    root = skills_root(config)
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if d.is_dir():
            m = read_manifest(d)
            if m:
                out.append(m)
    return out


class SkillList(Tool):
    def __init__(self, config):
        self.config = config
        self.spec = ToolSpec(
            id="skill_list", title="Навыки JARVIS",
            description="List the skills JARVIS has built for itself earlier (name, what it does, arguments). Check "
                        "this before building something new.",
            params={"type": "object", "properties": {}},
            risk=Risk.LOW, side_effects="none", timeout_s=10, model_callable=True,
            verification="Манифесты прочитаны с диска")

    async def run(self, args, ctx):
        skills = [{k: v for k, v in s.items() if k != "dir"} for s in list_skills(self.config)]
        names = ", ".join(s["name"] for s in skills) or "пока нет"
        return ToolResult({"skills": skills, "count": len(skills)}, f"Навыки JARVIS: {names}.")

    async def verify(self, args, result, ctx):
        return Verification("verified", f"Найдено навыков: {result.data['count']}")


class SkillRun(Tool):
    def __init__(self, config):
        self.config = config
        self.spec = ToolSpec(
            id="skill_run", title="Навык JARVIS",
            description="Run one of JARVIS's own skills (see skill_list) with its arguments (a list of strings, in the "
                        "order of the skill's args). Returns the command's output.",
            params={"type": "object", "required": ["name"], "properties": {
                "name": {"type": "string", "maxLength": 40},
                "args": {"type": "array", "items": {"type": "string", "maxLength": 2000}, "maxItems": 20}}},
            risk=Risk.MODERATE, side_effects="local", timeout_s=None, model_callable=True,
            verification="Код завершения команды навыка",
            impact="Запустит программу навыка, которую JARVIS ранее собрал и проверил сам (в её папке, без sudo).")

    def validate(self, args, ctx):
        a = Tool.validate(self, args, ctx)
        name = str(a["name"])
        if not NAME_RE.match(name):
            raise ToolInputError("Неверное имя навыка")
        m = read_manifest(skills_root(self.config) / name)
        if not m:
            raise ToolInputError(f"Навыка «{name}» нет (или он собран не полностью)")
        return {"name": name, "args": [str(x) for x in (a.get("args") or [])]}

    def describe(self, args):
        m = read_manifest(skills_root(self.config) / args["name"]) or {"title": args["name"]}
        shown = " ".join(args["args"])[:120]
        return f"Навык «{m['title']}»" + (f": {shown}" if shown else "")

    async def run(self, args, ctx):
        m = read_manifest(skills_root(self.config) / args["name"])
        if not m:
            raise ToolError("Навык пропал с диска")
        from .providers.claude_code import _clean_env
        t0 = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(*m["run"], *args["args"], cwd=m["dir"], env=_clean_env(),
                                                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            out, err = await proc.communicate()
        except asyncio.CancelledError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)       # «стоп» ends the skill and whatever it started
            except ProcessLookupError:
                pass
            raise
        text = out.decode("utf-8", "replace")
        tail = err.decode("utf-8", "replace")[-2000:]
        data = {"name": args["name"], "code": proc.returncode, "output": text[-OUTPUT_CAP:], "stderr": tail,
                "ms": round((time.perf_counter() - t0) * 1000)}
        if proc.returncode != 0:
            raise ToolError(f"Навык «{m['title']}» завершился с ошибкой (код {proc.returncode}): {tail[-300:] or text[-300:]}")
        return ToolResult(data, f"Навык «{m['title']}» выполнен.", untrusted=True)

    async def verify(self, args, result, ctx):
        return Verification("unverified", "Команда навыка завершилась с кодом 0; итог зависит от навыка")


class SkillCreate(Tool):
    """Claude Code builds a new skill in its own folder: finds and installs what is needed, writes the wrapper and the
    manifest, and tests it. Verified only when a complete manifest exists afterwards."""

    def __init__(self, manager, config):
        from .config import limit
        self.manager, self.config = manager, config
        self.spec = ToolSpec(
            id="skill_create", title="Новый навык JARVIS",
            description=("Teach JARVIS a new reusable ability when no tool or skill can do the task (e.g. download "
                         "videos, convert or transcribe media, OCR, read a site's API, edit images). Claude Code finds a "
                         "ready solution on the internet (GitHub, pip/pipx/uv, npm, cargo, release binaries), installs it "
                         "for the user (no sudo), wraps it as a command with a manifest and tests it. Then use skill_run. "
                         "name: short latin snake_case; instruction: what the skill must do, its inputs and outputs."),
            params={"type": "object", "required": ["name", "instruction"], "properties": {
                "name": {"type": "string", "maxLength": 40}, "instruction": {"type": "string", "maxLength": 3000}}},
            risk=Risk.HIGH, side_effects="local", timeout_s=limit(config.claude_timeout_s), model_callable=True,
            verification="После работы в папке навыка есть полный skill.json и команда запуска",
            impact="Claude Code скачает и установит нужные программы для вашего пользователя (без sudo), создаст папку "
                   "навыка и проверит его на примере. Пароли и аккаунты не трогает.")

    def validate(self, args, ctx):
        a = Tool.validate(self, args, ctx)
        name = str(a["name"]).strip().lower()
        if not NAME_RE.match(name):
            raise ToolInputError("Имя навыка: латиница, цифры и _, например video_download")
        if not str(a["instruction"]).strip():
            raise ToolInputError("Пустое задание")
        return {"name": name, "instruction": str(a["instruction"]).strip()}

    def describe(self, args):
        return f"Новый навык «{args['name']}»: {args['instruction'][:120]}"

    async def run(self, args, ctx):
        from .agency.tools import _run_claude
        from .providers.base import Cap, StepRequest
        d = skills_root(self.config) / args["name"]
        d.mkdir(parents=True, exist_ok=True)
        instruction = (
            f"Создай НАВЫК для ассистента JARVIS в текущей папке (имя навыка: {args['name']}).\n"
            f"Что должен уметь навык: {args['instruction']}\n\n"
            "Как работать:\n"
            "1. Найди готовое решение (GitHub, PyPI, npm, crates.io, релизы с бинарниками) — не пиши с нуля то, что "
            "уже есть. Ставь только для пользователя, без sudo: `uv tool install`, `pipx`, `python -m venv .venv` в этой "
            "папке, `npm install` в этой папке, бинарник из релиза в ./bin. Системные пакеты (pacman) нельзя — если без "
            "них никак, найди альтернативу или напиши в отчёте точное имя пакета.\n"
            "2. Сделай исполняемую обёртку ./run.sh (bash, `set -euo pipefail`), которая принимает аргументы в командной "
            "строке, делает работу и печатает в stdout понятный результат (что сделано, пути к файлам). Готовые файлы "
            "по умолчанию клади в ~/Downloads/Jarvis, если не сказано иное.\n"
            "3. Напиши ./skill.json строго такого вида: {\"name\": \"" + args["name"] + "\", \"title\": \"<коротко по-русски>\", "
            "\"description\": \"<что делает и когда звать, для модели>\", \"run\": [\"./run.sh\"], "
            "\"args\": [{\"name\": \"<аргумент>\", \"description\": \"<что передать>\"}], \"example\": [\"<пример аргументов>\"]}.\n"
            "4. Проверь навык на реальном примере (запусти ./run.sh с аргументами), исправь ошибки.\n"
            "В отчёте: что установлено, как вызывать, результат проверки.")
        req = StepRequest(Cap.CODE, instruction)
        req.permissions = ["browser", "download", "install"]      # finding and installing tools is the whole point
        ctx.payload = {**(ctx.payload or {}), "req": req}
        return await _run_claude(self.manager, ctx, {"instruction": instruction, "workdir": str(d),
                                                     "permissions": ["browser", "download", "install"]}, "change")

    async def verify(self, args, result, ctx):
        m = read_manifest(skills_root(self.config) / args["name"])
        if m:
            return Verification("verified", f"Навык готов: {m['title']} ({' '.join(m['run'])})")
        return Verification("failed", "Claude Code закончил, но полного skill.json с командой запуска нет")
