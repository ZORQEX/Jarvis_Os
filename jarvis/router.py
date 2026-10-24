"""Level-0/1 router: unambiguous commands are mapped straight to tools,
without any model call. Anything not matched confidently goes to Claude."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .tools.desktop import APP_ALIASES


@dataclass
class Route:
    kind: str                     # tool | reply | control | search_open | comms | connect | skill | ui | answer
    tool: str | None = None
    args: dict = field(default_factory=dict)
    reply: str | None = None
    why: str = ""
    level: int = 0


# Spellings the local recogniser produces for «Джарвис» (measured: «Джарис», «Джас», «Давис», «Джавис»).
WAKE = re.compile(r"^\s*(?:hey|эй|хэй|хей|ok|окей)?\s*(?:джарвис|jarvis|жарвис|джервис|джарис|джавис|джаревис|"
                  r"жарис|джас|давис|дарвис)(?=[\s,!.:?…—–-]|$)[\s,!.:?…—–-]*", re.I)

NUM_WORDS = {
    "ноль": 0, "один": 1, "одну": 1, "одна": 1, "одной": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "пятнадцать": 15, "двадцать": 20,
    "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80,
    "девяносто": 90, "сто": 100, "полчаса": 30,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30,
}
ORDINALS = {
    "первый": 1, "первую": 1, "первое": 1, "второй": 2, "вторую": 2, "второе": 2, "третий": 3, "третью": 3,
    "третье": 3, "четвертый": 4, "четвертую": 4, "пятый": 5, "пятую": 5, "шестой": 6, "седьмой": 7,
    "восьмой": 8, "девятый": 9, "десятый": 10, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
}

FOLDERS = {
    "документы": "~/Documents", "загрузки": "~/Downloads", "проекты": "~/Projects", "изображения": "~/Pictures",
    "картинки": "~/Pictures", "фото": "~/Pictures", "музыку": "~/Music", "видео": "~/Videos",
    "заметки": "~/Documents/Jarvis/Заметки", "скриншоты": "~/Pictures/Jarvis", "documents": "~/Documents",
    "downloads": "~/Downloads",
}


def _lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# «Джарвис— первое, …»: after a pause GigaAM writes a dash right after the name (measured 2026-10-24: such phrases were
# not recognised as addressed and the companion dropped them).
_FIRST_WORD = re.compile(r"^\s*(?:(?:hey|эй|хэй|хей|ok|окей)[\s,]+)?([^\W\d_]+)(?=[\s,!.:?…—–-]|$)[\s,!.:?…—–-]*",
                         re.I)


def wake_len(text: str) -> int:
    """Length of the «Джарвис, » at the start of text, or 0. Besides the known spellings, a first word within two letters
    of «джарвис» counts: through the laptop's speakers and mic the local recogniser heard «Джарвес» (measured 2026-10-23)."""
    m = WAKE.match(text or "")
    if m:
        return m.end()
    m = _FIRST_WORD.match(text or "")
    if not m:
        return 0
    return m.end() if is_wake_word(m.group(1)) else 0


def is_wake_word(word: str) -> bool:
    """«Джарвис» as the recognisers hear it: «Джарвес», «Джорис», «Чарвис», «Javis», «Jervis»…"""
    w = word.casefold().replace("ё", "е")
    if w == "jarvis" or WAKE.fullmatch(w):
        return True
    if 5 <= len(w) <= 9 and w[0] in "джчзj" and w[-1] in "сзш" and _lev(w, "джарвис") <= 2:
        return True
    return 5 <= len(w) <= 8 and w[0] in "jgdc" and w[-1] in "sz" and _lev(w, "jarvis") <= 2


_WORDS = re.compile(r"[^\W\d_]+")


def find_wake(text: str) -> int:
    """Where «Джарвис» starts in a phrase, or -1. Wake listening hears whole stretches of sound: the recording may begin
    with a TV or another person and only then «Джарвис, который час» — the command is what follows the name."""
    for m in _WORDS.finditer(text or ""):
        if is_wake_word(m.group(0)):
            return m.start()
    return -1


def norm(text: str) -> str:
    t = strip_wake(text)
    t = t.casefold().replace("ё", "е")
    t = re.sub(r"[«»\"“”!?.,;]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_number(tok: str) -> int | None:
    tok = tok.strip()
    if tok.isdigit():
        return int(tok)
    parts = tok.split()
    if parts and all(p in NUM_WORDS for p in parts):
        return sum(NUM_WORDS[p] for p in parts)
    return None


def _ordinal(tok: str) -> int | None:
    if tok in ORDINALS:
        return ORDINALS[tok]
    m = re.fullmatch(r"(\d+)(?:-?(?:й|ю|е|ой|ый|ий))?", tok)
    return int(m.group(1)) if m else None


COMMS_CHANNEL = (r"(телеграм\w*|телеграмм\w*|телег[аеиу]|\bтг\b|telegram|ват[сц]ап\w*|вотсап\w*|вацап\w*|whats ?app|"
                 r"на почту|по почте|почтой|на (e-?mail|имейл|емейл|мейл)|электронн\w+ почт\w*)")
COMMS_VERB = r"\b(отправ\w*|напиши|написать|напишите|пошли|послать|перешли|сообщи|скинь|черкни|запланируй)\b"


CONNECT_CHANNELS = (("telegram", r"телеграм\w*|телег[аеиу]\b|\bтг\b|telegram"),
                    ("whatsapp", r"ват[сц]ап\w*|вотсап\w*|вацап\w*|whats ?app"),
                    ("email", r"почт\w*|e-?mail|имейл\w*|емейл\w*|мейл\w*|gmail|джимейл\w*"))


def is_comms(t: str) -> bool:
    """Messages to people (send / schedule / status / cancel / edit / trust). Pure text drafting stays elsewhere."""
    if re.search(COMMS_VERB, t) and re.search(COMMS_CHANNEL, t):
        return True
    if re.search(COMMS_VERB + r"|\bподготовь\b", t) and re.search(r"[\w.+-]+@[\w-]+(\.|\s)[a-z]{2,}", t):
        return True                         # «отправь письмо на ivan@mail.ru. Тема: …» — an explicit e-mail address
    if re.search(r"\b(отправь|пошли|отправить|запланируй)\b.*\bсообщени", t):
        return True
    if re.search(r"\b(напиши|отправь|пошли)\s+\w+(у|ю|е|и)\s*[:,—-]", t):     # «напиши Ивану: …»
        return True
    if re.search(r"сообщени\w*.*(запланир|отложен)|(запланир|отложен)\w*.*сообщени|"
                 r"что (ты )?(сегодня )?отправил|что не (удалось|получилось) отправить|какие (письма|сообщения)", t):
        return True
    if re.search(r"\b(отмени|измени|поменяй|перенеси)\b.*\b(сообщени|письм|отправк)", t):
        return True
    if re.search(r"подтверждени\w* (больше )?не (спрашивай|нужно|надо)|без подтверждения отправляй", t):
        return True
    if re.search(r"\bзапомни\b.*\b(почт|адрес|номер|телефон|e-?mail|имейл|телеграм)", t):
        return True
    if re.search(COMMS_VERB + r".*\bконтакт", t):            # «напиши любимая, контакту …»
        return True
    return False


def is_comms_followup(t: str, ctx: dict) -> bool:
    """«Отправь ей ещё, что я её люблю» shortly after a message to someone: the same person, not a chat question."""
    import time as _t
    recent = ctx.get("last_contact") and _t.time() - (ctx.get("last_contact_at") or 0) < 30 * 60
    return bool(recent and re.search(r"\b(отправь|напиши|скажи|передай|черкни|пошли|ответь)\b", t)
                and re.search(r"\b(ей|ему|им|ей же|ему же|ещё|еще|тоже|туда же)\b", t))


def strip_wake(text: str) -> str:
    t = (text or "").strip()
    return t[wake_len(t):]


def route(text: str, ctx: dict | None = None) -> Route | None:
    ctx = ctx or {}
    raw = strip_wake(text)
    t = norm(text)
    if not t:
        # "Джарвис." alone → "Да?"
        if not wake_len(text):
            return None
        return Route("answer", reply="Yes?" if re.match(r"\s*(?:hey\s+|ok\s+)?[a-z]", text, re.I) else "Да?",
                     why="Обращение по имени")

    # --- control -----------------------------------------------------------
    if re.fullmatch(r"(экстренная остановка|остановить все|останови все|прекрати все|emergency stop|стоп все|"
                    r"стоп стоп|отмени все)", t):
        return Route("control", args={"op": "estop"}, why="Экстренная остановка")
    # «Стоп»: stop speaking AND stop the work in progress (a site build, a Claude Code job) — never a message that is
    # being sent right now. «Замолчи / хватит»: only silence and drop the answer; jobs keep running.
    if re.fullmatch(r"(стоп|stop|остановись|останови|прекрати|остановить|стоп джарвис|джарвис стоп)", t):
        return Route("control", args={"op": "interrupt"}, why="Остановить")
    if re.fullmatch(r"(подожди|погоди|секунду|замолчи|хватит|хватит говорить|помолчи|тише говори|stop talking|"
                    r"shut up|не говори|достаточно|всё хватит|все хватит)", t):
        return Route("control", args={"op": "quiet"}, why="Перебить ответ")
    if re.fullmatch(r"(отмени|отмена|отменить|отмени задачу|отмени это|cancel|cancel task)", t):
        return Route("control", args={"op": "cancel_last"}, why="Отмена задачи")
    if re.fullmatch(r"(пауза|поставь на паузу|приостанови|pause)", t):
        return Route("control", args={"op": "pause_all"}, why="Пауза")
    if re.fullmatch(r"(продолжи|продолжай|возобнови|resume|continue|сними блокировку|можно продолжать)", t):
        return Route("control", args={"op": "resume_all"}, why="Продолжить")
    if re.fullmatch(r"(новый разговор|начни заново|новая беседа|new chat|new conversation)", t):
        return Route("control", args={"op": "new_conversation"}, why="Новый разговор")

    # --- connect a messenger: JARVIS opens the connect card itself (and starts the WhatsApp QR) ----------
    m = re.match(r"(?:подключи(?:сь)?|подключить|подсоедини(?:сь)?|привяжи|войди|залогинься|авторизуйся)\b(.*)$", t)
    if m:
        found = sorted((mm.start(), ch) for ch, pat in CONNECT_CHANNELS
                       for mm in [re.search(pat, m.group(1))] if mm)
        chans = list(dict.fromkeys(ch for _, ch in found))
        if chans:
            return Route("connect", args={"channels": chans}, why="Подключение сервиса", level=0)

    # --- communication: messages, mail, scheduled sends ----------------------------
    if is_comms(t) or is_comms_followup(t, ctx):
        return Route("comms", args={"text": raw}, why="Сообщения: Telegram, WhatsApp, почта", level=1)

    # --- deterministic calculations (no AI) -------------------------------------
    from .calc import answer as calc_answer
    calc = calc_answer(raw)
    if calc:
        return Route("answer", reply=calc, why="Локальный расчёт")

    # --- simple replies --------------------------------------------------------
    if re.fullmatch(r"(привет|здравствуй|здравствуйте|добрый (день|вечер)|доброе утро|hi|hello|hey)( джарвис)?", t):
        return Route("reply", reply="greeting", why="Приветствие")
    if re.fullmatch(r"(что ты умеешь|помощь|help|какие команды|что ты можешь)", t):
        return Route("reply", reply="help", why="Справка")
    if re.fullmatch(r"((скажи|подскажи)( мне)? )?(который( сейчас)? час|сколько( сейчас)? (времени|время)|"
                    r"какое( сейчас)? время|какое (сегодня )?число|какой (сегодня )?день( недели)?|"
                    r"какое сегодня число|what time is it|какая сегодня дата|время)( сейчас)?", t):
        return Route("reply", reply="time", why="Время и дата")
    if re.search(r"(статус|состояние) задач|что ты (сейчас )?делаешь|какие задачи|task status", t):
        return Route("reply", reply="tasks", why="Статус задач")

    # --- system ----------------------------------------------------------------
    if re.search(r"(состояние|статус|как (там )?(дела у )?) ?(системы|компьютера|ноутбука)|"
                 r"сколько (свободного )?(места|памяти)|заряд|батаре|температур[аы] (процессора|cpu)|"
                 r"(нагрузк[аи]|загрузк[аи]) (процессора|cpu)|что с (памятью|диском|батареей|процессором)|"
                 r"system (status|info)|battery|disk space|how much (memory|space)", t):
        return Route("tool", "system_info", {}, why="Чтение /proc и /sys")

    # --- windows / workspaces --------------------------------------------------
    if re.search(r"(какие|список|покажи) (окна|окон|открытые окна)|какие окна открыты|list windows", t):
        return Route("tool", "window_list", {}, why="Hyprland IPC")
    m = re.fullmatch(r"(?:на|рабочий стол) ([\w-]+)(?: (?:рабочий стол|стол))", t)
    if m and (n := _ordinal(m.group(1)) or parse_number(m.group(1))) and 1 <= n <= 10:
        return Route("tool", "workspace_switch", {"number": n}, why="Hyprland IPC")
    m = re.fullmatch(r"(?:переключись|перейди|переключи|switch|go)(?: на| to)?(?: (?:рабочий стол|workspace|стол))?"
                     r" ([\w-]+)(?: (?:рабочий стол|стол|workspace))?", t) \
        or re.fullmatch(r"(?:рабочий стол|workspace) ([\w-]+)", t)
    if m:
        n = _ordinal(m.group(1)) or parse_number(m.group(1))
        if n and 1 <= n <= 10:
            return Route("tool", "workspace_switch", {"number": n}, why="Hyprland IPC")

    # --- the JARVIS window: workspaces («перейди в задачи», «домой») -------------------
    view = ui_view(t)
    if view:
        return Route("ui", args={"view": view}, reply=f"Открываю «{UI_TITLES[view]}».", why="Раздел окна JARVIS")

    # --- volume ------------------------------------------------------------------
    m = re.search(r"(?:громкость|звук|volume)(?: на| to)? (\d{1,3}|[а-я ]+?)(?: процент\w*|%)?$", t)
    if m and (n := parse_number(m.group(1))) is not None and 0 <= n <= 100:
        return Route("tool", "volume_set", {"level": n}, why="PipeWire (wpctl)")
    if re.search(r"(звук|громкость) на (максимум|полную)", t):
        return Route("tool", "volume_set", {"level": 100}, why="PipeWire (wpctl)")
    if re.search(r"(звук|громкость) на минимум", t):
        return Route("tool", "volume_set", {"level": 10}, why="PipeWire (wpctl)")
    if re.search(r"убав\w* (звук|громкость)", t):
        return Route("tool", "volume_set", {"delta": -10}, why="PipeWire (wpctl)")
    if re.search(r"прибав\w* (звук|громкость)", t):
        return Route("tool", "volume_set", {"delta": 10}, why="PipeWire (wpctl)")
    if re.search(r"(сделай |чуть |немного )*(тише|потише|quieter|volume down)", t):
        return Route("tool", "volume_set", {"delta": -10}, why="PipeWire (wpctl)")
    if re.search(r"(сделай |чуть |немного )*(громче|погромче|louder|volume up)", t):
        return Route("tool", "volume_set", {"delta": 10}, why="PipeWire (wpctl)")
    if re.fullmatch(r"(выключи|отключи) звук|mute|без звука", t):
        return Route("tool", "volume_set", {"mute": True}, why="PipeWire (wpctl)")
    if re.fullmatch(r"(включи|верни) звук|unmute", t):
        return Route("tool", "volume_set", {"mute": False}, why="PipeWire (wpctl)")

    # --- screenshot --------------------------------------------------------------
    if re.search(r"(сделай |сними )?(скриншот|снимок экрана|скрин экрана)|заскринь|take (a )?screenshot|^screenshot$", t):
        return Route("tool", "screenshot", {}, why="grim (Wayland)")

    # --- notes ---------------------------------------------------------------------
    m = re.match(r"\s*(?:запиши|создай|сделай|добавь|сохрани)\s+(?:мне\s+)?заметку\s*[:,—-]?\s*(.+)$", raw, re.I | re.S) \
        or re.match(r"\s*(?:заметка|запиши|запомни|note|take a note)\s*[:,—-]\s*(.+)$", raw, re.I | re.S) \
        or re.match(r"\s*запомни,?\s+что\s+(.+)$", raw, re.I | re.S)
    if m and m.group(1).strip():
        return Route("tool", "create_note", {"text": m.group(1).strip()}, why="Запись файла в Документы/Jarvis")

    # --- reminders -----------------------------------------------------------------
    m = re.match(r"\s*напомни(?:\s+мне)?\s+через\s+(.+?)\s+(секунд\w*|минут\w*|час\w*)\s*(.*)$", raw, re.I | re.S)
    if m:
        amount = parse_number(norm(m.group(1))) if m.group(1).strip().casefold() != "полчаса" else 30
        unit = m.group(2).casefold()
        if amount is None and unit.startswith(("минут", "час")) and norm(m.group(1)) in ("", "одну"):
            amount = 1
        if amount:
            minutes = amount / 60 if unit.startswith("секунд") else amount * 60 if unit.startswith("час") else amount
            what = re.sub(r"^(?:о том,? что|что|про|чтобы)\s+", "", m.group(3).strip(), flags=re.I) or "Напоминание"
            return Route("tool", "reminder_create", {"text": what, "minutes": round(minutes, 2)},
                         why="Локальный планировщик")
    m = re.match(r"\s*напомни(?:\s+мне)?\s+через\s+(час|полчаса|минуту)\s*(.*)$", raw, re.I | re.S)
    if m:
        minutes = {"час": 60, "полчаса": 30, "минуту": 1}[m.group(1).casefold()]
        what = re.sub(r"^(?:о том,? что|что|про|чтобы)\s+", "", m.group(2).strip(), flags=re.I) or "Напоминание"
        return Route("tool", "reminder_create", {"text": what, "minutes": minutes}, why="Локальный планировщик")
    m = re.match(r"\s*напомни(?:\s+мне)?\s+в\s+(\d{1,2})[:.](\d{2})\s*(.*)$", raw, re.I | re.S)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        import datetime as _dt
        now_ = _dt.datetime.now()
        due = now_.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if due <= now_:
            due += _dt.timedelta(days=1)
        what = re.sub(r"^(?:о том,? что|что|про|чтобы)\s+", "", m.group(3).strip(), flags=re.I) or "Напоминание"
        return Route("tool", "reminder_create", {"text": what, "minutes": round((due - now_).total_seconds() / 60, 2)},
                     why="Локальный планировщик")
    m = re.match(r"\s*remind me in (\d+) (minute|minutes|hour|hours) (?:to )?(.*)$", raw, re.I)
    if m:
        n = int(m.group(1)) * (60 if m.group(2).startswith("hour") else 1)
        return Route("tool", "reminder_create", {"text": m.group(3) or "Reminder", "minutes": n},
                     why="Local scheduler")

    # --- file search ----------------------------------------------------------------
    m = re.fullmatch(r"(?:найди|поищи|ищи|где (?:лежит|находится)|find|search(?: for)?)\s+(?:мне\s+)?"
                     r"(?:файл\w*|документ\w*|папк\w*|file|files|folder)\s+(.+)", t)
    if m:
        return Route("tool", "search_files", {"query": _clean_query(m.group(1))}, why="Поиск по разрешённым папкам")
    m = re.fullmatch(r"(?:где (?:лежит|лежат|находится)|where is)\s+(.+)", t)
    if m:
        return Route("tool", "search_files", {"query": _clean_query(m.group(1))}, why="Поиск по разрешённым папкам")
    m = re.fullmatch(r"(?:найди|поищи)\s+(.+?)\s+в\s+(документах|загрузках|проектах|изображениях)", t)
    if m:
        folder = {"документах": "~/Documents", "загрузках": "~/Downloads", "проектах": "~/Projects",
                  "изображениях": "~/Pictures"}[m.group(2)]
        return Route("tool", "search_files", {"query": _clean_query(m.group(1)),
                                              "folder": os.path.expanduser(folder)}, why="Поиск в папке")

    # --- open -------------------------------------------------------------------------
    # An exact path («открой файл /tmp/x.txt», «открой ~/Документы/a.pdf») is opened as written — never turned into a
    # search for a mangled name; a missing file is reported as missing.
    pm = re.match(r"\s*(?:открой|покажи|open)\s+(?:мне\s+)?(?:файл\w*\s+|документ\w*\s+|папк\w*\s+|file\s+|folder\s+)?"
                  r"[«\"']?((?:~|/)[^«»\"'\n]+?)[»\"']?\s*[.!]?\s*$", raw, re.I)
    if pm:
        return Route("tool", "open_path", {"path": os.path.expanduser(pm.group(1).strip())}, why="Точный путь")
    m = re.fullmatch(r"(?:открой|запусти|включи|покажи|open|launch|start|run)\s+(.+)", t)
    if m:
        target = re.sub(r"^(?:приложение|программу|app)\s+", "", m.group(1)).strip()
        target = re.sub(r"\s+(?:пожалуйста|please)$", "", target)
        # ordinal reference to the last file list: «открой второй»
        om = re.fullmatch(r"(?:(?:мне )?(?:файл |результат |номер ))?(\S+)(?: (?:файл|результат|документ))?", target)
        last = ctx.get("last_files") or []
        if om and last:
            word = om.group(1)
            idx = len(last) if word in ("последний", "last") else _ordinal(word)
            if idx and 1 <= idx <= len(last):
                return Route("tool", "open_path", {"path": last[idx - 1]}, why="Ссылка на прошлый результат")
        if target in APP_ALIASES:
            return Route("tool", "app_open", {"app": APP_ALIASES[target]}, why="Список разрешённых приложений")
        folder_key = re.sub(r"^(?:папку|папка|folder)\s+", "", target)
        if folder_key in FOLDERS:
            p = Path(os.path.expanduser(FOLDERS[folder_key]))
            return Route("tool", "open_path", {"path": str(p)}, why="Известная папка")
        fm = re.fullmatch(r"(?:файл|документ|file)\s+(.+)", target)
        if fm:
            return Route("search_open", "search_files", {"query": _clean_query(fm.group(1))},
                         why="Поиск файла, затем открытие")
    return None


# Workspaces of the JARVIS window. «Открой Telegram» still opens the Telegram app; the window's own Telegram
# workspace needs «перейди в Telegram» or a word like «раздел» / «вкладку».
UI_VIEWS = (
    ("home", r"главн\w*|домашн\w*(?: экран\w*)?|home"),
    ("chat", r"бесед\w*|чат\w*"),
    ("tasks", r"задач\w*|центр задач"),
    ("history", r"истори\w*(?: действий)?"),
    ("browser", r"браузер\w*"),
    ("telegram", r"телеграм\w*|телег[аеиу]|telegram"),
    ("whatsapp", r"ват[сц]ап\w*|вотсап\w*|вацап\w*|whats ?app"),
    ("mail", r"почт\w*|gmail|джимейл\w*"),
    ("files", r"файл\w*"),
    ("settings", r"настройк\w*"),
    ("comms", r"сообщени\w*|очеред\w* сообщений"),
)
UI_TITLES = {"home": "Главная", "chat": "Беседа", "tasks": "Задачи", "history": "История действий",
             "browser": "Браузер", "telegram": "Telegram", "whatsapp": "WhatsApp", "mail": "Почта", "files": "Файлы",
             "settings": "Настройки", "comms": "Сообщения"}
_UI_NOUN = r"(?:раздел|вкладк[уа]|экран|страниц[уа]|рабочее пространство|пространство)"


def ui_view(t: str) -> str | None:
    """Normalised text → workspace id, or None. Only explicit navigation phrases: never a bare «открой X»."""
    if re.fullmatch(r"(?:домой|на главную|вернись (?:домой|на главную)|go home)", t):
        return "home"
    m = re.fullmatch(r"(?:перейди|вернись|переключись|покажи|открой|go to)(?: мне)?(?: (?:на|в|во|к))? "
                     + _UI_NOUN + r" (.+)", t) \
        or re.fullmatch(r"(?:перейди|вернись)(?: (?:на|в|во|к))? (.+)", t) \
        or re.fullmatch(r"покажи (задачи|историю действий|настройки)", t)
    if not m:
        return None
    name = m.group(1).strip()
    for view, pat in UI_VIEWS:
        if re.fullmatch(pat, name):
            return view
    return None


def _clean_query(q: str) -> str:
    q = re.sub(r"\s+(?:пожалуйста|please)$", "", q.strip())
    q = re.sub(r"^(?:с названием|под названием|named|called)\s+", "", q)
    return q.strip()


# --- several tasks in one request ------------------------------------------------------------------------------
# «Первое, найди папку Колледж. А далее напиши в Телеграме Любимой «привет», и далее найди картинку шуруповёрта.»
# Measured 2026-10-24: such a request went whole to the messaging route, which did only the message — the folder and
# the picture were dropped. Split on explicit sequence words only (never on a bare «и»: «найди файл и открой его» is
# one task), and never inside quotes or the text of a message after «напиши …:».
_SEQ_OPEN = r"во[- ]первых|первое|перво-наперво|первая задача|первым делом|сначала|для начала"
_SEQ_NEXT = (r"во[- ]вторых|в[- ]третьих|в[- ]четв[её]ртых|в[- ]пятых|второе|третье|четв[её]ртое|пятое|"
             r"вторая задача|третья задача|четв[её]ртая задача|следующая задача|далее|дальше|потом|затем|"
             r"после этого|после чего|а после|и наконец|наконец|напоследок|и напоследок|и ещё|а ещё|и еще|а еще|"
             r"а также|также|кроме того")
_SEQ = re.compile(r"(?<![\w-])((?:(?:и|а|но|ну|так)\s+)?)(?:(" + _SEQ_OPEN + r")|(" + _SEQ_NEXT + r"))(?![\w-])"
                  r"|(?:(?<=\s)|^)(\d{1,2})[.)](?=\s)", re.I)
_FRAMING = re.compile(r"задач|слушай|смотри|дослушай|послушай|говорю|скажу|итак|значит|короче|запомни|"
                      r"запоминай|у меня|давай|нужно сделать|надо сделать|сделай вот", re.I)
_LEAD = re.compile(r"^(?:[\s,.;:!?—–-]+|(?:э+(?:-э+)*|м+(?:-м+)*|ну|и|а|так|вот|это|тебе|мне|надо|нужно|нужно будет|будет|"
                   r"задача|пожалуйста)(?![\w-])\s*)", re.I)
_PREP = {"в", "во", "на", "к", "ко", "с", "со", "по", "у", "о", "об", "из", "от", "до", "за", "под", "над", "при",
         "для", "про", "через", "без"}
_SEND_COLON = re.compile(r"\b(?:напиши|отправь|скажи|передай|пошли|сообщи|ответь|черкни|написать|отправить)\b"
                         r"[^.!?:]{0,80}:", re.I)


def _protected(text: str) -> list[tuple[int, int]]:
    spans = [(m.start(), m.end()) for m in re.finditer(r"«[^»]*»|\"[^\"]*\"|“[^”]*”|„[^“”]*[“”]", text)]
    for m in _SEND_COLON.finditer(text):        # the message body: up to the end of its sentence
        end = re.search(r"[.!?…](?:\s|$)", text[m.end():])
        spans.append((m.end(), m.end() + end.end() if end else len(text)))
    return spans


def _clean_part(s: str) -> str:
    s = s.strip()
    while True:
        n = _LEAD.sub("", s, count=1)
        if n == s:
            break
        s = n
    return re.sub(r"[\s,;:—–-]+$", "", s).strip()


def split_tasks(text: str) -> list[str] | None:
    """Several tasks said in a row → the tasks, in order; None for a single request."""
    raw = strip_wake(text or "").strip()
    if len(raw) < 12 or len(raw) > 3000:
        return None
    prot = _protected(raw)
    cuts = []                                   # (start of the marker, end of the marker, is an opening marker)
    for m in _SEQ.finditer(raw):
        st = m.start()
        if any(a <= st < b for a, b in prot):
            continue
        before = raw[:st].rstrip()
        conj = bool(m.group(1))
        if m.group(4) is None and before and not conj and before[-1] not in ".!?;,:—–-":
            continue                            # «найди файлы сначала в загрузках»: not a new task
        cuts.append((st, m.end(), bool(m.group(2)), int(m.group(4)) if m.group(4) else None))
    nums = [c for c in cuts if c[3] is not None]          # «1) … 2) …» counts only as a list numbered from one
    if nums and [c[3] for c in nums] != list(range(1, len(nums) + 1)) or len(nums) == 1:
        cuts = [c for c in cuts if c[3] is None]
    if not cuts:
        return None
    parts = []
    pre = raw[:cuts[0][0]]
    first_open = cuts[0][2]
    if _clean_part(pre) and not (first_open and (_FRAMING.search(pre) or len(pre.split()) <= 2)):
        parts.append(pre)
    for i, (st, en, _, _) in enumerate(cuts):
        parts.append(raw[en:cuts[i + 1][0]] if i + 1 < len(cuts) else raw[en:])
    out: list[str] = []
    for p in parts:
        c = _clean_part(p)
        words = c.split()
        if out and (len(words) < 2 or words[0].lower() in _PREP):
            out[-1] = out[-1] + " " + p.strip()     # «…сначала в загрузках, потом в документах»: one task
            continue
        if c:
            out.append(c)
    out = [_clean_part(x) for x in out if len(_clean_part(x).split()) >= 2]
    return out[:8] if len(out) >= 2 else None
