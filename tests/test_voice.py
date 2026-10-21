"""Voice conversation: fast path, streaming speech, interruption, telemetry."""
import asyncio
import base64
import time

import pytest

from jarvis.calc import answer
from jarvis.voice.speech import SentenceBuffer, SpeechManager
from jarvis.voice.tts import PiperTTS, clean_for_speech

from conftest import ScriptedGemini, install, wait_done, wait_for


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
    g = ScriptedGemini(core.config, [])
    install(core, gemini=g)
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
    g = ScriptedGemini(core.config, [["Искусственный интеллект — это область науки. ", "Она создаёт системы, ",
                                      "которые решают задачи. Например, распознают речь."]])
    install(core, gemini=g)
    drain = audio_events(core)
    tid = await core.director.submit("Что такое искусственный интеллект?", "voice")
    await wait_done(core, tid)
    await wait_for(lambda: any(e["kind"] == "speech.end" for e in drain()))
    audio = [e for e in drain() if e["kind"] == "speech.audio"]
    assert [e["seq"] for e in audio] == list(range(1, len(audio) + 1)) and len(audio) >= 2
    assert audio[0]["text"].startswith("Искусственный интеллект")
    assert base64.b64decode(audio[0]["audio_b64"]).startswith(b"RIFF")
    # the voice-mode instruction and the fast voice model were used
    assert g.calls and "голосового разговора" in g.last_system and g.last_model == core.config.model_voice


async def test_interrupt_stops_speech_and_cancels_answer_but_not_actions(core, speech, home):
    speech.engine.delay = 0.2
    g = ScriptedGemini(core.config, [["Первое предложение длинного ответа. "] * 3])
    install(core, gemini=g)
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
    g = ScriptedGemini(core.config, [["Майнинг — это проверка блоков. "]])
    install(core, gemini=g)
    tid = await core.director.submit("Расскажи подробно, как работает майнинг Bitcoin", "voice")
    await wait_for(lambda: speech.engine.calls)
    from jarvis.voice.tts import ACKS
    assert speech.engine.calls[0] in ACKS          # spoken before any model output, no AI call for it
    await wait_done(core, tid)


async def test_long_answers_are_capped_in_speech(core, speech):
    g = ScriptedGemini(core.config, [["Очень длинное предложение номер один про всё на свете. " * 40]])
    install(core, gemini=g)
    tid = await core.director.submit("Что такое блокчейн?", "voice")
    await wait_done(core, tid)
    await wait_for(lambda: speech.engine.calls and speech.engine.calls[-1] == "Остальное — на экране.")
    assert sum(len(c) for c in speech.engine.calls[:-1]) <= 1300


async def test_typed_requests_are_not_spoken_unless_asked(core, speech):
    g = ScriptedGemini(core.config, [["Ответ."], ["Ответ два."]])
    install(core, gemini=g)
    await wait_done(core, await core.director.submit("Что такое API?"))
    assert not speech.engine.calls
    await wait_done(core, await core.director.submit("Что такое REST?", speak=True))
    await wait_for(lambda: speech.engine.calls)


async def test_follow_up_keeps_recent_context(core, speech):
    g = ScriptedGemini(core.config, [["Илон Маск — предприниматель."], ["Ему 55 лет."]])
    install(core, gemini=g)
    await wait_done(core, await core.director.submit("Кто такой Илон Маск?", "voice"))
    await wait_done(core, await core.director.submit("А сколько ему лет?", "voice"))
    sent = " ".join(p.text or "" for c in g.calls[-1] for p in c.parts)
    assert "Илон Маск" in sent


def test_compact_history_is_bounded():
    from jarvis.agency.agency import compact_history
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "text": f"сообщение {i} " + "х" * 2000} for i in range(40)]
    recent, summary = compact_history(msgs)
    assert len(recent) == 6 and all(len(m["text"]) <= 600 for m in recent) and len(summary) < 900


# ---------------------------------------------------------------- telemetry
async def test_voice_turn_telemetry_records_real_stages(core, speech):
    g = ScriptedGemini(core.config, [["Готово, это просто. Вот ответ."]])
    install(core, gemini=g)
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


async def test_tts_falls_back_and_reports_failure():
    from jarvis.voice.tts import FallbackTTS

    class Broken(FakeTTS):
        name = "broken"

        async def synth(self, text):
            raise RuntimeError("engine died")

        def available(self):
            return True
    Broken.cache = {}
    good = FakeTTS(); good.available = lambda: True
    fb = FallbackTTS(Broken(), good)
    assert (await fb.synth("Привет.")).startswith(b"RIFF") and fb.last_engine == "fake"
    fb2 = FallbackTTS(Broken(), None)
    with pytest.raises(RuntimeError):
        await fb2.synth("Привет.")


async def test_speech_failure_is_reported_and_text_stays(core):
    class Dead(FakeTTS):
        async def synth(self, text):
            raise RuntimeError("нет звука")
    sp = SpeechManager(core.db, core.bus, Dead())
    core.director.speech = sp
    install(core, gemini=ScriptedGemini(core.config, [["Ответ есть. Второе предложение."]]))
    drain = audio_events(core)
    tid = await core.director.submit("Что такое API?", "voice")
    await wait_done(core, tid)
    await wait_for(lambda: any(e["kind"] == "speech.end" for e in drain()))
    kinds = [e["kind"] for e in drain()]
    assert "speech.error" in kinds and "speech.audio" not in kinds and "speech.synth" in kinds
    assert core.director.messages(core.director.conversation_id())[-1]["text"].startswith("Ответ есть")


async def test_cloud_voice_falls_back_to_local_and_reports_it():
    from jarvis.voice.tts import VoiceEngine

    class FakePiper(FakeTTS):
        voice, rate = "ru_RU-denis-medium", 1.0
        cache = {}

        async def configure(self, voice, rate):
            self.voice, self.rate = voice, rate

    class DeadGemini:
        def configured(self):
            return True

        def client(self):
            raise RuntimeError("429 quota")
    p = FakePiper()
    eng = VoiceEngine(p, DeadGemini(), "gemini:Charon")
    out = [c async for c in eng.synth_stream("Привет.")]
    assert out == [b"RIFF" + "Привет.".encode()] and eng.last_engine == "piper" and "429" in eng.last_fallback
    await eng.set_voice("piper:ruslan", 1.2)
    assert p.voice == "ru_RU-ruslan-medium" and p.rate == 1.2 and eng.cloud is None


async def test_streamed_voice_chunks_are_played_in_order(core):
    class Chunky(FakeTTS):
        async def synth_stream(self, text):
            for i in range(3):
                yield f"RIFF{i}".encode()
    sp = SpeechManager(core.db, core.bus, Chunky())
    core.director.speech = sp
    install(core, gemini=ScriptedGemini(core.config, [["Ответ."]]))
    drain = audio_events(core)
    tid = await core.director.submit("Что такое API?", "voice")
    await wait_done(core, tid)
    await wait_for(lambda: any(e["kind"] == "speech.end" for e in drain()))
    audio = [e for e in drain() if e["kind"] == "speech.audio"]
    assert [base64.b64decode(e["audio_b64"]) for e in audio] == [b"RIFF0", b"RIFF1", b"RIFF2"]
    assert [e["seq"] for e in audio] == [1, 2, 3]
