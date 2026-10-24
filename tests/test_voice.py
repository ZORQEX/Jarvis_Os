"""Voice conversation: fast path, streaming speech, interruption, telemetry."""
import asyncio
import base64
import time

import pytest

from jarvis.calc import answer
from jarvis.voice.speech import SentenceBuffer, SpeechManager
from jarvis.voice.tts import PiperTTS, SileroTTS, clean_for_speech

from conftest import ScriptedClaude, install, wait_done, wait_for


class FakeTTS:
    name = "fake"

    def __init__(self, delay=0.0):
        self.delay, self.calls = delay, []

    async def synth(self, text):
        self.calls.append(text)
        await asyncio.sleep(self.delay)
        return b"RIFF" + text.encode()

    async def prewarm(self):
        return None

    async def stop(self):
        return None


@pytest.fixture
def speech(core):
    sp = SpeechManager(core.db, core.bus, FakeTTS())
    core.director.speech = sp
    return sp


def audio_events(core):
    q = core.bus.subscribe()
    got = []

    def drain():
        while not q.empty():
            ev = q.get_nowait()
            if ev["kind"].startswith("speech."):
                got.append(ev)
        return got
    return drain


# ---------------------------------------------------------------- fast path (no AI)
@pytest.mark.parametrize("q,start", [
    ("Джарвис, сколько будет 25% от 800?", "200."), ("сколько будет двадцать пять процентов от восьмисот", "200."),
    ("Сколько будет 2 + 2?", "4."), ("сколько будет 100 разделить на 8", "12,5."),
    ("сколько метров в одном километре", "1 000."), ("сколько секунд в часе", "3 600."),
    ("корень из 144", "12."), ("20 процентов от 500", "100."),
])
def test_calculations_are_local(q, start):
    assert answer(q).startswith(start)


@pytest.mark.parametrize("q", ["Что такое API?", "Сколько ему лет?", "Сколько стоит доставка?", "Кто такой Илон Маск?"])
def test_non_calculations_go_to_ai(q):
    assert answer(q) is None


async def test_fast_path_answers_without_any_model_call(core, speech):
    g = ScriptedClaude(core.config, [])
    install(core, claude=g)
    t0 = time.perf_counter()
    tid = await core.director.submit("Джарвис, сколько будет 25% от 800?", "voice")
    await wait_done(core, tid)
    assert (time.perf_counter() - t0) < 0.5 and not g.calls
    assert core.director.messages(core.director.conversation_id())[-1]["text"].startswith("200.")
    await wait_for(lambda: speech.engine.calls)
    assert speech.engine.calls[0].startswith("200.")


def test_system_status_no_longer_catches_every_skolko():
    from jarvis.router import route
    assert route("сколько будет 25% от 800").kind == "answer"
    assert route("сколько места на диске").tool == "system_info"


# ---------------------------------------------------------------- sentence buffer
def test_sentence_buffer_emits_early_and_naturally():
    b = SentenceBuffer()
    assert b.feed("Конечно") == []
    assert b.feed(", сейчас") == []
    out = b.feed(" проверю. Я нашёл три варианта")
    assert out == ["Конечно, сейчас проверю."]
    assert b.feed(".") == []                        # "." at the very end may be a decimal point
    assert b.feed(" Первый") == ["Я нашёл три варианта."]
    assert b.flush() == ["Первый"]


def test_sentence_buffer_first_segment_on_comma_and_hard_limit():
    b = SentenceBuffer()
    assert b.feed("Искусственный интеллект, если говорить просто, ") == ["Искусственный интеллект, если говорить просто,"]
    long = "слово " * 60
    assert all(len(s) <= 220 for s in b.feed(long))


def test_speech_text_is_cleaned():
    assert clean_for_speech("**Итог:** см. https://x.example `код`") == "Итог: см. код"


# ---------------------------------------------------------------- streaming, order, interruption
async def test_voice_answer_is_spoken_while_generated(core, speech):
    g = ScriptedClaude(core.config, [["Искусственный интеллект — это область науки. ", "Она создаёт системы, ",
                                      "которые решают задачи. Например, распознают речь."]])
    install(core, claude=g)
    drain = audio_events(core)
    tid = await core.director.submit("Что такое искусственный интеллект?", "voice")
    await wait_done(core, tid)
    await wait_for(lambda: any(e["kind"] == "speech.end" for e in drain()))
    audio = [e for e in drain() if e["kind"] == "speech.audio"]
    assert [e["seq"] for e in audio] == list(range(1, len(audio) + 1)) and len(audio) >= 2
    assert audio[0]["text"].startswith("Искусственный интеллект")
    assert base64.b64decode(audio[0]["audio_b64"]).startswith(b"RIFF")
    # the voice-mode instruction and the fast voice model were used
    assert g.calls and "голосового разговора" in g.last_system and g.last_role == "voice"


async def test_interrupt_stops_speech_and_cancels_answer_but_not_actions(core, speech, home):
    speech.engine.delay = 0.2
    g = ScriptedClaude(core.config, [["Первое предложение длинного ответа. "] * 3])
    install(core, claude=g)
    drain = audio_events(core)
    tid = await core.director.submit("Расскажи подробно, как работает майнинг Bitcoin", "voice")
    await wait_for(lambda: speech.engine.calls)
    await core.director.submit("стоп", "voice")
    await wait_done(core, tid)
    assert tid not in speech.streams
    assert any(e["kind"] in ("speech.cancelled", "speech.stop") for e in drain())
    # a real action waiting for approval is not touched by «стоп»
    tid2 = await core.director.submit("запиши заметку: важное")
    await wait_for(lambda: core.approvals.pending())
    await core.director.submit("хватит", "voice")
    assert core.approvals.pending() and tid2 in core.director.rt
    core.director.decide(core.approvals.pending()[0]["id"], False)
    await wait_done(core, tid2)


async def test_long_request_gets_immediate_local_acknowledgement(core, speech):
    g = ScriptedClaude(core.config, [["Майнинг — это проверка блоков. "]])
    install(core, claude=g)
    tid = await core.director.submit("Расскажи подробно, как работает майнинг Bitcoin", "voice")
    await wait_for(lambda: speech.engine.calls)
    from jarvis.voice.tts import ACKS
    assert speech.engine.calls[0] in ACKS          # spoken before any model output, no AI call for it
    await wait_done(core, tid)


async def test_long_answers_are_capped_in_speech(core, speech):
    g = ScriptedClaude(core.config, [["Очень длинное предложение номер один про всё на свете. " * 40]])
    install(core, claude=g)
    tid = await core.director.submit("Что такое блокчейн?", "voice")
    await wait_done(core, tid)
    await wait_for(lambda: speech.engine.calls and speech.engine.calls[-1] == "Остальное — на экране.")
    assert sum(len(c) for c in speech.engine.calls[:-1]) <= 1300


async def test_typed_requests_are_not_spoken_unless_asked(core, speech):
    g = ScriptedClaude(core.config, [["Ответ."], ["Ответ два."]])
    install(core, claude=g)
    await wait_done(core, await core.director.submit("Что такое API?"))
    assert not speech.engine.calls
    await wait_done(core, await core.director.submit("Что такое REST?", speak=True))
    await wait_for(lambda: speech.engine.calls)


async def test_follow_up_keeps_recent_context(core, speech):
    g = ScriptedClaude(core.config, [["Илон Маск — предприниматель."], ["Ему 55 лет."]])
    install(core, claude=g)
    await wait_done(core, await core.director.submit("Кто такой Илон Маск?", "voice"))
    await wait_done(core, await core.director.submit("А сколько ему лет?", "voice"))
    sent = " ".join(m["text"] for m in g.calls[-1]["history"])
    assert "Илон Маск" in sent


def test_compact_history_is_bounded():
    from jarvis.agency.agency import compact_history
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "text": f"сообщение {i} " + "х" * 2000} for i in range(40)]
    recent, summary = compact_history(msgs)
    assert len(recent) == 6 and all(len(m["text"]) <= 600 for m in recent) and len(summary) < 900


# ---------------------------------------------------------------- telemetry
async def test_voice_turn_telemetry_records_real_stages(core, speech):
    g = ScriptedClaude(core.config, [["Готово, это просто. Вот ответ."]])
    install(core, claude=g)
    t = time.time()
    turn = speech.new_turn(t - 1.5, t)
    speech.mark(turn, "stt_start"); speech.mark(turn, "stt_end"); speech.mark(turn, "request_start")
    tid = await core.director.submit("Что такое API?", "voice", turn=turn)
    await wait_done(core, tid)
    await wait_for(lambda: speech.turns(1)[0]["speech_done"])
    ms = speech.turns(1)[0]["ms"]
    for k in ("stt_end", "ttft", "first_sentence", "tts_start", "first_audio_sent", "answer_done"):
        assert ms[k] is not None and ms[k] >= 0, k
    assert ms["speech_duration"] == 1500


# ---------------------------------------------------------------- local TTS (real Piper if installed)
@pytest.mark.skipif(not PiperTTS().available(), reason="Piper is not installed")
async def test_piper_synthesizes_fast():
    p = PiperTTS()
    try:
        await p.start()
        await p.synth("Прогрев.")
        t0 = time.perf_counter()
        wav = await p.synth("Двадцать пять процентов от восьмисот — это двести.")
        assert wav[:4] == b"RIFF" and len(wav) > 20000
        assert (time.perf_counter() - t0) < 1.5
    finally:
        await p.stop()


async def test_speech_failure_is_reported_and_text_stays(core):
    class Dead(FakeTTS):
        async def synth(self, text):
            raise RuntimeError("нет звука")
    sp = SpeechManager(core.db, core.bus, Dead())
    core.director.speech = sp
    install(core, claude=ScriptedClaude(core.config, [["Ответ есть. Второе предложение."]]))
    drain = audio_events(core)
    tid = await core.director.submit("Что такое API?", "voice")
    await wait_done(core, tid)
    await wait_for(lambda: any(e["kind"] == "speech.end" for e in drain()))
    kinds = [e["kind"] for e in drain()]
    assert "speech.error" in kinds and "speech.audio" not in kinds and "speech.synth" in kinds
    assert core.director.messages(core.director.conversation_id())[-1]["text"].startswith("Ответ есть")


async def test_silero_failure_falls_back_to_piper_and_is_reported():
    from jarvis.voice.tts import VoiceEngine

    class DeadSilero(FakeTTS):
        speaker, rate, cache = "aidar", 1.0, {}

        def available(self):
            return True

        async def synth(self, text, speaker=None, rate=None):
            raise RuntimeError("Silero остановился")

        async def configure(self, speaker, rate):
            self.speaker, self.rate = speaker, rate

    class OkPiper(FakeTTS):
        def available(self):
            return True
    eng = VoiceEngine(DeadSilero(), OkPiper(), "silero:xenia")
    assert (await eng.synth("Привет.")).startswith(b"RIFF") and eng.last_engine == "piper"
    assert "Silero" in eng.last_fallback
    await eng.set_voice("silero:eugene", 1.2)
    assert eng.silero.speaker == "eugene" and eng.silero.rate == 1.2


def test_old_cloud_or_piper_choice_becomes_a_local_voice(core):
    from jarvis.voice.tts import VOICES
    assert all(v["id"].split(":")[0] in ("supertonic", "silero") for v in VOICES)
    assert core.director.speech.engine.voice_id.startswith("silero:")       # Supertonic is not installed in tests


async def test_streamed_voice_chunks_are_played_in_order(core):
    class Chunky(FakeTTS):
        async def synth_stream(self, text):
            for i in range(3):
                yield f"RIFF{i}".encode()
    sp = SpeechManager(core.db, core.bus, Chunky())
    core.director.speech = sp
    install(core, claude=ScriptedClaude(core.config, [["Ответ."]]))
    drain = audio_events(core)
    tid = await core.director.submit("Что такое API?", "voice")
    await wait_done(core, tid)
    await wait_for(lambda: any(e["kind"] == "speech.end" for e in drain()))
    audio = [e for e in drain() if e["kind"] == "speech.audio"]
    assert [base64.b64decode(e["audio_b64"]) for e in audio] == [b"RIFF0", b"RIFF1", b"RIFF2"]
    assert [e["seq"] for e in audio] == [1, 2, 3]


# ---------------------------------------------------------------- real Silero (if installed)
@pytest.mark.skipif(not SileroTTS().available(), reason="Silero is not installed")
async def test_silero_speaks_fast_with_numbers_and_latin():
    s = SileroTTS(speaker="aidar")
    try:
        await s.start()
        await s.synth("Прогрев.")
        t0 = time.perf_counter()
        wav = await s.synth("Готово. Отправил 25 процентов от 800 в Telegram.")
        took = time.perf_counter() - t0
        import io
        import wave
        with wave.open(io.BytesIO(wav)) as w:
            dur = w.getnframes() / w.getframerate()
        assert dur > 2.0 and took < 2.0          # numbers and «Telegram» are spoken, not skipped
        other = await s.synth("Привет.", speaker="xenia")
        assert other[:4] == b"RIFF"
    finally:
        await s.stop()


# ---------------------------------------------------------------- local speech recognition (real GigaAM if installed)
from jarvis.voice.stt import LocalSTT  # noqa: E402


@pytest.mark.skipif(not LocalSTT().available(), reason="local recognition is not installed")
async def test_local_recognition_is_fast_and_russian():
    import io
    import wave as _wave
    tts = SileroTTS(speaker="aidar")
    stt = LocalSTT()
    try:
        raw = await tts.synth("Отправь Ивану в Telegram: привет.")
        with _wave.open(io.BytesIO(raw)) as w:                     # 24 kHz from Silero → the worker resamples
            assert w.getframerate() == 24000
        await stt.start()
        t0 = time.perf_counter()
        text = await stt.transcribe(raw)
        took = time.perf_counter() - t0
        assert "ивану" in text.lower() and "привет" in text.lower() and took < 3.0
        assert await stt.transcribe(_silence()) == ""
    finally:
        await tts.stop()
        await stt.stop()


def _silence() -> bytes:
    import io
    import wave as _wave
    buf = io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\0\0" * 800)
    return buf.getvalue()


async def test_voice_uses_local_recognition_even_fully_offline(core, monkeypatch):
    import base64 as b64
    from jarvis.server import Server

    class FakeLocal:
        name = "gigaam-test"

        def available(self):
            return True

        async def transcribe(self, wav):
            return "Сколько будет 2 плюс 2?"
    core.director.stt = FakeLocal()

    core.db.set_kv("privacy_profile", "STRICT_OFFLINE")           # local recognition works even fully offline
    srv = Server(core)
    r = await srv._voice_turn({"audio_b64": b64.b64encode(b"RIFF" + b"\0" * 4000).decode(), "mime": "audio/wav"})
    assert r["text"] == "Сколько будет 2 плюс 2?" and r["task_id"]
    await wait_done(core, r["task_id"])
    assert core.director.messages(core.director.conversation_id())[-1]["text"].startswith("4")


async def test_slow_answer_gets_a_spoken_hold_phrase(core, speech):
    from jarvis.voice.tts import ACKS
    g = ScriptedClaude(core.config, [["Ответ."]])
    orig = g.run_tool_loop

    async def slow(*a, **k):
        await asyncio.sleep(4.2)          # the hold phrase comes after 3.5 s of silence
        return await orig(*a, **k)
    g.run_tool_loop = slow
    install(core, claude=g)
    tid = await core.director.submit("Что такое API?", "voice")
    await wait_done(core, tid, timeout=8)
    await wait_for(lambda: len(speech.engine.calls) >= 2)
    assert speech.engine.calls[0] in ACKS and speech.engine.calls[1].startswith("Ответ")


FAKE_WORKER = r'''
import base64, json, os, sys
marker = os.environ["FAKE_MARKER"]
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if not os.path.exists(marker):            # first request ever: "crash" without answering
        open(marker, "w").close()
        os._exit(1)
    if "path" in req:
        print(json.dumps({"id": req["id"], "text": "привет"}), flush=True)
    else:
        print(json.dumps({"id": req["id"], "wav_b64": base64.b64encode(b"RIFF" + req["text"].encode()).decode()}), flush=True)
'''


def _fake_worker(tmp_path, monkeypatch):
    import sys
    w = tmp_path / "w.py"
    w.write_text(FAKE_WORKER)
    monkeypatch.setenv("FAKE_MARKER", str(tmp_path / "crashed"))
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").symlink_to(sys.executable)
    return w


async def test_silero_restarts_once_when_its_process_dies(tmp_path, monkeypatch):
    from jarvis.voice.tts import SileroTTS
    s = SileroTTS(home=tmp_path)
    s.worker = _fake_worker(tmp_path, monkeypatch)
    try:
        assert await s.synth("Первая фраза после сбоя.") == b"RIFF" + "Первая фраза после сбоя.".encode()
        old = s.proc.pid
        s.proc.kill()                                   # killed while idle (OOM, update, user)
        await s.proc.wait()
        assert (await s.synth("Вторая.")).startswith(b"RIFF") and s.proc.pid != old
    finally:
        await s.stop()


async def test_silero_phrase_cache_is_bounded(tmp_path, monkeypatch):
    from jarvis.voice import tts
    monkeypatch.setattr(tts, "CACHE_MAX", 3)
    s = tts.SileroTTS(home=tmp_path)
    s.worker = _fake_worker(tmp_path, monkeypatch)
    (tmp_path / "crashed").touch()
    try:
        for i in range(10):
            await s.synth(f"Фраза номер {i}.")
        assert len(s.cache) == 3
    finally:
        await s.stop()


async def test_local_stt_restarts_once_when_its_process_dies(tmp_path, monkeypatch):
    from jarvis.voice.stt import LocalSTT
    s = LocalSTT(home=tmp_path)
    s.worker = _fake_worker(tmp_path, monkeypatch)
    try:
        assert await s.transcribe(b"RIFF....WAVEfmt ") == "привет"
    finally:
        await s.stop()


def test_piper_creates_no_temp_dir_until_used(tmp_path):
    import tempfile
    from pathlib import Path
    from jarvis.voice.tts import PiperTTS
    before = set(Path(tempfile.gettempdir()).glob("jarvis-tts-*"))
    PiperTTS(home=tmp_path)
    assert set(Path(tempfile.gettempdir()).glob("jarvis-tts-*")) == before


class _SeqSTT:
    name = "gigaam-test"

    def __init__(self, *texts):
        self.texts, self.calls = list(texts), 0

    def available(self):
        return True

    async def transcribe(self, wav):
        self.calls += 1
        return self.texts.pop(0)


def _wav_b64():
    import base64 as b64
    return b64.b64encode(b"RIFF" + b"\0" * 4000).decode()


async def test_finished_phrase_is_answered_after_a_short_pause_with_one_recognition(core):
    from jarvis.server import Server
    stt = core.director.stt = _SeqSTT("Сколько будет 2 плюс 2?")
    srv = Server(core)
    p = await srv._voice_probe({"audio_b64": _wav_b64(), "mime": "audio/wav"})
    assert p["complete"] is True
    r = await srv._voice_commit({"probe_id": p["probe_id"], "speech_start": 1.0, "speech_end": 2.0})
    assert r["task_id"] and stt.calls == 1                     # the probe's transcript is reused, no second STT
    await wait_done(core, r["task_id"])
    t = core.director.speech.turns(1)[0] if core.director.speech else None
    if t:
        assert t["transcript"] == "Сколько будет 2 плюс 2?" and t["stt_end"]


async def test_cut_off_phrase_waits_for_the_full_pause(core):
    from jarvis.server import Server
    stt = core.director.stt = _SeqSTT("Отправь маме в Telegram.", "Открой мою.")
    srv = Server(core)
    p = await srv._voice_probe({"audio_b64": _wav_b64(), "mime": "audio/wav"})
    assert p["complete"] is False                  # the message itself may follow: the window keeps listening
    # Nothing more was said during the full pause: the same transcript is used (no second recognition).
    r = await srv._voice_commit({"probe_id": p["probe_id"], "submit": False})
    assert r["text"] == "Отправь маме в Telegram." and stt.calls == 1
    p2 = await srv._voice_probe({"audio_b64": _wav_b64(), "mime": "audio/wav"})
    assert p2["complete"] is False                 # «…мою» is cut off


async def test_expired_probe_is_refused(core):
    from jarvis.server import Server
    srv = Server(core)
    with pytest.raises(ValueError):
        await srv._voice_commit({"probe_id": "nope"})
