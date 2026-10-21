"""Text-to-speech engines.

PiperTTS: local, ~0.1–0.3 s per sentence, no quota, nothing leaves the computer.
Piper (GPL-3.0) runs as a separate, unmodified process from its own virtualenv
(~/.local/share/jarvis/piper); JARVIS talks to it over stdin/stderr only.
GeminiTTS: cloud fallback, higher latency (2–5 s per phrase measured).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

PIPER_HOME = Path(os.path.expanduser("~/.local/share/jarvis/piper"))
ACKS = ["Да, сейчас.", "Секунду.", "Сейчас проверю.", "Хорошо, ищу.", "Понял, делаю."]
NOT_HEARD = "Я не расслышал. Повтори, пожалуйста."
FAILED = "Не удалось получить ответ. Попробуйте ещё раз чуть позже."
CANNED = ACKS + [NOT_HEARD, FAILED]


def clean_for_speech(text: str) -> str:
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[*_`#>|]+", "", t)
    t = re.sub(r"\[уточнить[^\]]*\]", "уточнить", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


class PiperTTS:
    name = "piper"

    def __init__(self, voice: str = "ru_RU-denis-medium", home: Path = PIPER_HOME):
        self.home, self.voice = home, voice
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.outdir = Path(tempfile.mkdtemp(prefix="jarvis-tts-"))
        self.started_at: float | None = None
        self.cache: dict[str, bytes] = {}

    @property
    def python(self) -> Path:
        return self.home / ".venv" / "bin" / "python"

    @property
    def model(self) -> Path:
        return self.home / "voices" / f"{self.voice}.onnx"

    def available(self) -> bool:
        return self.python.exists() and self.model.exists()

    async def start(self) -> None:
        if self.proc and self.proc.returncode is None:
            return
        self.proc = await asyncio.create_subprocess_exec(
            str(self.python), "-m", "piper", "-m", str(self.model), "-d", str(self.outdir),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        self.started_at = time.time()

    async def synth(self, text: str) -> bytes:
        text = clean_for_speech(text).replace("\n", " ")
        if not text:
            return b""
        if text in self.cache:
            return self.cache[text]
        async with self.lock:                     # Piper handles one line at a time, in order
            await self.start()
            self.proc.stdin.write((text + "\n").encode())
            await self.proc.stdin.drain()
            while True:
                line = await asyncio.wait_for(self.proc.stderr.readline(), 30)
                if not line:
                    self.proc = None
                    raise RuntimeError("Piper остановился")
                s = line.decode("utf-8", "replace").strip()
                if "Wrote " in s:
                    path = Path(s.split("Wrote ", 1)[1].strip())
                    data = path.read_bytes()
                    path.unlink(missing_ok=True)
                    return data

    async def prewarm(self) -> None:
        await self.start()
        for a in CANNED:
            self.cache[clean_for_speech(a)] = await self.synth(a)

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        shutil.rmtree(self.outdir, ignore_errors=True)


class GeminiTTS:
    name = "gemini"

    def __init__(self, gemini):
        self.gemini = gemini
        self.cache: dict[str, bytes] = {}

    def available(self) -> bool:
        return self.gemini.configured()

    async def synth(self, text: str) -> bytes:
        text = clean_for_speech(text)
        return self.cache.get(text) or await self.gemini.speak(text)

    async def prewarm(self) -> None:
        return None

    async def stop(self) -> None:
        return None
