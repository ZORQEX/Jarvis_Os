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
    t = re.sub(r"\b([01]?\d|2[0-3]):00\b", r"\1 ноль-ноль", t)            # 13:00 → «тринадцать ноль-ноль»
    t = re.sub(r"\b([01]?\d|2[0-3]):0(\d)\b", r"\1 ноль \2", t)
    t = re.sub(r"\b([01]?\d|2[0-3]):([1-5]\d)\b", r"\1 \2", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


# Voices the user can choose in Settings → Голос. Local = Piper (offline, instant). Cloud = Gemini TTS (much more natural,
# measured first sound 0.5–1.0 s when streamed, uses the Gemini quota; falls back to the local voice on any error).
VOICES: list[dict] = [
    {"id": "piper:denis", "engine": "piper", "model": "ru_RU-denis-medium", "name": "Денис", "gender": "м",
     "kind": "local", "desc": "Локальный, мгновенный, работает без интернета", "license": "CC0 (данные), Piper GPL-3.0"},
    {"id": "piper:dmitri", "engine": "piper", "model": "ru_RU-dmitri-medium", "name": "Дмитрий", "gender": "м",
     "kind": "local", "desc": "Локальный, чуть ниже и глуше", "license": "CC0 (данные)"},
    {"id": "piper:ruslan", "engine": "piper", "model": "ru_RU-ruslan-medium", "name": "Руслан", "gender": "м",
     "kind": "local", "desc": "Локальный, дикторская манера", "license": "CC BY-NC-SA 4.0 — только личное некоммерческое использование"},
    {"id": "gemini:Kore", "engine": "gemini", "model": "Kore", "name": "Кора", "gender": "ж",
     "kind": "cloud", "desc": "Живой женский, уверенный"},
    {"id": "gemini:Aoede", "engine": "gemini", "model": "Aoede", "name": "Аэда", "gender": "ж",
     "kind": "cloud", "desc": "Живой женский, лёгкий и непринуждённый"},
    {"id": "gemini:Leda", "engine": "gemini", "model": "Leda", "name": "Леда", "gender": "ж",
     "kind": "cloud", "desc": "Живой женский, молодой"},
    {"id": "gemini:Zephyr", "engine": "gemini", "model": "Zephyr", "name": "Зефир", "gender": "ж",
     "kind": "cloud", "desc": "Живой женский, яркий"},
    {"id": "gemini:Charon", "engine": "gemini", "model": "Charon", "name": "Харон", "gender": "м",
     "kind": "cloud", "desc": "Живой мужской, спокойный, как у JARVIS из фильмов"},
    {"id": "gemini:Puck", "engine": "gemini", "model": "Puck", "name": "Пак", "gender": "м",
     "kind": "cloud", "desc": "Живой мужской, бодрый"},
    {"id": "gemini:Orus", "engine": "gemini", "model": "Orus", "name": "Орус", "gender": "м",
     "kind": "cloud", "desc": "Живой мужской, твёрдый"},
    {"id": "gemini:Fenrir", "engine": "gemini", "model": "Fenrir", "name": "Фенрир", "gender": "м",
     "kind": "cloud", "desc": "Живой мужской, эмоциональный"},
]
DEFAULT_VOICE = "piper:denis"
PREVIEW = "Привет! Я JARVIS. Так я буду звучать, когда отвечаю. Что сделать?"


def voice_info(vid: str) -> dict:
    return next((v for v in VOICES if v["id"] == vid), next(v for v in VOICES if v["id"] == DEFAULT_VOICE))


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
        # length_scale < 1 speaks faster; a little more noise makes the intonation less flat.
        self.proc = await asyncio.create_subprocess_exec(
            str(self.python), "-m", "piper", "-m", str(self.model), "-d", str(self.outdir),
            "--length-scale", f"{1.0 / max(0.6, min(1.6, self.rate)):.3f}", "--noise-scale", "0.75", "--noise-w-scale", "0.9",
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

    async def configure(self, voice: str, rate: float) -> None:
        """Switch voice/speed: the Piper process restarts with the new model on the next phrase."""
        if voice == self.voice and abs(rate - self.rate) < 1e-3:
            return
        async with self.lock:
            if self.proc and self.proc.returncode is None:
                self.proc.kill()
                await self.proc.wait()
            self.proc, self.voice, self.rate = None, voice, rate
            self.cache.clear()
            self.outdir.mkdir(parents=True, exist_ok=True)


class GeminiVoiceTTS:
    """Natural cloud voice (Gemini TTS), streamed: the first piece of sound arrives before the phrase is finished."""
    name = "gemini-voice"

    def __init__(self, gemini, voice: str = "Kore", model: str = "gemini-3.8-flash-tts"):
        self.gemini, self.voice, self.model = gemini, voice, model
        self.cache: dict[str, bytes] = {}

    def available(self) -> bool:
        return self.gemini.configured()

    async def synth_stream(self, text: str):
        """Yields WAV chunks (24 kHz mono PCM, each a complete little WAV) as Gemini produces them."""
        from google.genai import types
        text = clean_for_speech(text)
        if not text:
            return
        if text in self.cache:
            yield self.cache[text]
            return
        cfg = types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice))))
        client = self.gemini.client()
        it = await asyncio.wait_for(client.aio.models.generate_content_stream(model=self.model, contents=text,
                                                                              config=cfg), 8)
        # Gemini streams tiny pieces (measured: 239 for two sentences). Send the first ~0.25 s at once (fast start),
        # then ~1 s blocks, so the window schedules a handful of buffers instead of hundreds.
        got, pcm_all, buf, rate, first = False, b"", b"", 24000, True
        async for ch in _timeout_iter(it, 15):
            cand = (ch.candidates or [None])[0]
            for p in (cand.content.parts or []) if cand and cand.content else []:
                if p.inline_data and p.inline_data.data:
                    got = True
                    rate = _rate(p.inline_data.mime_type)
                    pcm_all += p.inline_data.data
                    buf += p.inline_data.data
                    if len(buf) >= rate * 2 * (0.25 if first else 1.0):
                        yield _wav(buf, rate)
                        buf, first = b"", False
        if buf:
            yield _wav(buf, rate)
        if not got:
            raise RuntimeError("Gemini не вернул звук")
        if len(text) < 80:
            self.cache[text] = _wav(pcm_all, 24000)

    async def synth(self, text: str) -> bytes:
        chunks = [c async for c in self.synth_stream(text)]
        if len(chunks) == 1:
            return chunks[0]
        return _wav(b"".join(_pcm(c) for c in chunks), 24000)

    async def prewarm(self) -> None:
        return None          # every phrase costs quota: nothing is synthesized in advance

    async def stop(self) -> None:
        return None


def _rate(mime: str | None) -> int:
    m = re.search(r"rate=(\d+)", mime or "")
    return int(m.group(1)) if m else 24000


def _wav(pcm: bytes, rate: int) -> bytes:
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _pcm(wav_bytes: bytes) -> bytes:
    import io
    import wave
    with wave.open(io.BytesIO(wav_bytes)) as w:
        return w.readframes(w.getnframes())


async def _timeout_iter(it, per_chunk: float):
    ait = it.__aiter__()
    while True:
        try:
            yield await asyncio.wait_for(ait.__anext__(), per_chunk)
        except StopAsyncIteration:
            return


class VoiceEngine:
    """What SpeechManager talks to. Holds the chosen voice; a cloud voice falls back to the local one on any error,
    and the failure is reported, never hidden."""

    def __init__(self, piper: PiperTTS, gemini, voice_id: str = DEFAULT_VOICE, rate: float = 1.0):
        self.piper, self.gemini = piper, gemini
        self.cloud: GeminiVoiceTTS | None = None
        self.voice_id, self.rate = DEFAULT_VOICE, rate
        self.last_engine = "piper"
        self.last_fallback: str | None = None
        self.cache = piper.cache
        self._pending = (voice_id, rate)

    @property
    def name(self) -> str:
        return self.voice_id

    async def apply(self) -> None:
        if self._pending:
            vid, rate = self._pending
            self._pending = None
            await self.set_voice(vid, rate)

    async def set_voice(self, voice_id: str, rate: float = 1.0) -> dict:
        v = voice_info(voice_id)
        self.voice_id, self.rate = v["id"], rate
        if v["engine"] == "piper":
            self.cloud = None
            await self.piper.configure(v["model"], rate)
        else:
            self.cloud = GeminiVoiceTTS(self.gemini, v["model"])
            await self.piper.configure(self.piper.voice, rate)     # local fallback keeps its own voice
        return v

    async def synth_stream(self, text: str):
        await self.apply()
        if self.cloud and self.cloud.available():
            sent = False
            try:
                async for chunk in self.cloud.synth_stream(text):
                    sent = True
                    yield chunk
                self.last_engine, self.last_fallback = "gemini", None
                return
            except Exception as e:   # noqa: BLE001
                if sent:
                    raise RuntimeError(f"Голос Gemini оборвался: {e}") from None
                self.last_fallback = f"{e.__class__.__name__}: {str(e)[:120]}"
        yield await self.piper.synth(text)
        self.last_engine = "piper"

    async def synth(self, text: str) -> bytes:
        chunks = [c async for c in self.synth_stream(text)]
        return chunks[0] if len(chunks) == 1 else _wav(b"".join(_pcm(c) for c in chunks), 24000)

    async def prewarm(self) -> None:
        await self.apply()
        await self.piper.prewarm()

    async def stop(self) -> None:
        await self.piper.stop()


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


class FallbackTTS:
    """Primary engine (local Piper) with a cloud fallback (Gemini TTS). Failures are reported, never hidden."""

    def __init__(self, primary, fallback=None):
        self.primary, self.fallback = primary, fallback
        self.name = primary.name + (f"+{fallback.name}" if fallback else "")
        self.cache = primary.cache
        self.last_engine = primary.name

    async def synth(self, text: str) -> bytes:
        try:
            out = await self.primary.synth(text)
            self.last_engine = self.primary.name
            return out
        except Exception as e:   # noqa: BLE001
            if not self.fallback or not self.fallback.available():
                raise RuntimeError(f"{self.primary.name}: {e}") from None
            out = await self.fallback.synth(text)
            self.last_engine = self.fallback.name
            return out

    async def prewarm(self) -> None:
        await self.primary.prewarm()

    async def stop(self) -> None:
        await self.primary.stop()
