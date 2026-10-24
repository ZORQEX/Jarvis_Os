"""Smart home through Home Assistant's REST API (idea, allowlists and safety rules from morph-rq/jarvis-os).

Setup: Settings → «Умный дом»: the address (http://homeassistant.local:8123) and a long-lived access token (HA profile →
Security). The token lives in the keyring, travels only in the Authorization header and is never shown or logged.

Three tools, one risk each (CLAUDE.md):
- home_status (LOW): what is on, temperatures, is the door locked, the device list;
- home_control (LOW): lights, switches, fans, media players, climate, scenes, ordinary covers (blinds) — at most
  MAX_SAFE_TARGETS devices; it refuses locks, alarms, garage/gate/door covers, scripts and bigger groups;
- home_secure (HIGH, always asked): exactly those.
Only the SERVICES table is ever called; nothing from the model becomes a service name or part of a URL.
"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlsplit

import aiohttp

from .base import Risk, Tool, ToolError, ToolInputError, ToolResult, ToolSpec, Verification

TOKEN_KEY = "home_assistant_token"
MAX_SAFE_TARGETS = 5
MAX_BYTES = 8 * 1024 * 1024

# action → Home Assistant service, per domain. Nothing outside this table is ever called.
SERVICES: dict[str, dict[str, str]] = {
    "light": {"turn_on": "turn_on", "turn_off": "turn_off", "toggle": "toggle", "set": "turn_on"},
    "switch": {"turn_on": "turn_on", "turn_off": "turn_off", "toggle": "toggle"},
    "input_boolean": {"turn_on": "turn_on", "turn_off": "turn_off", "toggle": "toggle"},
    "fan": {"turn_on": "turn_on", "turn_off": "turn_off", "toggle": "toggle", "set": "set_percentage"},
    "media_player": {"turn_on": "turn_on", "turn_off": "turn_off", "toggle": "toggle", "set": "volume_set"},
    "climate": {"turn_on": "turn_on", "turn_off": "turn_off", "set": "set_temperature"},
    "scene": {"turn_on": "turn_on"},
    "script": {"turn_on": "turn_on", "turn_off": "turn_off", "toggle": "toggle"},
    "cover": {"turn_on": "open_cover", "turn_off": "close_cover", "toggle": "toggle", "set": "set_cover_position"},
    "lock": {"turn_on": "lock", "turn_off": "unlock"},
    "alarm_control_panel": {"turn_on": "alarm_arm_away", "turn_off": "alarm_disarm"},
}
READ_ONLY = ("sensor", "binary_sensor")
BROAD = ("light", "switch", "fan", "media_player", "input_boolean")      # what «всё в спальне» means
SENSITIVE_DOMAINS = ("lock", "alarm_control_panel", "script")
SENSITIVE_COVERS = ("garage", "gate", "door")
SENSITIVE_WORDS = ("garage", "gate", "door", "lock", "alarm", "гараж", "ворот", "двер", "замок", "замк", "сигнализац",
                   "калитк")
DOMAIN_WORDS = {
    "light": ("свет", "лампа", "лампу", "лампы", "освещение", "светильник", "люстра", "люстру", "light", "lights", "lamp"),
    "switch": ("розетка", "розетку", "розетки", "выключатель", "switch", "plug"),
    "fan": ("вентилятор", "fan"),
    "climate": ("термостат", "кондиционер", "отопление", "обогреватель", "thermostat", "heating"),
    "cover": ("шторы", "жалюзи", "занавески", "рольставни", "blinds", "curtains"),
    "media_player": ("телевизор", "колонка", "колонку", "тв", "tv", "speaker"),
    "lock": ("замок", "замки", "lock"),
}
ALL_WORDS = {"все", "всё", "весь", "вся", "всю", "везде", "all", "everything"}
FILLER = {"на", "в", "во", "у", "the", "in", "on", "a"}
STATE_RU = {"on": "включено", "off": "выключено", "locked": "заперто", "unlocked": "отперто", "open": "открыто",
            "closed": "закрыто", "unavailable": "недоступно", "heat": "обогрев", "cool": "охлаждение",
            "playing": "играет", "paused": "пауза", "idle": "ожидание", "armed_away": "на охране",
            "disarmed": "снято с охраны"}
VERBS = {"turn_on": "Включил", "turn_off": "Выключил", "toggle": "Переключил", "set": "Установил", "lock": "Запер",
         "unlock": "Отпер", "open_cover": "Открыл", "close_cover": "Закрыл", "alarm_arm_away": "Поставил на охрану",
         "alarm_disarm": "Снял с охраны"}
EXPECT = {"turn_on": ("on", "playing", "idle", "heat", "cool", "auto", "heat_cool", "dry", "fan_only"),
          "turn_off": ("off",), "lock": ("locked",), "unlock": ("unlocked",), "open_cover": ("open", "opening"),
          "close_cover": ("closed", "closing"), "alarm_arm_away": ("armed_away", "arming"),
          "alarm_disarm": ("disarmed",)}


class HomeError(ToolError):
    pass


def norm(s: str) -> str:
    return re.sub(r"[^\w\s]", " ", str(s).casefold().replace("ё", "е")).strip()


def stem(w: str) -> str:
    return w[:-2] if len(w) >= 6 else w[:-1] if len(w) >= 4 else w


def domain_of(s: dict) -> str:
    return s["entity_id"].split(".", 1)[0]


def attrs(s: dict) -> dict:
    a = s.get("attributes")
    return a if isinstance(a, dict) else {}


def name_of(s: dict) -> str:
    n = attrs(s).get("friendly_name")
    return n if isinstance(n, str) and n.strip() else s["entity_id"]


def hay(s: dict) -> str:
    a = attrs(s)
    extra = " ".join(str(a.get(k) or "") for k in ("area", "area_name", "area_id"))
    return norm(f"{name_of(s)} {s['entity_id'].split('.', 1)[1].replace('_', ' ')} {extra}")


def words_match(words: list[str], s: dict) -> int:
    h = hay(s)
    return sum(1 for w in words if stem(w) in h)


def sensitive(s: dict) -> bool:
    d = domain_of(s)
    if d in SENSITIVE_DOMAINS:
        return True
    if d == "cover":
        dc = str(attrs(s).get("device_class") or "").lower()
        return dc in SENSITIVE_COVERS if dc else any(w in hay(s) for w in SENSITIVE_WORDS)
    return False


def select(states: list[dict], args: dict, control: bool) -> list[dict]:
    """Devices meant by a spoken name, a room and/or a domain."""
    allowed = set(SERVICES) | (set() if control else set(READ_ONLY))
    domain = norm(args.get("domain") or "").replace(" ", "_")
    if domain and domain not in allowed:
        raise HomeError(f"С устройствами «{domain}» JARVIS не работает")
    pool = [s for s in states if domain_of(s) in allowed and (not domain or domain_of(s) == domain)]
    area = [w for w in norm(args.get("area") or "").split() if w not in FILLER and w not in ALL_WORDS]
    if area:
        pool = [s for s in pool if words_match(area, s) == len(area)]
    entity = str(args.get("entity") or "").strip()
    if entity:
        eid = entity.lower()
        if re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", eid):
            exact = [s for s in pool if s["entity_id"] == eid]
            if exact:
                return exact
        words = [w for w in norm(entity).split() if w not in FILLER]
        everything = any(w in ALL_WORDS for w in words)
        words = [w for w in words if w not in ALL_WORDS]
        if everything and not words:
            raise HomeError("Что именно — свет, розетки, всё в комнате?")
        wd = next((d for w in words for d, names in DOMAIN_WORDS.items() if w in names), "")
        rest = [w for w in words if not any(w in names for names in DOMAIN_WORDS.values())]
        if wd:
            cands = [s for s in pool if domain_of(s) == wd]
            if rest:
                cands = [s for s in cands if words_match(rest, s) == len(rest)]
            if cands:
                return cands
        scored = [(words_match(words, s), s) for s in pool]
        best = max((n for n, _ in scored), default=0)
        if best:
            return [s for n, s in scored if n == best]
        raise HomeError(f"Не нашёл устройство «{entity}»" + (f" ({args.get('area')})" if area else ""))
    if not domain and not area:
        if control:
            raise HomeError("Какое устройство? Назовите его, комнату или тип")
        return pool
    broad = pool if domain else [s for s in pool if domain_of(s) in (BROAD if control else (*BROAD, *READ_ONLY, "climate"))]
    if not broad:
        raise HomeError("Таких устройств нет" + (f" ({args.get('area')})" if area else ""))
    return broad


def device(s: dict) -> dict:
    a, d = attrs(s), domain_of(s)
    out = {"entity_id": s["entity_id"], "name": name_of(s), "domain": d, "state": str(s.get("state", ""))}
    if d == "light" and isinstance(a.get("brightness"), (int, float)) and out["state"] == "on":
        out["brightness"] = round(a["brightness"] / 255 * 100)
    if d == "climate":
        if isinstance(a.get("current_temperature"), (int, float)):
            out["temperature"] = a["current_temperature"]
        if isinstance(a.get("temperature"), (int, float)):
            out["target"] = a["temperature"]
    if d == "sensor" and a.get("device_class") == "temperature":
        try:
            out["temperature"] = float(out["state"])
        except ValueError:
            pass
    return out


def describe(d: dict) -> str:
    extra = []
    if "brightness" in d:
        extra.append(f"{d['brightness']} %")
    if "temperature" in d:
        extra.append(f"{d['temperature']:g} °C")
    if "target" in d:
        extra.append(f"цель {d['target']:g} °C")
    state = "" if d["domain"] == "sensor" and "temperature" in d else STATE_RU.get(d["state"], d["state"])
    return f"{d['name']}: " + ", ".join([x for x in (state, *extra) if x])


def names(devs: list[dict]) -> str:
    shown = [d["name"] for d in devs[:4]]
    return ", ".join(shown) + (f" и ещё {len(devs) - 4}" if len(devs) > 4 else "")


def pct(v: float) -> int:
    if not 0 <= v <= 100:
        raise HomeError(f"{v:g} — это не проценты (0–100)")
    return round(v)


def service_for(s: dict, action: str, value: float | None) -> tuple[str, dict]:
    d = domain_of(s)
    svc = SERVICES.get(d, {}).get(action)
    if svc is None:
        raise HomeError(f"{name_of(s)} так не умеет")
    if action == "set" and value is None:
        raise HomeError("Нужно значение: яркость в %, температура или %")
    extra: dict = {}
    if value is not None:
        if d == "light" and action in ("turn_on", "set"):
            extra["brightness_pct"] = pct(value)
        elif d == "fan" and action in ("turn_on", "set"):
            extra["percentage"] = pct(value)
        elif d == "cover" and action == "set":
            extra["position"] = pct(value)
        elif d == "media_player" and action == "set":
            extra["volume_level"] = round(pct(value) / 100, 2)
        elif d == "climate" and action == "set":
            a = attrs(s)
            lo = a.get("min_temp") if isinstance(a.get("min_temp"), (int, float)) else 5
            hi = a.get("max_temp") if isinstance(a.get("max_temp"), (int, float)) else 35
            if not lo <= value <= hi:
                raise HomeError(f"{name_of(s)}: можно от {lo:g} до {hi:g} °C")
            extra["temperature"] = value
    return svc, extra


class HomeAssistant:
    """The connection: address in kv home_url, token in the keyring."""

    def __init__(self, db, store):
        self.db, self.store = db, store

    def url(self) -> str:
        return str(self.db.get_kv("home_url") or "")

    def configured(self) -> bool:
        return bool(self.url() and self.store.get(TOKEN_KEY))

    @staticmethod
    def check_url(url: str) -> str:
        url = (url or "").strip().rstrip("/")
        if url and "://" not in url:
            url = "http://" + url
        try:
            p = urlsplit(url)
            p.port  # noqa: B018 — raises on a malformed port
        except ValueError:
            raise ToolInputError("Адрес Home Assistant выглядит так: http://homeassistant.local:8123") from None
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.query or p.fragment:
            raise ToolInputError("Адрес Home Assistant выглядит так: http://homeassistant.local:8123")
        return url

    async def connect(self, url: str, token: str) -> dict:
        url, token = self.check_url(url), (token or "").strip()
        if not token or len(token) < 20 or any(c.isspace() for c in token):
            raise ToolInputError("Нужен долгосрочный токен: Home Assistant → профиль → Безопасность → «Создать токен»")
        states = await self._get_states(url, token)
        self.db.set_kv("home_url", url)
        self.store.store(TOKEN_KEY, token, "JARVIS: Home Assistant")
        return {"configured": True, "url": url, "devices": len(states)}

    def forget(self) -> None:
        self.store.clear(TOKEN_KEY)
        self.db.set_kv("home_url", None)

    async def state(self) -> dict:
        if not self.configured():
            return {"configured": False, "url": self.url()}
        try:
            n = len(await self.states())
            return {"configured": True, "url": self.url(), "devices": n}
        except HomeError as e:
            return {"configured": True, "url": self.url(), "error": str(e)}

    async def call(self, method: str, path: str, payload: dict | None = None, url: str | None = None,
                   token: str | None = None):
        base, token = url or self.url(), token or self.store.get(TOKEN_KEY)
        if not base or not token:
            raise HomeError("Умный дом не подключён: Настройки → «Умный дом» (адрес и токен Home Assistant)")
        headers = {"Authorization": f"Bearer {token}", "User-Agent": "JARVIS-WorkOS"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.request(method, base + path, headers=headers, json=payload, allow_redirects=False) as r:
                    if r.status in (401, 403):
                        raise HomeError("Home Assistant не принял токен — создайте новый в профиле → Безопасность")
                    body = await r.content.read(MAX_BYTES + 1)
                    status = r.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise HomeError(f"Home Assistant не отвечает по адресу {base} ({type(e).__name__})") from None
        if len(body) > MAX_BYTES:
            raise HomeError("Ответ Home Assistant слишком большой")
        if status >= 300:
            raise HomeError(f"Home Assistant отказал (HTTP {status})" + (" — проверьте адрес" if status < 400 else ""))
        try:
            return json.loads(body) if body else None
        except ValueError:
            raise HomeError("Home Assistant ответил непонятно") from None

    async def _get_states(self, url: str | None = None, token: str | None = None) -> list[dict]:
        data = await self.call("GET", "/api/states", url=url, token=token)
        return [s for s in data if isinstance(s, dict) and isinstance(s.get("entity_id"), str)
                and re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", s["entity_id"])] if isinstance(data, list) else []

    async def states(self) -> list[dict]:
        return await self._get_states()


ARGS = {"entity": {"type": "string", "maxLength": 200, "description": "device name as said, e.g. «свет на кухне», or entity_id"},
        "area": {"type": "string", "maxLength": 100, "description": "room, e.g. «спальня»"},
        "domain": {"type": "string", "maxLength": 40, "description": "light, switch, fan, climate, cover, media_player…"}}


class HomeStatus(Tool):
    def __init__(self, ha: HomeAssistant):
        self.ha = ha
        self.spec = ToolSpec(
            id="home_status", title="Умный дом: состояние",
            description="Smart home (Home Assistant), read only: what is on, temperatures, is the door locked; "
                        "action=list gives the device list. Filter by entity name, area (room) or domain.",
            params={"type": "object", "properties": {"action": {"type": "string", "enum": ["status", "list"],
                                                                "default": "status"}, **ARGS}},
            risk=Risk.LOW, side_effects="none", timeout_s=15,
            verification="Состояния прочитаны из Home Assistant в момент запроса")

    async def run(self, args, ctx):
        states = await self.ha.states()
        filtered = any(args.get(k) for k in ("entity", "area", "domain"))
        if args.get("action") == "list":
            pool = select(states, args, control=False) if filtered else \
                [s for s in states if domain_of(s) in set(SERVICES) | set(READ_ONLY)]
            devs = [device(s) for s in pool]
            return ToolResult({"devices": devs[:60], "count": len(devs)},
                              f"Устройств: {len(devs)}." if devs else "Устройства не найдены.", card="home")
        pool = select(states, args, control=False) if filtered else \
            [s for s in states if domain_of(s) in ("lock", "alarm_control_panel", "climate")
             or (domain_of(s) in BROAD and s.get("state") in ("on", "playing"))]
        devs = [device(s) for s in pool][:20]
        return ToolResult({"devices": devs}, "; ".join(describe(d) for d in devs[:8]) + "." if devs
                          else "Всё выключено.", card="home")

    async def verify(self, args, result, ctx):
        return Verification("verified", "Прочитано из Home Assistant только что")


class _HomeChange(Tool):
    secure = False

    def __init__(self, ha: HomeAssistant):
        self.ha = ha

    def describe(self, args):
        what = args.get("entity") or " ".join(x for x in (args.get("domain"), args.get("area")) if x) or "устройства"
        act = {"turn_on": "включить / открыть / запереть", "turn_off": "выключить / закрыть / отпереть",
               "toggle": "переключить", "set": f"установить {args.get('value', '')}"}[args["action"]]
        return f"Умный дом: {act} — {what}"

    async def run(self, args, ctx):
        states = await self.ha.states()
        targets = select(states, args, control=True)
        risky = [s for s in targets if sensitive(s)]
        if not self.secure and (risky or len(targets) > MAX_SAFE_TARGETS):
            why = (f"{len(targets)} устройств сразу" if len(targets) > MAX_SAFE_TARGETS
                   else ", ".join(name_of(s) for s in risky))
            raise HomeError(f"Это {why} — такое делается через home_secure, с подтверждением")
        value = args.get("value")
        calls: dict[tuple[str, str, str], list[str]] = {}
        skipped: list[str] = []
        for s in targets:
            try:
                svc, extra = service_for(s, args["action"], value)
            except HomeError as e:
                skipped.append(str(e))
                continue
            calls.setdefault((domain_of(s), svc, json.dumps(extra, sort_keys=True)), []).append(s["entity_id"])
        if not calls:
            raise HomeError(skipped[0] if skipped else "Нечего делать")
        used = []
        for (dom, svc, extra), ids in calls.items():
            if svc not in SERVICES.get(dom, {}).values():           # never an arbitrary service
                raise HomeError(f"{dom}.{svc} не разрешено")
            await self.ha.call("POST", f"/api/services/{dom}/{svc}",
                               {"entity_id": ids[0] if len(ids) == 1 else ids, **json.loads(extra)})
            used.append(svc)
        acted = [s for s in targets if any(s["entity_id"] in ids for ids in calls.values())]
        devs = [device(s) for s in acted]
        verb = VERBS.get(used[0], "Сделал") if len(set(used)) == 1 and args["action"] != "set" else VERBS["set"]
        msg = f"{verb}: {names(devs)}" + (f" → {value:g}" if args["action"] == "set" and value is not None else "") + "."
        if skipped:
            msg += " Пропущено: " + "; ".join(skipped[:3])
        return ToolResult({"devices": devs, "services": used, "entity_ids": [d["entity_id"] for d in devs]}, msg,
                          card="home")

    async def verify(self, args, result, ctx):
        """Read the devices back: the state each service promises (toggle / set: only that they answer)."""
        want = {svc: EXPECT.get(svc) for svc in result.data["services"]}
        ids = set(result.data["entity_ids"])
        for _ in range(6):                                  # devices take a moment to report
            await asyncio.sleep(0.5)
            try:
                now_states = {s["entity_id"]: s for s in await self.ha.states() if s["entity_id"] in ids}
            except HomeError as e:
                return Verification("unverified", f"Не удалось перечитать состояние: {e}")
            expected = [w for w in want.values() if w]
            # Scenes and scripts have no on/off state; toggles and values are not checked as on/off either.
            now_states = {k: v for k, v in now_states.items() if domain_of(v) not in ("scene", "script")}
            if not expected or not now_states:
                return Verification("unverified", "Команда принята; итоговое значение Home Assistant не подтверждает")
            if all(str(s.get("state")) in sum(expected, ()) for s in now_states.values()):
                return Verification("verified", "Home Assistant сообщает новое состояние: " +
                                    ", ".join(f"{name_of(s)} — {STATE_RU.get(s['state'], s['state'])}"
                                              for s in list(now_states.values())[:4]))
        return Verification("failed", "Устройство не перешло в нужное состояние: " +
                            ", ".join(f"{name_of(s)} — {s.get('state')}" for s in list(now_states.values())[:4]))


CHANGE_PARAMS = {"type": "object", "required": ["action"], "properties": {
    "action": {"type": "string", "enum": ["turn_on", "turn_off", "toggle", "set"]},
    "value": {"type": "number", "description": "brightness % (lights), °C (climate), % (fans, covers, volume)"},
    **ARGS}}


class HomeControl(_HomeChange):
    def __init__(self, ha):
        super().__init__(ha)
        self.spec = ToolSpec(
            id="home_control", title="Умный дом: свет и приборы",
            description="Smart home (Home Assistant): turn_on / turn_off / toggle / set lights (brightness %), switches, "
                        "fans, media players (volume %), climate (°C), scenes, blinds (covers turn_on=open). At most "
                        f"{MAX_SAFE_TARGETS} devices. Locks, alarms, garage/gate/door, scripts or more devices → home_secure.",
            params=CHANGE_PARAMS, risk=Risk.LOW, side_effects="external", timeout_s=20, idempotent=False,
            verification="Состояние устройств перечитано из Home Assistant после команды",
            impact="Меняет состояние приборов в доме")


class HomeSecure(_HomeChange):
    secure = True

    def __init__(self, ha):
        super().__init__(ha)
        self.spec = ToolSpec(
            id="home_secure", title="Умный дом: замки, ворота, сигнализация",
            description="Smart home (Home Assistant), always confirmed by the user: locks (turn_on=lock, turn_off=unlock), "
                        "alarm panels (turn_on=arm, turn_off=disarm), garage/gate/door covers (turn_on=open), scripts, "
                        f"or more than {MAX_SAFE_TARGETS} devices at once.",
            params=CHANGE_PARAMS, risk=Risk.HIGH, side_effects="external", timeout_s=20, idempotent=False,
            verification="Состояние перечитано из Home Assistant после команды",
            impact="Замки, ворота, сигнализация или много устройств сразу — физическая безопасность дома")


def parse_home(t: str) -> dict | None:
    """«включи свет на кухне», «выключи весь свет», «свет в спальне на 30 процентов», «что включено дома»."""
    if re.fullmatch(r"(?:что|какие приборы|какой свет) (?:сейчас )?включен\w*(?: дома| в доме)?|(?:что|как) дома|"
                    r"состояние (?:умного )?дома", t):
        return {"tool": "home_status", "args": {}}
    m = re.match(r"(включи|выключи|переключи|открой|закрой|сделай|поставь)\s+(.+)$", t)
    if not m:
        return None
    verb, rest = m.groups()
    if not any(w in rest.split() for names in DOMAIN_WORDS.values() for w in names):
        return None                                       # «открой браузер», «закрой телеграм» are not the home
    # Locks, gates, alarms and «весь свет» (maybe many devices) go to home_secure: the card asks the person.
    secure = any(w in rest for w in SENSITIVE_WORDS) or any(w in rest.split() for w in ALL_WORDS)
    value = re.search(r"(?:на\s+)?(\d{1,3})\s*(?:%|процент\w*|градус\w*)", rest)
    entity = re.sub(r"(?:на\s+)?\d{1,3}\s*(?:%|процент\w*|градус\w*)", "", rest).strip()
    entity = re.sub(r"\s+(?:пожалуйста|дома)$", "", entity).strip()
    action = {"включи": "turn_on", "открой": "turn_on", "выключи": "turn_off", "закрой": "turn_off",
              "переключи": "toggle"}.get(verb, "set")
    if value and action != "turn_off":
        action = "set" if not re.search(r"свет|ламп|люстр|светильник", entity) or verb != "включи" else "turn_on"
    args = {"action": action, "entity": entity}
    if value:
        args["value"] = float(value.group(1))
    elif action == "set":
        return None
    return {"tool": "home_secure" if secure else "home_control", "args": args}
