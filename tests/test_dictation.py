"""Dictation: speech typed into the person's window instead of being answered. «Джарвис, …» stays a command, «стоп
диктовка» ends it, «новая строка» is Enter except in a terminal, the window the person was in gets the text back.
VERIFIED MOCK: hyprctl and wtype are scripted (no keys are pressed on the real desktop)."""
import json

import pytest

from jarvis import router
from jarvis.dictation import Dictation

from conftest import wait_for
from test_overlay import WAV, FakeSTT, request


class Desk:
    """hyprctl + wtype as the dictation sees them: windows, focus, and what was typed."""

    def __init__(self, cls="org.gnome.TextEditor", title="Письмо фабрике"):
        self.active = {"address": "0xa1", "class": cls, "title": title}
        self.clients = [self.active, {"address": "0xb2", "class": "chromium", "title": "YouTube"}]
        self.typed, self.focused = [], []

    async def run(self, argv, timeout=10.0, **kw):
        from jarvis.tools.base import CmdResult
        if argv[:2] == ["hyprctl", "activewindow"]:
            return CmdResult(argv, 0, json.dumps(self.active), "", 0)
        if argv[:2] == ["hyprctl", "clients"]:
            return CmdResult(argv, 0, json.dumps(self.clients), "", 0)
        if argv[:3] == ["hyprctl", "dispatch", "focuswindow"]:
            addr = argv[3].split(":", 1)[1]
            self.active = next(c for c in self.clients if c["address"] == addr)
            self.focused.append(addr)
            return CmdResult(argv, 0, "ok", "", 0)
        if argv[0] == "wtype":
            self.typed.append(("key", argv[2]) if argv[1] == "-k" else ("text", argv[2]))
            return CmdResult(argv, 0, "", "", 0)
        raise AssertionError(argv)


@pytest.fixture
def desk(monkeypatch):
    d = Desk()
    import jarvis.dictation as dm
    monkeypatch.setattr(dm, "run_cmd", d.run)
    monkeypatch.setattr(dm, "screen_locked", lambda: False)
    monkeypatch.setattr(Dictation, "available", lambda self: True)
    return d


@pytest.mark.parametrize("said,op", [("Джарвис, диктовка", "dictation_on"), ("начни диктовку", "dictation_on"),
                                      ("я буду диктовать", "dictation_on"), ("стоп диктовка", "dictation_off"),
                                      ("закончи диктовку", "dictation_off")])
def test_dictation_by_voice(said, op):
    r = router.route(said)
    assert r and r.kind == "control" and r.args["op"] == op


async def test_phrases_go_into_the_window_the_person_was_in(desk):
    d = Dictation()
    msg = await d.start()
    assert d.on and "Письмо фабрике" in msg
    desk.active = desk.clients[1]                  # the person looked at another window meanwhile
    await d.type("Добрый день.")
    await d.type("Пришлите, пожалуйста, счёт. Новая строка")
    await d.type("Спасибо!")
    assert desk.focused == ["0xa1"]                # brought back once, then it stayed there
    assert desk.typed == [("text", "Добрый день."), ("text", " Пришлите, пожалуйста, счёт."), ("key", "Return"),
                          ("text", "Спасибо!")]
    assert "напечатано" in await d.stop() and not d.on


async def test_no_enter_in_a_terminal(desk):
    desk.active = {"address": "0xc3", "class": "kitty", "title": "~"}
    desk.clients.append(desk.active)
    d = Dictation()
    await d.start()
    await d.type("git status новая строка")
    assert desk.typed == [("text", "git status")]           # Enter would have run it


async def test_through_the_companion(ws_client, core, desk):
    tc = ws_client
    core.director.stt = FakeSTT()
    core.director.stt.texts = ["Джарвис, диктовка", "Добрый день, Алина.", "Джарвис, который час", "Стоп диктовка."]
    m = await request(tc, "voice.transcribe", 1, audio_b64=WAV, mime="audio/wav", wake=True, client="overlay")
    await wait_for(lambda: core.director.dictation.on)
    before = len(core.director.messages(core.director.conversation_id()))
    # no «Джарвис» needed while dictating, and nothing is stored or answered — it is typed
    m = await request(tc, "voice.transcribe", 2, audio_b64=WAV, mime="audio/wav", wake=True, client="overlay")
    assert m["data"]["dictation"] is True and desk.typed == [("text", "Добрый день, Алина.")]
    assert len(core.director.messages(core.director.conversation_id())) == before
    m = await request(tc, "voice.transcribe", 3, audio_b64=WAV, mime="audio/wav", wake=True, client="overlay")
    assert m["data"].get("task_id") and len(desk.typed) == 1           # a command, not typed
    m = await request(tc, "voice.transcribe", 4, audio_b64=WAV, mime="audio/wav", wake=True, client="overlay")
    assert m["data"]["dictation"] == "stopped" and not core.director.dictation.on


async def test_hotkey_toggles_through_the_api(ws_client, core, desk):
    tc = ws_client
    hdr = {**tc.hdr, "Authorization": f"Bearer {tc.srv.token}"}
    r = await tc.post("/api/control", json={"op": "dictation_toggle"}, headers=hdr)
    assert (await r.json())["ok"] and core.director.dictation.on
    r = await tc.post("/api/control", json={"op": "dictation_toggle"}, headers=hdr)
    assert not core.director.dictation.on and "закончена" in (await r.json())["message"]


async def test_never_types_into_the_lock_screen(desk, monkeypatch):
    import jarvis.dictation as dm
    d = Dictation()
    await d.start()
    monkeypatch.setattr(dm, "screen_locked", lambda: True)
    with pytest.raises(RuntimeError, match="заблокирован"):
        await d.type("пароль от почты")
    assert desk.typed == []
