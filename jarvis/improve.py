"""Self-improvement: JARVIS finds where it fails from its own records and improves its own code.

1. `collect_issues` reads JARVIS's own records (tasks, voice turns, messages) and ranks real problems with evidence:
   requests the person had to repeat, failed tasks, slow spoken answers, phrases not heard, «не понял» replies.
2. `self_improve` (the person confirms): Claude Code changes JARVIS's code on a new git branch in a separate worktree
   (~/Projects/.jarvis-improve/…), following the repository's CLAUDE.md, adds a test and commits. JARVIS then runs the
   whole test suite there itself — the running JARVIS and the main branch are untouched.
3. `self_improve_apply` (the person confirms again, seeing the changed files and the test result): merges the branch
   into the main branch and restarts JARVIS on the new code. Every step is a git commit: easy to see and to undo.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .tools.base import Risk, Tool, ToolInputError, ToolResult, ToolSpec, Verification, run_cmd

REPO = Path(__file__).resolve().parents[1]
WORK = Path(os.path.expanduser("~/Projects/.jarvis-improve"))     # inside the folders Claude Code may work in
SLOW_S = 6.0                         # a spoken answer slower than this (end of phrase → first sound) is a problem
BRANCH = re.compile(r"^improve/[\w.-]{3,80}$")


# ------------------------------------------------------------------------------------------------ the problem journal
def _norm(t: str) -> str:
    t = re.sub(r"^\s*(?:джарвис|jarvis|жарвис|джервис|джарис)[\s,.!:—–-]*", "", (t or "").casefold().replace("ё", "е"))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t)).strip()


def _same(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return False                         # «3 плюс 3» and «4 плюс 4» are two questions, not a repeat
    wa, wb = set(a.split()), set(b.split())
    return min(len(wa), len(wb)) >= 3 and len(wa & wb) / max(len(wa), len(wb)) >= 0.75


def _when(ts: float) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(ts))


def collect_issues(db, days: float = 7.0, limit: int = 8) -> list[dict]:
    """Real problems from JARVIS's own records of the last days, most frequent first. Each has a title, a count,
    examples and an `evidence` text that goes into the improvement task as it is."""
    since = time.time() - days * 86400
    out: list[dict] = []

    # 1. The person had to say the same thing again within 2 minutes: JARVIS did not understand or did not act.
    rows = [dict(r) for r in db.all("SELECT created_at, text, meta FROM messages WHERE role='user' AND created_at>? "
                                    "ORDER BY created_at", (since,))]
    reps, last = [], 0.0
    for a, b in zip(rows, rows[1:]):
        if b["created_at"] - a["created_at"] < 120 and _same(_norm(a["text"]), _norm(b["text"])):
            reps.append(b["text"][:120])
            last = b["created_at"]
    if reps:
        out.append({"kind": "repeat", "title": "Пришлось повторять просьбу", "count": len(reps), "examples": reps[:5],
                    "last": last,
                    "evidence": "Человек повторил ту же просьбу в течение 2 минут (JARVIS не понял или не выполнил): "
                                + "; ".join(f"«{x}»" for x in reps[:5])})

    # 2. Failed tasks, grouped by the error.
    groups: dict[str, list[str]] = {}
    lasts: dict[str, float] = {}
    for r in db.all("SELECT request, error, created_at FROM tasks WHERE state='failed' AND created_at>? "
                    "AND route!='ui_read'", (since,)):
        key = re.sub(r"\s+", " ", (r["error"] or "без описания"))[:140]
        groups.setdefault(key, []).append(r["request"][:100])
        lasts[key] = max(lasts.get(key, 0.0), r["created_at"])
    for err, reqs in sorted(groups.items(), key=lambda x: -len(x[1]))[:4]:
        out.append({"kind": "failed", "title": f"Ошибка: {err[:90]}", "count": len(reqs), "examples": reqs[:5],
                    "last": lasts[err],
                    "evidence": f"Задачи завершились ошибкой «{err}» ({len(reqs)} раз), например: "
                                + "; ".join(f"«{x}»" for x in reqs[:4])})

    # 3. Slow spoken answers.
    slow = []
    for r in db.all("SELECT transcript, route, speech_end, ttft, first_audio_played, created_at FROM voice_turns "
                    "WHERE created_at>? AND speech_end IS NOT NULL AND first_audio_played IS NOT NULL", (since,)):
        wait = r["first_audio_played"] - r["speech_end"]
        if wait > SLOW_S:
            slow.append((wait, r["transcript"] or "", r["route"] or "", (r["ttft"] or 0) - r["speech_end"],
                         r["created_at"]))
    if slow:
        last = max(x[4] for x in slow)
        slow.sort(reverse=True)
        out.append({"kind": "slow", "title": f"Медленный ответ голосом (дольше {SLOW_S:.0f} с)", "count": len(slow),
                    "last": last,
                    "examples": [f"{w:.1f} с — «{t[:80]}»" for w, t, *_ in slow[:5]],
                    "evidence": "От конца фразы до первого звука ответа: "
                                + "; ".join(f"{w:.1f} с (первый текст ИИ через {ai:.1f} с, маршрут {rt}, {_when(ts)}) "
                                            f"— «{t[:80]}»" for w, t, rt, ai, ts in slow[:5])})

    # 4. Phrases recognised as nothing, voice errors.
    empty = db.one("SELECT COUNT(*) AS n FROM voice_turns WHERE created_at>? AND (transcript='' OR transcript IS NULL) "
                   "AND route IS NULL", (since,))["n"]
    if empty:
        out.append({"kind": "not_heard", "title": "Фраза не распознана («не расслышал»)", "count": empty, "examples": [],
                    "evidence": f"{empty} записей речи распознаны как пустые (JARVIS ответил «не расслышал»)."})
    errs = [dict(r) for r in db.all("SELECT error FROM voice_turns WHERE created_at>? AND error IS NOT NULL", (since,))]
    if errs:
        out.append({"kind": "voice_error", "title": "Сбой голосового цикла", "count": len(errs),
                    "examples": [e["error"][:100] for e in errs[:5]],
                    "evidence": "Ошибки голосового цикла: " + "; ".join(e["error"][:120] for e in errs[:5])})

    # 5. JARVIS said it did not understand / could not.
    nu = [dict(r) for r in db.all("SELECT m.text AS reply, (SELECT u.text FROM messages u WHERE u.task_id=m.task_id "
                                  "AND u.role='user' LIMIT 1) AS req FROM messages m WHERE m.role='assistant' AND "
                                  "m.created_at>? AND (m.text LIKE 'Не понял%' OR m.text LIKE '%не понял,%')",
                                  (since,))]
    if nu:
        out.append({"kind": "not_understood", "title": "JARVIS ответил «не понял»", "count": len(nu),
                    "examples": [(x["req"] or "")[:80] for x in nu[:5]],
                    "evidence": "Ответы «не понял» на просьбы: " + "; ".join(f"«{(x['req'] or '')[:80]}» → «{x['reply'][:80]}»"
                                                                     for x in nu[:4])})
    for x in out:
        if x.get("last"):
            x["last_text"] = _when(x["last"])
    out.sort(key=lambda x: -x["count"])
    return out[:limit]


# ------------------------------------------------------------------------------------------------ git helpers
async def _git(*args: str, cwd: Path | None = None, timeout: float = 60) -> tuple[int, str]:
    r = await run_cmd(["git", "-C", str(cwd or REPO), *args], timeout=timeout)
    return r.code, (r.stdout or "") + (r.stderr or "")


def _slug(text: str) -> str:
    tr = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
                        "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
                        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
                        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"})
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").casefold().translate(tr)).strip("-")
    return "-".join(s.split("-")[:5])[:40] or "fix"


async def run_suite(wd: Path, timeout: float = 900) -> dict:
    """The project's own checks, run by JARVIS itself (not by the model that made the change)."""
    res = {}
    for name, cmd in (("pytest", ["uv", "run", "--quiet", "pytest", "-q", "-x", "--no-header"]),
                      ("ui-unit", ["node", "--test", "tests/ui/unit/"])):
        if name == "ui-unit" and not (wd / "tests" / "ui" / "unit").is_dir():
            continue
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(wd), stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
            res[name] = {"code": proc.returncode, "tail": out.decode("utf-8", "replace").strip()[-500:]}
        except asyncio.TimeoutError:
            res[name] = {"code": -1, "tail": f"не уложились в {timeout:.0f} с"}
        except OSError as e:
            res[name] = {"code": -1, "tail": str(e)}
    return res


def _tests_ok(t: dict) -> bool:
    return bool(t) and all(v.get("code") == 0 for v in t.values())


def _tests_line(t: dict) -> str:
    parts = []
    for name, v in t.items():
        last = (v.get("tail") or "").splitlines()[-1:] or [""]
        parts.append(f"{name}: {'прошли' if v.get('code') == 0 else 'НЕ прошли'} ({last[0][:90]})")
    return "; ".join(parts) or "тесты не запускались"


INSTRUCTION = """Ты улучшаешь сам JARVIS — это его собственный репозиторий (ветка {branch}, отдельная копия; основная
версия, которая сейчас работает, не затронута). Сначала прочитай CLAUDE.md и следуй его правилам.

Цель: {goal}

Доказательства из записей JARVIS:
{evidence}

Как работать:
- Найди причину в коде, а не только симптом. Сделай самое маленькое надёжное изменение.
- Добавь или поправь тест, который ловит эту проблему (tests/…; для окна — tests/ui/unit/…).
- Запусти `uv run pytest -q` (и `node --test tests/ui/unit/`, если менял web/) и добейся, чтобы всё прошло.
- Сделай коммит в этой ветке: `git add -A && git commit -m "<что исправлено и почему, по-английски>"`.
- Не делай git push, не трогай ключи, пароли и данные пользователя, не меняй ~/.local/share/jarvis.
- В конце коротко по-русски: что было причиной, что изменено, что проверено.
"""


class ImproveIssues(Tool):
    def __init__(self, db):
        self.db = db
        self.spec = ToolSpec(
            id="improve_issues", title="Что у JARVIS работает плохо",
            description="Problems JARVIS found in its own records of the last days (requests the user had to repeat, "
                        "failed tasks, slow spoken answers, phrases not heard, «не понял» replies), most frequent first, "
                        "with evidence. Use it before self_improve when the user asks you to improve or fix yourself.",
            params={"type": "object", "properties": {"days": {"type": "number", "minimum": 1, "maximum": 60}}},
            risk=Risk.LOW, side_effects="none", timeout_s=15, model_callable=True,
            verification="Прочитано из записей JARVIS (задачи, голосовые записи, сообщения)")

    async def run(self, args, ctx):
        issues = collect_issues(self.db, float(args.get("days") or 7))
        if not issues:
            return ToolResult({"issues": []}, "За последние дни проблем в записях не найдено.")
        lines = [f"{i + 1}. {x['title']} — {x['count']} раз. {x['evidence'][:300]}" for i, x in enumerate(issues)]
        return ToolResult({"issues": issues}, "Найдено проблем: " + str(len(issues)) + "\n" + "\n".join(lines))

    async def verify(self, args, result, ctx):
        return Verification("verified", self.spec.verification)


class SelfImprove(Tool):
    def __init__(self, manager, config):
        self.manager, self.config = manager, config
        self.spec = ToolSpec(
            id="self_improve", title="Улучшить JARVIS",
            description="Improve JARVIS's own code (this assistant): Claude Code works on a new git branch in a separate "
                        "copy of the repository, adds a test and commits; JARVIS then runs all its tests there. The "
                        "running JARVIS is not changed — applying needs self_improve_apply and a second confirmation. "
                        "Give a concrete goal (what goes wrong and what should happen) and the evidence (from "
                        "improve_issues or what the user said).",
            params={"type": "object", "required": ["goal"], "properties": {
                "goal": {"type": "string", "minLength": 5, "maxLength": 3000},
                "evidence": {"type": "string", "maxLength": 4000}}},
            risk=Risk.HIGH, side_effects="local", cancellable=True, idempotent=False, model_callable=True,
            timeout_s=None if not getattr(config, "claude_timeout_s", 0) else config.claude_timeout_s + 1200,
            verification="JARVIS сам прогоняет все тесты в новой ветке; изменения — коммиты git",
            impact="Claude Code изменит код JARVIS в отдельной копии (новая ветка git). Работающий JARVIS не меняется: "
                   "применить улучшение можно только отдельной командой и вашим вторым подтверждением.")

    def describe(self, args):
        return f"Улучшить JARVIS: «{args['goal'][:160]}»"

    async def run(self, args, ctx):
        if not shutil.which("git"):
            raise ToolInputError("git не установлен")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        branch = f"improve/{stamp}-{_slug(args['goal'])}"
        wd = WORK / branch.split("/", 1)[1]
        WORK.mkdir(parents=True, exist_ok=True)
        code, out = await _git("worktree", "add", "-b", branch, str(wd), "HEAD")
        if code != 0:
            raise RuntimeError("Не удалось создать отдельную копию: " + out[-300:])
        ctx.progress("Claude Code улучшает JARVIS в отдельной ветке", None)
        from .agency.tools import _run_claude
        instruction = INSTRUCTION.format(branch=branch, goal=args["goal"], evidence=args.get("evidence") or "—")
        res = await _run_claude(self.manager, ctx, {"instruction": instruction, "workdir": str(wd)}, "change")
        await _git("add", "-A", cwd=wd)                     # what the model left uncommitted is kept, not lost
        await _git("commit", "-q", "-m", f"JARVIS self-improvement: {args['goal'][:60]}", cwd=wd)
        _, log = await _git("log", "--oneline", "HEAD", "^main", cwd=wd)
        _, stat = await _git("diff", "--stat", "main...HEAD", cwd=wd)
        ctx.progress("JARVIS прогоняет свои тесты в новой ветке", None)
        tests = await run_suite(wd)
        commits = [x for x in log.splitlines() if x.strip()]
        data = {**res.data, "branch": branch, "worktree": str(wd), "commits": commits, "diffstat": stat.strip()[-1500:],
                "tests": tests, "tests_ok": _tests_ok(tests)}
        if not commits:
            summary = "Claude Code ничего не изменил. " + (res.data.get("text") or "")[:400]
        else:
            summary = (f"Улучшение готово в ветке {branch}: {len(commits)} коммит(ов). Тесты: {_tests_line(tests)}. "
                       + ("Применить его — скажите «примени улучшение»." if data["tests_ok"] else
                          "Тесты не прошли — применять нельзя.") + "\n\n" + (res.data.get("text") or "")[:1200])
        return ToolResult(data, summary, card="agent_result")

    async def verify(self, args, result, ctx):
        d = result.data
        if not d.get("commits"):
            return Verification("failed", "Изменений нет")
        return Verification("verified" if d.get("tests_ok") else "failed",
                            f"Ветка {d['branch']}: {len(d['commits'])} коммит(ов). {_tests_line(d.get('tests') or {})}")


def pending_branches() -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "branch", "--list", "improve/*", "--format=%(refname:short)"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [b.strip() for b in out.splitlines() if b.strip()]


def restart_later(delay: float = 2.0) -> None:
    """Restart this JARVIS on the new code: a detached helper stops it, waits until it is gone and starts it again
    (the windows reconnect and reload the new interface themselves)."""
    pid = os.getpid()
    py = sys.executable
    script = (f"sleep {delay}; kill -TERM {pid}; for i in $(seq 100); do kill -0 {pid} 2>/dev/null || break; "
              f"sleep 0.2; done; exec {py} -c 'from jarvis.__main__ import _cfg, ensure_core; p,c=_cfg(); "
              f"ensure_core(c,p)'")
    subprocess.Popen(["sh", "-c", script], cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


class SelfImproveApply(Tool):
    def __init__(self, config, restart=restart_later):
        self.config, self.restart = config, restart
        self.spec = ToolSpec(
            id="self_improve_apply", title="Применить улучшение JARVIS",
            description="Apply a tested self-improvement: merge its branch (improve/…) into JARVIS's main branch and "
                        "restart JARVIS on the new code. Only after self_improve reported that the tests passed.",
            params={"type": "object", "required": ["branch"], "properties": {
                "branch": {"type": "string", "maxLength": 120}}},
            risk=Risk.HIGH, side_effects="local", timeout_s=1200, idempotent=False, model_callable=True,
            verification="Тесты в ветке перепроверяются, слияние — коммит в main; затем перезапуск JARVIS",
            impact="Код JARVIS в основной ветке изменится, и JARVIS перезапустится (окна переподключатся сами). "
                   "Отменить можно через git revert.")

    def validate(self, args, ctx):
        a = Tool.validate(self, args, ctx)
        if not BRANCH.fullmatch(a["branch"]):
            raise ToolInputError("Это не ветка улучшения JARVIS")
        if a["branch"] not in pending_branches():
            raise ToolInputError("Такой ветки улучшения нет: " + a["branch"])
        return a

    def describe(self, args):
        try:
            stat = subprocess.run(["git", "-C", str(REPO), "diff", "--stat", f"main...{args['branch']}"],
                                  capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
        except (OSError, subprocess.SubprocessError):
            stat = []
        return f"Применить улучшение {args['branch']} и перезапустить JARVIS" + (f" ({stat[-1].strip()})" if stat else "")

    async def run(self, args, ctx):
        branch = args["branch"]
        code, status = await _git("status", "--porcelain", "--untracked-files=no")
        if code != 0 or status.strip():
            raise RuntimeError("В основной копии JARVIS есть несохранённые изменения — сначала их нужно закоммитить")
        _, cur = await _git("rev-parse", "--abbrev-ref", "HEAD")
        if cur.strip() != "main":
            raise RuntimeError(f"Основная копия не на ветке main (сейчас {cur.strip()})")
        wd = WORK / branch.split("/", 1)[1]
        ctx.progress("JARVIS перепроверяет тесты ветки", None)
        tests = await run_suite(wd) if wd.is_dir() else {}
        if wd.is_dir() and not _tests_ok(tests):
            raise RuntimeError("Тесты ветки не прошли — не применяю: " + _tests_line(tests))
        code, out = await _git("merge", "--no-ff", "--no-edit", "-m", f"Merge self-improvement {branch}", branch,
                               timeout=120)
        if code != 0:
            await _git("merge", "--abort")
            raise RuntimeError("Слияние не удалось (конфликт): " + out[-300:])
        _, head = await _git("rev-parse", "--short", "HEAD")
        if wd.is_dir():
            await _git("worktree", "remove", "--force", str(wd))
        await _git("branch", "-d", branch)
        self.restart()
        return ToolResult({"branch": branch, "head": head.strip(), "tests": tests},
                          f"Улучшение применено (коммит {head.strip()}). Перезапускаюсь на новом коде.")

    async def verify(self, args, result, ctx):
        code, _ = await _git("merge-base", "--is-ancestor", result.data["head"], "HEAD")
        return Verification("verified" if code == 0 else "failed", f"main содержит {result.data['head']}")
