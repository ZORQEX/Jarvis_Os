"""Desktop tools for Hyprland/Omarchy: apps, windows, workspaces, volume,
screenshots, notifications. Each one calls a native CLI with argv arrays and
verifies the resulting state where the system exposes it."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import struct
import time
from pathlib import Path

from .base import (Risk, Tool, ToolContext, ToolError, ToolInputError, ToolResult, ToolSpec,
                   Verification, run_cmd)

# name aliases -> (argv, expected window class regex, display name)
DEFAULT_APPS: dict[str, tuple[list[str], str, str]] = {
    "browser": (["omarchy-launch-browser"], r"chrom|firefox|brave", "Браузер"),
    "terminal": (["xdg-terminal-exec"], r"kitty|alacritty|ghostty|foot", "Терминал"),
    "files": (["nautilus", "--new-window"], r"nautilus", "Файлы"),
    "calculator": (["gnome-calculator"], r"calculator", "Калькулятор"),
    "obsidian": (["obsidian"], r"obsidian", "Obsidian"),
    "editor": (["omarchy-launch-editor"], r"code|nvim|kitty|alacritty", "Редактор"),
    "vscode": (["code"], r"code", "VS Code"),
    "typora": (["typora"], r"typora", "Typora"),
    "signal": (["signal-desktop"], r"signal", "Signal"),
    "spotify": (["spotify"], r"spotify", "Spotify"),
    "writer": (["libreoffice", "--writer"], r"libreoffice|soffice", "LibreOffice Writer"),
    "calc": (["libreoffice", "--calc"], r"libreoffice|soffice", "LibreOffice Calc"),
    "monitor": (["xdg-terminal-exec", "btop"], r"kitty|alacritty|btop", "Монитор ресурсов"),
    "localsend": (["localsend"], r"localsend", "LocalSend"),
    "xournal": (["xournalpp"], r"xournal", "Xournal++"),
    # Messaging opens as Omarchy web apps (the user's own browser session; JARVIS does not touch it).
    "whatsapp": (["omarchy-launch-webapp", "https://web.whatsapp.com/"], r"whatsapp", "WhatsApp"),
    "telegram": (["omarchy-launch-webapp", "https://web.telegram.org/a/"], r"telegram", "Telegram"),
    "mail": (["omarchy-launch-webapp", "https://mail.google.com/"], r"mail\.google|gmail", "Почта (Gmail)"),
}

APP_ALIASES: dict[str, str] = {
    "браузер": "browser", "браузерр": "browser", "хром": "browser", "chrome": "browser", "chromium": "browser",
    "интернет": "browser", "browser": "browser",
    "терминал": "terminal", "консоль": "terminal", "terminal": "terminal", "kitty": "terminal",
    "файлы": "files", "проводник": "files", "файловый менеджер": "files", "files": "files", "nautilus": "files",
    "калькулятор": "calculator", "calculator": "calculator",
    "обсидиан": "obsidian", "obsidian": "obsidian",
    "редактор": "editor", "editor": "editor",
    "vs code": "vscode", "vscode": "vscode", "код": "vscode", "code": "vscode",
    "тайпора": "typora", "typora": "typora",
    "сигнал": "signal", "signal": "signal",
    "спотифай": "spotify", "spotify": "spotify", "музыку": "spotify", "музыка": "spotify",
    "ворд": "writer", "текстовый редактор": "writer", "writer": "writer", "word": "writer",
    "эксель": "calc", "таблицы": "calc", "excel": "calc", "calc": "calc",
    "монитор ресурсов": "monitor", "диспетчер задач": "monitor", "btop": "monitor",
    "localsend": "localsend", "локалсенд": "localsend",
    "xournal": "xournal", "ксорнал": "xournal",
    "ватсап": "whatsapp", "вотсап": "whatsapp", "вацап": "whatsapp", "whatsapp": "whatsapp", "whats app": "whatsapp",
    "телеграм": "telegram", "телеграмм": "telegram", "телегу": "telegram", "telegram": "telegram", "тг": "telegram",
    "почту": "mail", "почта": "mail", "gmail": "mail", "джимейл": "mail", "почтовый ящик": "mail",
}


def available_apps(extra: dict[str, list[str]] | None = None) -> dict[str, tuple[list[str], str, str]]:
    apps = {k: v for k, v in DEFAULT_APPS.items() if shutil.which(v[0][0])}
    for k, argv in (extra or {}).items():
        if argv and shutil.which(argv[0]):
            apps[k] = (argv, re.escape(Path(argv[0]).name), k)
    return apps


async def hypr_json(*args: str) -> object:
    r = await run_cmd(["hyprctl", "-j", *args], timeout=3)
    if r.code != 0:
        raise ToolError("Hyprland недоступен: " + (r.stderr or r.stdout).strip()[:200])
    return json.loads(r.stdout or "null")


def hypr_available() -> bool:
    import os
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) and shutil.which("hyprctl") is not None


class AppOpen(Tool):
    def __init__(self, extra: dict[str, list[str]] | None = None):
        self.apps = available_apps(extra)
        self.spec = ToolSpec(
            id="app_open", title="Открыть приложение",
            description="Launch an allowlisted desktop application. Allowed app ids: " + ", ".join(sorted(self.apps)),
            params={"type": "object", "required": ["app"],
                    "properties": {"app": {"type": "string", "enum": sorted(self.apps)}}},
            risk=Risk.LOW, side_effects="local", timeout_s=10, idempotent=False,
            verification="Новое окно ожидаемого класса появилось в Hyprland",
        )

    def describe(self, args):
        return f"Открыть «{self.apps[args['app']][2]}»"

    async def run(self, args, ctx):
        argv, _, name = self.apps[args["app"]]
        before = set()
        if hypr_available():
            before = {c["address"] for c in await hypr_json("clients")}
        launcher = ["uwsm-app", "--"] if shutil.which("uwsm-app") else []
        await run_cmd(launcher + argv, detach=True)
        return ToolResult({"app": args["app"], "name": name, "argv": argv, "_before": sorted(before)},
                          f"Открываю «{name}».", card="app")

    async def verify(self, args, result, ctx):
        if not hypr_available():
            return Verification("unverified", "Hyprland недоступен — появление окна не проверить")
        _, cls_re, name = self.apps[args["app"]]
        before = set(result.data.pop("_before", []))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            for c in await hypr_json("clients"):
                if c["address"] not in before and re.search(cls_re, c.get("class", ""), re.I):
                    result.data["window"] = {"class": c["class"], "title": c["title"], "workspace": c["workspace"]["id"]}
                    return Verification("verified", f"Окно «{c['class']}» появилось на рабочем столе {c['workspace']['id']}")
            await asyncio.sleep(0.25)
        return Verification("unverified", f"Новое окно «{name}» не обнаружено за 8 с — возможно, приложение уже было открыто")


class WindowList(Tool):
    spec = ToolSpec(
        id="window_list", title="Список окон",
        description="List open windows (application class, title, workspace) on the Hyprland desktop.",
        params={"type": "object", "properties": {}}, risk=Risk.LOW, side_effects="none", timeout_s=4,
        verification="Данные получены из Hyprland IPC",
    )

    def describe(self, args):
        return "открытые окна (Hyprland)"

    async def run(self, args, ctx):
        clients = await hypr_json("clients")
        wins = [{"class": c["class"], "title": c["title"][:120], "workspace": c["workspace"]["id"],
                 "focused_history": c.get("focusHistoryID", 99)} for c in clients if c.get("mapped", True)]
        wins.sort(key=lambda w: (w["workspace"], w["focused_history"]))
        n = len(wins)
        return ToolResult({"windows": wins}, f"Открыто окон: {n}.", card="windows")

    async def verify(self, args, result, ctx):
        return Verification("verified", self.spec.verification)


class WorkspaceSwitch(Tool):
    spec = ToolSpec(
        id="workspace_switch", title="Переключить рабочий стол",
        description="Switch to Hyprland workspace number 1-10.",
        params={"type": "object", "required": ["number"],
                "properties": {"number": {"type": "integer", "minimum": 1, "maximum": 10}}},
        risk=Risk.LOW, side_effects="local", timeout_s=4,
        verification="Активный рабочий стол прочитан из Hyprland после переключения",
    )

    def describe(self, args):
        return f"Перейти на рабочий стол {args['number']}"

    async def run(self, args, ctx):
        r = await run_cmd(["hyprctl", "dispatch", "workspace", str(args["number"])], timeout=3)
        if r.code != 0 or "ok" not in r.stdout:
            raise ToolError("Hyprland отказал: " + (r.stdout + r.stderr).strip()[:200])
        return ToolResult({"number": args["number"]}, f"Рабочий стол {args['number']}.", card="workspace")

    async def verify(self, args, result, ctx):
        ws = await hypr_json("activeworkspace")
        ok = ws.get("id") == args["number"]
        return Verification("verified" if ok else "failed",
                            f"Активен рабочий стол {ws.get('id')}")


_VOL_RE = re.compile(r"Volume:\s*([\d.]+)(\s*\[MUTED\])?")


async def read_volume() -> tuple[int, bool]:
    r = await run_cmd(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], timeout=3)
    m = _VOL_RE.search(r.stdout)
    if not m:
        raise ToolError("Не удалось прочитать громкость (PipeWire недоступен?)")
    return round(float(m.group(1)) * 100), bool(m.group(2))


class VolumeSet(Tool):
    spec = ToolSpec(
        id="volume_set", title="Громкость",
        description="Set system output volume. Give exactly one of: level (0-100 absolute percent), "
                    "delta (-50..50 relative percent), mute (true/false).",
        params={"type": "object", "properties": {
            "level": {"type": "integer", "minimum": 0, "maximum": 100},
            "delta": {"type": "integer", "minimum": -50, "maximum": 50},
            "mute": {"type": "boolean"}}},
        risk=Risk.LOW, side_effects="local", timeout_s=4,
        verification="Громкость прочитана из PipeWire (wpctl) после изменения",
    )

    def validate(self, args, ctx):
        a = super().validate(args, ctx)
        if sum(k in a for k in ("level", "delta", "mute")) != 1:
            raise ToolInputError("Укажите ровно одно: level, delta или mute")
        return a

    def describe(self, args):
        if "mute" in args:
            return "Выключить звук" if args["mute"] else "Включить звук"
        if "delta" in args:
            return f"{'Увеличить' if args['delta'] > 0 else 'Уменьшить'} громкость на {abs(args['delta'])}%"
        return f"Громкость {args['level']}%"

    async def run(self, args, ctx):
        sink = "@DEFAULT_AUDIO_SINK@"
        before, _ = await read_volume()
        target = None
        if "mute" in args:
            argv = ["wpctl", "set-mute", sink, "1" if args["mute"] else "0"]
        else:
            target = args["level"] if "level" in args else max(0, min(100, before + args["delta"]))
            argv = ["wpctl", "set-volume", "-l", "1.0", sink, f"{target / 100:.2f}"]
            if target > 0:
                await run_cmd(["wpctl", "set-mute", sink, "0"], timeout=3)
        r = await run_cmd(argv, timeout=3)
        if r.code != 0:
            raise ToolError("wpctl: " + r.stderr.strip()[:200])
        return ToolResult({"before": before, "target": target, **args}, self.describe(args) + ".",
                          card="volume")

    async def verify(self, args, result, ctx):
        vol, muted = await read_volume()
        result.data.update(after=vol, muted=muted)
        if "mute" in args:
            ok = muted == args["mute"]
        else:
            ok = abs(vol - result.data["target"]) <= 1
        return Verification("verified" if ok else "failed", f"Сейчас громкость {vol}%{' (звук выключен)' if muted else ''}")


def png_size(path: Path) -> tuple[int, int] | None:
    with open(path, "rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


class Screenshot(Tool):
    spec = ToolSpec(
        id="screenshot", title="Снимок экрана",
        description="Capture a screenshot of the currently focused monitor and save it as PNG in the user's "
                    "Pictures/Jarvis folder. Privacy-sensitive: requires user approval.",
        params={"type": "object", "properties": {}},
        risk=Risk.MODERATE, side_effects="local", timeout_s=8,
        verification="Файл существует, это корректный PNG с ненулевым размером",
        impact="Снимок всего текущего монитора сохранится в «Изображения/Jarvis». На нём может быть личная информация.",
    )

    def describe(self, args):
        return "Сделать снимок текущего монитора и сохранить в «Изображения/Jarvis»"

    async def run(self, args, ctx):
        if not shutil.which("grim"):
            raise ToolError("Утилита grim не установлена")
        out_dir = ctx.config.expand(ctx.config.screenshot_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / time.strftime("Снимок %Y-%m-%d %H-%M-%S.png")
        argv = ["grim"]
        if hypr_available():
            mons = await hypr_json("monitors")
            focused = next((m["name"] for m in mons if m.get("focused")), None)
            if focused:
                argv += ["-o", focused]
        r = await run_cmd(argv + [str(path)], timeout=6)
        if r.code != 0:
            raise ToolError("grim: " + r.stderr.strip()[:200])
        return ToolResult({"path": str(path)}, f"Снимок сохранён: {path.name}.", card="screenshot",
                          artifacts=[str(path)])

    async def verify(self, args, result, ctx):
        p = Path(result.data["path"])
        if not p.exists() or p.stat().st_size == 0:
            return Verification("failed", "Файл снимка не создан")
        size = png_size(p)
        if not size:
            return Verification("failed", "Файл не является PNG")
        result.data.update(width=size[0], height=size[1], bytes=p.stat().st_size)
        return Verification("verified", f"PNG {size[0]}×{size[1]}, {p.stat().st_size // 1024} КБ")


class Notify(Tool):
    spec = ToolSpec(
        id="notify", title="Уведомление",
        description="Show a desktop notification to the user.",
        params={"type": "object", "required": ["title"], "properties": {
            "title": {"type": "string", "maxLength": 120}, "body": {"type": "string", "maxLength": 500}}},
        risk=Risk.LOW, side_effects="local", timeout_s=4, idempotent=False,
        verification="notify-send принял уведомление (код 0)",
    )

    async def run(self, args, ctx):
        r = await run_cmd(["notify-send", "-a", "JARVIS", "--", args["title"], args.get("body", "")], timeout=3)
        if r.code != 0:
            raise ToolError("notify-send: " + r.stderr.strip()[:200])
        return ToolResult({"title": args["title"]}, "Уведомление показано.")

    async def verify(self, args, result, ctx):
        return Verification("verified", self.spec.verification)
