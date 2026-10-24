"""Several tasks in one request, phrases that only look cut off, stopping one answer without losing the other tasks,
one voice at a time, and the two windows not hearing each other (measured problems of 2026-10-24)."""
import asyncio
import time

import pytest

import jarvis.server as server_mod
from jarvis.router import split_tasks
from jarvis.server import is_fragment
from jarvis.voice.speech import SpeechManager, strip_echo
from jarvis.voice.tts import PREVIEW

from conftest import ScriptedClaude, install, wait_done, wait_for
from test_overlay import WAV, FakeSTT, request
from test_voice import FakeTTS, audio_events


# ---------------------------------------------------------------- a phrase that only looks cut off
def test_tell_me_something_is_a_whole_phrase():
    assert not is_fragment("Джарвис, расскажи что-нибудь.") and not is_fragment("Расскажи что-нибудь")
    assert not is_fragment("Позвони кому-нибудь.") and not is_fragment("Что ещё?") and not is_fragment("Ты где?")
    assert is_fragment("Найди какую-нибудь") and is_fragment("Открой браузер, а потом")
    long_cut = "Первое, найди лабораторные в папках. Далее открой браузер и найди картинку. Далее напиши в Телеграме, контакту"
    assert is_fragment(long_cut)                    # a long dictation cut off on «контакту» still waits for the name


async def test_cut_off_phrase_with_no_continuation_is_done_after_a_pause(ws_client, core, monkeypatch):
    monkeypatch.setattr(server_mod, "FRAGMENT_WAIT_S", 0.3)
    tc = ws_client
    core.director.stt = FakeSTT()
    core.director.stt.texts = ["Напомни мне"]
    m = await request(tc, "voice.transcribe", 1, audio_b64=WAV, mime="audio/wav")
    assert m["data"].get("pending") and not m["data"].get("task_id")
    await wait_for(lambda: any(t["request"] == "Напомни мне" for t in core.tasks.recent(5)), timeout=3)


async def test_the_rest_said_in_time_is_joined(ws_client, core, monkeypatch):
    monkeypatch.setattr(server_mod, "FRAGMENT_WAIT_S", 0.3)
    tc = ws_client
    core.director.stt = FakeSTT()
    core.director.stt.texts = ["Напомни мне", "через 10 минут выключить чайник"]
    await request(tc, "voice.transcribe", 1, audio_b64=WAV, mime="audio/wav")
    await request(tc, "voice.active", 2, on=True)        # the person is still talking: no answer to the half phrase
    await asyncio.sleep(0.6)
    assert not any(t["request"] == "Напомни мне" for t in core.tasks.recent(5))
    m = await request(tc, "voice.transcribe", 3, audio_b64=WAV, mime="audio/wav")
    assert m["data"]["text"] == "Напомни мне через 10 минут выключить чайник" and m["data"]["task_id"]


# ---------------------------------------------------------------- several tasks in one request
SAID = ("Смотри, дослушай, я сейчас говорю полностью задачу. Первое, тебе надо найти папку лабораторную, она называется "
        "под названием Колледж. А далее тебе нужно написать в Телеграме под контактом«Любимое с сердечком»,«привет», "
        "и далее открыть и найти какую-нибудь картинку в браузере с шуруповёртом.")


def test_a_request_with_several_tasks_is_split_in_order():
    assert split_tasks(SAID) == ["найти папку лабораторную, она называется под названием Колледж.",
                                 "написать в Телеграме под контактом«Любимое с сердечком»,«привет»",
                                 "открыть и найти какую-нибудь картинку в браузере с шуруповёртом."]
    assert split_tasks("Джарвис, во-первых, открой терминал. Во-вторых, покажи загрузки.") == \
        ["открой терминал.", "покажи загрузки."]
    assert split_tasks("1) открой браузер 2) сделай громкость 30") == ["открой браузер", "сделай громкость 30"]
    assert split_tasks("Открой браузер и потом найди файл смета") == ["Открой браузер", "найди файл смета"]


@pytest.mark.parametrize("one", [
    "Напиши маме: сначала купи хлеб, потом молоко",          # the text of a message is never cut
    "Найди файлы сначала в загрузках, потом в документах",   # one task with two places
    "Расскажи, что ты делал потом", "Найди файл и открой его", "Расскажи что-нибудь.", "Первое.",
    "Отправь Любимой «сначала работа, потом отдых»",
])
def test_a_single_task_is_not_split(one):
    assert split_tasks(one) is None


async def test_tasks_said_together_run_one_after_another(core):
    d = core.director
    first = await d.submit("Первое, сколько будет 2 + 2. Второе, какое сегодня число.", "text")
    await wait_for(lambda: len([t for t in core.tasks.recent(10) if t["state"] == "completed"]) >= 2)
    done = [t for t in core.tasks.recent(10) if t["state"] == "completed"][:2][::-1]
    assert [t["request"] for t in done] == ["сколько будет 2 + 2.", "какое сегодня число."] and done[0]["id"] == first
    assert done[1]["created_at"] >= done[0]["finished_at"]           # the second started after the first ended
    plan = [m for m in d.messages(d.conversation_id()) if (m["meta"] or {}).get("route") == "batch"]
    assert plan and "Задач: 2" in plan[0]["text"]


async def test_stop_during_a_series_does_not_start_the_rest(core):
    d = core.director
    gate = asyncio.Event()

    class Slow(ScriptedClaude):
        async def run_tool_loop(self, *a, **k):
            await gate.wait()
            return await super().run_tool_loop(*a, **k)
    install(core, claude=Slow(core.config, [["Готово."]] * 3))
    first = await d.submit("Во-первых, придумай название для кафе. Во-вторых, придумай слоган для кафе.", "voice")
    await wait_for(lambda: first in d.rt)
    await d.control("interrupt")                     # «стоп»: the current one and the rest of the series
    await wait_done(core, first)
    await asyncio.sleep(0.2)
    assert not any(t["request"].startswith("придумай слоган") for t in core.tasks.recent(10))
    gate.set()


# ---------------------------------------------------------------- «стоп» stops what is heard, not the other tasks
async def test_silencing_one_answer_keeps_other_tasks_running(core):
    d = core.director
    sp = SpeechManager(core.db, core.bus, FakeTTS())
    d.speech = sp
    gate = asyncio.Event()

    class Slow(ScriptedClaude):
        async def run_tool_loop(self, *a, **k):
            if self.calls:                               # the second question waits in the background
                await gate.wait()
            return await super().run_tool_loop(*a, **k)
    install(core, claude=Slow(core.config, [["Это первый ответ, он сейчас звучит. "], ["Второй ответ."]]))
    a = await d.submit("Что такое API?", "voice")
    await wait_done(core, a)
    b = await d.submit("Что такое REST?", "text")
    await wait_for(lambda: b in d.rt)
    sp.played(a, "Это первый ответ, он сейчас звучит.", seconds=3)
    await d.control("quiet", task_ids=[a])               # Esc / «замолчи» while the first answer plays
    await asyncio.sleep(0.1)
    assert b in d.rt and core.tasks.get(b)["state"] not in ("cancelled", "failed")
    gate.set()
    assert (await wait_done(core, b))["state"] == "completed"


async def test_barge_in_remembers_what_it_cut_for_a_following_stop(core):
    sp = SpeechManager(core.db, core.bus, FakeTTS())
    core.director.speech = sp
    sp.played("t_a", "Длинный ответ про майнинг.", seconds=5)
    await core.director.control("stop_speech", task_ids=["t_a"])
    assert sp.audible_tasks() == [] and sp.silenced[1] == ["t_a"]


# ---------------------------------------------------------------- one voice at a time
async def test_answers_of_two_tasks_are_not_mixed(core):
    sp = SpeechManager(core.db, core.bus, FakeTTS(delay=0.02))
    drain = audio_events(core)
    a, b = sp.start("t_a", None), sp.start("t_b", None)
    a.say("А один.")
    b.say("Бэ один.")
    a.say("А два.")
    a.finish()
    b.finish()
    await wait_for(lambda: sum(1 for e in drain() if e["kind"] == "speech.end") >= 2)
    said = [e["text"] for e in drain() if e["kind"] == "speech.audio"]
    assert said == ["А один.", "А два.", "Бэ один."]


async def test_jarvis_waits_while_the_person_is_talking(core):
    sp = SpeechManager(core.db, core.bus, FakeTTS())
    sp.hold_until = time.time() + 30
    s = sp.start("t_a", None)
    s.say("Готово.")
    s.finish()
    await asyncio.sleep(0.3)
    assert not sp.engine.calls
    sp.hold_until = 0
    await wait_for(lambda: sp.engine.calls)


# ---------------------------------------------------------------- the two windows do not hear each other
def test_echo_sentences_are_cut_and_the_person_is_kept():
    heard = ("Джарвис. Как я буду звучать, когда отвечаешь? Привет, я Джарвис. Так я буду звучать, когда отвечаю. "
             "Что сделаешь?")
    assert strip_echo(heard, [PREVIEW]) == "Джарвис. Как я буду звучать, когда отвечаешь? Что сделаешь?"
    assert strip_echo("Привет. Я Джарви.", [PREVIEW]) == ""
    # an answer to JARVIS's question repeats its words but is the person's
    assert strip_echo("Открой подключение к PostgreSQL.", []) == "Открой подключение к PostgreSQL."


async def test_the_other_windows_voice_is_not_a_command(ws_client, core):
    """The main window plays the voice sample; the companion's microphone hears it during that time: dropped. The same
    words said later (nothing playing) are the person's."""
    tc = ws_client
    core.director.stt = FakeSTT()
    tc.srv._wake_until = 0
    t = time.time()
    core.director.speech.played("preview_1", PREVIEW, seconds=4)
    core.director.stt.texts = ["Привет! Я Джарвис. Так я буду звучать, когда отвечаю. Что сделать?"]
    m = await request(tc, "voice.transcribe", 1, audio_b64=WAV, mime="audio/wav", wake=True, client="overlay",
                      speech_start=t + 0.2, speech_end=t + 3.5)
    assert m["data"].get("ignored")
    core.director.stt.texts = ["Джарвис, так я буду звучать, когда отвечаю?"]
    m = await request(tc, "voice.transcribe", 2, audio_b64=WAV, mime="audio/wav", wake=True, client="overlay",
                      speech_start=t + 20, speech_end=t + 22)
    assert m["data"].get("task_id")


async def test_companion_knows_the_main_window_has_the_microphone(ws_client, core):
    tc = ws_client
    await request(tc, "voice.presence", 1, client="window", listening=True)
    assert (await request(tc, "overlay.state", 2))["data"]["window_listening"] is True
    q = core.bus.subscribe()
    await tc.ws.close()                               # the main window closed: its microphone is free again
    await wait_for(lambda: not q.empty())
    ev = q.get_nowait()
    assert ev["kind"] == "voice.presence" and ev["listening"] is False


async def test_name_followed_by_a_dash_is_addressed(ws_client, core):
    """«Джарвис— первое, …»: after a pause after the name the recogniser writes a dash. Such phrases were dropped by the
    companion as not addressed (measured 2026-10-24 with a recorded voice)."""
    tc = ws_client
    core.director.stt = FakeSTT()
    tc.srv._wake_until = 0
    core.director.stt.texts = ["Джарвис— который час?"]
    m = await request(tc, "voice.transcribe", 1, audio_b64=WAV, mime="audio/wav", wake=True, client="overlay")
    assert m["data"].get("task_id")


async def test_every_task_of_a_series_is_spoken_by_the_window_that_heard_it(core):
    d = core.director
    first = await d.submit("Первое, сколько будет 2 + 2. Второе, какое сегодня число.", "voice", client="overlay")
    await wait_for(lambda: len([t for t in core.tasks.recent(10) if t["state"] == "completed"]) >= 2)
    ids = [t["id"] for t in core.tasks.recent(10)][:2]
    assert first in ids and all(d.speech_owner.get(t) == "overlay" for t in ids)


class TurnSTT(FakeSTT):
    """A recogniser that also says how finished the voice sounds (Smart Turn)."""

    def __init__(self, items):
        self.items = list(items)
        self.last_turn = None

    async def transcribe(self, audio, wake=False, turn=False):
        text, p = self.items.pop(0)
        self.last_turn = p if turn else None
        return text


async def test_a_voice_that_trails_off_waits_for_the_rest(ws_client, core, monkeypatch):
    monkeypatch.setattr(server_mod, "FRAGMENT_WAIT_S", 5)
    tc = ws_client
    core.director.stt = TurnSTT([("Первое, найди папку колледж.", 0.04), ("Далее открой браузер.", 0.97)])
    m = await request(tc, "voice.transcribe", 1, audio_b64=WAV, mime="audio/wav")
    assert m["data"].get("pending")                         # the words look finished, the voice does not
    m = await request(tc, "voice.transcribe", 2, audio_b64=WAV, mime="audio/wav")
    assert m["data"]["text"] == "Первое, найди папку колледж Далее открой браузер." and m["data"]["task_id"]


async def test_no_early_answer_when_the_voice_is_not_done(ws_client, core):
    tc = ws_client
    core.director.stt = TurnSTT([("Найди папку колледж.", 0.1), ("Найди папку колледж.", 0.9)])
    assert (await request(tc, "voice.probe", 1, audio_b64=WAV, mime="audio/wav"))["data"]["complete"] is False
    assert (await request(tc, "voice.probe", 2, audio_b64=WAV, mime="audio/wav"))["data"]["complete"] is True
