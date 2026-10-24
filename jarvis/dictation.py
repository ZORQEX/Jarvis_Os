"""Dictation: what the person says is typed into the window they were working in (wtype), instead of being answered.

Started by «Джарвис, диктовка» or Super+Alt+D (`jarvis dictate`); «стоп диктовка» or Super+Alt+D again ends it. The
desktop companion listens meanwhile (its microphone opens even if wake listening is off) and the recogniser runs on
this computer. A phrase that starts with «Джарвис» is still a command and is not typed. «Новая строка» / «новый
абзац» press Enter — except in a terminal, where Enter would run a command. Only the person's own speech is typed:
the AI has no tool that types (idea from morph-rq/jarvis-os, written for JARVIS's voice pipeline).
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import time

from .tools.base import run_cmd, screen_locked

STOP = re.compile(r"^\s*(?:джарвис[,\s]*)?(?:стоп|хватит|закончи\w*|заверши\w*|конец|выключи|останови|прекрати)\w*[,\s]+"
                  r"(?:диктовк\w*|диктовать)\b|^\s*(?:конец|стоп) диктовки\b", re.I)
NEWLINE = re.compile(r"[,\s]*\b(новая строка|с новой строки|новый абзац|абзац)[.!]?\s*$", re.I)
TERMINALS = {"kitty", "alacritty", "com.mitchellh.ghostty", "ghostty", "foot", "footclient", "org.wezfurlong.wezterm",
             "xterm", "konsole", "org.gnome.console", "gnome-terminal-server", "terminator"}
JARVIS_WINDOWS = re.compile(r"chrome-localhost__|JARVIS")


async def _json(argv: list[str]) -> object:
    r = await run_cmd(argv, timeout=3)
    try:
        return json.loads(r.stdout) if r.code == 0 else None
    except ValueError:
        return None


class Dictation:
    def __init__(self, bus=None, audit=None):
        self.bus, self.audit = bus, audit
        self.on = False
        self.target: dict | None = None        # {"address", "class", "title"} of the window typed into
        self.chars = 0
        self.since = 0.0
        self.at_line_start = True
        self.lock = asyncio.Lock()

    def available(self) -> bool:
        return bool(shutil.which("wtype") and shutil.which("hyprctl"))

    def state(self) -> dict:
        return {"on": self.on, "title": (self.target or {}).get("title", ""), "chars": self.chars}

    def _publish(self) -> None:
        if self.bus:
            self.bus.publish("dictation", self.state())

    async def start(self) -> str:
        if not self.available():
            return "Для диктовки нужны wtype и Hyprland."
        w = await _json(["hyprctl", "activewindow", "-j"]) or {}
        # Started from JARVIS's own window or companion: type into whatever window is in focus when speaking.
        self.target = None if not w or JARVIS_WINDOWS.search(f"{w.get('class', '')} {w.get('title', '')}") else \
            {"address": w.get("address"), "class": w.get("class", ""), "title": (w.get("title") or "")[:60]}
        self.on, self.chars, self.since, self.at_line_start = True, 0, time.time(), True
        self._publish()
        if self.audit:
            self.audit.write("dictation.started", window=(self.target or {}).get("class", ""))
        where = f"в «{self.target['title']}»" if self.target and self.target["title"] else "в окно, где курсор"
        return f"Диктуйте — печатаю {where}. Закончить: «стоп диктовка» или Super+Alt+D."

    async def stop(self) -> str:
        if not self.on:
            return "Диктовка не включена."
        self.on = False
        self._publish()
        if self.audit:
            self.audit.write("dictation.stopped", chars=self.chars, s=round(time.time() - self.since))
        return f"Диктовка закончена: напечатано {self.chars} символов." if self.chars else "Диктовка закончена."

    async def toggle(self) -> str:
        return await (self.stop() if self.on else self.start())

    @staticmethod
    def is_stop(text: str) -> bool:
        return bool(STOP.search(text or ""))

    def parts(self, text: str, terminal: bool) -> list[tuple[str, str]]:
        """What to send: ("text", s) and ("key", "Return"); a space between phrases, none after a new line."""
        t = re.sub(r"\s+", " ", (text or "").strip())
        newline = NEWLINE.search(t)
        if newline:
            t = t[:newline.start()].rstrip()
        out: list[tuple[str, str]] = []
        if t:
            out.append(("text", ("" if self.at_line_start else " ") + t))
        if newline and not terminal:
            out.append(("key", "Return"))
            if "абзац" in newline.group(1).lower():
                out.append(("key", "Return"))
        return out

    async def type(self, text: str) -> int:
        """Type a dictated phrase into the target window (focusing it back first if the person moved away)."""
        async with self.lock:
            if screen_locked():
                # The lock screen has the focus: the words would go into its password field.
                raise RuntimeError("экран заблокирован")
            terminal = False
            if self.target:
                clients = await _json(["hyprctl", "clients", "-j"]) or []
                if not any(c.get("address") == self.target["address"] for c in clients):
                    self.target = None                      # the window was closed: type where the focus is
            if self.target:
                terminal = self.target["class"].lower() in TERMINALS
                active = await _json(["hyprctl", "activewindow", "-j"]) or {}
                if active.get("address") != self.target["address"]:
                    await run_cmd(["hyprctl", "dispatch", "focuswindow", f"address:{self.target['address']}"], timeout=3)
                    await asyncio.sleep(0.15)
            else:
                active = await _json(["hyprctl", "activewindow", "-j"]) or {}
                terminal = str(active.get("class", "")).lower() in TERMINALS
            typed = 0
            for kind, value in self.parts(text, terminal):
                if kind == "text":
                    r = await run_cmd(["wtype", "--", value], timeout=20)
                    if r.code != 0:
                        raise RuntimeError("wtype: " + (r.stderr or r.stdout).strip()[:200])
                    typed += len(value)
                    self.at_line_start = False
                else:
                    await run_cmd(["wtype", "-k", value], timeout=5)
                    self.at_line_start = True
            self.chars += typed
            self._publish()
            return typed
