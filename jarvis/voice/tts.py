"""Text-to-speech: local only — nothing about speech leaves the computer and there is no quota.

SileroTTS (main voice): Silero v5.2 Russian model, ~0.25 s per sentence on this laptop's CPU (measured), much more
natural than Piper. Runs as a separate process from its own virtualenv (~/.local/share/jarvis/silero) via
jarvis/voice/silero_worker.py. Silero models: non-commercial licence (snakers4/silero-models).
PiperTTS (emergency fallback only, voice «Денис»): used if the Silero process cannot start or fails.
Piper (GPL-3.0) runs as a separate, unmodified process from ~/.local/share/jarvis/piper.
KokoroTTS (other languages): Kokoro-82M (Apache-2.0), English with British/American voices, Spanish, French, Italian,
Portuguese; ~0.4× real time on this CPU. Silero also speaks German and Ukrainian. Each sentence is spoken by the voice
of its own language (jarvis/voice/lang.py): the Russian voice used to spell English letter by letter.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import shutil
import tempfile
import time
from pathlib import Path

PIPER_HOME = Path(os.path.expanduser("~/.local/share/jarvis/piper"))
SILERO_HOME = Path(os.path.expanduser("~/.local/share/jarvis/silero"))
KOKORO_HOME = Path(os.path.expanduser("~/.local/share/jarvis/kokoro"))
SILERO_MODEL = "v5_2_ru.pt"
SILERO_LANGS = {"ru": "v5_2_ru.pt", "de": "v3_de.pt", "uk": "v4_ua.pt"}
# JARVIS's own short phrases, in the language the person spoke. Short on purpose: say less, do more.
ACKS_BY_LANG = {"ru": ["Секунду.", "Сейчас.", "Делаю."], "en": ["One moment.", "On it."],
                "uk": ["Секунду.", "Зараз."], "de": ["Einen Moment."], "es": ["Un momento."], "fr": ["Un instant."],
                "it": ["Un attimo."], "pt": ["Um momento."]}
ACKS = ACKS_BY_LANG["ru"]
NOT_HEARD_BY_LANG = {"ru": "Не расслышал. Повтори, пожалуйста.", "en": "Sorry, I didn't catch that.",
                     "uk": "Не розчув. Повтори, будь ласка.", "de": "Das habe ich nicht verstanden.",
                     "es": "No te he entendido.", "fr": "Je n'ai pas compris.", "it": "Non ho capito.",
                     "pt": "Não entendi."}
NOT_HEARD = NOT_HEARD_BY_LANG["ru"]
FAILED = "Не удалось получить ответ. Попробуйте ещё раз чуть позже."
CANNED = ACKS + [NOT_HEARD, FAILED]
CANNED_EN = ACKS_BY_LANG["en"] + [NOT_HEARD_BY_LANG["en"]]


def phrase(table: dict, lang: str) -> str:
    v = table.get(lang) or table["ru"]
    return random.choice(v) if isinstance(v, list) else v

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
PREVIEW_EN = "Hello, I'm Jarvis. This is how I sound in English. What shall I do?"

# English voices (Kokoro). The British male voice suits «JARVIS»; a female Russian voice gets a female English one.
EN_VOICES: list[dict] = [
    {"id": "bm_george", "lang": "en-gb", "name": "George", "gender": "м", "desc": "Британский, спокойный"},
    {"id": "bm_lewis", "lang": "en-gb", "name": "Lewis", "gender": "м", "desc": "Британский, низкий"},
    {"id": "bm_fable", "lang": "en-gb", "name": "Fable", "gender": "м", "desc": "Британский, живой"},
    {"id": "am_michael", "lang": "en-us", "name": "Michael", "gender": "м", "desc": "Американский"},
    {"id": "bf_emma", "lang": "en-gb", "name": "Emma", "gender": "ж", "desc": "Британский, мягкий"},
    {"id": "bf_isabella", "lang": "en-gb", "name": "Isabella", "gender": "ж", "desc": "Британский, звонкий"},
    {"id": "af_heart", "lang": "en-us", "name": "Heart", "gender": "ж", "desc": "Американский, тёплый"},
]
# Other languages: (Kokoro voice, Kokoro language) for a male / female main voice; Silero speakers for de / uk.
KOKORO_LANGS = {"es": {"м": ("em_alex", "es"), "ж": ("ef_dora", "es")},
                "fr": {"м": ("ff_siwis", "fr-fr"), "ж": ("ff_siwis", "fr-fr")},
                "it": {"м": ("im_nicola", "it"), "ж": ("if_sara", "it")},
                "pt": {"м": ("pm_alex", "pt-br"), "ж": ("pf_dora", "pt-br")}}
SILERO_SPEAKERS = {"de": {"м": "bernd_ungerer", "ж": "eva_k"}, "uk": {"м": "mykyta", "ж": "mykyta"}}


def en_voice(vid: str | None, gender: str = "м") -> dict:
    return next((v for v in EN_VOICES if v["id"] == vid), None) or \
        next(v for v in EN_VOICES if v["gender"] == gender)


def voice_info(vid: str) -> dict:
    return next((v for v in VOICES if v["id"] == vid), next(v for v in VOICES if v["id"] == DEFAULT_VOICE))


def clean_for_speech(text: str, lang: str = "ru") -> str:
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[*_`#>|]+", "", t)
    t = re.sub(r"\[уточнить[^\]]*\]", "уточнить", t)
    if lang in ("ru", "uk"):
        t = re.sub(r"\b([01]?\d|2[0-3]):00\b", r"\1 ноль-ноль", t)        # 13:00 → «тринадцать ноль-ноль»
        t = re.sub(r"\b([01]?\d|2[0-3]):0(\d)\b", r"\1 ноль \2", t)
        t = re.sub(r"\b([01]?\d|2[0-3]):([1-5]\d)\b", r"\1 \2", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


CACHE_MAX = 300          # ≈ 300 short phrases × ~100 KB ≈ 30 MB at most


class WorkerDied(RuntimeError):
    """The speech process ended (crash, OOM, killed): the caller may restart it once."""


async def _kill(proc) -> None:
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


def sweep_stale_dirs() -> None:
    """Remove JARVIS speech temp dirs whose owning process is gone (dir name carries the pid)."""
    for d in Path(tempfile.gettempdir()).glob("jarvis-tts-*-*"):
        try:
            pid = int(d.name.split("-")[2])
        except (IndexError, ValueError):
            continue
        if pid != os.getpid() and not Path(f"/proc/{pid}").exists() and d.owner() == Path.home().owner():
            shutil.rmtree(d, ignore_errors=True)


class SileroTTS:
    name = "silero"
    worker = Path(__file__).with_name("silero_worker.py")

    def __init__(self, home: Path = SILERO_HOME, speaker: str = "aidar", rate: float = 1.0):
        self.home, self.speaker, self.rate = home, speaker, rate
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.cache: dict[tuple, bytes] = {}       # short phrases only, bounded (see CACHE_MAX)
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
        self.proc = await asyncio.create_subprocess_exec(
            str(self.python), "-I", str(self.worker), str(self.model),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True, limit=64 << 20)
        line = await asyncio.wait_for(self.proc.stdout.readline(), 60)       # model load: ~2–4 s
        if not line or not json.loads(line).get("ready"):
            self.proc = None
            raise RuntimeError("Silero не запустился")

    def has_lang(self, lang: str) -> bool:
        return lang in SILERO_LANGS and (self.home / "models" / SILERO_LANGS[lang]).exists()

    async def synth(self, text: str, speaker: str | None = None, rate: float | None = None, lang: str = "ru") -> bytes:
        text = clean_for_speech(text, lang).replace("\n", " ")
        if not text:
            return b""
        key = (text, speaker or self.speaker, round(rate or self.rate, 2), lang)
        if key in self.cache:
            return self.cache[key]
        async with self.lock:                     # one phrase at a time, in order
            try:
                data = await self._ask(text, key)
            except WorkerDied:
                await self.stop()                     # crashed or killed while idle: one fresh start, same phrase
                data = await self._ask(text, key)
            if len(text) < 80:
                if len(self.cache) >= CACHE_MAX:      # bounded: a long-running JARVIS must not grow forever
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key] = data
            return data

    async def _ask(self, text: str, key: tuple) -> bytes:
        await self.start()
        self._n += 1
        rid = f"r{self._n}"
        req = {"id": rid, "text": text, "speaker": key[1], "rate": key[2], "lang": key[3]}
        try:
            self.proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode())
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise WorkerDied("Silero остановился") from e
        while True:
            line = await asyncio.wait_for(self.proc.stdout.readline(), 60)      # a new language loads on its first phrase
            if not line:
                raise WorkerDied("Silero остановился")
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("id") != rid:
                continue
            if r.get("error"):
                raise RuntimeError("Silero: " + r["error"])
            return base64.b64decode(r["wav_b64"])

    async def prewarm(self) -> None:
        await self.start()
        for a in CANNED:
            await self.synth(a)

    async def stop(self) -> None:
        await _kill(self.proc)
        self.proc = None

    async def configure(self, speaker: str, rate: float) -> None:
        self.speaker, self.rate = speaker, rate      # per request: no restart needed


class KokoroTTS:
    """English (and es / fr / it / pt) voice: a Kokoro process from its own virtualenv, one phrase at a time."""
    name = "kokoro"
    worker = Path(__file__).with_name("kokoro_worker.py")

    def __init__(self, home: Path = KOKORO_HOME):
        self.home = home
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.cache: dict[tuple, bytes] = {}
        self._n = 0

    @property
    def python(self) -> Path:
        return self.home / ".venv" / "bin" / "python"

    def available(self) -> bool:
        return self.python.exists() and (self.home / "models" / "kokoro-v1.0.int8.onnx").exists() \
            and (self.home / "models" / "voices-v1.0.bin").exists()

    async def start(self) -> None:
        if self.proc and self.proc.returncode is None:
            return
        env = {k: v for k, v in os.environ.items() if not any(x in k.upper() for x in ("KEY", "TOKEN", "SECRET"))}
        self.proc = await asyncio.create_subprocess_exec(
            str(self.python), "-I", str(self.worker), str(self.home), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True,
            limit=64 << 20)
        line = await asyncio.wait_for(self.proc.stdout.readline(), 90)
        if not line or not json.loads(line).get("ready"):
            self.proc = None
            raise RuntimeError("Kokoro не запустился")

    async def synth(self, text: str, voice: str = "bm_george", lang: str = "en-gb", rate: float = 1.0) -> bytes:
        text = clean_for_speech(text, lang[:2]).replace("\n", " ")
        if not text:
            return b""
        key = (text, voice, lang, round(rate, 2))
        if key in self.cache:
            return self.cache[key]
        async with self.lock:
            try:
                data = await self._ask(key)
            except WorkerDied:
                await self.stop()
                data = await self._ask(key)
            if len(text) < 80:
                if len(self.cache) >= CACHE_MAX:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key] = data
            return data

    async def _ask(self, key: tuple) -> bytes:
        await self.start()
        self._n += 1
        rid = f"k{self._n}"
        req = {"id": rid, "text": key[0], "voice": key[1], "lang": key[2], "rate": key[3]}
        try:
            self.proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode())
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise WorkerDied("Kokoro остановился") from e
        while True:
            line = await asyncio.wait_for(self.proc.stdout.readline(), 60)
            if not line:
                raise WorkerDied("Kokoro остановился")
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("id") != rid:
                continue
            if r.get("error"):
                raise RuntimeError("Kokoro: " + r["error"])
            return base64.b64decode(r["wav_b64"])

    async def stop(self) -> None:
        await _kill(self.proc)
        self.proc = None


class PiperTTS:
    name = "piper"

    def __init__(self, voice: str = "ru_RU-denis-medium", home: Path = PIPER_HOME, rate: float = 1.0):
        self.home, self.voice, self.rate = home, voice, rate
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.outdir: Path | None = None           # created on first start only (measured: 7065 empty dirs leaked)
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
        if self.outdir is None or not self.outdir.is_dir():
            sweep_stale_dirs()
            self.outdir = Path(tempfile.mkdtemp(prefix=f"jarvis-tts-{os.getpid()}-"))
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
            try:
                return await self._ask(text)
            except WorkerDied:
                await _kill(self.proc)
                self.proc = None
                return await self._ask(text)

    async def _ask(self, text: str) -> bytes:
        await self.start()
        try:
            self.proc.stdin.write((text + "\n").encode())
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise WorkerDied("Piper остановился") from e
        while True:
            line = await asyncio.wait_for(self.proc.stderr.readline(), 30)
            if not line:
                raise WorkerDied("Piper остановился")
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
        await _kill(self.proc)
        self.proc = None
        if self.outdir:
            shutil.rmtree(self.outdir, ignore_errors=True)
            self.outdir = None


class VoiceEngine:
    """What SpeechManager talks to. Russian: the chosen Silero voice (Piper «Денис» only if Silero fails, reported).
    Each sentence in another language goes to that language's voice: English / es / fr / it / pt — Kokoro,
    German / Ukrainian — Silero. A language with no voice installed is read by the Russian voice (as before)."""
    multilingual = True

    def __init__(self, silero: SileroTTS, piper: PiperTTS | None = None, voice_id: str = DEFAULT_VOICE,
                 rate: float = 1.0, kokoro: KokoroTTS | None = None, en_voice_id: str | None = None):
        self.silero, self.piper, self.kokoro = silero, piper, kokoro
        v = voice_info(voice_id)
        self.voice_id, self.rate = v["id"], rate
        self.en = en_voice(en_voice_id, v["gender"])
        silero.speaker, silero.rate = v["speaker"], rate
        self.last_engine = "silero"
        self.last_fallback: str | None = None
        self.cache = silero.cache

    @property
    def name(self) -> str:
        return self.voice_id

    @property
    def gender(self) -> str:
        return voice_info(self.voice_id)["gender"]

    def languages(self) -> list[str]:
        """Languages this computer can speak right now."""
        out = ["ru"]
        if self.kokoro and self.kokoro.available():
            out += ["en", "es", "fr", "it", "pt"]
        out += [lang for lang in ("de", "uk") if self.silero.has_lang(lang)]
        return out

    async def set_voice(self, voice_id: str, rate: float = 1.0) -> dict:
        v = voice_info(voice_id)
        self.voice_id, self.rate = v["id"], rate
        await self.silero.configure(v["speaker"], rate)
        return v

    def set_en_voice(self, vid: str) -> dict:
        self.en = en_voice(vid, self.gender)
        return self.en

    async def _russian(self, text: str) -> bytes:
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

    async def _one(self, lang: str, text: str) -> bytes:
        if lang in ("en", *KOKORO_LANGS) and self.kokoro and self.kokoro.available():
            voice, kl = (self.en["id"], self.en["lang"]) if lang == "en" else KOKORO_LANGS[lang][self.gender]
            self.last_engine = "kokoro"
            return await self.kokoro.synth(text, voice, kl, self.rate)
        if lang in SILERO_SPEAKERS and self.silero.available() and self.silero.has_lang(lang):
            self.last_engine = "silero"
            return await self.silero.synth(text, speaker=SILERO_SPEAKERS[lang][self.gender], rate=self.rate, lang=lang)
        return await self._russian(text)

    async def synth_stream(self, text: str, lang: str = "ru"):
        """One audio piece per language run, in order (a Russian answer with an English quote: two pieces)."""
        from .lang import runs
        for lg, piece in runs(text, lang) or [(lang, text)]:
            audio = await self._one(lg, piece)
            if audio:
                yield audio

    async def synth(self, text: str, lang: str = "ru") -> bytes:
        pieces = [a async for a in self.synth_stream(text, lang)]
        return join_wavs(pieces)

    async def preview(self, voice_id: str, text: str = PREVIEW) -> bytes:
        v = voice_info(voice_id)
        return await self.silero.synth(text, speaker=v["speaker"], rate=self.rate)

    async def preview_en(self, vid: str) -> bytes:
        if not (self.kokoro and self.kokoro.available()):
            raise RuntimeError("Английский голос не установлен (scripts/install.sh)")
        v = en_voice(vid, self.gender)
        return await self.kokoro.synth(PREVIEW_EN, v["id"], v["lang"], self.rate)

    async def prewarm(self) -> None:
        if self.silero.available():
            await self.silero.prewarm()
        elif self.piper and self.piper.available():
            await self.piper.prewarm()
        if self.kokoro and self.kokoro.available():
            await self.kokoro.start()
            for a in CANNED_EN:
                await self.kokoro.synth(a, self.en["id"], self.en["lang"], self.rate)

    async def stop(self) -> None:
        await self.silero.stop()
        if self.piper:
            await self.piper.stop()
        if self.kokoro:
            await self.kokoro.stop()


def join_wavs(pieces: list[bytes]) -> bytes:
    """Several WAVs of the same format → one (pieces of another format are dropped; both voices use 24 kHz mono)."""
    import io
    import wave
    if len(pieces) <= 1:
        return pieces[0] if pieces else b""
    frames, params = [], None
    for p in pieces:
        try:
            with wave.open(io.BytesIO(p)) as w:
                pr = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                if params is None:
                    params = pr
                if pr == params:
                    frames.append(w.readframes(w.getnframes()))
        except (wave.Error, EOFError):
            continue
    if params is None:
        return pieces[0]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(params[0])
        w.setsampwidth(params[1])
        w.setframerate(params[2])
        w.writeframes(b"".join(frames))
    return buf.getvalue()
