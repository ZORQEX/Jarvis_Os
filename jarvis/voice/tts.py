"""Text-to-speech: local only — nothing about speech leaves the computer and there is no quota.

SupertonicTTS (main voice since 2026-10-24): Supertonic 3, one voice for every language JARVIS speaks (ru, en, uk, de,
es, fr, it, pt …), measured more natural than the voices below (UTMOS 3.95 vs 2.98) at ~0.35 s of compute per second
of speech; runs from ~/.local/share/jarvis/supertonic (jarvis/voice/supertonic_worker.py). The voices below are the
fallback when it is not installed or fails, and remain selectable in Settings.
SileroTTS (Russian voices): Silero v5.2 Russian model, ~0.25 s per sentence on this laptop's CPU (measured), much more
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
SUPERTONIC_HOME = Path(os.path.expanduser("~/.local/share/jarvis/supertonic"))
SUPERTONIC_LANGS = ["ru", "en", "uk", "de", "es", "fr", "it", "pt"]      # the ones jarvis/voice/lang.py tells apart
SILERO_MODEL = "v5_2_ru.pt"
SILERO_LANGS = {"ru": "v5_2_ru.pt", "de": "v3_de.pt", "uk": "v4_ua.pt"}
# JARVIS's own short phrases, in the language the person spoke. Short on purpose: say less, do more.
ACKS_BY_LANG = {"ru": ["Секунду.", "Сейчас.", "Делаю."], "en": ["One moment.", "On it."],
                "uk": ["Секунду.", "Зараз."], "de": ["Einen Moment."], "es": ["Un momento."], "fr": ["Un instant."],
                "it": ["Un attimo."], "pt": ["Um momento."]}
ACKS = ACKS_BY_LANG["ru"]
# While the answer is being thought out (the AI has not started it yet): JARVIS says that it is thinking.
THINKING_BY_LANG = {"ru": ["Секунду, думаю.", "Думаю.", "Сейчас подумаю."], "en": ["Let me think.", "Thinking."],
                    "uk": ["Секунду, думаю.", "Думаю."], "de": ["Ich denke nach."], "es": ["Déjame pensar."],
                    "fr": ["Je réfléchis."], "it": ["Ci penso."], "pt": ["Deixa eu pensar."]}
THINKING = THINKING_BY_LANG["ru"]
NOT_HEARD_BY_LANG = {"ru": "Не расслышал. Повтори, пожалуйста.", "en": "Sorry, I didn't catch that.",
                     "uk": "Не розчув. Повтори, будь ласка.", "de": "Das habe ich nicht verstanden.",
                     "es": "No te he entendido.", "fr": "Je n'ai pas compris.", "it": "Non ho capito.",
                     "pt": "Não entendi."}
NOT_HEARD = NOT_HEARD_BY_LANG["ru"]
FAILED = "Не удалось получить ответ. Попробуйте ещё раз чуть позже."
CANNED = ACKS + THINKING + [NOT_HEARD, FAILED, "Слушаю."]      # «Слушаю.» = router.WAKE_REPLY
CANNED_EN = ACKS_BY_LANG["en"] + THINKING_BY_LANG["en"] + [NOT_HEARD_BY_LANG["en"]]


def phrase(table: dict, lang: str) -> str:
    v = table.get(lang) or table["ru"]
    return random.choice(v) if isinstance(v, list) else v

# The voices offered in Settings → Голос: the two most natural voices of Supertonic 3 — one model that speaks every
# language. Measured naturalness (UTMOS, 1–5, mean of 8 phrases in 6 languages, 2026-10-24): F1 3.99, M4 3.95, the
# other eight 3.57–3.94; Silero + Kokoro 2.98 (Ukrainian 1.82). The person asked for one or two voices (2026-10-26):
# Silero (ru / de / uk), Kokoro (en / es / fr / it / pt) and Piper stay only as the fallback when Supertonic is not
# installed or fails — reported in the window, never offered as a choice.
VOICES: list[dict] = [
    {"id": "supertonic:M4", "engine": "supertonic", "speaker": "M4", "gender": "м", "name": "Мужской", "score": 3.95,
     "desc": "Все языки · естественность 3,95 из 5 · спокойный, уверенный"},
    {"id": "supertonic:F1", "engine": "supertonic", "speaker": "F1", "gender": "ж", "name": "Женский", "score": 3.99,
     "desc": "Все языки · естественность 3,99 из 5 · тёплый, ясный"},
]
FEMALE_BEFORE = {"silero:xenia", "silero:kseniya", "silero:baya"}       # earlier choices, for the gender
DEFAULT_VOICE = "supertonic:M4"
SILERO_DEFAULT = {"м": "aidar", "ж": "xenia"}       # the Russian fallback voice for a Supertonic voice
PREVIEW = "Привет! Я Джарвис. Так я буду звучать, когда отвечаю. Что сделать?"
PREVIEW_EN = "Hello, I'm Jarvis. This is how I sound in English. What shall I do?"

# The fallback English voice (Kokoro), by the gender of the main voice: British, as suits «JARVIS».
EN_VOICES: list[dict] = [
    {"id": "bm_george", "lang": "en-gb", "name": "George", "gender": "м"},
    {"id": "bf_emma", "lang": "en-gb", "name": "Emma", "gender": "ж"},
]
# Other languages: (Kokoro voice, Kokoro language) for a male / female main voice; Silero speakers for de / uk.
KOKORO_LANGS = {"es": {"м": ("em_alex", "es"), "ж": ("ef_dora", "es")},
                "fr": {"м": ("ff_siwis", "fr-fr"), "ж": ("ff_siwis", "fr-fr")},
                "it": {"м": ("im_nicola", "it"), "ж": ("if_sara", "it")},
                "pt": {"м": ("pm_alex", "pt-br"), "ж": ("pf_dora", "pt-br")}}
SILERO_SPEAKERS = {"de": {"м": "bernd_ungerer", "ж": "eva_k"}, "uk": {"м": "mykyta", "ж": "mykyta"}}


def en_voice(gender: str = "м") -> dict:
    return next(v for v in EN_VOICES if v["gender"] == gender)


def voice_info(vid: str) -> dict:
    """One of the two voices; an earlier choice (another Supertonic voice, a Silero one) → the best of its gender."""
    v = next((v for v in VOICES if v["id"] == vid), None)
    if v:
        return v
    female = str(vid) in FEMALE_BEFORE or str(vid).startswith("supertonic:F")
    return next(v for v in VOICES if v["gender"] == ("ж" if female else "м"))


def clean_for_speech(text: str, lang: str = "ru") -> str:
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"\[([^\]\n]+)\]\(https?://[^)\s]+\)", r"\1", t)     # [текст](ссылка) → текст
    t = re.sub(r"\s*\[\d{1,2}\]", "", t)                              # source numbers of a web answer: [1][2]
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[*_`#>|]+", "", t)
    t = re.sub(r"\[уточнить[^\]]*\]", "уточнить", t)
    t = re.sub(r"\[(?:голос|voice)\]\s*", "", t, flags=re.I)              # JARVIS's own mark, never read aloud
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


class WorkerTTS:
    """A voice in its own process and virtualenv, one phrase at a time over JSON lines, short phrases cached."""
    name = "worker"
    title = "Голос"
    worker: Path
    start_timeout = 90

    def __init__(self, home: Path):
        self.home = home
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.cache: dict[tuple, bytes] = {}
        self._n = 0

    @property
    def python(self) -> Path:
        return self.home / ".venv" / "bin" / "python"

    def args(self) -> list[str]:
        return [str(self.home)]

    async def start(self) -> None:
        if self.proc and self.proc.returncode is None:
            return
        env = {k: v for k, v in os.environ.items() if not any(x in k.upper() for x in ("KEY", "TOKEN", "SECRET"))}
        self.proc = await asyncio.create_subprocess_exec(
            str(self.python), "-I", str(self.worker), *self.args(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True,
            limit=64 << 20)
        line = await asyncio.wait_for(self.proc.stdout.readline(), self.start_timeout)
        if not line or not json.loads(line).get("ready"):
            self.proc = None
            raise RuntimeError(f"{self.title} не запустился")

    async def _synth(self, text: str, lang: str, req: dict) -> bytes:
        text = clean_for_speech(text, lang).replace("\n", " ")
        if not text:
            return b""
        key = (text, json.dumps(req, sort_keys=True))
        if key in self.cache:
            return self.cache[key]
        async with self.lock:
            try:
                data = await self._ask({**req, "text": text})
            except WorkerDied:
                await self.stop()
                data = await self._ask({**req, "text": text})
            if len(text) < 80:
                if len(self.cache) >= CACHE_MAX:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key] = data
            return data

    async def _ask(self, req: dict) -> bytes:
        await self.start()
        self._n += 1
        rid = f"w{self._n}"
        try:
            self.proc.stdin.write((json.dumps({**req, "id": rid}, ensure_ascii=False) + "\n").encode())
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise WorkerDied(f"{self.title} остановился") from e
        while True:
            line = await asyncio.wait_for(self.proc.stdout.readline(), 60)
            if not line:
                raise WorkerDied(f"{self.title} остановился")
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("id") != rid:
                continue
            if r.get("error"):
                raise RuntimeError(f"{self.title}: " + r["error"])
            return base64.b64decode(r["wav_b64"])

    async def stop(self) -> None:
        await _kill(self.proc)
        self.proc = None


class KokoroTTS(WorkerTTS):
    """English (and es / fr / it / pt) fallback voice: Kokoro-82M."""
    name, title = "kokoro", "Kokoro"
    worker = Path(__file__).with_name("kokoro_worker.py")

    def __init__(self, home: Path = KOKORO_HOME):
        super().__init__(home)

    def available(self) -> bool:
        return self.python.exists() and (self.home / "models" / "kokoro-v1.0.int8.onnx").exists() \
            and (self.home / "models" / "voices-v1.0.bin").exists()

    async def synth(self, text: str, voice: str = "bm_george", lang: str = "en-gb", rate: float = 1.0) -> bytes:
        return await self._synth(text, lang[:2], {"voice": voice, "lang": lang, "rate": round(rate, 2)})


class SupertonicTTS(WorkerTTS):
    """The main voice: Supertonic 3, the same voice in every language."""
    name, title = "supertonic", "Supertonic"
    worker = Path(__file__).with_name("supertonic_worker.py")
    start_timeout = 120

    def __init__(self, home: Path = SUPERTONIC_HOME):
        super().__init__(home)

    @property
    def models(self) -> Path:
        return self.home / "models"

    def args(self) -> list[str]:
        return [str(self.models)]

    def available(self) -> bool:
        return self.python.exists() and (self.models / "onnx" / "vector_estimator.onnx").exists() \
            and (self.models / "voice_styles" / "M4.json").exists()

    async def synth(self, text: str, voice: str = "M4", lang: str = "ru", rate: float = 1.0) -> bytes:
        return await self._synth(text, lang, {"voice": voice, "lang": lang, "rate": round(rate, 2)})


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
    """What SpeechManager talks to. A Supertonic voice speaks every language itself. With a Silero voice (or when
    Supertonic is not installed or fails — reported, never hidden) each sentence goes to its language's voice: Russian
    — Silero (Piper «Денис» if Silero fails), English / es / fr / it / pt — Kokoro, German / Ukrainian — Silero."""
    multilingual = True

    def __init__(self, silero: SileroTTS, piper: PiperTTS | None = None, voice_id: str = DEFAULT_VOICE,
                 rate: float = 1.0, kokoro: KokoroTTS | None = None, supertonic: SupertonicTTS | None = None):
        self.silero, self.piper, self.kokoro, self.supertonic = silero, piper, kokoro, supertonic
        self.voice, self.rate = voice_info(voice_id), rate
        self.en = en_voice(self.voice["gender"])
        silero.speaker, silero.rate = self._silero_speaker(), rate
        self.last_engine = "supertonic" if self._st_on() else "silero"
        self.last_fallback: str | None = None
        self.cache = silero.cache

    @property
    def voice_id(self) -> str:
        return self.voice["id"]

    @property
    def name(self) -> str:
        return self.voice_id

    @property
    def gender(self) -> str:
        return self.voice["gender"]

    @property
    def fell_back(self) -> bool:
        """The chosen voice did not speak the last phrase (another one did): the window says so."""
        return self.last_fallback is not None

    def _st_on(self) -> bool:
        return self.voice.get("engine") == "supertonic" and bool(self.supertonic and self.supertonic.available())

    def _silero_speaker(self) -> str:
        return SILERO_DEFAULT[self.voice["gender"]]

    def languages(self) -> list[str]:
        """Languages this computer can speak right now."""
        if self._st_on():
            return list(SUPERTONIC_LANGS)
        out = ["ru"]
        if self.kokoro and self.kokoro.available():
            out += ["en", "es", "fr", "it", "pt"]
        out += [lang for lang in ("de", "uk") if self.silero.has_lang(lang)]
        return out

    async def set_voice(self, voice_id: str, rate: float = 1.0) -> dict:
        self.voice, self.rate = voice_info(voice_id), rate
        self.en = en_voice(self.voice["gender"])
        await self.silero.configure(self._silero_speaker(), rate)
        return self.voice

    async def _russian(self, text: str) -> bytes:
        if self.silero.available():
            try:
                out = await self.silero.synth(text)
                self.last_engine = "silero"
                return out
            except Exception as e:   # noqa: BLE001
                self.last_fallback = str(e)[:200]
                if not (self.piper and self.piper.available()):
                    raise
        elif not (self.piper and self.piper.available()):
            raise RuntimeError("Голос не установлен (scripts/install.sh)")
        else:
            self.last_fallback = self.last_fallback or "Silero не установлен"
        self.last_engine = "piper"
        return await self.piper.synth(text)

    async def _one(self, lang: str, text: str) -> bytes:
        self.last_fallback = None
        if self.voice.get("engine") == "supertonic":
            if self._st_on():
                try:
                    out = await self.supertonic.synth(text, self.voice["speaker"], lang, self.rate)
                    self.last_engine = "supertonic"
                    return out
                except Exception as e:   # noqa: BLE001 — speak with the other voices, and say so
                    self.last_fallback = str(e)[:200]
            else:
                self.last_fallback = "Supertonic не установлен"
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

    async def preview(self, voice_id: str) -> bytes:
        """A sample of a voice without choosing it: a Russian and an English sentence — the same voice in both."""
        v = voice_info(voice_id)
        if not (self.supertonic and self.supertonic.available()):
            raise RuntimeError("Голос Supertonic не установлен (scripts/install.sh)")
        return join_wavs([await self.supertonic.synth(PREVIEW, v["speaker"], "ru", self.rate),
                          await self.supertonic.synth(PREVIEW_EN, v["speaker"], "en", self.rate)])

    async def prewarm(self) -> None:
        """Only the chosen voice is started (the fallback voices start on their first phrase: ~1 GB of memory less)."""
        if self._st_on():
            await self.supertonic.start()
            for a in CANNED:
                await self.supertonic.synth(a, self.voice["speaker"], "ru", self.rate)
            for a in CANNED_EN:
                await self.supertonic.synth(a, self.voice["speaker"], "en", self.rate)
            return
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
        for e in (self.piper, self.kokoro, self.supertonic):
            if e:
                await e.stop()


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
