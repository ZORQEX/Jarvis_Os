"""The computer and the desk (tools/machine.py, desk.py, daily.py — ported from morph-rq/jarvis-os): what the system
programs print is read right, dangerous actions need the person's yes, nothing but caches is ever deleted, the
briefing collects what matters today, copied passwords are never read. VERIFIED LOCAL (no desktop changes: parsers,
temporary folders, own child processes)."""
import asyncio
import os
import subprocess
import sys
import time

import pytest

from jarvis import router
from jarvis.tools import daily, desk, machine
from jarvis.tools.base import Risk, ToolContext, ToolError

WPCTL = """PipeWire 'pipewire-0' [1.6.9, tim@archlinux, cookie:3074490238]
 └─ Clients:
        32. xdg-desktop-portal                  [1.6.9, tim@archlinux, pid:1088]

Audio
 ├─ Devices:
 │      44. Built-in Audio                      [alsa]
 │
 ├─ Sinks:
 │      53. Built-in Audio Analog Stereo        [vol: 1.00]
 │  *   71. Sony WH-1000XM5                     [vol: 0.40]
 │
 ├─ Sources:
 │  *   54. Built-in Audio Analog Stereo        [vol: 0.27]
 │
 └─ Streams:

Video
 ├─ Sources:
 │  *   63. ASUS HD webcam (V4L2)
"""


def test_sound_devices_from_wpctl():
    nodes = machine.parse_wpctl(WPCTL)
    assert [n["name"] for n in nodes["sinks"]] == ["Built-in Audio Analog Stereo", "Sony WH-1000XM5"]
    assert next(n for n in nodes["sinks"] if n["default"])["id"] == 71
    assert [n["id"] for n in nodes["sources"]] == [54]               # the webcam (Video) is not a microphone


def test_brightness_devices():
    devs = machine.parse_brightness("intel_backlight,backlight,19200,100%,19200\nasus::kbd_backlight,leds,0,0%,3\n")
    assert devs["asus::kbd_backlight"]["max"] == 3 and devs["intel_backlight"]["class"] == "backlight"


@pytest.mark.parametrize("said,tool,args", [
    ("Какая погода?", "weather", {}),
    ("погода в Москве", "weather", {"city": "москве"}),
    ("брифинг", "daily_briefing", {}),
    ("что тормозит", "processes", {"sort": "cpu"}),
    ("какие программы жрут память", "processes", {"sort": "memory"}),
    ("закрой телеграм", "process_end", {"name": "telegram"}),
    ("сколько места на диске", "disk_usage", {}),
    ("почисти диск", "disk_cleanup", {}),
    ("очисти корзину", "disk_cleanup", {"items": ["trash"]}),
    ("проверь компьютер", "diagnostics", {}),
    ("включи подсветку клавиатуры", "brightness", {"target": "keyboard", "on": True}),
    ("выключи подсветку клавиатуры", "brightness", {"target": "keyboard", "on": False}),
    ("яркость 40", "brightness", {"target": "screen", "level": 40}),
    ("включи тёмную тему", "appearance", {"action": "dark"}),
    ("смени обои", "appearance", {"action": "wallpaper"}),
    ("включи режим не беспокоить", "appearance", {"action": "dnd_on"}),
    ("выключи не беспокоить", "appearance", {"action": "dnd_off"}),
    ("переключи звук на наушники", "audio_devices", {"action": "output", "name": "наушники"}),
    ("есть ли интернет", "network_status", {}),
    ("режим энергосбережения", "power_profile", {"profile": "power-saver"}),
    ("переведи компьютер в сон", "power_action", {"action": "suspend"}),
    ("выключи компьютер", "power_action", {"action": "poweroff"}),
    ("следующий трек", "media", {"action": "next"}),
    ("поставь музыку на паузу", "media", {"action": "pause"}),
    ("что в буфере обмена", "clipboard", {"action": "read"}),
    ("прочитай текст на экране", "screen_text", {}),
])
def test_everyday_commands_need_no_ai(said, tool, args):
    r = router.route(said)
    assert r and r.kind == "tool" and r.tool == tool and r.args == args, r


@pytest.mark.parametrize("said,tool", [("сделай тише", "volume_set"), ("выключи звук", "volume_set"),
                                       ("состояние системы", "system_info"), ("стоп", None), ("пауза", None)])
def test_older_commands_unchanged(said, tool):
    r = router.route(said)
    assert (r.tool if r and r.kind == "tool" else None) == tool


def test_risky_actions_ask_first(core):
    reg = core.registry
    assert reg.get("power_action").spec.risk == Risk.HIGH
    assert reg.get("process_end").spec.risk == Risk.MODERATE and reg.get("disk_cleanup").spec.risk == Risk.MODERATE
    for tid in ("processes", "disk_usage", "diagnostics", "weather", "daily_briefing", "clipboard", "media",
                "screen_text", "network_status", "brightness", "appearance"):
        assert reg.get(tid).spec.risk == Risk.LOW, tid


def ctx():
    return ToolContext(config=None, task_id="t")


async def test_ending_a_program_never_touches_session_or_jarvis(tmp_path):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL)
    try:
        ok, refused = machine.end_targets(None, os.getpid())
        assert not ok                                           # this test runner itself (JARVIS's own process)
        ok, _ = machine.end_targets(None, sleeper.pid)
        assert [p["pid"] for p in ok] == [sleeper.pid]
        tool = machine.ProcessEnd()
        res = await tool.run({"pid": sleeper.pid}, ctx())
        assert (await tool.verify({"pid": sleeper.pid}, res, ctx())).status == "verified"
        assert sleeper.wait(3) is not None
    finally:
        sleeper.kill()
    ok, refused = machine.end_targets("systemd", None)
    assert not ok


async def test_cleanup_empties_only_the_chosen_caches(tmp_path, monkeypatch):
    cache, docs, trash = tmp_path / "cache", tmp_path / "Documents", tmp_path / "Trash"
    (cache / "pkg").mkdir(parents=True)
    (cache / "pkg" / "big.whl").write_bytes(b"x" * 3_000_000)
    docs.mkdir()
    (docs / "отчёт.docx").write_text("важно")
    (trash / "files").mkdir(parents=True)
    (trash / "files" / "old.txt").write_text("x")
    link = cache / "to-docs"
    link.symlink_to(docs)                                       # a link into documents is removed, never followed
    monkeypatch.setattr(machine, "CLEANABLE", {"pip": ("Кэш pip", cache), "trash": ("Корзина", trash)})
    tool = machine.DiskCleanup()
    args = tool.validate({}, ctx())
    assert args["items"] == ["pip"]                             # the Trash only when asked for
    res = await tool.run(args, ctx())
    assert res.data["freed"] >= 3_000_000 and not any(cache.iterdir())
    assert (docs / "отчёт.docx").read_text() == "важно" and (trash / "files" / "old.txt").exists()
    assert (await tool.verify(args, res, ctx())).status == "verified"
    scan = await asyncio.to_thread(machine.cleanup_scan)
    assert all(i.get("manual") or i["item"] in ("pip", "trash") for i in scan)


async def test_briefing_collects_today(core, monkeypatch):
    async def fake_weather(city, days=2):
        assert city == "Казани"
        return {"message": "Казань: сейчас 5°, дождь.", "now": {"temp": 5}}
    monkeypatch.setattr(daily, "weather_report", fake_weather)
    from jarvis.memory import add
    add(core.db, "Я живу в Казани")
    now = time.time()
    core.db.execute("INSERT INTO reminders(id,text,due_at,created_at,status) VALUES('r1','позвонить фабрике',?,?,"
                    "'scheduled')", (now + 600, now))
    tid = core.tasks.create("отправь отчёт", None, route="ai", level=2)
    core.db.execute("UPDATE tasks SET state='failed' WHERE id=?", (tid,))
    tool = core.registry.get("daily_briefing")
    res = await tool.run({}, ctx())
    text = res.summary
    assert "Казань: сейчас 5°" in text and "позвонить фабрике" in text and "отправь отчёт" in text
    assert (await tool.verify({}, res, ctx())).status == "verified"


async def test_weather_needs_a_city(core):
    with pytest.raises(ToolError, match="Для какого города"):
        await core.registry.get("weather").run({}, ctx())


def test_city_forms():
    assert "Москва" in daily.base_forms("в Москве") and "Казань" in daily.base_forms("Казани")
    assert daily.base_forms("Lisbon") == ["Lisbon"]


async def test_copied_passwords_are_never_read(monkeypatch):
    async def types():
        return ["text/plain", "x-kde-passwordManagerHint"]
    monkeypatch.setattr(desk, "clip_types", types)
    with pytest.raises(ToolError, match="менеджера паролей"):
        await desk.Clipboard().run({"action": "read"}, ctx())


async def test_locked_screen_is_said_at_once(monkeypatch):
    monkeypatch.setattr(desk, "screen_locked", lambda: True)
    monkeypatch.setattr(desk.shutil, "which", lambda name: "/usr/bin/" + name)
    t0 = time.perf_counter()
    with pytest.raises(ToolError, match="заблокирован"):
        await desk.ScreenText().run({}, ctx())
    assert time.perf_counter() - t0 < 1


async def test_a_silent_weather_service_is_not_an_unknown_city(monkeypatch):
    """Seen 2026-10-26: a 10 s timeout became «Не нашёл город «алматы»»."""
    from jarvis.tools import daily
    from jarvis.tools.base import ToolError
    calls = []

    async def flaky(name, session):
        calls.append(name)
        if len(calls) == 1:
            raise asyncio.TimeoutError()
        return [{"name": "Алматы", "country": "Казахстан", "lat": 43.25, "lon": 76.9, "tz": "Asia/Almaty", "population": 1977011}]
    monkeypatch.setattr(daily, "geocode", flaky)
    assert (await daily.find_place("алматы", None))["name"] == "Алматы" and len(calls) == 2      # asked once more

    async def down(name, session):
        raise asyncio.TimeoutError()
    monkeypatch.setattr(daily, "geocode", down)
    with pytest.raises(ToolError, match="не отвечает"):
        await daily.find_place("алматы", None)
