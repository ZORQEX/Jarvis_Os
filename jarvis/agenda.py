"""The calendar: JARVIS's own events (with a reminder before each) and subscribed calendars (iCal links — Google's
«секретный адрес в формате iCal», Outlook, Apple), read-only. «Что у меня завтра?», «Добавь встречу с фабрикой в
четверг в 15:00», «Какие встречи на этой неделе?». The day's events also go into the daily briefing.
Idea and the iCal parser from morph-rq/jarvis-os; storage, tools and Russian dates are JARVIS's own.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import date, datetime, timedelta

import aiohttp

from . import ics
from .db import new_id, now
from .tools.base import Risk, Tool, ToolError, ToolInputError, ToolResult, ToolSpec, Verification

DAYS_ACC = {"понедельник": 0, "вторник": 1, "среду": 2, "среда": 2, "четверг": 3, "пятницу": 4, "пятница": 4,
            "субботу": 5, "суббота": 5, "воскресенье": 6}
MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "август": 8,
          "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
WEEKDAY_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTH_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
            "декабря"]
WORD_HOURS = {"час": 1, "один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8,
              "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12}


def parse_when(text: str, ref: datetime | None = None) -> dict:
    """Russian date / time / duration in a phrase → {"start": datetime|None, "all_day", "minutes", "rest"}.
    «в четверг в 15:00», «завтра в 3 часа дня», «25 октября в 10», «послезавтра», «в 9 утра на полчаса»."""
    ref = ref or datetime.now()
    # punctuation off (one space each, so positions stay), except inside «25.12» / «18:30»
    orig = " " + re.sub(r"(?<!\d)[.,!?;:«»\"]|[.,!?;:«»\"](?!\d)", " ", re.sub(r"\s+", " ", text)) + " "
    t = orig.lower().replace("ё", "е")              # matched in lower case, cut from the original (same length)
    if len(t) != len(orig):
        orig = t
    day: date | None = None
    spans: list[tuple[int, int]] = []

    def cut(m):
        spans.append(m.span())

    m = re.search(r" (сегодня|завтра|послезавтра) ", t)
    if m:
        day = ref.date() + timedelta(days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[m.group(1)])
        cut(m)
    m = re.search(r" (?:в|во) (понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье) ", t) if day is None else None
    weekday_named = bool(m)
    if m:
        ahead = (DAYS_ACC[m.group(1)] - ref.weekday()) % 7
        day = ref.date() + timedelta(days=ahead)
        cut(m)
    m = re.search(r" (\d{1,2})(?:-?го)? (январ\w*|феврал\w*|март\w*|апрел\w*|ма[яй]|июн\w*|июл\w*|август\w*|сентябр\w*|"
                  r"октябр\w*|ноябр\w*|декабр\w*)(?: (\d{4}))? ", t) if day is None else None
    if m:
        month = next(v for k, v in MONTHS.items() if m.group(2).startswith(k))
        year = int(m.group(3)) if m.group(3) else ref.year
        try:
            day = date(year, month, int(m.group(1)))
            if not m.group(3) and day < ref.date():
                day = date(year + 1, month, int(m.group(1)))
        except ValueError:
            day = None
        cut(m)
    m = re.search(r" (\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))? ", t) if day is None else None
    if m:
        try:
            year = int(m.group(3)) + (2000 if m.group(3) and len(m.group(3)) == 2 else 0) if m.group(3) else ref.year
            day = date(year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            day = None
        cut(m)
    hour = minute = None
    m = re.search(r" в (\d{1,2})[:.](\d{2})(?: (утра|дня|вечера|ночи))? ", t) or \
        re.search(r" в (\d{1,2})(?: час\w*)?(?: (утра|дня|вечера|ночи))? ", t)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.re.groups == 3 and m.group(2) and m.group(2).isdigit() else 0
        part = m.group(m.re.groups)
        if part in ("дня", "вечера") and hour < 12:
            hour += 12
        elif part == "ночи" and hour == 12:
            hour = 0
        cut(m)
    else:
        m = re.search(r" в (час|один|два|три|четыре|пять|шесть|семь|восемь|девять|десять|одиннадцать|двенадцать)"
                      r"(?: час\w*)?(?: (утра|дня|вечера|ночи))? ", t) or re.search(r" (в полдень|в полночь) ", t)
        if m:
            if m.group(1) in ("в полдень", "в полночь"):
                hour, minute = (12 if m.group(1) == "в полдень" else 0), 0
            else:
                hour, minute = WORD_HOURS[m.group(1)], 0
                if m.group(2) in ("дня", "вечера") and hour < 12:
                    hour += 12
            cut(m)
    minutes = 60
    m = re.search(r" на (полчаса|час|полтора часа|(\d{1,3}) (?:час\w*|минут\w*)) ", t)
    if m:
        if m.group(1) == "полчаса":
            minutes = 30
        elif m.group(1) == "час":
            minutes = 60
        elif m.group(1) == "полтора часа":
            minutes = 90
        else:
            n = int(m.group(2))
            minutes = n * 60 if "час" in m.group(1) else n
        cut(m)
    if hour is not None and not 0 <= hour <= 23 or minute is not None and not 0 <= minute <= 59:
        hour = minute = None
    start = None
    if day is None and hour is not None:
        day = ref.date()
        if datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute) <= ref:
            day += timedelta(days=1)
    if day is not None:
        start = datetime.combine(day, datetime.min.time())
        if hour is not None:
            start = start.replace(hour=hour, minute=minute)
            if weekday_named and start <= ref:
                start += timedelta(days=7)         # «в четверг в 10» said on Thursday at 11: next Thursday
    rest = orig
    for a, b in sorted(spans, reverse=True):
        rest = rest[:a] + " " + rest[b:]
    return {"start": start, "all_day": start is not None and hour is None, "minutes": minutes,
            "rest": re.sub(r"\s+", " ", rest).strip()}


def when_ru(start: datetime, all_day: bool = False, ref: datetime | None = None) -> str:
    ref = ref or datetime.now()
    delta = (start.date() - ref.date()).days
    day = {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get(delta) or \
        (f"в {WEEKDAY_RU[start.weekday()].replace('среда', 'среду').replace('пятница', 'пятницу').replace('суббота', 'субботу')}"
         if 0 < delta < 7 else f"{start.day} {MONTH_RU[start.month - 1]}")
    return day if all_day else f"{day} в {start:%H:%M}"


class Agenda:
    def __init__(self, db, scheduler=None):
        self.db, self.scheduler = db, scheduler
        self._feeds: dict[str, tuple[float, list[dict]]] = {}
        self.failed = 0

    # feeds -------------------------------------------------------------------------------------------------
    def feeds(self) -> list[dict]:
        return list(self.db.get_kv("calendar_feeds") or [])

    def add_feed(self, url: str, name: str = "") -> dict:
        if not re.match(r"^(https?|webcal)://", url or "", re.I):
            raise ToolInputError("Нужна ссылка на календарь (https://… или webcal://…, формат iCal)")
        feeds = [f for f in self.feeds() if f["url"] != url]
        feed = {"id": new_id("cal_"), "name": (name or "Календарь")[:60], "url": url}
        self.db.set_kv("calendar_feeds", feeds + [feed])
        return feed

    def remove_feed(self, fid: str) -> bool:
        feeds = self.feeds()
        left = [f for f in feeds if f["id"] != fid]
        self.db.set_kv("calendar_feeds", left)
        return len(left) != len(feeds)

    async def _fetch(self, url: str) -> list[dict]:
        cached = self._feeds.get(url)
        if cached and time.time() - cached[0] < ics.FEED_TTL:
            return cached[1]
        href = re.sub(r"^webcal://", "https://", url, flags=re.I)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
                                         headers={"User-Agent": "JARVIS-WorkOS/1", "Accept": "text/calendar"}) as s:
            async with s.get(href) as r:
                r.raise_for_status()
                data = await r.content.read(ics.MAX_FEED + 1)
        if len(data) > ics.MAX_FEED:
            raise ToolError("Календарь по ссылке слишком большой")
        events = ics.parse_ics(data)
        self._feeds[url] = (time.time(), events)
        return events

    # events ------------------------------------------------------------------------------------------------
    def local_events(self, start: datetime, end: datetime) -> list[dict]:
        rows = self.db.all("SELECT * FROM calendar_events WHERE state='active' AND start_at < ? AND end_at > ? ORDER BY start_at",
                           (end.timestamp(), start.timestamp()))
        return [{"id": r["id"], "title": r["title"], "start": datetime.fromtimestamp(r["start_at"]),
                 "end": datetime.fromtimestamp(r["end_at"]), "all_day": bool(r["all_day"]),
                 "location": r["location"] or "", "source": "local"} for r in rows]

    async def events(self, start: datetime, end: datetime) -> list[dict]:
        out = self.local_events(start, end)
        self.failed = 0
        for feed in self.feeds():
            try:
                vevents = await asyncio.wait_for(self._fetch(feed["url"]), 16)
                for ev in ics.expand(vevents, start, end):
                    out.append({**ev, "source": feed["name"]})
            except Exception:   # noqa: BLE001 — one broken feed never hides the others (its URL is never shown)
                self.failed += 1
        return sorted(out, key=lambda e: (e["start"], e["title"]))

    def add(self, title: str, start: datetime, minutes: int = 60, all_day: bool = False, location: str = "",
            remind_min: int | None = 15) -> dict:
        end = start + (timedelta(days=1) if all_day else timedelta(minutes=max(5, minutes)))
        eid = new_id("ev_")
        reminder = None
        if self.scheduler and remind_min is not None and not all_day:
            at = start.timestamp() - remind_min * 60
            if at > time.time():
                reminder = self.scheduler.add(f"Через {remind_min} мин: {title}" if remind_min else title, at, None)["id"]
        self.db.execute("INSERT INTO calendar_events(id,title,start_at,end_at,all_day,location,remind_min,reminder_id,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", (eid, title[:300], start.timestamp(), end.timestamp(), int(all_day),
                                                      location[:300] or None, remind_min, reminder, now()))
        return {"id": eid, "title": title, "start": start, "end": end, "all_day": all_day, "reminder_id": reminder}

    def cancel(self, eid: str) -> dict | None:
        r = self.db.one("SELECT * FROM calendar_events WHERE id=? AND state='active'", (eid,))
        if not r:
            return None
        self.db.execute("UPDATE calendar_events SET state='cancelled' WHERE id=?", (eid,))
        if r["reminder_id"] and self.scheduler:
            self.scheduler.cancel(r["reminder_id"])
        return dict(r)

    def find(self, query: str, after: datetime | None = None) -> list[dict]:
        """Upcoming own events whose title has all the words (SQLite's lower() does not know Cyrillic)."""
        words = [w for w in re.findall(r"\w+", query.casefold().replace("ё", "е")) if len(w) > 1]
        rows = self.db.all("SELECT * FROM calendar_events WHERE state='active' AND end_at > ? ORDER BY start_at",
                           ((after or datetime.now()).timestamp(),))
        title = lambda r: r["title"].casefold().replace("ё", "е")
        return [dict(r) for r in rows if words and all(w[:5] in title(r) for w in words)]


def span_for(when: str, ref: datetime | None = None) -> tuple[datetime, datetime, str]:
    ref = ref or datetime.now()
    today = datetime.combine(ref.date(), datetime.min.time())
    if when == "tomorrow":
        return today + timedelta(days=1), today + timedelta(days=2), "Завтра"
    if when == "week":
        return today, today + timedelta(days=7), "На неделе"
    if when == "next_week":
        monday = today + timedelta(days=7 - today.weekday())
        return monday, monday + timedelta(days=7), "На следующей неделе"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", when or ""):
        d = datetime.fromisoformat(when)
        return d, d + timedelta(days=1), f"{d.day} {MONTH_RU[d.month - 1]}"
    return today, today + timedelta(days=1), "Сегодня"


def describe_events(events: list[dict], label: str, ref: datetime | None = None) -> str:
    if not events:
        return f"{label} в календаре ничего нет."
    multi = len({e["start"].date() for e in events}) > 1
    parts = []
    for e in events[:12]:
        at = "весь день" if e["all_day"] else f"{e['start']:%H:%M}"
        if multi:
            at = f"{WEEKDAY_RU[e['start'].weekday()]} {e['start'].day}, {at}"
        parts.append(f"{at} — {e['title']}" + (f" ({e['location']})" if e.get("location") else ""))
    more = f" И ещё {len(events) - 12}." if len(events) > 12 else ""
    return f"{label}: " + "; ".join(parts) + "." + more


class CalendarShow(Tool):
    def __init__(self, agenda: Agenda):
        self.agenda = agenda
        self.spec = ToolSpec(
            id="calendar_show", title="Календарь",
            description="The user's calendar (JARVIS's own events + subscribed iCal calendars): today, tomorrow, the "
                        "next 7 days (week), next week, or one date (YYYY-MM-DD).",
            params={"type": "object", "properties": {"when": {"type": "string", "description":
                    "today | tomorrow | week | next_week | YYYY-MM-DD"}}},
            risk=Risk.LOW, side_effects="none", timeout_s=30, verification="Прочитано из календаря JARVIS и подписок")

    async def run(self, args, ctx):
        start, end, label = span_for(args.get("when") or "today")
        events = await self.agenda.events(start, end)
        text = describe_events(events, label)
        if self.agenda.failed:
            text += f" (Не открылось подписок: {self.agenda.failed}.)"
        return ToolResult({"events": [{**e, "start": e["start"].isoformat(), "end": e["end"].isoformat()} for e in events],
                           "label": label}, text, card="calendar")

    async def verify(self, args, result, ctx):
        return Verification("verified", self.spec.verification)


class CalendarAdd(Tool):
    def __init__(self, agenda: Agenda):
        self.agenda = agenda
        self.spec = ToolSpec(
            id="calendar_add", title="Добавить в календарь",
            description="Add an event to the user's calendar: title, start (local ISO «2026-10-29T15:00», or a date "
                        "«2026-10-29» for an all-day event), duration in minutes, place; a reminder N minutes before "
                        "(default 15, 0 = at the start).",
            params={"type": "object", "required": ["title", "start"], "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 300},
                "start": {"type": "string"}, "minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
                "location": {"type": "string", "maxLength": 300},
                "remind_min": {"type": "integer", "minimum": 0, "maximum": 10080}}},
            risk=Risk.LOW, side_effects="local", timeout_s=5, verification="Событие прочитано из календаря")

    def validate(self, args, ctx):
        a = super().validate(args, ctx)
        try:
            start = datetime.fromisoformat(a["start"])
        except ValueError:
            raise ToolInputError("Дата начала: YYYY-MM-DD или YYYY-MM-DDTHH:MM") from None
        if start.tzinfo:
            start = start.astimezone().replace(tzinfo=None)
        a["_all_day"] = len(a["start"]) == 10
        a["start"] = start.isoformat(timespec="minutes") if not a["_all_day"] else start.date().isoformat()
        return a

    def describe(self, args):
        start = datetime.fromisoformat(args["start"])
        return f"В календарь: «{args['title']}» {when_ru(start, args.get('_all_day'))}"

    async def run(self, args, ctx):
        start = datetime.fromisoformat(args["start"])
        ev = self.agenda.add(args["title"], start, args.get("minutes", 60), args.get("_all_day", False),
                             args.get("location", ""), args.get("remind_min", 15))
        text = f"Добавил: «{ev['title']}» {when_ru(start, ev['all_day'])}"
        if ev["reminder_id"]:
            m = args.get("remind_min", 15)
            text += f", напомню за {m} мин" if m else ", напомню в начале"
        return ToolResult({"id": ev["id"], "reminder_id": ev["reminder_id"]}, text + ".")

    async def verify(self, args, result, ctx):
        ok = self.agenda.db.one("SELECT 1 FROM calendar_events WHERE id=? AND state='active'", (result.data["id"],))
        return Verification("verified" if ok else "failed", self.spec.verification)


class CalendarCancel(Tool):
    def __init__(self, agenda: Agenda):
        self.agenda = agenda
        self.spec = ToolSpec(
            id="calendar_cancel", title="Удалить из календаря",
            description="Remove one of JARVIS's own upcoming events (and its reminder) by words of its title. "
                        "Subscribed calendars are read-only.",
            params={"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string", "minLength": 2, "maxLength": 200}}},
            risk=Risk.MODERATE, side_effects="local", timeout_s=5, verification="Событие помечено отменённым")

    def describe(self, args):
        return f"Удалить из календаря: «{args['query']}»"

    async def run(self, args, ctx):
        found = self.agenda.find(args["query"])
        if not found:
            raise ToolError(f"В календаре нет предстоящего события «{args['query']}»")
        if len(found) > 1:
            names = "; ".join(f"{r['title']} {when_ru(datetime.fromtimestamp(r['start_at']), bool(r['all_day']))}"
                              for r in found[:4])
            raise ToolError(f"Подходит несколько: {names}. Уточните.")
        r = self.agenda.cancel(found[0]["id"])
        return ToolResult({"id": r["id"]}, f"Удалил «{r['title']}» "
                          f"{when_ru(datetime.fromtimestamp(r['start_at']), bool(r['all_day']))}.")

    async def verify(self, args, result, ctx):
        r = self.agenda.db.one("SELECT state FROM calendar_events WHERE id=?", (result.data["id"],))
        return Verification("verified" if r and r["state"] == "cancelled" else "failed", self.spec.verification)


class CalendarSubscribe(Tool):
    def __init__(self, agenda: Agenda):
        self.agenda = agenda
        self.spec = ToolSpec(
            id="calendar_subscribe", title="Подписаться на календарь",
            description="Add a calendar by its iCal link (Google Calendar: Settings → the calendar → «Secret address "
                        "in iCal format»; Outlook / Apple: «publish» / «share» link). Read-only. The link is a secret: "
                        "never repeat it in answers.",
            params={"type": "object", "required": ["url"], "properties": {
                "url": {"type": "string", "maxLength": 2000}, "name": {"type": "string", "maxLength": 60}}},
            risk=Risk.LOW, side_effects="local", timeout_s=30, verification="Календарь по ссылке прочитан")

    def describe(self, args):
        return f"Подписаться на календарь «{args.get('name') or 'Календарь'}»"

    async def run(self, args, ctx):
        feed = self.agenda.add_feed(args["url"], args.get("name", ""))
        try:
            n = len(await self.agenda._fetch(feed["url"]))
        except Exception as e:   # noqa: BLE001
            self.agenda.remove_feed(feed["id"])
            raise ToolError(f"Календарь по ссылке не открылся ({e.__class__.__name__}) — проверьте ссылку") from None
        return ToolResult({"id": feed["id"], "events": n}, f"Подписался на «{feed['name']}»: событий в нём {n}.")

    async def verify(self, args, result, ctx):
        return Verification("verified", self.spec.verification)
