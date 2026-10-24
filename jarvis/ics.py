"""iCalendar (RFC 5545) reading for JARVIS's calendar: subscribed feeds (Google's «secret address in iCal format»,
Outlook, Apple, a factory's shared calendar) are read-only. Ported from morph-rq/jarvis-os (capabilities/mail.py):
folded lines, DATE and DATE-TIME (UTC, TZID, floating), DTEND or DURATION, simple RRULEs (DAILY / WEEKLY with INTERVAL,
COUNT, UNTIL, BYDAY; plain MONTHLY / YEARLY), EXDATE, RECURRENCE-ID overrides, cancelled instances. Instances come
out as local naive datetimes.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
MAX_PERIODS = 20000
FEED_TTL = 600.0
MAX_FEED = 5 * 1024 * 1024



@dataclass
class Stamp:
    wall: datetime          # wall-clock time in `tz` (midnight for dates)
    tz: tzinfo | None       # None: floating, i.e. the user's local time
    all_day: bool = False

    def local(self, wall: datetime | None = None) -> datetime:
        w = wall or self.wall
        if self.all_day or self.tz is None:
            return w
        return w.replace(tzinfo=self.tz).astimezone().replace(tzinfo=None)


def _zone(tzid: str) -> tzinfo | None:
    """IANA TZID -> tzinfo; unknown ids are treated as floating local time."""
    tzid = tzid.strip().strip('"')
    if tzid.upper() in ("UTC", "GMT", "Z", "ETC/UTC", "ETC/GMT"):
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
    except ImportError:   # pragma: no cover
        return None
    for candidate in (tzid, "/".join(tzid.strip("/").split("/")[-2:])):
        try:
            return ZoneInfo(candidate)
        except Exception:
            continue
    return None


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


def _split_prop(line: str) -> tuple[str, dict, str] | None:
    in_quotes, head, value = False, None, ""
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            head, value = line[:i], line[i + 1:]
            break
    if head is None:
        return None
    pieces = re.findall(r'(?:[^;"]|"[^"]*")+', head)
    if not pieces:
        return None
    params = {}
    for p in pieces[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip().upper()] = v.strip().strip('"')
    return pieces[0].strip().upper(), params, value


def unescape_text(value: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def _stamp(value: str, params: dict) -> Stamp | None:
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        try:
            return Stamp(datetime.strptime(value[:8], "%Y%m%d"), None, True)
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{8})T(\d{4})(\d{2})?(Z)?", value, re.I)
    if not m:
        return None
    try:
        wall = datetime.strptime(m.group(1) + m.group(2) + (m.group(3) or "00"), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    tz = timezone.utc if m.group(4) else (_zone(params["TZID"]) if params.get("TZID") else None)
    return Stamp(wall, tz, False)


def _duration(value: str) -> timedelta | None:
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip(), re.I)
    if not m:
        return None
    w, d, h, mi, s = (int(g or 0) for g in m.groups()[1:])
    delta = timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)
    return -delta if m.group(1) == "-" else delta


def _rrule(value: str) -> dict | None:
    rule: dict = {}
    for part in value.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            rule[k.strip().upper()] = v.strip()
    freq = rule.get("FREQ", "").upper()
    if not freq:
        return None
    out = {"FREQ": freq, "INTERVAL": 1, "COUNT": None, "UNTIL": None, "BYDAY": [], "OTHER": False}
    try:
        out["INTERVAL"] = max(1, min(1000, int(rule.get("INTERVAL", "1"))))
        if rule.get("COUNT"):
            out["COUNT"] = max(0, int(rule["COUNT"]))
    except ValueError:
        return None
    if rule.get("UNTIL"):
        out["UNTIL"] = _stamp(rule["UNTIL"], {})
    for day in rule.get("BYDAY", "").split(","):
        m = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", day.strip().upper())
        if m:
            if m.group(1):
                out["OTHER"] = True   # "2nd Tuesday": not supported, only the first instance is shown
            out["BYDAY"].append(WEEKDAYS[m.group(2)])
    out["OTHER"] = out["OTHER"] or any(k in rule for k in ("BYMONTHDAY", "BYMONTH", "BYSETPOS", "BYYEARDAY", "BYWEEKNO"))
    return out


def parse_ics(data: bytes | str) -> list[dict]:
    """VEVENTs of one calendar: {uid, title, location, start: Stamp, end: Stamp|None, duration, rrule,
    exdates: [Stamp], recurrence_id: Stamp|None, cancelled}. Nested components (VALARM) are skipped."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    events: list[dict] = []
    cur: dict | None = None
    nested = 0
    for line in _unfold(text.lstrip("\ufeff")):
        upper = line.strip().upper()
        if upper == "BEGIN:VEVENT" and cur is None:
            cur, nested = {"exdates": [], "title": "", "location": "", "uid": ""}, 0
            continue
        if cur is None:
            continue
        if upper.startswith("BEGIN:"):
            nested += 1
            continue
        if upper.startswith("END:"):
            if nested:
                nested -= 1
            elif upper == "END:VEVENT":
                if isinstance(cur.get("start"), Stamp):
                    events.append(cur)
                cur = None
            continue
        if nested:
            continue
        prop = _split_prop(line)
        if prop is None:
            continue
        name, params, value = prop
        if name == "SUMMARY":
            cur["title"] = unescape_text(value).strip()[:300]
        elif name == "LOCATION":
            cur["location"] = unescape_text(value).strip()[:300]
        elif name == "UID":
            cur["uid"] = value.strip()[:300]
        elif name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
            cur[{"DTSTART": "start", "DTEND": "end", "RECURRENCE-ID": "recurrence_id"}[name]] = _stamp(value, params)
        elif name == "DURATION":
            cur["duration"] = _duration(value)
        elif name == "RRULE":
            cur["rrule"] = _rrule(value)
        elif name == "EXDATE":
            cur["exdates"] += [s for s in (_stamp(v, params) for v in value.split(",")) if s]
        elif name == "STATUS":
            cur["cancelled"] = value.strip().upper() == "CANCELLED"
    return events


def _add_months(d: datetime, months: int) -> datetime | None:
    y, m = divmod(d.month - 1 + months, 12)
    try:
        return d.replace(year=d.year + y, month=m + 1)
    except ValueError:
        return None   # e.g. the 31st in a 30-day month: skipped, as RFC 5545 says


def _walls(start: Stamp, rule: dict, win_start: datetime, fast_forward: bool):
    """Candidate wall-clock starts in ascending order (the consumer stops at the window's end)."""
    base, interval, freq = start.wall, rule["INTERVAL"], rule["FREQ"]
    days = sorted(set(rule["BYDAY"]))
    if rule["OTHER"] or freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY") or \
            (days and freq in ("MONTHLY", "YEARLY")):
        yield base
        return
    gap = max(0, (win_start - base).days)
    if freq == "DAILY":
        k = max(0, gap // interval - 2) if fast_forward else 0
        for k in range(k, k + MAX_PERIODS):
            wall = base + timedelta(days=k * interval)
            if not days or wall.weekday() in days:
                yield wall
    elif freq == "WEEKLY":
        week = base - timedelta(days=base.weekday())
        k = max(0, gap // 7 // interval - 1) if fast_forward else 0
        for k in range(k, k + MAX_PERIODS):
            monday = week + timedelta(weeks=k * interval)
            for d in days or [base.weekday()]:
                wall = monday + timedelta(days=d)
                if wall >= base:
                    yield wall
    else:
        step = interval if freq == "MONTHLY" else 12 * interval
        for k in range(MAX_PERIODS):
            wall = _add_months(base, k * step)
            if wall is not None:
                yield wall


def _occurrences(ev: dict, win_start: datetime, win_end: datetime, skip: set) -> list[tuple[datetime, datetime]]:
    start: Stamp = ev["start"]
    first = start.local()
    if ev.get("end") is not None:
        span = ev["end"].local() - first
    elif ev.get("duration") is not None:
        span = ev["duration"]
    else:
        span = timedelta(days=1) if start.all_day else timedelta(0)
    span = max(span, timedelta(0))
    ex_times = {s.local() for s in ev["exdates"] if not s.all_day}
    ex_days = {s.wall.date() for s in ev["exdates"] if s.all_day}

    def keep(s: datetime) -> bool:
        if s in ex_times or (s, ev["uid"]) in skip or (start.all_day and s.date() in ex_days):
            return False
        e = s + span
        return s < win_end and (e > win_start or (span == timedelta(0) and s >= win_start))

    rule = ev.get("rrule")
    if not rule:
        return [(first, first + span)] if keep(first) else []
    until = rule["UNTIL"].local() if rule["UNTIL"] else None
    if until is not None and rule["UNTIL"].all_day:
        until += timedelta(days=1) - timedelta(seconds=1)
    out, n = [], 0
    for wall in _walls(start, rule, win_start - span - timedelta(days=2), rule["COUNT"] is None):
        s = start.local(wall)
        if until is not None and s > until:
            break
        n += 1
        if rule["COUNT"] is not None and n > rule["COUNT"]:
            break
        if s >= win_end:
            break
        if keep(s):
            out.append((s, s + span))
        if len(out) > 2000:
            break
    return out


def expand(vevents: list[dict], win_start: datetime, win_end: datetime) -> list[dict]:
    """Instances overlapping [win_start, win_end) as local naive datetimes, source "feed"."""
    overrides = {(ev["recurrence_id"].local(), ev["uid"]) for ev in vevents if ev.get("recurrence_id")}
    out = []
    for ev in vevents:
        if ev.get("cancelled"):
            continue
        skip = set() if ev.get("recurrence_id") else overrides
        single = dict(ev, rrule=None) if ev.get("recurrence_id") else ev
        for s, e in _occurrences(single, win_start, win_end, skip):
            digest = hashlib.sha1(f"{ev['uid']}|{s.isoformat()}".encode()).hexdigest()[:10]
            out.append({"id": f"feed-{digest}", "uid": ev["uid"] or digest, "title": ev["title"] or "(без названия)",
                        "start": s, "end": e, "all_day": ev["start"].all_day, "location": ev["location"], "source": "feed"})
    return out


