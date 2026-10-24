"""The calendar: Russian dates, own events with a reminder before each, subscribed iCal calendars (recurrence,
exceptions, time zones, all-day), the day's events in the briefing, a broken subscription never shown or fatal.
VERIFIED LOCAL (a real iCal file served over HTTP on this machine)."""
import socket
import time
from datetime import datetime, timedelta

import pytest
from aiohttp import web

from jarvis import router
from jarvis.agenda import parse_when, when_ru
from jarvis.tools.base import ToolContext

from conftest import wait_done

REF = datetime(2026, 10, 25, 10, 0)          # a Sunday


@pytest.mark.parametrize("said,start,all_day,minutes", [
    ("в четверг в 15:00", datetime(2026, 10, 29, 15), False, 60),
    ("завтра в 3 часа дня на полчаса", datetime(2026, 10, 26, 15), False, 30),
    ("12 ноября", datetime(2026, 11, 12), True, 60),
    ("в 9 утра", datetime(2026, 10, 26, 9), False, 60),              # 9 am already passed today → tomorrow
    ("послезавтра в 11", datetime(2026, 10, 27, 11), False, 60),
    ("25.12 в 18.30", datetime(2026, 12, 25, 18, 30), False, 60),
    ("в 7 вечера на 2 часа", datetime(2026, 10, 25, 19), False, 120),
    ("в воскресенье в 9", datetime(2026, 11, 1, 9), False, 60),       # said on Sunday after 9: next Sunday
    ("1 марта", datetime(2027, 3, 1), True, 60),                      # a past date this year → next year
])
def test_russian_dates(said, start, all_day, minutes):
    w = parse_when(said, REF)
    assert (w["start"], w["all_day"], w["minutes"]) == (start, all_day, minutes)


def test_said_back_in_russian():
    assert when_ru(datetime(2026, 10, 26, 15), ref=REF) == "завтра в 15:00"
    assert when_ru(datetime(2026, 10, 29, 15), ref=REF) == "в четверг в 15:00"
    assert when_ru(datetime(2026, 11, 12), True, ref=REF) == "12 ноября"


@pytest.mark.parametrize("said,tool", [("что у меня в календаре", "calendar_show"), ("какие встречи завтра", "calendar_show"),
                                       ("Джарвис, добавь встречу с фабрикой в четверг в 15:00", "calendar_add"),
                                       ("добавь заметку купить хлеб", "create_note")])
def test_calendar_phrases(said, tool):
    r = router.route(said)
    assert r and r.tool == tool


def ctx(core):
    return ToolContext(config=core.config, task_id="t")


async def test_an_event_gets_a_reminder_and_can_be_removed(core):
    start = (datetime.now() + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
    tid = await core.director.submit(f"запиши в календарь созвон с Романом завтра в 15:00", source="text")
    t = await wait_done(core, tid)
    assert t["state"] == "completed" and t["steps"][-1]["verification"]["status"] == "verified"
    ev = core.db.one("SELECT * FROM calendar_events")
    assert ev["title"] == "Созвон с Романом" and ev["start_at"] == start.timestamp()
    rem = core.db.one("SELECT * FROM reminders WHERE id=?", (ev["reminder_id"],))
    assert rem["status"] == "scheduled" and rem["due_at"] == start.timestamp() - 15 * 60 and "Созвон" in rem["text"]
    show = core.registry.get("calendar_show")
    res = await show.run({"when": "tomorrow"}, ctx(core))
    assert "15:00 — Созвон с Романом" in res.summary
    cancel = core.registry.get("calendar_cancel")
    assert cancel.spec.risk.value == "moderate"                      # removing asks first
    out = await cancel.run({"query": "созвон"}, ctx(core))
    assert (await cancel.verify({}, out, ctx(core))).status == "verified"
    assert core.db.one("SELECT status FROM reminders WHERE id=?", (ev["reminder_id"],))["status"] == "cancelled"


ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:weekly-1\r
SUMMARY:Планёрка с фабрикой\r
DTSTART;TZID=Europe/Rome:{d0}T090000\r
DTEND;TZID=Europe/Rome:{d0}T093000\r
RRULE:FREQ=DAILY;COUNT=5\r
EXDATE;TZID=Europe/Rome:{d1}T090000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:allday\r
SUMMARY:Показ коллекции\r
DTSTART;VALUE=DATE:{d2}\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:gone\r
SUMMARY:Отменено\r
STATUS:CANCELLED\r
DTSTART:{d0}T120000Z\r
END:VEVENT\r
END:VCALENDAR\r
"""


@pytest.fixture
async def feed():
    today = datetime.now().date()
    body = ICS.format(d0=today.strftime("%Y%m%d"), d1=(today + timedelta(days=1)).strftime("%Y%m%d"),
                      d2=(today + timedelta(days=2)).strftime("%Y%m%d"))

    async def handler(req):
        return web.Response(text=body, content_type="text/calendar")
    app = web.Application()
    app.router.add_get("/secret/basic.ics", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    await web.TCPSite(runner, "127.0.0.1", port).start()
    yield f"http://127.0.0.1:{port}/secret/basic.ics"
    await runner.cleanup()


async def test_a_subscribed_calendar(core, feed):
    sub = core.registry.get("calendar_subscribe")
    out = await sub.run({"url": feed, "name": "Фабрика"}, ctx(core))
    assert out.data["events"] == 3 and "secret" not in out.summary          # the link is never said
    week = await core.registry.get("calendar_show").run({"when": "week"}, ctx(core))
    titles = [e["title"] for e in week.data["events"]]
    assert titles.count("Планёрка с фабрикой") == 4                          # 5 days minus the excluded one
    assert "Показ коллекции" in titles and "Отменено" not in titles
    from zoneinfo import ZoneInfo
    first = min(datetime.fromisoformat(e["start"]) for e in week.data["events"] if e["title"] == "Планёрка с фабрикой")
    rome_nine = datetime.combine(datetime.now().date(), datetime.min.time()).replace(hour=9, tzinfo=ZoneInfo("Europe/Rome"))
    assert first == rome_nine.astimezone().replace(tzinfo=None)              # 9:00 in Rome, said in local time
    briefing = await core.registry.get("daily_briefing").run({}, ctx(core))
    today_left = [e for e in week.data["events"] if datetime.fromisoformat(e["start"]).date() == datetime.now().date()
                  and datetime.fromisoformat(e["end"]) > datetime.now()]
    assert ("Планёрка с фабрикой" in briefing.summary) == bool(today_left)


async def test_a_broken_subscription_is_counted_not_fatal(core):
    core.agenda = None
    from jarvis.agenda import Agenda
    a = Agenda(core.db)
    a.add_feed("http://127.0.0.1:9/secret.ics", "Сломанный")
    a.add(title="Своя встреча", start=datetime.now() + timedelta(hours=2))
    events = await a.events(datetime.now(), datetime.now() + timedelta(days=1))
    assert [e["title"] for e in events] == ["Своя встреча"] and a.failed == 1
    with pytest.raises(Exception, match="iCal"):
        a.add_feed("ftp://x", "")
