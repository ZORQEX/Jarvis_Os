"""The microphone broken after sleep (voice/mic.py): measured speed, the plain sentence instead of «Не расслышал»,
the check tool and the reboot it offers. Nothing here records the real microphone."""
import base64
import sys

from conftest import wait_for

from jarvis.router import route
from jarvis.tools import machine
from jarvis.tools.base import ToolContext
from jarvis.voice import mic


def _speed(value):
    async def fake(*a, **k):
        return value
    return fake


# ------------------------------------------------------------------ measuring

async def test_capture_speed_of_a_steady_and_a_slow_stream(tmp_path):
    """A fake recorder: 16 kHz s16 at full speed (healthy) and at 1/5 speed (the broken card measured 0.17–0.18)."""
    def recorder(rate_factor):
        script = tmp_path / f"rec{rate_factor}.py"
        script.write_text(
            "import sys, time\n"
            f"chunk, pause = 3200, 0.1 / {rate_factor}\n"
            "while True:\n"
            "    sys.stdout.buffer.write(bytes(chunk)); sys.stdout.buffer.flush(); time.sleep(pause)\n")
        return [sys.executable, str(script)]
    ok = await mic.capture_speed(seconds=1.0, argv=recorder(1))
    slow = await mic.capture_speed(seconds=1.0, argv=recorder(0.2))
    assert 0.8 < ok < 1.2 and not mic.broken(ok)
    assert slow < 0.35 and mic.broken(slow)


async def test_capture_speed_without_sound_or_recorder(tmp_path):
    silent = tmp_path / "silent.py"
    silent.write_text("import time\ntime.sleep(10)\n")
    assert await mic.capture_speed(seconds=0.5, wait_first=0.5, argv=[sys.executable, str(silent)]) == 0.0
    assert await mic.capture_speed(argv=["no-such-recorder-xyz"]) is None
    assert not mic.broken(None)


# ------------------------------------------------------------------ routing and the tool

def test_phrases_route_to_the_check():
    """Re-binding the driver did not help the real broken mic (tried live): «почини» measures and says what helps."""
    for phrase in ("Почини микрофон", "микрофон не работает", "перезапусти звуковую карту", "проверь микрофон",
                   "почему ты меня не слышишь"):
        assert route(phrase).tool == "mic_check", phrase
    assert route("какие микрофоны").tool == "audio_devices"          # the device list stays as it was


async def test_check_says_broken_or_fine(monkeypatch, cfg):
    ctx = ToolContext(config=cfg, task_id="t")
    monkeypatch.setattr(mic, "capture_speed", _speed(0.18))
    r = await machine.MicCheck().run({}, ctx)
    assert r.data["broken"] and "в 6 раз медленнее" in r.summary and "перезагрузи компьютер" in r.summary
    monkeypatch.setattr(mic, "capture_speed", _speed(1.0))
    r = await machine.MicCheck().run({}, ctx)
    assert not r.data["broken"] and "в порядке" in r.summary


def test_check_is_read_only_and_the_reboot_it_offers_always_asks(core):
    assert core.director.gw.registry.get("mic_check").spec.risk == "low"
    assert route("Перезагрузи компьютер").tool == "power_action"          # the companion's button sends this
    assert core.director.gw.registry.get("power_action").spec.risk == "high"


# ------------------------------------------------------------------ the voice path

class _EmptySTT:
    name = "fake"

    def available(self):
        return True

    async def transcribe(self, audio, **k):
        return ""

    async def stop(self):
        pass


LONG_WAV = base64.b64encode(b"RIFF" + bytes(48000)).decode()     # ~1.5 s of 16 kHz sound
SHORT_WAV = base64.b64encode(b"RIFF" + bytes(4000)).decode()


async def test_empty_phrase_with_a_broken_mic_says_what_helps(core, monkeypatch):
    from jarvis.server import MIC_SAY, Server
    monkeypatch.setattr(mic, "capture_speed", _speed(0.18))
    srv = Server(core)
    core.director.stt = _EmptySTT()
    said = []
    monkeypatch.setattr(core.director.speech, "say", lambda key, text, *a, **k: said.append(text))
    q = core.bus.subscribe()
    r = await srv._voice_turn({"audio_b64": LONG_WAV, "mime": "audio/wav"})
    assert r["mic"] == "broken" and said == [MIC_SAY]
    msgs = core.director.messages(core.director.conversation_id())
    assert "перезагрузка компьютера" in msgs[-1]["text"] and msgs[-1]["role"] == "assistant"
    kinds = []
    while not q.empty():
        kinds.append(q.get_nowait().get("kind"))
    assert "voice.mic" in kinds


async def test_short_noise_or_a_healthy_mic_keeps_not_heard(core, monkeypatch):
    from jarvis.server import Server
    from jarvis.voice.tts import NOT_HEARD
    measured = []

    async def capture(*a, **k):
        measured.append(1)
        return 1.0
    monkeypatch.setattr(mic, "capture_speed", capture)
    srv = Server(core)
    core.director.stt = _EmptySTT()
    said = []
    monkeypatch.setattr(core.director.speech, "say", lambda key, text, *a, **k: said.append(text))
    await srv._voice_turn({"audio_b64": SHORT_WAV, "mime": "audio/wav"})       # a click: not measured
    assert said == [NOT_HEARD] and not measured
    await srv._voice_turn({"audio_b64": LONG_WAV, "mime": "audio/wav"})        # healthy: measured once …
    await srv._voice_turn({"audio_b64": LONG_WAV, "mime": "audio/wav"})        # … then trusted for 5 min
    assert said == [NOT_HEARD] * 3 and len(measured) == 1


async def test_wake_listening_tells_a_broken_mic_once(core, monkeypatch):
    from jarvis.server import MIC_SAY, Server
    monkeypatch.setattr(mic, "capture_speed", _speed(0.18))
    srv = Server(core)
    core.director.stt = _EmptySTT()
    said = []
    monkeypatch.setattr(core.director.speech, "say", lambda key, text, *a, **k: said.append(text))
    for _ in range(3):
        r = await srv._voice_turn({"audio_b64": LONG_WAV, "mime": "audio/wav", "wake": True})
        assert r.get("ignored")
    await wait_for(lambda: said)
    assert said == [MIC_SAY]
