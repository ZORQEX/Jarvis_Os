"""The JARVIS window as something JARVIS can act on: show a workspace (Telegram connection, Task Center, Files…)
instead of telling the person where to click. If the window is closed, it is opened first."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from .base import Risk, Tool, ToolInputError, ToolResult, ToolSpec, Verification

VIEWS = {"home": "Главная", "chat": "Беседа", "tasks": "Задачи", "history": "История действий", "browser": "Браузер",
         "telegram": "Telegram", "whatsapp": "WhatsApp", "mail": "Почта", "files": "Файлы",
         "settings": "Настройки", "comms": "Сообщения: очередь и правила"}
ROOT = Path(__file__).resolve().parents[2]


def open_window() -> None:
    """`jarvis open` in the background: the launcher reuses an open window or starts one."""
    subprocess.Popen([sys.executable, "-m", "jarvis", "open"], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


class WindowShow(Tool):
    def __init__(self, bus, presence=lambda: False, opener=open_window, wait_s: float = 8.0):
        self.bus, self.presence, self.opener, self.wait_s = bus, presence, opener, wait_s
        self.hint = lambda view: ""       # what the shown workspace needs (set by the comms module: connect requirements)
        self.starter = None               # async view -> note: start connecting what JARVIS can start itself (WhatsApp QR)
        self.spec = ToolSpec(
            id="window_show", title="Раздел окна JARVIS",
            description=("Show a workspace of the JARVIS window to the user (opens the window if it is closed). Use it "
                         "instead of telling the user where to click: telegram / whatsapp / mail = connect that service "
                         "or see messages sent through JARVIS; tasks = task center; files; browser; settings; home; chat; "
                         "history; comms = message queue, rules, address book."),
            params={"type": "object", "required": ["view"], "properties": {
                "view": {"type": "string", "enum": sorted(VIEWS)},
                "connect": {"type": "boolean", "description": "telegram / whatsapp / mail: also start connecting the "
                            "service (WhatsApp shows its QR code at once)"}}},
            risk=Risk.LOW, side_effects="none", timeout_s=15, model_callable=True,
            verification="Окно JARVIS открыто и получило команду")

    def validate(self, args, ctx):
        a = Tool.validate(self, args, ctx)
        if a["view"] not in VIEWS:
            raise ToolInputError("Нет такого раздела: " + str(a["view"]))
        return a

    def describe(self, args):
        return f"Показать раздел «{VIEWS[args['view']]}»"

    async def run(self, args, ctx):
        opened = False
        if not self.presence():
            self.opener()
            opened = True
            for _ in range(int(self.wait_s / 0.2)):
                await asyncio.sleep(0.2)
                if self.presence():
                    await asyncio.sleep(0.4)          # the window applies its snapshot first
                    break
        started = await self.starter(args["view"]) if args.get("connect") and self.starter else ""
        self.bus.publish("ui.navigate", {"view": args["view"]})
        title = VIEWS[args["view"]]
        hint = " ".join(x for x in (started, self.hint(args["view"]) or "") if x)
        said = f"Открыл окно JARVIS на разделе «{title}»." if opened else f"Показал раздел «{title}»."
        return ToolResult({"view": args["view"], "window_opened": opened, "window_connected": self.presence(),
                           "connect_started": bool(started), "in_this_workspace": hint}, said + (" " + hint if hint else ""))

    async def verify(self, args, result, ctx):
        if result.data["window_connected"]:
            return Verification("verified", "Окно JARVIS открыто и получило команду")
        return Verification("failed", "Окно JARVIS не открылось")
