"""Quick file actions for the conversation: make a folder, write a text file, list a folder, move to the trash, and
read-only terminal commands. A model call plus milliseconds — instead of a whole Claude Code run for «создай папку».

Everything stays inside the folders JARVIS may work in (the home folder when the user allowed it) and never touches
keys, passwords, sessions or browser profiles. Overwriting keeps a backup; deleting goes to the Trash and always asks."""
from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import time
from pathlib import Path

from .base import Risk, Tool, ToolInputError, ToolResult, ToolSpec, Verification, is_secret_path, run_cmd

DESKTOP = "~/Desktop"     # «рабочий стол»: Omarchy has no desktop folder of its own; ~/Desktop is created when needed


def _roots(config) -> list[Path]:
    from ..agency.planner import claude_roots
    return claude_roots(config)


def resolve_target(raw: str, config) -> Path:
    """A path that may not exist yet, inside the allowed folders, never a secret one."""
    if not raw or "\x00" in raw:
        raise ToolInputError("Пустой путь")
    raw = raw.strip()
    for word in ("рабочий стол/", "рабочем столе/", "Desktop/"):
        if raw.startswith(word):
            raw = DESKTOP + "/" + raw[len(word):]
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        raise ToolInputError("Нужен полный путь (например, ~/Desktop/Папка)")
    rp = p.resolve()
    if is_secret_path(rp):
        raise ToolInputError("Эта папка закрыта для JARVIS: там ключи, пароли или данные входа")
    if not any(rp == r or rp.is_relative_to(r) for r in _roots(config)):
        raise ToolInputError("Путь вне разрешённых папок: " + str(p))
    return rp


def _short(p) -> str:
    return str(p).replace(os.path.expanduser("~"), "~", 1)


class FolderCreate(Tool):
    def __init__(self):
        self.spec = ToolSpec(
            id="folder_create", title="Создать папку",
            description="Create a folder (and missing parents). Desktop = ~/Desktop. Use a full path.",
            params={"type": "object", "required": ["path"], "properties": {"path": {"type": "string", "maxLength": 500}}},
            risk=Risk.MODERATE, side_effects="local", timeout_s=5, verification="Папка существует после создания",
            impact="Появится новая папка. Существующие файлы не меняются.")

    def validate(self, args, ctx):
        return {"path": str(resolve_target(Tool.validate(self, args, ctx)["path"], ctx.config))}

    def describe(self, args):
        return f"Создать папку {_short(args['path'])}"

    def paths(self, args):
        return [(Path(args["path"]), "write")]

    async def run(self, args, ctx):
        p = Path(args["path"])
        existed = p.is_dir()
        p.mkdir(parents=True, exist_ok=True)
        return ToolResult({"path": str(p), "existed": existed},
                          f"Папка {'уже была' if existed else 'создана'}: {_short(p)}.", artifacts=[str(p)])

    async def verify(self, args, result, ctx):
        ok = Path(args["path"]).is_dir()
        return Verification("verified" if ok else "failed", "Папка на месте" if ok else "Папки нет после создания")


class FileWrite(Tool):
    def __init__(self, config):
        self.config = config
        self.spec = ToolSpec(
            id="file_write", title="Записать текстовый файл",
            description=("Create a text file, or append to / overwrite one, with exactly the given text. mode: create "
                         "(default; refuses to replace a non-empty file), append, overwrite (keeps a backup). "
                         "Use a full path; Desktop = ~/Desktop."),
            params={"type": "object", "required": ["path", "text"], "properties": {
                "path": {"type": "string", "maxLength": 500}, "text": {"type": "string", "maxLength": 200000},
                "mode": {"type": "string", "enum": ["create", "append", "overwrite"]}}},
            risk=Risk.MODERATE, side_effects="local", timeout_s=10, idempotent=False,
            verification="Содержимое на диске совпадает с записанным",
            impact="Файл будет создан или дополнен. Если он перезаписывается — прежняя версия сохраняется в резервную копию.")

    def validate(self, args, ctx):
        a = Tool.validate(self, args, ctx)
        p = resolve_target(a["path"], ctx.config)
        mode = a.get("mode") or "create"
        if p.is_dir():
            raise ToolInputError("Это папка, а не файл: " + _short(p))
        if mode == "create" and p.exists() and p.stat().st_size > 0:
            raise ToolInputError(f"Файл {_short(p)} уже есть и не пустой — скажите, дописать или перезаписать")
        return {"path": str(p), "text": a["text"], "mode": mode}

    def describe(self, args):
        verb = {"create": "Записать файл", "append": "Дописать в файл", "overwrite": "Перезаписать файл"}[args["mode"]]
        return f"{verb} {_short(args['path'])} ({len(args['text'])} симв.)"

    def paths(self, args):
        return [(Path(args["path"]), "write")]

    async def run(self, args, ctx):
        p = Path(args["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if args["mode"] == "overwrite" and p.exists() and p.stat().st_size > 0:
            bdir = Path(os.path.expanduser(self.config.write_dir)).resolve().parent / ".jarvis-backups" \
                if os.environ.get("JARVIS_HOME") else Path(os.path.expanduser("~/.local/share/jarvis/backups"))
            bdir.mkdir(parents=True, exist_ok=True)
            backup = bdir / f"{time.strftime('%Y%m%d-%H%M%S')}-{p.name}"
            shutil.copy2(p, backup)
        with open(p, "a" if args["mode"] == "append" else "w", encoding="utf-8") as f:
            f.write(args["text"])
        body = p.read_bytes()
        return ToolResult({"path": str(p), "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                           "backup": str(backup) if backup else None},
                          f"Файл записан: {_short(p)}.", artifacts=[str(p)])

    async def verify(self, args, result, ctx):
        p = Path(args["path"])
        if not p.is_file():
            return Verification("failed", "Файла нет после записи")
        body = p.read_bytes()
        ok = hashlib.sha256(body).hexdigest() == result.data["sha256"] and args["text"].encode() in body
        return Verification("verified" if ok else "failed", "Текст на диске совпадает" if ok else "Текст на диске другой")


class FolderList(Tool):
    def __init__(self):
        self.spec = ToolSpec(
            id="folder_list", title="Содержимое папки",
            description="List what is inside a folder (names, types, sizes, dates). Desktop = ~/Desktop.",
            params={"type": "object", "required": ["path"], "properties": {
                "path": {"type": "string", "maxLength": 500}, "hidden": {"type": "boolean"}}},
            risk=Risk.LOW, side_effects="none", timeout_s=10, verification="Список прочитан с диска в момент запроса")

    def validate(self, args, ctx):
        a = Tool.validate(self, args, ctx)
        p = resolve_target(a["path"], ctx.config)
        if not p.is_dir():
            raise ToolInputError("Папка не найдена: " + _short(p))
        return {"path": str(p), "hidden": bool(a.get("hidden"))}

    def describe(self, args):
        return f"Содержимое {_short(args['path'])}"

    async def run(self, args, ctx):
        p = Path(args["path"])
        items = []
        for c in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.casefold())):
            if c.name.startswith(".") and not args["hidden"]:
                continue
            if is_secret_path(c):
                continue
            st = c.stat()
            items.append({"name": c.name, "dir": c.is_dir(), "size": None if c.is_dir() else st.st_size,
                          "modified": st.st_mtime})
            if len(items) >= 300:
                break
        names = ", ".join((i["name"] + ("/" if i["dir"] else "")) for i in items[:40]) or "пусто"
        return ToolResult({"path": str(p), "entries": items, "count": len(items)},
                          f"В {_short(p)} {len(items)} элем.: {names}.", untrusted=True)

    async def verify(self, args, result, ctx):
        return Verification("verified", f"Прочитано с диска: {result.data['count']} элементов")


class FileTrash(Tool):
    def __init__(self):
        self.spec = ToolSpec(
            id="file_trash", title="Удалить в корзину",
            description="Delete a file or folder by moving it to the Trash (can be restored). Always confirmed.",
            params={"type": "object", "required": ["path"], "properties": {"path": {"type": "string", "maxLength": 500}}},
            risk=Risk.HIGH, side_effects="local", timeout_s=30, idempotent=False,
            verification="Пути больше нет, объект в корзине",
            impact="Файл или папка уйдут в корзину — их можно восстановить оттуда.")

    def validate(self, args, ctx):
        p = resolve_target(Tool.validate(self, args, ctx)["path"], ctx.config)
        if not p.exists():
            raise ToolInputError("Такого файла или папки нет: " + _short(p))
        if p in _roots(ctx.config) or p == Path.home().resolve():
            raise ToolInputError("Корневую папку удалять нельзя")
        return {"path": str(p)}

    def describe(self, args):
        return f"Удалить в корзину: {_short(args['path'])}"

    def paths(self, args):
        return [(Path(args["path"]), "write")]

    async def run(self, args, ctx):
        r = await run_cmd(["gio", "trash", args["path"]], timeout=30)
        if r.code != 0:
            raise ToolInputError("Не удалось переместить в корзину: " + (r.stderr or r.stdout).strip()[:200])
        return ToolResult({"path": args["path"]}, f"В корзине: {_short(args['path'])}.")

    async def verify(self, args, result, ctx):
        gone = not Path(args["path"]).exists()
        return Verification("verified" if gone else "failed", "Перемещено в корзину" if gone else "Объект всё ещё на месте")


# Read-only terminal: the program and its arguments are checked; no shell, so pipes and redirections are plain text.
SHELL_OK = {"ls", "cat", "head", "tail", "wc", "df", "du", "free", "uptime", "uname", "whoami", "date", "ps", "pwd",
            "which", "file", "stat", "grep", "tree", "lsblk", "nproc", "lscpu", "lspci", "lsusb", "sensors", "echo",
            "hostnamectl", "xdg-user-dir", "md5sum", "sha256sum", "diff", "id", "env", "printenv"}
SUBCOMMANDS = {"git": {"status", "log", "diff", "branch", "show", "remote"}, "pacman": {"-Q", "-Qi", "-Ql", "-Si", "-Ss"},
               "systemctl": {"status", "is-active", "is-enabled", "list-units", "list-timers"},
               "journalctl": None, "find": None, "ip": {"addr", "a", "route", "link"}, "docker": {"ps", "images"}}
FORBIDDEN_ARGS = {"-delete", "-exec", "-execdir", "-ok", "-fprint", "--output", "-o"}


class ShellRead(Tool):
    def __init__(self):
        self.spec = ToolSpec(
            id="shell_read", title="Терминал (только чтение)",
            description=("Run one read-only terminal command and get its output: ls, cat, head, tail, wc, df, du, free, "
                         "ps, uname, date, grep, tree, find (no -exec/-delete), git status/log/diff, pacman -Q, "
                         "systemctl status, journalctl, ip addr… No pipes or redirections. For changes use the other "
                         "tools or computer_task."),
            params={"type": "object", "required": ["command"], "properties": {
                "command": {"type": "string", "maxLength": 500}, "folder": {"type": "string", "maxLength": 500}}},
            risk=Risk.LOW, side_effects="none", timeout_s=20, verification="Код выхода и вывод команды")

    def validate(self, args, ctx):
        a = Tool.validate(self, args, ctx)
        try:
            argv = [os.path.expanduser(x) if x.startswith("~") else x for x in shlex.split(a["command"])]
        except ValueError as e:
            raise ToolInputError("Не разобрал команду: " + str(e)) from None
        if not argv:
            raise ToolInputError("Пустая команда")
        prog = argv[0]
        if prog in ("printenv", "env"):
            raise ToolInputError("Переменные окружения не показываю: там бывают ключи")
        if prog not in SHELL_OK and prog not in SUBCOMMANDS:
            raise ToolInputError(f"Команда «{prog}» не только для чтения — для изменений нужен другой инструмент")
        allowed = SUBCOMMANDS.get(prog)
        if allowed is not None and (len(argv) < 2 or argv[1] not in allowed):
            raise ToolInputError(f"Для {prog} разрешено только: {', '.join(sorted(allowed))}")
        if any(x in FORBIDDEN_ARGS for x in argv[1:]) or any(c in a["command"] for c in ("|", ">", "<", ";", "&&", "`", "$(")):
            raise ToolInputError("Только одна команда для чтения: без -exec/-delete, каналов и перенаправлений")
        for x in argv[1:]:
            if x.startswith(("/", "~", ".")) or "/" in x:
                p = Path(os.path.expanduser(x)).resolve()
                if is_secret_path(p):
                    raise ToolInputError("Эта папка закрыта для JARVIS: там ключи, пароли или данные входа")
        folder = str(resolve_target(a["folder"], ctx.config)) if a.get("folder") else str(Path.home())
        return {"command": a["command"], "argv": argv, "folder": folder}

    def describe(self, args):
        return f"Терминал: {args['command'][:120]}"

    async def run(self, args, ctx):
        r = await run_cmd(args["argv"], timeout=15, max_out=60_000, cwd=args["folder"])
        out = (r.stdout or "")[:20000]
        err = (r.stderr or "")[:2000]
        return ToolResult({"command": args["command"], "exit_code": r.code, "stdout": out, "stderr": err},
                          f"Команда выполнена (код {r.code}).", untrusted=True)

    async def verify(self, args, result, ctx):
        c = result.data["exit_code"]
        return Verification("verified" if c == 0 else "failed",
                            "Код выхода 0" if c == 0 else f"Код выхода {c}: {result.data['stderr'][:200]}")
