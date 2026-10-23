"""Local speech recognition as a separate process. Nothing leaves the computer, no quota.

GigaAM v3 for Russian; Parakeet v3 re-hears phrases that are not Russian (English and 24 other European languages) —
see stt_worker.py. This is the only speech recognition in JARVIS (there is no cloud fallback)."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from .tts import WorkerDied, _kill

STT_HOME = Path(os.path.expanduser("~/.local/share/jarvis/stt"))
MODEL = "gigaam-v3-e2e-rnnt"
MULTI = "nemo-parakeet-tdt-0.6b-v3"
MULTI_DIR = "models--istupakov--parakeet-tdt-0.6b-v3-onnx"
LEXICON = "ru_50k.txt"          # Russian word frequencies (hermitdave/FrequencyWords, CC-BY-SA-4.0)
# Words of Russian commands that the frequency list lacks (apps, services, JARVIS itself, frequent verbs): a phrase
# made of them is Russian and is not re-heard by the second model.
VOCAB = """джарвис джарвиса джарвису джарвисом телеграм телеграме телеграму телеграмм телеграмме телеграма ватсап
ватсапе ватсапу вотсап вотсапе вотсапу вацап вацапе ютуб ютубе ютуба гугл гугле гуглу джимейл джимейле почту
омарчи хайпрленд вайфай блютуз блютуза спотифай яндекс яндексе браузер браузере хром хроме терминал терминале
подключи подключись подключить отключи напомни открой закрой скинь отправь напиши набери включи выключи сделай
создай удали перенеси переименуй скачай установи найди покажи прочитай озвучь громче тише скриншот скриншоте
гитхаб гитхабе гитхаба навык навыки навыка джорис джарыс джарвес чарвис джарвиз жарвис джервис джарис джавис""".split()


class LocalSTT:
    name = MODEL
    worker = Path(__file__).with_name("stt_worker.py")

    def __init__(self, home: Path = STT_HOME):
        self.home = home
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self._n = 0
        self.mode = "auto"              # "auto": Russian + other languages; "ru": Russian only (less memory)
        self.vocab: list[str] = []      # names and brands JARVIS knows: Russian phrases with them are not re-heard
        self.last_model = "gigaam"

    @property
    def python(self) -> Path:
        return self.home / ".venv" / "bin" / "python"

    def available(self) -> bool:
        return self.python.exists() and (self.home / "hf").exists()

    def multilingual(self) -> bool:
        return (self.home / "hf" / "hub" / MULTI_DIR).is_dir()

    async def start(self) -> None:
        if self.proc and self.proc.returncode is None:
            return
        env = {**{k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET"))},
               "HF_HOME": str(self.home / "hf"), "HF_HUB_OFFLINE": "1", "JARVIS_STT_MODEL": MODEL,
               "JARVIS_STT_MULTI": MULTI if self.mode != "ru" and self.multilingual() else "none",
               "JARVIS_STT_LEXICON": str(self.home / LEXICON)}
        self.proc = await asyncio.create_subprocess_exec(
            str(self.python), "-I", str(self.worker), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True, limit=1 << 20)
        line = await asyncio.wait_for(self.proc.stdout.readline(), 120)        # model load: ~3–25 s
        if not line or not json.loads(line).get("ready"):
            self.proc = None
            raise RuntimeError("Локальное распознавание не запустилось")
        if self.vocab:
            await self._send({"vocab": self.vocab})

    async def set_mode(self, mode: str) -> None:
        mode = "ru" if mode == "ru" else "auto"
        if mode != self.mode:
            self.mode = mode
            async with self.lock:
                await self.stop()          # the next phrase starts the worker with (or without) the second model

    async def add_vocab(self, words) -> None:
        new = [w for w in {str(w).casefold() for w in words if w} if w not in set(self.vocab)]
        if not new:
            return
        self.vocab += new
        if self.proc and self.proc.returncode is None:
            async with self.lock:
                await self._send({"vocab": new})

    async def transcribe(self, wav: bytes) -> str:
        if wav[:4] != b"RIFF":
            raise ValueError("локальное распознавание принимает только WAV")
        async with self.lock:
            fd, path = tempfile.mkstemp(prefix="jarvis-stt-", suffix=".wav")      # 0600, removed right after
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(wav)
                try:
                    return await self._ask(path)
                except WorkerDied:
                    await self.stop()             # crashed or killed while idle: one fresh start, same recording
                    return await self._ask(path)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    async def _ask(self, path: str) -> str:
        await self.start()
        r = await self._send({"path": path, "mode": self.mode})
        self.last_model = r.get("model") or "gigaam"
        return r.get("text") or ""

    async def _send(self, req: dict) -> dict:
        self._n += 1
        rid = f"s{self._n}"
        try:
            self.proc.stdin.write((json.dumps({"id": rid, **req}, ensure_ascii=False) + "\n").encode())
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise WorkerDied("Локальное распознавание остановилось") from e
        while True:
            line = await asyncio.wait_for(self.proc.stdout.readline(), 150)     # the second model may still be loading
            if not line:
                raise WorkerDied("Локальное распознавание остановилось")
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("id") != rid:
                continue
            if r.get("error"):
                raise RuntimeError(r["error"])
            return r

    async def stop(self) -> None:
        await _kill(self.proc)
        self.proc = None
