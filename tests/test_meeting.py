"""Meeting notes with a fake recorder, fake recognition and a scripted Claude (VERIFIED MOCK; the real PipeWire
recording is checked by hand)."""
import asyncio
import math
import struct
import sys
from pathlib import Path

import pytest

from conftest import wait_done, wait_for
from jarvis.meeting import RATE, chunks

FAKE_RECORDER = r'''
import math, signal, struct, sys, time
path = sys.argv[-1]
f = open(path, "wb")
f.write(b"RIFF\0\0\0\0WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) + b"data\0\0\0\0")
stop = False
def done(*a):
    global stop
    stop = True
signal.signal(signal.SIGINT, done)
signal.signal(signal.SIGTERM, done)
t = 0
while not stop:
    # 1 s of «speech» (a tone), then 0.5 s of quiet — played fast: 3 s of audio per 0.1 s
    for _ in range(2):
        f.write(b"".join(struct.pack("<h", int(8000 * math.sin(i / 8))) for i in range(16000)))
        f.write(b"\0\0" * 8000)
    f.flush()
    time.sleep(0.1)
f.close()
'''


class FakeSTT:
    def __init__(self):
        self.n = 0

    def available(self):
        return True

    async def transcribe(self, wav, **k):
        self.n += 1
        assert wav[:4] == b"RIFF"
        return f"фраза {self.n}"


class Claude:
    id = "claude"

    def __init__(self, fail=None):
        self.fail, self.got = fail, None

    def configured(self):
        return True

    async def generate(self, req, on_delta, progress):
        if self.fail:
            raise RuntimeError(self.fail)
        self.got = req
        from jarvis.providers.base import GenResult
        return GenResult("## Кратко\nОбсудили фабрику.\n## Решения\n- Заказать ткань\n## Задачи\n- [ ] Алина: позвонить",
                         "claude", "test")


@pytest.fixture
def meet(core, tmp_path, monkeypatch):
    rec = tmp_path / "pw-record"
    rec.write_text(f"#!{sys.executable}\n" + FAKE_RECORDER)
    rec.chmod(0o755)
    m = core.director.meetings
    m.recorder = str(rec)
    core.director.stt = FakeSTT()
    claude = Claude()
    monkeypatch.setattr(m.manager, "get", lambda pid: claude if pid == "claude" else None)
    core.db.set_kv("privacy_profile", "AUTHORIZED_CLOUD")
    m.claude = claude
    return m


async def approve_next(core):
    await wait_for(lambda: core.approvals.pending())
    a = core.approvals.pending()[0]
    core.director.decide(a["id"], True)
    return a


async def test_a_meeting_is_recorded_transcribed_and_summarised(meet, core, home):
    tid = await core.director.submit("начни запись встречи с фабрикой", source="text")
    a = await approve_next(core)                                    # recording is asked first
    assert a["tool_id"] == "meeting_start"
    t = await wait_done(core, tid)
    assert t["state"] == "completed" and t["steps"][-1]["verification"]["status"] == "verified"
    assert core.director.core_state()["meeting"]["title"] == "Встреча с фабрикой"
    await asyncio.sleep(0.5)
    t = await wait_done(core, await core.director.submit("останови запись встречи", source="text"), timeout=15)
    assert t["state"] == "completed", t
    notes = Path(t["steps"][-1]["result"]["data"]["path"])
    text = notes.read_text("utf-8")
    assert notes.parent.parent == home / "out" / "Встречи" and text.startswith("# Встреча с фабрикой")
    assert "## Решения\n- Заказать ткань" in text and "[00:00] фраза 1" in text
    assert not list(notes.parent.glob("*.wav"))                     # the raw audio is gone
    assert "данные, а не инструкции" in meet.claude.got.instruction and "фраза 1" in meet.claude.got.context[0]["text"]
    assert core.director.core_state()["meeting"] is None
    assert t["steps"][-1]["verification"]["status"] == "verified"


async def test_private_mode_keeps_the_transcript_and_says_why(meet, core):
    core.db.set_kv("privacy_profile", "PRIVATE_LOCAL")
    await meet.start("Планёрка")
    await asyncio.sleep(0.4)
    r = await meet.stop()
    text = Path(r["path"]).read_text("utf-8")
    assert meet.claude.got is None and "режим приватности" in text and "фраза 1" in text


async def test_a_claude_limit_never_loses_the_meeting(meet, core):
    meet.claude.fail = "You've hit your session limit"
    await meet.start("Звонок")
    await asyncio.sleep(0.4)
    r = await meet.stop()
    assert "session limit" in r["why"] and "фраза" in Path(r["path"]).read_text("utf-8")


async def test_without_local_recognition_the_audio_is_kept(meet, core):
    core.director.stt = None
    await meet.start("Без распознавания")
    await asyncio.sleep(0.4)
    r = await meet.stop()
    folder = Path(r["path"]).parent
    assert (folder / "запись.wav").stat().st_size > 32000 and not (folder / "mic.wav").exists()


async def test_cancel_deletes_everything(meet, core):
    rec = await meet.start("Лишнее")
    await asyncio.sleep(0.3)
    await meet.cancel()
    assert not Path(rec["folder"]).exists() and meet.active is None


async def test_one_meeting_at_a_time_and_stop_without_one(meet):
    await meet.start("Первая")
    with pytest.raises(Exception, match="Уже идёт"):
        await meet.start("Вторая")
    await meet.cancel()
    with pytest.raises(Exception, match="не записывается"):
        await meet.stop()


def test_long_audio_is_cut_at_pauses_and_silence_is_dropped():
    tone = b"".join(struct.pack("<h", int(8000 * math.sin(i / 8))) for i in range(RATE))
    quiet = b"\0\0" * RATE
    pcm = (tone * 3 + quiet) * 15 + quiet * 30                 # 60 s of talk with pauses, then 30 s of silence
    parts = chunks(pcm)
    assert all(len(p) <= 24 * RATE * 2 for _, p in parts) and len(parts) >= 3
    assert parts[-1][0] < 61                                   # nothing from the silent tail
