"""Text-to-speech: local only — nothing about speech leaves the computer and there is no quota.

SileroTTS (main voice): Silero v5.2 Russian model, ~0.25 s per sentence on this laptop's CPU (measured), much more
natural than Piper. Runs as a separate process from its own virtualenv (~/.local/share/jarvis/silero) via
jarvis/voice/silero_worker.py. Silero models: non-commercial licence (snakers4/silero-models).
PiperTTS (emergency fallback only, voice «Денис»): used if the Silero process cannot start or fails.
Piper (GPL-3.0) runs as a separate, unmodified process from ~/.local/share/jarvis/piper.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

PIPER_HOME = Path(os.path.expanduser("~/.local/share/jarvis/piper"))
SILERO_HOME = Path(os.path.expanduser("~/.local/share/jarvis/silero"))
SILERO_MODEL = "v5_2_ru.pt"
ACKS = ["Да, сейчас.", "Секунду.", "Сейчас проверю.", "Хорошо, ищу.", "Понял, делаю."]
NOT_HEARD = "Я не расслышал. Повтори, пожалуйста."
FAILED = "Не удалось получить ответ. Попробуйте ещё раз чуть позже."
CANNED = ACKS + [NOT_HEARD, FAILED]

# The voices offered in Settings → Голос: the five speakers of the Silero Russian model.
VOICES: list[dict] = [
    {"id": "silero:aidar", "speaker": "aidar", "name": "Айдар", "gender": "м", "desc": "Спокойный, уверенный"},
    {"id": "silero:eugene", "speaker": "eugene", "name": "Евгений", "gender": "м", "desc": "Мягкий, разговорный"},
    {"id": "silero:xenia", "speaker": "xenia", "name": "Ксения", "gender": "ж", "desc": "Чистый, дружелюбный"},
    {"id": "silero:kseniya", "speaker": "kseniya", "name": "Ксения (вторая)", "gender": "ж", "desc": "Ниже и строже"},
    {"id": "silero:baya", "speaker": "baya", "name": "Бая", "gender": "ж", "desc": "Звонкий, живой"},
]
DEFAULT_VOICE = "silero:aidar"
PREVIEW = "Привет! Я Джарвис. Так я буду звучать, когда отвечаю. Что сделать?"


def voice_info(vid: str) -> dict:
    return next((v for v in VOICES if v["id"] == vid), next(v for v in VOICES if v["id"] == DEFAULT_VOICE))


def clean_for_speech(text: str) -> str:
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[*_`#>|]+", "", t)
    t = re.sub(r"\[уточнить[^\]]*\]", "уточнить", t)
    t = re.sub(r"\b([01]?\d|2[0-3]):00\b", r"\1 ноль-ноль", t)            # 13:00 → «тринадцать ноль-ноль»
    t = re.sub(r"\b([01]?\d|2[0-3]):0(\d)\b", r"\1 ноль \2", t)
    t = re.sub(r"\b([01]?\d|2[0-3]):([1-5]\d)\b", r"\1 \2", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


class SileroTTS:
    name = "silero"

    def __init__(self, home: Path = SILERO_HOME, speaker: str = "aidar", rate: float = 1.0):
        self.home, self.speaker, self.rate = home, speaker, rate
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.cache: dict[tuple, bytes] = {}
        self._n = 0

    @property
    def python(self) -> Path:
        return self.home / ".venv" / "bin" / "python"

    @property
    def model(self) -> Path:
        return self.home / "models" / SILERO_MODEL

    def available(self) -> bool:
        return self.python.exists() and self.model.exists()

    async def start(self) -> None:
        if self.proc and self.proc.returncode is None:
            return
        worker = Path(__file__).with_name("silero_worker.py")
        self.proc = await asyncio.create_subprocess_exec(
            str(self.python), "-I", str(worker), str(self.model),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True, limit=1 << 20)
        line = await asyncio.wait_for(self.proc.stdout.readline(), 60)       # model load: ~2–4 s
        if not line or not json.loads(line).get("ready"):
            self.proc = None
            raise RuntimeError("Silero не запустился")

    async def synth(self, text: str, speaker: str | None = None, rate: float | None = None) -> bytes:
        text = clean_for_speech(text).replace("\n", " ")
        if not text:
            return b""
        key = (text, speaker or self.speaker, round(rate or self.rate, 2))
        if key in self.cache:
            return self.cache[key]
        async with self.lock:                     # one phrase at a time, in order
            await self.start()
            self._n += 1
            rid = f"r{self._n}"
            req = {"id": rid, "text": text, "speaker": key[1], "rate": key[2]}
            self.proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode())
            await self.proc.stdin.drain()
            while True:
                line = await asyncio.wait_for(self.proc.stdout.readline(), 30)
                if not line:
                    self.proc = None
                    raise RuntimeError("Silero остановился")
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("id") != rid:
                    continue
                if r.get("error"):
                    raise RuntimeError("Silero: " + r["error"])
                path = Path(r["path"])
                data = path.read_bytes()
                path.unlink(missing_ok=True)
                if len(text) < 80:
                    self.cache[key] = data
                return data

    async def prewarm(self) -> None:
        await self.start()
        for a in CANNED:
            await self.synth(a)

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        self.proc = None

    async def configure(self, speaker: str, rate: float) -> None:
        self.speaker, self.rate = speaker, rate      # per request: no restart needed


class PiperTTS:
    name = "piper"

    def __init__(self, voice: str = "ru_RU-denis-medium", home: Path = PIPER_HOME, rate: float = 1.0):
        self.home, self.voice, self.rate = home, voice, rate
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
            "--length-scale", f"{1.0 / max(0.6, min(1.6, self.rate)):.3f}",
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


class VoiceEngine:
    """What SpeechManager talks to: the chosen Silero voice; Piper «Денис» only if Silero fails (reported)."""

    def __init__(self, silero: SileroTTS, piper: PiperTTS | None = None, voice_id: str = DEFAULT_VOICE,
                 rate: float = 1.0):
        self.silero, self.piper = silero, piper
        v = voice_info(voice_id)
        self.voice_id, self.rate = v["id"], rate
        silero.speaker, silero.rate = v["speaker"], rate
        self.last_engine = "silero"
        self.last_fallback: str | None = None
        self.cache = silero.cache

    @property
    def name(self) -> str:
        return self.voice_id

    async def set_voice(self, voice_id: str, rate: float = 1.0) -> dict:
        v = voice_info(voice_id)
        self.voice_id, self.rate = v["id"], rate
        await self.silero.configure(v["speaker"], rate)
        return v

    async def synth(self, text: str) -> bytes:
        if self.silero.available():
            try:
                out = await self.silero.synth(text)
                self.last_engine, self.last_fallback = "silero", None
                return out
            except Exception as e:   # noqa: BLE001
                self.last_fallback = str(e)[:200]
                if not (self.piper and self.piper.available()):
                    raise
        elif not (self.piper and self.piper.available()):
            raise RuntimeError("Голос Silero не установлен (scripts/install.sh)")
        else:
            self.last_fallback = "Silero не установлен"
        self.last_engine = "piper"
        return await self.piper.synth(text)

    async def preview(self, voice_id: str, text: str = PREVIEW) -> bytes:
        v = voice_info(voice_id)
        return await self.silero.synth(text, speaker=v["speaker"], rate=self.rate)

    async def prewarm(self) -> None:
        if self.silero.available():
            await self.silero.prewarm()
        elif self.piper and self.piper.available():
            await self.piper.prewarm()

    async def stop(self) -> None:
        await self.silero.stop()
        if self.piper:
            await self.piper.stop()
