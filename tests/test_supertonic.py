"""One natural voice for every language (Supertonic 3), the fallback voices, text normalisation, the recogniser's
second model on noise (2026-10-24)."""
import importlib.util
import io
import time
import wave
from pathlib import Path

import pytest

from jarvis.voice.tts import VOICES, SupertonicTTS, VoiceEngine, voice_info

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "jarvis" / "voice" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


textnorm = _load("textnorm")
lexicon = _load("lexicon")


VOICE_PY = next((p for p in (Path.home() / ".local/share/jarvis/supertonic/.venv/bin/python",
                             Path.home() / ".local/share/jarvis/silero/.venv/bin/python") if p.exists()), None)


@pytest.mark.skipif(VOICE_PY is None, reason="no voice virtualenv with num2words")
def test_numbers_and_symbols_become_words_in_each_language():
    """Run where it runs for real: in a voice worker's virtualenv (num2words lives there, not in JARVIS's)."""
    import json
    import subprocess
    code = ("import importlib.util,json,sys;s=importlib.util.spec_from_file_location('t',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "print(json.dumps([m.normalize('25% от 800','ru'),m.normalize('It is 25% of 800.','en'),"
            "m.normalize('3,5 раза','ru'),m.normalize('Открыл YouTube','ru',translit=False),"
            "m.normalize('Открыл YouTube','ru')],ensure_ascii=False))")
    out = json.loads(subprocess.run([str(VOICE_PY), "-I", "-c", code, str(ROOT / "jarvis/voice/textnorm.py")],
                                    capture_output=True, text=True, timeout=60).stdout)
    assert out[0] == "двадцать пять процентов от восемьсот"
    assert out[1] == "It is twenty-five percent of eight hundred."
    assert out[2].startswith("три целых пять")
    assert out[3] == "Открыл YouTube"           # Supertonic reads brand names in a Russian sentence itself
    assert out[4] == "Открыл ютуб"              # Silero needs them in Russian letters


def test_wake_like_words_only_run_the_second_recogniser():
    for t in ("Джарвис, который час", "Javis what time", "Джервис", "джарвес привет", "Жарвис"):
        assert lexicon.wake_like(t), t
    for t in ("ну и вот он говорит", "Давай", "Джаз играет", ""):
        assert not lexicon.wake_like(t), t


class Fake:
    def __init__(self, ok=True, tag=b"X"):
        self.ok, self.tag, self.calls, self.cache, self.speaker, self.rate = ok, tag, [], {}, None, 1.0

    def available(self):
        return True

    def has_lang(self, lang):
        return lang in ("de", "uk")

    async def synth(self, text, *a, **k):
        self.calls.append((text, a, k))
        if not self.ok:
            raise RuntimeError("сломался")
        return b"RIFF" + self.tag

    async def configure(self, speaker, rate):
        self.speaker, self.rate = speaker, rate

    async def start(self):
        pass

    async def stop(self):
        pass


async def test_one_voice_speaks_every_language():
    st, silero, kokoro = Fake(tag=b"S"), Fake(tag=b"R"), Fake(tag=b"K")
    eng = VoiceEngine(silero, None, "supertonic:M4", kokoro=kokoro, supertonic=st)
    pieces = [a async for a in eng.synth_stream("Готово. Done, I've sent it to your mom. Я відкрив браузер.", "ru")]
    assert pieces == [b"RIFFS"] * 3 and not silero.calls and not kokoro.calls
    assert [c[1][1] for c in st.calls] == ["ru", "en", "uk"] and all(c[1][0] == "M4" for c in st.calls)
    assert eng.languages()[:2] == ["ru", "en"] and not eng.fell_back
    assert silero.speaker == "aidar"                     # the Russian fallback voice matches the gender


async def test_when_the_voice_fails_the_old_voices_speak_and_say_so():
    st, silero, kokoro = Fake(ok=False), Fake(tag=b"R"), Fake(tag=b"K")
    eng = VoiceEngine(silero, None, "supertonic:F1", kokoro=kokoro, supertonic=st)
    assert await eng.synth("Привет.") == b"RIFFR" and eng.fell_back and "сломался" in eng.last_fallback
    assert silero.speaker == "xenia"
    assert await eng.synth("Hello there, how are you?", "en") == b"RIFFK"


async def test_an_earlier_choice_becomes_the_best_voice_of_its_gender():
    st, silero, kokoro = Fake(tag=b"S"), Fake(tag=b"R"), Fake(tag=b"K")
    eng = VoiceEngine(silero, None, "silero:baya", kokoro=kokoro, supertonic=st)    # a Silero voice chosen before
    assert eng.voice_id == "supertonic:F1" and await eng.synth("Привет.") == b"RIFFS" and st.calls[-1][1][0] == "F1"
    await eng.set_voice("supertonic:M5")
    assert eng.voice_id == "supertonic:M4" and await eng.synth("Привет.") == b"RIFFS" and st.calls[-1][1][0] == "M4"


def test_two_voices_the_most_natural_of_each_gender():
    """The person asked for one or two voices (2026-10-26): the two highest measured, both speak every language."""
    assert [(v["id"], v["gender"]) for v in VOICES] == [("supertonic:M4", "м"), ("supertonic:F1", "ж")]
    assert all(v["engine"] == "supertonic" and "Все языки" in v["desc"] for v in VOICES)
    assert voice_info("unknown")["id"] == "supertonic:M4" and voice_info("silero:xenia")["id"] == "supertonic:F1"
    assert voice_info("supertonic:F3")["id"] == "supertonic:F1" and voice_info("silero:eugene")["id"] == "supertonic:M4"


@pytest.mark.skipif(not SupertonicTTS().available(), reason="Supertonic is not installed")
async def test_real_voice_is_fast_and_valid():
    tts = SupertonicTTS()
    try:
        await tts.start()
        t0 = time.perf_counter()
        audio = await tts.synth("Готово. Я открыл папку с лабораторными.", "M4", "ru")
        ms = (time.perf_counter() - t0) * 1000
        with wave.open(io.BytesIO(audio)) as w:
            dur = w.getnframes() / w.getframerate()
        assert audio[:4] == b"RIFF" and dur > 1.5 and ms < 3000, (ms, dur)
        en = await tts.synth("It costs 25% of 800 dollars.", "M4", "en")
        assert en[:4] == b"RIFF"
    finally:
        await tts.stop()
