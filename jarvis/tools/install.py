"""Installing programs into the system — the one kind of work that needs the administrator password.

JARVIS never sees or stores the password: `pkexec` makes the desktop's own polkit dialog ask the user. Only packages
from the official Arch Linux repositories (checked with `pacman -Si` before asking), never AUR builds or scripts."""
from __future__ import annotations

import re
import shutil
import subprocess

from .base import Risk, Tool, ToolInputError, ToolResult, ToolSpec, Verification, run_cmd

PKG_RE = re.compile(r"^[a-z0-9@_+][a-z0-9@._+-]{0,79}$")


def _pacman(*args: str, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(["pacman", *args], capture_output=True, text=True, timeout=timeout)


class SystemInstall(Tool):
    def __init__(self):
        self.spec = ToolSpec(
            id="system_install", title="Установка программ в систему",
            description=("Install programs system-wide from the official Arch Linux repositories (pacman): a compiler, "
                         "a runtime, a database, a desktop application that a task needs and that is not installed. "
                         "Give exact package names. The user confirms and types the administrator password in the "
                         "system's own dialog — you never see it. AUR packages are not supported. After it succeeds, "
                         "continue the original task (for example with computer_task)."),
            params={"type": "object", "required": ["packages"], "properties": {
                "packages": {"type": "array", "items": {"type": "string", "maxLength": 80}, "minItems": 1,
                             "maxItems": 20},
                "reason": {"type": "string", "maxLength": 300}}},
            risk=Risk.HIGH, side_effects="local", timeout_s=900, model_callable=True, idempotent=True,
            verification="pacman -Q: каждый пакет установлен",
            impact="Программы будут установлены в систему из официальных репозиториев Arch Linux. Система сама "
                   "попросит пароль администратора в своём окне — JARVIS пароль не видит и не хранит.")

    def validate(self, args, ctx):
        a = Tool.validate(self, args, ctx)
        if not shutil.which("pacman") or not shutil.which("pkexec"):
            raise ToolInputError("Установка в систему недоступна: нет pacman или pkexec")
        names = []
        for raw in a["packages"]:
            n = str(raw).strip().lower()
            if not PKG_RE.match(n):
                raise ToolInputError(f"Недопустимое имя пакета: {raw!r}")
            if n not in names:
                names.append(n)
        missing = [n for n in names if _pacman("-Si", n).returncode != 0]
        if missing:
            raise ToolInputError("Нет в официальных репозиториях Arch: " + ", ".join(missing) +
                                 " (возможно, это пакет AUR — его JARVIS не ставит)")
        todo = [n for n in names if _pacman("-Q", n).returncode != 0]
        if not todo:
            raise ToolInputError("Уже установлено: " + ", ".join(names))
        return {"packages": todo, "reason": str(a.get("reason") or "")[:300]}

    def describe(self, args):
        why = f" — {args['reason']}" if args.get("reason") else ""
        return f"Установить в систему: {', '.join(args['packages'])}{why}"

    async def run(self, args, ctx):
        ctx.progress("Ждёт пароль в системном окне", None)
        r = await run_cmd(["pkexec", "pacman", "-S", "--needed", "--noconfirm", *args["packages"]], timeout=900)
        out = (r.stdout + "\n" + r.stderr)[-1500:]
        if r.code in (126, 127):
            raise ToolInputError("Пароль не введён — установка отменена")
        if r.code != 0:
            if "failed retrieving file" in out or "404" in out:
                raise ToolInputError("Базы пакетов устарели — сначала обновите систему (Omarchy → Update), "
                                     "потом повторите")
            raise ToolInputError("pacman не смог установить: " + out.strip().splitlines()[-1][:200] if out.strip()
                                 else f"pacman завершился с кодом {r.code}")
        return ToolResult({"packages": args["packages"], "log_tail": out[-600:]},
                          "Установлено: " + ", ".join(args["packages"]) + ".")

    async def verify(self, args, result, ctx):
        bad = [n for n in args["packages"] if (await run_cmd(["pacman", "-Q", n], timeout=15)).code != 0]
        if bad:
            return Verification("failed", "Не установлены: " + ", ".join(bad))
        return Verification("verified", "pacman -Q подтверждает: " + ", ".join(args["packages"]))
