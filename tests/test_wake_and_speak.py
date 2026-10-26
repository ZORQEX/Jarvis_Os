"""«Джарвис» right after the lid opens, and every finished task said aloud — spoken or typed (the user's request
2026-10-26). Nothing here records the real microphone or plays sound."""
import json
import time

from conftest import wait_for

from jarvis import server
from jarvis.router import WAKE_REPLY, route
from jarvis.voice import mic


def _spoken(core, monkeypatch):
    started = []
    real = core.director.speech.start

    def start(task_id, *a, **k):
        started.append(task_id)
        return real(task_id, *a, **k)
    monkeypatch.setattr(core.director.speech, "start", start)
    return started


async def test_bare_name_answers_that_it_listens():
    assert route("Джарвис.").reply == WAKE_REPLY == "Слушаю."
    assert route("Эй, Джарвис").reply == "Слушаю."


async def test_typed_answers_are_spoken_unless_turned_off(core, monkeypatch):
    started = _spoken(core, monkeypatch)
    d = core.director
    tid = await d.submit("Джарвис", "text")
    assert tid in started
    await wait_for(lambda: core.tasks.get(tid)["state"] == "completed")
    tid = await d.submit("Джарвис", "cli")                 # `jarvis «…»` in a terminal: the person is at the computer
    assert tid in started
    tid = await d.submit("Джарвис", "api")                 # programs and the phone stay silent
    assert tid not in started
    core.db.set_kv("speak_typed", False)
    tid = await d.submit("Джарвис", "text")
    assert tid not in started
    tid = await d.submit("Джарвис", "voice")               # a spoken request is always answered aloud
    assert tid in started


async def test_a_reminder_is_said(core, monkeypatch):
    said = []
    monkeypatch.setattr(core.director.speech, "say", lambda key, text, *a, **k: said.append(text))
    core.director._on_reminder({"id": "r1", "text": "позвонить маме", "due_at": time.time()}, False)
    core.director._on_reminder({"id": "r2", "text": "старое", "due_at": time.time() - 3600}, True)    # missed: only shown
    assert said == ["Напоминание: позвонить маме"]


async def test_typed_in_the_companion_is_spoken_by_the_companion(ws_client, core):
    tc = ws_client
    await tc.ws.send_str(json.dumps({"type": "submit", "req_id": 1, "text": "Джарвис", "client": "overlay"}))
    await tc.ws.send_str(json.dumps({"type": "submit", "req_id": 2, "text": "Джарвис", "client": "evil"}))
    got = {}
    for _ in range(80):
        m = json.loads((await tc.ws.receive(timeout=5)).data)
        if m["type"] == "reply":
            got[m["req_id"]] = m["data"]["task_id"]
        if len(got) == 2:
            break
    assert core.director.speech_owner.get(got[1]) == "overlay"
    assert got[2] not in core.director.speech_owner                 # unknown client → the main window


async def test_waking_from_sleep_checks_the_mic_twice(core, monkeypatch):
    monkeypatch.setattr(server, "SLEEP_CHECK_WAITS", (0, 0))
    srv = server.Server(core)
    said = []
    monkeypatch.setattr(core.director.speech, "say", lambda key, text, *a, **k: said.append(text))
    speeds = [0.18, 1.0]                                  # settling right after waking, then fine: nothing said

    async def speed(*a, **k):
        return speeds.pop(0)
    monkeypatch.setattr(mic, "capture_speed", speed)
    await srv._mic_after_sleep()
    assert said == [] and speeds == []
    speeds[:] = [0.18, 0.17]                              # still broken a few seconds later: said at once
    await srv._mic_after_sleep()
    assert said == [server.MIC_SAY]


async def test_woke_and_reopened_are_recorded(ws_client, core, monkeypatch):
    monkeypatch.setattr(server, "SLEEP_CHECK_WAITS", (0, 0))
    tc = ws_client
    for i, (typ, extra) in enumerate((("voice.woke", {"slept_s": 3600}), ("voice.reopened", {"why": "silent"}))):
        await tc.ws.send_str(json.dumps({"type": typ, "req_id": i, "client": "overlay", **extra}))
    await wait_for(lambda: {"voice.woke", "voice.mic_reopened"} <= {json.loads(x)["kind"] for x in core.audit.path.read_text("utf-8").splitlines()})


class _TurnSTT:
    """Text plus Smart Turn's «finished» probability, as the real worker reports them."""
    name = "fake"

    def __init__(self, text, p):
        self.text, self.last_turn = text, p

    def available(self):
        return True

    async def transcribe(self, wav, wake=False, turn=False):
        return self.text


async def test_a_bare_name_that_sounds_finished_is_answered_after_the_short_pause(core):
    """Measured on Silero recordings: «Джарвис.» 0.81, «Джарвис?» 0.51, «Джарвис, открой» 0.05."""
    import base64
    srv = server.Server(core)
    wav = base64.b64encode(b"RIFF" + bytes(4000)).decode()
    for text, p, done in (("Джарвис.", 0.81, True), ("Джарвис?", 0.51, True), ("Джарвис.", 0.1, False),
                          ("Джарвис.", None, False), ("Джарвис, открой", 0.05, False)):
        core.director.stt = _TurnSTT(text, p)
        r = await srv._voice_probe({"audio_b64": wav, "mime": "audio/wav", "wake": True, "client": "overlay"})
        assert r["complete"] is done, (text, p)
