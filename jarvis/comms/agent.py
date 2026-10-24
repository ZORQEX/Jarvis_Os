"""Communication agent: voice/text command → local reading or Claude structured intent → recipient resolution →
clarification or confirmation → Gateway tool. Claude only reads the command (never credentials, never mail/chat contents);
the providers perform the real action; the agent never says "sent" unless a provider confirmed it."""
from __future__ import annotations

import datetime as dt
import re
import time

from ..db import now
from ..events import spawn
from .base import CHANNEL_RU, CHANNEL_TO, UNSUPPORTED, CommsError, Contact
from .contacts import best, is_self, match_score
from .outbox import STATUS_RU, _short, when_ru

PENDING_TTL = 300
PLATFORMS = {"telegram": r"телеграм\w*|телеграмм\w*|телег[аеиу]|тг|telegram|tg",
             "whatsapp": r"ват[сц]ап\w*|вотсап\w*|вацап\w*|whats ?app|ватс ап",
             "email": r"почт\w*|письм\w*|e-?mail|имейл\w*|емейл\w*|мейл\w*|электронк\w*"}
ORD = {"перв": 1, "втор": 2, "трет": 3, "четв": 4, "пят": 5, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}
# Spoken answers to «Нужно ваше решение…». A whole short phrase must match (never part of a longer command).
_YES_W = (r"да|ага|угу|давай|конечно|верно|точно|подтверждаю|подтвердить|подтверди|отправляй|отправь|отправляем|"
          r"запланируй|планируй|ок|окей|хорошо|ладно|разрешаю|разреши|разрешить|разрешено|одобряю|одобрить|одобри|"
          r"выполняй|выполни|выполнить|делай|сделай|можно|запускай|запусти|поехали|вперед|согласен|согласна|"
          r"принято|действуй|yes|okay|go")
_NO_W = (r"нет|не надо|не отправляй|не нужно|не делай|не разрешаю|не разрешать|запрещаю|запретить|отклоняю|отклонить|"
         r"отклони|отказ|отказываюсь|отбой|нельзя|отмена|отмени|отменить|стой|no")
YES = rf"((да|ну|так)[, ]+)?({_YES_W})([, ]+({_YES_W}))?"
NO = rf"((нет|ну)[, ]+)?({_NO_W})([, ]+({_NO_W}))?"
# Confirmed by a click only, never by voice: durable permission changes, and locks / gates / alarms (anyone at the door
# could say «да»).
CLICK_ONLY = {"trust_rule_add", "home_secure"}

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["send", "list_scheduled", "list_sent", "list_failed", "cancel", "edit",
                                               "trust", "save_contact", "other"]},
        "recipients": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"},
            "platform": {"type": "string", "enum": ["telegram", "whatsapp", "email", "unknown"]}},
            "required": ["name", "platform"]}},
        "message": {"type": "string"},
        "subject": {"type": "string"},
        "when": {"type": "string"},
        "time_unclear": {"type": "boolean"},
        "in_minutes": {"type": "number"},
        "target": {"type": "object", "properties": {
            "name": {"type": "string"}, "platform": {"type": "string"}, "when": {"type": "string"},
            "all": {"type": "boolean"}}},
        "changes": {"type": "object", "properties": {
            "message": {"type": "string"}, "when": {"type": "string"}, "platform": {"type": "string"},
            "recipient": {"type": "string"}, "subject": {"type": "string"}}},
        "contact": {"type": "object", "properties": {
            "name": {"type": "string"}, "email": {"type": "string"}, "phone": {"type": "string"},
            "telegram": {"type": "string"}}},
    },
    "required": ["action", "recipients", "message", "subject", "when"],
}

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def system_prompt(t: dt.datetime) -> str:
    return f"""Ты — модуль связи голосового ассистента JARVIS. Разбери команду пользователя (обычно распознанную с голоса,
возможны ошибки распознавания) и верни JSON по схеме. Ничего не выдумывай.
Сейчас {t:%Y-%m-%d %H:%M}, {WEEKDAYS[t.weekday()]}. Время — местное.

action:
- send — отправить или запланировать сообщение/письмо;
- list_scheduled — какие сообщения запланированы; list_sent — что отправлено (сегодня); list_failed — что не удалось отправить;
- cancel — отменить запланированное; edit — изменить запланированное или только что подготовленное;
- trust — «больше не спрашивай подтверждение для сообщений X в Y»; save_contact — «запомни почту/номер X»;
- other — команда не про сообщения.
Если фраза начинается с «нет», «не то», «не так», а дальше идёт команда отправить — это новая команда send, не отмена.
Пользователь может называть контакт так, как он записан: «контакту Любимая», «моей любимой», «маме» → name «Любимая»,
«мама» (без слов «контакт», «мой», «моей»). Слова «с сердечком», «со смайликом» к имени не относятся.
recipients: каждый получатель отдельно, name — в именительном падеже, как назвал пользователь: «Ивану» → «Иван»,
«Сергею Петрову» → «Сергей Петров», «маме» → «мама». «себе», «мне» → «себе». «ему», «ей», «этому человеку», «ему же» → «__last__».
Если e-mail адрес или номер телефона продиктован — name = адрес/номер.
platform: telegram (телеграм, телега, тг), whatsapp (ватсап, вотсап, вацап), email (почта, письмо, на почту, имейл).
Не сказано — unknown. «в Telegram и WhatsApp» одному человеку → два элемента с одним именем.
message: ровно текст, который уйдёт получателю. Если текст продиктован («…: Привет, буду через час») — дословно, только
поставь знаки препинания и исправь очевидные ошибки распознавания. Если это пересказ («напиши Сергею, что документы готовы») —
сформулируй коротко от лица пользователя: «Документы готовы.» Не добавляй приветствий и подписей, которых не просили.
Без кавычек вокруг. Нет текста — "".
subject: только для почты: названная тема или короткая тема 2–5 слов по смыслу; иначе "".
when: время отправки «YYYY-MM-DDTHH:MM», если указано. «в 13:00» — сегодня, если ещё не наступило, иначе завтра.
«завтра в 10 утра» → завтра 10:00. Не указано — "".
in_minutes: только для относительного времени («через 15 минут» → 15, «через час» → 60, «через полчаса» → 30);
тогда when = "". Для точного времени («в 13:00», «завтра в 10») — 0.
time_unclear: true, если про время сказано размыто («вечером», «попозже») и точного часа нет.
target (для cancel/edit): кого/что искать: name (именительный падеж или «__pending__», если речь о только что подготовленном
сообщении), platform, when («YYYY-MM-DDTHH:MM» или ""), all=true, если сказано «все».
changes (для edit): только то, что меняется: message, when, platform, recipient, subject.
В командах отмены и изменения «сообщение Ивану на 13:00» указывает, КАКОЕ сообщение (target.when), а не новое время;
changes.when — только если сказано «перенеси на…», «поменяй время на…».
contact (для save_contact): name, email, phone, telegram."""


def _local_dt(s: str) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s[:19])
    except ValueError:
        return None
    if d.tzinfo is not None:
        return d.timestamp()
    return time.mktime(d.timetuple())


_FILLER = {"джарвис", "jarvis", "говорю", "пожалуйста", "сообщение", "сообщения", "письмо", "моему", "моей", "моим",
            "мой", "моя", "контакту", "контакт", "контакта", "в", "во", "на", "через", "по", "почту", "почте", "а", "ну",
            "э", "ээ", "эм", "давай", "срочно", "отправь", "отправить", "напиши", "написать", "пошли", "скажи", "передай",
            "черкни", "сейчас", "с", "сердечком", "смайликом", "это", "этому", "человеку", "теперь", "ещё", "еще", "тоже",
            "у", "для", "что", "чтобы", "сегодня", "завтра", "послезавтра", "вечером", "утром", "днем", "днём", "ночью",
            "позже", "попозже", "потом", "текст", "и"}
_PRONOUN = {"ей", "ему", "им", "её", "его"}


def local_parse(text: str) -> dict | None:
    """Local reading (no AI): «отправь <кому> в <сервис> [через N минут | в HH:MM]: <текст>» and
    «… что <текст>», plus the status question. Anything less clear returns None and goes to Claude."""
    from ..router import strip_wake
    raw = strip_wake(text)
    t = raw.lower().replace("ё", "е")
    if re.search(r"(какие|что).*(запланир)", t):
        return {"action": "list_scheduled", "recipients": [], "message": "", "subject": "", "when": ""}
    if not re.search(r"\b(отправь|напиши|пошли|скажи|передай|черкни|подготовь)\b", t):
        return None
    # «Отправь письмо на ivan@mail.ru. Тема: Встреча. Текст: Буду в 10.» — address, subject and text as written.
    em = re.search(r"([\w.+-]+@[\w-]+\.[\w.-]*[a-z])\W*?(?:тема|subject)\s*[:—-]\s*(.+?)[.\s]*\s(?:текст|сообщение|"
                   r"содержание|body)\s*[:—-]\s*(.+)$", raw, re.I | re.S)
    if em:
        msg = em.group(3).strip()
        return {"action": "send", "message": msg[:1].upper() + msg[1:], "subject": em.group(2).strip(" .—-:"), "when": "",
                "time_unclear": False, "recipients": [{"name": em.group(1), "platform": "email"}], "_retold": False}
    chan = "|".join(PLATFORMS.values())
    retold = False
    m = (re.search(r"[:—](?!\d)\s*(.+)$", raw)
         or re.search(r"\b(?:а|и)\s+(?:сообщение|текст)\s*[:,]?\s+(.+)$", raw, re.I))          # «…а сообщение привет»
    if not m:
        m = re.search(r"\b(?:что|чтобы)\s+(.+)$", raw, re.I)
        retold = bool(m)                     # «напиши маме, что я задержусь»: Claude rephrases this better
    m = (m or re.search(r"(?<=[.!?])\s+(\S.+)$", raw)                                       # «…в ватсапе. Привет.»
         or re.search(rf"\b(?:в|на|по)\s+(?:{chan})\s*,?\s+(?!(?:в|на|через|завтра)\b)(\S.+)$", raw, re.I))  # «…в WhatsApp привет»
    if not m:
        return None
    msg = re.sub(r"[«»\"“”]", "", m.group(1)).strip()
    head = raw[:m.start(1)]                  # everything before the text itself (keeps «в WhatsApp» for the service)
    verb = re.search(r"\b(отправь|напиши|пошли|скажи|передай|черкни)\b", head, re.I)
    if verb:
        head = head[verb.start():]          # «Ещё раз повторяю, отправь …»: only what follows the command counts
    plats = [k for k, rx in PLATFORMS.items() if re.search(rf"\b({rx})\b", head.lower())] or ["unknown"]
    out = {"action": "send", "message": msg[:1].upper() + msg[1:], "subject": "", "when": "", "time_unclear": False}
    hl = head.lower()
    mm = re.search(r"через\s+(\d+|полчаса|час|минуту)\s*(минут\w*|час\w*)?", hl)
    if mm:
        n = {"полчаса": 30, "час": 60, "минуту": 1}.get(mm.group(1)) or int(mm.group(1))
        out["in_minutes"] = n * (60 if (mm.group(2) or "").startswith("час") else 1)
        hl = hl.replace(mm.group(0), " ")
    mt = re.search(r"\b(завтра\s+)?в\s+(\d{1,2})(?::(\d{2}))?\b", hl)
    if mt:
        d = dt.datetime.now().replace(hour=int(mt.group(2)) % 24, minute=int(mt.group(3) or 0), second=0, microsecond=0)
        if mt.group(1) or d <= dt.datetime.now():
            d += dt.timedelta(days=1)
        out["when"] = d.strftime("%Y-%m-%dT%H:%M")
        hl = hl.replace(mt.group(0), " ")
    for rx in PLATFORMS.values():
        hl = re.sub(rf"\b({rx})\b", " ", hl)
    tok = r"[\w.+-]+@[\w.-]+|\+?\d[\d -]{8,}\d|\w+(?:-\w+)?"
    words = [w for w in re.findall(tok, hl) if w not in _FILLER and w not in ("то", "вот", "так") and
             not re.fullmatch(r"(э+|м+|а+|эм+)(-(э+|м+|а+))*", w)]       # «э», «мм», «э-э» — but not «мама»
    if not words:
        return None
    named = [w for w in words if w not in _PRONOUN]
    if not named:
        name = "__last__"
    else:                      # keep the user's capitalisation: «Любимая», «Иван»
        orig = {w.lower().replace("ё", "е"): w for w in re.findall(tok, head)}
        name = " ".join(orig.get(w, w) for w in named)
    out["recipients"] = [{"name": name, "platform": p} for p in plats]
    out["_retold"] = retold or bool(re.search(r"\b(и|или)\b|,", re.sub(rf"\b({chan})\b", " ", head.lower())
                                                                  .split(verb.group(0).lower(), 1)[-1] if verb else ""))
    # «Сергею и Ивану», «маме или папе»: several people — left to Claude, never guessed locally
    vague = re.search(r"\b(вечером|утром|днем|днём|ночью|позже|попозже|потом|послезавтра|завтра|в понедельник|"
                      r"во вторник|в среду|в четверг|в пятницу|в субботу|в воскресенье|на выходных)\b", t)
    if vague and not out.get("when") and not out.get("in_minutes"):
        out["time_unclear"] = True           # never send «завтра» right now: JARVIS asks «во сколько?»
    return out


def parse_platform(text: str) -> str | None:
    t = text.lower()
    found = [k for k, rx in PLATFORMS.items() if re.search(rf"\b({rx})\b", t)]
    return found[0] if len(found) == 1 else None


def yes_no(text: str) -> bool | None:
    from ..router import strip_wake
    t = re.sub(r"[^\w\s,]", "", strip_wake(text).lower().replace("ё", "е")).strip()
    t = re.sub(r"\s+", " ", t)
    tail = r"(,?\s*(пожалуйста|спасибо|джарвис))?"
    if re.fullmatch(NO + tail, t):           # «нет, не надо» / «не разрешаю» — checked first: «не …» is never a yes
        return False
    if re.fullmatch(YES + tail, t):
        return True
    return None


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    w = one if n10 == 1 and n100 != 11 else few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many
    return f"{n} {w}"


def _count_words(n: int, noun=("сообщение", "сообщения", "сообщений")) -> str:
    words = {1: "одно", 2: "два", 3: "три", 4: "четыре"}
    return f"{words.get(n, n)} {plural(n, *noun).split(' ', 1)[1]}"


class CommsAgent:
    def __init__(self, hub, ai, config):
        self.hub, self.ai, self.config = hub, ai, config
        self.revisions: dict[str, dict] = {}       # task id → revised args (edit while a confirmation is open)
        self.superseded: set[str] = set()           # tasks whose open question was replaced by a newer command
        self.local_first = True                     # clear commands are read without AI (instant, no quota)

    # ---------------------------------------------------------------- entry points
    def pending(self, ctx: dict) -> dict | None:
        p = ctx.get("comms_pending")
        return p if p and time.time() - p.get("at", 0) < PENDING_TTL else None

    def answers_pending(self, text: str, ctx: dict) -> bool:
        """Does this utterance answer the question JARVIS just asked (platform / which person / what text)?"""
        p = self.pending(ctx)
        if not p:
            return False
        stage = p["stage"]
        if stage in ("message", "recipient_name", "time"):
            return True
        if stage == "platform":
            return parse_platform(text) is not None
        if stage in ("recipient", "cancel_pick", "edit_pick"):
            # An answer to «кому из них?» is short («второй», «Мама Active», «всем»). A whole sentence or a
            # question is a new request — measured: «Сколько будет 25 умножить на 4?» picked option 2.
            if len(re.findall(r"\w+", text)) > 5 or text.strip().endswith("?"):
                return False
            return self._pick(text, p["options"]) is not None or bool(re.search(r"\b(все|всех|обоим|обе|оба)\b", text.lower()))
        return False

    async def handle(self, director, tid: str, cid: str, text: str) -> None:
        ctx = director.ctx(cid)
        p = self.pending(ctx)
        if p and self.answers_pending(text, ctx):
            ctx.pop("comms_pending", None)
            director.save_ctx(cid, ctx)
            return await self._continue(director, tid, cid, p, text)
        ctx.pop("comms_pending", None)
        director.save_ctx(cid, ctx)
        try:
            intent = await self.parse(text)
        except Exception as e:   # noqa: BLE001
            return director._finish(tid, cid, "Не понял, кому и куда отправить, а Claude сейчас не отвечает. Скажите так: "
                                    "«Отправь маме в WhatsApp: привет» — кто, сервис и текст после двоеточия, "
                                    "тогда я пойму и без ИИ.", meta=self._meta(error=str(e)[:200]), failed=True)
        await self.dispatch(director, tid, cid, intent, text)

    async def parse(self, text: str) -> dict:
        t0 = time.perf_counter()
        local = local_parse(text)
        if local and self.local_first and self._confident(local):
            # A clear «кому / куда / что» command needs no AI: instant, no quota (measured: an AI round trip took 3–26 s).
            local["_ms"], local["_model"] = round((time.perf_counter() - t0) * 1000), "local"
            return local
        try:
            d = await self.ai.structured(system_prompt(dt.datetime.now()), text, SCHEMA, budget=60.0)
        except Exception:   # noqa: BLE001 — limits / network: simple commands still work without AI
            if local is None:
                raise
            local["_ms"], local["_model"] = round((time.perf_counter() - t0) * 1000), "local"
            return local
        if not self._plausible(d, text):        # rare garbled answer (e.g. a recipient called "а"): ask once more
            d = await self.ai.structured(system_prompt(dt.datetime.now()), text, SCHEMA)
            if not self._plausible(d, text):
                d = {"action": "other"} if d.get("action") != "send" else d
        d["_ms"] = round((time.perf_counter() - t0) * 1000)
        d["_model"] = getattr(self.ai, "last_structured_model", "")
        return d

    @staticmethod
    def _confident(d: dict) -> bool:
        """The local reading is unambiguous: one service, a short recipient, a text — and nothing that needs judgement
        (relative words like «завтра вечером» with no hour, pronouns, retold text with «что»)."""
        if d.get("action") == "list_scheduled":
            return True
        rc = d.get("recipients") or []
        msg = (d.get("message") or "").strip()
        return (d.get("action") == "send" and len(rc) == 1 and rc[0]["platform"] != "unknown"
                and rc[0]["name"] != "__last__" and 0 < len(rc[0]["name"].split()) <= 3 and 0 < len(msg) <= 500
                and not d.get("_retold"))

    @staticmethod
    def _plausible(d: dict, text: str) -> bool:
        if d.get("action") not in SCHEMA["properties"]["action"]["enum"]:
            return False
        if d.get("action") == "send":
            for r in d.get("recipients") or []:
                n = (r.get("name") or "").strip()
                if len(n) < 2:
                    return False
            msg = (d.get("message") or "").strip()
            if ":" in text and msg:
                import difflib
                said = text.split(":", 1)[1].strip().lower()
                if said and difflib.SequenceMatcher(None, said, msg.lower()).ratio() < 0.5:
                    return False
            elif msg and len(msg) < 2:
                return False
        return True

    async def dispatch(self, director, tid, cid, intent: dict, text: str) -> None:
        a = intent.get("action")
        if a == "send":
            return await self._send(director, tid, cid, intent)
        if a in ("list_scheduled", "list_sent", "list_failed"):
            return director._finish(tid, cid, self.status_text(a), meta=self._meta(intent))
        if a == "cancel":
            return await self._cancel(director, tid, cid, intent)
        if a == "edit":
            return await self._edit(director, tid, cid, intent)
        if a == "trust":
            return await self._trust(director, tid, cid, intent)
        if a == "save_contact":
            c = intent.get("contact") or {}
            args = {k: v for k, v in c.items() if v}
            if not args.get("name") or len(args) < 2:
                return director._finish(tid, cid, "Чей контакт и какой адрес или номер запомнить?", meta=self._meta(intent))
            out = await director.execute(tid, cid, "contact_save", args, "Запомнить контакт по вашей просьбе")
            return director._finish(tid, cid, director.outcome_text(out), meta=self._meta(intent),
                                    failed=out.status in ("failed", "invalid"))
        # Not about messages after all: hand over to the general AI path.
        await director.agency.run(director, tid, cid, text)

    # ---------------------------------------------------------------- send
    async def _send(self, director, tid, cid, intent: dict, resolved: list | None = None) -> None:
        ctx = director.ctx(cid)
        resolved = list(resolved or [])
        rcps = intent.get("recipients") or []
        if not rcps and not resolved:
            return self._ask(director, tid, cid, intent, "recipient_name", "Кому отправить?", resolved)
        if intent.get("time_unclear"):
            return self._ask(director, tid, cid, intent, "time", "Во сколько отправить?", resolved)
        due = _local_dt(intent.get("when"))
        mins = intent.get("in_minutes")
        if isinstance(mins, (int, float)) and not isinstance(mins, bool) and 0 < mins <= 366 * 24 * 60:
            due = now() + float(mins) * 60     # «через N минут»: exact, not rounded to the minute
            intent["in_minutes"] = None        # a follow-up turn must not shift it again
            intent["when"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(due))
        if due is not None and due < now() - 60:
            return self._ask(director, tid, cid, intent, "time", "Это время уже прошло. На когда запланировать?", resolved)
        for i, r in enumerate(rcps):
            name, plat = (r.get("name") or "").strip(), r.get("platform") or "unknown"
            last = ctx.get("last_contact")
            if name == "__last__":
                if not last:
                    return self._ask(director, tid, cid, intent, "recipient_name", "Кому именно отправить?", resolved)
                if plat in ("unknown", last["channel"]):
                    resolved.append(last)
                    continue
                name = last["to_name"]
            if plat == "unknown":
                if last and match_score(name, last["to_name"]) >= 0.8:
                    plat = last["channel"]
                else:
                    intent = dict(intent, recipients=[dict(r, name=name)] + rcps[i + 1:])
                    return self._ask(director, tid, cid, intent, "platform",
                                     f"Куда отправить {self._dative(name)}: в Telegram, WhatsApp или на почту?", resolved)
            p = self.hub.provider(plat)
            if hasattr(p, "warm") and p.connected():
                spawn(p.warm(), "whatsapp-warm")     # get WhatsApp Web ready while the user hears the question
            if not p.connected():
                shown = await director.open_connection(tid, cid, plat)
                return director._finish(tid, cid, f"{p.title} не подключён — отправить пока не могу. {shown} "
                                                  "Как подключится — повторите команду.", meta=self._meta(intent))
            if due and not p.caps.get("can_schedule"):
                return director._finish(tid, cid, UNSUPPORTED, meta=self._meta(intent))
            try:
                cands = best(await p.search(name))
            except CommsError as e:
                return director._finish(tid, cid, f"{p.title}: {e}", meta=self._meta(intent, error=e.detail), failed=True)
            except Exception as e:   # noqa: BLE001 — a provider bug must never end as «внутренняя ошибка»
                director.audit.write("comms.search_failed", channel=plat, error=repr(e)[:300])
                return director._finish(tid, cid, f"{p.title}: не получилось найти контакт ({e.__class__.__name__}). "
                                        "Ничего не отправлено — попробуйте ещё раз.", meta=self._meta(intent), failed=True)
            if not cands:
                return director._finish(tid, cid, f"Не нашёл «{name}» {CHANNEL_TO[plat].replace('на почту', 'в почте')}. "
                                        + ("Скажите адрес или попросите запомнить его: «запомни почту Сергея: …»."
                                           if plat == "email" else "Проверьте имя или назовите иначе."),
                                        meta=self._meta(intent))
            if len(cands) > 1:
                intent = dict(intent, recipients=rcps[i + 1:])
                opts = [self._delivery(c) for c in cands[:5]]
                lst = "; ".join(f"{n}) {o['to_name']}{' — ' + o['to_address'] if o.get('to_address') else ''}"
                                for n, o in enumerate(opts, 1))
                return self._ask(director, tid, cid, intent, "recipient", f"Нашёл несколько: {lst}. Кому отправить?",
                                 resolved, options=opts)
            d = self._delivery(cands[0])
            if cands[0].score < 0.7:
                d["guess"] = True          # «любимое» → «Любимая ❤️»: ask «это она?» before anything is sent
            resolved.append(d)
        if not resolved:
            return self._ask(director, tid, cid, intent, "recipient_name", "Кому отправить?", resolved)
        body = (intent.get("message") or "").strip()
        if not body:
            names = ", ".join(self._dative(d["to_name"]) for d in resolved)
            return self._ask(director, tid, cid, dict(intent, recipients=[]), "message", f"Что написать {names}?", resolved)
        args = {"deliveries": resolved, "body": body}
        if any(d["channel"] == "email" for d in resolved):
            args["subject"] = (intent.get("subject") or "").strip() or _subject_from(body)
        if due:
            args["due_at"] = due
        ctx = director.ctx(cid)
        ctx["last_contact"] = resolved[-1]
        ctx["last_contact_at"] = time.time()      # «отправь ей ещё…» right after is about the same person
        director.save_ctx(cid, ctx)
        await self._confirm_and_run(director, tid, cid, args, intent)

    async def _confirm_and_run(self, director, tid, cid, args: dict, intent: dict) -> None:
        # One open message question at a time: a new command replaces an unanswered one, so «да» is never ambiguous.
        for a in director.approvals.pending():
            if a["tool_id"] == "message_send" and a["task_id"] != tid:
                self.superseded.add(a["task_id"])
                director.approvals._finish(a["id"], "expired", None, reason="Новая команда — прежний вопрос снят")
        while True:
            self._say(director, tid, self.question(args))
            out = await director.execute(tid, cid, "message_send", args, "Отправка по вашей команде")
            rev = self.revisions.pop(tid, None)
            if out.status == "denied" and rev:
                args = rev
                continue
            break
        text = self.result_text(args, out)
        if out.status == "denied" and tid in self.superseded:
            self.superseded.discard(tid)
            text = "Не отправлено: вы дали новую команду, не ответив на вопрос."
        director._finish(tid, cid, text, meta=self._meta(intent, out=out),
                         failed=out.status in ("failed", "invalid"),
                         result={"status": out.status, "step_id": out.step_id})

    @staticmethod
    def _to(args: dict) -> str:
        """«Иван Петров, Telegram» — names stay in the nominative, so no Russian declension guesswork."""
        ch = {"telegram": "Telegram", "whatsapp": "WhatsApp", "email": "почта"}
        return "; ".join(f"{d['to_name']}, {ch[d['channel']]}" for d in args["deliveries"])

    def question(self, args: dict) -> str:
        body = _short(args["body"], 160).rstrip(" .!?…")
        guessed = [d["to_name"] for d in args["deliveries"] if d.get("guess")]
        parts = [f"Похоже, вы имеете в виду контакт «{self._join(guessed)}» — это нужный контакт?"] if guessed else []
        parts.append(f"Кому: {self._to(args)}.")
        if args.get("due_at"):
            parts.append(f"Когда: {when_ru(args['due_at'])}.")
        if args.get("subject"):
            parts.append(f"Тема: «{args['subject'].rstrip(' .')}».")
        parts.append(f"Текст: «{body}».")
        verb = "Изменить?" if args.get("replaces") else "Запланировать?" if args.get("due_at") else "Отправить?"
        return " ".join(parts + [verb])

    def result_text(self, args: dict, out) -> str:
        to = self._to(args)
        if out.status == "ok":
            if out.result.data.get("scheduled"):
                return f"Готово. Запланировал на {when_ru(args['due_at'])}: {to}. Отменить можно словами «отмени сообщение»."
            res = out.result.data.get("results") or []
            bad = [r for r in res if r["status"] != "sent"]
            if bad:
                return ("Отправлено частично. " + "; ".join(r["summary"] for r in res) + ".")
            if out.verification and out.verification.status == "unverified":
                return f"Отправил {to}, но сервис пока не подтвердил: {out.verification.detail}."
            return f"Готово. Отправил: {to}."
        if out.status == "denied":
            if "экстренная" in (out.error or "").lower():
                return "Не отправлено: включена экстренная остановка. Скажите «продолжи», чтобы снять её."
            if "отклонили" in (out.error or ""):
                if any(d.get("guess") for d in args["deliveries"]):
                    return "Хорошо, не отправляю. Назовите контакт точнее — как он записан в контактах."
                return "Хорошо, не отправляю."
            return f"Не отправлено: {out.error}."
        return f"Не отправлено: {out.error}"

    async def _continue(self, director, tid, cid, p: dict, text: str) -> None:
        intent, resolved, stage = p["intent"], p.get("resolved") or [], p["stage"]
        if stage == "platform":
            plat = parse_platform(text)
            rc = intent["recipients"]
            rc[0] = dict(rc[0], platform=plat)
            return await self._send(director, tid, cid, intent, resolved)
        if stage == "recipient":
            if re.search(r"\b(все|всех|обоим|оба|обе)\b", text.lower()):
                resolved += p["options"]
            else:
                resolved.append(p["options"][self._pick(text, p["options"]) - 1])
            return await self._send(director, tid, cid, intent, resolved)
        if stage == "recipient_name":
            sub = await self._reparse_part(text, intent, "recipients")
            return await self._send(director, tid, cid, sub, resolved)
        if stage == "message":
            return await self._send(director, tid, cid, dict(intent, message=text.strip()), resolved)
        if stage == "time":
            sub = await self._reparse_part(text, intent, "when")
            return await self._send(director, tid, cid, sub, resolved)
        if stage in ("cancel_pick", "edit_pick"):
            if re.search(r"\b(все|всех|оба|обе)\b", text.lower()) and stage == "cancel_pick":
                ids = [o["id"] for o in p["options"]]
            else:
                ids = [p["options"][self._pick(text, p["options"]) - 1]["id"]]
            if stage == "cancel_pick":
                return await self._run_cancel(director, tid, cid, ids, intent)
            return await self._apply_edit(director, tid, cid, self.hub.outbox.get(ids[0]), intent)

    async def _reparse_part(self, text: str, intent: dict, field: str) -> dict:
        """The user answered a follow-up ("Ивану", "в 14:00"): read just that piece with Claude."""
        d = await self.parse(("Отправь " + text) if field == "recipients" else ("Отправь сообщение " + text))
        out = dict(intent)
        if field == "recipients":
            out["recipients"] = d.get("recipients") or []
        else:
            out["when"], out["time_unclear"] = d.get("when") or "", bool(d.get("time_unclear"))
        return out

    # ---------------------------------------------------------------- status
    def status_text(self, action: str) -> str:
        ob = self.hub.outbox
        if action == "list_scheduled":
            jobs = ob.scheduled()
            if not jobs:
                return "Запланированных сообщений нет."
            items = self._join([ob.describe(j) for j in jobs[:6]])
            more = f" И ещё {len(jobs) - 6}." if len(jobs) > 6 else ""
            return f"Запланировано {_count_words(len(jobs))}: {items}.{more}"
        midnight = time.mktime(dt.date.today().timetuple())
        if action == "list_sent":
            jobs = [j for j in ob.list(("sent",), since=midnight)]
            if not jobs:
                return "Сегодня я ничего не отправлял."
            return (f"Сегодня отправлено {_count_words(len(jobs))}: " +
                    self._join([ob.describe(j, False) + f" в {time.strftime('%H:%M', time.localtime(j['sent_at']))}"
                                for j in jobs[:6]]) + ".")
        jobs = ob.list(("failed", "unknown", "missed"), since=midnight - 6 * 86400)
        if not jobs:
            return "За последнюю неделю всё, что я отправлял, ушло."
        return ("Не удалось отправить: " +
                self._join([ob.describe(j, False) + f" — {j.get('last_error') or STATUS_RU[j['status']]}" for j in jobs[:5]])
                + ".")

    # ---------------------------------------------------------------- cancel / edit
    def _find(self, target: dict, ctx: dict) -> list[dict]:
        jobs = self.hub.outbox.scheduled()
        name = (target or {}).get("name") or ""
        if name == "__last__" and ctx.get("last_contact"):
            name = ctx["last_contact"]["to_name"]
        plat = (target or {}).get("platform") or ""
        when = _local_dt((target or {}).get("when"))
        out = []
        for j in jobs:
            if is_self(name):
                if not (j["to_id"] == "self" or j["to_name"].startswith(("Вы", "Избранное"))):
                    continue
            elif name and name not in ("__pending__",) and not match_score(name, j["to_name"]):
                continue
            if plat in CHANNEL_RU and j["channel"] != plat:
                continue
            if when and abs((j["due_at"] or 0) - when) > 90:
                continue
            out.append(j)
        return out

    async def _cancel(self, director, tid, cid, intent):
        ctx = director.ctx(cid)
        target = intent.get("target") or {}
        jobs = self._find(target, ctx)
        if not jobs:
            return director._finish(tid, cid, "Не нашёл такого запланированного сообщения. " +
                                    self.status_text("list_scheduled"), meta=self._meta(intent))
        if len(jobs) > 1 and not target.get("all"):
            opts = [{"id": j["id"], "to_name": j["to_name"], "label": self.hub.outbox.describe(j)} for j in jobs[:6]]
            lst = "; ".join(f"{n}) {o['label']}" for n, o in enumerate(opts, 1))
            return self._ask(director, tid, cid, intent, "cancel_pick", f"Какое сообщение отменить? {lst}", [], options=opts)
        await self._run_cancel(director, tid, cid, [j["id"] for j in jobs], intent)

    async def _run_cancel(self, director, tid, cid, ids, intent):
        out = await director.execute(tid, cid, "message_cancel", {"job_ids": ids}, "Отмена по вашей команде")
        director._finish(tid, cid, director.outcome_text(out), meta=self._meta(intent, out=out),
                         failed=out.status in ("failed", "invalid"))

    async def _edit(self, director, tid, cid, intent):
        target = intent.get("target") or {}
        # Editing what JARVIS is asking about right now: re-ask with the new content.
        pend = [a for a in director.approvals.pending() if a["tool_id"] == "message_send"]
        if pend and (target.get("name") in ("", "__pending__", "__last__", None) or len(pend) == 1):
            a = pend[-1]
            new = self._apply_changes(dict(a["args"]), intent.get("changes") or {})
            if isinstance(new, str):
                return director._finish(tid, cid, new, meta=self._meta(intent))
            self.revisions[a["task_id"]] = new
            director.approvals._finish(a["id"], "expired", None, reason="Изменено вами — нужен новый ответ")
            return director._finish(tid, cid, "Изменил. Подтвердите ещё раз.", meta=self._meta(intent))
        jobs = self._find(target, director.ctx(cid))
        if not jobs:
            return director._finish(tid, cid, "Не нашёл такого запланированного сообщения. " +
                                    self.status_text("list_scheduled"), meta=self._meta(intent))
        if len(jobs) > 1:
            opts = [{"id": j["id"], "to_name": j["to_name"], "label": self.hub.outbox.describe(j)} for j in jobs[:6]]
            lst = "; ".join(f"{n}) {o['label']}" for n, o in enumerate(opts, 1))
            return self._ask(director, tid, cid, intent, "edit_pick", f"Какое сообщение изменить? {lst}", [], options=opts)
        await self._apply_edit(director, tid, cid, jobs[0], intent)

    async def _apply_edit(self, director, tid, cid, job: dict, intent: dict):
        base = {"deliveries": [{"channel": job["channel"], "to_id": job["to_id"], "to_name": job["to_name"],
                                "to_address": job.get("to_address") or ""}],
                "body": job["body"], "due_at": job["due_at"], "replaces": [job["id"]]}
        if job.get("subject"):
            base["subject"] = job["subject"]
        ch = intent.get("changes") or {}
        if ch.get("recipient") or (ch.get("platform") and ch["platform"] != job["channel"]):
            plat = ch.get("platform") if ch.get("platform") in CHANNEL_RU else job["channel"]
            p = self.hub.provider(plat)
            if not p.connected():
                return director._finish(tid, cid, f"{p.title} не подключён.", meta=self._meta(intent))
            cands = best(await p.search(ch.get("recipient") or job["to_name"]))
            if len(cands) != 1:
                return director._finish(tid, cid, "Не смог однозначно найти нового получателя — назовите точнее.",
                                        meta=self._meta(intent))
            base["deliveries"] = [self._delivery(cands[0])]
            if plat == "email" and not base.get("subject"):
                base["subject"] = _subject_from(base["body"])
            if plat != "email":
                base.pop("subject", None)
        new = self._apply_changes(base, {k: v for k, v in ch.items() if k in ("message", "when", "subject")})
        if isinstance(new, str):
            return director._finish(tid, cid, new, meta=self._meta(intent))
        await self._confirm_and_run(director, tid, cid, new, intent)

    def _apply_changes(self, args: dict, ch: dict):
        if ch.get("message"):
            args["body"] = ch["message"].strip()
        if ch.get("subject"):
            args["subject"] = ch["subject"].strip()
        if ch.get("when"):
            t = _local_dt(ch["when"])
            if not t or t < now() - 60:
                return "Не понял новое время — скажите, например, «в 14:30»."
            args["due_at"] = t
        return args

    async def revise(self, director, aid: str, body: str | None, subject: str | None) -> None:
        """The approval card's editor: new text → the old confirmation closes, a new one is asked."""
        a = director.approvals.get(aid)
        if not a or a["status"] != "pending" or a["tool_id"] != "message_send":
            raise ValueError("Это подтверждение уже закрыто")
        new = dict(a["args"])
        if body is not None:
            if not body.strip():
                raise ValueError("Пустой текст")
            new["body"] = body.strip()
        if subject is not None and any(d["channel"] == "email" for d in new["deliveries"]):
            new["subject"] = subject.strip() or _subject_from(new["body"])
        self.revisions[a["task_id"]] = new
        director.approvals._finish(aid, "expired", None, reason="Изменено вами — нужен новый ответ")

    # ---------------------------------------------------------------- trust rules
    async def _trust(self, director, tid, cid, intent):
        r = (intent.get("recipients") or [{}])[0]
        plat = r.get("platform") if r.get("platform") in CHANNEL_RU else None
        if not r.get("name") or not plat:
            return director._finish(tid, cid, "Для кого и в каком сервисе не спрашивать подтверждение? "
                                    "Например: «для сообщений Ивану в Telegram подтверждение не спрашивай».",
                                    meta=self._meta(intent))
        p = self.hub.provider(plat)
        if not p.connected():
            return director._finish(tid, cid, f"{p.title} не подключён.", meta=self._meta(intent))
        cands = best(await p.search(r["name"]))
        if len(cands) != 1:
            return director._finish(tid, cid, "Не смог однозначно найти этого человека — назовите точнее.",
                                    meta=self._meta(intent))
        d = self._delivery(cands[0])
        self._say(director, tid, "Подтвердите правило кнопкой на экране.")
        out = await director.execute(tid, cid, "trust_rule_add", {k: d[k] for k in ("channel", "to_id", "to_name")},
                                     "Правило по вашей просьбе")
        director._finish(tid, cid, director.outcome_text(out), meta=self._meta(intent, out=out),
                         failed=out.status in ("failed", "invalid"))

    # ---------------------------------------------------------------- helpers
    def _ask(self, director, tid, cid, intent, stage, question, resolved, options=None) -> None:
        ctx = director.ctx(cid)
        ctx["comms_pending"] = {"stage": stage, "intent": {k: v for k, v in intent.items() if not k.startswith("_")},
                                "resolved": resolved, "options": options or [], "at": time.time()}
        director.save_ctx(cid, ctx)
        director._finish(tid, cid, question, meta=self._meta(intent, asked=stage))

    def _say(self, director, tid, text: str) -> None:
        rt = director.rt.get(tid)
        if rt and rt.voice and director.speech:
            director.speech.speak_now(tid, text, rt.turn)

    @staticmethod
    def _pick(text: str, options: list[dict]) -> int | None:
        t = text.lower()
        for k, n in ORD.items():
            pat = rf"\b{k}\b" if k.isdigit() else rf"\b{k}"      # «2» alone, never the start of «25»
            if re.search(pat, t) and n <= len(options):
                return n
        scores = [match_score(t, o.get("to_name", "")) for o in options]
        if scores and max(scores) > 0 and scores.count(max(scores)) == 1:
            return scores.index(max(scores)) + 1
        for i, o in enumerate(options, 1):          # a surname or a word of the name said alone
            if any(len(w) > 2 and match_score(w, o.get("to_name", "")) for w in re.findall(r"\w+", t)) and \
                    sum(1 for o2 in options if any(len(w) > 2 and match_score(w, o2.get("to_name", ""))
                                                   for w in re.findall(r"\w+", t))) == 1:
                return i
        return None

    @staticmethod
    def _delivery(c: Contact) -> dict:
        return {"channel": c.channel, "to_id": c.id, "to_name": c.name, "to_address": c.address or ""}

    @staticmethod
    def _dative(name: str) -> str:
        return name   # names are shown as stored; Russian declension of arbitrary names is not attempted

    @staticmethod
    def _join(items: list[str]) -> str:
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " и " + items[-1] if items else ""

    @staticmethod
    def _meta(intent: dict | None = None, out=None, **kw) -> dict:
        m = {"route": "comms", "provider": "claude" if intent and intent.get("_model") not in (None, "", "local") else "local"}
        if intent and intent.get("_ms") is not None:
            m.update(intent_ms=intent["_ms"], model=intent.get("_model"))
        if out is not None:
            m["status"] = out.status
        m.update(kw)
        return m


def _subject_from(body: str) -> str:
    words = re.findall(r"[\w-]+", body)[:5]
    s = " ".join(words)
    return (s[:1].upper() + s[1:]) if s else "Сообщение"
