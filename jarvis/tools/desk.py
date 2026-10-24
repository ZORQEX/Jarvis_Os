"""Things on the desk: the clipboard, music and video players, text on the screen (ported from morph-rq/jarvis-os
capabilities/clipboard.py, media.py, visual.py, rewritten to JARVIS's tool contract).

- clipboard: read (a copy marked by a password manager is never read) or put text into it. What is read is the
  person's data: the AI may explain / translate / fix it; it is never sent anywhere by itself.
- media: MPRIS players through playerctl — play, pause, next, previous, what is playing.
- screen_text: the focused screen photographed (grim) and read on this computer (tesseract rus+eng); nothing uploaded.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile

from .base import Risk, Tool, ToolError, ToolInputError, ToolResult, ToolSpec, Verification, run_cmd, screen_locked

MAX_CLIP = 20_000


async def clip_types() -> list[str]:
    r = await run_cmd(["wl-paste", "--list-types"], timeout=3)
    return r.stdout.split() if r.code == 0 else []


class Clipboard(Tool):
    spec = ToolSpec(
        id="clipboard", title="Буфер обмена",
        description="Read the text the user copied (to explain, translate, fix or summarize it), or put text into the "
                    "clipboard for the user to paste. Copies from password managers are never read.",
        params={"type": "object", "required": ["action"], "properties": {
            "action": {"type": "string", "enum": ["read", "write"]},
            "text": {"type": "string", "maxLength": MAX_CLIP}}},
        risk=Risk.LOW, side_effects="local", timeout_s=6, verification="Буфер прочитан обратно")

    def validate(self, args, ctx):
        a = super().validate(args, ctx)
        if not shutil.which("wl-paste"):
            raise ToolInputError("wl-clipboard не установлен")
        if a["action"] == "write" and not a.get("text"):
            raise ToolInputError("Что положить в буфер?")
        return a

    def describe(self, args):
        return "Прочитать буфер обмена" if args["action"] == "read" else "Положить текст в буфер обмена"

    async def run(self, args, ctx):
        if args["action"] == "write":
            proc = await asyncio.create_subprocess_exec("wl-copy", stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                                                        start_new_session=True)
            await asyncio.wait_for(proc.communicate(args["text"].encode()), 5)
            return ToolResult({"chars": len(args["text"])}, f"Положил в буфер ({len(args['text'])} символов) — можно вставлять.")
        types = await clip_types()
        if any("password" in t.lower() or "secret" in t.lower() for t in types):
            raise ToolError("Это скопировано из менеджера паролей — читать не буду")
        if not any(t.startswith("text/") or t in ("UTF8_STRING", "STRING", "TEXT") for t in types):
            what = "картинка" if any(t.startswith("image/") for t in types) else "не текст" if types else "пусто"
            return ToolResult({"text": "", "types": types}, f"В буфере {what}.")
        r = await run_cmd(["wl-paste", "--no-newline", "--type", "text"], timeout=4, max_out=MAX_CLIP * 4)
        text = r.stdout[:MAX_CLIP]
        preview = text.strip().replace("\n", " ")
        return ToolResult({"text": text, "chars": len(text)},
                          f"В буфере ({len(text)} символов): «{preview[:200]}{'…' if len(preview) > 200 else ''}»",
                          untrusted=True)

    async def verify(self, args, result, ctx):
        if args["action"] == "read":
            return Verification("verified", "Прочитано из буфера")
        r = await run_cmd(["wl-paste", "--no-newline", "--type", "text"], timeout=4, max_out=MAX_CLIP * 4)
        return Verification("verified" if r.stdout == args["text"] else "failed", self.spec.verification)


async def player_state() -> dict:
    if not shutil.which("playerctl"):
        return {}
    r = await run_cmd(["playerctl", "metadata", "--format",
                       '{"status":"{{status}}","player":"{{playerName}}","artist":"{{markup_escape(artist)}}",'
                       '"title":"{{markup_escape(title)}}"}'], timeout=4)
    try:
        return json.loads(r.stdout) if r.code == 0 and r.stdout.strip() else {}
    except ValueError:
        return {}


class Media(Tool):
    spec = ToolSpec(
        id="media", title="Музыка и видео",
        description="Control the music / video player (Spotify, a browser tab, mpv…): play, pause, toggle, next, "
                    "previous, or say what is playing.",
        params={"type": "object", "required": ["action"], "properties": {
            "action": {"type": "string", "enum": ["play", "pause", "toggle", "next", "previous", "status"]}}},
        risk=Risk.LOW, side_effects="local", timeout_s=6, verification="Состояние плеера прочитано (playerctl)")

    def describe(self, args):
        return {"play": "Включить музыку", "pause": "Пауза", "toggle": "Пауза / продолжить", "next": "Следующий трек",
                "previous": "Предыдущий трек", "status": "Что играет"}[args["action"]]

    async def run(self, args, ctx):
        if not shutil.which("playerctl"):
            raise ToolError("playerctl не установлен")
        before = await player_state()
        if not before:
            raise ToolError("Сейчас нет открытого плеера (музыка или видео)")
        a = args["action"]
        if a != "status":
            cmd = {"play": "play", "pause": "pause", "toggle": "play-pause", "next": "next", "previous": "previous"}[a]
            r = await run_cmd(["playerctl", cmd], timeout=4)
            if r.code != 0:
                raise ToolError("playerctl: " + (r.stderr or r.stdout).strip()[:200])
            await asyncio.sleep(0.4)
        st = await player_state() or before
        what = " — ".join(x for x in (st.get("artist"), st.get("title")) if x) or st.get("player", "плеер")
        status = {"Playing": "играет", "Paused": "на паузе", "Stopped": "остановлено"}.get(st.get("status"), st.get("status"))
        text = {"pause": "Пауза.", "play": f"Играет: {what}.", "next": f"Дальше: {what}.", "previous": f"Назад: {what}.",
                "toggle": f"{what}: {status}.", "status": f"Сейчас {status}: {what}."}[a]
        return ToolResult({"before": before, "after": st}, text)

    async def verify(self, args, result, ctx):
        a, st = args["action"], result.data["after"]
        ok = {"play": st.get("status") == "Playing", "pause": st.get("status") != "Playing",
              "next": st.get("title") != result.data["before"].get("title") or st.get("status") == "Playing",
              "previous": True, "toggle": st.get("status") != result.data["before"].get("status"), "status": True}[a]
        return Verification("verified" if ok else "unverified", self.spec.verification)


class ScreenText(Tool):
    spec = ToolSpec(
        id="screen_text", title="Прочитать текст с экрана",
        description="Read the text on the focused screen on this computer (screenshot + Tesseract OCR, Russian and "
                    "English); nothing leaves the computer. Use it to answer «что написано на экране», or to quote an "
                    "error message.",
        params={"type": "object", "properties": {}}, risk=Risk.LOW, side_effects="none", timeout_s=40,
        verification="Распознано Tesseract на этом компьютере")

    async def run(self, args, ctx):
        if not (shutil.which("grim") and shutil.which("tesseract")):
            raise ToolError("Нужны grim и tesseract (с русским языком: tesseract-data-rus)")
        if screen_locked():
            raise ToolError("Экран заблокирован — прочитать его можно после разблокировки")
        r = await run_cmd(["hyprctl", "monitors", "-j"], timeout=3)
        out = None
        try:
            out = next((m["name"] for m in json.loads(r.stdout) if m.get("focused")), None)
        except (ValueError, TypeError):
            pass
        fd, png = tempfile.mkstemp(prefix="jarvis-ocr-", suffix=".png")
        os.close(fd)
        try:
            shot = await run_cmd(["grim", *(["-o", out] if out else []), png], timeout=10)
            if shot.code != 0:
                raise ToolError("grim: " + shot.stderr.strip()[:200])
            langs = await run_cmd(["tesseract", "--list-langs"], timeout=5)
            lang = "+".join(x for x in ("rus", "eng") if x in langs.stdout.split()) or "eng"
            ocr = await run_cmd(["tesseract", png, "-", "-l", lang, "--psm", "3"], timeout=35, max_out=400_000)
            if ocr.code != 0:
                raise ToolError("tesseract: " + ocr.stderr.strip()[:200])
        finally:
            os.unlink(png)
        text = "\n".join(line for line in ocr.stdout.splitlines() if line.strip())[:30_000]
        words = len(text.split())
        return ToolResult({"text": text, "words": words, "lang": lang},
                          f"На экране {words} слов. Начало: «{text[:240].replace(chr(10), ' ')}»" if words else
                          "Текста на экране не нашёл.", untrusted=True)

    async def verify(self, args, result, ctx):
        return Verification("verified", self.spec.verification)


TOOLS = [Clipboard, Media, ScreenText]
