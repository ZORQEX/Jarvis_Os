"""Weather and the daily briefing (ported from morph-rq/jarvis-os capabilities/web.py, Russian first).

Weather: Open-Meteo (no key, no account; only the city name and its coordinates leave the computer). The city is the
one asked about, else the one the person told JARVIS to remember («запомни, что я живу в Москве»).
Briefing: date and time, weather, today's reminders and scheduled messages, what waits for the person's decision
(JARVIS's approvals, proposals in «Заказы»), a failed task, a low battery — each source with its own short deadline,
so one slow source never holds up the rest.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta

import aiohttp

from .base import Risk, Tool, ToolError, ToolResult, ToolSpec, Verification

UA = "JARVIS-WorkOS/1 (personal desktop assistant)"
WMO_RU = {0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно", 45: "туман", 48: "изморозь",
          51: "морось", 53: "морось", 55: "сильная морось", 56: "ледяная морось", 57: "ледяная морось",
          61: "небольшой дождь", 63: "дождь", 65: "сильный дождь", 66: "ледяной дождь", 67: "ледяной дождь",
          71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа", 80: "ливни", 81: "ливни",
          82: "сильные ливни", 85: "снегопад", 86: "снегопад", 95: "гроза", 96: "гроза с градом", 99: "гроза с градом"}
DAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
             "декабря"]
_GEO: dict[str, dict] = {}


def base_forms(city: str) -> list[str]:
    """«в Москве» → Москва, «в Казани» → Казань, «в Санкт-Петербурге» → Санкт-Петербург (the geocoder wants the name)."""
    c = re.sub(r"^(?:в|во|на)\s+", "", city.strip(), flags=re.I).strip(" .,!?")
    if not re.search(r"[а-яё]", c, re.I) or len(c) < 4:
        return [c]
    stem = c[:-1]
    if c[-1] in "еЕ":
        return [c, stem + "а", stem, stem + "я", stem + "ь"]
    if c[-1] in "иИ":
        return [c, stem + "ь", stem + "я"]
    if c[-1] in "уУ":
        return [c, stem + "а", stem + "я"]
    return [c]


async def geocode(name: str, session: aiohttp.ClientSession) -> list[dict]:
    key = name.casefold()
    if key in _GEO:
        return _GEO[key]
    async with session.get("https://geocoding-api.open-meteo.com/v1/search", timeout=aiohttp.ClientTimeout(total=5),
                           params={"name": name, "count": 3, "language": "ru", "format": "json"}) as r:
        r.raise_for_status()
        res = (await r.json()).get("results") or []
    places = [{"name": t.get("name", name), "country": t.get("country", ""), "lat": t["latitude"], "lon": t["longitude"],
               "tz": t.get("timezone", "auto"), "population": t.get("population") or 0} for t in res]
    _GEO[key] = places
    return places


async def find_place(city: str, session: aiohttp.ClientSession) -> dict | None:
    """All grammatical forms are asked at once; the most populated match wins («Москве» → Москва, not Москверо). A form
    whose request failed is asked once more; when the service never answered, that is said — never «no such city»
    (seen 2026-10-26: «Не нашёл город «алматы»» after a 10 s timeout)."""
    forms = base_forms(city)
    found = await asyncio.gather(*(geocode(n, session) for n in forms), return_exceptions=True)
    failed = [n for n, f in zip(forms, found) if isinstance(f, BaseException)]
    if failed:
        found = [f for f in found if not isinstance(f, BaseException)] + \
            list(await asyncio.gather(*(geocode(n, session) for n in failed), return_exceptions=True))
    places = [p for f in found if isinstance(f, list) for p in f]
    if not places and any(isinstance(f, BaseException) for f in found):
        raise ToolError("Сервис погоды сейчас не отвечает — проверьте интернет и спросите ещё раз")
    return max(places, key=lambda p: p["population"]) if places else None


async def weather_report(city: str, days: int = 2) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), headers={"User-Agent": UA}) as s:
        place = await find_place(city, s)
        if place is None:
            raise ToolError(f"Не нашёл город «{city}»")
        async with s.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": place["lat"], "longitude": place["lon"], "timezone": place["tz"] or "auto",
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "forecast_days": max(1, min(7, days)), "wind_speed_unit": "ms"}) as r:
            r.raise_for_status()
            data = await r.json()
    cur, daily = data.get("current", {}), data.get("daily", {})
    temp, feels = round(cur.get("temperature_2m", 0)), round(cur.get("apparent_temperature", 0))
    text = WMO_RU.get(int(cur.get("weather_code", 0)), "")
    out = [{"date": d, "min": round(daily["temperature_2m_min"][i]), "max": round(daily["temperature_2m_max"][i]),
            "rain": (daily.get("precipitation_probability_max") or [None] * 9)[i],
            "text": WMO_RU.get(int(daily["weather_code"][i]), "")} for i, d in enumerate(daily.get("time", [])[:days])]
    msg = f"{place['name']}: сейчас {temp}°" + (f", ощущается как {feels}°" if abs(feels - temp) >= 2 else "") + \
        (f", {text}" if text else "") + "."
    if cur.get("wind_speed_10m") and cur["wind_speed_10m"] >= 7:
        msg += f" Ветер {round(cur['wind_speed_10m'])} м/с."
    if out:
        msg += f" Сегодня от {out[0]['min']}° до {out[0]['max']}°"
        msg += f", вероятность осадков {out[0]['rain']}%." if (out[0]["rain"] or 0) >= 40 else "."
    if len(out) > 1:
        msg += f" Завтра {out[1]['min']}…{out[1]['max']}°, {out[1]['text']}."
    return {"place": place, "now": {"temp": temp, "feels": feels, "text": text, "humidity": cur.get("relative_humidity_2m"),
                                    "wind_ms": cur.get("wind_speed_10m")}, "days": out, "message": msg}


CITY_FACT = re.compile(r"(?:живу|живём|живем|нахожусь|работаю)\s+(?:в|во)\s+([А-ЯЁA-Z][\w-]+(?:[\s-][А-ЯЁA-Z][\w-]+)?)|"
                       r"(?:мой|наш)\s+город\s*[—:-]?\s*([А-ЯЁA-Z][\w-]+)|я\s+из\s+([А-ЯЁA-Z][\w-]+)")


def home_city(db) -> str:
    """The city from Settings (kv home_city) or from what the person asked JARVIS to remember."""
    if db is None:
        return ""
    c = db.get_kv("home_city")
    if c:
        return str(c)
    from ..memory import facts
    for f in facts(db):
        m = CITY_FACT.search(f["text"])
        if m:
            return next(g for g in m.groups() if g)          # «Москве» is fine: weather_report tries base forms
    return ""


class Weather(Tool):
    def __init__(self, db=None):
        self.db = db
        self.spec = ToolSpec(
            id="weather", title="Погода",
            description="Current weather and a short forecast (Open-Meteo) for a city; without a city — the user's home "
                        "city (from memory: «живу в …»).",
            params={"type": "object", "properties": {"city": {"type": "string", "maxLength": 80},
                                                     "days": {"type": "integer", "minimum": 1, "maximum": 7}}},
            risk=Risk.LOW, side_effects="none", timeout_s=15, verification="Данные получены от Open-Meteo")

    def describe(self, args):
        return "Погода" + (f": {args['city']}" if args.get("city") else "")

    async def run(self, args, ctx):
        city = (args.get("city") or "").strip() or home_city(self.db)
        if not city:
            raise ToolError("Для какого города? Скажите, например: «запомни, что я живу в Москве»")
        rep = await weather_report(city, int(args.get("days", 2)))
        return ToolResult(rep, rep["message"], card="weather")

    async def verify(self, args, result, ctx):
        return Verification("verified" if result.data.get("now") else "failed", self.spec.verification)


def _date_ru(now: datetime) -> str:
    return f"{DAYS_RU[now.weekday()].capitalize()}, {now.day} {MONTHS_RU[now.month - 1]}, {now:%H:%M}"


class DailyBriefing(Tool):
    def __init__(self, db=None, fashion=None, agenda=None):
        self.db, self.fashion, self.agenda = db, fashion, agenda
        self.spec = ToolSpec(
            id="daily_briefing", title="Сводка на сегодня",
            description="A short briefing for today: date, weather, today's reminders and scheduled messages, what "
                        "waits for the user's decision (approvals, order changes), a failed task, low battery.",
            params={"type": "object", "properties": {}}, risk=Risk.LOW, side_effects="none", timeout_s=20,
            verification="Собрано из погоды, напоминаний, очереди сообщений и задач JARVIS")

    async def _deadline(self, coro, s: float, fallback=None):
        try:
            return await asyncio.wait_for(coro, s)
        except Exception:   # noqa: BLE001 — one slow or failing source never spoils the briefing
            return fallback

    async def run(self, args, ctx):
        now = datetime.now()
        city = home_city(self.db)
        start = datetime(now.year, now.month, now.day).timestamp()
        end = start + 86400
        day_start = datetime(now.year, now.month, now.day)
        weather, orders, events = await asyncio.gather(
            self._deadline(weather_report(city, 1), 8) if city else asyncio.sleep(0, None),
            self._deadline(self.fashion.state(), 4) if self.fashion and self.fashion.running() else asyncio.sleep(0, None),
            self._deadline(self.agenda.events(day_start, day_start + timedelta(days=1)), 8, []) if self.agenda
            else asyncio.sleep(0, []))
        parts = [_date_ru(now) + "."]
        data: dict = {"date": now.isoformat(timespec="minutes")}
        if weather:
            parts.append(weather["message"])
            data["weather"] = weather
        elif not city:
            parts.append("Погоду скажу, когда узнаю ваш город: «запомни, что я живу в …».")
        events = [e for e in (events or []) if e["all_day"] or e["end"] > now]
        if events:
            parts.append("В календаре: " + "; ".join(("весь день" if e["all_day"] else f"в {e['start']:%H:%M}") + f" — {e['title']}"
                                                     for e in events[:6]) + ".")
            data["events"] = len(events)
        if self.db is not None:
            rem = [dict(r) for r in self.db.all("SELECT text, due_at FROM reminders WHERE status='scheduled' AND "
                                                "due_at BETWEEN ? AND ? ORDER BY due_at", (time.time(), end))]
            if rem:
                parts.append("Напоминания: " + "; ".join(f"в {datetime.fromtimestamp(r['due_at']):%H:%M} — {r['text']}"
                                                         for r in rem[:5]) + ".")
            jobs = [dict(r) for r in self.db.all("SELECT to_name, channel, due_at FROM comm_jobs WHERE status='scheduled' "
                                                 "AND due_at BETWEEN ? AND ? ORDER BY due_at", (time.time(), end))]
            if jobs:
                parts.append(f"Запланировано сообщений: {len(jobs)} (первое в "
                             f"{datetime.fromtimestamp(jobs[0]['due_at']):%H:%M}, {jobs[0]['to_name']}).")
            waiting = self.db.one("SELECT COUNT(*) AS n FROM approvals WHERE status='pending'")
            if waiting and waiting["n"]:
                parts.append(f"Ждут вашего решения действий: {waiting['n']}.")
            failed = self.db.one("SELECT request FROM tasks WHERE state='failed' AND created_at > ? AND route != 'ui_read' "
                                 "ORDER BY created_at DESC LIMIT 1", (time.time() - 12 * 3600,))
            if failed:
                parts.append(f"Не получилось недавно: «{failed['request'][:70]}».")
            data.update(reminders=rem, scheduled_messages=len(jobs))
        if orders and orders.get("pending"):
            parts.append(f"В «Заказах» ждут подтверждения изменений: {orders['pending']}.")
            data["orders_pending"] = orders["pending"]
        from .system import _battery
        bat = _battery()
        if bat and bat["percent"] < 30 and bat["status"].lower() not in ("charging", "full"):
            parts.append(f"Батарея {bat['percent']}% — стоит подключить зарядку.")
        if len(parts) <= 2 and not (orders and orders.get("pending")):
            parts.append("Срочного ничего нет.")
        return ToolResult(data, " ".join(parts), card="briefing")

    async def verify(self, args, result, ctx):
        return Verification("verified", self.spec.verification)
