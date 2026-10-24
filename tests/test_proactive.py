"""Proactive notices and watches (VERIFIED LOCAL with fake readings; the real loop reads /proc and /sys)."""
from collections import namedtuple
from datetime import datetime

import pytest

from conftest import wait_done
from jarvis.proactive import Proactive, in_quiet_hours, parse_watch
from jarvis.router import route

Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def pa(core):
    p = Proactive(core.db, core.bus, core.audit, core.director)
    p.notified = []

    async def notify(title, body, urgent):
        p.notified.append((title, urgent))
    p.notify = notify
    p.battery = lambda: {"percent": 60, "status": "discharging"}
    p.failed_units = list
    p.disk_usage = lambda m: Usage(100, 50, 50)
    p.memory_pct = lambda: 40
    p.online = lambda: True
    p.cpu_times = lambda: None
    p.dnd = lambda: False
    core.db.set_kv("proactive_quiet", "-")
    return p


def notices(core):
    return [m["text"] for m in core.db.all("SELECT text FROM messages WHERE meta LIKE '%notice%' ORDER BY created_at")]


async def test_a_low_battery_is_said_once_not_every_minute(pa, core):
    pa.battery = lambda: {"percent": 15, "status": "discharging"}
    await pa.tick()
    await pa.tick()
    assert notices(core) == ["🔔 Батарея 15 %. Пора найти зарядку"]


async def test_charging_or_a_full_battery_says_nothing(pa, core):
    pa.battery = lambda: {"percent": 5, "status": "charging"}
    await pa.tick()
    assert not notices(core)


async def test_critical_battery_is_urgent_even_in_quiet_hours(pa, core):
    core.db.set_kv("proactive_quiet", "00:00-23:59")
    pa.battery = lambda: {"percent": 6, "status": "discharging"}
    pa.disk_usage = lambda m: Usage(100, 97, 3)
    await pa.tick()
    assert notices(core) == ["🔔 Батарея 6 %. Подключите зарядку или сохраните работу"]   # the disk waits till morning


async def test_full_disk_new_failed_service_and_lost_internet(pa, core):
    pa.disk_usage = lambda m: Usage(100 * 2 ** 30, 95 * 2 ** 30, 5 * 2 ** 30)
    await pa.tick()                                   # first look: the services that already failed are known, not news
    pa.failed_units = lambda: [{"unit": "backup.service"}]
    pa.online = lambda: False
    await pa.tick()
    await pa.tick()                                   # offline twice in a row before saying so
    pa.online = lambda: True
    await pa.tick()
    said = notices(core)
    assert said[0].startswith("🔔 Диск почти заполнен. Свободно 5,0 ГБ")
    assert "🔔 Упала служба. backup.service" in said
    assert any("Интернет пропал" in s for s in said) and said[-1] == "🔔 Интернет снова есть"


async def test_no_more_than_four_ordinary_notices_an_hour(pa):
    assert sum(pa.say(f"k{i}", f"n{i}") for i in range(6)) == 4


async def test_do_not_disturb_holds_ordinary_notices(pa):
    pa.dnd = lambda: True
    assert not pa.say("disk", "x") and pa.say("battery_critical", "y")


def test_quiet_hours_across_midnight():
    assert in_quiet_hours("23:00-08:00", datetime(2026, 1, 1, 2, 0))
    assert not in_quiet_hours("23:00-08:00", datetime(2026, 1, 1, 12, 0))
    assert not in_quiet_hours("-")


@pytest.mark.parametrize("said, args", [
    ("скажи когда батарея будет ниже 30", {"kind": "battery", "condition": "below", "threshold": 30.0}),
    ("Джарвис, сообщи, когда процессор выше 90 процентов", {"kind": "cpu", "condition": "above", "threshold": 90.0}),
    ("предупреди, когда сайт example.com упадёт", {"kind": "website", "target": "example.com", "condition": "down"}),
    ("дай знать, когда биткоин будет ниже 50000", {"kind": "price", "target": "bitcoin", "condition": "below"}),
])
def test_watch_phrases_without_the_ai(said, args):
    r = route(said)
    assert r.tool == "watch" and args.items() <= r.args.items()


def test_other_when_phrases_are_not_watches():
    assert parse_watch("скажи когда придет мама") is None
    assert parse_watch("напомни мне купить хлеб") is None


async def test_a_watch_fires_once_and_is_spoken(pa, core):
    tid = await core.director.submit("скажи когда батарея будет ниже 30", source="text")
    t = await wait_done(core, tid)
    assert t["state"] == "completed" and t["steps"][-1]["verification"]["status"] == "verified"
    spoken = []
    core.director.speech = type("S", (), {"say": lambda self, tid, text, turn=None: spoken.append(text)})()
    pa.battery = lambda: {"percent": 35, "status": "charging"}
    await pa.check_watches()
    assert not notices(core)
    pa.battery = lambda: {"percent": 28, "status": "discharging"}
    await pa.check_watches()
    await pa.check_watches()
    assert notices(core) == ["🔔 Наблюдение: батарея ниже 30 %. Сейчас 28 %"]
    assert spoken == ["Наблюдение: батарея ниже 30 %. Сейчас 28 %"]
    assert pa.settings()["watches"][0]["active"] == 0


async def test_watch_list_and_remove(core):
    for said in ("скажи когда память выше 95", "скажи когда диск выше 90"):
        await wait_done(core, await core.director.submit(said, source="text"))
    t = await wait_done(core, await core.director.submit("что ты наблюдаешь", source="text"))
    assert "память выше 95 %" in t["steps"][-1]["result"]["summary"]
    gw = core.director.gw.registry.get("watch")
    from jarvis.tools.base import ToolContext
    res = await gw.run({"action": "remove"}, ToolContext(config=core.config, task_id="t"))
    assert "диск выше 90" in res.summary


async def test_a_website_needs_an_address_and_a_threshold_needs_a_number(core):
    from jarvis.tools.base import ToolContext, ToolInputError
    w = core.director.gw.registry.get("watch")
    with pytest.raises(ToolInputError):
        await w.run({"action": "add", "kind": "website", "target": "не сайт"}, ToolContext(config=core.config, task_id="t"))
    with pytest.raises(ToolInputError):
        await w.run({"action": "add", "kind": "cpu"}, ToolContext(config=core.config, task_id="t"))
