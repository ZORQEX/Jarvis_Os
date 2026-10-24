"""Proactive notices: JARVIS speaks up by itself only when it matters, and rarely (idea and rules from
morph-rq/jarvis-os, written for JARVIS's core).

Once a minute, cheap /proc and /sys reads (no AI): a low or critical battery, an almost full disk, memory nearly
exhausted, a service that just failed, the internet gone / back. Plus the person's own watches: «скажи, когда
батарея будет ниже 30 %», «когда процессор выше 90», «когда сайт example.com упадёт», «когда биткоин ниже 50000».

A notice = a line in the chat, a desktop notification and a toast; spoken only when urgent (critical battery, a
watch that fired) and never in quiet hours. Rules: a cooldown per kind, at most `MAX_PER_HOUR` non-urgent notices,
quiet hours (only urgent ones, silent), do-not-disturb respected. Notice text is JARVIS's own; nothing observed is
sent to a model. Settings → «Предупреждения» (kv proactive_enabled / proactive_quiet).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from datetime import datetime

import aiohttp

from .db import new_id, now
from .events import spawn
from .tools.base import Risk, Tool, ToolInputError, ToolResult, ToolSpec, Verification

INTERVAL = 60
MAX_PER_HOUR = 4
QUIET_DEFAULT = "23:00-08:00"
COOLDOWN = {"battery": 1800, "battery_critical": 300, "disk": 6 * 3600, "memory": 1800, "units": 3600,
            "network_down": 900, "network_up": 60, "watch": 0}
URGENT = {"battery_critical", "watch"}
KINDS = ("cpu", "memory", "disk", "battery", "website", "price")
KIND_RU = {"cpu": "процессор", "memory": "память", "disk": "диск", "battery": "батарея", "website": "сайт",
           "price": "цена"}
COINS = {"биткоин": "bitcoin", "биткойн": "bitcoin", "btc": "bitcoin", "bitcoin": "bitcoin", "эфир": "ethereum",
         "эфириум": "ethereum", "ethereum": "ethereum", "eth": "ethereum", "тон": "the-open-network",
         "toncoin": "the-open-network", "солана": "solana", "solana": "solana"}
MAX_WATCHES = 30


def in_quiet_hours(spec: str, at: datetime | None = None) -> bool:
    try:
        start, end = (datetime.strptime(x.strip(), "%H:%M").time() for x in spec.split("-"))
    except ValueError:
        return False
    t = (at or datetime.now()).time()
    return start <= t < end if start <= end else (t >= start or t < end)


def memory_pct() -> int | None:
    try:
        with open("/proc/meminfo") as fh:
            info = {line.split(":")[0]: int(line.split()[1]) for line in fh if line.split()[1:2]}
        return round(100 * (1 - info["MemAvailable"] / info["MemTotal"]))
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None


def cpu_times() -> list[int] | None:
    try:
        with open("/proc/stat") as fh:
            return [int(x) for x in fh.readline().split()[1:]]
    except (OSError, ValueError):
        return None


def cpu_pct(prev: list[int] | None, cur: list[int] | None) -> int | None:
    if not prev or not cur:
        return None
    idle = (cur[3] + cur[4]) - (prev[3] + prev[4])
    total = sum(cur) - sum(prev)
    return round(100 * (1 - idle / total)) if total > 0 else None


def online() -> bool:
    from .tools.machine import _probe
    return _probe("1.1.1.1", 443) is not None or _probe("8.8.8.8", 53) is not None


def gb(n: float) -> str:
    v = n / 2 ** 30
    return f"{v:.1f}".replace(".", ",") if v < 10 else f"{v:.0f}"


class Proactive:
    def __init__(self, db, bus, audit, director=None):
        self.db, self.bus, self.audit, self.director = db, bus, audit, director
        self.last: dict[str, float] = {}
        self.sent: list[float] = []
        self.mem_high = 0
        self.net_fail = 0
        self.was_offline = False
        self.known_units: set[str] | None = None
        self.cpu_prev: list[int] | None = None
        self.task: asyncio.Task | None = None
        # Replaced in tests: the machine's readings and the outside world.
        from .tools import machine
        self.battery = machine._battery
        self.failed_units = machine._failed_units
        self.disk_usage = shutil.disk_usage
        self.memory_pct, self.online, self.cpu_times = memory_pct, online, cpu_times
        self.dnd = self._dnd
        self.notify = self._notify

    # ------------------------------------------------------------------ settings
    def enabled(self) -> bool:
        return bool(self.db.get_kv("proactive_enabled", True))

    def quiet(self) -> str:
        return str(self.db.get_kv("proactive_quiet") or QUIET_DEFAULT)

    def settings(self, enabled: bool | None = None, quiet: str | None = None) -> dict:
        if enabled is not None:
            self.db.set_kv("proactive_enabled", bool(enabled))
        if quiet is not None:
            q = quiet.strip()
            if q and not re.fullmatch(r"\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}", q):
                raise ValueError("Тихие часы пишутся так: 23:00-08:00 (пусто — без тихих часов)")
            self.db.set_kv("proactive_quiet", q or "-")
        q = self.quiet()
        return {"enabled": self.enabled(), "quiet": "" if q == "-" else q,
                "watches": [dict(r) for r in self.db.all(
                    "SELECT id, label, active, last_value, fired_at FROM watches ORDER BY created_at DESC LIMIT 20")]}

    # ------------------------------------------------------------------ loop
    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop(), name="proactive")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            self.task = None

    async def _loop(self) -> None:
        await asyncio.sleep(20)                      # let the session settle after login / a restart
        while True:
            if self.enabled():
                try:
                    await self.tick()
                except Exception as e:   # noqa: BLE001 — one odd reading never stops the checks
                    self.audit.write("proactive.failed", error=repr(e)[:200])
            await asyncio.sleep(INTERVAL)

    # ------------------------------------------------------------------ rules
    def allow(self, kind: str) -> bool:
        """Cooldown per kind, the hourly budget and quiet hours; records the notice when allowed."""
        t = time.time()
        if t - self.last.get(kind, 0) < COOLDOWN.get(kind, 600):
            return False
        if kind not in URGENT:
            if in_quiet_hours(self.quiet()) or self.dnd():
                return False
            self.sent = [x for x in self.sent if t - x < 3600]
            if len(self.sent) >= MAX_PER_HOUR:
                return False
            self.sent.append(t)
        self.last[kind] = t
        return True

    def say(self, kind: str, title: str, body: str = "", speak: bool = False) -> bool:
        if not self.allow(kind):
            return False
        text = title + (f". {body}" if body else "")
        self.audit.write("proactive.notice", notice=kind)
        self.bus.publish("notice", {"kind": kind, "title": title, "body": body, "urgent": kind in URGENT})
        spawn(self.notify(title, body, kind in URGENT), "proactive-notify")
        d = self.director
        if d is not None:
            d.add_message(d.conversation_id(), "assistant", "🔔 " + text, meta={"route": "notice", "kind": kind})
            if speak and d.speech and not in_quiet_hours(self.quiet()):
                d.speech.say(new_id("notice-"), text)
        return True

    async def _notify(self, title: str, body: str, urgent: bool) -> None:
        from .scheduler import run_cmd
        try:
            await run_cmd(["notify-send", "-a", "JARVIS", "-u", "critical" if urgent else "normal", "--", title, body],
                          timeout=3)
        except Exception:   # noqa: BLE001 — the window and the chat still have it
            pass

    @staticmethod
    def _dnd() -> bool:
        try:
            from .tools.machine import _dnd
            return _dnd()
        except Exception:   # noqa: BLE001 — no mako: no do-not-disturb to respect
            return False

    async def tick(self) -> None:
        bat = await asyncio.to_thread(self.battery)
        if bat and bat.get("status") == "discharging":
            pct = bat.get("percent") or 0
            if pct <= 8:
                self.say("battery_critical", f"Батарея {pct} %", "Подключите зарядку или сохраните работу", speak=True)
            elif pct <= 20:
                self.say("battery", f"Батарея {pct} %", "Пора найти зарядку")
        pct = await asyncio.to_thread(self.memory_pct)
        self.mem_high = self.mem_high + 1 if pct is not None and pct >= 92 else 0
        if self.mem_high >= 2:
            from .tools.machine import top_processes
            top = await asyncio.to_thread(top_processes, "memory", 1, 0.2)
            self.say("memory", f"Память заполнена на {pct} %", f"Больше всех занимает {top[0]['name']}" if top else "")
        for mount in ("/", os.path.expanduser("~")):
            try:
                u = self.disk_usage(mount)
            except OSError:
                continue
            if u.total and u.free / u.total < 0.08:
                if self.say("disk", "Диск почти заполнен",
                            f"Свободно {gb(u.free)} ГБ — скажите «почисти диск», покажу, что можно удалить"):
                    break
        units = {u["unit"] for u in await asyncio.to_thread(self.failed_units)}
        if self.known_units is not None and units - self.known_units:
            self.say("units", "Упала служба", ", ".join(sorted(units - self.known_units))[:120])
        self.known_units = units
        ok = await asyncio.to_thread(self.online)
        self.net_fail = 0 if ok else self.net_fail + 1
        if self.net_fail >= 2 and not self.was_offline:
            self.was_offline = True
            self.say("network_down", "Интернет пропал", "Команды компьютера и голос работают, ИИ и сообщения — нет")
        elif ok and self.was_offline:
            self.was_offline = False
            self.say("network_up", "Интернет снова есть")
        await self.check_watches()

    # ------------------------------------------------------------------ watches
    async def check_watches(self) -> None:
        rows = [dict(r) for r in self.db.all("SELECT * FROM watches WHERE active=1")]
        cur = self.cpu_times()
        cpu, self.cpu_prev = cpu_pct(self.cpu_prev, cur), cur
        if not rows:
            return
        values = {"cpu": cpu, "memory": await asyncio.to_thread(self.memory_pct)}
        for w in rows:
            try:
                value = await self.value(w, values)
            except Exception:   # noqa: BLE001 — a site or price that can't be read is tried again next minute
                continue
            if value is None:
                continue
            if w["kind"] == "website":
                hit = (value == 0) == (w["condition"] == "down")
                shown = "не отвечает" if value == 0 else "снова отвечает"
            else:
                hit = value >= w["threshold"] if w["condition"] == "above" else value <= w["threshold"]
                shown = f"{value:g} $" if w["kind"] == "price" else f"{value:g} %"
            self.db.execute("UPDATE watches SET last_value=? WHERE id=?", (str(value), w["id"]))
            if hit:
                self.db.execute("UPDATE watches SET active=0, fired_at=? WHERE id=?", (now(), w["id"]))
                self.last.pop("watch", None)
                self.say("watch", f"Наблюдение: {w['label']}", f"Сейчас {shown}", speak=True)

    async def value(self, w: dict, values: dict) -> float | None:
        kind = w["kind"]
        if kind in ("cpu", "memory"):
            return values.get(kind)
        if kind == "battery":
            bat = await asyncio.to_thread(self.battery)
            return bat.get("percent") if bat else None
        if kind == "disk":
            u = self.disk_usage(w["target"] or "/")
            return round(100 * u.used / u.total)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10),
                                         headers={"User-Agent": "JARVIS-WorkOS"}) as s:
            if kind == "website":
                try:
                    async with s.get(w["target"], allow_redirects=True) as r:
                        return 1 if r.status < 500 else 0
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    return 0
            if kind == "price":
                async with s.get("https://api.coingecko.com/api/v3/simple/price",
                                 params={"ids": w["target"], "vs_currencies": "usd"}) as r:
                    return (await r.json(content_type=None)).get(w["target"], {}).get("usd")
        return None


def watch_label(kind: str, target: str, condition: str, threshold: float | None) -> str:
    if kind == "website":
        return f"{target} {'упадёт' if condition == 'down' else 'заработает'}"
    word = {"above": "выше", "below": "ниже"}[condition]
    if kind == "price":
        return f"{target} {word} {threshold:g} $"
    return f"{KIND_RU[kind]}{' ' + target if target else ''} {word} {threshold:g} %"


class Watch(Tool):
    def __init__(self, db):
        self.db = db
        self.spec = ToolSpec(
            id="watch", title="Наблюдение",
            description="Watch something and tell the user ONCE when it happens: CPU or memory above N %, disk above N %, "
                        "battery below N %, a website down/up (http(s) URL), a crypto price above/below a value in USD "
                        "(CoinGecko id: bitcoin, ethereum…). action=add|list|remove. Checked every minute while JARVIS runs.",
            params={"type": "object", "required": ["action"], "properties": {
                "action": {"type": "string", "enum": ["add", "list", "remove"]},
                "kind": {"type": "string", "enum": list(KINDS)},
                "target": {"type": "string", "maxLength": 300},
                "threshold": {"type": "number"},
                "condition": {"type": "string", "enum": ["above", "below", "down", "up"]},
                "id": {"type": "string"}}},
            risk=Risk.LOW, side_effects="local", timeout_s=5, idempotent=False,
            verification="Наблюдение записано в базу (или снято) — перечитано после записи")

    def describe(self, args):
        if args.get("action") == "add":
            return "Наблюдать: " + watch_label(*self._norm(args))
        return {"list": "Мои наблюдения", "remove": "Снять наблюдение"}.get(args.get("action"), "Наблюдение")

    def _norm(self, args) -> tuple[str, str, str, float | None]:
        kind = args.get("kind")
        if kind not in KINDS:
            raise ToolInputError("Что наблюдать: процессор, память, диск, батарею, сайт или цену")
        target = str(args.get("target") or "").strip()
        cond = args.get("condition") or ("down" if kind == "website" else "below" if kind == "battery" else "above")
        if kind == "website":
            if not target.startswith(("http://", "https://")):
                target = "https://" + target if re.fullmatch(r"[\w.-]+\.[a-z]{2,}(/\S*)?", target, re.I) else ""
            if not target:
                raise ToolInputError("Для сайта нужен адрес, например example.com")
            if cond not in ("down", "up"):
                cond = "down"
            return kind, target, cond, None
        if cond not in ("above", "below"):
            raise ToolInputError("Условие: выше или ниже")
        if args.get("threshold") is None:
            raise ToolInputError("Нужно число: при скольких процентах (или какой цене) сказать")
        if kind == "price":
            target = COINS.get(target.casefold(), target.casefold())
            if not re.fullmatch(r"[a-z0-9-]{2,40}", target):
                raise ToolInputError("Для цены нужна монета: bitcoin, ethereum…")
        elif kind == "disk":
            target = target or ""
        else:
            target = ""
        return kind, target, cond, float(args["threshold"])

    async def run(self, args, ctx):
        act = args["action"]
        if act == "list":
            rows = [dict(r) for r in self.db.all("SELECT id, label, last_value FROM watches WHERE active=1 "
                                                 "ORDER BY created_at DESC LIMIT 20")]
            return ToolResult({"watches": rows}, ("Наблюдаю: " + "; ".join(r["label"] for r in rows) + ".") if rows
                              else "Сейчас ничего не наблюдаю.")
        if act == "remove":
            wid = str(args.get("id") or "")
            row = self.db.one("SELECT id, label FROM watches WHERE active=1 AND id=?", (wid,)) if wid else \
                self.db.one("SELECT id, label FROM watches WHERE active=1 ORDER BY created_at DESC LIMIT 1")
            if not row:
                raise ToolInputError("Такого наблюдения нет")
            self.db.execute("UPDATE watches SET active=0 WHERE id=?", (row["id"],))
            return ToolResult({"id": row["id"], "removed": True}, f"Больше не наблюдаю: {row['label']}.")
        kind, target, cond, threshold = self._norm(args)
        if self.db.one("SELECT COUNT(*) AS n FROM watches WHERE active=1")["n"] >= MAX_WATCHES:
            raise ToolInputError(f"Уже {MAX_WATCHES} наблюдений — снимите ненужные")
        label = watch_label(kind, target, cond, threshold)
        wid = new_id("w_")
        self.db.execute("INSERT INTO watches(id, kind, target, threshold, condition, label, active, created_at)"
                        " VALUES(?,?,?,?,?,?,1,?)", (wid, kind, target, threshold, cond, label, now()))
        return ToolResult({"id": wid, "label": label}, f"Хорошо, скажу один раз, когда {label}.")

    async def verify(self, args, result, ctx):
        if args["action"] == "list":
            return Verification("verified", "Список прочитан из базы")
        r = self.db.one("SELECT active FROM watches WHERE id=?", (result.data["id"],))
        want = 0 if args["action"] == "remove" else 1
        if r and r["active"] == want:
            return Verification("verified", "Наблюдение " + ("снято" if want == 0 else "записано") + " — перечитано из базы")
        return Verification("failed", "Запись наблюдения не найдена после изменения")


def parse_watch(t: str, raw: str = "") -> dict | None:
    """«скажи, когда батарея будет ниже 30» → watch args (t: the router's normalized text without dots; raw: as said)."""
    m = re.match(r"(?:скажи|сообщи|предупреди|напомни|дай знать)(?: мне)?,? (?:когда|если) (.+)$", t)
    if not m:
        return None
    s = m.group(1)
    if re.search(r"упадет|ляжет|перестанет работать|не отвечает|станет недоступ|заработает|оживет|снова работает", s):
        site = re.search(r"((?:https?://)?(?:[\w-]+\.)+[a-z]{2,}(?:/[^\s,]*)?)", raw or "", re.I)
        if site:
            up = re.search(r"заработает|оживет|снова работает", s)
            return {"action": "add", "kind": "website", "target": site.group(1).rstrip("."),
                    "condition": "up" if up else "down"}
    num = re.search(r"(\d+(?:[.,]\d+)?)", s)
    if not num:
        return None
    value = float(num.group(1).replace(",", "."))
    below = bool(re.search(r"ниже|меньше|упадет|опустится|сядет", s))
    above = bool(re.search(r"выше|больше|превысит|поднимется|вырастет", s))
    if not (below or above):
        return None
    cond = "below" if below else "above"
    for word, kind in (("батаре", "battery"), ("заряд", "battery"), ("процессор", "cpu"), ("цп", "cpu"), ("cpu", "cpu"),
                       ("памят", "memory"), ("оперативк", "memory"), ("диск", "disk")):
        if word in s:
            return {"action": "add", "kind": kind, "condition": cond, "threshold": value}
    for word, coin in COINS.items():
        if re.search(rf"\b{word}\w*", s):
            return {"action": "add", "kind": "price", "target": coin, "condition": cond, "threshold": value}
    return None
